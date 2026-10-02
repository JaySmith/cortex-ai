#! /usr/bin/env python3
"""
cortex.encoder.core — Sync an Obsidian vault to agent-specific formats.

TIERED MODEL (driven by each note's `tier` frontmatter):
  core           -> eager: concatenated into a single core-context.md
  <custom-name>  -> any string listed in eager_tiers is also eager (e.g. "jira")
  skill:<name>   -> lazy: encoded body embedded into skills/<name>/reference.md
   project        -> lazy: written to encoded/projects/<id>.md
  vault-only     -> never encoded.

Config is read from cortex.yaml (co-located with this script by default;
in a normal install it lives at <vault>/_sync/cortex.yaml).

Usage via CLI:
  cortex encode                        # sync all enabled targets
  cortex encode --dry-run              # show changes without writing
  cortex encode --list                 # list all vault notes with tier
  cortex encode --show-config          # print resolved paths as JSON
  cortex encode --purge                # preview drained log/session deletions
  cortex encode --purge-apply          # delete drained logs/sessions + rebuild
  cortex encode --config /path/to.yaml # use a specific config file
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import yaml

from cortex.hub.client import HubClient, HubConnectionError
from cortex.vault.links import extract_wiki_links, resolve_wiki_links, strip_wiki_links

# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------


def cortex_version() -> str:
    """Read Cortex release version from the installed package."""
    from cortex import __version__

    return __version__


def schema_version() -> int:
    """Read the on-disk schema version this code understands (fallback 1)."""
    from cortex import SCHEMA_VERSION

    return SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def _migrate_1_to_2(vault_path: Path) -> None:
    """Add hive block to cortex.yaml if missing (schema v1 -> v2)."""
    cfg_file = vault_path / "_sync" / "cortex.yaml"
    if not cfg_file.exists():
        return
    text = cfg_file.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return
    if not isinstance(data, dict):
        return
    if "hive" in data:
        return
    text_lower = text.rstrip()
    hive_yaml = (
        "\n\n# --- Added by schema v2 migration ---\n"
        "hive:\n"
        "  enabled: false\n"
        '  hub_url: "http://localhost:4096/mcp"\n'
        '  machine_id: ""\n'
        '  hub_token: ""\n'
        "  replicate_tiers:\n"
        "    - core\n"
        '    - "skill:*"\n'
        "    - project\n"
        "  sync_interval: 300\n"
    )
    cfg_file.write_text(text_lower + hive_yaml, encoding="utf-8")
    print(f"    added hive block to {cfg_file}")


MIGRATIONS: dict[int, tuple[int, str, Callable[[Path], None]]] = {
    1: (2, "add hive config block to cortex.yaml", _migrate_1_to_2),
}


def read_vault_schema(vault_path: Path) -> int | None:
    """Return the schema version recorded in the vault's memory.json, or None."""
    for candidate in (vault_path / "_sync" / "encoded" / "memory.json",):
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                sv = data.get("_meta", {}).get("schema_version")
                return int(sv) if sv is not None else None
            except (OSError, ValueError, json.JSONDecodeError):
                return None
    return None


def check_and_migrate(vault_path: Path, dry: bool) -> None:
    """Compare code schema vs vault schema and reconcile."""
    code_schema = schema_version()
    vault_schema = read_vault_schema(vault_path)

    if vault_schema is None:
        return

    if vault_schema == code_schema:
        return

    if vault_schema > code_schema:
        sys.exit(
            f"ERROR: vault schema is v{vault_schema} but this Cortex code only "
            f"understands v{code_schema}.\n"
            f"       Your data is newer than the code. Update Cortex (git pull) "
            f"before running.\n"
            f"       Refusing to run to avoid corrupting your vault."
        )

    print(f"==> Schema migration needed: v{vault_schema} -> v{code_schema}")
    if not dry:
        backup_sync_dir(vault_path, reason="migration")

    current = vault_schema
    while current < code_schema:
        step = MIGRATIONS.get(current)
        if step is None:
            sys.exit(
                f"ERROR: no migration registered from schema v{current}. "
                f"Cannot safely upgrade. Report this."
            )
        to_v, desc, fn = step
        print(f"    migrating v{current} -> v{to_v}: {desc}")
        if not dry:
            fn(vault_path)
        current = to_v
    print(f"    migration complete (now v{code_schema})")


def backup_sync_dir(vault_path: Path, reason: str = "backup") -> Path:
    """Snapshot the vault's _sync/ into a timestamped backup folder."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = vault_path / "_sync" / "backups" / f"{stamp}-{reason}"
    backup_root.mkdir(parents=True, exist_ok=True)
    sync_dir = vault_path / "_sync"
    for item in ("cortex.yaml", "encoded", "last-sync.json"):
        src = sync_dir / item
        if not src.exists():
            continue
        dst = backup_root / item
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    print(f"    backed up _sync/ -> {backup_root}")
    return backup_root


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config(config_file: Path) -> dict:
    from cortex.config.loader import load_config as _load

    return _load(config_file)


def validate_target_config(name: str, cfg: dict, required_keys: list[str]) -> bool:
    """Return True if all required keys are present; print error otherwise."""
    for key in required_keys:
        if key not in cfg:
            print(f"  ERROR: target '{name}' is missing required config key '{key}' — skipping.")
            return False
    return True


# Targets whose outputs are derived state belonging to the vault being encoded.
# The skill-embed target is deliberately absent: it deploys reference.md into a
# configured agent skills directory, which is outside the vault by design.
_VAULT_ARTIFACT_TARGETS = ("core_context", "projects", "python-agents")
_VAULT_ARTIFACT_KEYS = ("output_file", "output_dir")


def validate_vault_artifact_paths(vault: Path, targets: dict) -> None:
    """Refuse to write derived vault artifacts outside the vault being encoded.

    cortex.yaml stores absolute output paths, and `cortex encode --config X`
    takes the *config* location as input while ignoring it for output. That
    combination is silent: point a copied vault's config at the encode command
    and it cheerfully rewrites the original vault's memory.json, because the
    path in the config still names the original. The vault files survive, but
    the index is rebuilt from a vault nobody asked about — and it will have
    dropped any note the copy added.

    Checking up front, before any target writes, means a misconfigured run
    fails without touching anything. Relative paths resolve against the vault
    root, so a portable config stays portable.
    """
    vault_root = vault.resolve()
    for name in _VAULT_ARTIFACT_TARGETS:
        cfg = targets.get(name) or {}
        if not cfg.get("enabled"):
            continue
        for key in _VAULT_ARTIFACT_KEYS:
            raw = cfg.get(key)
            if not raw:
                continue
            resolved = Path(raw).expanduser()
            if not resolved.is_absolute():
                resolved = vault_root / resolved
            resolved = resolved.resolve()
            if resolved != vault_root and vault_root not in resolved.parents:
                sys.exit(
                    f"ERROR: target '{name}.{key}' points outside the vault being encoded.\n"
                    f"       configured: {raw}\n"
                    f"       vault:       {vault_root}\n"
                    f"\n"
                    f"  Encoding this vault would overwrite a DIFFERENT vault's files.\n"
                    f"  Use a path inside {vault_root / '_sync' / 'encoded'}, or a path\n"
                    f"  relative to the vault root."
                )


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def parse_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content
    yaml_block = content[3:end].strip("\n")
    body = content[end + 4 :].strip()
    try:
        meta = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        return {}, body
    if not isinstance(meta, dict):
        return {}, body
    return meta, body


# strip_wiki_links and extract_wiki_links are now in cortex.vault.links


def strip_leading_h1(text: str) -> str:
    """Drop a note body's own leading `# Title` (redundant with encoder heading)."""
    stripped = text.lstrip("\n")
    if not stripped.startswith("# "):
        return text
    parts = stripped.split("\n", 1)
    rest = parts[1] if len(parts) > 1 else ""
    return rest.lstrip("\n")


def strip_related_section(text: str) -> str:
    """Drop a trailing `## Related` section (vault nav; dead weight in encoded context)."""
    return re.sub(
        r"\n#+\s+Related\b.*\Z",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ).rstrip()


# ---------------------------------------------------------------------------
# Vault note
# ---------------------------------------------------------------------------


class VaultNote:
    @staticmethod
    def _as_str(v: object) -> str:
        if v is None:
            return ""
        return str(v)

    @staticmethod
    def _as_list(v: object) -> list[str]:
        if v is None or v == "":
            return []
        if isinstance(v, list):
            return [str(x) for x in v]
        return [str(v)]

    def __init__(self, path: Path, meta: dict, body: str) -> None:
        self.path = path
        self.meta = meta
        self.body = body
        self.name: str = self._as_str(meta.get("id")) or path.stem
        self.note_type: str = self._as_str(meta.get("type")) or "unknown"
        self.category: str = self._as_str(meta.get("category"))
        self.tier: str = self._as_str(meta.get("tier"))
        self.tags: list[str] = self._as_list(meta.get("tags"))
        self.agents: list[str] = self._as_list(meta.get("agents"))
        self.aliases: list[str] = self._as_list(meta.get("aliases"))
        self.updated: str = self._as_str(meta.get("updated"))
        self.drained: bool = meta.get("drained") is True
        self.expires_at: str = self._as_str(meta.get("expires_at"))
        self.hive: bool | None = None
        raw_hive = meta.get("hive")
        if raw_hive is True:
            self.hive = True
        elif raw_hive is False:
            self.hive = False

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            exp = datetime.strptime(self.expires_at, "%Y-%m-%d")
            return datetime.now() > exp
        except ValueError:
            return False

    def title(self) -> str:
        if self.aliases:
            return self.aliases[0]
        return self.name.replace("-", " ").title()

    def clean_body(
        self,
        strip_links: bool = True,
        drop_h1: bool = True,
        drop_related: bool = False,
    ) -> str:
        body = self.body
        if strip_links:
            body = strip_wiki_links(body)
        if drop_h1:
            body = strip_leading_h1(body)
        if drop_related:
            body = strip_related_section(body)
        return body

    def to_dict(self) -> dict:
        d = {
            "id": self.name,
            "type": self.note_type,
            "category": self.category,
            "tier": self.tier,
            "tags": self.tags,
            "updated": self.updated,
            "aliases": self.aliases,
            "content": strip_wiki_links(self.body),
        }
        if self.expires_at:
            d["expires_at"] = self.expires_at
        return d


def scan_vault(
    vault_path: Path,
    skip_dirs: set[str] | None = None,
    *,
    require_type: bool = True,
) -> list[VaultNote]:
    """Scan vault_path recursively, returning all notes.

    Parameters
    ----------
    vault_path : Path
        Root of the vault.
    skip_dirs : set[str] | None
        Directory names to skip entirely (default: ``{"templates"}``).
    require_type : bool
        If True (default), skip any file without a ``type`` frontmatter field.
        Set to False for linting, so notes missing ``type`` can be detected.
    """
    if skip_dirs is None:
        skip_dirs = {"templates"}

    notes: list[VaultNote] = []
    seen_ids: dict[str, Path] = {}

    for md in vault_path.rglob("*.md"):
        rel = md.relative_to(vault_path)
        parts = rel.parts
        if any(p.startswith(".") or p.startswith("_") for p in parts):
            continue
        if parts[0] in skip_dirs:
            continue
        try:
            content = md.read_text(encoding="utf-8")
        except Exception as e:
            print(f"  WARNING: Could not read {md}: {e}")
            continue
        meta, body = parse_frontmatter(content)
        if require_type and not meta.get("type"):
            continue
        note = VaultNote(md, meta, body)
        if note.name in seen_ids:
            print(
                f"  WARNING: duplicate id '{note.name}' in {note.path} "
                f"(first seen at {seen_ids[note.name]}) — both kept, "
                "last write wins in dict targets",
                file=sys.stderr,
            )
        else:
            seen_ids[note.name] = note.path
        notes.append(note)
    return notes


def excluded(note: VaultNote, exclude_tags: set[str], vault_only_types: set[str]) -> bool:
    if note.note_type in vault_only_types:
        return True
    return bool(set(note.tags) & exclude_tags)


# ---------------------------------------------------------------------------
# Graph extraction
# ---------------------------------------------------------------------------


def build_wiki_graph(notes: list[VaultNote]) -> dict:
    """Parse [[wiki-links]] from all note bodies into a directed graph with analytics."""
    edges, dangling = resolve_wiki_links(notes)

    id_set = {n.name for n in notes}
    out_degree: dict[str, int] = {n.name: 0 for n in notes}
    in_degree: dict[str, int] = {n.name: 0 for n in notes}

    for e in edges:
        out_degree[e["source"]] = out_degree.get(e["source"], 0) + 1
        in_degree[e["target"]] = in_degree.get(e["target"], 0) + 1

    total_degree = {}
    for nid in id_set:
        total_degree[nid] = out_degree.get(nid, 0) + in_degree.get(nid, 0)

    isolated = sorted(nid for nid, d in total_degree.items() if d == 0)

    degrees = sorted(total_degree.values())
    if degrees:
        median = degrees[len(degrees) // 2]
        mean = sum(degrees) / len(degrees)
        variance = sum((d - mean) ** 2 for d in degrees) / len(degrees)
        stdev = variance**0.5
        threshold = median + 2 * stdev
    else:
        threshold = 0
    god_nodes = sorted(
        [{"id": nid, "degree": d} for nid, d in total_degree.items() if d > threshold],
        key=lambda x: x["degree"],
        reverse=True,
    )

    nodes = [
        {
            "id": n.name,
            "type": n.note_type,
            "category": n.category,
            "degree": total_degree.get(n.name, 0),
        }
        for n in notes
    ]

    return {
        "nodes": nodes,
        "edges": edges,
        "dangling": dangling,
        "isolated": isolated,
        "god_nodes": god_nodes,
    }


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


def write_file(path: Path, content: str, dry: bool) -> None:
    """Write content to path, skipping if content is already identical."""
    if dry:
        print(f"  [DRY] write {path} ({len(content)} chars)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        print(f"  unchanged {path}")
        return
    path.write_text(content, encoding="utf-8")
    print(f"  wrote {path} ({len(content)} chars)")


def sync_core_context(
    notes: list[VaultNote],
    cfg: dict,
    eager_tiers: list[str],
    strip_links: bool,
    dry: bool,
) -> Path | None:
    if not validate_target_config("core_context", cfg, ["output_file"]):
        return None
    out = Path(cfg["output_file"])
    eager = [n for n in notes if n.tier in eager_tiers]
    eager.sort(key=lambda n: (eager_tiers.index(n.tier), n.name))
    print(f"\n--- core-context: {len(eager)} eager notes ({', '.join(eager_tiers)}) ---")

    parts: list[str] = [
        "# Core Context (auto-generated by cortex encode — do not edit)",
        "",
        "Always-loaded memory. Heavy/specialised knowledge is loaded on demand via skills.",
        "",
    ]
    for n in eager:
        parts.append(f"## {n.title()}")
        parts.append("")
        parts.append(n.clean_body(strip_links, drop_related=True))
        parts.append("")

    skill_topics: dict[str, list[str]] = {}
    project_notes: list[VaultNote] = []
    for n in notes:
        if n.tier.startswith("skill:"):
            skill_topics.setdefault(n.tier.split(":", 1)[1], []).append(n.title())
        elif n.tier == "project":
            project_notes.append(n)

    if skill_topics or project_notes:
        parts.append("## On-Demand Memory (load only when relevant)")
        parts.append("")
        if skill_topics:
            parts.append(
                "Specialized knowledge lives in skills. Invoke the skill and "
                "read its `reference.md`:"
            )
            parts.append("")
            for skill in sorted(skill_topics):
                topics = ", ".join(sorted(skill_topics[skill]))
                parts.append(f"- **{skill}** skill — {topics}")
            parts.append("")
        if project_notes:
            proj_dir = out.parent / "projects"
            parts.append("Project context (read the file when the project comes up):")
            parts.append("")
            for n in sorted(project_notes, key=lambda x: x.name):
                parts.append(f"- **{n.title()}** — `{proj_dir / (n.name + '.md')}`")
            parts.append("")

    content = "\n".join(parts).rstrip() + "\n"
    write_file(out, content, dry)
    return out


def _resolve_skill_dirs(cfg: dict) -> list[Path]:
    """Build an ordered list of skill search dirs.

    Supports both the singular ``skills_dir`` (a string) and the plural
    ``skill_dirs`` (a list). When both are set, ``skills_dir`` is searched
    first, then each entry in ``skill_dirs`` in order. Duplicates are removed
    while preserving order.
    """
    dirs: list[Path] = []
    primary = cfg.get("skills_dir")
    if primary:
        dirs.append(Path(primary).expanduser())
    extra = cfg.get("skill_dirs") or []
    if isinstance(extra, str):
        extra = [extra]
    for d in extra:
        dirs.append(Path(d).expanduser())
    # de-dupe, preserve order
    seen: set[Path] = set()
    ordered: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered


def sync_skill_embeds(notes: list[VaultNote], cfg: dict, strip_links: bool, dry: bool) -> list[str]:
    search_dirs = _resolve_skill_dirs(cfg)
    if not search_dirs:
        print(
            "  ERROR: target 'skills' needs 'skills_dir' or 'skill_dirs' — skipping."
        )
        return []
    embed_name = cfg.get("embed_filename", "reference.md")
    by_skill: dict[str, list[VaultNote]] = {}
    for n in notes:
        if n.tier.startswith("skill:"):
            by_skill.setdefault(n.tier.split(":", 1)[1], []).append(n)
    print(
        f"\n--- skill-embeds: {sum(len(v) for v in by_skill.values())} "
        f"notes -> {len(by_skill)} skills ---"
    )
    written: list[str] = []
    for skill, ns in sorted(by_skill.items()):
        target_dir = next(
            (d / skill for d in search_dirs if (d / skill).exists()), None
        )
        if target_dir is None:
            searched = ", ".join(str(d / skill) for d in search_dirs)
            print(f"  WARNING: skill dir missing, skipping: {searched}")
            continue
        ns.sort(key=lambda n: n.name)
        parts: list[str] = [
            f"# {skill} — Reference (auto-generated by cortex encode — do not edit)",
            "",
            "Encoded memory for this skill. Loaded only when the skill is invoked.",
            "",
        ]
        for n in ns:
            parts.append(f"## {n.title()}")
            parts.append("")
            parts.append(n.clean_body(strip_links))
            parts.append("")
        content = "\n".join(parts).rstrip() + "\n"
        write_file(target_dir / embed_name, content, dry)
        written.append(skill)
    return written


def sync_projects(notes: list[VaultNote], cfg: dict, strip_links: bool, dry: bool) -> Path | None:
    if not validate_target_config("projects", cfg, ["output_dir"]):
        return None
    out_dir = Path(cfg["output_dir"])
    projs = [n for n in notes if n.tier == "project"]
    print(f"\n--- project-context: {len(projs)} notes ---")
    if not dry:
        out_dir.mkdir(parents=True, exist_ok=True)
    keep: set[str] = set()
    for n in sorted(projs, key=lambda n: n.name):
        fn = f"{n.name}.md"
        keep.add(fn)
        parts = [f"# {n.title()}", "", n.clean_body(strip_links), ""]
        write_file(out_dir / fn, "\n".join(parts).rstrip() + "\n", dry)
    if out_dir.exists():
        for f in out_dir.glob("*.md"):
            if f.name not in keep:
                if dry:
                    print(f"  [DRY] prune stale {f}")
                else:
                    f.unlink()
                    print(f"  pruned stale {f}")
    return out_dir


def sync_python_agents(
    notes: list[VaultNote],
    cfg: dict,
    exclude_tags: list[str],
    vault_only_types: list[str],
    dry: bool,
) -> None:
    if not validate_target_config("python-agents", cfg, ["output_file"]):
        return
    out = Path(cfg["output_file"])
    inc: list[str] = cfg.get("include_types", [])
    extra_excl: list[str] = cfg.get("exclude_tags", [])

    all_exclude: set[str] = set(exclude_tags) | set(extra_excl)
    vault_only_set: set[str] = set(vault_only_types)

    matching = [
        n for n in notes if n.note_type in inc and not excluded(n, all_exclude, vault_only_set)
    ]
    print(f"\n--- python-agents (json): {len(matching)} notes ---")
    graph = build_wiki_graph(notes)
    adjacency: dict[str, list[str]] = {}
    for edge in graph["edges"]:
        adjacency.setdefault(edge["source"], []).append(edge["target"])
        adjacency.setdefault(edge["target"], []).append(edge["source"])
    _now = datetime.now().isoformat()
    result = {
        "_meta": {
            # Both stamped on a full rebuild: a full encode is simultaneously the
            # last write and the last reconcile, so the two never diverge here.
            # They only diverge via the CLI's inline write/delete paths, which
            # move `generated` and leave `last_full_encode` alone.
            "generated": _now,
            "last_full_encode": _now,
            "source": "Cortex vault",
            "cortex_version": cortex_version(),
            "schema_version": schema_version(),
            "count": len(matching),
            "graph_edges": len(graph["edges"]),
            "graph_dangling": len(graph["dangling"]),
            "graph_isolated": len(graph["isolated"]),
        },
        "notes": {n.name: n.to_dict() for n in sorted(matching, key=lambda n: n.name)},
        "_graph": {
            "edges": graph["edges"],
            "adjacency": adjacency,
        },
    }
    write_file(out, json.dumps(result, indent=2, ensure_ascii=False), dry)


def prune_encoded_opencode(core_out: Path, dry: bool) -> None:
    d = core_out.parent
    if not d.exists():
        return
    keep = {core_out.name}
    for f in d.glob("*.md"):
        if f.name in keep:
            continue
        if dry:
            print(f"  [DRY] prune legacy encoded {f}")
        else:
            f.unlink()
            print(f"  pruned legacy encoded {f}")


def update_opencode_instructions(opencode_cfg_path: Path, core_out: Path, dry: bool) -> None:
    if not opencode_cfg_path.exists():
        print(f"  WARNING: opencode config not found: {opencode_cfg_path}")
        return
    text = opencode_cfg_path.read_text(encoding="utf-8")
    core_path_str = str(core_out)
    new_block = f'"instructions": [\n    "{core_path_str}"\n  ]'
    pattern = re.compile(r'"instructions"\s*:\s*\[.*?\]', re.DOTALL)
    if pattern.search(text):
        new_text = pattern.sub(new_block, text, count=1)
        action = "updated"
    else:
        new_text = text.replace('"lsp": true,', '"lsp": true,\n  ' + new_block + ",", 1)
        if new_text == text:
            print(
                f"  WARNING: Could not insert 'instructions' into "
                f"{opencode_cfg_path}.\n"
                f"           No existing 'instructions' key and no 'lsp' "
                f"anchor found.\n"
                f"           Add manually:\n"
                f'             "instructions": ["{core_path_str}"]'
            )
            return
        action = "inserted"

    if new_text == text:
        print("  opencode instructions unchanged (skipped write)")
        return

    if dry:
        print(f"  [DRY] would {action} opencode instructions -> [core-context.md]")
    else:
        opencode_cfg_path.write_text(new_text, encoding="utf-8")
        print(f"  {action} opencode instructions -> [core-context.md]")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def save_sync_state(vault_path: Path, notes: list[VaultNote]) -> None:
    state_file = vault_path / "_sync" / "last-sync.json"
    state = {
        "timestamp": datetime.now().isoformat(),
        "note_count": len(notes),
        "notes": {
            n.name: {
                "path": str(n.path.relative_to(vault_path)),
                "type": n.note_type,
                "tier": n.tier,
                "updated": n.updated,
                "hash": hashlib.md5(n.body.encode()).hexdigest(),
            }
            for n in notes
        },
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------

PURGEABLE_TYPES = ("log", "session")


def find_drained_notes(notes: list[VaultNote]) -> list[VaultNote]:
    return [n for n in notes if n.note_type in PURGEABLE_TYPES and n.drained]


def purge_drained_logs(vault_path: Path, apply: bool) -> list[VaultNote]:
    skip_dirs: set[str] = {"templates"}
    notes = scan_vault(vault_path, skip_dirs)
    drained = find_drained_notes(notes)

    print(f"\n--- purge: {len(drained)} drained log/session note(s) ---")
    if not drained:
        print("  nothing to purge (no notes flagged drained: true)")
        return []

    for n in drained:
        rel = n.path.relative_to(vault_path)
        if apply:
            n.path.unlink()
            print(f"  deleted {rel}")
        else:
            print(f"  [DRY] would delete {rel}  (type={n.note_type})")

    if not apply:
        print("\n  [DRY RUN] No files were deleted. Re-run with --purge-apply to delete.")
    return drained


# ---------------------------------------------------------------------------
# --show-config
# ---------------------------------------------------------------------------


def show_config(cfg: dict, cfg_path: Path) -> None:
    targets: dict = cfg.get("targets", {})
    cc = targets.get("core_context", {})
    pa = targets.get("python-agents", {})
    sk = targets.get("skills", {})
    pr = targets.get("projects", {})

    vault_schema = None
    vp = cfg.get("vault_path", "")
    if vp:
        vault_schema = read_vault_schema(Path(vp))

    resolved: dict[str, object] = {
        "cortex_version": cortex_version(),
        "schema_version": schema_version(),
        "vault_schema_version": vault_schema,
        "config_file": str(cfg_path.resolve()),
        "vault_path": cfg.get("vault_path", ""),
        "encode_script": str(Path(__file__).resolve()),
        "core_context": cc.get("output_file", ""),
        "memory_json": pa.get("output_file", ""),
        "skills_dir": sk.get("skills_dir", ""),
        "skill_dirs": [str(d) for d in _resolve_skill_dirs(sk)],
        "projects_dir": pr.get("output_dir", ""),
        "hive_enabled": cfg.get("hive", {}).get("enabled", False),
        "hive_hub_url": cfg.get("hive", {}).get("hub_url", ""),
        "hive_machine_id": cfg.get("hive", {}).get("machine_id", ""),
    }
    print(json.dumps(resolved, indent=2, ensure_ascii=False))


def run_check(vault_path: Path) -> None:
    code_v = cortex_version()
    code_schema = schema_version()
    vault_schema = read_vault_schema(vault_path)

    print(f"Cortex release version : {code_v}")
    print(f"Code schema version    : {code_schema}")
    print(
        f"Vault schema version   : {vault_schema if vault_schema is not None else '(none — fresh)'}"
    )

    if vault_schema is None:
        print("Status                 : OK — fresh vault; first encode will stamp schema.")
        return
    if vault_schema == code_schema:
        print("Status                 : OK — schema in sync.")
        return
    if vault_schema < code_schema:
        print(f"Status                 : MIGRATION PENDING (v{vault_schema} -> v{code_schema}).")
        print("                         Run encode to migrate (auto-backs up _sync/ first).")
        return
    print("Status                 : ERROR — vault is NEWER than this code.")
    print("                         Update Cortex (git pull) before running.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Hive
# ---------------------------------------------------------------------------


def hive_eligible(note: VaultNote, config: dict) -> bool:
    if note.hive is True:
        return True
    if note.hive is False:
        return False
    replicate = config.get("hive", {}).get("replicate_tiers", [])
    for tier_pattern in replicate:
        if tier_pattern.endswith("*"):
            if note.tier.startswith(tier_pattern[:-1]):
                return True
        elif note.tier == tier_pattern:
            return True
    return False


def hive_push(config: dict, notes: list[VaultNote] | None = None) -> int:
    hive_cfg = config.get("hive", {})
    if not hive_cfg.get("enabled"):
        sys.exit("ERROR: hive is not enabled in cortex.yaml")
    machine_id = hive_cfg.get("machine_id", "")
    if not machine_id:
        sys.exit("ERROR: hive.machine_id is not set in cortex.yaml")

    if notes is None:
        vault = Path(config["vault_path"])
        skip_dirs: set[str] = set(config.get("skip_dirs", ["templates"]))
        notes = scan_vault(vault, skip_dirs)

    client = HubClient(
        hive_cfg["hub_url"],
        token=hive_cfg.get("hub_token", ""),
    )
    client.connect()
    pushed = 0
    for note in notes:
        if not hive_eligible(note, config):
            continue
        key = f"vault/{machine_id}/{note.name}"
        value = json.dumps(
            {
                "id": note.name,
                "type": note.note_type,
                "category": note.category,
                "tier": note.tier,
                "tags": note.tags,
                "aliases": note.aliases,
                "updated": note.updated,
                "content": note.clean_body(strip_links=True, drop_h1=True, drop_related=True),
                "machine_id": machine_id,
            }
        )
        tags = ["vault", machine_id, note.tier]
        if note.note_type:
            tags.append(note.note_type)
        client.memory_set(key, value, tags)
        pushed += 1
    client.close()
    return pushed


def hive_pull(config: dict) -> int:
    hive_cfg = config.get("hive", {})
    if not hive_cfg.get("enabled"):
        sys.exit("ERROR: hive is not enabled in cortex.yaml")
    machine_id = hive_cfg.get("machine_id", "")
    if not machine_id:
        sys.exit("ERROR: hive.machine_id is not set in cortex.yaml")

    client = HubClient(
        hive_cfg["hub_url"],
        token=hive_cfg.get("hub_token", ""),
    )
    client.connect()
    vault_root = Path(config["vault_path"])
    results = client.memory_search("vault/")
    client.close()

    if not results:
        return 0

    pulled = 0
    for entry in results:
        try:
            note_data = (
                json.loads(entry["value"])
                if isinstance(entry.get("value"), str)
                else entry.get("value", {})
            )
        except (json.JSONDecodeError, ValueError):
            continue
        if note_data.get("machine_id") == machine_id:
            continue
        note_id = note_data.get("id", "")
        local_path = None
        for md in vault_root.rglob("*.md"):
            meta, _ = parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
            if meta.get("id") == note_id:
                local_path = md
                break
        if local_path is None:
            note_type = note_data.get("type", "knowledge")
            type_dir = vault_root / (note_type + "s" if not note_type.endswith("s") else note_type)
            type_dir.mkdir(parents=True, exist_ok=True)
            local_path = type_dir / f"{note_id}.md"
        if local_path.exists():
            local_meta, _ = parse_frontmatter(
                local_path.read_text(encoding="utf-8", errors="replace")
            )
            local_updated = local_meta.get("updated", "1970-01-01")
            if local_updated >= note_data.get("updated", "1970-01-01"):
                continue
        parts = ["---"]
        fm: dict[str, object] = {
            "id": note_data.get("id", note_id),
            "type": note_data.get("type", "knowledge"),
            "tier": note_data.get("tier", "core"),
            "aliases": note_data.get("aliases", []),
            "updated": note_data.get("updated", datetime.now().strftime("%Y-%m-%d")),
        }
        if note_data.get("category"):
            fm["category"] = note_data["category"]
        if note_data.get("tags"):
            fm["tags"] = note_data["tags"]
        for key, val in fm.items():
            if isinstance(val, list):
                parts.append(f"{key}: {json.dumps(val)}")
            elif isinstance(val, str) and not val.startswith('"'):
                parts.append(f'{key}: "{val}"')
            else:
                parts.append(f"{key}: {val}")
        parts.append("---")
        parts.append("")
        parts.append(note_data.get("content", ""))
        parts.append("")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text("\n".join(parts), encoding="utf-8")
        pulled += 1
    return pulled


def hive_status(config: dict) -> dict[str, object]:
    hive_cfg = config.get("hive", {})
    hub_url = hive_cfg.get("hub_url", "")
    machine_id = hive_cfg.get("machine_id", "")
    result: dict[str, object] = {
        "connected": False,
        "hub_url": hub_url,
        "machine_id": machine_id,
        "enabled": hive_cfg.get("enabled", False),
        "replicate_tiers": hive_cfg.get("replicate_tiers", []),
        "notes_synced": 0,
    }
    if not hive_cfg.get("enabled"):
        return result
    try:
        client = HubClient(
            hub_url,
            token=hive_cfg.get("hub_token", ""),
            timeout=5,
        )
        client.connect()
        results = client.memory_search(f"vault/{machine_id}/")
        result["connected"] = True
        result["notes_synced"] = len(results) if isinstance(results, list) else 0
        client.close()
    except HubConnectionError:
        pass
    return result


# ---------------------------------------------------------------------------
# Encode runner (shared by CLI command and standalone script)
# ---------------------------------------------------------------------------


def run_encode(
    config_path: Path,
    dry_run: bool = False,
    list_only: bool = False,
    show_config_only: bool = False,
    check_only: bool = False,
    graph_only: bool = False,
    purge_only: bool = False,
    purge_apply: bool = False,
    hive_push_only: bool = False,
    hive_pull_only: bool = False,
    hive_status_only: bool = False,
) -> int:
    """Run the encoding. Returns exit code (0 = ok)."""
    cfg = load_config(config_path)

    # `--show-config` stays reachable: it is how you inspect this mismatch.
    if show_config_only:
        show_config(cfg, config_path)
        return 0

    # The vault comes from `vault_path` inside the config, NOT from where the
    # config file sits. So `cortex encode --config <copy>/_sync/cortex.yaml`
    # encodes whatever that copy's yaml names — normally the original vault.
    # The copy's own notes are ignored and the original is silently rebuilt,
    # which drops any note the rebuild's include_types does not admit.
    # A config living in a `_sync/` dir implies a vault; if that disagrees with
    # what the config declares, refuse rather than guess.
    cfg_file = Path(config_path).expanduser().resolve()
    if cfg_file.parent.name == "_sync":
        implied = cfg_file.parent.parent
        declared = Path(cfg["vault_path"]).expanduser().resolve()
        if implied != declared:
            sys.exit(
                f"ERROR: config location and declared vault disagree.\n"
                f"  config file:   {cfg_file}\n"
                f"  implies vault: {implied}\n"
                f"  vault_path:    {cfg['vault_path']}\n"
                f"\n"
                f"  This would encode {declared}, not the vault containing the\n"
                f"  config. If that is intended, edit vault_path to match, or run\n"
                f"  'cortex encode' without --config."
            )

    if hive_status_only:
        status = hive_status(cfg)
        print(json.dumps(status, indent=2))
        return 0
    if hive_push_only:
        pushed = hive_push(cfg)
        print(f"\nHive push complete: {pushed} notes pushed to hub.")
        return 0
    if hive_pull_only:
        pulled = hive_pull(cfg)
        print(f"\nHive pull complete: {pulled} notes pulled from hub.")
        return 0

    vault = Path(cfg["vault_path"])
    if not vault.exists():
        print(f"ERROR: vault_path does not exist: {vault}", file=sys.stderr)
        return 1

    if check_only:
        run_check(vault)
        return 0

    if purge_only and not purge_apply:
        purge_drained_logs(vault, apply=False)
        return 0
    if purge_apply:
        deleted = purge_drained_logs(vault, apply=True)
        if not deleted:
            return 0
        print("\n==> Rebuilding encoded outputs after purge...")

    check_and_migrate(vault, dry_run)

    skip_dirs: set[str] = set(cfg.get("skip_dirs", ["templates"]))
    notes = scan_vault(vault, skip_dirs)
    print(f"Scanned vault: {len(notes)} notes with metadata")

    expired = [n for n in notes if n.is_expired()]
    if expired:
        print(f"  Skipping {len(expired)} expired note(s): {', '.join(n.name for n in expired)}")
    active_notes = [n for n in notes if not n.is_expired()]

    if list_only:
        for n in sorted(notes, key=lambda x: (x.tier, x.name)):
            print(f"  [{n.tier or 'untagged':20}] {n.name:36} type={n.note_type}")
        return 0

    if graph_only:
        graph = build_wiki_graph(notes)
        graph_out = vault / "_sync" / "encoded" / "graph.json"
        graph_data = {
            "_meta": {
                "generated": datetime.now().isoformat(),
                "source": "Cortex vault wiki-links",
                "cortex_version": cortex_version(),
                "node_count": len(graph["nodes"]),
                "edge_count": len(graph["edges"]),
                "dangling_count": len(graph["dangling"]),
                "isolated_count": len(graph["isolated"]),
                "god_node_count": len(graph["god_nodes"]),
            },
            **graph,
        }
        graph_out.parent.mkdir(parents=True, exist_ok=True)
        graph_out.write_text(
            json.dumps(graph_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Wrote graph: {graph_out}")
        print(f"  Nodes: {len(graph['nodes'])}")
        print(f"  Edges: {len(graph['edges'])}")
        print(f"  Dangling links: {len(graph['dangling'])}")
        print(f"  Isolated nodes: {len(graph['isolated'])}")
        if graph["isolated"]:
            print(f"    {', '.join(graph['isolated'][:10])}")
        print(f"  God nodes (degree > threshold): {len(graph['god_nodes'])}")
        for g in graph["god_nodes"][:5]:
            print(f"    {g['id']}: degree {g['degree']}")
        return 0

    eager_tiers: list[str] = cfg.get("eager_tiers", ["core"])
    exclude_tags: list[str] = cfg.get("exclude_tags", [])
    vault_only_types: list[str] = cfg.get("vault_only_types", [])
    strip_links: bool = cfg.get("strip_wiki_links", True)
    targets: dict = cfg.get("targets", {})

    dry = dry_run

    # Before anything is written: a config copied from another vault carries
    # absolute paths back to that vault, and would silently overwrite it.
    validate_vault_artifact_paths(vault, targets)

    core_out: Path | None = None
    cc = targets.get("core_context", {})
    if cc.get("enabled"):
        core_out = sync_core_context(active_notes, cc, eager_tiers, strip_links, dry)

    sk = targets.get("skills", {})
    if sk.get("enabled"):
        sync_skill_embeds(active_notes, sk, strip_links, dry)

    pr = targets.get("projects", {})
    if pr.get("enabled"):
        sync_projects(active_notes, pr, strip_links, dry)

    pa = targets.get("python-agents", {})
    if pa.get("enabled"):
        sync_python_agents(active_notes, pa, exclude_tags, vault_only_types, dry)

    if core_out:
        if cc.get("prune_legacy", False):
            prune_encoded_opencode(core_out, dry)
        if cc.get("opencode_config"):
            update_opencode_instructions(Path(cc["opencode_config"]), core_out, dry)

    if not dry:
        save_sync_state(vault, notes)
        print(f"\nSync complete. {len(notes)} notes processed.")
    else:
        print("\n[DRY RUN] No files were modified.")

    return 0


# ---------------------------------------------------------------------------
# CLI (standalone script compat)
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Encode Obsidian vault (tiered)")
    ap.add_argument(
        "--version",
        action="version",
        version=f"Cortex {cortex_version()}",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--list",
        action="store_true",
        help="List all vault notes with tier/type and exit",
    )
    ap.add_argument(
        "--show-config",
        action="store_true",
        help="Print resolved paths as JSON and exit",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="Report version/schema health and exit",
    )
    ap.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent.parent.parent / "cortex.yaml"),
        help=("Path to config file (default: cortex.yaml next to this script)"),
    )
    ap.add_argument(
        "--hive-push",
        action="store_true",
        help="Push vault notes to hub",
    )
    ap.add_argument(
        "--hive-pull",
        action="store_true",
        help="Pull vault notes from hub",
    )
    ap.add_argument(
        "--hive-status",
        action="store_true",
        help="Show hive connection status",
    )
    ap.add_argument(
        "--purge",
        action="store_true",
        help="Preview deletion of drained log/session notes, then exit",
    )
    ap.add_argument(
        "--purge-apply",
        action="store_true",
        help="Delete drained log/session notes and rebuild",
    )
    ap.add_argument(
        "--graph",
        action="store_true",
        help="Output wiki-link graph to encoded/graph.json and exit",
    )
    args = ap.parse_args()

    cfg_path = Path(args.config)
    rc = run_encode(
        config_path=cfg_path,
        dry_run=args.dry_run,
        list_only=args.list,
        show_config_only=args.show_config,
        check_only=args.check,
        graph_only=args.graph,
        purge_only=args.purge,
        purge_apply=args.purge_apply,
        hive_push_only=args.hive_push,
        hive_pull_only=args.hive_pull,
        hive_status_only=args.hive_status,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
