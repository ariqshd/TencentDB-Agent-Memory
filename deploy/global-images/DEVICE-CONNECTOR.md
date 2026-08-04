# TencentDB Agent Memory — device connector

Two ways to give an **opencode** install on any tailnet device shared team
memory. Both coexist — pick one or use both.

| | Approach A: Proxy provider | Approach B: MCP server (new) |
|---|---|---|
| **Best for** | A specific model with **automatic** injection + extraction | **Any** model — memory as on-demand tools |
| **How** | Route LLM traffic through context-proxy | opencode manages providers; MCP tools call memory-core directly |
| **Models** | Only ones configured in the proxy upstream | Every model in the picker |
| **Memory** | Auto-injected into system prompt each turn | Model calls tools (`memory_search`, `memory_save_*`) |
| **Config duplication** | Model must exist in both device + proxy | Model defined **once**, in the device |

---

## Prereqs (both approaches)
- Device is on the Tailnet (`100.64.0.0/10`), reachable to `thinkcenter`.
- opencode installed.
- Your admin user-key. On the homelab:
  ```bash
  cat /home/ariq/codes/TencentDB-Agent-Memory/deploy/global-images/.admin-key
  ```

---

## Approach A — Proxy provider (automatic memory, single model)

Point opencode at the context-proxy. The proxy injects memory into the system
prompt and writes back each turn automatically — but only for models it knows
(currently `glm-5.2`).

### 1. Add the provider to `~/.config/opencode/opencode.jsonc`

Merge into the top level (sibling of `"mcp"`). If a `"provider"` key already
exists, merge `"tdai"` into it.

```jsonc
"provider": {
  "tdai": {
    "npm": "@ai-sdk/openai-compatible",
    "name": "TencentDB Memory",
    "options": { "baseURL": "http://llm.004141.xyz/proxy/default/v1" },
    "models": {
      "glm-5.2": { "name": "GLM-5.2 + Memory" }
    }
  }
}
```

### 2. Set the API key (must equal the admin user-key)

```bash
opencode auth login --provider tdai
# paste: sk-mem-<...>  (from the .admin-key file above)
```

### 3. Verify

```bash
curl -s http://llm.004141.xyz/proxy/default/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <user-key>" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"say pong"}],"max_tokens":512,"stream":false}'
```
Expected: `"content": "pong"`.

### 4. Use
Restart opencode, pick **GLM-5.2 + Memory** (`tdai` provider). Memory injection
and write-back are automatic.

---

## Approach B — MCP server (any model, tool-based memory)

opencode manages its own providers (GLM, Zen, OpenRouter, …) directly. A
lightweight MCP server exposes memory-core as tools the model calls on demand.
No proxy routing; no duplicate model config.

### 1. Ensure memory-core is reachable from the device

Memory-core runs on the homelab at `127.0.0.1:8420`. Expose it via Caddy on
the tailnet (one-time on thinkcenter):

```
# Caddyfile — add a route
mem-core.004141.xyz {
  reverse_proxy 127.0.0.1:8420
}
```

Plus a Cloudflare DNS A record for `mem-core` → tailnet IP (grey-cloud), same
as the existing `llm` / `mem` / `mem-api` records.

Verify from the device:
```bash
curl -s http://mem-core.004141.xyz/v3/core/read \
  -H "Authorization: Bearer local" \
  -H "x-tdai-service-id: default" \
  -H "Content-Type: application/json" \
  -d '{"team_id":"default","agent_id":"opencode","user_id":"test"}'
```

### 2. Get the MCP server on the device

The server is a single pure-stdlib Python file — no dependencies:

```bash
# from the repo
cp sdk/memory-mcp/server.py ~/tdai-memory-mcp-server.py
```

Or clone the repo and reference it in place.

### 3. Add to `~/.config/opencode/opencode.jsonc`

```jsonc
"mcp": {
  "tdai-memory": {
    "type": "local",
    "command": ["python3", "/home/<user>/tdai-memory-mcp-server.py"],
    "enabled": true,
    "timeout": 15000,
    "environment": {
      "MEMORY_ENDPOINT": "http://mem-core.004141.xyz",
      "MEMORY_API_KEY": "local",
      "MEMORY_SERVICE_ID": "default",
      "MEMORY_TEAM_ID": "default",
      "MEMORY_AGENT_ID": "opencode",
      "MEMORY_USER_ID": "<device-user>"
    }
  }
}
```

### 4. Add the memory instruction

Copy `sdk/memory-mcp/../instructions/memory.md` (or create your own) and
reference it:

```jsonc
"instructions": ["~/.config/opencode/instructions/memory.md"]
```

### 5. Use
Restart opencode. Pick **any** model from the picker. The model can now call
`memory_search`, `memory_read_profile`, `memory_save_core`, and
`memory_save_conversation` — all backed by the shared team memory.

---

## Reminders
- Traffic is plain HTTP over WireGuard — tailnet-only, keep it that way.
- The gateway key defaults to `local`. For tighter security set
  `MEMORY_CORE_GATEWAY_API_KEY` to a real secret in `.env` and restart the stack.
- Approach A memory write-back is async: L0 records each turn, extraction
  pipeline every ~5 conversations, persona on ~50.
- Approach B stores are explicit (model calls the tool). L0 conversations saved
  via `memory_save_conversation` are auto-distilled into L1 by memory-core.

## Other harnesses
- **Claude Code** (same proxy, no per-device install):
  ```bash
  export ANTHROPIC_BASE_URL=http://llm.004141.xyz/claude-code/default
  export ANTHROPIC_AUTH_TOKEN='<user-key>'
  claude --model glm-5.2
  ```

---
Manage the stack on the homelab:
```bash
sudo systemctl {start,stop,restart,status} tdai-memory
```
