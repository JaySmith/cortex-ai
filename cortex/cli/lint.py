#! /usr/bin/env python3
"""
cortex.cli.lint — Lint a Cortex vault for common issues.

Usage:
    cortex lint                     # scan auto-detected vault
    cortex lint --vault ~/my-vault  # explicit vault path
    cortex lint --strict            # error on warnings too
    cortex lint --fix               # auto-fix what we can
    cortex lint --note <id>         # lint a single note
    cortex lint --json              # machine-readable output
    cortex lint --rules             # list available rules

Exit codes:
    0 = clean
    1 = errors found
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from collections.abc import Callable
from typing import Any

import typer

try:
    from cortex.encoder.core import VaultNote, scan_vault
    from cortex.vault.links import resolve_wiki_links
except ImportError:
    # Allow importing lint.py as a standalone module during dev
    VaultNote = None  # type: ignore[assignment]
    scan_vault = None  # type: ignore[assignment]
    resolve_wiki_links = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Rule result type
# ---------------------------------------------------------------------------

class LintResult:
    """A single lint finding."""

    __slots__ = ("severity", "rule", "note_id", "message", "fix")

    def __init__(
        self,
        severity: str,  # "error" | "warning" | "info"
        rule: str,
        note_id: str,
        message: str,
        fix: str | None = None,
    ) -> None:
        self.severity = severity
        self.rule = rule
        self.note_id = note_id
        self.message = message
        self.fix = fix

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "severity": self.severity,
            "rule": self.rule,
            "note": self.note_id,
            "message": self.message,
        }
        if self.fix:
            d["fix"] = self.fix
        return d

    def __repr__(self) -> str:
        tag = {"error": "E", "warning": "W", "info": "I"}.get(self.severity, "?")
        fix = " [fixable]" if self.fix else ""
        return f"  {tag}  {self.rule:35s} {self.note_id:30s} {self.message}{fix}"


# ---------------------------------------------------------------------------
# Rules registry
# ---------------------------------------------------------------------------

_rules: list[dict[str, Any]] = []


def register_rule(
    name: str,
    description: str,
    severity: str = "error",
    auto_fix: bool = False,
) -> Callable:  # type: ignore[type-arg]
    """Decorator to register a lint rule function."""

    def decorator(fn: Callable) -> Callable:  # type: ignore[type-arg]
        _rules.append(
            {
                "name": name,
                "description": description,
                "severity": severity,
                "auto_fix": auto_fix,
                "fn": fn,
            }
        )
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@register_rule(
    "missing-id",
    "Note is missing an `id` field in frontmatter",
    severity="error",
)
def _check_missing_id(notes: list[VaultNote], note_filter: str | None) -> list[LintResult]:
    results: list[LintResult] = []
    for n in notes:
        if note_filter and n.name != note_filter:
            continue
        if not n.name or n.name == n.path.stem:
            # name defaults to path.stem, which is okay if id is absent
            # We need to check raw frontmatter. Use meta dict.
            meta = n.meta if hasattr(n, "meta") else {}
            if "id" not in meta or not meta["id"]:
                results.append(
                    LintResult("error", "missing-id", n.path.stem, "No `id` field in frontmatter")
                )
    return results


@register_rule(
    "missing-type",
    "Note is missing a `type` field in frontmatter",
    severity="error",
)
def _check_missing_type(notes: list[VaultNote], note_filter: str | None) -> list[LintResult]:
    results: list[LintResult] = []
    for n in notes:
        if note_filter and n.name != note_filter:
            continue
        if not n.note_type or n.note_type == "unknown":
            results.append(
                LintResult(
                    "error", "missing-type", n.name, "No `type` field in frontmatter",
                    fix="Add `type: knowledge` (or entity/feedback/session/log)"
                )
            )
    return results


@register_rule(
    "missing-tier",
    "Note is missing a `tier` field in frontmatter",
    severity="error",
)
def _check_missing_tier(notes: list[VaultNote], note_filter: str | None) -> list[LintResult]:
    results: list[LintResult] = []
    for n in notes:
        if note_filter and n.name != note_filter:
            continue
        if not n.tier:
            results.append(
                LintResult(
                    "error", "missing-tier", n.name, "No `tier` field in frontmatter",
                    fix="Add `tier: core` (or skill:<name>/project/vault-only)"
                )
            )
    return results


@register_rule(
    "invalid-tier",
    "Note has an unrecognized tier value",
    severity="error",
)
def _check_invalid_tier(notes: list[VaultNote], note_filter: str | None) -> list[LintResult]:
    valid_tiers = {"core", "vault-only"}
    results: list[LintResult] = []
    for n in notes:
        if note_filter and n.name != note_filter:
            continue
        tier = n.tier or ""
        if tier and tier not in valid_tiers and not tier.startswith("skill:") and tier != "project":
            results.append(
                LintResult(
                    "error", "invalid-tier", n.name,
                    f"Unrecognized tier: '{tier}'. Expected core/skill:<name>/project/vault-only",
                )
            )
    return results


@register_rule(
    "missing-aliases",
    "Note is missing an `aliases` field — display title defaults to slug",
    severity="warning",
)
def _check_missing_aliases(notes: list[VaultNote], note_filter: str | None) -> list[LintResult]:
    results: list[LintResult] = []
    for n in notes:
        if note_filter and n.name != note_filter:
            continue
        if not n.aliases:
            results.append(
                LintResult(
                    "warning", "missing-aliases", n.name,
                    "No `aliases` field — display title will be derived from id slug",
                    fix="Add `aliases: [\"Human Readable Title\"]` to frontmatter",
                )
            )
    return results


@register_rule(
    "slug-mismatch",
    "Filename stem does not match the `id` field",
    severity="warning",
)
def _check_slug_mismatch(notes: list[VaultNote], note_filter: str | None) -> list[LintResult]:
    results: list[LintResult] = []
    for n in notes:
        if note_filter and n.name != note_filter:
            continue
        file_stem = n.path.stem
        if n.name and file_stem != n.name:
            results.append(
                LintResult(
                    "warning", "slug-mismatch", n.name,
                    f"File stem '{file_stem}' != id '{n.name}'",
                    fix=f"Rename file to {n.name}.md or change id to {file_stem}",
                )
            )
    return results


@register_rule(
    "duplicate-id",
    "Multiple files share the same `id`",
    severity="error",
)
def _check_duplicate_id(notes: list[VaultNote], note_filter: str | None) -> list[LintResult]:
    seen: dict[str, list[str]] = {}
    for n in notes:
        if note_filter and n.name != note_filter:
            continue
        seen.setdefault(n.name, []).append(str(n.path))
    results: list[LintResult] = []
    for nid, paths in seen.items():
        if len(paths) > 1:
            results.append(
                LintResult(
                    "error", "duplicate-id", nid,
                    f"ID '{nid}' used in {len(paths)} files: {', '.join(paths)}",
                    fix="Remove or rename duplicate notes so each id is unique",
                )
            )
    return results


@register_rule(
    "missing-updated",
    "Note is missing an `updated` date",
    severity="info",
)
def _check_missing_updated(notes: list[VaultNote], note_filter: str | None) -> list[LintResult]:
    results: list[LintResult] = []
    for n in notes:
        if note_filter and n.name != note_filter:
            continue
        if not n.updated:
            results.append(
                LintResult(
                    "info", "missing-updated", n.name,
                    "No `updated` date — freshness cannot be tracked",
                    fix="Add `updated: \"2026-01-01\"` (use current date)",
                )
            )
    return results


@register_rule(
    "dangling-wiki-link",
    "Wiki-link targets a note id that does not exist in the vault",
    severity="warning",
)
def _check_dangling_wiki_link(notes: list[VaultNote], note_filter: str | None) -> list[LintResult]:
    if resolve_wiki_links is None:
        return []
    # Filter notes if note_filter is set
    filtered = [n for n in notes if not note_filter or n.name == note_filter]
    if not filtered:
        return []
    _edges, dangling = resolve_wiki_links(filtered)
    results: list[LintResult] = []
    for d in dangling:
        results.append(
            LintResult(
                "warning", "dangling-wiki-link", d["note"],
                f"Wiki-link [[{d['target']}]] has no matching note in the vault",
                fix=f"Create a note with id '{d['target']}' or remove the link",
            )
        )
    return results


@register_rule(
    "non-slug-id",
    "`id` contains characters other than lowercase letters, digits, and hyphens",
    severity="warning",
)
def _check_non_slug_id(notes: list[VaultNote], note_filter: str | None) -> list[LintResult]:
    import re

    results: list[LintResult] = []
    for n in notes:
        if note_filter and n.name != note_filter:
            continue
        if n.name and not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", n.name):
            results.append(
                LintResult(
                    "warning", "non-slug-id", n.name,
                    f"ID '{n.name}' is not a valid slug (lowercase, hyphens, alphanumeric only)",
                    fix="Use slug format: e.g., 'my-note-1' instead of 'My Note 1'",
                )
            )
    return results


@register_rule(
    "empty-body",
    "Note has no body content (frontmatter only)",
    severity="info",
)
def _check_empty_body(notes: list[VaultNote], note_filter: str | None) -> list[LintResult]:
    results: list[LintResult] = []
    for n in notes:
        if note_filter and n.name != note_filter:
            continue
        if not n.body or not n.body.strip():
            results.append(
                LintResult(
                    "warning", "empty-body", n.name,
                    "Note body is empty — frontmatter only. Use `cortex memory write --update --title X --body ''` to set deliberately, or add content.",
                )
            )
    return results


# ---------------------------------------------------------------------------
# Registry access
# ---------------------------------------------------------------------------


def list_rules() -> list[dict[str, Any]]:
    """Return registered rule metadata."""
    return [
        {
            "name": r["name"],
            "description": r["description"],
            "severity": r["severity"],
            "auto_fix": r["auto_fix"],
        }
        for r in _rules
    ]


# ---------------------------------------------------------------------------
# Lint runner
# ---------------------------------------------------------------------------


def run_lint(
    vault_path: Path,
    *,
    note_filter: str | None = None,
    strict: bool = False,
    fix: bool = False,
) -> dict[str, Any]:
    """Run all lint rules on a vault.

    Returns a dict with keys: errors, warnings, infos, fixable, results.
    """
    if scan_vault is None or VaultNote is None:
        return {"errors": 0, "warnings": 0, "infos": 0, "fixable": 0, "results": [], "error": "cortex modules not available"}

    notes = scan_vault(vault_path, require_type=False)

    all_results: list[LintResult] = []
    for rule in _rules:
        fn = rule["fn"]
        try:
            results = fn(notes, note_filter)
            all_results.extend(results)
        except Exception as exc:
            all_results.append(
                LintResult(
                    "error", rule["name"], "(internal)",
                    f"Rule raised exception: {exc}",
                )
            )

    # Apply fixes if requested
    if fix:
        _apply_fixes(all_results, vault_path, notes)

    errors = [r for r in all_results if r.severity == "error"]
    warnings = [r for r in all_results if r.severity == "warning"]
    infos = [r for r in all_results if r.severity == "info"]
    fixable = [r for r in all_results if r.fix]

    if strict:
        # Promote warnings to errors
        errors = errors + warnings
        warnings = []

    return {
        "errors": errors,
        "warnings": warnings,
        "infos": infos,
        "fixable": fixable,
        "results": all_results,
        "error": None,
    }


def _apply_fixes(results: list[LintResult], vault_path: Path, notes: list[VaultNote]) -> None:
    """Apply auto-fixes for fixable issues."""
    # Build lookup
    note_map: dict[str, VaultNote] = {n.name: n for n in notes}

    for result in results:
        if not result.fix:
            continue
        note = note_map.get(result.note_id)
        if not note:
            continue

        if result.rule == "missing-type":
            _fix_missing_field(note.path, "type", "knowledge")
        elif result.rule == "missing-tier":
            _fix_missing_field(note.path, "tier", "core")
        elif result.rule == "missing-aliases":
            _fix_missing_aliases(note)
        elif result.rule == "non-slug-id":
            _fix_non_slug_id(note)


def _fix_missing_field(path: Path, field: str, value: str) -> None:
    """Add a missing frontmatter field with a default value."""
    content = path.read_text(encoding="utf-8")
    lines = content.split("\n")
    # Find end of frontmatter (second ---)
    fm_end = -1
    dash_count = 0
    for i, line in enumerate(lines):
        if line.strip() == "---":
            dash_count += 1
            if dash_count == 2:
                fm_end = i
                break
    if fm_end < 0:
        return

    # Check if field already exists (shouldn't, but be safe)
    for line in lines[1:fm_end]:
        if line.strip().startswith(f"{field}:"):
            return

    # Insert before closing ---
    lines.insert(fm_end, f'{field}: "{value}"')
    path.write_text("\n".join(lines), encoding="utf-8")


def _fix_missing_aliases(note: VaultNote) -> None:
    """Derive an aliases field from the filename."""
    title = note.path.stem.replace("-", " ").title()
    content = note.path.read_text(encoding="utf-8")
    lines = content.split("\n")
    fm_end = -1
    dash_count = 0
    for i, line in enumerate(lines):
        if line.strip() == "---":
            dash_count += 1
            if dash_count == 2:
                fm_end = i
                break
    if fm_end < 0:
        return

    for line in lines[1:fm_end]:
        if line.strip().startswith("aliases:"):
            return

    lines.insert(fm_end, f'aliases: ["{title}"]')
    note.path.write_text("\n".join(lines), encoding="utf-8")


def _fix_non_slug_id(note: VaultNote) -> None:
    """Rename file to kebab-case slug and update frontmatter id."""
    old_stem = note.path.stem
    slug = re.sub(r"[^a-z0-9]+", "-", old_stem.lower()).strip("-")
    if not slug or slug == old_stem:
        return

    new_path = note.path.with_stem(slug)
    if new_path.exists():
        return

    content = note.path.read_text(encoding="utf-8")
    lines = content.split("\n")
    fm_end = -1
    dash_count = 0
    for i, line in enumerate(lines):
        if line.strip() == "---":
            dash_count += 1
            if dash_count == 2:
                fm_end = i
                break
    if fm_end < 0:
        return

    updated = False
    for i in range(1, fm_end):
        if lines[i].strip().startswith("id:"):
            lines[i] = f'id: {slug}'
            updated = True
            break
    if not updated:
        lines.insert(fm_end, f"id: {slug}")

    has_aliases = any(
        lines[i].strip().startswith("aliases:") for i in range(1, fm_end)
    )
    if not has_aliases:
        title = old_stem.replace("-", " ").title()
        lines.insert(fm_end, f'aliases: ["{title}"]')

    note.path.write_text("\n".join(lines), encoding="utf-8")
    note.path.rename(new_path)
    note.name = slug
    print(f"  renamed: {old_stem} -> {slug}")


# ---------------------------------------------------------------------------
# Typer command
# ---------------------------------------------------------------------------


def _find_vault_path(vault_arg: str | None) -> Path | None:
    """Shared vault path resolution (duplicated from main.py to keep lint standalone)."""
    if vault_arg:
        # An explicit --vault is trusted: lint operates on the vault's notes and
        # does not need cortex.yaml (which is an install artifact, absent from a
        # fresh or example vault). Only require that the path is a real directory.
        p = Path(vault_arg).expanduser().resolve()
        if p.is_dir():
            return p
        return None
    cwd = Path.cwd()
    if (cwd / "_sync" / "cortex.yaml").exists():
        return cwd
    home = Path.home()
    for d in home.iterdir():
        if d.is_dir() and (d / "_sync" / "cortex.yaml").exists():
            return d
    return None


lint_app = typer.Typer(
    name="lint",
    help="Lint a Cortex vault for common issues",
    no_args_is_help=True,
)


@lint_app.callback(invoke_without_command=True)
def lint_cmd(
    ctx: typer.Context,
    vault: str | None = typer.Option(None, "--vault", help="Vault path (auto-detect by default)"),
    strict: bool = typer.Option(False, "--strict", help="Treat warnings as errors"),
    fix: bool = typer.Option(False, "--fix", help="Auto-fix fixable issues"),
    note: str | None = typer.Option(None, "--note", help="Lint a single note by id"),
    json_output: bool = typer.Option(False, "--json", help="Output results as JSON"),
    rules: bool = typer.Option(False, "--rules", help="List available rules and exit"),
) -> None:
    """Lint a Cortex vault for common issues."""
    if rules:
        _list_rules_cmd()
        return

    vault_path = _find_vault_path(vault)
    if not vault_path:
        typer.echo("Error: No vault found. Run 'cortex install' first, or pass --vault.", err=True)
        raise typer.Exit(code=1)

    result = run_lint(vault_path, note_filter=note, strict=strict, fix=fix)

    if json_output:
        _emit_json(result)
    else:
        _emit_human(result, vault_path)

    if result.get("error"):
        raise typer.Exit(code=1)
    if result["errors"]:
        raise typer.Exit(code=1)


def _list_rules_cmd() -> None:
    """Print available rules and exit."""
    typer.echo("Available lint rules:")
    typer.echo("")
    for r in list_rules():
        tag = {"error": "E", "warning": "W", "info": "I"}.get(r["severity"], "?")
        fix_mark = " [fixable]" if r["auto_fix"] else ""
        typer.echo(f"  {tag}  {r['name']:35s} {r['description']}{fix_mark}")


def _emit_json(result: dict[str, Any]) -> None:
    """Emit results as JSON."""
    output = {
        "summary": {
            "errors": len(result["errors"]),
            "warnings": len(result["warnings"]),
            "infos": len(result["infos"]),
            "fixable": len(result["fixable"]),
        },
        "results": [r.to_dict() for r in result["results"]],
    }
    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _emit_human(result: dict[str, Any], vault_path: Path) -> None:
    """Emit results as human-readable text."""
    errors = result["errors"]
    warnings = result["warnings"]
    infos = result["infos"]
    fixable = result["fixable"]

    typer.echo(f"Linting vault: {vault_path}")
    typer.echo(f"  Notes scanned: {len(result.get('results', []))} total checks (not per-note count, see below)")
    typer.echo("")

    if not errors and not warnings and not infos:
        typer.echo("✓ No issues found.")
        return

    for category, items in [("Errors", errors), ("Warnings", warnings), ("Info", infos)]:
        if items:
            typer.echo(f"--- {category} ---")
            for item in items:
                typer.echo(str(item))
            typer.echo("")

    typer.echo(f"Summary: {len(errors)} errors, {len(warnings)} warnings, {len(infos)} infos")
    if fixable:
        typer.echo(f"  {len(fixable)} issue(s) are auto-fixable with --fix")


def main() -> None:
    lint_app()


if __name__ == "__main__":
    main()
