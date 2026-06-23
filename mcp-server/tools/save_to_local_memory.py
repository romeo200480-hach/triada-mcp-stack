"""
save_to_local_memory — записує текст у Qdrant з bge-m3 embedding.
Послідовність: embed → upsert → log. Лог пишеться ТІЛЬКИ після upsert.
Помилки: ollama↓ → RuntimeError (503), qdrant↓ → RuntimeError (502).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from core.embedding import embed_text

from core import (
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_DIGEST,
    Memory,
    no_pilot_principle,
)

_QDRANT_URL  = "http://127.0.0.1:6333"
_OLLAMA_URL  = "http://127.0.0.1:11434"
_COLLECTION  = "roman_memory_v1"
_EVENTS_FILE = Path.home() / "logs" / "qdrant-events.jsonl"
_EVENTS_MAX  = 100 * 1024 * 1024  # 100 MB


def _rotate(path: Path) -> None:
    if not path.exists() or path.stat().st_size < _EVENTS_MAX:
        return
    for i in range(2, 0, -1):
        src = path.parent / f"{path.name}.{i - 1}" if i > 1 else path
        dst = path.parent / f"{path.name}.{i}"
        if src.exists():
            src.rename(dst)


def _append(path: Path, record: dict) -> None:
    _rotate(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


@no_pilot_principle
async def save_to_local_memory(
    text: str,
    category: str = "general",
    source_agent: str = "unknown",
    structured_metadata: dict[str, Any] | None = None,
) -> str:
    """
    Записує текст у Qdrant roman_memory_v1 з bge-m3 embedding.
    category: general|audit|note|conversation|system_event
    Послідовність: embed → upsert → log.
    """
    model_tag = f"{EMBEDDING_MODEL}@{EMBEDDING_MODEL_DIGEST}"

    # Step 1: embedding через активний провайдер (.env)
    vector = await embed_text(text, "RETRIEVAL_DOCUMENT")

    # Step 2: Memory object з core.schema
    mem = Memory(
        text=text,
        category=category,
        source_agent=source_agent,
        embedding=vector,
        embedding_model=model_tag,
        structured_metadata=structured_metadata or {},
    )

    # Step 3: Qdrant UPSERT — помилка → RuntimeError 502, event log не чіпаємо
    point = {
        "id": mem.id,
        "vector": vector,
        "payload": mem.to_qdrant_payload(),
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.put(
                f"{_QDRANT_URL}/collections/{_COLLECTION}/points",
                json={"points": [point]},
            )
    except httpx.ConnectError as exc:
        raise RuntimeError(f"502 qdrant_unavailable: {exc}") from exc

    if r.status_code not in (200, 201):
        raise RuntimeError(f"502 qdrant HTTP {r.status_code}: {r.text[:200]}")

    # Step 4: Event log — тільки після підтвердженого upsert
    _append(_EVENTS_FILE, {
        "ts": mem.created_at,
        "action": "upsert",
        "collection": _COLLECTION,
        "id": mem.id,
        "payload": mem.to_qdrant_payload(),
        "model": model_tag,
    })

    return json.dumps({"status": "ok", "id": mem.id}, ensure_ascii=False)
