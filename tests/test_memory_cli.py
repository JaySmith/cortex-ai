"""Tests for CLI memory commands — get, write, search."""

import json
from pathlib import Path

from typer.testing import CliRunner

from cortex.cli.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_memory_json(vault_root: Path, notes: dict) -> Path:
    """Write a memory.json into the vault's encoded directory."""
    encoded_dir = vault_root / "_sync" / "encoded"
    encoded_dir.mkdir(parents=True, exist_ok=True)
    path = encoded_dir / "memory.json"
    data = {
        "_meta": {"generated": "2026-01-01T00:00:00", "count": len(notes)},
        "notes": notes,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


# Canonical top-level dir per note type (mirrors cortex.cli.main._TYPE_DIR_MAP).
_TYPE_DIR = {
    "knowledge": "knowledge",
    "entity": "entities",
    "feedback": "feedback",
    "decision": "decisions",
    "log": "logs",
    "session": "logs",
}


def _create_note_file(vault_root: Path, note_id: str, note_type: str, body: str = "") -> Path:
    """Create a raw vault note .md file in the canonical type directory."""
    type_dir = vault_root / _TYPE_DIR.get(note_type, f"{note_type}s")
    type_dir.mkdir(parents=True, exist_ok=True)
    path = type_dir / f"{note_id}.md"
    content = (
        f"---\nid: {note_id}\ntype: {note_type}\n---\n\n{body}"
        if body
        else (f"---\nid: {note_id}\ntype: {note_type}\n---\n\n")
    )
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# cortex memory get
# ---------------------------------------------------------------------------


class TestMemoryGet:
    def test_get_existing_note(self, vault):
        """Returns full note content + metadata from memory.json."""
        _write_memory_json(
            vault,
            {
                "test-note": {
                    "id": "test-note",
                    "type": "knowledge",
                    "category": "patterns",
                    "tier": "core",
                    "tags": ["test"],
                    "aliases": ["Test Note"],
                    "updated": "2026-01-01",
                    "content": "This is a test note body.",
                }
            },
        )
        result = runner.invoke(app, ["memory", "get", "test-note", "--vault", str(vault)])
        assert result.exit_code == 0
        assert "# test-note" in result.stdout
        assert "knowledge" in result.stdout
        assert "core" in result.stdout
        assert "This is a test note body" in result.stdout

    def test_get_nonexistent_note(self, vault):
        """Returns error for missing note."""
        _write_memory_json(vault, {})
        result = runner.invoke(app, ["memory", "get", "no-such-note"])
        assert result.exit_code == 1
        assert "not found" in result.stdout.lower() or "not found" in result.stderr.lower()

    def test_get_note_from_vault_fallback(self, vault):
        """Falls back to vault file scan when not in memory.json."""
        _write_memory_json(vault, {})
        _create_note_file(vault, "vault-only-note", "knowledge", body="Found via file scan.")
        result = runner.invoke(app, ["memory", "get", "vault-only-note", "--vault", str(vault)])
        assert result.exit_code == 0
        assert "vault-only-note" in result.stdout
        assert "Found via file scan" in result.stdout

    def test_get_with_custom_vault_path(self, vault):
        """Respects --vault flag."""
        _write_memory_json(
            vault,
            {
                "custom-note": {
                    "id": "custom-note",
                    "type": "entity",
                    "tier": "project",
                    "aliases": ["Custom Note"],
                    "content": "Custom vault note.",
                }
            },
        )
        result = runner.invoke(app, ["memory", "get", "custom-note", "--vault", str(vault)])
        assert result.exit_code == 0
        assert "custom-note" in result.stdout

    def test_get_no_memory_json_no_vault(self, tmp_path):
        """Fails cleanly when no vault or memory.json is available."""
        result = runner.invoke(app, ["memory", "get", "anything"], catch_exceptions=False)
        # In a tmp_path with no vault, should error
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# cortex memory write
# ---------------------------------------------------------------------------


class TestMemoryWrite:
    def test_write_frontmatter_only(self, vault):
        """Creates a note file with frontmatter when no --body."""
        result = runner.invoke(
            app,
            [
                "memory",
                "write",
                "--title",
                "Test Note",
                "--type",
                "knowledge",
                "--tier",
                "core",
                "--vault",
                str(vault),
            ],
        )
        assert result.exit_code == 0
        assert "Created note" in result.stdout

        note_path = vault / "knowledge" / "test-note.md"
        assert note_path.exists()
        content = note_path.read_text(encoding="utf-8")
        assert "id: test-note" in content
        assert "type: knowledge" in content
        assert 'tier: "core"' in content
        assert 'aliases: ["Test Note"]' in content

    def test_write_with_body(self, vault):
        """Creates a note file with body content."""
        result = runner.invoke(
            app,
            [
                "memory",
                "write",
                "--title",
                "Body Note",
                "--type",
                "feedback",
                "--tier",
                "core",
                "--body",
                "This is the body content.",
                "--vault",
                str(vault),
            ],
        )
        assert result.exit_code == 0
        assert "body: included" in result.stdout

        note_path = vault / "feedback" / "body-note.md"
        assert note_path.exists()
        content = note_path.read_text(encoding="utf-8")
        assert "This is the body content." in content

    def test_update_without_body_refuses_and_preserves_content(self, vault):
        """--update with no --body must not truncate the note.

        Regression: the update path rebuilt the file around an empty body with
        no guard, so `memory write --update --title X` silently deleted
        everything the note said. Six notes in the live vault lost their bodies
        this way (recoverable only from a 2026-08-25 backup).
        """
        original = "PRECIOUS CONTENT THAT MUST SURVIVE THE UPDATE"
        runner.invoke(
            app,
            [
                "memory", "write",
                "--title", "Clobber Probe",
                "--type", "knowledge",
                "--tier", "core",
                "--body", original,
                "--vault", str(vault),
            ],
            catch_exceptions=False,
        )
        note_path = vault / "knowledge" / "clobber-probe.md"
        assert original in note_path.read_text(encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "memory", "write",
                "--title", "Clobber Probe",
                "--type", "knowledge",
                "--tier", "core",
                "--update",
                "--vault", str(vault),
            ],
        )
        assert result.exit_code == 1
        assert "Refusing to update without a body" in result.stderr
        # The whole point: content intact, file untouched.
        assert original in note_path.read_text(encoding="utf-8")

    def test_update_with_body_still_works(self, vault):
        """The guard must not block a legitimate body-carrying update."""
        runner.invoke(
            app,
            [
                "memory", "write",
                "--title", "Legit Update",
                "--type", "knowledge",
                "--tier", "core",
                "--body", "first version",
                "--vault", str(vault),
            ],
            catch_exceptions=False,
        )
        result = runner.invoke(
            app,
            [
                "memory", "write",
                "--title", "Legit Update",
                "--type", "knowledge",
                "--tier", "core",
                "--update",
                "--body", "second version",
                "--vault", str(vault),
            ],
        )
        assert result.exit_code == 0
        content = (vault / "knowledge" / "legit-update.md").read_text(encoding="utf-8")
        assert "second version" in content
        assert "first version" not in content

    def test_update_with_empty_body_is_explicit(self, vault):
        """--body '' is a deliberate truncation, not an omission, so it is allowed."""
        runner.invoke(
            app,
            [
                "memory", "write",
                "--title", "Deliberate Empty",
                "--type", "knowledge",
                "--tier", "core",
                "--body", "content to remove",
                "--vault", str(vault),
            ],
            catch_exceptions=False,
        )
        result = runner.invoke(
            app,
            [
                "memory", "write",
                "--title", "Deliberate Empty",
                "--type", "knowledge",
                "--tier", "core",
                "--update",
                "--body", "",
                "--vault", str(vault),
            ],
        )
        assert result.exit_code == 0

    def test_write_with_body_file(self, vault, tmp_path):
        """Reads body from a file."""
        body_file = tmp_path / "body.md"
        body_file.write_text("Body from file.", encoding="utf-8")
        result = runner.invoke(
            app,
            [
                "memory",
                "write",
                "--title",
                "File Body Note",
                "--type",
                "knowledge",
                "--tier",
                "project",
                "--body-file",
                str(body_file),
                "--vault",
                str(vault),
            ],
        )
        assert result.exit_code == 0
        assert "body: included" in result.stdout

        note_path = vault / "knowledge" / "file-body-note.md"
        assert note_path.exists()
        content = note_path.read_text(encoding="utf-8")
        assert "Body from file." in content

    def test_write_entity_uses_entities_dir(self, vault):
        """`entity` maps to entities/, not the buggy `entitys/`."""
        result = runner.invoke(
            app,
            [
                "memory", "write",
                "--title", "Some Entity",
                "--type", "entity",
                "--tier", "project",
                "--vault", str(vault),
            ],
        )
        assert result.exit_code == 0
        assert (vault / "entities" / "some-entity.md").exists()
        assert not (vault / "entitys" / "some-entity.md").exists()

    def test_write_new_note_nests_under_category(self, vault):
        """A new note with --category lands in <type-dir>/<category>/."""
        result = runner.invoke(
            app,
            [
                "memory", "write",
                "--title", "Categorized Note",
                "--type", "knowledge",
                "--tier", "core",
                "--category", "patterns",
                "--body", "Body.",
                "--vault", str(vault),
            ],
        )
        assert result.exit_code == 0
        assert (vault / "knowledge" / "patterns" / "categorized-note.md").exists()

    def test_write_update_finds_note_in_category_subdir(self, vault):
        """--update patches a note nested under <type>/<category>/ regardless of layout.

        Regression test: previously --update only looked in <type>s/<id>.md and
        failed for notes that live under a category subdirectory.
        """
        nested_dir = vault / "knowledge" / "patterns"
        nested_dir.mkdir(parents=True, exist_ok=True)
        note_path = nested_dir / "nested-note.md"
        note_path.write_text(
            "---\nid: nested-note\ntype: knowledge\ncategory: patterns\n---\n\nOriginal.\n",
            encoding="utf-8",
        )
        result = runner.invoke(
            app,
            [
                "memory", "write",
                "--title", "Nested Note",
                "--type", "knowledge",
                "--tier", "core",
                "--category", "patterns",
                "--body", "Patched.",
                "--update",
                "--vault", str(vault),
            ],
        )
        assert result.exit_code == 0
        assert "Updated note" in result.stdout
        # Patched in place — no stray knowledges/ file created.
        content = note_path.read_text(encoding="utf-8")
        assert "Patched." in content
        assert "Original." not in content
        assert not (vault / "knowledges").exists()

    def test_write_update_existing(self, vault):
        """Patches existing note body with --update."""
        _create_note_file(vault, "updatable-note", "knowledge", body="Original body.")
        result = runner.invoke(
            app,
            [
                "memory",
                "write",
                "--title",
                "Updatable Note",
                "--type",
                "knowledge",
                "--tier",
                "core",
                "--body",
                "Updated body.",
                "--update",
                "--vault",
                str(vault),
            ],
        )
        assert result.exit_code == 0
        assert "Updated note" in result.stdout

        note_path = vault / "knowledge" / "updatable-note.md"
        content = note_path.read_text(encoding="utf-8")
        assert "Updated body." in content
        assert "Original body." not in content

    def test_write_update_nonexistent_fails(self, vault):
        """--update on a non-existent note raises error."""
        result = runner.invoke(
            app,
            [
                "memory",
                "write",
                "--title",
                "No Such Note",
                "--type",
                "knowledge",
                "--tier",
                "core",
                "--body",
                "Body.",
                "--update",
                "--vault",
                str(vault),
            ],
        )
        assert result.exit_code == 1
        assert (
            "does not exist" in result.stderr.lower() or "does not exist" in result.stdout.lower()
        )

    def test_write_existing_no_update_fails(self, vault):
        """Creating a note that already exists (without --update) raises error."""
        _create_note_file(vault, "duplicate-note", "knowledge", body="Existing.")
        result = runner.invoke(
            app,
            [
                "memory",
                "write",
                "--title",
                "Duplicate Note",
                "--type",
                "knowledge",
                "--tier",
                "core",
                "--vault",
                str(vault),
            ],
        )
        assert result.exit_code == 1
        assert (
            "already exists" in result.stderr.lower() or "already exists" in result.stdout.lower()
        )

    def test_write_with_no_encode_flag(self, vault):
        """--no-encode skips the background encode."""
        result = runner.invoke(
            app,
            [
                "memory",
                "write",
                "--title",
                "No Encode",
                "--type",
                "knowledge",
                "--tier",
                "project",
                "--no-encode",
                "--vault",
                str(vault),
            ],
        )
        assert result.exit_code == 0
        note_path = vault / "knowledge" / "no-encode.md"
        assert note_path.exists()

    def test_write_conflicting_body_options(self, vault):
        """--body and --body-file are mutually exclusive."""
        result = runner.invoke(
            app,
            [
                "memory",
                "write",
                "--title",
                "Conflict",
                "--type",
                "knowledge",
                "--tier",
                "core",
                "--body",
                "inline",
                "--body-file",
                "/tmp/foo.md",
                "--vault",
                str(vault),
            ],
        )
        assert result.exit_code == 1
        assert "Conflicting" in result.stderr or "Conflicting" in result.stdout


# ---------------------------------------------------------------------------
# _meta freshness stamping
# ---------------------------------------------------------------------------


class TestMemoryMetaFreshness:
    """`_meta.generated` must move on every write, not just full encodes.

    Regression: the inline write/delete paths patched only `_meta.count`, so
    `generated` kept reporting the last full encode. The index then read stale
    while actually being current — the failure direction that trains you to
    ignore the freshness signal.
    """

    def _meta(self, vault):
        path = vault / "_sync" / "encoded" / "memory.json"
        return json.loads(path.read_text(encoding="utf-8"))["_meta"]

    def _write(self, vault, title):
        return runner.invoke(
            app,
            [
                "memory",
                "write",
                "--title",
                title,
                "--type",
                "knowledge",
                "--tier",
                "project",
                "--vault",
                str(vault),
            ],
        )

    def test_inline_write_advances_generated(self, vault):
        """A CLI write moves `generated` forward off the encoded value."""
        _write_memory_json(vault, {})
        before = self._meta(vault)["generated"]
        assert before == "2026-01-01T00:00:00"

        assert self._write(vault, "Freshness Probe").exit_code == 0
        assert self._meta(vault)["generated"] != before

    def test_inline_write_preserves_last_full_encode(self, vault):
        """`last_full_encode` tracks rebuilds only, so a write must not move it."""
        path = _write_memory_json(vault, {})
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_meta"]["last_full_encode"] = "2026-01-01T00:00:00"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        assert self._write(vault, "Reconcile Probe").exit_code == 0
        meta = self._meta(vault)
        assert meta["last_full_encode"] == "2026-01-01T00:00:00"
        assert meta["generated"] > meta["last_full_encode"]

    def test_backfill_from_legacy_generated(self, vault):
        """A pre-upgrade file has no `last_full_encode`; its `generated` is it.

        Exact rather than heuristic: the inline paths provably never touched
        `generated` before this change, so the stored value IS the last encode.
        """
        _write_memory_json(vault, {})
        assert "last_full_encode" not in self._meta(vault)

        assert self._write(vault, "Legacy Probe").exit_code == 0
        meta = self._meta(vault)
        assert meta["last_full_encode"] == "2026-01-01T00:00:00"
        assert meta["generated"] != meta["last_full_encode"]

    def test_inline_delete_advances_generated(self, vault):
        """The delete path had the identical omission, so it is covered too."""
        note = {
            "doomed-note": {
                "id": "doomed-note",
                "type": "knowledge",
                "category": "",
                "tier": "project",
                "tags": [],
                "updated": "2026-01-01",
                "aliases": [],
                "content": "temporary",
            }
        }
        _write_memory_json(vault, note)
        _create_note_file(vault, "doomed-note", "knowledge", "temporary")

        result = runner.invoke(
            app, ["memory", "delete", "doomed-note", "--yes", "--vault", str(vault)]
        )
        assert result.exit_code == 0
        assert self._meta(vault)["generated"] != "2026-01-01T00:00:00"


# ---------------------------------------------------------------------------
# cortex memory search
# ---------------------------------------------------------------------------


class TestMemorySearch:
    def test_search_finds_match(self, vault):
        """Search returns matching notes."""
        _write_memory_json(
            vault,
            {
                "jira-tips": {
                    "id": "jira-tips",
                    "type": "knowledge",
                    "category": "patterns",
                    "tier": "skill:jira",
                    "aliases": ["Jira Tips"],
                    "updated": "2026-01-01",
                    "content": "Use JQL for advanced filtering.",
                }
            },
        )
        result = runner.invoke(app, ["memory", "search", "jira", "--vault", str(vault)])
        assert result.exit_code == 0
        assert "jira-tips" in result.stdout

    def test_search_no_match(self, vault):
        """Search returns empty results."""
        _write_memory_json(vault, {})
        result = runner.invoke(app, ["memory", "search", "nonexistent", "--vault", str(vault)])
        assert result.exit_code == 0
        assert "No results" in result.stdout

    def test_search_with_multiple_notes(self, vault):
        """Search ranks by relevance and returns top results."""
        _write_memory_json(
            vault,
            {
                "python-style": {
                    "id": "python-style",
                    "type": "feedback",
                    "tier": "core",
                    "aliases": ["Python Style"],
                    "content": "Use snake_case in Python.",
                },
                "typescript-style": {
                    "id": "typescript-style",
                    "type": "feedback",
                    "tier": "core",
                    "aliases": ["TypeScript Style"],
                    "content": "Use camelCase in TypeScript.",
                },
            },
        )
        result = runner.invoke(app, ["memory", "search", "style", "--vault", str(vault)])
        assert result.exit_code == 0
        assert "python-style" in result.stdout
        assert "typescript-style" in result.stdout


def _write_memory_json_with_graph(vault_root: Path, notes: dict, graph: dict | None = None) -> Path:
    """Write a memory.json with optional graph data."""
    encoded_dir = vault_root / "_sync" / "encoded"
    encoded_dir.mkdir(parents=True, exist_ok=True)
    path = encoded_dir / "memory.json"
    data = {
        "_meta": {"generated": "2026-01-01T00:00:00", "count": len(notes)},
        "notes": notes,
    }
    if graph is not None:
        data["_graph"] = graph
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _result_ids(stdout: str) -> list[str]:
    """Extract note ids from search output (lines indented with two spaces)."""
    ids = []
    for line in stdout.split("\n"):
        if line.startswith("  ") and "·" in line:
            ids.append(line.strip().split()[0])
    return ids


class TestMemorySearchImproved:
    """Tests for tokenized per-term scoring, IDF, tier, recency, and graph boost."""

    def test_multi_word_query(self, vault):
        """Multi-word query matches notes with terms in separate locations."""
        _write_memory_json(
            vault,
            {
                "sprint-retro": {
                    "id": "sprint-retro",
                    "type": "knowledge",
                    "category": "patterns",
                    "tier": "core",
                    "aliases": [],
                    "updated": "2026-01-01",
                    "content": "Hold a sprint retrospective after each sprint.",
                },
            },
        )
        result = runner.invoke(app, ["memory", "search", "sprint retro", "--vault", str(vault)])
        assert result.exit_code == 0
        assert "sprint-retro" in result.stdout

    def test_term_coverage_ranking(self, vault):
        """Note matching all query terms ranks above note matching only some."""
        _write_memory_json(
            vault,
            {
                "python-style": {
                    "id": "python-style",
                    "type": "feedback",
                    "tier": "core",
                    "aliases": [],
                    "updated": "2026-01-01",
                    "content": "Use snake_case in Python.",
                },
                "python-testing": {
                    "id": "python-testing",
                    "type": "knowledge",
                    "tier": "project",
                    "aliases": [],
                    "updated": "2026-01-01",
                    "content": "Use pytest for Python testing.",
                },
            },
        )
        result = runner.invoke(app, ["memory", "search", "python testing", "--vault", str(vault)])
        assert result.exit_code == 0
        result_ids = _result_ids(result.stdout)
        assert result_ids[0] == "python-testing"

    def test_idf_rare_term_boosts(self, vault):
        """A term rare in 1 note scores higher than a term common in all notes."""
        _write_memory_json(
            vault,
            {
                "common-note": {
                    "id": "common-note",
                    "type": "knowledge",
                    "tier": "core",
                    "aliases": [],
                    "updated": "2026-01-01",
                    "content": "The word project appears in every note here.",
                },
                "rare-note": {
                    "id": "rare-note",
                    "type": "knowledge",
                    "tier": "core",
                    "aliases": [],
                    "updated": "2026-01-01",
                    "content": "The word zephyr appears only here.",
                },
            },
        )
        result = runner.invoke(app, ["memory", "search", "zephyr", "--vault", str(vault)])
        assert result.exit_code == 0
        assert "rare-note" in result.stdout
        assert "common-note" not in result.stdout

    def test_tier_bonus(self, vault):
        """Core tier note ranks above vault-only for same content match."""
        _write_memory_json(
            vault,
            {
                "core-note": {
                    "id": "core-note",
                    "type": "knowledge",
                    "tier": "core",
                    "aliases": [],
                    "updated": "2026-01-01",
                    "content": "The word nebula appears in both notes.",
                },
                "vault-note": {
                    "id": "vault-note",
                    "type": "knowledge",
                    "tier": "vault-only",
                    "aliases": [],
                    "updated": "2026-01-01",
                    "content": "The word nebula appears in both notes.",
                },
            },
        )
        result = runner.invoke(app, ["memory", "search", "nebula", "--vault", str(vault)])
        assert result.exit_code == 0
        result_ids = _result_ids(result.stdout)
        assert result_ids[0] == "core-note"

    def test_recency_tiebreaker(self, vault):
        """Same score — newer updated date wins."""
        _write_memory_json(
            vault,
            {
                "old-note": {
                    "id": "old-note",
                    "type": "knowledge",
                    "tier": "core",
                    "aliases": [],
                    "updated": "2025-01-01",
                    "content": "Both have the word quasar.",
                },
                "new-note": {
                    "id": "new-note",
                    "type": "knowledge",
                    "tier": "core",
                    "aliases": [],
                    "updated": "2026-06-01",
                    "content": "Both have the word quasar.",
                },
            },
        )
        result = runner.invoke(app, ["memory", "search", "quasar", "--vault", str(vault)])
        assert result.exit_code == 0
        result_ids = _result_ids(result.stdout)
        assert result_ids[0] == "new-note"

    def test_graph_boost(self, vault):
        """Note linked to a top-3 result gets +2 graph boost."""
        _write_memory_json_with_graph(
            vault,
            {
                "main-topic": {
                    "id": "main-topic",
                    "type": "knowledge",
                    "tier": "core",
                    "aliases": [],
                    "updated": "2026-01-01",
                    "content": "Core content about the word cipher.",
                },
                "linked-topic": {
                    "id": "linked-topic",
                    "type": "knowledge",
                    "tier": "project",
                    "aliases": [],
                    "updated": "2026-01-01",
                    "content": "Mentions cipher briefly.",
                },
                "unlinked-topic": {
                    "id": "unlinked-topic",
                    "type": "knowledge",
                    "tier": "project",
                    "aliases": [],
                    "updated": "2026-01-01",
                    "content": "Also mentions cipher briefly.",
                },
            },
            graph={
                "edges": [["main-topic", "linked-topic"]],
                "adjacency": {"main-topic": ["linked-topic"], "linked-topic": ["main-topic"]},
            },
        )
        result = runner.invoke(app, ["memory", "search", "cipher", "--vault", str(vault)])
        assert result.exit_code == 0
        result_ids = _result_ids(result.stdout)
        # linked-topic should rank above unlinked-topic due to graph boost
        assert result_ids.index("linked-topic") < result_ids.index("unlinked-topic")

    def test_snippet_shows_match_context(self, vault):
        """Snippet contains the matching term, not just first 80 chars."""
        long_prefix = "word " * 16  # 80 chars of filler before the match
        _write_memory_json(
            vault,
            {
                "note-with-long-prefix": {
                    "id": "note-with-long-prefix",
                    "type": "knowledge",
                    "tier": "core",
                    "aliases": [],
                    "updated": "2026-01-01",
                    "content": f"{long_prefix}The target word calderon appears here.",
                },
            },
        )
        result = runner.invoke(app, ["memory", "search", "calderon", "--vault", str(vault)])
        assert result.exit_code == 0
        assert "calderon" in result.stdout

    def test_empty_query_terms(self, vault):
        """Query with only stop words returns no results gracefully."""
        _write_memory_json(
            vault,
            {
                "some-note": {
                    "id": "some-note",
                    "type": "knowledge",
                    "tier": "core",
                    "aliases": [],
                    "updated": "2026-01-01",
                    "content": "Just a regular note.",
                },
            },
        )
        result = runner.invoke(app, ["memory", "search", "the and", "--vault", str(vault)])
        assert result.exit_code == 0
        assert "No results" in result.stdout


# ---------------------------------------------------------------------------
# cortex memory delete
# ---------------------------------------------------------------------------


class TestMemoryDelete:
    def test_delete_with_yes_removes_file(self, vault):
        """--yes deletes the note file without prompting."""
        _create_note_file(vault, "doomed-note", "knowledge", body="Delete me.")
        note_path = vault / "knowledge" / "doomed-note.md"
        assert note_path.exists()

        result = runner.invoke(
            app, ["memory", "delete", "doomed-note", "--yes", "--vault", str(vault)]
        )
        assert result.exit_code == 0
        assert "Deleted note" in result.stdout
        assert not note_path.exists()

    def test_delete_prunes_memory_json(self, vault):
        """Delete removes the note entry and its graph edges from memory.json."""
        _create_note_file(vault, "linked-note", "knowledge", body="Linked.")
        _write_memory_json(
            vault,
            {
                "linked-note": {
                    "id": "linked-note",
                    "type": "knowledge",
                    "tier": "core",
                    "aliases": ["Linked Note"],
                    "content": "Linked.",
                },
                "other-note": {
                    "id": "other-note",
                    "type": "knowledge",
                    "tier": "core",
                    "aliases": ["Other Note"],
                    "content": "Other.",
                },
            },
        )
        # Add graph edges referencing the note both ways.
        mem_path = vault / "_sync" / "encoded" / "memory.json"
        data = json.loads(mem_path.read_text(encoding="utf-8"))
        data["_graph"] = {
            "adjacency": {
                "linked-note": ["other-note"],
                "other-note": ["linked-note"],
            },
            "edges": [
                {"source": "linked-note", "target": "other-note"},
                {"source": "other-note", "target": "linked-note"},
            ],
        }
        mem_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        result = runner.invoke(
            app, ["memory", "delete", "linked-note", "--yes", "--vault", str(vault)]
        )
        assert result.exit_code == 0

        data = json.loads(mem_path.read_text(encoding="utf-8"))
        assert "linked-note" not in data["notes"]
        assert "other-note" in data["notes"]
        assert data["_meta"]["count"] == 1
        adjacency = data["_graph"]["adjacency"]
        assert "linked-note" not in adjacency
        assert "linked-note" not in adjacency.get("other-note", [])
        edges = data["_graph"]["edges"]
        assert all(
            e["source"] != "linked-note" and e["target"] != "linked-note" for e in edges
        )

    def test_delete_nonexistent_fails(self, vault):
        """Deleting a note that does not exist raises an error."""
        result = runner.invoke(
            app, ["memory", "delete", "no-such-note", "--yes", "--vault", str(vault)]
        )
        assert result.exit_code == 1
        assert "not found" in result.stdout.lower() or "not found" in result.stderr.lower()

    def test_delete_aborts_on_no_confirmation(self, vault):
        """Answering 'n' to the prompt keeps the file."""
        _create_note_file(vault, "keep-note", "knowledge", body="Keep me.")
        note_path = vault / "knowledge" / "keep-note.md"

        result = runner.invoke(
            app, ["memory", "delete", "keep-note", "--vault", str(vault)], input="n\n"
        )
        assert result.exit_code == 0
        assert "Aborted" in result.stdout
        assert note_path.exists()

    def test_delete_confirms_and_deletes(self, vault):
        """Answering 'y' to the prompt deletes the file."""
        _create_note_file(vault, "confirm-note", "knowledge", body="Bye.")
        note_path = vault / "knowledge" / "confirm-note.md"

        result = runner.invoke(
            app, ["memory", "delete", "confirm-note", "--vault", str(vault)], input="y\n"
        )
        assert result.exit_code == 0
        assert "Deleted note" in result.stdout
        assert not note_path.exists()

    def test_delete_no_encode_skips_pruning(self, vault):
        """--no-encode deletes the file but leaves memory.json untouched."""
        _create_note_file(vault, "stale-note", "knowledge", body="Stale.")
        _write_memory_json(
            vault,
            {
                "stale-note": {
                    "id": "stale-note",
                    "type": "knowledge",
                    "tier": "core",
                    "aliases": ["Stale Note"],
                    "content": "Stale.",
                }
            },
        )
        result = runner.invoke(
            app,
            ["memory", "delete", "stale-note", "--yes", "--no-encode", "--vault", str(vault)],
        )
        assert result.exit_code == 0
        assert not (vault / "knowledge" / "stale-note.md").exists()

        mem_path = vault / "_sync" / "encoded" / "memory.json"
        data = json.loads(mem_path.read_text(encoding="utf-8"))
        # Entry left intact because pruning was skipped.
        assert "stale-note" in data["notes"]
