#!/usr/bin/env python3
"""tdai-memory-mcp — exposes TencentDB Agent Memory (memory-core v3) as MCP tools.

Pure-stdlib implementation (urllib + json + sys + http.server). Speaks the
Model Context Protocol over stdio (JSON-RPC 2.0, newline-delimited) for local
use, or over the MCP Streamable HTTP transport (JSON-RPC over HTTP) when run
with --http, so the same server can be a remote MCP endpoint. stdio remains
the default and is byte-for-byte unchanged.

Configuration via environment variables:
  MEMORY_ENDPOINT    default http://127.0.0.1:8420
  MEMORY_API_KEY     default local        (gateway bearer token)
  MEMORY_SERVICE_ID  default default      (x-tdai-service-id)
  MEMORY_TEAM_ID     default default
  MEMORY_AGENT_ID    default opencode
  MEMORY_USER_ID     default ariq

All models sharing these values read/write the SAME shared team memory.
MCP_HTTP_TOKEN     if set (--http mode only), require Authorization: Bearer <token>
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import sys
import threading
import urllib.error
import urllib.request
import uuid

ENDPOINT = os.environ.get("MEMORY_ENDPOINT", "http://127.0.0.1:8420").rstrip("/")
API_KEY = os.environ.get("MEMORY_API_KEY", "local")
SERVICE_ID = os.environ.get("MEMORY_SERVICE_ID", "default")
TEAM_ID = os.environ.get("MEMORY_TEAM_ID", "default")
AGENT_ID = os.environ.get("MEMORY_AGENT_ID", "opencode")
USER_ID = os.environ.get("MEMORY_USER_ID", "ariq")
TIMEOUT = float(os.environ.get("MEMORY_TIMEOUT", "30"))

SERVER_NAME = "tdai-memory"
SERVER_VERSION = "0.1.0"
SERVER_VERSION_HTTP = "0.2.0"
PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2025-03-26"
HTTP_TOKEN = os.environ.get("MCP_HTTP_TOKEN")


def _post(path: str, body: dict) -> dict:
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{ENDPOINT}{path}",
        data=payload,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "x-tdai-service-id": SERVICE_ID,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:500]
        raise RuntimeError(f"memory-core HTTP {e.code}: {detail}") from None
    except Exception as e:
        raise RuntimeError(f"memory-core unreachable: {e}") from None
    envelope = json.loads(raw)
    if envelope.get("code") != 0:
        raise RuntimeError(
            f"memory-core code={envelope.get('code')}: {envelope.get('message')}"
        )
    return envelope.get("data") or {}


def _iso() -> dict:
    return {"team_id": TEAM_ID, "agent_id": AGENT_ID, "user_id": USER_ID}


# ── Tool implementations ────────────────────────────────────────────────────

def t_memory_search(query: str, limit: int = 5, level: str = "atomic") -> str:
    """Semantic search over distilled memories (L1 atomic) or raw conversations (L0)."""
    limit = max(1, min(int(limit or 5), 20))
    base = _iso()
    if level == "conversation":
        data = _post("/v3/conversation/search", {**base, "query": query, "limit": limit})
        items = data.get("messages") or data.get("items") or data.get("data") or []
        lines = [f"L0 conversation matches ({len(items)}):"]
        for it in items[:limit]:
            role = it.get("role", "?")
            content = (it.get("content") or "")
            if isinstance(content, list):
                content = " ".join(str(c.get("text", c)) if isinstance(c, dict) else str(c) for c in content)
            lines.append(f"  [{role}] {str(content)[:300]}")
        return "\n".join(lines) if items else "No L0 conversation matches."
    else:
        data = _post("/v3/atomic/search", {**base, "query": query, "limit": limit})
        items = data.get("atomics") or data.get("items") or data.get("data") or []
        lines = [f"L1 atomic memories ({len(items)}):"]
        for it in items[:limit]:
            content = it.get("content") or it.get("text") or ""
            mtype = it.get("type", "")
            lines.append(f"  ({mtype}) {str(content)[:300]}")
        return "\n".join(lines) if items else "No L1 atomic memories found."


def t_memory_read_profile(path: str = "") -> str:
    """Read long-term memory: L3 core profile and/or a specific L2 scenario note."""
    base = _iso()
    parts = []
    if not path or path == "core":
        try:
            core = _post("/v3/core/read", base)
            content = core.get("content")
            if content:
                parts.append(f"=== L3 Core Profile ===\n{content}")
        except RuntimeError as e:
            parts.append(f"[L3 core read error: {e}]")
    if not path:
        try:
            ls = _post("/v3/scenario/ls", base)
            entries = ls.get("files") or ls.get("entries") or ls.get("scenarios") or ls.get("data") or []
            names = [e.get("path", e.get("name", str(e))) if isinstance(e, dict) else str(e) for e in entries]
            if names:
                parts.append("=== L2 Scenario Notes (paths) ===\n" + "\n".join(f"  {n}" for n in names))
        except RuntimeError as e:
            parts.append(f"[L2 ls error: {e}]")
    elif path and path != "core":
        try:
            sc = _post("/v3/scenario/read", {**base, "path": path})
            content = sc.get("content", "")
            parts.append(f"=== L2 Scenario: {path} ===\n{content}")
        except RuntimeError as e:
            parts.append(f"[L2 read '{path}' error: {e}]")
    return "\n\n".join(parts) if parts else "No long-term memory stored yet."


def t_memory_save_core(content: str) -> str:
    """Overwrite the L3 core profile (long-term identity, persistent preferences)."""
    _post("/v3/core/write", {**_iso(), "content": content})
    return "L3 core profile updated."


def t_memory_save_conversation(messages: str, session_id: str = "opencode") -> str:
    """Save a conversation snippet to L0 for future recall/distillation.

    messages: JSON string of [{"role":"user|assistant","content":"..."}].
    """
    try:
        msgs = json.loads(messages) if isinstance(messages, str) else messages
    except json.JSONDecodeError:
        msgs = [{"role": "user", "content": str(messages)}]
    if not isinstance(msgs, list) or not msgs:
        return "No messages to save."
    _post("/v3/conversation/add", {**_iso(), "session_id": session_id, "messages": msgs})
    return f"Saved {len(msgs)} message(s) to L0 (session={session_id})."


# ── MCP tool registry ───────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "memory_search",
        "description": (
            "Search shared team memory for facts, preferences, decisions, and past "
            "conversations. Use this to recall context the user mentioned previously. "
            "level='atomic' (default) searches distilled L1 memories; "
            "level='conversation' searches raw L0 conversation history."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to recall (natural language)."},
                "limit": {"type": "integer", "default": 5, "description": "Max results (1-20)."},
                "level": {"type": "string", "enum": ["atomic", "conversation"], "default": "atomic"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_read_profile",
        "description": (
            "Read long-term shared memory: the L3 core profile (persistent identity/"
            "preferences) and list of L2 scenario notes. Pass a path to read a specific note."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Empty = core profile + list notes; 'core' = just core; a path = that L2 note.",
                },
            },
        },
    },
    {
        "name": "memory_save_core",
        "description": (
            "Overwrite the L3 core profile — long-term persistent context shared across "
            "all sessions (identity, standing preferences). Use sparingly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    },
    {
        "name": "memory_save_conversation",
        "description": (
            "Save a conversation snippet to shared memory (L0) for future recall and "
            "auto-distillation. Pass messages as a JSON array string of "
            "[{\"role\":\"user|assistant\",\"content\":\"...\"}]."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "messages": {"type": "string", "description": 'JSON: [{"role":"user","content":"..."}]'},
                "session_id": {"type": "string", "default": "opencode"},
            },
            "required": ["messages"],
        },
    },
]

DISPATCH = {
    "memory_search": t_memory_search,
    "memory_read_profile": t_memory_read_profile,
    "memory_save_core": t_memory_save_core,
    "memory_save_conversation": t_memory_save_conversation,
}


# ── MCP stdio JSON-RPC server ───────────────────────────────────────────────

def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _result(req_id: str | None, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: str | None, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def dispatch(method: str, params: dict, req_id, protocol_version: str = PROTOCOL_VERSION, server_version: str = SERVER_VERSION) -> dict | None:
    """Process a decoded JSON-RPC message and return the response object.

    Returns None for 'notifications/initialized' (no response is expected).
    Shared by the stdio and Streamable HTTP transports.
    """
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": server_version},
        }}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        fn = DISPATCH.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown tool: {name}"}}
        try:
            text = fn(**args)
            return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": str(text)}]}}
        except TypeError as e:
            return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": f"Argument error: {e}"}], "isError": True}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def handle(method: str, params: dict, req_id) -> None:
    resp = dispatch(method, params, req_id)
    if resp is not None:
        _send(resp)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method", "")
        params = msg.get("params", {}) or {}
        req_id = msg.get("id")
        try:
            handle(method, params, req_id)
        except Exception as e:
            _error(req_id, -32603, f"Internal error: {e}")


# ── MCP Streamable HTTP transport ──────────────────────────────────────────
# Single JSON-RPC endpoint at the base URL ("/"). POST carries one JSON-RPC
# request/notification per body. The session id is server-issued on initialize
# and must be echoed via the mcp-session-id header on every later request.

_SESSIONS: dict[str, str] = {}
_SESSIONS_LOCK = threading.Lock()


def _negotiate(client_version: str) -> str:
    return client_version if client_version in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL


def _authorized(headers) -> bool:
    if not HTTP_TOKEN:
        return True
    auth = headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[7:] == HTTP_TOKEN


class _HttpHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[tdai-memory http] %s\n" % (fmt % args))

    def _send_error(self, status, message, protocol=DEFAULT_PROTOCOL):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32000, "message": message},
        }).encode()
        self.send_response(status)
        self.send_header("MCP-Protocol-Version", protocol)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_response(self, payload, protocol, extra_headers=None):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("MCP-Protocol-Version", protocol)
        self.send_header("Content-Type", "application/json")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_202(self, protocol):
        self.send_response(202)
        self.send_header("MCP-Protocol-Version", protocol)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_405(self):
        self.send_response(405)
        self.send_header("MCP-Protocol-Version", DEFAULT_PROTOCOL)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._send_405()

    def do_DELETE(self):
        self._send_405()

    def do_POST(self):
        if not _authorized(self.headers):
            self._send_error(401, "Unauthorized: missing or invalid Authorization header")
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send_error(415, "Content-Type must be application/json")
            return
        accept = (self.headers.get("Accept") or "").lower()
        if "application/json" not in accept or "text/event-stream" not in accept:
            self._send_error(406, "Accept must include both application/json and text/event-stream")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            self._send_error(400, "Empty request body")
            return
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_error(400, "Invalid JSON body")
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("method", ""), str):
            self._send_error(400, "Body must be a JSON-RPC object with a string 'method'")
            return

        method = msg["method"]
        params = msg.get("params", {}) or {}
        if not isinstance(params, dict):
            params = {}
        req_id = msg.get("id")
        session_hdr = self.headers.get("mcp-session-id")

        if method == "initialize":
            negotiated = _negotiate(str(params.get("protocolVersion") or ""))
            if session_hdr and session_hdr in _SESSIONS:
                session_id = session_hdr
            else:
                session_id = uuid.uuid4().hex
            with _SESSIONS_LOCK:
                _SESSIONS[session_id] = negotiated
            try:
                resp = dispatch(
                    method, params, req_id,
                    protocol_version=negotiated, server_version=SERVER_VERSION_HTTP,
                )
            except Exception as e:
                self._send_error(500, f"Internal error: {e}", negotiated)
                return
            self._send_response(resp, negotiated, {"mcp-session-id": session_id})
            return

        if not session_hdr:
            self._send_error(400, "Missing MCP-Session-Id header (call initialize first)")
            return
        with _SESSIONS_LOCK:
            session_protocol = _SESSIONS.get(session_hdr)
        if session_protocol is None:
            self._send_error(400, f"Unknown or expired MCP-Session-Id: {session_hdr}")
            return

        try:
            resp = dispatch(
                method, params, req_id,
                protocol_version=session_protocol, server_version=SERVER_VERSION_HTTP,
            )
        except Exception as e:
            self._send_error(500, f"Internal error: {e}", session_protocol)
            return
        if req_id is None or resp is None:
            self._send_202(session_protocol)
            return
        self._send_response(resp, session_protocol)


def main_http(host: str = "127.0.0.1", port: int = 8423) -> None:
    server = http.server.ThreadingHTTPServer((host, port), _HttpHandler)
    sys.stderr.write(f"[tdai-memory] Streamable HTTP server on http://{host}:{port}\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="tdai-memory MCP server (stdio by default; Streamable HTTP with --http)",
    )
    parser.add_argument("--http", action="store_true", help="run as a Streamable HTTP MCP server")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8423, help="HTTP bind port (default 8423)")
    args = parser.parse_args()
    if args.http:
        main_http(args.host, args.port)
    else:
        main()
