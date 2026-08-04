#!/usr/bin/env python3
"""tdai-memory-mcp — exposes TencentDB Agent Memory (memory-core v3) as MCP tools.

Pure-stdlib implementation (urllib + json + sys). Speaks the Model Context
Protocol over stdio (JSON-RPC 2.0, newline-delimited) so it can be used as a
local MCP server in opencode / Claude / any MCP host.

Configuration via environment variables:
  MEMORY_ENDPOINT    default http://127.0.0.1:8420
  MEMORY_API_KEY     default local        (gateway bearer token)
  MEMORY_SERVICE_ID  default default      (x-tdai-service-id)
  MEMORY_TEAM_ID     default default
  MEMORY_AGENT_ID    default opencode
  MEMORY_USER_ID     default ariq

All models sharing these values read/write the SAME shared team memory.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error

ENDPOINT = os.environ.get("MEMORY_ENDPOINT", "http://127.0.0.1:8420").rstrip("/")
API_KEY = os.environ.get("MEMORY_API_KEY", "local")
SERVICE_ID = os.environ.get("MEMORY_SERVICE_ID", "default")
TEAM_ID = os.environ.get("MEMORY_TEAM_ID", "default")
AGENT_ID = os.environ.get("MEMORY_AGENT_ID", "opencode")
USER_ID = os.environ.get("MEMORY_USER_ID", "ariq")
TIMEOUT = float(os.environ.get("MEMORY_TIMEOUT", "30"))

SERVER_NAME = "tdai-memory"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"


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


def handle(method: str, params: dict, req_id) -> None:
    if method == "initialize":
        _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
        return
    if method == "notifications/initialized":
        return
    if method == "tools/list":
        _result(req_id, {"tools": TOOLS})
        return
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        fn = DISPATCH.get(name)
        if not fn:
            _error(req_id, -32601, f"Unknown tool: {name}")
            return
        try:
            text = fn(**args)
            _result(req_id, {"content": [{"type": "text", "text": str(text)}]})
        except TypeError as e:
            _result(req_id, {"content": [{"type": "text", "text": f"Argument error: {e}"}], "isError": True})
        except Exception as e:
            _result(req_id, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
        return
    _error(req_id, -32601, f"Method not found: {method}")


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


if __name__ == "__main__":
    main()
