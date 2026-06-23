"""
core.embedding — провайдер-агностичний embedding-шар.

Перемикач EMBEDDING_PROVIDER (gemini | ollama) читається з .env.
Декаплінг: тули не знають, хто рахує вектор. Повернення на локальне
= зміна одного рядка в .env + нова колекція Qdrant.

task_type:
  RETRIEVAL_DOCUMENT — для записів у память (save)
  RETRIEVAL_QUERY    — для пошукового запиту (query)
Асиметрія покращує якість семантичного пошуку (підтримує Gemini).
ollama task_type ігнорує (bge-m3 симетрична).
"""
from __future__ import annotations

from pathlib import Path

import httpx

_ENV_FILE = Path.home() / ".config" / "mcp-server" / ".env"


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for raw in _ENV_FILE.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


_ENV = _load_env()

_PROVIDER = _ENV.get("EMBEDDING_PROVIDER", "ollama").lower()
_GEMINI_KEY = _ENV.get("GEMINI_API_KEY", "")
_OLLAMA_URL = _ENV.get("OLLAMA_URL", "http://127.0.0.1:11434")

_GEMINI_MODEL = "gemini-embedding-001"
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    f"models/{_GEMINI_MODEL}:embedContent"
)
_GEMINI_DIM = 768

_OLLAMA_MODEL = "bge-m3"


async def _embed_gemini(text: str, task_type: str) -> list[float]:
    if not _GEMINI_KEY:
        raise RuntimeError("503 gemini: GEMINI_API_KEY не заданий у .env")
    body = {
        "model": f"models/{_GEMINI_MODEL}",
        "content": {"parts": [{"text": text}]},
        "taskType": task_type,
        "outputDimensionality": _GEMINI_DIM,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                _GEMINI_URL,
                headers={
                    "x-goog-api-key": _GEMINI_KEY,
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise RuntimeError(f"503 gemini_unavailable: {exc}") from exc
    if r.status_code != 200:
        raise RuntimeError(f"503 gemini HTTP {r.status_code}: {r.text[:200]}")
    vector = r.json().get("embedding", {}).get("values", [])
    if not vector:
        raise RuntimeError(f"503 gemini empty embedding: {str(r.json())[:100]}")
    return vector


async def _embed_ollama(text: str) -> list[float]:
    try:
        async with httpx.AsyncClient(timeout=180.0) as c:
            r = await c.post(
                f"{_OLLAMA_URL}/api/embeddings",
                json={"model": _OLLAMA_MODEL, "prompt": text},
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise RuntimeError(f"503 ollama_unavailable: {exc}") from exc
    if r.status_code != 200:
        raise RuntimeError(f"503 ollama HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    vector = data.get("embedding") or (data.get("embeddings") or [[]])[0]
    if not vector:
        raise RuntimeError(f"503 ollama empty embedding: {str(data)[:100]}")
    return vector


async def embed_text(
    text: str,
    task_type: str = "RETRIEVAL_DOCUMENT",
) -> list[float]:
    """Повертає вектор для тексту через активний провайдер (.env)."""
    if _PROVIDER == "gemini":
        return await _embed_gemini(text, task_type)
    return await _embed_ollama(text)
