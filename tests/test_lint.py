"""Tests for cortex lint — all rules and CLI integration."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cortex.cli.lint import lint_app, run_lint, list_rules

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_note(vault: Path, note_id: str, *, body: str = "", filename_suffix: str = "", **frontmatter: str | list[str]) -> Path:
    """Create a vault note with given frontmatter fields.

    Use ``filename_suffix`` when two notes share the same id (to test duplicate-id detection).
    """
    note_type: str = frontmatter.get("type", "knowledge")  # type: ignore[assignment]
    type_dir_name = note_type + "s" if not note_type.endswith("s") else note_type
    target_dir = vault / type_dir_name
    target_dir.mkdir(parents=True, exist_ok=True)

    # Build frontmatter YAML lines
    lines = ["---"]
    lines.append(f"id: {note_id}")
    for key, val in frontmatter.items():
        if isinstance(val, list):
            items = ", ".join(f'"{v}"' for v in val)
            lines.append(f"{key}: [{items}]")
        else:
            lines.append(f'{key}: "{val}"')
    lines.append("---")
    if body:
        lines.append("")
        lines.append(body)

    stem = f"{note_id}{filename_suffix}"
    path = target_dir / f"{stem}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _make_bare_file(vault: Path, name: str) -> Path:
    """Create a .md file with no frontmatter."""
    path = vault / name
    path.write_text("# Just a markdown file\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lint_vault(tmp_path: Path) -> Path:
    """Create a test vault with a mix of well-formed and broken notes."""
    vault = tmp_path / "lint-vault"
    vault.mkdir()
    (vault / "_sync").mkdir()
    (vault / "_sync" / "cortex.yaml").write_text("", encoding="utf-8")

    # Well-formed notes
    _make_note(vault, "good-note", type="knowledge", tier="core", aliases=["Good Note"], updated="2026-01-01")
    _make_note(vault, "good-skill", type="knowledge", tier="skill:jira", aliases=["Good Skill"], updated="2026-01-01")
    _make_note(vault, "good-project", type="entity", tier="project", aliases=["Good Project"], updated="2026-01-01")

    # Broken notes
    _make_note(vault, "no-type-note", tier="core", aliases=["No Type"], updated="2026-01-01")  # missing type
    _make_note(vault, "no-tier-note", type="knowledge", aliases=["No Tier"], updated="2026-01-01")  # missing tier
    _make_note(vault, "bad-tier-note", type="knowledge", tier="invalid", aliases=["Bad Tier"], updated="2026-01-01")  # invalid tier
    _make_note(vault, "no-alias-note", type="knowledge", tier="core", updated="2026-01-01")  # missing aliases
    _make_note(vault, "no-updated-note", type="knowledge", tier="core", aliases=["No Updated"])  # missing updated
    _make_note(vault, "empty-body-note", type="knowledge", tier="core", aliases=["Empty Body"], updated="2026-01-01")  # empty body
    _make_note(vault, "CamelCase-ID", type="knowledge", tier="core", aliases=["Bad Slug"], updated="2026-01-01")  # non-slug id
    _make_note(vault, "dup-id-note", type="feedback", tier="core", aliases=["Dup 1"], updated="2026-01-01")
    _make_note(vault, "dup-id-note", type="feedback", tier="core", aliases=["Dup 2"], updated="2026-01-01",
               filename_suffix="-2")  # duplicate id, distinct file

    # Bare file (no frontmatter at all)
    _make_bare_file(vault, "README.md")

    # Note with a dangling wiki link
    _make_note(vault, "has-dangling", type="knowledge", tier="core", aliases=["Has Dangling"], updated="2026-01-01",
               body="This references [[non-existent-note]] and [[good-note]]")

    # Slug mismatch: file is slug-mismatch.md but id is something-else
    _make_note_in_path(vault, "slug-mismatch", "something-else", type="knowledge", tier="core", aliases=["Slug Mismatch"], updated="2026-01-01")

    return vault


def _make_note_in_path(vault: Path, filename_stem: str, note_id: str, *, body: str = "", **frontmatter) -> Path:
    """Create a note where the file stem differs from the id."""
    note_type: str = frontmatter.get("type", "knowledge")  # type: ignore[assignment]
    type_dir_name = note_type + "s" if not note_type.endswith("s") else note_type
    target_dir = vault / type_dir_name
    target_dir.mkdir(parents=True, exist_ok=True)

    lines = ["---"]
    lines.append(f"id: {note_id}")
    for key, val in frontmatter.items():
        if isinstance(val, list):
            items = ", ".join(f'"{v}"' for v in val)
            lines.append(f"{key}: [{items}]")
        else:
            lines.append(f'{key}: "{val}"')
    lines.append("---")
    if body:
        lines.append("")
        lines.append(body)

    path = target_dir / f"{filename_stem}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# run_lint unit tests
# ---------------------------------------------------------------------------


class TestRunLint:
    """Test the run_lint function directly."""

    def test_clean_vault(self, vault):
        """Example vault only flags README.md (not a real note, missing frontmatter)."""
        result = run_lint(vault)
        assert result["error"] is None
        # README.md has no frontmatter — flagged for missing-id, missing-type, missing-tier
        readme_errors = [r for r in result["errors"] if r.note_id in ("README", "readme")]
        assert len(readme_errors) == 3
        # But no real notes should have errors
        real_note_errors = [r for r in result["errors"] if r.note_id not in ("README", "readme")]
        assert len(real_note_errors) == 0

    def test_missing_type(self, lint_vault):
        """Detects notes without a type field."""
        result = run_lint(lint_vault)
        errors = result["errors"]
        matching = [r for r in errors if r.rule == "missing-type" and r.note_id == "no-type-note"]
        assert len(matching) == 1

    def test_missing_tier(self, lint_vault):
        """Detects notes without a tier field."""
        result = run_lint(lint_vault)
        errors = result["errors"]
        matching = [r for r in errors if r.rule == "missing-tier" and r.note_id == "no-tier-note"]
        assert len(matching) == 1

    def test_invalid_tier(self, lint_vault):
        """Detects notes with unrecognized tier values."""
        result = run_lint(lint_vault)
        errors = result["errors"]
        matching = [r for r in errors if r.rule == "invalid-tier" and r.note_id == "bad-tier-note"]
        assert len(matching) == 1

    def test_missing_aliases(self, lint_vault):
        """Detects notes without aliases."""
        result = run_lint(lint_vault)
        warnings = result["warnings"]
        matching = [r for r in warnings if r.rule == "missing-aliases" and r.note_id == "no-alias-note"]
        assert len(matching) == 1

    def test_slug_mismatch(self, lint_vault):
        """Detects when file stem != id."""
        result = run_lint(lint_vault)
        warnings = result["warnings"]
        # The note has id "something-else" in file "slug-mismatch.md"
        matching = [r for r in warnings if r.rule == "slug-mismatch"]
        assert len(matching) >= 1
        assert any("slug-mismatch" in r.message for r in matching)

    def test_duplicate_id(self, lint_vault):
        """Detects duplicate ids across files."""
        result = run_lint(lint_vault)
        errors = result["errors"]
        matching = [r for r in errors if r.rule == "duplicate-id" and r.note_id == "dup-id-note"]
        assert len(matching) == 1

    def test_missing_updated(self, lint_vault):
        """Detects notes without updated dates."""
        result = run_lint(lint_vault)
        infos = result["infos"]
        matching = [r for r in infos if r.rule == "missing-updated" and r.note_id == "no-updated-note"]
        assert len(matching) == 1

    def test_dangling_wiki_link(self, lint_vault):
        """Detects wiki-links to non-existent notes."""
        result = run_lint(lint_vault)
        warnings = result["warnings"]
        matching = [r for r in warnings if r.rule == "dangling-wiki-link" and r.note_id == "has-dangling"]
        assert len(matching) >= 1
        assert any("non-existent-note" in r.message for r in matching)

        # Should NOT flag [[good-note]] (exists)
        flagged_for_good = [r for r in matching if "good-note" in r.message]
        assert len(flagged_for_good) == 0

    def test_non_slug_id(self, lint_vault):
        """Detects ids with non-slug characters."""
        result = run_lint(lint_vault)
        warnings = result["warnings"]
        matching = [r for r in warnings if r.rule == "non-slug-id" and r.note_id == "CamelCase-ID"]
        assert len(matching) == 1

    def test_empty_body(self, lint_vault):
        """Detects notes with no body content."""
        result = run_lint(lint_vault)
        warnings = result["warnings"]
        matching = [r for r in warnings if r.rule == "empty-body" and r.note_id == "empty-body-note"]
        assert len(matching) == 1

    def test_note_filter(self, lint_vault):
        """--note flag filters to a single note."""
        result = run_lint(lint_vault, note_filter="good-note")
        # Should only check good-note
        note_ids = {r.note_id for r in result["results"]}
        assert note_ids == {"good-note"}

    def test_strict_promotes_warnings(self, lint_vault):
        """--strict promotes warnings to errors."""
        result = run_lint(lint_vault, strict=True)
        # no-type-note is already an error; with strict, warnings become errors too
        # So the total errors should increase
        assert len(result["errors"]) > 0
        # Missing-aliases is a warning; should now be in errors
        alias_errors = [r for r in result["errors"] if r.rule == "missing-aliases"]
        assert len(alias_errors) >= 1

    def test_rules_list(self):
        """list_rules returns all registered rules."""
        rules = list_rules()
        rule_names = {r["name"] for r in rules}
        expected = {
            "missing-id", "missing-type", "missing-tier", "invalid-tier",
            "missing-aliases", "slug-mismatch", "duplicate-id", "missing-updated",
            "dangling-wiki-link", "non-slug-id", "empty-body",
        }
        assert rule_names == expected


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestLintCLI:
    """Test the cortex lint command via Typer CLI runner."""

    def test_rules_flag(self):
        """--rules lists available rules."""
        result = runner.invoke(lint_app, ["--rules"])
        assert result.exit_code == 0
        assert "missing-id" in result.stdout
        assert "missing-type" in result.stdout
        assert "missing-tier" in result.stdout
        assert "missing-aliases" in result.stdout
        assert "dangling-wiki-link" in result.stdout

    def test_lint_clean_vault(self, vault):
        """Clean vault exits 1 (README.md flagged) but real notes are clean."""
        result = runner.invoke(lint_app, ["--vault", str(vault)])
        assert result.exit_code == 1
        assert "README" in result.stdout
        # But no errors from actual notes
        assert "good-note" not in result.stdout

    def test_lint_broken_vault(self, lint_vault):
        """Broken vault exits 1 with issues reported."""
        result = runner.invoke(lint_app, ["--vault", str(lint_vault)])
        assert result.exit_code == 1
        assert "missing-type" in result.stdout
        assert "missing-tier" in result.stdout
        assert "invalid-tier" in result.stdout
        assert "missing-aliases" in result.stdout
        assert "dangling-wiki-link" in result.stdout

    def test_lint_note_filter(self, lint_vault):
        """--note filters to a single note."""
        result = runner.invoke(lint_app, ["--vault", str(lint_vault), "--note", "good-note"])
        # good-note has no body, so it gets an empty-body warning item
        assert "empty-body" in result.stdout
        # But it should NOT flag other notes like no-type-note
        assert "no-type-note" not in result.stdout

    def test_lint_json_output(self, lint_vault):
        """--json outputs machine-readable results."""
        result = runner.invoke(lint_app, ["--vault", str(lint_vault), "--json"])
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert "summary" in data
        assert "results" in data
        assert data["summary"]["errors"] > 0
        assert data["summary"]["warnings"] > 0

    def test_lint_no_vault(self, tmp_path):
        """Running lint without a vault directory exits with error."""
        result = runner.invoke(lint_app, ["--vault", str(tmp_path / "nonexistent")])
        assert result.exit_code == 1
        assert "No vault found" in result.stderr

    def test_lint_fix(self, lint_vault):
        """--fix auto-fixes fixable issues (missing-type, missing-tier, missing-aliases)."""
        result = runner.invoke(lint_app, ["--vault", str(lint_vault), "--fix"])
        # Should still exit 1 because some issues aren't fixable (dangling links, etc.)
        # But the fixable ones should be applied

        # Re-run without fix to see if the fixable issues are gone
        result2 = runner.invoke(lint_app, ["--vault", str(lint_vault), "--json"])
        assert result2.exit_code == 1
        data = json.loads(result2.stdout)

        # missing-type for no-type-note should still exist (it wasn't fixable via --fix
        # since --fix only handles missing-type, missing-tier, missing-aliases)
        # Actually let's verify the fix was applied to the files
        no_type_path = lint_vault / "knowledges" / "no-type-note.md"
        assert no_type_path.exists()
        content = no_type_path.read_text(encoding="utf-8")
        assert 'type: "knowledge"' in content

        no_tier_path = lint_vault / "knowledges" / "no-tier-note.md"
        assert no_tier_path.exists()
        content2 = no_tier_path.read_text(encoding="utf-8")
        assert 'tier: "core"' in content2

        no_alias_path = lint_vault / "knowledges" / "no-alias-note.md"
        content3 = no_alias_path.read_text(encoding="utf-8")
        assert "aliases:" in content3
