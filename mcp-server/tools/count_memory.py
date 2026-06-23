from __future__ import annotations
from datetime import datetime, timezone, timedelta
import httpx
from .query_local_memory import _QDRANT_URL, _COLLECTION


async def count_memory(
    tag: str = "",
    category: str = "",
    days: int = 0,
    include_inactive: bool = False,
) -> str:
    """Рахує кількість записів памʼяті за фільтром (без вектора). tag, category, days, include_inactive. Повертає число."""
    must = []
    if not include_inactive:
        must.append({"key": "active", "match": {"value": True}})
    if tag:
        must.append({"key": "tag", "match": {"value": tag}})
    if category:
        must.append({"key": "category", "match": {"value": category}})
    if days and days > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        must.append({"key": "created_at", "range": {"gte": since}})
    body = {"exact": True}
    if must:
        body["filter"] = {"must": must}
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{_QDRANT_URL}/collections/{_COLLECTION}/points/count", json=body)
    except httpx.ConnectError as exc:
        raise RuntimeError(f"502 qdrant_unavailable: {exc}") from exc
    if r.status_code != 200:
        raise RuntimeError(f"502 qdrant HTTP {r.status_code}: {r.text[:200]}")
    return str(r.json().get("result", {}).get("count", 0))
