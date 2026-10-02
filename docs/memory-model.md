# Memory Model

Cortex is a persistent, tiered memory system for AI agents. Notes live in a
plain-Markdown vault. An encoding pipeline reads every note and routes it
to the right output based on its **tier** — so the agent loads only what it
needs, when it needs it.

## Core Concepts

### The Vault

An Obsidian-style directory of Markdown notes, each with YAML frontmatter.
Notes are grouped by what they describe:

```
your-vault/
├── feedback/       ← preferences, persona, standing rules
├── knowledge/      ← reference docs, patterns, procedures
├── entities/       ← projects, people, systems, teams
├── decisions/      ← records of choices
└── logs/           ← session and event notes
```

### Note Types

Every note declares a `type` in its frontmatter:

| Type | Purpose |
|------|---------|
| `knowledge` | Patterns, reference material, how-tos |
| `entity` | Projects, people, systems, data structures |
| `feedback` | User preferences, persona, working style |
| `decision` | Records of choices with reasoning |
| `session` | Transient conversation summaries |
| `log` | Session logs, event trails |

Notes without a `type` are skipped entirely.

### The Tier System

Every note declares a `tier` that controls when it reaches the agent:

| Tier | When Loaded | Output Target |
|------|-------------|---------------|
| `core` | Every conversation, unconditionally | `core-context.md` |
| `skill:<name>` | Only when that skill is invoked | `skills/<name>/reference.md` |
| `project` | On demand, when the project comes up | `projects/<id>.md` |
| `vault-only` | Never | Nothing (stays in vault only) |

**Core** keeps the always-loaded footprint small — standing preferences,
persona, rules. **Skill** and **project** tiers load lazily on demand.
**Vault-only** keeps drafts, session notes, and research private.

The always-loaded context also includes a lightweight **pointer index** —
a table of contents that tells the agent what skill and project notes exist,
without loading their content. This lets the agent reason about available
knowledge and pull in detail when relevant.

### Encoding

The encoder (`cortex/encoder/core.py`, invoked via `cortex encode`) reads every note
in the vault and routes it to output targets based on tier:

```
vault notes
  → scan & parse frontmatter
  → tier routing:
      core            → core-context.md (eager, always loaded)
      skill:jira      → skills/jira/reference.md (lazy)
      skill:clarity   → skills/clarity-ppm/reference.md (lazy)
      project         → projects/<id>.md (lazy)
      vault-only      → skip
  → write changed outputs (idempotent — only writes diff)
```

No database, no cloud service, no background daemon. Files in, files out.

### Capture-Then-Rebuild

The everyday workflow is **capture first, rebuild second**:

1. **Capture** — the agent writes notes into the vault during conversation
   (preferences, decisions, patterns, session summaries) using
   `cortex memory write`. Individual writes trigger an automatic rebuild in
   the background (unless `--no-encode` is passed).
2. **Rebuild** — `cortex encode` regenerates all encoded outputs from the
   current vault state. This is the explicit step you run after a batch of
   manual edits.

A "sync" is this two-step process. The rebuild without capture is just a
rebuild — it re-emits what's already there. The capture without a rebuild
leaves the encoded outputs stale.

## Scoring

Search ranks notes by weighted matches across the note's id, aliases, tags,
category, and body. Id and alias hits score highest; body matches score
lowest. Related-note scoring weights shared tags most heavily, then category,
then type. The scoring is deliberately simple and explainable — you can
predict what will come back.

## Vault-Only Notes

`vault-only` is a deliberate feature. A note in this tier can be written by
the agent but is never encoded back into its context. This gives you:

- **Signal filtering** — the agent captures observations without them
  becoming established fact on the next turn. You review and promote the
  solid ones.
- **A private workspace** — half-formed thinking stays yours.
- **An audit trail** — if the agent records something wrong, you can see it
  and correct the source.
- **Zero context cost** — the agent can be proactive about capture without
  bloating the always-loaded budget.

`session` and `log` note types default to this tier. Promote a note later by
changing its `tier` and re-encoding.

## Which Notes Reach the Index

Two independent gates decide whether a vault file appears in `memory.json`:

1. `vault_only_types` (`session`, `log`) — dropped by `excluded()` before the
   type filter ever runs.
2. `include_types` — a whitelist. A note whose `type` is absent is not indexed.

**`include_types` must list every type you intend to search.** This is not a
preference list. The full encode rebuilds the index from exactly this set, while
the CLI's inline write path does *not* filter by type at all. So a note of an
unlisted type:

- is searchable immediately after `cortex memory write`
- vanishes from the index on the next `cortex encode`

That asymmetry means search results depend on whether the last write went through
the CLI or a rebuild — the same vault, two different answers, no error either
time. Types outside `include_types` are dropped silently, so the failure shows
up as "my note disappeared", not as a config complaint.

The default covers `knowledge`, `entity`, `decision`, `feedback`, and `risk`.
`meta` is intentionally absent (it is the vault index file, not a note). A note
holding a live compliance or security finding is `type: risk`, `tier: project` —
excluded by default it was searchable only until the next rebuild, which is how a
Minstrel PII attestation gap sat unsearchable.

## Encode Guards

`cortex encode` refuses to run in two situations, because both cause **silent
writes to the wrong vault**:

**Output paths outside the vault.** `core_context`, `projects`, and
`python-agents` outputs are derived state belonging to the vault being encoded,
so their paths must resolve inside it. Relative paths resolve against the vault
root, which keeps a portable config portable. The `skills` target is exempt — it
deploys `reference.md` into a configured agent skills directory, which is
outside the vault by design.

**Config location disagreeing with `vault_path`.** The vault being encoded comes
from `vault_path` *inside* the config, not from where the config file sits. So
`cortex encode --config <copy>/_sync/cortex.yaml` encodes whatever that copy's
YAML names — normally the original vault. The copy's notes are ignored and the
original is rebuilt, dropping anything the rebuild's filters exclude. If the
config lives in a `_sync/` directory it implies a vault; when that disagrees
with the declared `vault_path`, the encode aborts.

`cortex encode --show-config` bypasses both guards — it is how you inspect the
mismatch — but it also bypasses them, so it is the only diagnostic that will run.

## Versioning

Cortex tracks two independent numbers:

| Number | File | Meaning |
|--------|------|---------|
| Release version (SemVer) | `VERSION` | Which release of the toolchain |
| Schema version (integer) | `SCHEMA_VERSION` | The on-disk data contract |

Every encode run compares the code's schema version against the vault's. If
the vault is newer, the encoder refuses to run (no silent downgrades). If
the code is newer, it auto-migrates (backing up first). Check status with
`cortex encode --check` or `cortex status`.
