# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro uninstall — remove everything okuro put on the system.
# index: imports | def uninstall | def _remove_services | def _unregister_mcp | def _list_instruction_files | def _maybe_delete_data_dir
# AGENT_HEADER_END -->
"""okuro uninstall — roll back everything onboarding did.

Designed so you can run: onboard → test → uninstall → reinstall → test again
without accumulated state between runs. Leaves the pip-installed package
itself alone — that's the user's last step (``pip uninstall okuro``) and we
don't self-delete our own process.

Flags:
  --keep-data       preserve ~/.okuro/ (keyring, DB, design profiles)
  --dry-run         print what would happen, change nothing
  --yes             skip the "delete ~/.okuro/?" confirmation (CI)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import click

from .output import console, fail, heading, info, ok, warn
from okuro.db.engine import okuro_home


@click.command()
@click.option("--keep-data", is_flag=True, help="Preserve ~/.okuro/ (secrets, memory, config).")
@click.option("--dry-run", is_flag=True, help="Print the plan; don't change anything.")
@click.option("--yes", "assume_yes", is_flag=True, help="Skip confirmation on data-dir deletion.")
def uninstall(keep_data: bool, dry_run: bool, assume_yes: bool) -> None:
    """Remove okuro services, MCP registrations, and (optionally) your data dir.

    Handy after a test run. Reinstall with ``./install.sh`` or ``okuro init``.
    """
    from okuro import __version__

    console.print(f"\n  [bold]Okuro v{__version__}[/bold] — uninstall\n")

    heading("Services")
    _remove_services(dry_run)

    heading("MCP registrations")
    _unregister_mcp(dry_run)

    heading("Instruction files")
    _list_instruction_files(dry_run)

    heading("Linger (Linux only)")
    _disable_linger(dry_run)

    heading("CLI symlink + PATH block")
    _remove_cli_symlink_and_path(dry_run)

    heading("Data directory")
    _maybe_delete_data_dir(dry_run, keep_data, assume_yes)

    console.print()
    if dry_run:
        info("Dry run complete — nothing changed.")
    else:
        ok("Okuro has been removed from this system.")
        info("Final step: run [bold]pip uninstall okuro[/bold] to remove the package itself.")
    console.print()


# ── services ───────────────────────────────────────────────────────────


def _remove_services(dry_run: bool) -> None:
    try:
        from okuro.system.service_manager import get_service_manager
        from .cmd_service import get_okuro_service_registry
    except Exception as exc:  # noqa: BLE001
        warn(f"Service manager unavailable: {exc}")
        return

    try:
        mgr = get_service_manager()
    except (NotImplementedError, RuntimeError) as exc:
        info(f"Service manager not applicable on this platform ({exc})")
        return

    registry = get_okuro_service_registry()
    for name in registry:
        try:
            status = mgr.status(name)
        except Exception as exc:  # noqa: BLE001
            # status() fails on oddball systemd configurations (e.g. user-
            # session not running, WSL without systemd, podman rootless).
            # Treat as "unknown state, try to uninstall anyway".
            info(f"{name}: status probe failed ({exc}) — attempting removal anyway")
            status = {"state": "unknown"}

        present = status.get("state") not in ("not-found", None)
        if not present and not status.get("active"):
            info(f"{name}: not installed")
            continue

        if dry_run:
            info(f"{name}: would stop + disable + uninstall")
            continue

        # Each step is best-effort and logged separately — a half-installed
        # service (e.g. plist file written but never loaded) must still
        # uninstall cleanly rather than abort the loop on the first error.
        step_notes: list[str] = []
        try:
            mgr.stop(name)
        except Exception as exc:  # noqa: BLE001
            step_notes.append(f"stop skipped ({exc})")
        try:
            mgr.disable(name)
        except Exception as exc:  # noqa: BLE001
            step_notes.append(f"disable skipped ({exc})")
        try:
            mgr.uninstall(name)
            if step_notes:
                ok(f"{name}: removed — " + "; ".join(step_notes))
            else:
                ok(f"{name}: removed")
        except Exception as exc:  # noqa: BLE001
            warn(f"{name}: uninstall failed — {exc}")
            for note in step_notes:
                info(f"  ↳ {note}")


# ── mcp configs ────────────────────────────────────────────────────────


def _unregister_mcp(dry_run: bool) -> None:
    try:
        from .mcp_config import unregister_all_configs, SERVERS
    except Exception as exc:  # noqa: BLE001
        warn(f"MCP config module unavailable: {exc}")
        return

    if dry_run:
        info(f"Would remove okuro entries ({', '.join(SERVERS.keys())}) from every provider config.")
        return

    results = unregister_all_configs()
    for provider, count in results.items():
        if provider.startswith("_") and provider.endswith("_error"):
            warn(f"{provider[1:-6]}: {count}")
            continue
        if count == -1:
            warn(f"{provider}: error (see above)")
        elif count == 0:
            info(f"{provider}: already clean")
        else:
            ok(f"{provider}: removed {count} okuro entr{'y' if count == 1 else 'ies'}")


# ── instruction files ──────────────────────────────────────────────────


def _list_instruction_files(dry_run: bool) -> None:
    """Instruction files (CLAUDE.md, AGENTS.md, …) may mix okuro content with
    the user's own. Rather than destructively rewriting them, we LIST them so
    the user can review and delete the ones they want gone. Safer default.
    """
    home = Path.home()
    candidates = [
        home / ".claude" / "CLAUDE.md",
        home / ".codex" / "AGENTS.md",
        home / ".gemini" / "GEMINI.md",
        home / ".cursor" / "rules" / "okuro.mdc",
        home / ".okuro" / "TOOL-PROTOCOL.md",
    ]

    # TOOL-PROTOCOL.md is pure okuro — safe to auto-delete.
    tp = home / ".okuro" / "TOOL-PROTOCOL.md"
    if tp.exists():
        if dry_run:
            info(f"Would delete {tp}")
        else:
            try:
                tp.unlink()
                ok(f"deleted {tp}")
            except Exception as exc:  # noqa: BLE001
                warn(f"could not delete {tp}: {exc}")

    # For CLAUDE.md / AGENTS.md / GEMINI.md — list so user decides.
    remaining = [p for p in candidates if p != tp and p.exists()]
    if not remaining:
        info("No provider instruction files found.")
        return

    console.print("  [dim]These files were written by okuro but may contain your own edits — review and delete manually if you want a clean slate:[/dim]")
    for p in remaining:
        console.print(f"    [bold]{p}[/bold]")


# ── linger ─────────────────────────────────────────────────────────────


def _remove_cli_symlink_and_path(dry_run: bool) -> None:
    """Remove the ~/.local/bin/okuro symlink + the PATH block install.sh
    appends to the user's shell rc file.

    Symlink: only delete if it's a SYMLINK pointing into an okuro venv —
    refuses to remove a real file with the same name (foreign binary).

    PATH block: install.sh writes a fenced block:
        # >>> okuro PATH >>>
        ...
        # <<< okuro PATH <<<
    in ~/.zshrc / ~/.bash_profile / ~/.bashrc / ~/.profile (whichever the
    user's shell uses). Strip it from each by deleting the lines between
    those markers. Idempotent — no markers, no change.
    """
    home = Path.home()

    # 1. Symlink
    for link_dir in (home / ".local" / "bin", home / "bin"):
        link = link_dir / "okuro"
        if link.is_symlink():
            target = ""
            try:
                target = str(link.readlink())
            except OSError:
                pass
            if "okuro" in target:
                if dry_run:
                    info(f"Would remove symlink {link} -> {target}")
                else:
                    try:
                        link.unlink()
                        ok(f"removed symlink {link}")
                    except Exception as exc:  # noqa: BLE001
                        warn(f"could not remove {link}: {exc}")
            else:
                info(f"preserving {link} (target {target!r} doesn't look like okuro)")
        elif link.exists():
            info(f"{link} exists but is not a symlink — leaving alone")

    # 2. PATH block in shell rc files
    BEGIN = "# >>> okuro PATH >>>"
    END = "# <<< okuro PATH <<<"
    rc_files = [
        home / ".zshrc",
        home / ".bash_profile",
        home / ".bashrc",
        home / ".profile",
    ]
    for rc in rc_files:
        if not rc.is_file():
            continue
        try:
            text = rc.read_text()
        except Exception as exc:  # noqa: BLE001
            warn(f"could not read {rc}: {exc}")
            continue
        if BEGIN not in text:
            continue
        if dry_run:
            info(f"Would strip okuro PATH block from {rc}")
            continue
        # Drop everything between the markers (inclusive). Other content stays.
        out_lines = []
        skipping = False
        for line in text.splitlines():
            if line.strip() == BEGIN:
                skipping = True
                continue
            if line.strip() == END:
                skipping = False
                continue
            if skipping:
                continue
            out_lines.append(line)
        try:
            rc.write_text("\n".join(out_lines).rstrip() + "\n")
            ok(f"stripped okuro PATH block from {rc}")
        except Exception as exc:  # noqa: BLE001
            warn(f"could not write {rc}: {exc}")


def _disable_linger(dry_run: bool) -> None:
    if not shutil.which("loginctl"):
        info("Not a systemd environment (no loginctl) — skip.")
        return
    import getpass
    user = getpass.getuser()
    probe = subprocess.run(
        ["loginctl", "show-user", user, "--property=Linger"],
        capture_output=True,
        text=True,
    )
    if "Linger=yes" not in probe.stdout:
        info("Linger already disabled.")
        return
    if dry_run:
        info("Would run: loginctl disable-linger " + user)
        return
    for argv in (["loginctl", "disable-linger", user], ["sudo", "-n", "loginctl", "disable-linger", user]):
        if argv[0] == "sudo" and not shutil.which("sudo"):
            continue
        r = subprocess.run(argv, capture_output=True, text=True)
        if r.returncode == 0:
            ok("Linger disabled.")
            return
    warn(f"Could not auto-disable linger. Run manually: sudo loginctl disable-linger {user}")


# ── data dir ───────────────────────────────────────────────────────────


def _maybe_delete_data_dir(dry_run: bool, keep_data: bool, assume_yes: bool) -> None:
    data_dir = okuro_home()
    if not data_dir.exists():
        info(f"{data_dir} does not exist.")
        return

    if keep_data:
        info(f"{data_dir} preserved (--keep-data).")
        return

    # Calculate size so the user knows what they're deleting.
    try:
        size_mb = sum(f.stat().st_size for f in data_dir.rglob("*") if f.is_file()) / 1024 / 1024
        size_str = f"{size_mb:.1f} MB"
    except Exception:
        size_str = "unknown size"

    if dry_run:
        info(f"Would delete {data_dir} ({size_str})")
        return

    if not assume_yes:
        console.print(
            f"\n  [yellow]About to delete {data_dir} ({size_str}).[/yellow]\n"
            "  This removes your keyring, memory, progress logs, and cached models.\n"
            "  Use --keep-data to preserve it, or --yes to skip this prompt.\n"
        )
        if not click.confirm("  Delete it?", default=False):
            info("Data directory preserved.")
            return

    try:
        shutil.rmtree(data_dir)
        ok(f"deleted {data_dir}")
    except Exception as exc:  # noqa: BLE001
        fail(f"could not delete {data_dir}: {exc}")
