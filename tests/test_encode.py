"""Tests for encoder — pure functions and VaultNote model."""

import json

import pytest

from cortex.encoder.core import (
    VaultNote,
    _resolve_skill_dirs,
    excluded,
    find_drained_notes,
    hive_eligible,
    load_config,
    parse_frontmatter,
    read_vault_schema,
    scan_vault,
    strip_leading_h1,
    strip_related_section,
    strip_wiki_links,
    sync_skill_embeds,
    validate_target_config,
    validate_vault_artifact_paths,
    write_file,
)

# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_valid_frontmatter(self):
        content = "---\nid: foo\ntype: knowledge\n---\n\nBody here."
        meta, body = parse_frontmatter(content)
        assert meta["id"] == "foo"
        assert meta["type"] == "knowledge"
        assert body == "Body here."

    def test_no_frontmatter(self):
        content = "Just plain text, no YAML."
        meta, body = parse_frontmatter(content)
        assert meta == {}
        assert body == content

    def test_missing_closing_delimiter(self):
        content = "---\nid: foo\n---NOPE"
        meta, body = parse_frontmatter(content)
        # `\n---` is found inside `---NOPE` so frontmatter is still parsed
        assert meta["id"] == "foo"

    def test_malformed_yaml(self):
        content = "---\n: : invalid: yaml:\n---\nBody."
        meta, body = parse_frontmatter(content)
        # Should return empty meta on YAML error
        assert meta == {}

    def test_empty_frontmatter(self):
        content = "---\n---\nBody."
        meta, body = parse_frontmatter(content)
        # Empty YAML parses to None which is not a dict
        assert meta == {}

    def test_non_dict_frontmatter(self):
        content = "---\n- item1\n- item2\n---\nBody."
        meta, body = parse_frontmatter(content)
        assert meta == {}

    def test_body_with_blank_lines(self):
        content = "---\nid: test\n---\n\n\nBody with leading blanks."
        meta, body = parse_frontmatter(content)
        assert body == "Body with leading blanks."

    def test_frontmatter_with_dates(self):
        content = '---\nupdated: "2024-01-15"\n---\nBody.'
        meta, body = parse_frontmatter(content)
        assert meta["updated"] == "2024-01-15"

    def test_frontmatter_with_lists(self):
        content = "---\ntags: [foo, bar, baz]\n---\nBody."
        meta, body = parse_frontmatter(content)
        assert meta["tags"] == ["foo", "bar", "baz"]


# ---------------------------------------------------------------------------
# strip_wiki_links
# ---------------------------------------------------------------------------


class TestStripWikiLinks:
    def test_simple_link(self):
        assert strip_wiki_links("See [[page]] for details.") == "See page for details."

    def test_alias_link(self):
        assert strip_wiki_links("See [[page|display text]] here.") == "See display text here."

    def test_no_links(self):
        text = "No wiki links here."
        assert strip_wiki_links(text) == text

    def test_multiple_links(self):
        text = "See [[foo]] and [[bar|Bar]] and [[baz]]."
        assert strip_wiki_links(text) == "See foo and Bar and baz."

    def test_nested_brackets_no_match(self):
        text = "Not a link [[incomplete"
        assert strip_wiki_links(text) == text

    def test_empty_link(self):
        assert strip_wiki_links("[[]]") == "[[]]"

    def test_link_at_start(self):
        assert strip_wiki_links("[[page]] is first.") == "page is first."

    def test_link_with_hyphenated_id(self):
        assert strip_wiki_links("[[my-page-name]]") == "my-page-name"


# ---------------------------------------------------------------------------
# strip_leading_h1
# ---------------------------------------------------------------------------


class TestStripLeadingH1:
    def test_strips_h1(self):
        text = "# Title\n\nBody content."
        assert strip_leading_h1(text) == "Body content."

    def test_no_h1(self):
        text = "## Subtitle\nBody."
        assert strip_leading_h1(text) == text

    def test_h1_with_leading_newlines(self):
        text = "\n\n# Title\nBody."
        assert strip_leading_h1(text) == "Body."

    def test_only_h1(self):
        text = "# Title"
        assert strip_leading_h1(text) == ""

    def test_h1_not_at_start(self):
        text = "Some text\n# Title\nBody."
        assert strip_leading_h1(text) == text

    def test_h1_with_body_after_blank(self):
        text = "# Title\n\nBody after blank line."
        result = strip_leading_h1(text)
        assert result == "Body after blank line."


# ---------------------------------------------------------------------------
# strip_related_section
# ---------------------------------------------------------------------------


class TestStripRelatedSection:
    def test_strips_trailing_related(self):
        text = "Body content.\n\n## Related\n- [[note1]]\n- [[note2]]"
        assert strip_related_section(text) == "Body content."

    def test_no_related_section(self):
        text = "Body content."
        assert strip_related_section(text) == text

    def test_related_not_at_end(self):
        text = "## Related\nSome stuff\n\nMore body after."
        result = strip_related_section(text)
        assert "More body after" in result

    def test_case_insensitive(self):
        text = "Body.\n\n## RELATED\nstuff"
        assert strip_related_section(text) == "Body."

    def test_related_with_no_content_after(self):
        text = "Body.\n\n## Related"
        assert strip_related_section(text) == "Body."


# ---------------------------------------------------------------------------
# VaultNote
# ---------------------------------------------------------------------------


class TestVaultNote:
    @pytest.fixture
    def note(self, tmp_path):
        p = tmp_path / "test-note.md"
        meta = {
            "id": "test-note",
            "type": "knowledge",
            "tier": "core",
            "category": "patterns",
            "tags": ["tag1", "tag2"],
            "aliases": ["Test Note"],
            "updated": "2024-01-15",
        }
        return VaultNote(p, meta, "Some body text.")

    @pytest.fixture
    def note_no_alias(self, tmp_path):
        p = tmp_path / "no-alias.md"
        meta = {"id": "my-note-id", "type": "feedback"}
        return VaultNote(p, meta, "Body.")

    def test_name_from_meta(self, note):
        assert note.name == "test-note"

    def test_name_from_path_stem(self, tmp_path):
        p = tmp_path / "fallback-name.md"
        note = VaultNote(p, {"type": "knowledge"}, "Body.")
        assert note.name == "fallback-name"

    def test_title_from_alias(self, note):
        assert note.title() == "Test Note"

    def test_title_from_id(self, note_no_alias):
        assert note_no_alias.title() == "My Note Id"

    def test_as_str_none(self):
        assert VaultNote._as_str(None) == ""

    def test_as_str_int(self):
        assert VaultNote._as_str(42) == "42"

    def test_as_list_none(self):
        assert VaultNote._as_list(None) == []

    def test_as_list_string(self):
        assert VaultNote._as_list("single") == ["single"]

    def test_as_list_list(self):
        assert VaultNote._as_list(["a", "b"]) == ["a", "b"]

    def test_as_list_empty_string(self):
        assert VaultNote._as_list("") == []

    def test_clean_body_default(self, note):
        body = "# Title\n\nSome body with [[link]].\n\n## Related\n- stuff"
        note.body = body
        result = note.clean_body()
        # Default: strip_links=True, drop_h1=True, drop_related=False
        assert "[[link]]" not in result
        assert "link" in result

    def test_clean_body_no_strip(self, note):
        body = "# Title\nContent [[link]]."
        note.body = body
        result = note.clean_body(strip_links=False, drop_h1=False)
        assert "[[link]]" in result
        assert result.startswith("# Title")

    def test_clean_body_drop_related(self, note):
        body = "Content.\n\n## Related\n- stuff"
        note.body = body
        result = note.clean_body(drop_related=True)
        assert "Related" not in result

    def test_to_dict(self, note):
        d = note.to_dict()
        assert d["id"] == "test-note"
        assert d["type"] == "knowledge"
        assert d["tier"] == "core"
        assert "tag1" in d["tags"]

    def test_drained_flag(self, tmp_path):
        p = tmp_path / "drained.md"
        note = VaultNote(p, {"type": "session", "drained": True}, "Body.")
        assert note.drained is True

    def test_drained_false_by_default(self, tmp_path):
        p = tmp_path / "fresh.md"
        note = VaultNote(p, {"type": "session"}, "Body.")
        assert note.drained is False

    def test_hive_true(self, tmp_path):
        p = tmp_path / "h.md"
        note = VaultNote(p, {"type": "knowledge", "hive": True}, "Body.")
        assert note.hive is True

    def test_hive_false(self, tmp_path):
        p = tmp_path / "h.md"
        note = VaultNote(p, {"type": "knowledge", "hive": False}, "Body.")
        assert note.hive is False

    def test_hive_none_default(self, tmp_path):
        p = tmp_path / "h.md"
        note = VaultNote(p, {"type": "knowledge"}, "Body.")
        assert note.hive is None


# ---------------------------------------------------------------------------
# validate_target_config
# ---------------------------------------------------------------------------


class TestValidateTargetConfig:
    def test_all_keys_present(self):
        assert validate_target_config("test", {"a": 1, "b": 2}, ["a", "b"]) is True

    def test_missing_key(self):
        assert validate_target_config("test", {"a": 1}, ["a", "b"]) is False

    def test_empty_required(self):
        assert validate_target_config("test", {}, []) is True


# ---------------------------------------------------------------------------
# excluded
# ---------------------------------------------------------------------------


class TestValidateVaultArtifactPaths:
    """Encoded artifacts must land inside the vault being encoded.

    Regression: `cortex encode --config <copy>/cortex.yaml` ignored the copy for
    output, because cortex.yaml stores absolute paths. It rewrote the original
    vault's memory.json — and that rebuild dropped every note whose type was
    missing from `include_types`, including ones the copy had just added.
    """

    def _targets(self, vault, **overrides):
        enc = vault / "_sync" / "encoded"
        t = {
            "core_context": {"enabled": True, "output_file": str(enc / "core-context.md")},
            "projects": {"enabled": True, "output_dir": str(enc / "projects")},
            "python-agents": {"enabled": True, "output_file": str(enc / "memory.json")},
        }
        t.update(overrides)
        return t

    def test_paths_inside_vault_pass(self, tmp_path):
        validate_vault_artifact_paths(tmp_path, self._targets(tmp_path))  # must not raise

    def test_absolute_path_to_another_vault_refused(self, tmp_path):
        vault = tmp_path / "vault"
        other = tmp_path / "original"
        (vault / "_sync").mkdir(parents=True)
        targets = self._targets(
            vault,
            **{
                "python-agents": {
                    "enabled": True,
                    "output_file": str(other / "memory.json"),
                }
            },
        )
        with pytest.raises(SystemExit) as exc:
            validate_vault_artifact_paths(vault, targets)
        assert "outside the vault" in str(exc.value)
        assert str(other) in str(exc.value)

    def test_relative_paths_resolve_against_vault(self, tmp_path):
        """A portable config must stay portable rather than error out."""
        targets = self._targets(
            tmp_path,
            core_context={"enabled": True, "output_file": "_sync/encoded/core-context.md"},
        )
        validate_vault_artifact_paths(tmp_path, targets)  # must not raise

    def test_traversal_out_of_vault_refused(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        targets = self._targets(
            vault,
            core_context={"enabled": True, "output_file": "../elsewhere/core-context.md"},
        )
        with pytest.raises(SystemExit):
            validate_vault_artifact_paths(vault, targets)

    def test_disabled_target_not_checked(self, tmp_path):
        """A disabled target with a stale path is not an error."""
        targets = self._targets(
            tmp_path,
            **{"python-agents": {"enabled": False, "output_file": "/nonexistent/elsewhere.json"}},
        )
        validate_vault_artifact_paths(tmp_path, targets)  # must not raise

    def test_skill_embed_outside_vault_allowed(self, tmp_path):
        """skill-embed deploys into an agent skills dir by design, so it is exempt."""
        targets = self._targets(
            tmp_path,
            **{
                "skills": {
                    "enabled": True,
                    "output_file": "/somewhere/else/.config/opencode/skills",
                    "skills_dir": "/somewhere/else/.config/opencode/skills",
                }
            },
        )
        validate_vault_artifact_paths(tmp_path, targets)  # must not raise

    def test_vault_root_itself_allowed(self, tmp_path):
        targets = self._targets(tmp_path, projects={"enabled": True, "output_dir": str(tmp_path)})
        validate_vault_artifact_paths(tmp_path, targets)  # must not raise


# ---------------------------------------------------------------------------
# excluded
# ---------------------------------------------------------------------------


class TestExcluded:
    def test_vault_only_type(self, tmp_path):
        p = tmp_path / "n.md"
        note = VaultNote(p, {"type": "session", "tier": "vault-only"}, "")
        assert excluded(note, set(), {"session"}) is True

    def test_excluded_tag(self, tmp_path):
        p = tmp_path / "n.md"
        note = VaultNote(p, {"type": "knowledge", "tags": ["draft"]}, "")
        assert excluded(note, {"draft"}, set()) is True

    def test_no_exclusion(self, tmp_path):
        p = tmp_path / "n.md"
        note = VaultNote(p, {"type": "knowledge", "tags": ["workflow"]}, "")
        assert excluded(note, {"draft"}, set()) is False

    def test_type_not_in_vault_only(self, tmp_path):
        p = tmp_path / "n.md"
        note = VaultNote(p, {"type": "knowledge"}, "")
        assert excluded(note, set(), {"session"}) is False


# ---------------------------------------------------------------------------
# hive_eligible
# ---------------------------------------------------------------------------


class TestHiveEligible:
    def _note(self, tmp_path, tier, hive_override=None):
        p = tmp_path / "n.md"
        meta = {"type": "knowledge", "tier": tier}
        if hive_override is not None:
            meta["hive"] = hive_override
        return VaultNote(p, meta, "")

    def test_explicit_hive_true(self, tmp_path, vault_notes):
        note = self._note(tmp_path, "vault-only", hive_override=True)
        config = {"hive": {"replicate_tiers": []}}
        assert hive_eligible(note, config) is True

    def test_explicit_hive_false(self, tmp_path):
        note = self._note(tmp_path, "core", hive_override=False)
        config = {"hive": {"replicate_tiers": ["core"]}}
        assert hive_eligible(note, config) is False

    def test_exact_tier_match(self, tmp_path):
        note = self._note(tmp_path, "core")
        config = {"hive": {"replicate_tiers": ["core"]}}
        assert hive_eligible(note, config) is True

    def test_wildcard_tier_match(self, tmp_path):
        note = self._note(tmp_path, "skill:python")
        config = {"hive": {"replicate_tiers": ["skill:*"]}}
        assert hive_eligible(note, config) is True

    def test_no_match(self, tmp_path):
        note = self._note(tmp_path, "vault-only")
        config = {"hive": {"replicate_tiers": ["core", "project"]}}
        assert hive_eligible(note, config) is False

    def test_empty_replicate_tiers(self, tmp_path):
        note = self._note(tmp_path, "core")
        config = {"hive": {"replicate_tiers": []}}
        assert hive_eligible(note, config) is False

    def test_multiple_patterns(self, tmp_path):
        note = self._note(tmp_path, "project")
        config = {"hive": {"replicate_tiers": ["core", "skill:*", "project"]}}
        assert hive_eligible(note, config) is True


# ---------------------------------------------------------------------------
# find_drained_notes
# ---------------------------------------------------------------------------


class TestFindDrainedNotes:
    def test_finds_drained_session(self, tmp_path):
        p = tmp_path / "drained.md"
        note = VaultNote(p, {"type": "session", "drained": True}, "")
        assert len(find_drained_notes([note])) == 1

    def test_skips_non_drained(self, tmp_path):
        p = tmp_path / "fresh.md"
        note = VaultNote(p, {"type": "session", "drained": False}, "")
        assert len(find_drained_notes([note])) == 0

    def test_skips_wrong_type(self, tmp_path):
        p = tmp_path / "knowledge.md"
        note = VaultNote(p, {"type": "knowledge", "drained": True}, "")
        assert len(find_drained_notes([note])) == 0

    def test_mixed_notes(self, tmp_path):
        notes = [
            VaultNote(tmp_path / "a.md", {"type": "session", "drained": True}, ""),
            VaultNote(tmp_path / "b.md", {"type": "session", "drained": False}, ""),
            VaultNote(tmp_path / "c.md", {"type": "log", "drained": True}, ""),
            VaultNote(tmp_path / "d.md", {"type": "knowledge", "drained": True}, ""),
        ]
        assert len(find_drained_notes(notes)) == 2


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_valid_config(self, tmp_path):
        cfg_file = tmp_path / "cortex.yaml"
        cfg_file.write_text("vault_path: /tmp/vault\neager_tiers:\n  - core\n")
        cfg = load_config(cfg_file)
        assert cfg["vault_path"] == "/tmp/vault"
        assert cfg["eager_tiers"] == ["core"]
        # Hive defaults should be applied
        assert cfg["hive"]["enabled"] is False
        assert cfg["hive"]["sync_interval"] == 300

    def test_config_with_hive_block(self, tmp_path):
        cfg_file = tmp_path / "cortex.yaml"
        cfg_file.write_text(
            "vault_path: /tmp/vault\nhive:\n  enabled: true\n  machine_id: test-1\n"
        )
        cfg = load_config(cfg_file)
        assert cfg["hive"]["enabled"] is True
        assert cfg["hive"]["machine_id"] == "test-1"
        # Defaults still applied for unset keys
        assert cfg["hive"]["sync_interval"] == 300


# ---------------------------------------------------------------------------
# read_vault_schema
# ---------------------------------------------------------------------------


class TestReadVaultSchema:
    def test_valid_memory_json(self, tmp_path):
        sync_dir = tmp_path / "_sync" / "encoded"
        sync_dir.mkdir(parents=True)
        (sync_dir / "memory.json").write_text(json.dumps({"_meta": {"schema_version": 2}}))
        assert read_vault_schema(tmp_path) == 2

    def test_missing_memory_json(self, tmp_path):
        assert read_vault_schema(tmp_path) is None

    def test_no_meta_field(self, tmp_path):
        sync_dir = tmp_path / "_sync" / "encoded"
        sync_dir.mkdir(parents=True)
        (sync_dir / "memory.json").write_text(json.dumps({"notes": {}}))
        assert read_vault_schema(tmp_path) is None

    def test_corrupted_json(self, tmp_path):
        sync_dir = tmp_path / "_sync" / "encoded"
        sync_dir.mkdir(parents=True)
        (sync_dir / "memory.json").write_text("NOT JSON {{{")
        assert read_vault_schema(tmp_path) is None


# ---------------------------------------------------------------------------
# scan_vault (integration with example-vault fixture)
# ---------------------------------------------------------------------------


class TestScanVault:
    def test_finds_all_typed_notes(self, vault_notes):
        names = {n.name for n in vault_notes}
        assert "dev-preferences" in names
        assert "vault-capture-rules" in names
        assert "tiered-memory" in names
        assert "example-project" in names
        assert "2024-01-15-session" in names

    def test_note_count(self, vault_notes):
        assert len(vault_notes) == 5

    def test_tiers_assigned(self, vault_notes):
        by_name = {n.name: n for n in vault_notes}
        assert by_name["dev-preferences"].tier == "core"
        assert by_name["tiered-memory"].tier == "skill:example-skill"
        assert by_name["example-project"].tier == "project"
        assert by_name["2024-01-15-session"].tier == "vault-only"

    def test_skips_dot_dirs(self, vault):
        hidden = vault / ".hidden" / "secret.md"
        hidden.parent.mkdir()
        hidden.write_text("---\nid: secret\ntype: knowledge\n---\nBody.")
        notes = scan_vault(vault)
        assert all(n.name != "secret" for n in notes)

    def test_skips_underscore_dirs(self, vault):
        # example-vault already ships a _sync/ dir, so allow it to exist.
        udir = vault / "_sync" / "test.md"
        udir.parent.mkdir(parents=True, exist_ok=True)
        udir.write_text("---\nid: sync-note\ntype: knowledge\n---\nBody.")
        notes = scan_vault(vault)
        assert all(n.name != "sync-note" for n in notes)


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class TestWriteFile:
    def test_writes_new_file(self, tmp_path):
        p = tmp_path / "out" / "test.txt"
        write_file(p, "hello", dry=False)
        assert p.read_text() == "hello"

    def test_skips_unchanged(self, tmp_path, capsys):
        p = tmp_path / "test.txt"
        p.write_text("hello")
        write_file(p, "hello", dry=False)
        captured = capsys.readouterr()
        assert "unchanged" in captured.out

    def test_overwrites_changed(self, tmp_path):
        p = tmp_path / "test.txt"
        p.write_text("old")
        write_file(p, "new", dry=False)
        assert p.read_text() == "new"

    def test_dry_run(self, tmp_path, capsys):
        p = tmp_path / "test.txt"
        write_file(p, "content", dry=True)
        assert not p.exists()
        captured = capsys.readouterr()
        assert "[DRY]" in captured.out


# ---------------------------------------------------------------------------
# _resolve_skill_dirs
# ---------------------------------------------------------------------------


class TestResolveSkillDirs:
    def test_singular_only(self):
        dirs = _resolve_skill_dirs({"skills_dir": "/a/skills"})
        assert [str(d) for d in dirs] == ["/a/skills"]

    def test_plural_only(self):
        dirs = _resolve_skill_dirs({"skill_dirs": ["/a/skills", "/b/skills"]})
        assert [str(d) for d in dirs] == ["/a/skills", "/b/skills"]

    def test_singular_first_then_plural(self):
        dirs = _resolve_skill_dirs(
            {"skills_dir": "/a/skills", "skill_dirs": ["/b/skills"]}
        )
        assert [str(d) for d in dirs] == ["/a/skills", "/b/skills"]

    def test_dedupes_preserving_order(self):
        dirs = _resolve_skill_dirs(
            {"skills_dir": "/a/skills", "skill_dirs": ["/a/skills", "/b/skills"]}
        )
        assert [str(d) for d in dirs] == ["/a/skills", "/b/skills"]

    def test_plural_as_string(self):
        dirs = _resolve_skill_dirs({"skill_dirs": "/b/skills"})
        assert [str(d) for d in dirs] == ["/b/skills"]

    def test_empty(self):
        assert _resolve_skill_dirs({}) == []

    def test_expands_user(self):
        dirs = _resolve_skill_dirs({"skills_dir": "~/skills"})
        assert not str(dirs[0]).startswith("~")


# ---------------------------------------------------------------------------
# sync_skill_embeds — multi-dir resolution
# ---------------------------------------------------------------------------


class TestSyncSkillEmbeds:
    def _note(self, tmp_path, skill):
        p = tmp_path / f"{skill}-note.md"
        meta = {
            "id": f"{skill}-note",
            "type": "knowledge",
            "tier": f"skill:{skill}",
            "aliases": [f"{skill.title()} Note"],
        }
        return VaultNote(p, meta, "Encoded body.")

    def test_embeds_into_primary_dir(self, tmp_path):
        primary = tmp_path / "opencode" / "skills"
        (primary / "jira").mkdir(parents=True)
        note = self._note(tmp_path, "jira")
        written = sync_skill_embeds(
            [note], {"skills_dir": str(primary)}, strip_links=True, dry=False
        )
        assert written == ["jira"]
        assert (primary / "jira" / "reference.md").exists()

    def test_falls_back_to_secondary_dir(self, tmp_path):
        primary = tmp_path / "opencode" / "skills"
        secondary = tmp_path / "claude" / "skills"
        primary.mkdir(parents=True)
        (secondary / "graphify").mkdir(parents=True)
        note = self._note(tmp_path, "graphify")
        written = sync_skill_embeds(
            [note],
            {"skills_dir": str(primary), "skill_dirs": [str(secondary)]},
            strip_links=True,
            dry=False,
        )
        assert written == ["graphify"]
        # landed in secondary, not primary
        assert (secondary / "graphify" / "reference.md").exists()
        assert not (primary / "graphify").exists()

    def test_primary_wins_when_both_present(self, tmp_path):
        primary = tmp_path / "opencode" / "skills"
        secondary = tmp_path / "claude" / "skills"
        (primary / "jira").mkdir(parents=True)
        (secondary / "jira").mkdir(parents=True)
        note = self._note(tmp_path, "jira")
        sync_skill_embeds(
            [note],
            {"skills_dir": str(primary), "skill_dirs": [str(secondary)]},
            strip_links=True,
            dry=False,
        )
        assert (primary / "jira" / "reference.md").exists()
        assert not (secondary / "jira" / "reference.md").exists()

    def test_warns_when_missing_everywhere(self, tmp_path, capsys):
        primary = tmp_path / "opencode" / "skills"
        secondary = tmp_path / "claude" / "skills"
        primary.mkdir(parents=True)
        secondary.mkdir(parents=True)
        note = self._note(tmp_path, "ghost")
        written = sync_skill_embeds(
            [note],
            {"skills_dir": str(primary), "skill_dirs": [str(secondary)]},
            strip_links=True,
            dry=False,
        )
        assert written == []
        out = capsys.readouterr().out
        assert "skill dir missing" in out
        # names both searched locations
        assert str(primary / "ghost") in out
        assert str(secondary / "ghost") in out

    def test_no_dirs_configured(self, tmp_path, capsys):
        note = self._note(tmp_path, "jira")
        written = sync_skill_embeds([note], {}, strip_links=True, dry=False)
        assert written == []
        assert "skills_dir" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# config location vs declared vault_path
# ---------------------------------------------------------------------------


class TestConfigVaultAgreement:
    """`vault_path` in the config, not the config's location, picks the vault.

    Regression: `cortex encode --config <copy>/_sync/cortex.yaml` encoded the
    vault named inside that yaml — the original — while appearing to operate on
    the copy. The original was rebuilt from its own notes and lost every note
    whose type was absent from `include_types`.
    """

    def _vault(self, root, name, declared):
        v = root / name
        (v / "_sync").mkdir(parents=True)
        (v / "_sync" / "cortex.yaml").write_text(
            f'schema_version: 2\nvault_path: "{declared}"\n', encoding="utf-8"
        )
        return v

    def test_mismatched_config_refused(self, tmp_path):
        from cortex.encoder.core import run_encode

        original = self._vault(tmp_path, "original", tmp_path / "original")
        copy = self._vault(tmp_path, "copy", original)  # copy's yaml points at original
        with pytest.raises(SystemExit) as exc:
            run_encode(config_path=copy / "_sync" / "cortex.yaml")
        msg = str(exc.value)
        assert "disagree" in msg
        assert str(original) in msg

    def test_matching_config_allowed(self, tmp_path):
        from cortex.encoder.core import run_encode

        v = self._vault(tmp_path, "solo", tmp_path / "solo")
        # list_only short-circuits before any write, but still passes the check.
        assert run_encode(config_path=v / "_sync" / "cortex.yaml", list_only=True) == 0

    def test_show_config_bypasses_check(self, tmp_path):
        """The diagnostic must stay reachable — it is how you see the mismatch."""
        from cortex.encoder.core import run_encode

        original = self._vault(tmp_path, "original", tmp_path / "original")
        copy = self._vault(tmp_path, "copy", original)
        assert run_encode(config_path=copy / "_sync" / "cortex.yaml", show_config_only=True) == 0

    def test_config_outside_sync_dir_not_checked(self, tmp_path):
        """A repo-root cortex.yaml declares a vault elsewhere by design.

        Uses list_only (not show_config_only) because show_config_only
        short-circuits ahead of the guard and would pass vacuously.
        """
        from cortex.encoder.core import run_encode

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (tmp_path / "cortex.yaml").write_text(
            f'schema_version: 2\nvault_path: "{elsewhere}"\n', encoding="utf-8"
        )
        assert run_encode(config_path=tmp_path / "cortex.yaml", list_only=True) == 0
