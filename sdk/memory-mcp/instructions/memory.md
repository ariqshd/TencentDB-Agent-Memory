# Shared Team Memory

You have access to a shared team memory via the `tdai-memory` MCP tools. This
memory persists across sessions and is shared by all models/users on this
machine and across all tailnet devices. Use it to recall context and persist
important knowledge.

## When to recall
- At the start of a task, call `memory_read_profile` (path: "core") to load the
  persistent L3 profile (identity, preferences, environment).
- When the user references something discussed before, call `memory_search` with
  a natural-language query to find relevant past memories.
- When working with code, call `skill_search` to find proven workflows or
  procedures relevant to the task (e.g. deployment checklists, triage steps).

## When to store
- Call `memory_save_conversation` after meaningful exchanges (decisions, facts the
  user shares, problem resolutions) so future sessions can recall them. Pass the
  relevant messages as a JSON array string.
- Call `memory_save_core` to update the persistent L3 profile when the user states
  a durable preference, identity fact, or environment detail worth remembering
  long-term. This OVERWRITES the profile, so include prior content plus additions.
- Call `skill_create` when the user establishes a repeatable workflow or procedure
  worth reusing (e.g. a deployment checklist, a debugging triage protocol).

## Code knowledge (Wiki + Code-Graph)
- `codegraph_create` — index a repository to enable symbol search, caller
  analysis, and impact tracing. Use when the user wants code-aware assistance on
  a specific repo.
- `codegraph_search` / `codegraph_explore` — find symbols or files relevant to a
  query in an indexed repo.
- `codegraph_callers` / `codegraph_impact` — before changing a function, check
  who calls it and what the blast radius is.
- `wiki_create` / `wiki_search` / `wiki_page_read` — create and query wiki
  knowledge bases built from documents.

## Notes
- `memory_search` level="atomic" (default) searches distilled memories; level=
  "conversation" searches raw conversation history.
- Keep saves concise — store the essential facts, not full transcripts.
- Skill names use lowercase letters, digits, and hyphens (e.g.
  `k8s-crashloop-triage`). Content is auto-wrapped in SKILL.md frontmatter.
- Code-Graph indexing is async — after `codegraph_create`, allow time before
  searching.
