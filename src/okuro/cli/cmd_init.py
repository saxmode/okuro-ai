# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro init — set up everything.
# index:
#   imports
#   def init
#   def _setup_cortex
#   def _detect_environment
#   def _setup_database
#   def _setup_keyring
#   def _register_mcp_servers
#   def _detect_providers
#   def _generate_instruction_files
#   def _install_services
#   def _index_codebase
#   def _migrate_from_tm
# AGENT_HEADER_END -->
"""okuro init — set up everything."""

import sys
from pathlib import Path

import click

from .output import console, ok, warn, fail, heading, info


@click.command()
@click.option("--migrate", is_flag=True, help="Import from existing tm-* installation.")
@click.option("--cli", "cli_mode", is_flag=True, help="Run the text-mode wizard instead of opening a browser.")
@click.option("--non-interactive", is_flag=True, help="CLI mode, accept all defaults without prompting (for CI).")
@click.option("--no-browser", is_flag=True, help="Web mode, but print the URL instead of opening a browser.")
@click.option("--port", default=None, type=int, help="Port to bind the wizard server (default: 13335).")
@click.option("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1 — loopback only).")
def init(migrate, cli_mode, non_interactive, no_browser, port, host):
    """Set up Okuro — opens the web wizard by default.

    Default (recommended): launches a local web server and opens the onboarding
    wizard in your browser.

    Use --cli for a text-only wizard (fewer features, runs in terminal).
    Use --non-interactive for unattended CI install (implies --cli).
    Use --no-browser to print the URL instead of auto-launching your browser
    (useful over SSH — tunnel the port back to your laptop).
    """
    from okuro import __version__

    # Rename the process so `ps` / `top` show "okuro-init" while the
    # wizard is running — matches the orchestrator/daemon/embed
    # setproctitle pattern.
    try:
        import setproctitle
        setproctitle.setproctitle("okuro-init")
    except ImportError:
        pass

    if non_interactive or cli_mode:
        _run_cli_init(non_interactive=non_interactive, migrate=migrate)
        return

    console.print(f"\n  [bold]Okuro v{__version__}[/bold] — launching setup wizard\n")
    _run_web_init(host=host, port=port, no_browser=no_browser)


def _run_web_init(host: str, port: int | None, no_browser: bool) -> None:
    """Launch a transient uvicorn wizard + open the browser at /onboarding."""
    from . import web_launcher

    # Migrations must run before uvicorn starts, otherwise the onboarding
    # endpoints (which read/write user_profile) hit "no such table" errors
    # on a fresh install.
    try:
        from .db_helpers import get_db

        db = get_db()
        applied = db.migrate()
        db.close()
        if applied:
            ok(f"Database: {len(applied)} migration(s) applied")
    except Exception as e:
        fail(f"Database setup failed: {e}")
        info("Try --cli for diagnostic output")
        raise SystemExit(1)

    # The wizard MUST avoid every port the persistent services own:
    # 13333 (orchestrator) and 13334 (embed). If the wizard squats on either
    # of those, the matching plist can't bind at /complete (orchestrator) or
    # at first embed lookup (HTTP 405 from the wizard's FastAPI app, observed
    # 2026-04-28 install report). The port_registry helpers are the single
    # source of truth — never hardcode here.
    from okuro.system.port_registry import (
        wizard_port_preferred,
        wizard_fallback_range,
    )

    chosen_port = port or web_launcher.pick_free_port(
        preferred=wizard_port_preferred(),
        fallback_range=wizard_fallback_range(),
    )
    url = f"http://{host}:{chosen_port}/onboarding"

    info(f"Starting wizard at {url}")
    web_launcher.start_server_thread(host, chosen_port)

    if not web_launcher.wait_for_ready(chosen_port):
        fail("Wizard server did not become ready in 30s")
        info("Try --cli for the text-mode wizard, or run `okuro doctor` to diagnose")
        raise SystemExit(1)

    ok("Ready")

    if no_browser or not web_launcher.has_display():
        console.print(f"\n  Open [bold]{url}[/bold] in your browser\n")
        console.print(f"  [dim]SSH users: tunnel with `ssh -L {chosen_port}:127.0.0.1:{chosen_port} <host>`[/dim]\n")
        console.print(f"  [dim]Press Ctrl-C to stop the wizard server once you're done[/dim]\n")
        web_launcher.serve_until_signal()
    else:
        console.print(f"  [dim]Opening Okuro — close the window to stop the wizard[/dim]\n")
        # private_mode=True for the wizard: prevents WKWebView from
        # restoring back-forward-cache state (including scroll position)
        # on a re-launch after a failed first install attempt. Audit
        # 2026-04-27: this was the macOS-only "wizard opens on page 5"
        # symptom — pywebview restored cached scroll, browser didn't.
        web_launcher.open_in_webview(url, title="Okuro — Setup", private_mode=True)


def _run_cli_init(non_interactive: bool, migrate: bool) -> None:
    """Original text-mode setup — kept for --cli / --non-interactive / CI."""
    from okuro import __version__

    console.print(f"\n  [bold]Okuro v{__version__}[/bold] — Agent Infrastructure Platform (CLI mode)\n")

    # --- Detection ---
    heading("Detected")
    _detect_environment()

    if not non_interactive:
        if not click.confirm("\n  Use recommended defaults?", default=True):
            console.print("  [dim]Custom setup not yet implemented — using defaults[/dim]")

    # --- Database ---
    heading("Database")
    _setup_database()

    # --- Roles ---
    heading("Roles")
    _seed_roles()

    # --- Keyring ---
    heading("Keyring")
    _setup_keyring(non_interactive)

    # --- MCP Registration ---
    heading("MCP Servers")
    _register_mcp_servers()

    # --- Provider Detection ---
    heading("Providers")
    _detect_providers()

    # --- Instruction Files ---
    heading("Instruction Files")
    _generate_instruction_files()

    # --- Cortex Config ---
    heading("Cortex")
    _setup_cortex(non_interactive)

    # --- Background Services ---
    heading("Background Services")
    _install_services()

    # --- Codebase Index ---
    heading("Codebase Index")
    _index_codebase()

    # --- Migration ---
    if migrate:
        heading("Migration")
        _migrate_from_tm()

    # --- Install report ---
    # Mirrors what ``POST /api/onboarding/complete`` writes for the web
    # wizard, so headless / CI installs produce the same diagnostic
    # artifact at ``~/.okuro/install-report.json``. live=False keeps this
    # cheap (no extra subprocess spawns).
    heading("Install Report")
    try:
        from okuro.diagnostics import write_report
        from datetime import datetime, timezone

        report_path = write_report(
            live=False,
            extra={
                "cli_init": True,
                "non_interactive": non_interactive,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        ok(f"{report_path}")
    except Exception as e:  # noqa: BLE001
        warn(f"Install report write failed (non-fatal): {e}")

    console.print(f"\n  [bold green]Done.[/bold green] Run [bold]okuro doctor[/bold] to verify health.\n")


def _setup_cortex(non_interactive: bool):
    """Configure cortex .gitignore preference and explain LLM enrichment."""
    ok("Cortex writes per-directory .okuro-index.yaml sidecars (never mutates source).")

    # .gitignore preference
    cwd = Path.cwd()
    gitignore = cwd / ".gitignore"
    sidecar_pattern = ".okuro-index.yaml"

    already_ignored = False
    if gitignore.exists():
        try:
            already_ignored = sidecar_pattern in gitignore.read_text()
        except OSError:
            pass

    if already_ignored:
        info(f"{sidecar_pattern} already in .gitignore")
    elif non_interactive:
        info(f".gitignore: skipped (run interactively to configure)")
    else:
        info("Sidecar files (.okuro-index.yaml) can be committed or gitignored:")
        info("  [bold]commit[/bold]   — teammates' agents benefit from your index")
        info("  [bold]gitignore[/bold] — each developer generates their own")
        if click.confirm("  Add .okuro-index.yaml to .gitignore?", default=False):
            try:
                with open(gitignore, "a") as f:
                    f.write(f"\n# okuro cortex sidecar metadata\n{sidecar_pattern}\n")
                ok(f"Added {sidecar_pattern} to .gitignore")
            except OSError as e:
                warn(f"Could not update .gitignore: {e}")
        else:
            info("Sidecar files will be committable (team sharing)")

    # LLM enrichment explanation
    info("Cortex uses your cheapest available model to generate purpose")
    info("descriptions for files without good docstrings. This makes every")
    info("file in your project searchable with high-quality descriptions.")
    info("Runs once at setup, then incrementally on changed files only.")


def _detect_environment():
    """Print detected system info."""
    import platform

    info(f"OS ........... {platform.system()} {platform.release()}")
    info(f"Python ....... {platform.python_version()}")

    try:
        from okuro.system.gpu import get_gpu_status
        gpus = get_gpu_status().get("gpus", [])
        if gpus:
            for g in gpus:
                vram = g.get('vram', {}).get('total_mb', '?')
                model = g.get('model', g.get('name', '?'))
                info(f"GPU .......... {model} ({vram} MB)")
        else:
            info("GPU .......... none detected")
    except Exception:
        info("GPU .......... detection failed")

    import os
    providers = []
    for key in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"]:
        if os.environ.get(key):
            providers.append(key.split("_")[0].lower())
    if providers:
        info(f"API Keys ..... {', '.join(providers)}")
    else:
        info("API Keys ..... none in env")


def _setup_database():
    """Initialize database and run migrations."""
    try:
        from .db_helpers import get_db

        db = get_db()
        applied = db.migrate()
        if applied:
            ok(f"Migrations applied: {len(applied)}")
        else:
            ok("Database up to date")
        db.close()
    except Exception as e:
        fail(f"Database setup failed: {e}")


def _seed_roles():
    """Seed the role catalog on fresh install. Idempotent — no-op when the
    roles table already has rows (returning user, tm-* migration, manual
    additions). Roles are a hard dependency for roles_match / role-based
    agent routing; an empty table silently breaks every downstream feature.
    """
    try:
        from .db_helpers import get_db
        from okuro.roles.seed import seed_if_empty

        db = get_db()
        inserted = seed_if_empty(db)
        db.close()
        if inserted:
            ok(f"Seeded {inserted} roles from catalog")
        else:
            ok("Roles already present — no seed needed")
    except Exception as e:  # noqa: BLE001
        warn(f"Role seed skipped: {e}")


def _setup_keyring(non_interactive: bool):
    """Ensure keyring is initialized."""
    try:
        from okuro.keyring import KeyringStorage

        store = KeyringStorage()
        if store.is_initialized:
            ok("Keyring already initialized")
        else:
            if non_interactive:
                warn("Keyring not initialized (run interactively to set password)")
            else:
                password = click.prompt("  Keyring password", hide_input=True, confirmation_prompt=True)
                store.initialize(password)
                ok("Keyring initialized")
    except Exception as e:
        fail(f"Keyring setup failed: {e}")


def _register_mcp_servers():
    """Register MCP servers with detected providers in BOTH the real user
    home (so the user's own CLI sessions see them) AND the scoped agent
    home ``~/.okuro-agent-home/`` (so orchestrator-spawned subagents get an
    isolated tool surface). See packaging audit Gap 1.
    """
    from okuro.keyring import real_user_home

    from .mcp_config import write_all_configs, SERVERS

    # 1. Real user home — user's own CLIs
    results = write_all_configs()

    for provider, (added, updated) in results.items():
        if added or updated:
            ok(f"{provider}: {added} added, {updated} updated")
        else:
            info(f"{provider}: already up to date")

    # 2. Scoped agent home — subagent isolation
    agent_home = real_user_home() / ".okuro-agent-home"
    try:
        agent_home.mkdir(parents=True, exist_ok=True)
        scoped = write_all_configs(base_home=agent_home)
        scoped_added = sum(a for a, _ in scoped.values())
        scoped_updated = sum(u for _, u in scoped.values())
        if scoped_added or scoped_updated:
            ok(f"scoped agent home: {agent_home} ({scoped_added} added, {scoped_updated} updated)")
        else:
            info(f"scoped agent home: {agent_home} (up to date)")
    except Exception as e:
        warn(f"Failed to populate scoped agent home: {e}")

    info(f"{len(SERVERS)} MCP servers across {len(results)} providers")


def _detect_providers():
    """Detect AI providers via the canonical ``cli_probe`` module.

    ``cli_probe.detect_all()`` is the single source of truth across
    wizard / dashboard / bridge / doctor: it asks the user's interactive
    shell where each binary lives (catches every package manager the
    user has set up) and asks the CLI itself for its auth state (no
    file-existence proxies that can lie about expired tokens).

    Output mirrors ``okuro doctor``'s Providers section so init's
    summary and the doctor's summary always agree.
    """
    from okuro.system.cli_probe import detect_all

    states = detect_all()
    for name, st in states.items():
        if st.ready:
            tail = f" [{st.auth.method}]" if st.auth.method else ""
            ok(f"{name} ({st.path}){tail}")
        elif st.on_path:
            # Binary is invokable but auth says no — surface the auth
            # state so the user knows what to do (run `claude /login`,
            # etc.). This is the case the old probe missed: it'd report
            # "found" without checking whether creds were valid.
            warn(f"{name} ({st.path}) — auth: {st.auth.state}: {st.auth.detail or ''}")
        else:
            info(f"{name} — not on PATH")


def _generate_instruction_files():
    """Generate CLAUDE.md, AGENTS.md, etc. for detected providers.

    ``generate_all()`` writes each adapter's file to its canonical home
    (e.g. ``~/.claude/``, ``~/.codex/``) plus the standalone
    ``~/.okuro/TOOL-PROTOCOL.md`` — no output_dir argument, by design.
    """
    try:
        from okuro.sense.providers import generate_all
        generated = generate_all()
        if generated:
            for f in generated:
                ok(f"{Path(f).name}")
        else:
            info("No providers detected — skipped")
    except Exception as e:
        warn(f"Instruction file generation failed: {e}")


def _install_services():
    """Install and enable background services (daemon, embed)."""
    try:
        from okuro.system.service_manager import (
            LinuxServiceManager,
            get_service_manager,
        )
        from .cmd_service import get_okuro_service_registry

        mgr = get_service_manager()
        registry = get_okuro_service_registry()

        if isinstance(mgr, LinuxServiceManager):
            enabled, msg = mgr.ensure_linger()
            if enabled:
                ok(f"Linger: {msg}")
            else:
                warn(f"Linger: {msg}")
                info("Services will stop when you log out until linger is enabled")

        # Remove old timer-based services that the daemon replaces
        for old_name in ["okuro-refresh", "okuro-reminders", "okuro-scheduler",
                         "tm-launcher-refresh", "tm-launcher-maintenance",
                         "tm-reminder", "tm-git-autosave"]:
            try:
                status = mgr.status(old_name)
                if status.get("active") or status.get("state") not in ("not-found", None):
                    mgr.stop(old_name)
                    mgr.disable(old_name)
                    mgr.uninstall(old_name)
                    ok(f"Removed legacy service: {old_name}")
            except Exception:
                pass

        for name, spec in registry.items():
            try:
                mgr.install(spec)
                mgr.enable(name)
                ok(f"{name}: installed and enabled")
            except Exception as e:
                warn(f"{name}: {e}")

        info(f"Start services with: okuro service start <name>")
    except NotImplementedError:
        warn("Service manager not available on this platform")
    except Exception as e:
        warn(f"Service setup failed: {e}")


def _index_codebase():
    """Index current directory for semantic search (headers + vectorstore)."""
    cwd = Path.cwd()

    # 1. AGENT_HEADER scan
    try:
        from okuro.cortex.scanner import scan_directory
        results, updated = scan_directory(cwd)
        if updated > 0:
            ok(f"Headers: {len(results)} files ({updated} updated)")
        else:
            ok(f"Headers up to date ({len(results)} files scanned)")
    except Exception as e:
        warn(f"Header scan skipped: {e}")

    # 2. Vectorstore index for semantic search
    try:
        from okuro.cortex.vectorstore import VectorStore
        vs = VectorStore()
        stats = vs.get_stats()
        doc_count = stats.get("total_documents", 0)
        if doc_count == 0:
            total, indexed = vs.index_directory(cwd)
            ok(f"Vectorstore: indexed {indexed} of {total} files")
        else:
            ok(f"Vectorstore: {doc_count} documents already indexed")
    except Exception as e:
        warn(f"Vectorstore indexing skipped: {e}")


def _migrate_from_tm():
    """Import data from existing tm-* installation."""
    from pathlib import Path

    import os

    from okuro.yu.conventions import get_convention

    configured = os.environ.get("OKURO_TM_ROOT") or get_convention("host.tm_root")
    if not configured:
        warn("No tm-* root configured (set OKURO_TM_ROOT or the host.tm_root convention)")
        return
    tm_root = Path(configured).expanduser()
    if not tm_root.exists():
        warn(f"No tm-* installation found at {tm_root}")
        return

    # Keyring migration
    tm_keyring = Path.home() / ".tm-keyring"
    if tm_keyring.exists():
        ok("Found tm-keyring — import available via: okuro keys import")
    else:
        info("No tm-keyring found")

    # Roles are seeded from the YAML catalog by _seed_roles() during init —
    # --migrate mode doesn't need a separate step.

    # Cortex data
    info("Cortex index: re-index with okuro search --reindex")

    ok("Migration hints complete — run individual import commands above")
