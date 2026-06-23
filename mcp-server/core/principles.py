"""
ADR-001..005 як runtime-константи + decorator @no_pilot_principle.
Не конфіг — архітектурні зобов'язання вбудовані у виконання.
"""
from __future__ import annotations

import functools
import inspect
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# ADR константи
# ---------------------------------------------------------------------------

EMBEDDING_MODEL             = "gemini-embedding-001"
EMBEDDING_MODEL_DIGEST   = "sha256-daec91ffb5dd0c27411bd71f29932917c49cf529a641d0168496c3a501e3062c"
EMBEDDING_DIMENSIONS         = 768
SOFT_DELETE_FIELD        = "active"
MVP_MODE                 = True
NO_PILOT_PRINCIPLE       = True

AUDIT_LOG_PATH: Path = Path.home() / "logs" / "mcp-audit.jsonl"

# ---------------------------------------------------------------------------
# Secret masking
# ---------------------------------------------------------------------------

# Маскує hex-рядки ≥32 символів (API ключі, digest-и) у повідомленнях про помилки
_SECRET_RE = re.compile(r"[0-9a-f]{32,}", re.IGNORECASE)


def _mask(text: str) -> str:
    return _SECRET_RE.sub("***REDACTED***", text)


# ---------------------------------------------------------------------------
# Audit log writer
# ---------------------------------------------------------------------------

def _write_audit(record: dict[str, Any]) -> None:
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(AUDIT_LOG_PATH), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# @no_pilot_principle
# ---------------------------------------------------------------------------

def no_pilot_principle(func: Callable) -> Callable:
    """
    Обгортає тул (sync або async):
    — пише запис у AUDIT_LOG_PATH з ts, tool, status, elapsed_ms
    — маскує секрети у полі error
    — не змінює логіку або повернення функції
    """
    semantic_name: str = getattr(func, "semantic_name", func.__name__)

    def _make_record(status: str, elapsed_ms: float, error: str | None = None) -> dict:
        r: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": semantic_name,
            "status": status,
            "elapsed_ms": round(elapsed_ms, 2),
        }
        if error is not None:
            r["error"] = _mask(error)
        return r

    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def _async(*args: Any, **kwargs: Any) -> Any:
            t0 = time.monotonic()
            try:
                result = await func(*args, **kwargs)
                _write_audit(_make_record("ok", (time.monotonic() - t0) * 1000))
                return result
            except Exception as exc:
                _write_audit(_make_record("error", (time.monotonic() - t0) * 1000, str(exc)))
                raise
        _async.semantic_name = semantic_name  # type: ignore[attr-defined]
        return _async

    @functools.wraps(func)
    def _sync(*args: Any, **kwargs: Any) -> Any:
        t0 = time.monotonic()
        try:
            result = func(*args, **kwargs)
            _write_audit(_make_record("ok", (time.monotonic() - t0) * 1000))
            return result
        except Exception as exc:
            _write_audit(_make_record("error", (time.monotonic() - t0) * 1000, str(exc)))
            raise
    _sync.semantic_name = semantic_name  # type: ignore[attr-defined]
    return _sync
