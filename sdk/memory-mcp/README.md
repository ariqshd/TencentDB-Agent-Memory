# tdai-memory-mcp

A lightweight MCP (Model Context Protocol) server that exposes the TencentDB
Agent Memory **memory-core** v3 API as tools. Pure Python stdlib — zero
dependencies, runs anywhere `python3` exists.

Lives in the fork: `git clone git@github.com:ariqshd/TencentDB-Agent-Memory.git`
(see `sdk/memory-mcp/`).

## Why

The proxy-based approach (`tdai` provider in opencode) routes LLM traffic
through the context-proxy for automatic memory injection + extraction. That
works great but ties memory to a specific upstream model — every model you want
memory on must also be configured inside the proxy.

This MCP server takes the opposite approach: **opencode manages the providers,
memory is a tool layer on top.** Any model in the picker (GLM, Zen free,
OpenRouter free, …) gets shared team memory via tool calls — no proxy routing,
no duplicate model config.

Both approaches coexist. Use the proxy provider for auto-injection with a
specific model; use this MCP server for tool-based memory with any model.

## Tools

| Tool | Memory tier | Description |
|---|---|---|
| `memory_search` | L1 / L0 | Semantic search over distilled memories (atomic) or raw conversations |
| `memory_read_profile` | L3 / L2 | Read the persistent core profile and list scenario notes |
| `memory_save_core` | L3 | Overwrite the long-term core profile |
| `memory_save_conversation` | L0 | Save a conversation for recall + auto-distillation |

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `MEMORY_ENDPOINT` | `http://127.0.0.1:8420` | memory-core gateway URL |
| `MEMORY_API_KEY` | `local` | Gateway bearer token (`MEMORY_CORE_GATEWAY_API_KEY`) |
| `MEMORY_SERVICE_ID` | `default` | `x-tdai-service-id` |
| `MEMORY_TEAM_ID` | `default` | Team isolation key |
| `MEMORY_AGENT_ID` | `opencode` | Agent isolation key |
| `MEMORY_USER_ID` | `ariq` | User isolation key |
| `MEMORY_TIMEOUT` | `30` | HTTP timeout (seconds) |

All models sharing the same team/agent/user triple read/write the **same**
shared memory.

> **Important (web UI visibility):** the Memory Panel (`memory-hub`, port 8125)
> is **meta-layer asset-centric**. It lists memory *blocks* — one `chat_memory`
> asset per registered agent — and only then lazy-loads the L0/L1/L2/L3 layers
> (`/chat-memory/layer`). For the data written by this MCP server to appear in
> the Panel, the `MEMORY_TEAM_ID`/`MEMORY_AGENT_ID`/`MEMORY_USER_ID` must point
> at an **already-registered** meta-layer team/agent:
>
> 1. Register the team: `POST /v3/meta/team/create` (name + `owner_user_id`).
>    The API assigns the id — note it.
> 2. Register the agent: `POST /v3/meta/agent/create` under that team
>    (`owner_user_id` must be the caller). This auto-mints the `chat_memory`
>    asset + fixed-asset binding (`createAgent` → `ensureChatMemoryAsset`).
> 3. Set `MEMORY_TEAM_ID`, `MEMORY_AGENT_ID`, `MEMORY_USER_ID` to the returned
>    `team_id`, `agent_id`, and the asset's `owner_user_id` (the Panel reads
>    data-plane layers with `user_id = asset.owner_user_id`).
> 4. Restart opencode so the MCP server picks up the new environment.
>
> **Joining an existing shared block:** skip the registration above if you want
> to share an already-registered block — just reuse that identity's
> `team_id`/`agent_id`/`owner_user_id` on your device. The Panel block is keyed
> by (team, agent), so every device with the same triple shares one block.
> Registering a new agent would create a separate block instead.
>
> If the agent isn't registered, `ensureChatMemoryAsset` fails with
> `agent_not_found` (non-blocking warn) and the Panel shows nothing — even
> though search/read via MCP still works.

## Usage in opencode

```jsonc
"tdai-memory": {
  "type": "local",
  "command": ["python3", "/path/to/sdk/memory-mcp/server.py"],
  "enabled": true,
  "timeout": 15000,
  "environment": {
    "MEMORY_ENDPOINT": "http://127.0.0.1:8420",
    "MEMORY_API_KEY": "local",
    "MEMORY_SERVICE_ID": "default",
    "MEMORY_TEAM_ID": "default",
    "MEMORY_AGENT_ID": "opencode",
    "MEMORY_USER_ID": "ariq"
  }
}
```

Pair with the instruction file (`instructions/memory.md`) so the model knows
when to recall/store. See `deploy/global-images/DEVICE-CONNECTOR.md` for the
full device setup procedure.

## Architecture

```
┌─────────────┐     MCP stdio      ┌──────────────────┐     HTTP v3     ┌──────────────┐
│  opencode   │ ◄─────────────────► │  server.py       │ ◄─────────────► │  memory-core │
│ (any model) │   tools/call        │  (this server)   │   /v3/*         │  (port 8420) │
└─────────────┘                     └──────────────────┘                 └──────────────┘
```

The proxy pipeline is **not** in the path — memory-core is called directly.
The original proxy-based architecture continues to work independently for any
device/provider that routes through it.
