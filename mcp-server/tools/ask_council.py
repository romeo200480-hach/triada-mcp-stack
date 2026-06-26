"""Каскадний роутер ask_council: keyword-логіка спереду, Gemini-диригент на сірій зоні.
Явний код/аналіз і свідома рада -> миттєво (0 викликів диригента).
Лише невизначена сіра зона -> 1 виклик диригента (free Gemini REST, structured JSON).
Диригент впав/timeout -> рада (безпечний відкат)."""
import asyncio
import json
import os

import httpx

# --- LiteLLM (рада) ---
_LITELLM_URL = "http://127.0.0.1:4000/v1/chat/completions"
_LITELLM_ENV = "/home/mac/.config/litellm/.env"

# --- Gemini-диригент (прямий REST, безкоштовний ключ розробника) ---
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_CONDUCTOR_MODEL = "gemini-2.5-flash"   # свап-константа; якщо 404 -> інша flash-модель
_CONDUCTOR_TIMEOUT = 8                   # сек; перевищення -> відкат на раду
_CONFIDENCE_THRESHOLD = 0.7              # single з нижчою впевненістю -> ескалація в раду
_GEMINI_ENV_FILES = [
    "/home/mac/.config/mcp-server/.env",
    "/home/mac/.config/litellm/.env",
]

# --- keyword-стадія (Етап 1, запасний парашут) ---
_CODE_KEYWORDS = ["код","python","bash","функція","функцію","баг","помилка","script","скрипт","sql","регулярка","regex","клас","масив","цикл","змінна","дебаг","debug","traceback"]
_ANALYSIS_KEYWORDS = ["поясни","чому","проаналізуй","аналіз","порівняй","порівняння","думка","вважаєш","переклади","переклад","опиши","розкажи","що краще","як краще","сенс","значення"]
_IMPORTANCE_KEYWORDS = ["критично","важливо","перевір","архітектура","архітектуру","ризик","безпека","вирішення","стратегія","терміново"]
_COUNCIL_FLAG = "рада:"

_CONDUCTOR_SYSTEM = """Ти — маршрутизатор запитів. Тобі дають текст запиту користувача.
Єдина задача — вирішити, якій моделі його віддати, і повернути результат структуровано.

Маршрути:
- single / deepseek — код, дебаг, рефакторинг, написання скриптів, формальна логіка, математика, SQL, регулярні вирази.
- single / gemini — аналіз, пояснення, міркування, переклад, багатомовність, довгий текст, загальні знання, креатив.
- council — коли: запит змішаний (потрібен і код, і змістовний аналіз); або високі ставки (критично, архітектура, безпека, фінанси, юридичне, незворотне); або ти не впевнений, що одна модель упорається.

confidence — число 0.0-1.0, наскільки ти впевнений, що маршрут правильний і достатній. Вагаєшся між single і council — став нижчу впевненість; система сама ескалює до ради."""

_CONDUCTOR_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "mode": {"type": "STRING", "enum": ["single", "council"]},
        "model": {"type": "STRING", "enum": ["gemini", "deepseek"]},
        "confidence": {"type": "NUMBER"},
    },
    "required": ["mode", "confidence"],
}


def _read_gemini_key() -> str:
    val = os.environ.get("GEMINI_API_KEY")
    if val:
        return val
    for path in _GEMINI_ENV_FILES:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GEMINI_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except FileNotFoundError:
            continue
    return ""


def _master_key() -> str:
    for line in open(_LITELLM_ENV):
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.strip().split("=", 1)[1]
    return ""


def _route_by_keywords(prompt: str) -> dict:
    text = prompt.lower().strip()
    if text.startswith(_COUNCIL_FLAG):
        return {"mode": "council", "reason": "flag"}
    if any(word in text for word in _IMPORTANCE_KEYWORDS):
        return {"mode": "council", "reason": "importance"}
    has_code = any(word in text for word in _CODE_KEYWORDS)
    has_analysis = any(word in text for word in _ANALYSIS_KEYWORDS)
    if has_code and not has_analysis:
        return {"mode": "single", "model": "deepseek", "reason": "code"}
    if has_analysis and not has_code:
        return {"mode": "single", "model": "gemini", "reason": "analysis"}
    return {"mode": "council", "reason": "ambiguous"}


def _validate_decision(d):
    if not isinstance(d, dict):
        return None
    if d.get("mode") == "council":
        return {"mode": "council"}
    if d.get("mode") == "single" and d.get("model") in ("gemini", "deepseek"):
        try:
            conf = float(d.get("confidence", 1.0))
        except (TypeError, ValueError):
            conf = 1.0
        return {"mode": "single", "model": d["model"], "confidence": conf}
    return None


async def _ask_conductor(prompt: str):
    key = _read_gemini_key()
    if not key:
        print("[router] GEMINI_API_KEY не знайдено -> диригента пропущено")
        return None
    url = f"{_GEMINI_BASE}/models/{_CONDUCTOR_MODEL}:generateContent"
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    payload = {
        "systemInstruction": {"parts": [{"text": _CONDUCTOR_SYSTEM}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _CONDUCTOR_SCHEMA,
            "temperature": 0,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=_CONDUCTOR_TIMEOUT) as client:
            r = await client.post(url, headers=headers, json=payload)
        raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return _validate_decision(json.loads(raw))
    except Exception as exc:
        print(f"[router] диригент впав ({exc}) -> відкат на раду")
        return None


async def route_request(prompt: str) -> dict:
    base = _route_by_keywords(prompt)
    if base["reason"] in ("code", "analysis"):
        return base
    if base["reason"] in ("flag", "importance"):
        return {"mode": "council"}
    decision = await _ask_conductor(prompt)
    if decision is None:
        return {"mode": "council"}
    if decision["mode"] == "single" and decision.get("confidence", 0.0) < _CONFIDENCE_THRESHOLD:
        return {"mode": "council"}
    return decision


async def _ask(model: str, question: str) -> str:
    headers = {"Authorization": "Bearer " + _master_key()}
    payload = {"model": model, "messages": [{"role": "user", "content": question}]}
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(_LITELLM_URL, headers=headers, json=payload)
                d = r.json()
                return d["choices"][0]["message"]["content"]
        except Exception as exc:
            if attempt == 2:
                return "ERROR: " + str(exc)[:300]
            await asyncio.sleep(2 * (attempt + 1))


async def _ask_with_fallback(group: str, question: str) -> str:
    fallback = "smart" if group == "deepseek" else "deepseek"
    result = await _ask(group, question)
    if result.startswith("ERROR:"):
        print(f"[router] {group} впав, фолбек на {fallback}")
        return await _ask(fallback, question)
    return result


async def ask_council(question: str) -> str:
    """Каскадний роутер: код->DeepSeek, аналіз->Gemini, змішане/важливе/невизначене->рада (Gemini-диригент на сірій зоні)."""
    route = await route_request(question)
    if route["mode"] == "single":
        group = "smart" if route["model"] == "gemini" else "deepseek"
        return await _ask_with_fallback(group, question)
    a, b = await asyncio.gather(_ask("smart", question), _ask("deepseek", question))
    return json.dumps({"gemini": a, "deepseek": b}, ensure_ascii=False)
