"""
health_check_all — паралельна перевірка 7 компонентів системи.
Повертає JSON: timestamp, overall_status, components, warnings.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from core import HealthStatus, no_pilot_principle

# ---------------------------------------------------------------------------
# Константи
# ---------------------------------------------------------------------------

_QDRANT_URL  = "http://127.0.0.1:6333"
_OLLAMA_URL  = "http://127.0.0.1:11434"
_CF_METRICS  = "http://127.0.0.1:20241/metrics"
_N8N_HOST    = ("127.0.0.1", 5678)
_EVENT_LOG   = Path.home() / "logs" / "qdrant-events.jsonl"
_BGE_M3      = "bge-m3"

# ---------------------------------------------------------------------------
# Окремі checker-и
# ---------------------------------------------------------------------------

async def _check_qdrant() -> HealthStatus:
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{_QDRANT_URL}/healthz")
        ms = round((time.monotonic() - t0) * 1000, 1)
        if r.status_code == 200:
            return HealthStatus("qdrant", "ok", latency_ms=ms,
                                details={"body": r.text.strip()})
        return HealthStatus("qdrant", "degraded", latency_ms=ms,
                            error=f"HTTP {r.status_code}")
    except Exception as exc:
        return HealthStatus("qdrant", "down", error=str(exc))


async def _check_ollama() -> HealthStatus:
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{_OLLAMA_URL}/api/tags")
        ms = round((time.monotonic() - t0) * 1000, 1)
        if r.status_code != 200:
            return HealthStatus("ollama", "degraded", latency_ms=ms,
                                error=f"HTTP {r.status_code}")
        models = [m.get("name", "") for m in r.json().get("models", [])]
        has_bge = any(_BGE_M3 in m for m in models)
        return HealthStatus(
            "ollama",
            "ok" if has_bge else "degraded",
            latency_ms=ms,
            details={"models_count": len(models), "bge_m3_present": has_bge},
            error=None if has_bge else f"{_BGE_M3} not found in loaded models",
        )
    except Exception as exc:
        return HealthStatus("ollama", "down", error=str(exc))


async def _check_n8n() -> HealthStatus:
    t0 = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(*_N8N_HOST), timeout=1.0
        )
        ms = round((time.monotonic() - t0) * 1000, 1)
        writer.close()
        await writer.wait_closed()
        return HealthStatus("n8n_local", "ok", latency_ms=ms)
    except asyncio.TimeoutError:
        return HealthStatus("n8n_local", "down", error="TCP timeout 1s")
    except ConnectionRefusedError:
        return HealthStatus("n8n_local", "down", error="connection refused :5678")
    except Exception as exc:
        return HealthStatus("n8n_local", "down", error=str(exc))


async def _check_cloudflare() -> HealthStatus:
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(_CF_METRICS)
        ms = round((time.monotonic() - t0) * 1000, 1)
        if r.status_code == 200:
            return HealthStatus("cloudflare_tunnel", "ok", latency_ms=ms,
                                details={"metrics_bytes": len(r.content)})
        return HealthStatus("cloudflare_tunnel", "degraded", latency_ms=ms,
                            error=f"HTTP {r.status_code}")
    except Exception as exc:
        return HealthStatus("cloudflare_tunnel", "down", error=str(exc))


async def _check_tailscale() -> HealthStatus:
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        ms = round((time.monotonic() - t0) * 1000, 1)
        data = json.loads(stdout)
        backend = data.get("BackendState", "unknown")
        ip = (data.get("TailscaleIPs") or ["?"])[0]
        ok = backend == "Running"
        return HealthStatus(
            "tailscale",
            "ok" if ok else "degraded",
            latency_ms=ms,
            details={"BackendState": backend, "ip": ip},
            error=None if ok else f"BackendState={backend}",
        )
    except Exception as exc:
        return HealthStatus("tailscale", "down", error=str(exc))


async def _check_event_log() -> HealthStatus:
    try:
        if not _EVENT_LOG.exists():
            return HealthStatus(
                "event_log", "degraded",
                details={"exists": False, "path": str(_EVENT_LOG)},
                error="file not found — no upserts recorded yet",
            )
        size = _EVENT_LOG.stat().st_size
        lines = sum(1 for _ in _EVENT_LOG.open())
        return HealthStatus(
            "event_log", "ok",
            details={"exists": True, "size_bytes": size,
                     "lines": lines, "path": str(_EVENT_LOG)},
        )
    except Exception as exc:
        return HealthStatus("event_log", "degraded", error=str(exc))


async def _check_mcp_self() -> HealthStatus:
    t0 = time.monotonic()
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        uptime_s = round(time.time() - proc.create_time())
        mem = proc.memory_info()
        ms = round((time.monotonic() - t0) * 1000, 1)
        return HealthStatus(
            "mcp_self", "ok",
            latency_ms=ms,
            details={
                "pid": proc.pid,
                "uptime_seconds": uptime_s,
                "rss_mb": round(mem.rss / 1_048_576, 1),
            },
        )
    except Exception as exc:
        return HealthStatus("mcp_self", "degraded", error=str(exc))


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@no_pilot_principle
async def _check_qdrant_memory() -> HealthStatus:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get("http://127.0.0.1:6333/collections/roman_memory_v1")
        info = r.json().get("result", {})
        points = info.get("points_count") or 0
        vec_mb = round(points * 768 * 4 / 1048576, 2)
        budget = 2000
        pct = round(points / budget * 100, 1)
        return HealthStatus(
            "qdrant_memory", "ok",
            details={"points": points, "vectors_mb": vec_mb,
                     "budget_points": budget, "fill_percent": pct},
        )
    except Exception as exc:
        return HealthStatus("qdrant_memory", "down", error=str(exc))


async def health_check_all() -> str:
    """
    Паралельна перевірка 7 компонентів: qdrant, ollama, n8n_local,
    cloudflare_tunnel, tailscale, event_log, mcp_self.
    overall_status: healthy (всі ok) | degraded (хоч один не ok).
    """
    raw = await asyncio.gather(
        _check_qdrant(),
        _check_qdrant_memory(),
        _check_ollama(),
        _check_n8n(),
        _check_cloudflare(),
        _check_tailscale(),
        _check_event_log(),
        _check_mcp_self(),
        return_exceptions=True,
    )

    components: dict[str, dict] = {}
    warnings: list[str] = []

    for r in raw:
        hs: HealthStatus = (
            HealthStatus("unknown_checker", "down", error=str(r))
            if isinstance(r, BaseException)
            else r
        )
        entry: dict = {"status": hs.status}
        if hs.latency_ms is not None:
            entry["latency_ms"] = hs.latency_ms
        if hs.details:
            entry["details"] = hs.details
        if hs.error is not None:
            entry["error"] = hs.error
        components[hs.component] = entry

        if hs.status != "ok":
            msg = f"{hs.component}: {hs.status}"
            if hs.error:
                msg += f" — {hs.error}"
            warnings.append(msg)

    return json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "overall_status": "healthy" if not warnings else "degraded",
        "components": components,
        "warnings": warnings,
    }, ensure_ascii=False)
