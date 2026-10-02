#! /usr/bin/env python3
"""
cortex.cli.main — Typer CLI entry point for the `cortex` command.

All user-facing operations consolidated behind a single CLI:
  cortex bootstrap       Create venv + install Python deps
  cortex install         Bootstrap or upgrade Cortex for a user
  cortex install --upgrade Upgrade an existing install
  cortex uninstall       Revert Cortex-installed assets
  cortex encode          Run vault-to-agent encoding
  cortex status          Show basic health of Cortex
   cortex memory search   Search encoded memory
   cortex memory get      Fetch a single note by id
   cortex memory list     List all notes with type, tier, alias
   cortex memory write    Write a memory note to the vault
   cortex memory delete   Delete a memory note from the vault
  cortex import          Import existing agent context
  cortex version         Print version information
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import typer

from cortex.cli.commands import import_agent as import_agent_module
from cortex.cli.commands import uninstall as uninstall_cmd
from cortex.cli.lint import lint_app
from cortex.encoder.core import (
    cortex_version,
    load_config,
    read_vault_schema,
    run_encode,
    schema_version,
)
from cortex.vault.links import extract_wiki_links, strip_wiki_links

app = typer.Typer(
    name="cortex",
    help="Persistent, tiered memory for AI coding agents",
    no_args_is_help=True,
)
memory_app = typer.Typer(
    name="memory",
    help="Search, write, and delete vault memory",
    no_args_is_help=True,
)
app.add_typer(memory_app, name="memory")
app.add_typer(lint_app, name="lint")

# Platform subcommands — one Typer per platform
platform_apps: dict[str, typer.Typer] = {}
for _pname in ("opencode", "codex", "copilot"):
    _plat = typer.Typer(
        name=_pname, help=f"Manage Cortex integration with {_pname}", no_args_is_help=True
    )
    platform_apps[_pname] = _plat
    app.add_typer(_plat, name=_pname)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _skill_src() -> Path | None:
    """Find the skill template file, wherever the package is installed."""
    # 1. CORTEX_SKILL_DIR env var
    if "CORTEX_SKILL_DIR" in os.environ:
        p = Path(os.environ["CORTEX_SKILL_DIR"]) / "cortex-ai" / "SKILL.md"
        if p.exists():
            return p
    # 2. Repo layout (dev install, git clone)
    p = _REPO_ROOT / "skills" / "cortex-ai" / "SKILL.md"
    if p.exists():
        return p
    # 3. Bundled in the package (uv tool install, pip install)
    try:
        from importlib.resources import files as _files

        return _files("cortex").joinpath("SKILL.md")  # type: ignore[return-value]
    except Exception:
        pass
    return None


def _error(what: str, why: str, fix: str) -> None:
    """Print a structured error message."""
    typer.echo(f"\n  What failed:\n    {what}\n", err=True)
    typer.echo(f"  Why it matters:\n    {why}\n", err=True)
    typer.echo(f"  Suggested fix:\n    {fix}\n", err=True)


def _cortex_config_dir() -> Path:
    return Path.home() / ".config" / "cortex"


def _cortex_pointer() -> Path:
    return _cortex_config_dir() / "config"


def _load_cortex_pointer() -> Path | None:
    """Read the vault path from ~/.config/cortex/config."""
    ptr = _cortex_pointer()
    if not ptr.exists():
        return None
    try:
        text = ptr.read_text(encoding="utf-8").strip()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("vault_path:"):
                raw = line.split(":", 1)[1].strip().strip("\"'")
                p = Path(raw).expanduser()
                if (p / "_sync" / "cortex.yaml").exists():
                    return p
    except Exception:
        pass
    return None


def _save_cortex_pointer(vault_path: Path) -> None:
    """Write the vault path to ~/.config/cortex/config."""
    _cortex_config_dir().mkdir(parents=True, exist_ok=True)
    _cortex_pointer().write_text(f"vault_path: {vault_path}\n", encoding="utf-8")


def _find_vault_path_cortex_yaml(root: Path) -> Path | None:
    """Walk root looking for _sync/cortex.yaml up to 2 levels deep."""
    try:
        for p in root.iterdir():
            if not p.is_dir() or p.name.startswith("."):
                continue
            if (p / "_sync" / "cortex.yaml").exists():
                return p
            for sub in p.iterdir():
                if sub.is_dir() and not sub.name.startswith("."):
                    if (sub / "_sync" / "cortex.yaml").exists():
                        return sub
    except PermissionError:
        pass
    return None


def _find_vault() -> Path:
    """Try to find an existing vault by scanning home for _sync/cortex.yaml."""
    vp = _find_vault_path(None)
    if vp:
        return vp
    typer.echo(
        "  WARNING: No vault found — defaulting to current directory. "
        "Pass --vault to specify your vault path.",
        err=True,
    )
    return Path.cwd()


def _backup_file(src: Path, backup_dir: Path) -> Path | None:
    """Snapshot a file or directory into backup_dir. Returns the backup path,
    or None if the source didn't exist."""
    if not src.exists():
        return None
    flat = str(src).replace(str(Path.home()), "~").replace("/", "_").lstrip("_")
    dst = backup_dir / flat
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)
    return dst


def _record_action(actions_file: Path, op: str, path: str, saved_as: str = "") -> None:
    entry = {"op": op, "path": path}
    if saved_as:
        entry["saved_as"] = saved_as
    with open(actions_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _write_manifest(
    manifest_path: Path,
    actions_file: Path,
    vault_root: str,
    repo_root: str,
) -> None:
    actions = []
    if actions_file.exists():
        for line in actions_file.read_text().splitlines():
            line = line.strip()
            if line:
                actions.append(json.loads(line))
        actions_file.unlink()
    if not actions:
        manifest_path.parent.rmdir() if not any(manifest_path.parent.iterdir()) else None
        return
    doc = {
        "cortex_version": cortex_version(),
        "schema_version": schema_version(),
        "created": datetime.now().isoformat(),
        "vault_root": vault_root,
        "repo_root": repo_root,
        "actions": actions,
    }
    manifest_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


@app.command()
def bootstrap(
    repo_root: str | None = typer.Argument(
        None,
        help="Repo root (default: auto-detect)",
    ),
) -> None:
    """Create a venv and install Python dependencies.

    This is the chicken-and-egg command: run it before `cortex install` if you
    don't have a venv yet. Safe to re-run (idempotent).
    """
    root = Path(repo_root) if repo_root else _REPO_ROOT
    venv_dir = root / ".venv"

    typer.echo("==> Cortex bootstrap")
    typer.echo(f"    repo: {root}")

    # Check python3
    if not shutil.which("python3"):
        _error(
            "python3 not found on PATH",
            "Cortex requires Python 3.10+ to create a venv and run the encoder.",
            "Install Python 3.10+ and ensure 'python3' is on your PATH.",
        )
        raise typer.Exit(code=1)

    # Create venv
    if not venv_dir.exists():
        typer.echo("    creating venv ...")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    else:
        typer.echo("    venv already exists")

    python = venv_dir / "bin" / "python"

    # Upgrade pip
    typer.echo("    upgrading pip ...")
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        check=True,
    )

    # Install deps
    req = root / "requirements.txt"
    if req.exists():
        typer.echo("    installing dependencies ...")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet", "-r", str(req)],
            check=True,
        )
    else:
        typer.echo("    installing package ...")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet", "-e", str(root)],
            check=True,
        )

    typer.echo("\nBootstrap complete. Activate with:")
    typer.echo(f"  source {venv_dir}/bin/activate")
    typer.echo("  cortex install")


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


@app.command()
def install(
    vault: str | None = typer.Argument(
        None,
        help="Vault path (default: auto-detect or prompt)",
    ),
    upgrade: bool = typer.Option(
        False,
        "--upgrade",
        help="Upgrade an existing install",
    ),
    no_encode: bool = typer.Option(
        False,
        "--no-encode",
        help="Skip re-encoding after install/upgrade",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would change without writing anything",
    ),
) -> None:
    """Bootstrap or upgrade Cortex for a user.

    Handles everything: venv deps, config generation, skill installation,
    backup, and encoding.
    """
    repo_root = _REPO_ROOT

    if not vault:
        vault = typer.prompt("Vault path", default=str(_find_vault()))
    vault_path = Path(vault).expanduser()

    if not vault_path.exists():
        _error(
            f"Vault path does not exist: {vault_path}",
            "The vault directory must exist before Cortex can install into it.",
            f"Create the directory first: mkdir -p {vault_path}",
        )
        raise typer.Exit(code=1)

    vault_path = vault_path.resolve()
    sync_dir = vault_path / "_sync"
    encoded_dir = sync_dir / "encoded"
    memory_json = encoded_dir / "memory.json"
    core_context = encoded_dir / "core-context.md"
    projects_dir = encoded_dir / "projects"
    config_file = sync_dir / "cortex.yaml"
    opencode_skills_dir = Path.home() / ".config" / "opencode" / "skills"
    claude_skills_dir = Path.home() / ".claude" / "skills"

    # Install manifest — records everything this run creates/modifies so
    # `cortex uninstall` can cleanly revert.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    manifest_dir = sync_dir / "backups" / f"{stamp}-{'upgrade' if upgrade else 'install'}"
    actions_file = manifest_dir / ".actions.jsonl"
    if not dry_run:
        manifest_dir.mkdir(parents=True, exist_ok=True)
        actions_file.write_text("")

    rel_ver = cortex_version()
    schema_ver = schema_version()
    mode = "DRY-RUN" if dry_run else "APPLY"
    verb = "upgrade" if upgrade else "install"
    typer.echo(f"==> Cortex {verb} [{mode}] (v{rel_ver}, schema v{schema_ver})")
    typer.echo(f"    vault: {vault_path}")

    # ---- Version guard (upgrade only) ----
    if upgrade:
        live_schema = read_vault_schema(vault_path)
        if live_schema is not None and live_schema > schema_ver:
            _error(
                f"Live vault schema (v{live_schema}) is NEWER than this code (v{schema_ver})",
                "Downgrading the schema would risk data loss in your vault.",
                "Update Cortex code to at least v{live_schema}, or back up and recreate the vault.",
            )
            raise typer.Exit(code=1)

    python = sys.executable

    # ---- [1/4] Ensure venv deps ----
    typer.echo("\n==> [1/4] Dependencies")
    venv_dir = repo_root / ".venv"
    venv_python = venv_dir / "bin" / "python"
    if venv_python.exists():
        typer.echo("    venv deps already installed")
    elif not dry_run:
        typer.echo("    venv not found — run 'cortex bootstrap' first, or")
        typer.echo("    installing deps inline ...")
        subprocess.run([python, "-m", "venv", str(venv_dir)], check=True)
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
            check=True,
        )
        req = repo_root / "requirements.txt"
        if req.exists():
            subprocess.run(
                [str(venv_python), "-m", "pip", "install", "--quiet", "-r", str(req)],
                check=True,
            )
        typer.echo("    deps installed into .venv")
    else:
        typer.echo("    [DRY] would create venv and install deps")

    # ---- [2/4] Config (only if missing) ----
    typer.echo("\n==> [2/4] Config")
    sync_dir.mkdir(parents=True, exist_ok=True)
    if config_file.exists():
        typer.echo(f"    {config_file} already exists — leaving it untouched")
    else:
        yaml_content = (
            f"# Cortex — generated by cortex install\n"
            f"# Points at: {vault_path}\n"
            f"schema_version: {schema_ver}\n"
            f'vault_path: "{vault_path}"\n'
            f"\n"
            f"eager_tiers:\n"
            f"  - core\n"
            f"\n"
            f"skip_dirs:\n"
            f"  - templates\n"
            f"  - archive\n"
            f"\n"
            f"exclude_tags:\n"
            f"  - draft\n"
            f"  - archived\n"
            f"\n"
            f"vault_only_types:\n"
            f"  - session\n"
            f"  - log\n"
            f"\n"
            f"targets:\n"
            f"  core_context:\n"
            f"    enabled: true\n"
            f"    type: core-context\n"
            f'    output_file: "{core_context}"\n'
            f"\n"
            f"  skills:\n"
            f"    enabled: true\n"
            f"    type: skill-embed\n"
            f'    skills_dir: "{opencode_skills_dir}"\n'
            f"    # Extra dirs to search for skill folders (e.g. Claude Desktop /\n"
            f"    # Orca skills). skills_dir is searched first, then each entry here.\n"
            f"    skill_dirs:\n"
            f'      - "{claude_skills_dir}"\n'
            f'    embed_filename: "reference.md"\n'
            f"\n"
            f"  projects:\n"
            f"    enabled: true\n"
            f"    type: project-context\n"
            f'    output_dir: "{projects_dir}"\n'
            f"\n"
            f"  python-agents:\n"
            f"    enabled: true\n"
            f"    type: json\n"
            f'    output_file: "{memory_json}"\n'
            f"    include_types:\n"
            f"      - knowledge\n"
            f"      - entity\n"
            f"\n"
            f"strip_wiki_links: true\n"
        )
        if dry_run:
            typer.echo(f"    [DRY] would create {config_file}")
        else:
            config_file.write_text(yaml_content, encoding="utf-8")
            _record_action(actions_file, "created", str(config_file))
            typer.echo(f"    wrote {config_file}")

    # ---- Core notes (behavior rules every vault should carry) ----
    from cortex.templates._render import apply_core_notes

    core_created = apply_core_notes(vault_path, dry_run=dry_run)
    if core_created:
        if dry_run:
            typer.echo(f"    [DRY] would write core notes: {', '.join(core_created)}")
        else:
            for rel in core_created:
                _record_action(actions_file, "created", str(vault_path / rel))
            typer.echo(f"    wrote core notes: {', '.join(core_created)}")
    else:
        typer.echo("    core notes already present — skipped")

    # ---- [3/4] Skill + first encode ----
    typer.echo("\n==> [3/4] Skill + encode")
    if not dry_run:
        # Pre-create skill dirs
        for md in vault_path.rglob("*.md"):
            if any(p.startswith((".", "_")) for p in md.relative_to(vault_path).parts):
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            m = re.search(r"^tier:\s*skill:(\S+)", text, re.M)
            if m:
                (opencode_skills_dir / m.group(1)).mkdir(parents=True, exist_ok=True)

    # Install skill
    skill_src = _skill_src()
    if skill_src is not None and skill_src.exists():
        skill_dest_dir = opencode_skills_dir / "cortex-ai"
        skill_dest = skill_dest_dir / "SKILL.md"
        if dry_run:
            typer.echo(f"    [DRY] would install skill -> {skill_dest}")
        else:
            # Backup existing skill
            if skill_dest.exists():
                _backup_file(skill_dest, manifest_dir)
                _record_action(actions_file, "modified", str(skill_dest), "cortex-ai.SKILL.md.bak")
            else:
                _record_action(actions_file, "created", str(skill_dest))
            skill_dest_dir.mkdir(parents=True, exist_ok=True)
            text = skill_src.read_text(encoding="utf-8")
            text = text.replace("<CORTEX_HOME>", str(repo_root))
            skill_dest.write_text(text, encoding="utf-8")
            typer.echo(f"    installed skill -> {skill_dest}")
    else:
        typer.echo(
            f"    WARNING: skill source not found at {skill_src} — skipping",
            err=True,
        )

    # Run encode
    if not dry_run and config_file.exists():
        rc = run_encode(config_path=config_file)
        if rc != 0:
            typer.echo("    WARNING: encode had errors", err=True)

    # ---- [4/4] Re-encode if upgrade ----
    if upgrade and not no_encode and not dry_run:
        typer.echo("\n==> [4/4] Re-encode")
        rc = run_encode(config_path=config_file)
        if rc != 0:
            typer.echo("    WARNING: re-encode had errors", err=True)
        typer.echo("    Re-encoded after upgrade")
    elif not upgrade:
        typer.echo("\n==> [4/4] (skipped — initial install)")

    # ---- Write manifest ----
    if not dry_run:
        _write_manifest(
            manifest_dir / "manifest.json", actions_file, str(vault_path), str(repo_root)
        )
        # Clean up empty backup dir
        if manifest_dir.exists() and not any(
            f for f in manifest_dir.iterdir() if f.name != ".actions.jsonl"
        ):
            actions_file.unlink(missing_ok=True)
            manifest_dir.rmdir()

    # ---- Write pointer file ----
    if not dry_run:
        _save_cortex_pointer(vault_path)
        typer.echo(f"    wrote {_cortex_pointer()}")

    # ---- Done ----
    typer.echo("\n==> Done")
    if upgrade:
        typer.echo("Upgrade complete. Restart your agent to load changes.")
    else:
        typer.echo("\nCortex is installed. Next steps:")
        typer.echo("  cortex status         # verify the install")
        typer.echo("  cortex version        # show version info")
        typer.echo("  cortex import --dry-run  # import existing agent context")
        typer.echo("  cortex encode        # run encoding manually")


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


@app.command()
def uninstall(
    vault: str = typer.Option(
        ...,
        "--vault",
        help="Vault root Cortex was installed against",
        prompt=True,
    ),
    latest: bool = typer.Option(
        True,
        "--latest",
        help="Undo only the most recent manifest",
    ),
    backup: str | None = typer.Option(
        None,
        "--backup",
        help="Undo a specific backup dir name",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually make changes (default is dry-run)",
    ),
    purge: bool = typer.Option(
        False,
        "--purge",
        help="Also delete _sync/encoded (notes are kept)",
    ),
) -> None:
    """Safely revert Cortex-installed assets while preserving user notes."""
    vault_path = Path(vault).expanduser()
    rc = uninstall_cmd.run_uninstall(
        vault=vault_path,
        latest=latest if not backup else False,
        backup=backup,
        apply=apply,
        purge=purge,
    )
    # Clean up pointer file on successful apply
    if rc == 0 and apply:
        ptr = _cortex_pointer()
        if ptr.exists():
            ptr.unlink()
            typer.echo(f"  removed {ptr}")
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# encode
# ---------------------------------------------------------------------------


@app.command()
def encode(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show changes without writing"),
    list_notes: bool = typer.Option(False, "--list", help="List all vault notes with tier/type"),
    show_config: bool = typer.Option(False, "--show-config", help="Print resolved paths as JSON"),
    check: bool = typer.Option(False, "--check", help="Report version/schema health and exit"),
    graph: bool = typer.Option(False, "--graph", help="Output wiki-link graph and exit"),
    purge: bool = typer.Option(
        False,
        "--purge",
        help="Preview deletion of drained log/session notes",
    ),
    purge_apply: bool = typer.Option(
        False,
        "--purge-apply",
        help="Delete drained log/session notes and rebuild",
    ),
    hive_push: bool = typer.Option(False, "--hive-push", help="Push vault notes to hub"),
    hive_pull: bool = typer.Option(False, "--hive-pull", help="Pull vault notes from hub"),
    hive_status: bool = typer.Option(False, "--hive-status", help="Show hive connection status"),
    config_path: str | None = typer.Option(
        None,
        "--config",
        help="Path to cortex.yaml config file",
    ),
) -> None:
    """Run the vault-to-agent encoding process."""
    cfg_path: Path
    if config_path:
        cfg_path = Path(config_path)
    else:
        vault_path = _find_vault_path(None)
        if vault_path:
            cfg_path = vault_path / "_sync" / "cortex.yaml"
        else:
            cfg_path = Path.cwd() / "_sync" / "cortex.yaml"
    rc = run_encode(
        config_path=cfg_path,
        dry_run=dry_run,
        list_only=list_notes,
        show_config_only=show_config,
        check_only=check,
        graph_only=graph,
        purge_only=purge,
        purge_apply=purge_apply,
        hive_push_only=hive_push,
        hive_pull_only=hive_pull,
        hive_status_only=hive_status,
    )
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@app.command()
def status(
    vault: str | None = typer.Option(
        None,
        "--vault",
        help="Vault path (default: auto-detect from config discovery)",
    ),
) -> None:
    """Show basic health of the Cortex installation."""
    vp: Path | None = _find_vault_path(vault)
    config_file: Path | None = None
    vault_path: Path | None = None
    if vp:
        cfg = vp / "_sync" / "cortex.yaml"
        if cfg.exists():
            config_file = cfg
            vault_path = vp

    if config_file and config_file.exists():
        typer.echo("Config:           found")
        typer.echo(f"  path: {config_file}")
        try:
            cfg_data = load_config(config_file)
            vp = Path(cfg_data.get("vault_path", ""))
            typer.echo(f"  vault: {vp}")
            if vp.exists():
                typer.echo("Vault:            found")
            else:
                typer.echo("Vault:            MISSING")
            mem_json = vp / "_sync" / "encoded" / "memory.json"
            if mem_json.exists():
                typer.echo("Encoded memory:   found")
                schema = read_vault_schema(vp)
                code_schema = schema_version()
                if schema is None:
                    typer.echo("Schema:           unknown (fresh vault?)")
                elif schema == code_schema:
                    typer.echo("Schema:           compatible")
                elif schema < code_schema:
                    typer.echo(f"Schema:           migration pending (v{schema} -> v{code_schema})")
                else:
                    typer.echo(
                        "Schema:           ERROR — vault newer than code",
                        err=True,
                    )
            else:
                typer.echo("Encoded memory:   MISSING — run 'cortex encode'")

            # Check opencode config exists
            opencode_cfg = Path.home() / ".config" / "opencode" / "opencode.jsonc"
            if not opencode_cfg.exists():
                opencode_cfg = Path.home() / ".config" / "opencode" / "opencode.json"
            env_cfg = os.environ.get("OPENCODE_CONFIG")
            if env_cfg:
                opencode_cfg = Path(env_cfg)
            if opencode_cfg.exists():
                typer.echo("opencode config:  found")
            else:
                typer.echo("opencode config:  not found")

            typer.echo(
                "\nStatus:           HEALTHY"
                if vp.exists() and mem_json.exists()
                else "\nStatus:           NEEDS ATTENTION"
            )
        except Exception as e:
            _error(
                f"Could not read config: {e}",
                "The config file is present but unreadable, so Cortex cannot "
                "locate the vault or validate the installation.",
                "Check file permissions and ensure cortex.yaml is valid YAML.",
            )
            raise typer.Exit(code=1) from None
    else:
        _error(
            "Config not found",
            "Cortex needs a cortex.yaml config to locate the vault and run encoding.",
            "Run 'cortex install' to set up Cortex.",
        )
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


@app.command(name="import")
def import_cmd(
    vault: str | None = typer.Option(
        None,
        "--vault",
        help="Vault root to import into (default: auto-detect)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would happen without writing anything",
    ),
    agents_md: str | None = typer.Option(None, "--agents-md", help="Path to agents.md"),
    claude_md: str | None = typer.Option(None, "--claude-md", help="Path to CLAUDE.md"),
    opencode: str | None = typer.Option(None, "--opencode", help="Path to opencode.jsonc"),
    claude_memory: str | None = typer.Option(
        None,
        "--claude-memory",
        help="Path to ~/.claude/memory directory",
    ),
    ) -> None:
    """Import existing agent context into the Cortex vault."""
    vault_path = Path(vault).expanduser() if vault else _find_vault()
    if not (vault_path / "_sync" / "cortex.yaml").exists():
        what = f"  Vault path: {vault_path}"
        why = "That directory does not contain a Cortex vault (_sync/cortex.yaml missing)."
        fix = "Pass --vault to point at your real vault, or run 'cortex install' first."
        _error(what, why, fix)
        raise typer.Exit(code=1)

    import_agent_module.run_import(
        vault=vault_path,
        dry_run=dry_run,
        agents_md=agents_md,
        claude_md=claude_md,
        opencode=opencode,
        claude_memory=claude_memory,
    )


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


@app.command()
def version() -> None:
    """Print Cortex version information."""
    typer.echo(f"Cortex:  {cortex_version()}")
    typer.echo(f"Schema:  {schema_version()}")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

config_app = typer.Typer(
    name="config",
    help="Manage local Cortex pointer config",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")


@config_app.command(name="set")
def _config_set(
    vault: str = typer.Argument(
        ...,
        help="Vault path to store as the default",
    ),
) -> None:
    """Set the default vault path in ~/.config/cortex/config."""
    vault_path = Path(vault).expanduser().resolve()
    if not vault_path.exists():
        _error(
            f"Vault path does not exist: {vault_path}",
            "The path must point to an existing directory.",
            "Create the directory first or provide a different path.",
        )
        raise typer.Exit(code=1)
    _save_cortex_pointer(vault_path)
    typer.echo(f"Set default vault to: {vault_path}")


@config_app.command(name="get")
def config_get() -> None:
    """Print the current default vault path."""
    p = _load_cortex_pointer()
    if p:
        typer.echo(str(p))
    else:
        typer.echo("No default vault set.")


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@app.command()
def init(
    vault: str | None = typer.Argument(
        None,
        help="Vault path (default: current directory)",
    ),
    template: str | None = typer.Option(
        None,
        "--template",
        "-t",
        help="Starter template (personal, engineering, product-management, knowledge-base)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing"),
) -> None:
    """Initialize a new Cortex vault with starter notes."""
    from cortex.templates._render import (
        TEMPLATES,
        apply_core_notes,
        apply_template,
        list_templates,
    )

    vault_path = Path(vault) if vault else Path.cwd()

    if template is None:
        typer.echo("Available templates:\n")
        for name, desc in list_templates().items():
            typer.echo(f"  {name:24s} {desc}")
        typer.echo(f"\nUsage: cortex init --template <name> {vault_path}")
        return

    if template not in TEMPLATES:
        _error(
            f"Unknown template: {template!r}",
            f"Available templates: {', '.join(TEMPLATES)}",
            "Run 'cortex init' to see available templates.",
        )
        raise typer.Exit(code=1)

    mode = "DRY-RUN" if dry_run else "APPLY"
    typer.echo(f"==> Cortex init [{mode}] (template: {template})")
    typer.echo(f"    vault: {vault_path}\n")

    core_created = apply_core_notes(vault_path, dry_run=dry_run)
    template_created = apply_template(vault_path, template, dry_run=dry_run)

    typer.echo("  Core notes:")
    if core_created:
        for rel in core_created:
            typer.echo(f"    Created: {rel}")
    else:
        typer.echo("    (already present — skipped)")
    typer.echo(f"    {len(core_created)} file(s) written")

    typer.echo(f"\n  Template: {template}")
    if template_created:
        for rel in template_created:
            typer.echo(f"    Created: {rel}")
    else:
        typer.echo("    (already present — skipped)")
    typer.echo(f"    {len(template_created)} file(s) written")

    typer.echo("\nNext steps:")
    typer.echo(f"  1. Review and customize the notes in {vault_path}")
    typer.echo(f"  2. Run 'cortex install {vault_path}' to set up Cortex")


# ---------------------------------------------------------------------------
# memory helpers
# ---------------------------------------------------------------------------


def _find_memory_json(vault: str | None) -> Path | None:
    """Locate memory.json from --vault option, env var, or auto-detect."""
    if vault:
        vp = Path(vault).expanduser()
        mp = vp / "_sync" / "encoded" / "memory.json"
        if mp.exists():
            return mp
        return mp  # return path even if missing — caller handles
    vp = _find_vault_path(None)
    if vp:
        mp = vp / "_sync" / "encoded" / "memory.json"
        if mp.exists():
            return mp
        return mp
    return None


def _load_memory_json(vault: str | None) -> tuple[dict, Path]:
    """Load and return memory.json data + path. Exits on error."""
    mem_path = _find_memory_json(vault)
    if not mem_path or not mem_path.exists():
        _error(
            "memory.json not found",
            "The encoded memory index is required.",
            "Run 'cortex encode' to generate memory.json from your vault notes.",
        )
        raise typer.Exit(code=1)
    try:
        data = json.loads(mem_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _error(
            f"Could not read memory.json: {e}",
            "The memory index file exists but is unreadable.",
            "Run 'cortex encode' to regenerate memory.json.",
        )
        raise typer.Exit(code=1) from e
    return data, mem_path


def _find_vault_path(vault: str | None) -> Path | None:
    """Locate vault root from --vault option, env var, pointer file, or auto-detect."""
    if vault:
        return Path(vault).expanduser()
    # 1. CORTEX_VAULT env var override
    if "CORTEX_VAULT" in os.environ:
        p = Path(os.environ["CORTEX_VAULT"]).expanduser()
        if (p / "_sync" / "cortex.yaml").exists():
            return p
    # 2. ~/.config/cortex/config pointer file (set by `cortex install` / `cortex config set`)
    p = _load_cortex_pointer()
    if p:
        return p
    # 3. Check cwd
    cwd = Path.cwd()
    if (cwd / "_sync" / "cortex.yaml").exists():
        return cwd
    # 4. Scan home dirs up to 2 levels deep
    return _find_vault_path_cortex_yaml(Path.home())


def _scan_vault_for_note(vault_root: Path, note_id: str) -> Path | None:
    """Walk the vault looking for <note_id>.md in any subdirectory."""
    for md_file in vault_root.rglob(f"{note_id}.md"):
        # skip dot-dirs and _sync
        parts = md_file.relative_to(vault_root).parts
        if any(p.startswith((".", "_")) for p in parts):
            continue
        return md_file
    return None


# Canonical type -> top-level vault directory. English plurals are irregular, so a
# naive `type + "s"` is wrong (e.g. "knowledge" -> "knowledges", "entity" -> "entitys").
# Only listed types get a fixed directory; anything else falls through to the
# generic rule below.
_TYPE_DIR_MAP = {
    "knowledge": "knowledge",  # mass noun — stays singular
    "entity": "entities",
    "feedback": "feedback",  # mass noun — stays singular
    "decision": "decisions",
    "log": "logs",
    "session": "logs",  # sessions live alongside logs
}


def _type_dir_name(note_type: str) -> str:
    """Map a note type to its canonical top-level directory name."""
    if note_type in _TYPE_DIR_MAP:
        return _TYPE_DIR_MAP[note_type]
    # Generic fallback for unknown types: proper English pluralization.
    if note_type.endswith("s"):
        return note_type
    if note_type.endswith("y") and note_type[-2:-1] not in "aeiou":
        return note_type[:-1] + "ies"
    return note_type + "s"


def _resolve_note_path(
    vault_path: Path, note_id: str, note_type: str, category: str | None
) -> tuple[Path, bool]:
    """Resolve where a note lives (for update) or should be created (for new).

    Returns (path, exists). For updates, the note is located anywhere in the vault
    by its id via rglob, so it is patched in place regardless of directory layout.
    For new notes, the path is built as <vault>/<type-dir>[/<category>]/<id>.md.
    """
    existing = _scan_vault_for_note(vault_path, note_id)
    if existing is not None:
        return existing, True

    target_dir = vault_path / _type_dir_name(note_type)
    if category:
        target_dir = target_dir / category
    return target_dir / f"{note_id}.md", False


def _print_note(note_id: str, note: dict) -> None:
    """Pretty-print a note dict from memory.json."""
    typer.echo(f"# {note_id}")
    typer.echo(f"**Type:** {note.get('type', '?')}")
    typer.echo(f"**Category:** {note.get('category', '')}")
    typer.echo(f"**Tier:** {note.get('tier', '')}")
    updated = note.get("updated", "")
    if updated:
        typer.echo(f"**Updated:** {updated}")
    aliases = note.get("aliases", [])
    if aliases:
        typer.echo(f"**Aliases:** {', '.join(aliases)}")
    tags = note.get("tags", [])
    if tags:
        typer.echo(f"**Tags:** {', '.join(tags)}")
    body = note.get("content", "")
    if body:
        typer.echo("")
        typer.echo(body)


def _print_note_from_vault_file(vault_root: Path, note_id: str) -> None:
    """Find and print a note by scanning vault files."""
    file_path = _scan_vault_for_note(vault_root, note_id)
    if file_path is None:
        _error(
            f"Note '{note_id}' not found in vault",
            f"No file named {note_id}.md found in {vault_root}",
            "Check the id for typos, or run 'cortex encode' first.",
        )
        raise typer.Exit(code=1)

    content = file_path.read_text(encoding="utf-8")
    typer.echo(f"# {note_id} (from vault file)")
    typer.echo(f"**Path:** {file_path}")
    typer.echo("")
    typer.echo(content)


# ---------------------------------------------------------------------------
# memory subcommands
# ---------------------------------------------------------------------------


@memory_app.command()
def get(
    note_id: str = typer.Argument(..., help="Note id slug (e.g. 'askdel', 'jira-rest-api')"),
    vault: str | None = typer.Option(None, "--vault", help="Vault path (auto-detect by default)"),
) -> None:
    """Fetch a single memory note by its id from memory.json or vault files."""
    # Find memory.json
    mem_path = _find_memory_json(vault)

    if not mem_path or not mem_path.exists():
        # Fall back to vault file scan
        vault_path = _find_vault_path(vault)
        if vault_path is not None:
            _print_note_from_vault_file(vault_path, note_id)
            return
        _error(
            "memory.json not found and no vault path available",
            "A memory index or vault directory is needed to retrieve a note.",
            "Run 'cortex encode' to generate memory.json, or specify --vault.",
        )
        raise typer.Exit(code=1)

    try:
        data = json.loads(mem_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _error(
            f"Could not read memory.json: {e}",
            "The memory index file exists but is unreadable or contains invalid JSON.",
            "Run 'cortex encode' to regenerate memory.json.",
        )
        raise typer.Exit(code=1) from e

    notes = data.get("notes", {})
    note = notes.get(note_id)
    if note is not None:
        _print_note(note_id, note)
        return

    # Not in memory.json — try vault file fallback
    vault_path = _find_vault_path(vault)
    if vault_path is not None:
        _print_note_from_vault_file(vault_path, note_id)
        return

    _error(
        f"Note '{note_id}' not found",
        "The note was not found in memory.json or the vault directory.",
        "Check the id for typos. Use 'cortex memory search' to find available notes.",
    )
    raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Search scoring helpers (shared by search, think, related)
# ---------------------------------------------------------------------------

_STOP_WORDS_SEARCH = frozenset(
    """a an and are as at be but by for from has have how if in into is it its of on
    or that the their then there these this to was were what when where which who will
    with you your we our not no do does can may""".split()
)


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens of length >= 3, minus stop words."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if len(t) >= 3 and t not in _STOP_WORDS_SEARCH]


def _tokenize_query(query: str) -> list[str]:
    """Tokenize a search query into individual content words."""
    return _tokenize(query)


def _compute_content_idf(notes: dict[str, dict]) -> dict[str, float]:
    """Inverse-document-frequency for content words across all notes.

    Returns ln(N / df) per word. A word appearing in every note weighs ~0;
    a word in 1 of 100 notes weighs ~4.6.
    """
    n = max(len(notes), 1)
    df: dict[str, int] = {}
    for note in notes.values():
        for word in set(_tokenize(note.get("content", ""))):
            df[word] = df.get(word, 0) + 1
    return {w: math.log(n / d) for w, d in df.items()}


def _score_note(
    query_terms: list[str],
    nid: str,
    note: dict,
    idf_cache: dict[str, float] | None = None,
    adjacency: dict[str, list[str]] | None = None,
    top_ids: set[str] | None = None,
) -> int:
    """Score a note against tokenized query terms.

    Per-term scoring (substring match preserved for prefix/abbreviation support):
      id contains term         +10
      alias contains term      +8
      tag exact match term     +6
      tag contains term        +3
      category contains term   +5
      content contains term    round(idf * 1.5) or 1 if idf unavailable

    Post-term bonuses:
      completeness            round(matched/total * 5)
      tier: core +3, skill:* +2, project +0, vault-only -1
      graph boost             +2 if linked to a top-3 result
    """
    total_terms = max(len(query_terms), 1)
    matched_terms = 0
    score = 0

    for term in query_terms:
        term_matched = False
        if term in nid:
            score += 10
            term_matched = True
        for alias in note.get("aliases", []):
            if term in alias.lower():
                score += 8
                term_matched = True
                break
        for tag in note.get("tags", []):
            tag_lower = tag.lower()
            if term == tag_lower:
                score += 6
                term_matched = True
            elif term in tag_lower:
                score += 3
                term_matched = True
        if term in note.get("category", "").lower():
            score += 5
            term_matched = True
        if term in note.get("content", "").lower():
            if idf_cache is not None and term in idf_cache:
                score += round(idf_cache[term] * 1.5)
            else:
                score += 1
            term_matched = True
        if term_matched:
            matched_terms += 1

    if matched_terms == 0:
        return 0

    # Completeness bonus: reward matching more query terms
    score += round(matched_terms / total_terms * 5)

    # Tier bonus
    tier = note.get("tier", "")
    if tier == "core":
        score += 3
    elif tier.startswith("skill:"):
        score += 2
    elif tier == "vault-only":
        score -= 1

    # Graph boost: +2 if linked to a top-3 result
    if adjacency and top_ids and nid not in top_ids:
        neighbors = set()
        for top_id in top_ids:
            neighbors.update(adjacency.get(top_id, []))
        if nid in neighbors:
            score += 2

    return score


def _extract_snippet(content: str, query_terms: list[str], window: int = 80) -> str:
    """Extract a snippet around the first matching query term in content."""
    if not content:
        return ""
    content_lower = content.lower()
    for term in query_terms:
        idx = content_lower.find(term)
        if idx >= 0:
            start = max(0, idx - window // 4)
            end = min(len(content), idx + window * 3 // 4)
            snippet = content[start:end].replace("\n", " ").strip()
            if start > 0:
                snippet = "..." + snippet
            if end < len(content):
                snippet = snippet + "..."
            return snippet
    return content[:80].replace("\n", " ")


@memory_app.command()
def search(
    query: str = typer.Argument(..., help="Search query string"),
    vault: str | None = typer.Option(None, "--vault", help="Vault path (auto-detect by default)"),
) -> None:
    """Search encoded memory from the CLI."""
    mem_path = _find_memory_json(vault)

    if not mem_path or not mem_path.exists():
        _error(
            "memory.json not found",
            "The encoded memory index is required to search notes.",
            "Run 'cortex encode' to generate memory.json from your vault notes.",
        )
        raise typer.Exit(code=1)

    try:
        data = json.loads(mem_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _error(
            f"Could not read memory.json: {e}",
            "The memory index file exists but is unreadable or contains invalid JSON.",
            "Run 'cortex encode' to regenerate memory.json.",
        )
        raise typer.Exit(code=1) from e

    notes = data.get("notes", {})
    query_terms = _tokenize_query(query)
    if not query_terms:
        typer.echo(f"No results for '{query}'")
        return

    idf_cache = _compute_content_idf(notes)
    graph = data.get("_graph", {})
    adjacency = graph.get("adjacency", {})

    # Score all notes
    scored = []
    for nid, note in notes.items():
        s = _score_note(query_terms, nid, note, idf_cache=idf_cache)
        if s > 0:
            scored.append((s, nid, note))

    # Sort by score desc, then recency desc for ties
    scored.sort(key=lambda x: (x[0], x[2].get("updated", "0000-00-00")), reverse=True)

    # Graph boost: +2 for notes wiki-linked to a top-3 result
    if adjacency and scored:
        top_ids = [nid for _s, nid, _n in scored[:3]]
        linked = set()
        for top_id in top_ids:
            linked.update(adjacency.get(top_id, []))
        for i, (s, nid, note) in enumerate(scored):
            if nid in linked:
                scored[i] = (s + 2, nid, note)
        scored.sort(key=lambda x: (x[0], x[2].get("updated", "0000-00-00")), reverse=True)

    if not scored:
        typer.echo(f"No results for '{query}'")
        return

    typer.echo(f"Found {len(scored)} result(s) for '{query}':\n")
    for _score, nid, note in scored[:20]:
        aliases = note.get("aliases", [])
        alias = aliases[0] if aliases else nid
        cat = note.get("category", "")
        snippet = _extract_snippet(note.get("content", ""), query_terms)
        typer.echo(f"  {nid}  · {note.get('type', '?')}/{cat}  · {alias}")
        if snippet:
            typer.echo(f"    {snippet}...")


@memory_app.command(name="list")
def list_notes_cmd(
    tier: str | None = typer.Option(
        None, "--tier", help="Filter by tier (core, skill:*, project, vault-only)"
    ),
    note_type: str | None = typer.Option(
        None, "--type", help="Filter by type (knowledge, entity, feedback, etc.)"
    ),
    vault: str | None = typer.Option(None, "--vault", help="Vault path (auto-detect by default)"),
) -> None:
    """List all notes in encoded memory as a table."""
    mem_path = _find_memory_json(vault)
    if not mem_path or not mem_path.exists():
        _error(
            "memory.json not found",
            "Run 'cortex encode' to build the memory index.",
            "If you have a vault, run: cortex encode",
        )
        raise typer.Exit(code=1)

    try:
        data = json.loads(mem_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _error(f"Could not read memory.json: {e}", "", "")
        raise typer.Exit(code=1) from e

    notes = data.get("notes", {})
    if not notes:
        typer.echo("No notes found in memory.json.")
        return

    # Filter
    items: list[tuple[str, dict]] = []
    for nid, note in sorted(notes.items()):
        if tier and note.get("tier", "") != tier:
            continue
        if note_type and note.get("type", "") != note_type:
            continue
        items.append((nid, note))

    if not items:
        typer.echo("No notes match the filter.")
        return

    typer.echo(f"\n{'ID':<30} {'Type':<14} {'Tier':<16} {'Alias'}")
    typer.echo("-" * 80)
    for nid, note in items:
        aliases = note.get("aliases", [])
        alias = aliases[0] if aliases else ""
        ntype = note.get("type", "?")
        ntier = note.get("tier", "?")
        typer.echo(f"{nid:<30} {ntype:<14} {ntier:<16} {alias}")

    typer.echo(f"\n{len(items)} note(s)")


@memory_app.command()
def write(
    title: str = typer.Option(..., "--title", help="Note title (becomes alias and id)"),
    note_type: str = typer.Option(
        ..., "--type", help="Note type (knowledge, entity, feedback, etc.)"
    ),
    tier: str = typer.Option(..., "--tier", help="Tier (core, skill:<name>, project, vault-only)"),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags"),
    category: str | None = typer.Option(
        None, "--category", help="Category (patterns, api, projects, etc.)"
    ),
    body: str | None = typer.Option(None, "--body", help="Note body content (inline text)"),
    body_file: str | None = typer.Option(
        None, "--body-file", help="Path to file containing note body content"
    ),
    update: bool = typer.Option(
        False, "--update", help="Update an existing note (patch body + bump date)"
    ),
    no_encode: bool = typer.Option(False, "--no-encode", help="Skip automatic encode after write"),
    root: bool = typer.Option(
        False, "--root", help="Write to vault root as a flat file (e.g. Learnings.md)"
    ),
    vault: str | None = typer.Option(None, "--vault", help="Vault path (auto-detect by default)"),
) -> None:
    """Write a memory note to the vault. Without --body/--body-file, writes frontmatter only."""
    # Validate mutually exclusive body options
    if body and body_file:
        _error(
            "Conflicting body options",
            "Both --body and --body-file were provided.",
            "Use one or the other, not both.",
        )
        raise typer.Exit(code=1)

    # Find vault
    vault_path = _find_vault_path(vault)
    if not vault_path or not vault_path.exists():
        _error(
            "Vault not found",
            "A vault directory is required to write notes into.",
            "Run 'cortex install' first, or pass --vault to specify the vault path.",
        )
        raise typer.Exit(code=1)

    note_id = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not note_id:
        note_id = "untitled"

    today = datetime.now().strftime("%Y-%m-%d")

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    if root:
        # Flat file at the vault root (e.g. Learnings.md).
        note_path = vault_path / f"{note_id}.md"
        exists = note_path.exists()
    else:
        # Locate an existing note by id anywhere in the vault; otherwise build the
        # canonical creation path (<type-dir>[/<category>]/<id>.md).
        note_path, exists = _resolve_note_path(vault_path, note_id, note_type, category)

    if exists:
        if not update:
            _error(
                f"Note already exists at {note_path}",
                "Overwriting an existing note would lose its current content.",
                "Use --update to patch the existing note, or use a different --title.",
            )
            raise typer.Exit(code=1)
        # Update mode: patch body and bump date, preserving the note's location.
        body_text = _read_body(body, body_file)
        new_content = _build_note_content(
            note_id, note_type, tier, title, today, category, tag_list, body_text
        )
        note_path.write_text(new_content, encoding="utf-8")
        typer.echo(f"Updated note: {note_path}")
    else:
        if update:
            _error(
                f"Note does not exist (searched vault for id '{note_id}')",
                "--update was passed but no existing note was found.",
                "Remove --update to create a new note, or check the --title value.",
            )
            raise typer.Exit(code=1)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        body_text = _read_body(body, body_file)
        content = _build_note_content(
            note_id, note_type, tier, title, today, category, tag_list, body_text
        )
        note_path.write_text(content, encoding="utf-8")
        typer.echo(f"Created note: {note_path}")

    typer.echo(f"  id: {note_id}")
    typer.echo(f"  type: {note_type}  tier: {tier}")
    if tag_list:
        typer.echo(f"  tags: {tag_list}")
    if body is not None or body_file is not None:
        typer.echo("  body: included")

    if not no_encode:
        _update_memory_json_inline(
            vault_root=vault_path,
            note_id=note_id,
            note_type=note_type,
            tier=tier,
            category=category,
            aliases=[title],
            tags=tag_list,
            body=body_text,
            updated=today,
        )


@memory_app.command()
def delete(
    note_id: str = typer.Argument(..., help="Note id slug to delete (e.g. 'askdel')"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt"
    ),
    no_encode: bool = typer.Option(
        False, "--no-encode", help="Skip pruning memory.json after delete"
    ),
    vault: str | None = typer.Option(None, "--vault", help="Vault path (auto-detect by default)"),
) -> None:
    """Delete a memory note from the vault and prune it from memory.json.

    Locates the note file by its id anywhere in the vault, removes it, and
    (unless --no-encode) drops the entry and any graph edges from memory.json.
    """
    vault_path = _find_vault_path(vault)
    if not vault_path or not vault_path.exists():
        _error(
            "Vault not found",
            "A vault directory is required to delete notes from.",
            "Run 'cortex install' first, or pass --vault to specify the vault path.",
        )
        raise typer.Exit(code=1)

    note_path = _scan_vault_for_note(vault_path, note_id)
    if note_path is None:
        _error(
            f"Note '{note_id}' not found in vault",
            f"No file named {note_id}.md found in {vault_path}",
            "Check the id for typos. Use 'cortex memory search' to find available notes.",
        )
        raise typer.Exit(code=1)

    if not yes:
        confirmed = typer.confirm(f"Delete note '{note_id}' at {note_path}?")
        if not confirmed:
            typer.echo("Aborted. No changes made.")
            raise typer.Exit(code=0)

    note_path.unlink()
    typer.echo(f"Deleted note: {note_path}")
    typer.echo(f"  id: {note_id}")

    if not no_encode:
        _remove_from_memory_json_inline(vault_root=vault_path, note_id=note_id)


@memory_app.command()
def related(
    note_id: str = typer.Argument(..., help="Note id slug to find related notes for"),
    limit: int = typer.Option(5, "--limit", "-n", help="Max related notes to return"),
    vault: str | None = typer.Option(None, "--vault", help="Vault path (auto-detect by default)"),
) -> None:
    """Find notes related to a given note by shared tags, category, and wiki-links."""
    data, _mem_path = _load_memory_json(vault)

    notes = data.get("notes", {})
    note = notes.get(note_id)
    if note is None:
        _error(
            f"Note '{note_id}' not found",
            "The note was not found in memory.json.",
            "Check the id for typos. Use 'cortex memory search' to find available notes.",
        )
        raise typer.Exit(code=1)

    graph = data.get("_graph", {})
    adjacency: dict[str, list[str]] = graph.get("adjacency", {})
    graph_neighbors = set(adjacency.get(note_id, []))

    note_tags = set(note.get("tags", []))
    note_category = note.get("category", "")
    note_type = note.get("type", "")

    scored = []
    for nid, n in notes.items():
        if nid == note_id:
            continue
        score = 0
        if nid in graph_neighbors:
            score += 20
        tag_overlap = len(set(n.get("tags", [])) & note_tags)
        score += tag_overlap * 4
        if n.get("category", "") == note_category:
            score += 3
        if n.get("type", "") == note_type:
            score += 1
        if score > 0:
            scored.append((score, nid, n))

    scored.sort(key=lambda x: -x[0])
    results = scored[:limit]

    if not results:
        typer.echo(f"No related notes found for '{note_id}'.")
        return

    typer.echo(f"Found {len(results)} related note(s) for '{note_id}':\n")
    for score, nid, n in results:
        aliases = n.get("aliases", [])
        alias = aliases[0] if aliases else nid
        snippet = n.get("content", "")[:120].replace("\n", " ").strip()
        typer.echo(f"  {nid}  · {n.get('type', '?')}/{n.get('category', '')}  · {alias}  (score: {score})")
        if snippet:
            typer.echo(f"    {snippet}...")


@memory_app.command(name="think")
def think(
    query: str = typer.Argument(..., help="Question or topic to synthesize context for"),
    limit: int = typer.Option(5, "--limit", "-n", help="Max primary notes to include"),
    vault: str | None = typer.Option(None, "--vault", help="Vault path (auto-detect by default)"),
) -> None:
    """Synthesize a rich context for answering a question from the vault.

    Gathers full content from primary results, cross-references related notes,
    and identifies gaps in coverage.
    """
    data, _mem_path = _load_memory_json(vault)

    notes: dict[str, dict] = data.get("notes", {})
    query_terms = _tokenize_query(query)
    search_limit = max(limit * 2, 10)

    idf_cache = _compute_content_idf(notes)
    graph = data.get("_graph", {})
    adjacency = graph.get("adjacency", {})

    # Phase 1: broad search
    scored = []
    for nid, note in notes.items():
        s = _score_note(query_terms, nid, note, idf_cache=idf_cache)
        if s > 0:
            scored.append((s, nid, note))

    scored.sort(key=lambda x: (x[0], x[2].get("updated", "0000-00-00")), reverse=True)
    primary_results = scored[:search_limit]

    # Phase 2: full content for top results
    source_notes = []
    primary_ids = set()
    for _s, nid, note in primary_results:
        primary_ids.add(nid)
        source_notes.append({
            "id": nid,
            "type": note.get("type", ""),
            "category": note.get("category", ""),
            "aliases": note.get("aliases", []),
            "tags": note.get("tags", []),
            "content": note.get("content", ""),
            "relevance": "primary",
        })

    # Phase 3: cross-references between primary results
    cross_refs = []
    for i in range(len(primary_results)):
        for j in range(i + 1, len(primary_results)):
            _, nid_i, note_i = primary_results[i]
            _, nid_j, note_j = primary_results[j]
            shared = set(note_i.get("tags", [])) & set(note_j.get("tags", []))
            if shared:
                cross_refs.append({
                    "from": nid_i,
                    "to": nid_j,
                    "sharedTags": list(shared),
                })

    # Phase 4: pull in related notes not already in primary results
    related_ids = set()
    for _s, nid, note in primary_results[:3]:
        note_tags_set = set(note.get("tags", []))
        note_category = note.get("category", "")
        note_type = note.get("type", "")
        graph_neighbors = set(adjacency.get(nid, []))

        for rnid, rnote in notes.items():
            if rnid == nid or rnid in primary_ids or rnid in related_ids:
                continue
            rscore = 0
            if rnid in graph_neighbors:
                rscore += 20
            rtag_overlap = len(set(rnote.get("tags", [])) & note_tags_set)
            rscore += rtag_overlap * 4
            if rnote.get("category", "") == note_category:
                rscore += 3
            if rnote.get("type", "") == note_type:
                rscore += 1
            if rscore > 0:
                related_ids.add(rnid)
                source_notes.append({
                    "id": rnid,
                    "type": rnote.get("type", ""),
                    "category": rnote.get("category", ""),
                    "aliases": rnote.get("aliases", []),
                    "tags": rnote.get("tags", []),
                    "content": rnote.get("content", "")[:500],
                    "relevance": "related",
                })

    # Phase 5: gap analysis
    gaps: list[str] = []
    all_types = {n["type"] for n in source_notes}
    if not source_notes:
        gaps.append("No notes found matching this query — the vault has no coverage on this topic.")
    elif len(source_notes) <= 2:
        gaps.append(
            f"Only {len(source_notes)} note(s) found — coverage may be thin. "
            "Consider capturing more context on this topic."
        )
    if len(all_types) == 1 and len(source_notes) > 2:
        gaps.append(
            f'All results are type "{list(all_types)[0]}" — no cross-type perspective '
            "found (e.g. decisions, risks, or entities that relate)."
        )

    now = datetime.now()
    stale_threshold = 90 * 24 * 60 * 60
    stale = [n["id"] for n in source_notes if n["id"] in notes]
    stale_ids = []
    for sid in stale:
        orig = notes.get(sid)
        if orig and orig.get("updated"):
            try:
                updated = datetime.strptime(orig["updated"], "%Y-%m-%d")
                if (now - updated).days > 90:
                    stale_ids.append(sid)
            except (ValueError, TypeError):
                pass
    if stale_ids:
        gaps.append(
            f"{len(stale_ids)} note(s) haven't been updated in 90+ days: "
            f"{', '.join(stale_ids)}. Information may be outdated."
        )

    # Output
    primary = [n for n in source_notes if n["relevance"] == "primary"]
    related_n = [n for n in source_notes if n["relevance"] == "related"]

    typer.echo(f"# Synthesis Context for: \"{query}\"\n")

    typer.echo(f"## Primary Sources ({len(primary)})\n")
    for n in primary:
        alias_str = f" (aka {', '.join(n['aliases'])})" if n["aliases"] else ""
        tags_str = f" [{', '.join(n['tags'])}]" if n["tags"] else ""
        typer.echo(f"### {n['id']}{alias_str} — {n['type']}/{n['category']}{tags_str}\n")
        typer.echo(n["content"])
        typer.echo("")

    if related_n:
        typer.echo(f"## Related Context ({len(related_n)})\n")
        for n in related_n:
            tags_str = f" [{', '.join(n['tags'])}]" if n["tags"] else ""
            snippet = n["content"][:300].replace("\n", " ").strip()
            typer.echo(f"- **{n['id']}** ({n['type']}/{n['category']}){tags_str}")
            typer.echo(f"  {snippet}…\n")

    if cross_refs:
        typer.echo("## Cross-References\n")
        for ref in cross_refs:
            typer.echo(f"- {ref['from']} ↔ {ref['to']} (shared: {', '.join(ref['sharedTags'])})")
        typer.echo("")

    if gaps:
        typer.echo("## Gaps in Coverage\n")
        for g in gaps:
            typer.echo(f"- {g}")
        typer.echo("")

    typer.echo("---")
    typer.echo("Use the above context to synthesize a direct, well-cited answer. "
               "Cite note ids (e.g. [[note-id]]) when referencing specific sources. "
               "If gaps exist, explicitly note what the vault doesn't know.")


# Common English stop words filtered out before measuring content overlap, so the
# Jaccard similarity reflects meaningful terms rather than grammatical glue.
_STOP_WORDS = frozenset(
    """a an and are as at be but by for from has have how if in into is it its of on
    or that the their then there these this to was were what when where which who will
    with you your we our not no do does can may""".split()
)


def _content_words(text: str) -> set[str]:
    """Lowercase alphanumeric tokens of length >= 3, minus stop words."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if len(t) >= 3 and t not in _STOP_WORDS}


def _jaccard_words(a: str, b: str) -> float:
    """Jaccard similarity of the meaningful word sets of two note bodies (0.0-1.0)."""
    wa = _content_words(a)
    wb = _content_words(b)
    if not wa or not wb:
        return 0.0
    inter = len(wa & wb)
    union = len(wa | wb)
    return inter / union if union else 0.0


def _tag_idf(notes: dict) -> dict[str, float]:
    """Inverse-document-frequency weight per tag.

    A tag on almost every note (e.g. 'project', 'fy26', 'jira') carries little
    signal about whether two specific notes are duplicates, so it is down-weighted.
    A rare tag shared by two notes is a strong signal. Weight = ln(N / df), so a
    tag appearing on every note weighs ~0 and a tag on 2 of 80 notes weighs ~3.7.
    """
    n = max(len(notes), 1)
    df: dict[str, int] = {}
    for note in notes.values():
        for t in set(note.get("tags", [])):
            df[t] = df.get(t, 0) + 1
    return {t: math.log(n / d) for t, d in df.items()}


def _pair_score(
    nid_a: str,
    a: dict,
    nid_b: str,
    b: dict,
    adjacency: dict[str, list[str]],
    tag_idf: dict[str, float] | None = None,
) -> int:
    """Similarity score between two notes, weighted toward genuine duplication.

    Content overlap dominates because real duplicates say the same thing. Shared
    tags are IDF-weighted so ubiquitous tags ('project', 'jira') barely count while
    rare shared tags matter. Category/type are weak tie-breakers, not bridges.

      wiki-link neighbor  +20
      content overlap     +round(jaccard * 30), capped at 30   (dominant signal)
      shared tags         +round(sum(idf) * 3)                 (IDF-weighted)
      same category       +2
      same type           +1

    Passing tag_idf=None falls back to a flat weight of 1.0 per shared tag (used
    only by unit tests that exercise a single pair in isolation).
    """
    score = 0
    if nid_b in set(adjacency.get(nid_a, [])):
        score += 20

    overlap = _jaccard_words(a.get("content", ""), b.get("content", ""))
    score += min(round(overlap * 30), 30)

    shared_tags = set(a.get("tags", [])) & set(b.get("tags", []))
    if tag_idf is None:
        score += len(shared_tags) * 1
    else:
        score += round(sum(tag_idf.get(t, 0.0) for t in shared_tags) * 3)

    if a.get("category", "") and a.get("category", "") == b.get("category", ""):
        score += 2
    if a.get("type", "") and a.get("type", "") == b.get("type", ""):
        score += 1
    return score


def _cluster_notes(
    ids: list[str],
    pair_scores: dict[tuple[str, str], int],
    threshold: int,
    max_cluster_size: int = 6,
) -> list[list[str]]:
    """Cluster notes from qualifying pairs, guarding against runaway chains.

    Uses single-linkage union-find over pairs scoring >= threshold, but any
    connected component larger than max_cluster_size is treated as noise (a weak
    bridge chaining unrelated notes) and split back into its strongest pairs
    instead of one giant blob. This prevents the whole vault collapsing into a
    single 'cluster' when many notes share a common tag.

    Returns clusters with 2+ members, ordered by input order within each cluster.
    """
    parent: dict[str, str] = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    qualifying = {pair: s for pair, s in pair_scores.items() if s >= threshold}
    for a, b in qualifying:
        union(a, b)

    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)

    clusters: list[list[str]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        if len(members) <= max_cluster_size:
            clusters.append(members)
            continue
        # Runaway component: keep only tight pairs. Emit each qualifying pair whose
        # score is at least 2x the threshold as its own two-note cluster, so we
        # surface the genuinely-similar pairs and drop the weak bridges.
        strong = sorted(
            (
                (s, pair)
                for pair, s in qualifying.items()
                if pair[0] in members and pair[1] in members and s >= threshold * 2
            ),
            reverse=True,
        )
        seen: set[str] = set()
        for _s, (a, b) in strong:
            if a in seen or b in seen:
                continue
            clusters.append([a, b] if ids.index(a) < ids.index(b) else [b, a])
            seen.add(a)
            seen.add(b)
    return clusters


def _slug_root(note_id: str) -> str:
    """Strip a trailing -<digits> or -vN suffix so 'jira-workflow-2026' shares a root
    with 'jira-workflow'. Used only as a contradiction heuristic."""
    return re.sub(r"-(?:v?\d+)$", "", note_id)


def _days_between(a: str, b: str) -> int | None:
    """Absolute day gap between two YYYY-MM-DD strings, or None if unparseable."""
    try:
        da = datetime.strptime(a, "%Y-%m-%d")
        db = datetime.strptime(b, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return abs((da - db).days)


def _flag_conflict(cluster: list[str], notes: dict) -> bool:
    """Heuristic: does this cluster likely contain contradictory/superseding notes?

    Fires if any pair (a) shares slug root, (b) shares category+type with >=3 tag
    overlap, or (c) is on the same topic (>=2 shared tags) but >=90 days apart.
    """
    for i in range(len(cluster)):
        for j in range(i + 1, len(cluster)):
            a, b = notes[cluster[i]], notes[cluster[j]]
            if _slug_root(cluster[i]) == _slug_root(cluster[j]):
                return True
            shared_tags = len(set(a.get("tags", [])) & set(b.get("tags", [])))
            same_cat = a.get("category", "") and a.get("category", "") == b.get("category", "")
            same_type = a.get("type", "") and a.get("type", "") == b.get("type", "")
            if same_cat and same_type and shared_tags >= 3:
                return True
            if shared_tags >= 2:
                gap = _days_between(a.get("updated", ""), b.get("updated", ""))
                if gap is not None and gap >= 90:
                    return True
    return False


def _print_cluster(
    idx: int, cluster: list[str], notes: dict, max_score: int, conflict: bool
) -> None:
    """Render one cluster block for interactive review."""
    marker = "  \u26a0 possible conflict" if conflict else ""
    typer.echo("\u2500" * 60)
    typer.echo(f"Cluster {idx}  [score: {max_score}]{marker}")
    typer.echo("\u2500" * 60)
    for label, nid in zip(_cluster_labels(len(cluster)), cluster, strict=False):
        n = notes[nid]
        tags = ", ".join(n.get("tags", []))
        snippet = n.get("content", "")[:100].replace("\n", " ").strip()
        typer.echo(f"  [{label}] {nid}")
        typer.echo(
            f"      type: {n.get('type', '?')}  tier: {n.get('tier', '?')}  "
            f"updated: {n.get('updated', '?')}"
        )
        if tags:
            typer.echo(f"      tags: {tags}")
        if snippet:
            typer.echo(f'      "{snippet}..."')
    typer.echo("")


def _cluster_labels(n: int) -> list[str]:
    """Letter labels A, B, C, ... for cluster members."""
    return [chr(ord("A") + i) for i in range(n)]


def _merge_cluster(
    vault_path: Path,
    cluster: list[str],
    notes: dict,
) -> tuple[str, list[str]]:
    """Merge every note in the cluster into one. Returns (merged_id, deleted_ids).

    - Body: each source body concatenated with a '---' separator and a provenance line.
    - Tier: the highest-priority tier present (core > project > skill:* > vault-only).
    - Tags: union of all source tags. Category: prompted if sources disagree.
    - Type: the most common type among sources (ties broken by first occurrence).
    """
    typer.echo("\nMerging: " + ", ".join(cluster))
    default_title = notes[cluster[0]].get("aliases", [cluster[0]])[0]
    title = typer.prompt("  Title for merged note", default=default_title)

    tier_rank = {"core": 3, "project": 2, "vault-only": 0}

    def tier_priority(t: str) -> int:
        if t.startswith("skill:"):
            return 1
        return tier_rank.get(t, 0)

    tiers = [notes[nid].get("tier", "vault-only") for nid in cluster]
    merged_tier = max(tiers, key=tier_priority)

    types = [notes[nid].get("type", "knowledge") for nid in cluster]
    merged_type = max(set(types), key=lambda t: (types.count(t), -types.index(t)))

    tag_union: list[str] = []
    for nid in cluster:
        for t in notes[nid].get("tags", []):
            if t not in tag_union:
                tag_union.append(t)

    categories = {
        notes[nid].get("category", "") for nid in cluster if notes[nid].get("category", "")
    }
    if len(categories) <= 1:
        merged_category = next(iter(categories), None)
    else:
        merged_category = typer.prompt(
            f"  Sources have differing categories ({', '.join(sorted(categories))}). Category",
            default=sorted(categories)[0],
        )

    body_parts: list[str] = []
    for nid in cluster:
        body_parts.append(f"<!-- merged from {nid} -->\n{notes[nid].get('content', '').strip()}")
    merged_body = "\n\n---\n\n".join(body_parts)

    merged_id = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "merged-note"
    today = datetime.now().strftime("%Y-%m-%d")

    note_path, exists = _resolve_note_path(vault_path, merged_id, merged_type, merged_category)
    content = _build_note_content(
        merged_id, merged_type, merged_tier, title, today, merged_category, tag_union, merged_body
    )
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(content, encoding="utf-8")
    _update_memory_json_inline(
        vault_root=vault_path,
        note_id=merged_id,
        note_type=merged_type,
        tier=merged_tier,
        category=merged_category,
        aliases=[title],
        tags=tag_union,
        body=merged_body,
        updated=today,
    )
    typer.echo(f"  Created merged note: {note_path}")

    # Delete the originals (except when a source shares the merged id — the merge
    # already overwrote it in place).
    deleted: list[str] = []
    for nid in cluster:
        if nid == merged_id:
            continue
        src = _scan_vault_for_note(vault_path, nid)
        if src is not None:
            src.unlink()
            _remove_from_memory_json_inline(vault_root=vault_path, note_id=nid)
            deleted.append(nid)
            typer.echo(f"  Deleted original: {nid}")
    return merged_id, deleted


def _delete_note(vault_path: Path, note_id: str) -> bool:
    """Delete a single note by id. Returns True if removed."""
    src = _scan_vault_for_note(vault_path, note_id)
    if src is None:
        typer.echo(f"  (skip) {note_id} not found on disk")
        return False
    src.unlink()
    _remove_from_memory_json_inline(vault_root=vault_path, note_id=note_id)
    typer.echo(f"  Deleted: {note_id}")
    return True


@memory_app.command()
def compact(
    threshold: int = typer.Option(
        8, "--threshold", "-t", help="Minimum similarity score to form a cluster"
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Max clusters to present"),
    max_cluster_size: int = typer.Option(
        6, "--max-cluster-size", help="Components larger than this are split into strong pairs"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show clusters, make no changes"),
    include_vault_only: bool = typer.Option(
        False, "--include-vault-only", help="Also sweep vault-only and drained notes"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Non-interactive: report clusters, skip all (no writes)"
    ),
    vault: str | None = typer.Option(None, "--vault", help="Vault path (auto-detect by default)"),
) -> None:
    """Analyze the vault for overlapping or contradictory notes and resolve them.

    Clusters notes by similarity (wiki-links, shared tags, category/type, content
    overlap), flags likely contradictions, and lets you merge, delete, or keep each
    cluster. Runs 'cortex encode' at the end unless nothing changed or --dry-run.
    """
    vault_path = _find_vault_path(vault)
    if not vault_path or not vault_path.exists():
        _error(
            "Vault not found",
            "A vault directory is required to compact notes.",
            "Run 'cortex install' first, or pass --vault to specify the vault path.",
        )
        raise typer.Exit(code=1)

    data, _mem_path = _load_memory_json(vault)
    notes: dict = dict(data.get("notes", {}))

    if include_vault_only:
        # memory.json omits vault-only and drained notes; sweep the raw vault to
        # include them in the analysis.
        from cortex.encoder.core import scan_vault
        from cortex.vault.links import strip_wiki_links

        for vn in scan_vault(vault_path):
            if vn.name in notes:
                continue
            notes[vn.name] = {
                "id": vn.name,
                "type": vn.note_type,
                "category": vn.category,
                "tier": vn.tier,
                "tags": vn.tags,
                "updated": vn.updated,
                "aliases": vn.aliases,
                "content": strip_wiki_links(vn.body),
            }

    if len(notes) < 2:
        typer.echo("Fewer than 2 notes available — nothing to compact.")
        return

    adjacency: dict[str, list[str]] = data.get("_graph", {}).get("adjacency", {})
    tag_idf = _tag_idf(notes)

    # Phase: score every unique pair.
    ids = list(notes.keys())
    pair_scores: dict[tuple[str, str], int] = {}
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a_id, b_id = ids[i], ids[j]
            score = _pair_score(a_id, notes[a_id], b_id, notes[b_id], adjacency, tag_idf)
            if score >= threshold:
                pair_scores[(a_id, b_id)] = score

    clusters = _cluster_notes(ids, pair_scores, threshold, max_cluster_size)
    if not clusters:
        typer.echo(
            f"No overlapping clusters found at threshold {threshold}. Vault looks tidy."
        )
        return

    def cluster_max_score(cluster: list[str]) -> int:
        best = 0
        for i in range(len(cluster)):
            for j in range(i + 1, len(cluster)):
                key = (cluster[i], cluster[j])
                rev = (cluster[j], cluster[i])
                best = max(best, pair_scores.get(key, pair_scores.get(rev, 0)))
        return best

    clusters.sort(key=cluster_max_score, reverse=True)
    clusters = clusters[:limit]

    typer.echo(
        f"Found {len(clusters)} cluster(s) of related notes "
        f"(threshold {threshold}).\n"
    )

    merged_count = 0
    deleted_count = 0
    skipped_count = 0
    changed = False

    for idx, cluster in enumerate(clusters, start=1):
        conflict = _flag_conflict(cluster, notes)
        _print_cluster(idx, cluster, notes, cluster_max_score(cluster), conflict)

        if dry_run or yes:
            skipped_count += 1
            continue

        labels = _cluster_labels(len(cluster))
        label_map = dict(zip(labels, cluster, strict=False))
        del_opts = " ".join(f"(d{lbl}) delete {lbl}" for lbl in labels)
        typer.echo(
            f"  (m) merge all    (k) keep all/skip    {del_opts}\n"
            f"  (s) show full content    (q) quit"
        )

        while True:
            choice = typer.prompt("  >").strip().lower()
            if choice == "q":
                typer.echo("\nQuitting compaction.")
                _finish_compact(
                    vault_path, changed, dry_run, merged_count, deleted_count, skipped_count
                )
                return
            if choice == "k":
                skipped_count += 1
                break
            if choice == "s":
                for lbl, nid in label_map.items():
                    typer.echo(f"\n  ===== [{lbl}] {nid} =====")
                    typer.echo(notes[nid].get("content", "(empty)"))
                typer.echo("")
                continue
            if choice == "m":
                _merged_id, deleted = _merge_cluster(vault_path, cluster, notes)
                merged_count += 1
                deleted_count += len(deleted)
                changed = True
                break
            if choice.startswith("d") and len(choice) == 2 and choice[1].upper() in label_map:
                nid = label_map[choice[1].upper()]
                if _delete_note(vault_path, nid):
                    deleted_count += 1
                    changed = True
                break
            typer.echo("  Unrecognized option. Choose m / k / d<letter> / s / q.")

    _finish_compact(vault_path, changed, dry_run, merged_count, deleted_count, skipped_count)


def _finish_compact(
    vault_path: Path,
    changed: bool,
    dry_run: bool,
    merged: int,
    deleted: int,
    skipped: int,
) -> None:
    """Print the compaction summary and run a full encode if anything changed."""
    typer.echo(
        f"\nCompact complete: {merged} merged, {deleted} deleted, {skipped} skipped."
    )
    if dry_run:
        typer.echo("(dry run — no changes written)")
        return
    if not changed:
        return
    config_path = vault_path / "_sync" / "cortex.yaml"
    if not config_path.exists():
        typer.echo(
            "Note: cortex.yaml not found; skipping encode. "
            "Run 'cortex encode' manually to regenerate distilled outputs."
        )
        return
    typer.echo("Running cortex encode...")
    run_encode(config_path=config_path)


def _read_body(body: str | None, body_file: str | None) -> str:
    """Read body content from inline text or file."""
    if body is not None:
        return body
    if body_file is not None:
        bf = Path(body_file).expanduser()
        if not bf.exists():
            _error(
                f"Body file not found: {bf}",
                "The --body-file path does not exist.",
                "Check the path and try again.",
            )
            raise typer.Exit(code=1)
        return bf.read_text(encoding="utf-8")
    return ""


def _split_frontmatter(content: str) -> tuple[str, str]:
    """Split markdown content into frontmatter and body."""
    parts = content.split("---", 2)
    if len(parts) >= 3:
        return parts[0] + "---" + parts[1] + "---\n", parts[2]
    return "", content


def _build_note_content(
    note_id: str,
    note_type: str,
    tier: str,
    title: str,
    today: str,
    category: str | None,
    tag_list: list[str],
    body_text: str,
) -> str:
    """Build markdown content with YAML frontmatter and optional body."""
    parts = [
        "---",
        f"id: {note_id}",
        f"type: {note_type}",
        f'tier: "{tier}"',
        f'aliases: ["{title}"]',
        f'updated: "{today}"',
    ]
    if category:
        parts.append(f'category: "{category}"')
    if tag_list:
        parts.append(f"tags: [{', '.join(tag_list)}]")
    parts.append("---")
    parts.append("")
    if body_text:
        parts.append(body_text)
        if not body_text.endswith("\n"):
            parts.append("")
    return "\n".join(parts)


def _touch_memory_meta(meta: dict) -> None:
    """Stamp memory.json freshness metadata after an inline write or delete.

    Two distinct facts, previously conflated in one field:

    ``generated``        when this file was last written, by any path.
    ``last_full_encode`` when it was last fully rebuilt from the vault.

    The inline paths only ever patched ``_meta.count``, so before this,
    ``generated`` silently meant "last full encode" — it read stale while the
    index was in fact current, which is the dangerous direction for a freshness
    signal: it teaches you to ignore the field.

    Keeping both makes the state legible. Equal values mean the index is fully
    reconciled with the vault. ``generated`` newer than ``last_full_encode``
    means inline patches have landed since the last rebuild.

    Backfill is exact, not a guess: a file written before this distinction
    existed never had ``generated`` touched by the inline paths, so its stored
    value *is* the last full encode. Files with no prior value at all get "now",
    which is the least wrong answer available.
    """
    previous = meta.get("generated")
    now = datetime.now().isoformat()
    meta["generated"] = now
    meta.setdefault("last_full_encode", previous or now)


def _update_memory_json_inline(
    vault_root: Path,
    note_id: str,
    note_type: str,
    tier: str,
    category: str | None,
    aliases: list[str],
    tags: list[str],
    body: str,
    updated: str,
) -> None:
    """Inline update of memory.json after a write — no full encode needed.

    Upserts the note and patches _graph.adjacency from [[wiki-links]] in the body.
    """
    mem_path = vault_root / "_sync" / "encoded" / "memory.json"
    if not mem_path.exists():
        return

    try:
        data = json.loads(mem_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    notes = data.setdefault("notes", {})
    graph = data.setdefault("_graph", {})
    adjacency = graph.setdefault("adjacency", {})

    # Build note dict matching VaultNote.to_dict format
    note_entry = {
        "id": note_id,
        "type": note_type,
        "category": category or "",
        "tier": tier,
        "tags": tags or [],
        "updated": updated,
        "aliases": aliases or [],
        "content": strip_wiki_links(body or ""),
    }
    notes[note_id] = note_entry

    # Update meta count and freshness
    meta = data.setdefault("_meta", {})
    meta["count"] = len(notes)
    _touch_memory_meta(meta)

    # Parse [[wiki-links]] from raw body
    targets = set(extract_wiki_links(body or ""))

    # Remove stale reverse edges for this note
    for key in list(adjacency):
        adjacency[key] = [n for n in adjacency[key] if n != note_id]
        if not adjacency[key]:
            del adjacency[key]

    # Set new forward edges
    if targets:
        adjacency[note_id] = list(targets)
    elif note_id in adjacency:
        del adjacency[note_id]

    # Set new reverse edges
    for target in targets:
        if target not in adjacency:
            adjacency[target] = []
        if note_id not in adjacency[target]:
            adjacency[target].append(note_id)

    # Update edges list if present
    if "edges" in graph:
        graph["edges"] = [
            e for e in graph["edges"]
            if e["source"] != note_id and e["target"] != note_id
        ]
        for target in targets:
            graph["edges"].append({"source": note_id, "target": target})

    mem_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _remove_from_memory_json_inline(vault_root: Path, note_id: str) -> None:
    """Inline removal of a note from memory.json after a delete — no full encode needed.

    Drops the note entry and prunes any _graph edges referencing it (both forward
    and reverse), mirroring the upsert logic in _update_memory_json_inline.
    """
    mem_path = vault_root / "_sync" / "encoded" / "memory.json"
    if not mem_path.exists():
        return

    try:
        data = json.loads(mem_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    notes = data.setdefault("notes", {})
    if note_id not in notes:
        # Nothing indexed for this id; still prune any dangling graph edges below.
        pass
    else:
        del notes[note_id]

    # Update meta count and freshness
    meta = data.setdefault("_meta", {})
    meta["count"] = len(notes)
    _touch_memory_meta(meta)

    graph = data.setdefault("_graph", {})
    adjacency = graph.setdefault("adjacency", {})

    # Drop this note's forward edges.
    adjacency.pop(note_id, None)

    # Drop reverse edges pointing at this note.
    for key in list(adjacency):
        adjacency[key] = [n for n in adjacency[key] if n != note_id]
        if not adjacency[key]:
            del adjacency[key]

    # Prune the edges list if present.
    if "edges" in graph:
        graph["edges"] = [
            e for e in graph["edges"] if e["source"] != note_id and e["target"] != note_id
        ]

    mem_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Platform commands: cortex <platform> install/uninstall/status
# ---------------------------------------------------------------------------


def _build_context(vault: str | None, dry_run: bool = False):
    """Build an InstallContext from CLI args."""
    from cortex.platforms.base import InstallContext

    vault_path = Path(vault) if vault else _find_vault()
    config_path = vault_path / "_sync" / "cortex.yaml"
    skills_dir = Path.home() / ".config" / "opencode" / "skills"
    return InstallContext(
        repo_root=_REPO_ROOT,
        vault_root=vault_path,
        config_path=config_path,
        skills_dir=skills_dir,
        dry_run=dry_run,
    )


def _add_platform_commands(platform_name: str) -> None:
    """Register install/uninstall/status commands on a platform's Typer app."""
    from cortex.platforms.registry import get_installer

    plat_app = platform_apps[platform_name]
    installer = get_installer(platform_name)
    if installer is None:
        return

    @plat_app.command(name="install")
    def _install(
        vault: str | None = typer.Option(None, "--vault", help="Vault path"),
        dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing"),
    ) -> None:
        ctx = _build_context(vault, dry_run)
        mode = "DRY-RUN" if dry_run else "APPLY"
        typer.echo(f"==> {installer.platform_name} install [{mode}]")
        if not installer.detect():
            typer.echo(
                f"WARNING: {installer.platform_name} config not detected at expected location",
                err=True,
            )
        result = installer.install(ctx)
        typer.echo(result.summary(dry_run))
        if not dry_run and result.changed:
            typer.echo(f"\nInstalled Cortex for {installer.platform_name}.")
        elif dry_run and result.changed:
            typer.echo(f"\nWould install Cortex for {installer.platform_name}.")

    @plat_app.command(name="uninstall")
    def _uninstall(
        vault: str | None = typer.Option(None, "--vault", help="Vault path"),
        dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing"),
    ) -> None:
        ctx = _build_context(vault, dry_run)
        mode = "DRY-RUN" if dry_run else "APPLY"
        typer.echo(f"==> {installer.platform_name} uninstall [{mode}]")
        result = installer.uninstall(ctx)
        typer.echo(result.summary(dry_run))
        if result.changed:
            typer.echo(f"\nRemoved Cortex assets for {installer.platform_name}.")

    @plat_app.command(name="status")
    def _status(
        vault: str | None = typer.Option(None, "--vault", help="Vault path"),
    ) -> None:
        ctx = _build_context(vault)
        detected = installer.detect()
        errors = installer.validate(ctx)
        typer.echo(f"Platform: {installer.platform_name}")
        typer.echo(f"Detected: {'yes' if detected else 'no'}")
        if errors:
            for e in errors:
                typer.echo(f"  ✗ {e}")
            typer.echo("Status: NEEDS ATTENTION")
        else:
            typer.echo("Status: HEALTHY")


for _pname in platform_apps:
    _add_platform_commands(_pname)


# ---------------------------------------------------------------------------
# cortex doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor(
    platform: str | None = typer.Option(None, "--platform", help="Check a specific platform only"),
    vault: str | None = typer.Option(None, "--vault", help="Vault path"),
) -> None:
    """Validate Cortex installation across all platforms."""
    from cortex.platforms.registry import get_installer, list_platforms

    vault_path = Path(vault) if vault else _find_vault()
    typer.echo("Cortex doctor\n")

    all_healthy = True

    # ---- Core section ----
    typer.echo("Core")

    # Python version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 10)
    typer.echo(f"  {'✓' if py_ok else '✗'} Python {py_ver} (>= 3.10)")
    if not py_ok:
        all_healthy = False

    # Config
    config_path = vault_path / "_sync" / "cortex.yaml"
    cfg_ok = config_path.exists()
    typer.echo(f"  {'✓' if cfg_ok else '✗'} Config found at {config_path}")
    if not cfg_ok:
        typer.echo("    Suggested fix: Run 'cortex install' to set up Cortex")
        all_healthy = False

    # Vault writable
    vault_ok = vault_path.exists() and os.access(vault_path, os.W_OK)
    vault_label = "writable" if vault_ok else "not writable"
    typer.echo(f"  {'✓' if vault_ok else '✗'} Vault {vault_label} at {vault_path}")
    if not vault_ok:
        typer.echo("    Suggested fix: Ensure the vault directory exists and is writable")
        all_healthy = False

    # Schema compatibility
    if cfg_ok:
        live_schema = read_vault_schema(vault_path)
        code_schema = schema_version()
        if live_schema is None:
            typer.echo("  ✓ Schema v? (fresh vault, no prior schema)")
        elif live_schema == code_schema:
            typer.echo(f"  ✓ Schema v{live_schema} (compatible)")
        elif live_schema < code_schema:
            typer.echo(f"  ⚠ Schema v{live_schema} (migration pending → v{code_schema})")
        else:
            typer.echo(f"  ✗ Schema v{live_schema} > code v{code_schema} (vault newer than code)")
            all_healthy = False
    else:
        typer.echo("  ○ Schema (skipped — config not found)")

    # Encoded memory
    mem_json = vault_path / "_sync" / "encoded" / "memory.json"
    if mem_json.exists():
        # Check freshness
        mtime = mem_json.stat().st_mtime
        age_seconds = datetime.now().timestamp() - mtime
        age_days = age_seconds / 86400
        if age_days < 1:
            age_str = f"{int(age_seconds / 3600)}h ago"
        elif age_days < 2:
            age_str = "1 day ago"
        else:
            age_str = f"{int(age_days)} days ago"
        fresh = age_days <= 7
        mem_label = "current" if fresh else "stale"
        typer.echo(f"  {'✓' if fresh else '⚠'} Encoded memory {mem_label} (encoded {age_str})")
        if not fresh:
            typer.echo("    Suggested fix: Run 'cortex encode' to refresh memory")
    else:
        typer.echo("  ✗ Encoded memory not found")
        typer.echo("    Suggested fix: Run 'cortex encode'")
        all_healthy = False

    # Memory file validity
    if mem_json.exists():
        try:
            data = json.loads(mem_json.read_text(encoding="utf-8"))
            note_count = len(data.get("notes", {}))
            typer.echo(f"  ✓ Memory file valid ({note_count} notes)")
        except (OSError, json.JSONDecodeError) as e:
            typer.echo(f"  ✗ Memory file invalid: {e}")
            typer.echo("    Suggested fix: Run 'cortex encode' to regenerate")
            all_healthy = False
    else:
        typer.echo("  ○ Memory file (skipped — encoded memory not found)")

    # Skill installed
    skill_file = Path.home() / ".config" / "opencode" / "skills" / "cortex-ai" / "SKILL.md"
    skill_ok = skill_file.exists()
    typer.echo(f"  {'✓' if skill_ok else '✗'} Skill installed")
    if not skill_ok:
        typer.echo("    Suggested fix: Run 'cortex install' to install the skill")

    # ---- Platform section ----
    platforms_to_check = list_platforms()
    if platform:
        inst = get_installer(platform)
        if inst is None:
            typer.echo(f"\nUnknown platform: {platform}")
            typer.echo(f"Available: {', '.join(p.platform_name for p in list_platforms())}")
            raise typer.Exit(code=1)
        platforms_to_check = [inst]

    for inst in platforms_to_check:
        typer.echo(f"\n{inst.platform_name.title()}")
        detected = inst.detect()
        typer.echo(f"  {'✓' if detected else '○'} Detected")
        if detected:
            ctx = _build_context(vault)
            errors = inst.validate(ctx)
            if errors:
                for err in errors:
                    typer.echo(f"  ✗ {err}")
                all_healthy = False
            else:
                typer.echo("  ✓ Skill installed")

    # ---- Summary ----
    typer.echo(f"\nStatus: {'HEALTHY' if all_healthy else 'NEEDS ATTENTION'}")
    if not all_healthy:
        typer.echo("Run 'cortex install' to fix issues.")


# ---------------------------------------------------------------------------
# cortex upgrade
# ---------------------------------------------------------------------------


@app.command()
def upgrade(
    vault: str | None = typer.Option(None, "--vault", help="Vault path override"),
    apply: bool = typer.Option(False, "--apply", help="Apply changes (default is preview)"),
) -> None:
    """Re-encode memory and refresh Cortex assets.

    Default is dry-run (preview only). Pass --apply to make changes.
    """
    vault_path = Path(vault) if vault else _find_vault()
    sync_dir = vault_path / "_sync"
    config_file = sync_dir / "cortex.yaml"

    if not vault_path.exists():
        _error(
            f"Vault path does not exist: {vault_path}",
            "Cannot upgrade without a valid vault directory.",
            "Run 'cortex install' to set up a new vault.",
        )
        raise typer.Exit(code=1)

    if not config_file.exists():
        _error(
            f"Config not found at {config_file}",
            "Cannot upgrade without a cortex.yaml config.",
            "Run 'cortex install' to create the config.",
        )
        raise typer.Exit(code=1)

    # Schema check
    live_schema = read_vault_schema(vault_path)
    code_schema = schema_version()
    schema_changed = live_schema is not None and live_schema != code_schema

    # Backup directory
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = sync_dir / "backups" / f"{stamp}-upgrade"

    typer.echo(f"Cortex upgrade{' preview' if not apply else ''}\n".rstrip())

    # Backup
    if not apply:
        typer.echo(f"  Would backup: {sync_dir}")
    else:
        backup_dir.mkdir(parents=True, exist_ok=True)
        # Backup the encoded directory and config
        for fname in ["cortex.yaml"]:
            _backup_file(sync_dir / fname, backup_dir)
        encoded = sync_dir / "encoded"
        if encoded.exists():
            for item in encoded.iterdir():
                _backup_file(item, backup_dir / "encoded")
        typer.echo(f"  Backed up: {backup_dir}")

    # Schema
    if live_schema is None:
        typer.echo("  Schema: unknown (fresh vault)")
    elif schema_changed:
        typer.echo(f"  Schema: {live_schema} -> {code_schema} (change detected)")
        if not apply:
            typer.echo(
                "  Migration will run automatically during re-encode "
                "(a _sync/ backup is taken first). See docs/migration.md."
            )
        else:
            typer.echo(
                "  Migrating during re-encode (a _sync/ backup is taken first). "
                "See docs/migration.md."
            )
    else:
        typer.echo(f"  Schema: {code_schema} (unchanged)")

    # Re-encode
    if not apply:
        typer.echo("  Would re-encode memory")
    else:
        rc = run_encode(config_path=config_file)
        if rc != 0:
            typer.echo("  WARNING: encode had errors", err=True)
        else:
            typer.echo("  Re-encoded memory")

    typer.echo("")
    if not apply:
        typer.echo("Run 'cortex upgrade --apply' to apply changes.")
    else:
        typer.echo("Upgrade complete. Restart your agent to load changes.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app()


if __name__ == "__main__":
    main()
