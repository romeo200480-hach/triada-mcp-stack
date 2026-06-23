"""Eidos gateway bot — крок 7c: Gemini + MCP, ручна конвертація схем.

Обхід бага google-genai: схеми MCP-інструментів чистяться від
additionalProperties / anyOf, які ламають конвертер Gemini.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any
from collections import deque

from dotenv import load_dotenv
from google import genai
from google.genai import types
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv(Path(__file__).parent / ".env")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MCP_URL = os.environ["MCP_URL"]
MCP_API_KEY = os.environ["MCP_API_KEY"]

MCP_ENDPOINT = f"{MCP_URL}/mcp"
MCP_HEADERS = {"X-MCP-API-Key": MCP_API_KEY}
MODEL = "gemini-2.5-flash"
SYSTEM_PROMPT = (
    "Ти Eidos — особистий технічний асистент Романа в Telegram. "
    "Маєш інструменти стека: health_check_all, system_info, audit_local_ports, "
    "save_to_local_memory, query_local_memory. "
    "Викликай інструмент, коли питання його стосується. "
    "Відповідай стисло, українською, без підлещування."
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("aiden-bot")

client = genai.Client(api_key=GEMINI_API_KEY)

HISTORY: deque = deque(maxlen=16)  # 8 останніх обмінів user+model

_ALLOWED_KEYS = {"type", "properties", "items", "required", "enum", "description"}


async def reply_long(message, text):
    limit = 4096
    if not text:
        text = "(порожня відповідь)"
    for i in range(0, len(text), limit):
        await message.reply_text(text[i:i + limit])


def _clean_schema(node: Any) -> Any:
    """Рекурсивно чистить JSON-схему до того, що приймає Gemini."""
    if not isinstance(node, dict):
        return node
    # anyOf -> беремо першу не-null гілку
    if "anyOf" in node:
        for branch in node["anyOf"]:
            if isinstance(branch, dict) and branch.get("type") != "null":
                return _clean_schema(branch)
        return {"type": "string"}
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k not in _ALLOWED_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _clean_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _clean_schema(v)
        else:
            out[k] = v
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    return out


def _is_allowed(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == ALLOWED_USER_ID


async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await update.message.reply_text("Доступ заборонено.")
        return
    await update.message.reply_text("Eidos на Gemini + MCP. Питай.")


async def _run_agent(user_text: str, media_part=None) -> str:
    async with streamablehttp_client(MCP_ENDPOINT, headers=MCP_HEADERS) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = await session.list_tools()

            decls = []
            for t in mcp_tools.tools:
                decls.append(
                    types.FunctionDeclaration(
                        name=t.name,
                        description=t.description or "",
                        parameters=_clean_schema(t.inputSchema),
                    )
                )
            gemini_tools = [types.Tool(function_declarations=decls)]

            contents = list(HISTORY) + [
                types.Content(role="user", parts=([media_part] if media_part else []) + [types.Part(text=user_text)])
            ]

            # цикл: модель може попросити інструмент, ми викликаємо, повертаємо
            for _ in range(5):
                for attempt in range(3):
                    try:
                        resp = await client.aio.models.generate_content(
                            model=MODEL,
                            contents=contents,
                            config=types.GenerateContentConfig(
                                system_instruction=SYSTEM_PROMPT,
                                tools=gemini_tools,
                            ),
                        )
                        break
                    except genai.errors.ServerError:
                        if attempt == 2:
                            log.error("Gemini 503: усі спроби вичерпано")
                            return "⚠️ Gemini зараз перевантажений (503). Спробуй ще раз за хвилину."
                        log.warning("Gemini 503, retry %d", attempt + 1)
                        await asyncio.sleep(2 * (attempt + 1))

                cand = resp.candidates[0]
                calls = [p.function_call for p in cand.content.parts if p.function_call]
                log.info("DEBUG parts=%s calls=%s", len(cand.content.parts), len(calls))
                if not calls:
                    answer = (resp.text or "").strip() or "(порожня відповідь)"
                    HISTORY.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
                    HISTORY.append(types.Content(role="model", parts=[types.Part(text=answer)]))
                    return answer
                contents.append(cand.content)
                for fc in calls:
                    log.info("tool call: %s args=%s", fc.name, dict(fc.args or {}))
                    result = await session.call_tool(fc.name, dict(fc.args or {}))
                    text_out = "".join(
                        c.text for c in result.content if hasattr(c, "text")
                    )
                    contents.append(
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_function_response(
                                    name=fc.name,
                                    response={"result": text_out[:4000]},
                                )
                            ],
                        )
                    )

            return "(перевищено ліміт викликів інструментів)"


async def on_message(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        log.warning("denied from user_id=%s", update.effective_user.id)
        return
    user_text = update.message.text or ""
    log.info("got: %s", user_text[:80])
    await update.message.chat.send_action("typing")
    try:
        answer = await _run_agent(user_text)
    except Exception as exc:  # noqa: BLE001
        log.exception("agent error")
        await update.message.reply_text(f"Помилка: {exc}")
        return
    await reply_long(update.message, answer)


async def on_voice(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    await update.message.chat.send_action("typing")
    try:
        tg_file = await update.message.voice.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        part = types.Part.from_bytes(data=data, mime_type="audio/ogg")
        answer = await _run_agent(
            "Голосове повідомлення. Розпізнай мову і відповідай по суті.",
            media_part=part,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("voice error")
        await update.message.reply_text(f"Помилка: {exc}")
        return
    await reply_long(update.message, answer)


async def on_photo(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    await update.message.chat.send_action("typing")
    try:
        tg_file = await update.message.photo[-1].get_file()
        data = bytes(await tg_file.download_as_bytearray())
        part = types.Part.from_bytes(data=data, mime_type="image/jpeg")
        caption = update.message.caption or "Фото. Опиши вміст; якщо є текст — витягни його."
        answer = await _run_agent(caption, media_part=part)
    except Exception as exc:  # noqa: BLE001
        log.exception("photo error")
        await update.message.reply_text(f"Помилка: {exc}")
        return
    await reply_long(update.message, answer)


async def cmd_weather(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await update.message.reply_text("Доступ заборонено.")
        return
    import urllib.request
    def _hit():
        try:
            urllib.request.urlopen("http://localhost:5678/webhook/weather-now", timeout=10).read()
        except Exception as e:
            log.warning("weather webhook failed: %s", e)
    await asyncio.to_thread(_hit)


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    log.info("starting aiden-bot (gemini+mcp manual) polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
