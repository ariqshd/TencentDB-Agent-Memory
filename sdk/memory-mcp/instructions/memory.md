# Shared Team Memory

You have access to a shared team memory via the `tdai-memory` MCP tools. This
memory persists across sessions and is shared by all models/users on this
machine. Use it to recall context and persist important knowledge.

## When to recall
- At the start of a task, call `memory_read_profile` (path: "core") to load the
  persistent L3 profile (identity, preferences, environment).
- When the user references something discussed before, call `memory_search` with
  a natural-language query to find relevant past memories.

## When to store
- Call `memory_save_conversation` after meaningful exchanges (decisions, facts the
  user shares, problem resolutions) so future sessions can recall them. Pass the
  relevant messages as a JSON array string.
- Call `memory_save_core` to update the persistent L3 profile when the user states
  a durable preference, identity fact, or environment detail worth remembering
  long-term. This OVERWRITES the profile, so include prior content plus additions.

## Notes
- `memory_search` level="atomic" (default) searches distilled memories; level=
  "conversation" searches raw conversation history.
- Keep saves concise — store the essential facts, not full transcripts.
