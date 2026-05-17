#!/usr/bin/env python3
"""
GATEWAY.py — ARCHON Security Choke Point (v3 — Thin Gateway)
Mission: HTTP server only. Route to KERNEL, wrap in crash isolation.
         If KERNEL or PROVIDER crash → returns 500, not a dead process.

Created: 2026-05-16  (v3 thin: split KERNEL + PROVIDER)
"""

import json, os, sys, sqlite3, logging, time, uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

# ── Logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gateway")
logging.getLogger("urllib3").setLevel(logging.ERROR)

# ── Paths ──────────────────────────────────────────────────────────
CORE_DIR = Path(__file__).parent.resolve()
WORKSPACE = CORE_DIR.parent
ENV_FILE = WORKSPACE / ".env"
AUDIT_DB = CORE_DIR / "audit_vault.db"
TRACE_DIR = Path(os.path.expanduser("~/ARCHON/logs/traces"))

# ── Global config ──────────────────────────────────────────────────
CONFIG = {
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com/v1",
    "air_gap_mode": False,
    "port": 18788,
    "ollama_url": "http://127.0.0.1:11434",
    "local_model": "phi:latest",
    "sentry_model": "phi:latest",
    "rag_enabled": True,
    "governor_enabled": True,
    "telegram_bot_token": "",
    "telegram_chat_id": "6189741164",
}

# ── Config Loading ─────────────────────────────────────────────────

def load_config():
    """Load .env + openclaw.json into CONFIG dict."""
    if ENV_FILE.exists():
        env = {}
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        CONFIG["deepseek_api_key"] = env.get("DEEPSEEK_API_KEY", "")
        CONFIG["air_gap_mode"] = env.get("AIR_GAP_MODE", "false").lower() == "true"
        CONFIG["port"] = int(env.get("PORT_GATEWAY", "18788"))
        CONFIG["ollama_url"] = env.get("OLLAMA_URL", "http://127.0.0.1:11434")
        CONFIG["local_model"] = env.get("LOCAL_MODEL", "llama3.2:3b")
        CONFIG["sentry_model"] = env.get("SENTRY_MODEL", "llama3.2:3b")
        CONFIG["rag_enabled"] = env.get("RAG_ENABLED", "true").lower() == "true"
        CONFIG["governor_enabled"] = env.get("GOVERNOR_ENABLED", "true").lower() == "true"

    # Telegram token from openclaw.json
    openclaw_cfg = Path(os.path.expanduser("~/.openclaw/openclaw.json"))
    if openclaw_cfg.exists():
        try:
            with open(openclaw_cfg) as f:
                oc = json.load(f)
            tg = oc.get("channels", {}).get("telegram", {})
            CONFIG["telegram_bot_token"] = tg.get("botToken", "")
            logger.warning(f"Telegram bot loaded: {CONFIG['telegram_bot_token'][:10]}...")
        except Exception as e:
            logger.warning(f"Telegram config load failed: {e}")

    TRACE_DIR.mkdir(parents=True, exist_ok=True)


# ── Audit Vault ────────────────────────────────────────────────────

def init_audit_vault() -> sqlite3.Connection:
    db = sqlite3.connect(str(AUDIT_DB), check_same_thread=False)
    db.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            request_id TEXT NOT NULL,
            original_prompt TEXT,
            redacted_prompt TEXT,
            model_routed TEXT NOT NULL,
            air_gap_status TEXT NOT NULL,
            response_status TEXT,
            duration_ms INTEGER,
            response_text TEXT,
            agent_name TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_log(request_id)")
    existing = {row[1] for row in db.execute("PRAGMA table_info(audit_log)").fetchall()}
    for col, typedef in [("response_text", "TEXT"), ("agent_name", "TEXT")]:
        if col not in existing:
            db.execute(f"ALTER TABLE audit_log ADD COLUMN {col} {typedef}")
    db.commit()
    logger.warning(f"Audit vault: {AUDIT_DB}")
    return db


def log_audit(db, entry):
    try:
        db.execute(
            """INSERT INTO audit_log (timestamp, request_id, original_prompt, redacted_prompt,
               model_routed, air_gap_status, response_status, duration_ms, response_text, agent_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (entry.get("timestamp"), entry.get("request_id"), entry.get("original_prompt"),
             entry.get("redacted_prompt"), entry.get("model_routed"), entry.get("air_gap_status"),
             entry.get("response_status"), entry.get("duration_ms", 0),
             entry.get("response_text"), entry.get("agent_name")),
        )
        db.commit()
    except Exception as e:
        logger.error(f"Audit write failed: {e}")


# ── HTTP Handler ───────────────────────────────────────────────────

class SovereignHandler(BaseHTTPRequestHandler):
    """Thin HTTP wrapper — routes to KERNEL, wraps in crash isolation."""

    db = None
    _request_counter = 0

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_response(self, data):
        """Convert a complete chat.completion into SSE for OpenClaw streaming."""
        choices = data.get("choices", [])
        content = str(choices[0].get("message", {}).get("content", "")) if choices else ""
        model = data.get("model", "archon")
        cid = data.get("id", f"gw-{int(time.time())}")
        created = data.get("created", int(time.time()))

        chunk = json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]})
        done_chunk = json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        body = f"data: {chunk}\n\ndata: {done_chunk}\n\ndata: [DONE]\n\n".encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-OpenClaw-Agent-Id")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json_response({
                "status": "sovereign",
                "air_gap": CONFIG["air_gap_mode"],
                "vault": str(AUDIT_DB),
            })
        elif parsed.path == "/v1/models":
            self._json_response({
                "object": "list",
                "data": [{"id": "openclaw:main", "object": "model",
                          "created": int(time.time()), "owned_by": "openclaw"}],
            })
        elif parsed.path == "/v1/sentinel":
            # Phase 3 — Health Sentinel report
            try:
                sys.path.insert(0, str(CORE_DIR / "TOOLS"))
                from graph_memory import health_sentinel
                self._json_response(health_sentinel())
            except Exception as e:
                self._json_response({"error": str(e), "severity": "error"}, 500)
        elif parsed.path == "/v1/reasoning":
            # Phase 3 — Recent reasoning traces
            try:
                sys.path.insert(0, str(CORE_DIR / "TOOLS"))
                from graph_memory import get_recent_decisions
                limit = int(self.path.split("?limit=")[1]) if "?limit=" in self.path else 10
                self._json_response({"decisions": get_recent_decisions(limit)})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
        elif parsed.path == "/v1/costs":
            # Cost ledger report
            try:
                sys.path.insert(0, str(CORE_DIR / "TOOLS"))
                from cost_ledger import daily_report, budget_check
                report = daily_report()
                report["budget"] = budget_check()
                self._json_response(report)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
        elif parsed.path.startswith("/v1/costs/agents/"):
            # Per-agent cost breakdown
            try:
                agent_name = parsed.path.split("/v1/costs/agents/")[1]
                sys.path.insert(0, str(CORE_DIR / "TOOLS"))
                from cost_ledger import agent_report
                self._json_response(agent_report(agent_name))
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
        elif parsed.path == "/v1/memory/stats":
            # GraphThink — memory dashboard stats
            try:
                sys.path.insert(0, str(CORE_DIR / "TOOLS"))
                from graph_memory import _driver
                db = _driver
                stats = {}
                with db.session() as session:
                    for label in ["Conversation", "Message", "Entity", "ThinkingNode"]:
                        r = session.run(f"MATCH (n:{label}) RETURN count(*) AS cnt").single()
                        stats[label.lower()] = r["cnt"]
                    # Relationships
                    for rel in ["HAS_MESSAGE", "BELONGS_TO", "EXTRACTED"]:
                        r = session.run(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS cnt").single()
                        stats[rel.lower()] = r["cnt"]
                self._json_response({"ok": True, "stats": stats})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
        else:
            self._json_response({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)

        # ── Workbench (stays in Gateway — simple, isolated) ────
        if parsed.path == "/v1/workbench":
            try:
                cl = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(cl) if cl > 0 else b"{}"
                payload = json.loads(body.decode())
                sys.path.insert(0, str(CORE_DIR / "TOOLS"))
                import shared_workbench
                action = payload.get("action", "read")
                if action == "read":
                    key = payload.get("key")
                    data = shared_workbench.load()
                    if key:
                        if key in data:
                            self._json_response({"ok": True, "data": data[key]})
                        else:
                            parts = key.split(".")
                            if len(parts) == 2 and parts[0] in data:
                                for entry in data[parts[0]]:
                                    if entry.get("key") == parts[1]:
                                        self._json_response({"ok": True, "data": entry})
                                        return
                            self._json_response({"ok": True, "data": None})
                    else:
                        self._json_response({"ok": True, "data": data})
                elif action == "write":
                    key, value = payload.get("key"), payload.get("value")
                    if key and value is not None:
                        shared_workbench.write(key, value)
                        self._json_response({"ok": True, "msg": f"Written {key}"})
                    else:
                        self._json_response({"ok": False, "error": "key and value required"}, 400)
                else:
                    self._json_response({"ok": False, "error": f"unknown: {action}"}, 400)
                return
            except Exception as e:
                self._json_response({"ok": False, "error": str(e)}, 500)
                return

        if parsed.path != "/v1/chat/completions":
            # ── GraphThink Memory API ────────────────────────────────
            if parsed.path == "/v1/memory/store":
                try:
                    cl = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(cl) if cl > 0 else b"{}"
                    payload = json.loads(body.decode())
                    session_id = payload.get("session_id", "default")
                    # Extract tenant from session_id prefix (tenant:session)
                    tenant_id = payload.get("tenant_id", "")
                    tenant_header = self.headers.get("X-Tenant-ID", "")
                    if not tenant_id and tenant_header:
                        tenant_id = tenant_header
                    if tenant_id and not session_id.startswith(tenant_id + ":"):
                        session_id = f"{tenant_id}:{session_id}"
                    role = payload.get("role", "user")
                    content = payload.get("content", "")
                    if not content:
                        self._json_response({"ok": False, "error": "content required"}, 400)
                        return
                    sys.path.insert(0, str(CORE_DIR / "TOOLS"))
                    from graph_memory import add_message
                    import asyncio
                    asyncio.run(add_message(session_id, role, content))
                    self._json_response({"ok": True, "msg": f"Stored in session {session_id}",
                                         "tenant_id": tenant_id or "default"})
                except Exception as e:
                    self._json_response({"ok": False, "error": str(e)}, 500)
                return
            elif parsed.path == "/v1/memory/search":
                try:
                    cl = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(cl) if cl > 0 else b"{}"
                    payload = json.loads(body.decode())
                    query = payload.get("query", "")
                    limit = int(payload.get("limit", 5))
                    min_score = float(payload.get("min_score", 0.3))
                    tenant_id = payload.get("tenant_id", "")
                    # Support tenant_id via header too
                    tenant_header = self.headers.get("X-Tenant-ID", "")
                    if not tenant_id and tenant_header:
                        tenant_id = tenant_header
                    if not query:
                        self._json_response({"ok": False, "error": "query required"}, 400)
                        return
                    sys.path.insert(0, str(CORE_DIR / "TOOLS"))
                    from graph_memory import semantic_search
                    results = semantic_search(query, limit, min_score, tenant_id)
                    self._json_response({"ok": True, "results": results, "query": query,
                                         "tenant_id": tenant_id or "global"})
                except Exception as e:
                    self._json_response({"ok": False, "error": str(e)}, 500)
                return
            self._json_response({"error": "not found"}, 404)
            return

        # ═══════════════════════════════════════════════════════════
        # THIN GATEWAY — CRASH ISOLATION BOUNDARY
        # Everything below is wrapped. If KERNEL or PROVIDER throws,
        # we catch it here and return a 500 instead of dying.
        # ═══════════════════════════════════════════════════════════
        SovereignHandler._request_counter += 1
        request_id = f"GW-{int(time.time())}-{SovereignHandler._request_counter}"
        start_time = time.time()

        try:
            # Read body
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            payload = json.loads(body.decode())
            source_ip = self.headers.get("X-Forwarded-For", self.client_address[0])
            stream = payload.get("stream", False)

            # ── Import & call KERNEL ──────────────────────────────
            sys.path.insert(0, str(CORE_DIR))
            from KERNEL import handle_request

            kernel_result = handle_request(CONFIG, payload, source_ip)

            response_data = kernel_result.get("response")

            # ── Audit ────────────────────────────────────────────
            if self.db and kernel_result.get("audit_entry"):
                log_audit(self.db, kernel_result["audit_entry"])

            # ── Telegram trace update — complete ──────────────────
            msg_id = kernel_result.get("trace_telegram_msg_id")
            if msg_id and CONFIG.get("telegram_bot_token"):
                try:
                    final_text = ""
                    if response_data and response_data.get("choices"):
                        final_text = str(response_data["choices"][0].get("message", {}).get("content", ""))
                    tg_url = f"https://api.telegram.org/bot{CONFIG['telegram_bot_token']}/editMessageText"
                    tg_payload = json.dumps({
                        "chat_id": CONFIG["telegram_chat_id"],
                        "message_id": msg_id,
                        "text": f"✅ Complete in {kernel_result.get('duration_ms',0)}ms.\n`{final_text[:60]}`",
                        "parse_mode": "Markdown",
                    }).encode()
                    tg_req = urllib.request.Request(tg_url, data=tg_payload, headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(tg_req, timeout=5)
                except:
                    pass

            # ── Respond ──────────────────────────────────────────
            if response_data is None:
                response_data = {
                    "id": f"error_{int(time.time())}",
                    "object": "chat.completion", "created": int(time.time()),
                    "model": "error",
                    "choices": [{"index": 0, "message": {"role": "assistant",
                        "content": "Kernel returned no response. Check logs."}, "finish_reason": "stop"}],
                }
            if stream:
                self._sse_response(response_data)
            else:
                self._json_response(response_data)

        except Exception as e:
            # ── CRASH ISOLATION ─────────────────────────────────
            # If anything in KERNEL or PROVIDER throws, we catch it here.
            # The Gateway process survives. The user gets a 500 with details.
            logger.error(f"KERNEL CRASHED (Gateway surviving): {e}", exc_info=True)
            duration_ms = int((time.time() - start_time) * 1000)
            try:
                self._json_response({
                    "id": f"gw_error_{int(time.time())}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "gateway",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant",
                            "content": f"ARCHON Gateway error: {type(e).__name__}. The gateway is still running."},
                        "finish_reason": "stop",
                    }],
                })
            except:
                pass

            # Still try to audit the crash
            if self.db:
                try:
                    log_audit(self.db, {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "request_id": request_id,
                        "original_prompt": body.decode()[:200],
                        "model_routed": "crash",
                        "air_gap_status": "error",
                        "response_status": f"crash_{type(e).__name__}",
                        "duration_ms": duration_ms,
                    })
                except:
                    pass


# ── Main ───────────────────────────────────────────────────────────

def main():
    load_config()
    db = init_audit_vault()
    SovereignHandler.db = db

    # ── Init graph memory ─────────────────────────────────────────
    try:
        sys.path.insert(0, str(CORE_DIR / "TOOLS"))
        from graph_memory import ensure_schema, ensure_vector_index
        ensure_schema()
        ensure_vector_index()
        logger.warning("Graph Memory: schema ready")
    except Exception as e:
        logger.warning(f"Graph Memory init: {e}")

    # ── Init workbench ────────────────────────────────────────────
    try:
        sys.path.insert(0, str(CORE_DIR / "TOOLS"))
        import shared_workbench
        shared_workbench.new()
        logger.warning("Shared Workbench: ready")
    except Exception as e:
        logger.warning(f"Workbench init: {e}")

    logger.warning(f"=== SOVEREIGN KERNEL ACTIVE (v3 Thin Gateway) ===")
    logger.warning(f"Air Gap: {CONFIG['air_gap_mode']}")
    logger.warning(f"Port: {CONFIG['port']}")

    server = HTTPServer(("127.0.0.1", CONFIG["port"]), SovereignHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        db.close()
        server.server_close()


if __name__ == "__main__":
    main()
