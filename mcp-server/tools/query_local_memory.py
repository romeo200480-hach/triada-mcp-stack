"""
query_local_memory — семантичний пошук у Qdrant roman_memory_v1.
Embed query → search → повернути ranked results.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta

import json
from typing import Any

import httpx
from core.embedding import embed_text

from core import EMBEDDING_MODEL, no_pilot_principle

_QDRANT_URL = "http://127.0.0.1:6333"
_OLLAMA_URL = "http://127.0.0.1:11434"
_COLLECTION = "roman_memory_v1"


@no_pilot_principle
async def query_local_memory(
    query: str,
    limit: int = 5,
    include_inactive: bool = False,
    tag: str = "",
    category: str = "",
    date: str = "",
    days: int = 0,
) -> str:
    """
    Семантичний пошук у локальній пам'яті Qdrant.
    include_inactive=False повертає тільки active=true записи (default).
    Повертає JSON list: [{id, score, payload}, ...].
    """
    # Step 1: embedding запиту через активний провайдер (.env)
    vector = await embed_text(query, "RETRIEVAL_QUERY")

    # Step 2: Qdrant search
    body: dict[str, Any] = {
        "vector": vector,
        "limit": limit,
        "with_payload": True,
    }
    must = []
    if not include_inactive:
        must.append({"key": "active", "match": {"value": True}})
    if tag:
        must.append({"key": "tag", "match": {"value": tag}})
    if category:
        must.append({"key": "category", "match": {"value": category}})
    if date:
        must.append({"key": "date", "match": {"value": date}})
    if days and days > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        must.append({"key": "created_at", "range": {"gte": since}})
    if must:
        body["filter"] = {"must": must}

    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"{_QDRANT_URL}/collections/{_COLLECTION}/points/search",
                json=body,
            )
    except httpx.ConnectError as exc:
        raise RuntimeError(f"502 qdrant_unavailable: {exc}") from exc

    if r.status_code != 200:
        raise RuntimeError(f"502 qdrant HTTP {r.status_code}: {r.text[:200]}")

    hits = r.json().get("result", [])
    results = [
        {"id": h["id"], "score": round(h["score"], 6), "payload": h.get("payload", {})}
        for h in hits
    ]

    return json.dumps(results, ensure_ascii=False)
