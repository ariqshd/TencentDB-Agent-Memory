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
KNOWLEDGE_ENDPOINT = os.environ.get("KNOWLEDGE_ENDPOINT", "http://127.0.0.1:8424").rstrip("/")
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

# Optional per-request scope overrides (HTTP mode). A remote client may join any
# team/agent/user scope by sending these headers on every request; values fall
# back to the MEMORY_TEAM_ID / MEMORY_AGENT_ID / MEMORY_USER_ID env defaults.
SCOPE_HEADERS = {
    "x-tdai-team-id": "team_id",
    "x-tdai-agent-id": "agent_id",
    "x-tdai-user-id": "user_id",
}
_scope = threading.local()


def _post_to(base_url: str, path: str, body: dict) -> dict:
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base_url}{path}",
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
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None
    except Exception as e:
        raise RuntimeError(f"unreachable: {e}") from None
    envelope = json.loads(raw)
    if envelope.get("code") != 0:
        raise RuntimeError(
            f"code={envelope.get('code')}: {envelope.get('message')}"
        )
    return envelope.get("data") or {}


def _post(path: str, body: dict) -> dict:
    return _post_to(ENDPOINT, path, body)


def _post_knowledge(path: str, body: dict) -> dict:
    return _post_to(KNOWLEDGE_ENDPOINT, path, body)


def _iso() -> dict:
    """Current scope identity: per-request header overrides, else env defaults."""
    over = getattr(_scope, "ids", None) or {}
    return {
        "team_id": over.get("team_id") or TEAM_ID,
        "agent_id": over.get("agent_id") or AGENT_ID,
        "user_id": over.get("user_id") or USER_ID,
    }


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


# ── Skill tools ─────────────────────────────────────────────────────────────

def t_skill_search(query: str, top_k: int = 5) -> str:
    """Search the team's skill library by keyword."""
    top_k = max(1, min(int(top_k or 5), 20))
    data = _post("/v3/skill/search", {**_iso(), "query": query, "top_k": top_k})
    items = data.get("skills") or data.get("items") or []
    if not items:
        return "No skills found."
    lines = [f"Skills ({len(items)}):"]
    for s in items[:top_k]:
        lines.append(f"  [{s.get('skill_id', '?')}] {s.get('name', '?')}: {str(s.get('content', ''))[:200]}")
    return "\n".join(lines)


def t_skill_create(name: str, content: str, description: str = "") -> str:
    """Create a new skill in the team library.

    name must be lowercase letters/digits/hyphens (e.g. 'k8s-crashloop-triage').
    content is wrapped in SKILL.md frontmatter automatically if you pass plain text;
    for full control pass content already starting with '---'.
    """
    if not content.startswith("---"):
        desc = description or name.replace("-", " ")
        content = f"---\nname: {name}\ndescription: {desc}\n---\n\n{content}\n"
    data = _post("/v3/skill/create", {**_iso(), "name": name, "content": content})
    return f"Skill created: {data.get('skill_id', '?')} ({name})."


def t_skill_list() -> str:
    """List all skills for the current team/agent."""
    data = _post("/v3/skill/list", _iso())
    items = data.get("skills") or data.get("items") or []
    if not items:
        return "No skills found."
    lines = [f"Skills ({len(items)}):"]
    for s in items:
        lines.append(f"  [{s.get('skill_id', '?')}] {s.get('name', '?')} (v{s.get('version', '?')})")
    return "\n".join(lines)


# ── Wiki tools ──────────────────────────────────────────────────────────────

def t_wiki_create(name: str) -> str:
    """Create a wiki knowledge base (metadata + shell). Call wiki_ingest after."""
    data = _post_knowledge("/v3/wiki/create", {**_iso(), "name": name})
    return f"Wiki created: {data.get('wiki_id', '?')} ({name}). Call wiki_ingest to build it."


def t_wiki_search(wiki_id: str, query: str, limit: int = 10) -> str:
    """Search wiki pages by keyword."""
    limit = max(1, min(int(limit or 10), 50))
    data = _post_knowledge("/v3/wiki/search", {"wiki_id": wiki_id, "query": query, "limit": limit})
    items = data.get("pages") or data.get("items") or data.get("results") or []
    if not items:
        return "No wiki pages found."
    lines = [f"Wiki pages ({len(items)}):"]
    for p in items[:limit]:
        lines.append(f"  {str(p.get('title', p.get('ref', '?')))[:200]}")
    return "\n".join(lines)


def t_wiki_ingest(wiki_id: str) -> str:
    """Trigger async ingestion/build for a wiki. Returns immediately."""
    _post_knowledge("/v3/wiki/ingest", {"wiki_id": wiki_id})
    return f"Wiki ingest triggered for {wiki_id}. Check status via the Panel."


def t_wiki_page_read(wiki_id: str, refs: str) -> str:
    """Read wiki page content by ref(s). refs: JSON array string or single ref."""
    try:
        ref_list = json.loads(refs) if isinstance(refs, str) else [refs]
    except json.JSONDecodeError:
        ref_list = [str(refs)]
    if not isinstance(ref_list, list):
        ref_list = [str(ref_list)]
    data = _post_knowledge("/v3/wiki/page/read", {"wiki_id": wiki_id, "refs": ref_list[:20]})
    pages = data.get("pages") or data.get("items") or []
    if not pages:
        return "No pages returned."
    parts = []
    for p in pages:
        parts.append(f"=== {p.get('ref', p.get('title', '?'))} ===\n{str(p.get('content', p.get('markdown', '')))[:2000]}")
    return "\n\n".join(parts)


# ── Code-Graph tools ────────────────────────────────────────────────────────

def t_codegraph_create(repo_url: str, branch: str = "main", repo_name: str = "") -> str:
    """Register a code repository for async clone + indexing."""
    body = {**_iso(), "repo_url": repo_url, "branch": branch}
    if repo_name:
        body["repo_name"] = repo_name
    data = _post_knowledge("/v3/code-graph/create", body)
    cg_id = data.get("code_graph_id", "?")
    return f"Code-Graph created: {cg_id}. Indexing runs async — check status via codegraph_status or the Panel."


def t_codegraph_search(code_graph_id: str, query: str, kind: str = "any", limit: int = 10) -> str:
    """Search indexed code symbols/files by keyword."""
    limit = max(1, min(int(limit or 10), 50))
    data = _post_knowledge("/v3/code-graph/search", {
        "code_graph_id": code_graph_id, "query": query, "kind": kind, "limit": limit,
    })
    items = data.get("nodes") or data.get("symbols") or data.get("items") or data.get("results") or []
    if not items:
        return "No code matches found."
    lines = [f"Code matches ({len(items)}):"]
    for n in items[:limit]:
        lines.append(f"  [{n.get('kind', '?')}] {n.get('name', '?')} @ {n.get('file', n.get('path', '?'))}:{n.get('line', '?')}")
    return "\n".join(lines)


def t_codegraph_explore(code_graph_id: str, query: str, max_files: int = 12) -> str:
    """Find files relevant to a natural-language query (semantic explore)."""
    max_files = max(1, min(int(max_files or 12), 50))
    data = _post_knowledge("/v3/code-graph/explore", {
        "code_graph_id": code_graph_id, "query": query, "maxFiles": max_files,
    })
    items = data.get("files") or data.get("items") or data.get("results") or []
    if not items:
        return "No relevant files found."
    lines = [f"Relevant files ({len(items)}):"]
    for f in items[:max_files]:
        lines.append(f"  {f.get('path', f.get('file', '?'))} (score: {f.get('score', '?')})")
    return "\n".join(lines)


def t_codegraph_callers(code_graph_id: str, symbol: str, limit: int = 20) -> str:
    """Find all callers of a function/method symbol."""
    limit = max(1, min(int(limit or 20), 100))
    data = _post_knowledge("/v3/code-graph/callers", {
        "code_graph_id": code_graph_id, "symbol": symbol, "limit": limit,
    })
    items = data.get("callers") or data.get("nodes") or data.get("items") or []
    if not items:
        return "No callers found."
    lines = [f"Callers of '{symbol}' ({len(items)}):"]
    for c in items[:limit]:
        lines.append(f"  [{c.get('kind', '?')}] {c.get('name', '?')} @ {c.get('file', '?')}:{c.get('line', '?')}")
    return "\n".join(lines)


def t_codegraph_impact(code_graph_id: str, symbol: str, depth: int = 2) -> str:
    """Trace the impact/blast-radius of changing a symbol (transitive callers)."""
    depth = max(1, min(int(depth or 2), 10))
    data = _post_knowledge("/v3/code-graph/impact", {
        "code_graph_id": code_graph_id, "symbol": symbol, "depth": depth,
    })
    items = data.get("impacts") or data.get("nodes") or data.get("items") or []
    if not items:
        return "No impact path found."
    lines = [f"Impact of '{symbol}' ({len(items)} affected):"]
    for n in items[:30]:
        lines.append(f"  d{n.get('depth', '?')}: [{n.get('kind', '?')}] {n.get('name', '?')} @ {n.get('file', '?')}:{n.get('line', '?')}")
    return "\n".join(lines)


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
    {
        "name": "skill_search",
        "description": (
            "Search the team's skill library for reusable workflows and expertise. "
            "Skills are proven procedures extracted from past work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to find."},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "skill_create",
        "description": (
            "Create a new skill in the team library — a reusable workflow, procedure, "
            "or piece of expertise distilled from experience. The name must be lowercase "
            "letters/digits/hyphens. Content is auto-wrapped in SKILL.md frontmatter."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Lowercase, hyphens only (e.g. 'deploy-checklist')."},
                "content": {"type": "string", "description": "Full skill content/instructions (markdown body)."},
                "description": {"type": "string", "description": "One-line description (optional, defaults to name)."},
            },
            "required": ["name", "content"],
        },
    },
    {
        "name": "skill_list",
        "description": "List all skills for the current team/agent.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "wiki_create",
        "description": (
            "Create a wiki knowledge base (metadata + shell). After creating, call "
            "wiki_ingest to build it from source documents/repositories."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "wiki_search",
        "description": "Search wiki pages by keyword.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wiki_id": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["wiki_id", "query"],
        },
    },
    {
        "name": "wiki_ingest",
        "description": "Trigger async ingestion/build for a wiki. Returns immediately.",
        "inputSchema": {
            "type": "object",
            "properties": {"wiki_id": {"type": "string"}},
            "required": ["wiki_id"],
        },
    },
    {
        "name": "wiki_page_read",
        "description": "Read wiki page content by ref(s).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wiki_id": {"type": "string"},
                "refs": {"type": "string", "description": 'JSON array of refs, or a single ref string.'},
            },
            "required": ["wiki_id", "refs"],
        },
    },
    {
        "name": "codegraph_create",
        "description": (
            "Register a code repository for async clone + indexing into a code graph. "
            "Indexes symbols, files, call relationships, and impact paths."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_url": {"type": "string", "description": "HTTPS clone URL."},
                "branch": {"type": "string", "default": "main"},
                "repo_name": {"type": "string"},
            },
            "required": ["repo_url"],
        },
    },
    {
        "name": "codegraph_search",
        "description": "Search indexed code symbols/files by keyword.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code_graph_id": {"type": "string"},
                "query": {"type": "string"},
                "kind": {"type": "string", "enum": ["symbol", "file", "any"], "default": "any"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["code_graph_id", "query"],
        },
    },
    {
        "name": "codegraph_explore",
        "description": "Find files relevant to a natural-language query (semantic explore).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code_graph_id": {"type": "string"},
                "query": {"type": "string"},
                "max_files": {"type": "integer", "default": 12},
            },
            "required": ["code_graph_id", "query"],
        },
    },
    {
        "name": "codegraph_callers",
        "description": "Find all callers of a function/method symbol.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code_graph_id": {"type": "string"},
                "symbol": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["code_graph_id", "symbol"],
        },
    },
    {
        "name": "codegraph_impact",
        "description": (
            "Trace the impact/blast-radius of changing a symbol (transitive callers). "
            "Tells you what else might break if you change this code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code_graph_id": {"type": "string"},
                "symbol": {"type": "string"},
                "depth": {"type": "integer", "default": 2},
            },
            "required": ["code_graph_id", "symbol"],
        },
    },
]

DISPATCH = {
    "memory_search": t_memory_search,
    "memory_read_profile": t_memory_read_profile,
    "memory_save_core": t_memory_save_core,
    "memory_save_conversation": t_memory_save_conversation,
    "skill_search": t_skill_search,
    "skill_create": t_skill_create,
    "skill_list": t_skill_list,
    "wiki_create": t_wiki_create,
    "wiki_search": t_wiki_search,
    "wiki_ingest": t_wiki_ingest,
    "wiki_page_read": t_wiki_page_read,
    "codegraph_create": t_codegraph_create,
    "codegraph_search": t_codegraph_search,
    "codegraph_explore": t_codegraph_explore,
    "codegraph_callers": t_codegraph_callers,
    "codegraph_impact": t_codegraph_impact,
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
        result = {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": server_version},
        }
        # Expose the default scope so remote clients can see which team/agent/
        # user they land on (and can override via the x-tdai-* scope headers).
        result["_tdaiScope"] = _iso()
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
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
        # Errors can be raised before the request body is consumed; leaving it
        # unread on a kept-alive connection makes the next request on that
        # connection get misparsed (body bytes read as the request line). Close
        # the connection on errors so buffered body bytes are never reused.
        self.send_header("Connection", "close")
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
        overrides = {}
        for header, key in SCOPE_HEADERS.items():
            val = (self.headers.get(header) or "").strip()
            if val:
                overrides[key] = val
        _scope.ids = overrides
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
