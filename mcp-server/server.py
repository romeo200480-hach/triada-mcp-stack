"""
MCP server — виконавчий шар для мосту claude.ai → Mac Mini.
Транспорт: Streamable HTTP.  Адреса: 127.0.0.1:8765
Auth: X-MCP-API-Key header (32-byte hex, з ~/.config/mcp-server/.env)
"""

import collections
import json
import logging
import os
import platform
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from core import no_pilot_principle

# ---------------------------------------------------------------------------
# Config — завантажуємо .env вручну, без зовнішніх залежностей
# ---------------------------------------------------------------------------

_ENV_FILE = Path.home() / ".config" / "mcp-server" / ".env"
_LOG_FILE = Path(__file__).parent / "server.log"


def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


_ENV = _load_dotenv(_ENV_FILE)

MCP_API_KEY: str = _ENV.get("MCP_API_KEY", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(_LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("mcp-server")

if not MCP_API_KEY:
    log.warning("MCP_API_KEY is empty — every request will get 401")

# ---------------------------------------------------------------------------
# Rate limiter (in-memory, per IP, для /api/health)
# ---------------------------------------------------------------------------

_rate_buckets: dict[str, list[float]] = collections.defaultdict(list)
_RATE_WINDOW = 60.0
_RATE_MAX    = 60


def _check_rate(ip: str) -> bool:
    now = time.monotonic()
    cutoff = now - _RATE_WINDOW
    _rate_buckets[ip] = [t for t in _rate_buckets[ip] if t > cutoff]
    if len(_rate_buckets[ip]) >= _RATE_MAX:
        return False
    _rate_buckets[ip].append(now)
    return True


# ---------------------------------------------------------------------------
# HTTP route handlers — /api/health і /dashboard (own auth: header OR ?key=)
# ---------------------------------------------------------------------------

def _request_key(request: Request) -> str:
    return (request.headers.get("X-MCP-API-Key")
            or request.query_params.get("key", "")
            or request.cookies.get("mcp_key", ""))


async def _api_health(request: Request) -> JSONResponse:
    if _request_key(request) != MCP_API_KEY:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    ip = request.client.host if request.client else "unknown"
    if not _check_rate(ip):
        return JSONResponse(
            {"error": "rate_limit_exceeded", "retry_after_seconds": 60},
            status_code=429,
        )
    from tools.health_check_all import health_check_all
    return JSONResponse(json.loads(await health_check_all()))


async def _chat(request: Request) -> JSONResponse:
    key = _request_key(request)
    if key != MCP_API_KEY:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import asyncio
    import httpx
    body = await request.json()
    messages = body.get("messages") or []
    model = body.get("model") or "smart"
    lk = ""
    for line in open("/home/mac/.config/litellm/.env"):
        if line.startswith("LITELLM_MASTER_KEY="):
            lk = line.strip().split("=", 1)[1]
    hdrs = {"Authorization": "Bearer " + lk}
    url = "http://127.0.0.1:4000/v1/chat/completions"

    async def ask(m, msgs):
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(url, headers=hdrs, json={"model": m, "messages": msgs})
        d = r.json()
        try:
            return d["choices"][0]["message"]["content"]
        except Exception:
            return "ERROR: " + str(d)[:400]

    if model != "council":
        answer = await ask(model, messages)
        return JSONResponse({"model": model, "answer": answer})

    a, b = await asyncio.gather(ask("smart", messages), ask("deepseek", messages))
    q = messages[-1].get("content", "") if messages else ""
    syn = [{"role": "user", "content": "Питання: " + q
            + "\n\nВідповідь A (Gemini):\n" + a
            + "\n\nВідповідь B (DeepSeek):\n" + b
            + "\n\nСинтезуй одну найкращу відповідь українською: об'єднай сильне, прибери повтори, познач важливі розбіжності."}]
    final = await ask("smart", syn)
    return JSONResponse({"model": "council", "answer": final,
                         "raw": {"gemini": a, "deepseek": b}})
async def _dashboard(request: Request) -> HTMLResponse:
    html_path = Path(__file__).parent / "web" / "dashboard.html"
    try:
        html = html_path.read_text()
    except FileNotFoundError:
        return HTMLResponse(
            "<html><body><h1>404 — create web/dashboard.html (Крок 7)</h1></body></html>",
            status_code=404,
        )
    return HTMLResponse(html.replace("{{API_KEY}}", ""))


# ---------------------------------------------------------------------------
# API Key Middleware (pure ASGI — не залежить від Starlette internals)
# ---------------------------------------------------------------------------

class _APIKeyMiddleware:
    """Перевіряє X-MCP-API-Key на всіх HTTP-запитах; lifespan пропускає."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        provided = headers.get(b"x-mcp-api-key", b"").decode()
        if not provided:
            qs = scope.get("query_string", b"").decode()
            for part in qs.split("&"):
                if part.startswith("key="):
                    provided = part[4:]
                    break

        if not MCP_API_KEY or provided != MCP_API_KEY:
            client = scope.get("client")
            ip = client[0] if client else "?"
            log.warning("auth=fail ip=%s path=%s", ip, scope.get("path", "?"))
            resp = JSONResponse({"error": "Unauthorized"}, status_code=401)
            await resp(scope, receive, send)
            return

        await self._app(scope, receive, send)


class _AppRouter:
    """Dispatches /api/health and /dashboard to route handlers; rest to _APIKeyMiddleware+MCP."""

    def __init__(self, mcp_with_auth: ASGIApp) -> None:
        self._mcp_with_auth = mcp_with_auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/api/health":
                req = Request(scope, receive, send)
                resp = await _api_health(req)
                await resp(scope, receive, send)
                return
            elif path == "/api/chat":
                req = Request(scope, receive, send)
                resp = await _chat(req)
                await resp(scope, receive, send)
                return
            elif path in ("/manifest.json", "/sw.js", "/icon-192.png", "/icon-512.png"):
                import mimetypes
                from starlette.responses import Response as _Resp
                from pathlib import Path as _P
                fp = _P(__file__).parent / "web" / path.lstrip("/")
                if fp.is_file():
                    mt = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
                    resp = _Resp(fp.read_bytes(), media_type=mt)
                else:
                    from starlette.responses import PlainTextResponse as _PT
                    resp = _PT("not found", status_code=404)
                await resp(scope, receive, send)
                return
            elif path == "/dashboard":
                req = Request(scope, receive, send)
                qkey = req.query_params.get("key", "")
                if qkey:
                    from starlette.responses import RedirectResponse
                    resp = RedirectResponse(url="/dashboard", status_code=302)
                    resp.set_cookie("mcp_key", qkey, httponly=True,
                                    secure=True, samesite="lax", max_age=31536000)
                    await resp(scope, receive, send)
                    return
                resp = await _dashboard(req)
                await resp(scope, receive, send)
                return
        await self._mcp_with_auth(scope, receive, send)

# ---------------------------------------------------------------------------
# FastMCP app
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="mac-mini-executor",
    instructions=(
        "Виконавчий шар на Mac Mini з Lubuntu. "
        "Тули: system_info, health_check_all, save_to_local_memory, query_local_memory, audit_local_ports. "
        "Пам'ять: Qdrant roman_memory_v1, embedding bge-m3. "
        "Оркестратор — Opus 4.7 у claude.ai."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*", "localhost:*", "[::1]:*", "100.112.146.84:*",
            "mcp.romeo2004.pp.ua",
        ],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    ),
)

# Реєстрація тулів із tools/
from tools import register_all as _register_tools
_register_tools(mcp)

# ---------------------------------------------------------------------------
# Tool: system_info
# ---------------------------------------------------------------------------

_WATCHED_SERVICES = [
    "ssh", "NetworkManager", "systemd-resolved", "systemd-timesyncd",
    "cron", "dbus", "accounts-daemon", "polkit", "udisks2", "upower",
    "avahi-daemon", "cups", "bluetooth", "snapd",
]


def _read_meminfo() -> dict[str, int]:
    mem: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(":")] = int(parts[1])
    except OSError:
        pass
    return mem


def _uptime_seconds() -> float:
    try:
        with open("/proc/uptime") as fh:
            return float(fh.read().split()[0])
    except OSError:
        return 0.0


def _disk_free_gb(path: str = "/") -> float:
    st = os.statvfs(path)
    return round(st.f_bavail * st.f_frsize / 1_073_741_824, 2)


def _cpu_load_1min() -> float:
    return round(os.getloadavg()[0], 2)


def _active_services(names: list[str]) -> list[str]:
    active = []
    for name in names:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", "--quiet", name],
                capture_output=True, timeout=3,
            )
            if r.returncode == 0:
                active.append(name)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return active


@mcp.tool()
@no_pilot_principle
def system_info() -> str:
    """Повертає JSON зі snapshot системного стану Mac Mini. Read-only."""
    mem = _read_meminfo()
    data = {
        "hostname": socket.gethostname(),
        "kernel": platform.release(),
        "uptime_seconds": _uptime_seconds(),
        "disk_root_free_gb": _disk_free_gb("/"),
        "mem_total_mb": round(mem.get("MemTotal", 0) / 1024, 1),
        "mem_available_mb": round(mem.get("MemAvailable", 0) / 1024, 1),
        "cpu_load_1min": _cpu_load_1min(),
        "services_active": _active_services(_WATCHED_SERVICES),
    }
    return json.dumps(data, ensure_ascii=False)

# ---------------------------------------------------------------------------
# Tool: audit_local_ports
# ---------------------------------------------------------------------------

_PROC_RE = re.compile(r'users:\(\("([^"]+)",pid=(\d+)')
_PUBLIC_ADDRS = {"0.0.0.0", "*", "::"}


def _parse_ss(raw: str) -> tuple[list[dict], int]:
    """Returns (public_bindings, localhost_count)."""
    public: list[dict] = []
    localhost_count = 0

    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] != "LISTEN":
            continue

        local = parts[3]
        addr, _, port_str = local.rpartition(":")
        addr = addr.strip("[]")
        try:
            port = int(port_str)
        except ValueError:
            continue

        proc, pid = "unknown", None
        m = _PROC_RE.search(line)
        if m:
            proc, pid = m.group(1), int(m.group(2))

        if addr in _PUBLIC_ADDRS:
            public.append({
                "address": addr,
                "port": port,
                "process": proc,
                "pid": pid,
                "severity": "warn",
            })
        else:
            localhost_count += 1

    return public, localhost_count


@mcp.tool()
def audit_local_ports() -> str:
    """
    Повертає JSON зі списком listening sockets прив'язаних до 0.0.0.0 або *.
    Read-only. Без sudo — PIDs видні тільки для процесів поточного користувача.
    """
    log.info("tool=audit_local_ports called")
    try:
        result = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=10,
        )
        raw = result.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return json.dumps({"error": str(exc)})

    public_bindings, localhost_count = _parse_ss(raw)
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "public_bindings": public_bindings,
        "localhost_bindings_count": localhost_count,
        "raw_evidence_truncated": "\n".join(raw.splitlines()[:50]),
    }
    log.info("tool=audit_local_ports public=%d localhost=%d", len(public_bindings), localhost_count)
    return json.dumps(data, ensure_ascii=False)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    log.info("Starting MCP server on 100.112.146.84:8765 (Streamable HTTP + API key auth)")
    mcp_base = mcp.streamable_http_app()
    mcp_with_auth = _APIKeyMiddleware(mcp_base)
    app = _AppRouter(mcp_with_auth)
    uvicorn.run(app, host="100.112.146.84", port=8765, log_level="info")
