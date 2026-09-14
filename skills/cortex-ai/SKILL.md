---
name: cortex-ai
description: Work with the Cortex vault — search notes, list by tier/type, add new notes, capture session knowledge, sync (capture-then-rebuild), import an existing agent, check the version or vault health, look up a note's vault path, or manage hive connection (shared vault across machines). Use when the user says "cortex", "search my notes", "add a note", "capture this", "cortex search", "cortex list", "cortex add", "cortex sync", "sync", "cortex get", "cortex open", "cortex import", "cortex version", "cortex status", "vault health", "update my memory", "cortex hive", "hive status", "hive push", "hive pull", or similar vault operations. For "auto-learn", "what did you learn?", "what should I capture?", "learning mode", or "learning check", load the auto-learn skill instead. On "sync", always capture session knowledge before rebuilding.
---

# Cortex AI

Persistent, tiered memory for AI agents. Notes live in an Obsidian-style vault
and are encoded into agent-consumable files.

**First stop for any Cortex vault operation.** The workflows below carry the
full guardrails — search-before-write, capture-then-rebuild sync order, drain
logic, tier guidance — that bare `cortex` CLI calls give up in exchange for
flexibility. Fall back to the CLI only when no workflow here covers the
operation (see Skill → CLI fallback below), and never reach for direct file
edits without Jay's approval — surface the gap instead.

Under the hood these workflows drive the `cortex` CLI:

**Read:** `cortex memory search` · `cortex memory get` · `cortex memory related` · `cortex memory think`
**Write:** `cortex memory write` — creates/updates a note and auto-triggers encoding

> **Paths.** Resolve vault paths at runtime: `cortex encode --show-config`
> (returns JSON with `vault_path`, `config_file`, `memory_json`, etc.).

---

## Core Commands

### search `<query>`

```
cortex memory search "<query>"
```

Display: `id · type/category · alias · snippet`
If one clear match, offer to fetch the full note with `cortex memory get <id>`.

### get `<id>`

```
cortex memory get <id>
```

Returns full content + metadata. Falls back to raw vault file if not in memory.json.

### related `<id>`

```
cortex memory related <id>
```

Use after a `search` or `get` to surface notes that connect to the same topic.
The command scores by shared tags, same category/type, and wiki-link graph
adjacency.

### think `<query>`

```
cortex memory think "<query>"
```

Synthesizes a rich context bundle: primary search results at full depth,
cross-references between them, related notes pulled in from the graph, and gap
analysis (thin coverage, narrow type spread, stale notes). Designed to answer a
question in one pass.

### learnings

`Learnings.md` at vault root is a scratch file for quick capture — no frontmatter
needed, just append. Entries in Learnings.md are **not** indexed by `memory search`
or returned by `memory think`. To make them visible to agents, promote periodically:

```
cortex memory write --title "<title>" --type knowledge --tier core --category patterns --body-file Learnings.md --root
```

Without `--root`, notes go into typed subdirectories (e.g. `knowledge/patterns/`).
With `--root`, the file lands at the vault root (e.g. `Learnings.md`).

Or use `capture` after a session to pull key points directly into proper notes.

### add `<title>`

Create a new vault note:

1. **Ask type**: `["knowledge", "entity", "feedback", "decision", "log", "session"]`
2. **Ask tier** (suggest from type): `["core", "skill:<name>", "project", "vault-only"]`
3. **Ask category** (suggest from type):
   - knowledge → `patterns` · `api` · `calendars` · `infrastructure`
   - entity → `projects` · `people` · `systems` · `teams`
   - feedback → `preferences` · `workflow`
4. **Ask tags** (comma-separated, or skip).
5. Write with `cortex memory write --title "..." --type ... --tier ... --body "..."`.
6. Print: `Note <created|updated>: <path>`

- `id` = slugified title (lowercase, hyphens)
- `aliases` = `["<title>"]`
- `updated` = today (`YYYY-MM-DD`)

To patch an existing note, add `--update`:
```
cortex memory write --title "..." --type ... --tier ... --body "..." --update
```

### capture

Evaluate the recent conversation for capturable content and write it to the vault:

1. For each capturable item, `cortex memory search` for an existing note.
2. `cortex memory write ... --body "..."` to create, or add `--update` to patch.
3. Report a one-line summary (id + created/updated).

Trigger phrases: "capture this", "encode that", "save what we decided", "update your memory".

### sync

A sync is **capture-then-rebuild-then-drain**, in this order:

1. **Capture first.** Scan the session for capturable content (preferences, corrections,
   non-obvious knowledge, decisions, project changes) and `cortex memory write` each item.
   If nothing meaningful happened, say so and skip. Do **not** skip silently.

2. **Then rebuild.**
   ```bash
   cortex encode
   ```

3. **Then drain spent session artifacts.** For each `log`/`session` note written *before
   this session*, patch `drained: true` via `cortex memory write ... --update`.
   Never drain notes written in the current session. Then purge:
   ```bash
   cortex encode --purge-apply
   ```

Trigger phrases: "sync", "cortex sync", "sync my vault".

**Optional: auto-learn step.** After drain, if the session produced substantive
content (>5 meaningful turns), optionally run the `auto-learn` skill's observe
→ propose cycle. The human decides what gets captured. Do not auto-write.

> **For full details** on sync drain guardrails, protected notes, and all advanced
> commands (version, status, uninstall, memory list, import, purge, portfolio,
> open, hive), read `reference.md` in this skill directory. Some land outside
> this skill's workflows — see Skill → CLI fallback below.

---

## Skill → CLI fallback

No workflow in this skill covers these — call the `cortex` CLI directly, do not
try to shoehorn them through the skill:

- `cortex encode` / `cortex encode --purge-apply` — rebuild / purge-apply the
  encoded output (the sync workflow runs these internally but only as a whole)
- `cortex status` / `cortex version` / `cortex encode --show-config` — health
  and diagnostics, no skill workflow
- `cortex memory compact` — dedupe pass
- `cortex memory list` — raw tier/type listing for bulk inspection
- `cortex import` — agent import

Anything the CLI has no command for (bulk edits, retiers, frontmatter repair)
means direct file edits — those require Jay's approval first. Surface the gap;
do not patch around it silently.

---

## Tier Guide

| tier | when to use |
|------|-------------|
| `core` | Standing preferences, persona, default behaviours — always loaded |
| `skill:<name>` | Heavy knowledge needed only when a specific skill loads |
| `project` | Project context (goals, roadmap, phase) |
| `vault-only` | Research, drafts, session notes — never encoded to agents |

---

`cortex memory write` triggers encoding automatically in the background
(unless `--no-encode` is passed). The CLI re-reads `memory.json` fresh on
every call, so no manual reload is needed.
