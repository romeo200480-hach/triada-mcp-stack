"""Розумний роутер: код→DeepSeek, аналіз→Gemini, важливе/неоднозначне→рада."""
import asyncio
import json

import httpx

_ENV = "/home/mac/.config/litellm/.env"
_URL = "http://127.0.0.1:4000/v1/chat/completions"

_CODE_KEYWORDS = ["код","python","bash","функція","функцію","баг","помилка","script","скрипт","sql","регулярка","regex","клас","масив","цикл","змінна","дебаг","debug","traceback"]
_ANALYSIS_KEYWORDS = ["поясни","чому","проаналізуй","аналіз","порівняй","порівняння","думка","вважаєш","переклади","переклад","опиши","розкажи","що краще","як краще","сенс","значення"]
_IMPORTANCE_KEYWORDS = ["критично","важливо","перевір","архітектура","архітектуру","ризик","безпека","вирішення","стратегія","терміново"]
_COUNCIL_FLAG = "рада:"


def _master_key() -> str:
    for line in open(_ENV):
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.strip().split("=", 1)[1]
    return ""


def route_request(prompt: str) -> dict:
    text = prompt.lower().strip()
    if text.startswith(_COUNCIL_FLAG):
        return {"mode": "council"}
    if any(word in text for word in _IMPORTANCE_KEYWORDS):
        return {"mode": "council"}
    has_code = any(word in text for word in _CODE_KEYWORDS)
    has_analysis = any(word in text for word in _ANALYSIS_KEYWORDS)
    if has_code and not has_analysis:
        return {"mode": "single", "model": "deepseek"}
    if has_analysis and not has_code:
        return {"mode": "single", "model": "gemini"}
    return {"mode": "council"}


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


async def _ask_with_fallback(group: str, question: str) -> str:
    fallback = "smart" if group == "deepseek" else "deepseek"
    result = await _ask(group, question)
    if result.startswith("ERROR:"):
        print(f"[router] {group} впав, фолбек на {fallback}")
        return await _ask(fallback, question)
    return result


async def ask_council(question: str) -> str:
    """Розумний роутер: код→DeepSeek, аналіз→Gemini, важливе/неоднозначне→рада."""
    route = route_request(question)
    if route["mode"] == "single":
        group = "smart" if route["model"] == "gemini" else "deepseek"
        return await _ask_with_fallback(group, question)
    a, b = await asyncio.gather(_ask("smart", question), _ask("deepseek", question))
    return json.dumps({"gemini": a, "deepseek": b}, ensure_ascii=False)
