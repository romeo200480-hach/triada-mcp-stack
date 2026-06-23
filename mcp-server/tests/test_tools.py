"""
tests/test_tools.py — unit tests for MCP tools and HTTP routes.
Run: pytest tests/test_tools.py -v
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_KEY = "a" * 64
FAKE_VECTOR = [0.1] * 1024


def _ollama_ok_response():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"embedding": FAKE_VECTOR}
    return mock


def _qdrant_ok_response(status=200):
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = {"result": {"operation_id": 1, "status": "completed"}}
    return mock


def _qdrant_search_response(hits: list):
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"result": hits}
    return mock


# ---------------------------------------------------------------------------
# 1. health_check_all returns all 7 component keys
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_check_all_returns_all_components():
    import asyncio
    from tools.health_check_all import health_check_all

    # Mock asyncio.create_subprocess_exec for tailscale check
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(
        return_value=(b'{"BackendState":"Running","TailscaleIPs":["100.0.0.1"]}', b"")
    )
    mock_proc.returncode = 0

    async def _fake_get(url, **kw):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"models": [{"name": "bge-m3"}]}
        m.text = ""
        return m

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=False)
    cm.get = _fake_get

    with patch("tools.health_check_all.httpx.AsyncClient", return_value=cm), \
         patch("asyncio.create_subprocess_exec", return_value=mock_proc):

        result = await health_check_all()

    data = json.loads(result)
    assert "overall_status" in data
    assert "components" in data
    expected = {"qdrant", "ollama", "n8n_local", "cloudflare_tunnel", "tailscale", "event_log", "mcp_self"}
    assert expected == set(data["components"].keys())


# ---------------------------------------------------------------------------
# 2. save_to_local_memory — valid input → status ok, id returned
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_to_local_memory_valid():
    from tools.save_to_local_memory import save_to_local_memory

    with patch("tools.save_to_local_memory.httpx.AsyncClient") as mock_cls, \
         patch("tools.save_to_local_memory._append") as mock_append:

        call_count = 0

        async def _fake_post(url, **kw):
            nonlocal call_count
            call_count += 1
            return _ollama_ok_response()

        async def _fake_put(url, **kw):
            return _qdrant_ok_response(200)

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=cm)
        cm.__aexit__ = AsyncMock(return_value=False)
        cm.post = _fake_post
        cm.put = _fake_put
        mock_cls.return_value = cm

        result = await save_to_local_memory(
            text="test memory entry",
            category="note",
            source_agent="test",
        )

    data = json.loads(result)
    assert data["status"] == "ok"
    assert "id" in data
    mock_append.assert_called_once()


# ---------------------------------------------------------------------------
# 3. save_to_local_memory — ollama down → RuntimeError 503, nothing written
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_to_local_memory_ollama_down():
    import httpx
    from tools.save_to_local_memory import save_to_local_memory

    with patch("tools.save_to_local_memory.httpx.AsyncClient") as mock_cls, \
         patch("tools.save_to_local_memory._append") as mock_append:

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=cm)
        cm.__aexit__ = AsyncMock(return_value=False)
        cm.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        mock_cls.return_value = cm

        with pytest.raises(RuntimeError, match="503"):
            await save_to_local_memory(text="x")

    mock_append.assert_not_called()


# ---------------------------------------------------------------------------
# 4. save_to_local_memory — qdrant down → RuntimeError 502, event log not touched
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_to_local_memory_qdrant_down_no_event_log():
    import httpx
    from tools.save_to_local_memory import save_to_local_memory

    with patch("tools.save_to_local_memory.httpx.AsyncClient") as mock_cls, \
         patch("tools.save_to_local_memory._append") as mock_append:

        post_cm = AsyncMock()
        post_cm.__aenter__ = AsyncMock(return_value=post_cm)
        post_cm.__aexit__ = AsyncMock(return_value=False)
        post_cm.post = AsyncMock(return_value=_ollama_ok_response())
        post_cm.put = AsyncMock(side_effect=httpx.ConnectError("qdrant down"))
        mock_cls.return_value = post_cm

        with pytest.raises(RuntimeError, match="502"):
            await save_to_local_memory(text="x")

    mock_append.assert_not_called()


# ---------------------------------------------------------------------------
# 5. query_local_memory — only active=true records returned by default
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_query_local_memory_only_active():
    from tools.query_local_memory import query_local_memory

    hits = [
        {"id": "aaa", "score": 0.9, "payload": {"text": "active", "active": True}},
    ]

    with patch("tools.query_local_memory.httpx.AsyncClient") as mock_cls:
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=cm)
        cm.__aexit__ = AsyncMock(return_value=False)
        cm.post = AsyncMock(side_effect=[
            _ollama_ok_response(),
            _qdrant_search_response(hits),
        ])
        mock_cls.return_value = cm

        result = await query_local_memory(query="test", limit=5)

    data = json.loads(result)
    assert len(data) == 1
    assert data[0]["id"] == "aaa"


# ---------------------------------------------------------------------------
# 6. _APIKeyMiddleware — missing key → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_key_middleware_unauthorized():
    import server

    original_key = server.MCP_API_KEY
    server.MCP_API_KEY = FAKE_KEY
    try:
        responses = []

        async def mock_send(message):
            responses.append(message)

        scope = {
            "type": "http",
            "headers": [],
            "path": "/mcp",
            "method": "GET",
            "client": ("127.0.0.1", 9999),
        }

        inner_called = []

        async def inner_app(s, r, snd):
            inner_called.append(True)

        mw = server._APIKeyMiddleware(inner_app)
        await mw(scope, AsyncMock(), mock_send)

        assert any(r.get("status") == 401 for r in responses if r.get("type") == "http.response.start")
        assert not inner_called
    finally:
        server.MCP_API_KEY = original_key


# ---------------------------------------------------------------------------
# 7. /dashboard — returns HTML with 200
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dashboard_returns_html():
    import server

    original_key = server.MCP_API_KEY
    server.MCP_API_KEY = FAKE_KEY
    try:
        request = MagicMock()
        request.headers.get.side_effect = lambda k, d="": FAKE_KEY if k == "X-MCP-API-Key" else d
        request.query_params.get.side_effect = lambda k, d="": d

        dashboard_html = Path(__file__).parent.parent / "web" / "dashboard.html"
        assert dashboard_html.exists(), "web/dashboard.html must exist"

        resp = await server._dashboard(request)
        assert resp.status_code == 200
        assert "text/html" in resp.media_type
        assert FAKE_KEY in resp.body.decode()
    finally:
        server.MCP_API_KEY = original_key


# ---------------------------------------------------------------------------
# 8. /api/health — returns JSON with overall_status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_health_returns_json():
    import server

    original_key = server.MCP_API_KEY
    server.MCP_API_KEY = FAKE_KEY
    try:
        request = MagicMock()
        request.headers.get = lambda k, d="": FAKE_KEY if k == "X-MCP-API-Key" else d
        request.query_params.get = lambda k, d="": d
        request.client = MagicMock(host="127.0.0.1")

        health_payload = json.dumps({
            "timestamp": "2026-01-01T00:00:00Z",
            "overall_status": "healthy",
            "components": {
                "qdrant": {"status": "ok"},
                "ollama": {"status": "ok"},
                "n8n": {"status": "ok"},
                "cloudflare": {"status": "ok"},
                "tailscale": {"status": "ok"},
                "event_log": {"status": "ok"},
                "mcp_self": {"status": "ok"},
            },
            "warnings": [],
        })

        with patch("tools.health_check_all.health_check_all", new=AsyncMock(return_value=health_payload)):
            resp = await server._api_health(request)

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert "overall_status" in body
    finally:
        server.MCP_API_KEY = original_key


# ---------------------------------------------------------------------------
# 9. Rate limiter — 61st request from same IP returns 429
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limit_triggers_after_60():
    import server

    original_key = server.MCP_API_KEY
    server.MCP_API_KEY = FAKE_KEY
    test_ip = "10.0.0.1"

    # clear any existing bucket
    server._rate_buckets.pop(test_ip, None)
    try:
        # fill bucket to exactly _RATE_MAX
        now = time.monotonic()
        server._rate_buckets[test_ip] = [now] * server._RATE_MAX

        request = MagicMock()
        request.headers.get = lambda k, d="": FAKE_KEY if k == "X-MCP-API-Key" else d
        request.query_params.get = lambda k, d="": d
        request.client = MagicMock(host=test_ip)

        resp = await server._api_health(request)
        assert resp.status_code == 429

        body = json.loads(resp.body)
        assert body.get("error") == "rate_limit_exceeded"
    finally:
        server.MCP_API_KEY = original_key
        server._rate_buckets.pop(test_ip, None)
