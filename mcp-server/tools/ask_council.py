"""ask_council — віяло питання у кілька моделей через LiteLLM."""
import asyncio
import json

import httpx

_ENV = "/home/mac/.config/litellm/.env"
_URL = "http://127.0.0.1:4000/v1/chat/completions"


def _master_key() -> str:
    for line in open(_ENV):
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.strip().split("=", 1)[1]
    return ""


async def _ask(model: str, question: str) -> str:
    headers = {"Authorization": "Bearer " + _master_key()}
    payload = {"model": model, "messages": [{"role": "user", "content": question}]}
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(_URL, headers=headers, json=payload)
            d = r.json()
            return d["choices"][0]["message"]["content"]
        except Exception as exc:
            if attempt == 2:
                return "ERROR: " + str(exc)[:300]
            await asyncio.sleep(2 * (attempt + 1))


async def ask_council(question: str) -> str:
    """Опитує раду моделей (Gemini і DeepSeek) паралельно, повертає JSON їхніх сирих відповідей."""
    a, b = await asyncio.gather(_ask("smart", question), _ask("deepseek", question))
    return json.dumps({"gemini": a, "deepseek": b}, ensure_ascii=False)
