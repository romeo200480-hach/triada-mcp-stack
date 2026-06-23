# mcp-server — виконавчий шар Mac Mini

Streamable HTTP MCP-сервер на `127.0.0.1:8765`.  
Оркестратор: Opus 4.7 у claude.ai → Cloudflare Tunnel → цей сервер.  
Auth: `X-MCP-API-Key` header (32-byte hex, з `~/.config/mcp-server/.env`).

---

## Запуск

```bash
cd ~/mcp-server
source .venv/bin/activate
python server.py
```

Фоновий режим:

```bash
nohup python server.py > server.log 2>&1 &
```

---

## Тули

Перед curl-тестами встанови змінну:

```bash
KEY=$(grep MCP_API_KEY ~/.config/mcp-server/.env | cut -d= -f2)
```

### system_info

Read-only snapshot: hostname, kernel, uptime, disk, RAM, CPU load, active services.

```bash
curl -s -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -H "X-MCP-API-Key: $KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'

curl -s -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -H "X-MCP-API-Key: $KEY" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"system_info","arguments":{}}}'
```

### audit_local_ports

Повертає список TCP LISTEN сокетів прив'язаних до `0.0.0.0` / `*`.

```bash
curl -s -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -H "X-MCP-API-Key: $KEY" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"audit_local_ports","arguments":{}}}'
```

### health_check_all

Перевіряє 7 компонентів: qdrant, ollama/bge-m3, n8n_local, cloudflare_tunnel, tailscale, event_log, mcp_self.

```bash
curl -s -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -H "X-MCP-API-Key: $KEY" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"health_check_all","arguments":{}}}'
```

### save_to_local_memory

Записує текст у Qdrant `roman_memory_v1` з bge-m3 embedding.  
`category`: `general` | `audit` | `note` | `conversation` | `system_event`

```bash
curl -s -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -H "X-MCP-API-Key: $KEY" \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"save_to_local_memory","arguments":{"text":"Test memory entry","category":"note","source_agent":"curl-test"}}}'
```

### query_local_memory

Семантичний пошук у Qdrant (тільки `active=true` записи за замовчуванням).

```bash
curl -s -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -H "X-MCP-API-Key: $KEY" \
  -d '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"query_local_memory","arguments":{"query":"test memory","limit":3}}}'
```

---

## Dashboard

Веб-інтерфейс (read-only). Відкрити у Firefox:

```
http://127.0.0.1:8765/dashboard?key=<YOUR_KEY>
```

Або через змінну:

```bash
xdg-open "http://127.0.0.1:8765/dashboard?key=$KEY"
```

Автооновлення кожні 30 секунд. Показує статус 7 компонентів + overall badge.

REST endpoint для CI/моніторингу:

```bash
curl -s http://127.0.0.1:8765/api/health \
  -H "X-MCP-API-Key: $KEY" | python3 -m json.tool
```

Rate limit: 60 запитів/хв з однієї IP.

---

## Audit logs

### Audit log операцій тулів

Кожен виклик тула з декоратором `@no_pilot_principle` пише рядок у:

```
~/logs/mcp-audit.jsonl
```

Формат (один JSON per line):

```json
{"ts": "2026-05-15T12:00:00.000Z", "tool": "save_to_local_memory", "status": "ok", "elapsed_ms": 142}
{"ts": "2026-05-15T12:01:00.000Z", "tool": "query_local_memory", "status": "error", "elapsed_ms": 31, "error": "503 ollama_unavailable"}
```

Секрети (hex ≥ 32 символів) автоматично маскуються як `***REDACTED***`.

### Event log Qdrant

Кожен підтверджений upsert у Qdrant пише рядок у:

```
~/logs/qdrant-events.jsonl
```

Формат:

```json
{"ts": "2026-05-15T12:00:00Z", "action": "upsert", "collection": "roman_memory_v1", "id": "uuid", "payload": {...}, "model": "bge-m3@sha256-..."}
```

Ротація при досягненні 100 MB → `qdrant-events.jsonl.1`, `.2`.  
Права доступу: `0o600` (тільки поточний користувач).

---

## Архітектура

```
claude.ai (Opus 4.7)
    │  MCP Streamable HTTP
    ▼
Cloudflare Tunnel  ──── cloudflared (user systemd service)
    │
    ▼
127.0.0.1:8765  ─── _AppRouter
                        ├── /api/health    → _api_health()
                        ├── /dashboard     → _dashboard()
                        └── /mcp/*         → _APIKeyMiddleware → FastMCP
                                                    │
                                          ┌─────────┴──────────┐
                                          ▼                     ▼
                                    Qdrant :6333          Ollama :11434
                                  roman_memory_v1          bge-m3
```

**Принципи (ADR-001…ADR-005):**  
`core/principles.py` — no-pilot, audit-first, soft-delete, embedding-locked, MVP-mode.

**Колекція Qdrant:** `roman_memory_v1`, Cosine distance, dim=1024.  
**Embedding model:** `bge-m3` (digest `sha256-daec91ffb5dd0c27411bd71f29932917c49cf529a641d0168496c3a501e3062c`).

---

## Встановлення (свіже середовище)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Тести:

```bash
pytest tests/test_tools.py -v
```

---

## Структура проєкту

```
mcp-server/
├── server.py              # FastMCP + _AppRouter + _APIKeyMiddleware + system_info + audit_local_ports
├── core/
│   ├── __init__.py
│   ├── principles.py      # no_pilot_principle decorator, AUDIT_LOG_PATH, constants
│   ├── schema.py          # Memory, HealthStatus dataclasses, PRINCIPAL
│   ├── contracts.py       # ToolContract, WriteToolContract protocols
│   └── topology.py        # TRIAD layers definition
├── tools/
│   ├── __init__.py        # register_all(mcp)
│   ├── health_check_all.py
│   ├── save_to_local_memory.py
│   └── query_local_memory.py
├── web/
│   └── dashboard.html     # Dark theme, auto-refresh 30s, no CDN
├── tests/
│   └── test_tools.py      # 9 pytest-asyncio tests
├── requirements.txt
└── pytest.ini
```
