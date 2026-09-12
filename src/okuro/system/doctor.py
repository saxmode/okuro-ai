# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Shared health-check registry — single source of truth for CLI + web doctor.
# index:
#   imports
#   class CheckResult
#   def _check_python
#   def _check_database
#   def _check_keyring
#   def _check_git_safety
#   def _check_gpu
#   def _check_disk_smart
#   def _check_embeddings
#   def _check_cortex
#   def _check_providers
#   def _check_design
#   def _check_roles
#   def _check_services
#   def _check_instructions_deployed
#   def _check_canon_deploy_consent
#   def _check_telemetry_write_health
#   def _check_mcp_responding
#   def _check_subagent_artifact_health
#   def _check_os_keychain_reachable
#   def _check_probe_health
#   CHECKS
#   def run_all
#   def to_dict
# AGENT_HEADER_END -->
"""Shared health-check registry.

Before H10 the CLI doctor (``okuro.cli.cmd_doctor``) and the web endpoint
(``okuro.web.app:api_doctor``) re-implemented the same checks in parallel.
The web version was out of sync — it had 7 of 9 checks, missing keyring
and cortex. Any new probe (services, orchestrator health, telemetry size)
would have drifted further.

Single source of truth now lives here. Both callers iterate ``CHECKS``.

Each check is a ``Callable[[], CheckResult]`` — no arguments, no side
effects on the caller's process. The web endpoint wraps ``run_all`` in
``asyncio.to_thread`` so blocking probes (DB migrate, embed HTTP, cortex
stats) don't stall the event loop.

Audit ref: 06-stability.md HIGH-4.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from typing import Callable, Literal, Optional
from okuro.db.engine import okuro_home

CheckStatus = Literal["ok", "warn", "fail"]


@dataclass
class CheckResult:
    """Outcome of a single probe.

    ``category`` groups related checks for optional filtering in the UI
    layer. Kept loose (free-form string) so new checks can introduce a
    category without migrating every existing one.
    """

    name: str
    status: CheckStatus
    message: str
    fix: Optional[str] = None
    category: str = "runtime"


def _check_python() -> CheckResult:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 11):
        return CheckResult("Python", "ok", ver, category="env")
    return CheckResult("Python", "fail", f"{ver} (need >=3.11)", "Install Python 3.11+", "env")


def _check_database() -> CheckResult:
    try:
        from okuro.cli.db_helpers import get_db

        db = get_db()
        try:
            # READ-ONLY: report drift, never apply. A health check must not
            # mutate the schema — migrations are applied explicitly (daemon
            # boot, `okuro init`, `okuro migrate`, and update.sh). Doctor only
            # observes. A clean DB reads ok; genuine drift (services not yet
            # migrated) warns with an action; downgrade / non-allowed gap /
            # corruption is surfaced as fail by _check_migrations + below.
            pending = db.pending_migrations()
        finally:
            db.close()
        if pending:
            return CheckResult(
                "Database",
                "warn",
                f"{len(pending)} migration(s) pending",
                "Restart okuro services, or run: okuro migrate",
                "storage",
            )
        return CheckResult("Database", "ok", "up to date", None, "storage")
    except Exception as e:
        return CheckResult("Database", "fail", str(e), "Run: okuro init", "storage")


def _check_migrations() -> CheckResult:
    """Surface migration-sequence health and version provenance.

    Reports four concerns:
      - schema head (highest applied migration in the DB)
      - whether the DB has been migrated past what this binary knows
      - any non-allowed gap in the on-disk migration sequence
      - distinct code versions that have applied migrations to this DB

    Sprint-1 audit C; see ``okuro.db.sqlite._ALLOWED_GAPS`` for the
    historical 013/014/015 gap that is intentionally tolerated.
    """
    try:
        from okuro.cli.db_helpers import get_db

        db = get_db()
        try:
            health = db.migration_health()
        finally:
            db.close()

        disk_max = health["disk_max"]
        db_max = health["db_max"]
        missing = health["missing"]
        allowed = health["allowed_gaps"]
        newer = health["newer_than_code"]
        versions = sorted(
            {v for v in health["code_versions"].values() if v}
        )

        if newer:
            return CheckResult(
                "Migrations",
                "fail",
                (
                    f"DB applied migration {db_max:03d}, this binary only knows "
                    f"up to {disk_max:03d} — downgrade detected"
                ),
                "Upgrade okuro or restore a backup from before the downgrade",
                "storage",
            )
        if missing:
            return CheckResult(
                "Migrations",
                "fail",
                (
                    "non-contiguous migrations on disk — missing "
                    + ", ".join(f"{n:03d}" for n in missing)
                ),
                (
                    "Add the missing migration files (or register the gap in "
                    "okuro.db.sqlite._ALLOWED_GAPS)"
                ),
                "storage",
            )

        head = f"{db_max:03d}" if db_max is not None else "none"
        ver_msg = ", ".join(versions) if versions else "unknown"
        gap_note = (
            f" (allowed gap: {', '.join(f'{n:03d}' for n in allowed)})"
            if allowed
            else ""
        )
        return CheckResult(
            "Migrations",
            "ok",
            f"head {head}, applied by okuro {ver_msg}{gap_note}",
            None,
            "storage",
        )
    except Exception as e:
        return CheckResult("Migrations", "fail", str(e), "Run: okuro init", "storage")


def _check_keyring() -> CheckResult:
    try:
        from okuro.keyring import KeyringStorage

        store = KeyringStorage()
        if store.is_initialized:
            try:
                count = len(store.list_keys())
                return CheckResult("Keyring", "ok", f"initialized ({count} keys)", None, "storage")
            except ValueError:
                # Password not set but keyring exists
                return CheckResult(
                    "Keyring", "ok", "initialized (password required to list)", None, "storage"
                )
        return CheckResult("Keyring", "warn", "not initialized", "Run: okuro init", "storage")
    except Exception as e:
        return CheckResult("Keyring", "warn", str(e), "Run: okuro init", "storage")


def _check_gpu() -> CheckResult:
    # "No GPU" is the default reality on macOS laptops + most non-gaming Linux
    # installs. Returning warn for it painted fresh installs as DEGRADED in the
    # pulse canvas health dot, which mapped warn → amber DEGRADED. Treat no-GPU
    # as ok with a descriptive message so only actual driver failures escalate.
    try:
        from okuro.system.gpu import get_gpu_status

        status = get_gpu_status()
        gpus = status.get("gpus", [])
        if not gpus:
            return CheckResult("GPU", "ok", "no discrete GPU", None, "hardware")
        names = [g.get("model", g.get("name", "?")) for g in gpus]
        return CheckResult("GPU", "ok", ", ".join(names), None, "hardware")
    except Exception as e:
        return CheckResult("GPU", "warn", f"detection failed: {e}", None, "hardware")


def _check_disk_smart() -> CheckResult:
    # SMART disk health (SATA + NVMe). Reads via smartctl (separate program,
    # no bundled GPL) with an opportunistic sudo -n escalation, or the MIT
    # smartie fallback. Unreadable-without-privilege is reported as ok (like
    # the no-GPU case) so a locked-down host isn't painted DEGRADED — only an
    # actual failing/degrading drive escalates.
    try:
        from okuro.system.smart import get_disk_health

        report = get_disk_health()
        devices = report.get("devices", [])
        if not devices:
            return CheckResult("Disk SMART", "ok", report.get("note", "no disks"), None, "hardware")

        bad = [d for d in devices if d["health"] in ("warn", "fail")]
        if bad:
            worst = "fail" if any(d["health"] == "fail" for d in bad) else "warn"
            headline = "; ".join(
                f"{d.get('model') or d['device']}: {d['findings'][0]['message']}"
                for d in bad
                if d.get("findings")
            )
            fix = " ".join(d["findings"][0]["message"] for d in bad if d.get("findings"))
            return CheckResult("Disk SMART", worst, headline, fix, "hardware")

        readable = [d for d in devices if d["health"] != "unknown"]
        if not readable:
            return CheckResult("Disk SMART", "ok", report.get("note", ""), None, "hardware")
        return CheckResult(
            "Disk SMART", "ok", f"{len(readable)} disk(s) healthy", None, "hardware"
        )
    except Exception as e:
        return CheckResult("Disk SMART", "warn", f"detection failed: {e}", None, "hardware")


def _check_embeddings() -> CheckResult:
    try:
        from okuro.embed import embed_one

        vec = embed_one("test")
        dim = len(vec) if vec else 0
        return CheckResult("Embeddings", "ok", f"working ({dim}d)", None, "ai")
    except Exception as e:
        # The hint must match the install path users actually have. okuro is
        # currently distributed as an editable clone (./install.sh) — telling
        # them to run a non-existent wizard step or `pip install okuro[...]`
        # (no PyPI release yet) is a dead end.
        return CheckResult(
            "Embeddings",
            "warn",
            str(e),
            (
                "Re-run ./install.sh from your okuro clone (it now installs "
                "the embed extras automatically), or from the venv: "
                "`./venv/bin/pip install -e '.[embed-cpu]' "
                "--index-url https://download.pytorch.org/whl/cpu` "
                "(swap embed-cpu → embed-apple on macOS arm64)."
            ),
            "ai",
        )


def _check_cortex() -> CheckResult:
    try:
        from okuro.cortex.vectorstore import VectorStore

        vs = VectorStore()
        stats = vs.get_stats()
        total = stats.get("total_documents", 0)
        files = stats.get("files_indexed", 0)
        return CheckResult(
            "Cortex Index", "ok", f"{total} documents ({files} files indexed)", None, "ai"
        )
    except Exception as e:
        return CheckResult("Cortex Index", "warn", str(e), "Run: okuro init (indexes codebase)", "ai")


def _check_providers() -> CheckResult:
    """Provider summary line for ``okuro doctor``.

    Delegates to ``cli_probe.detect_all`` — the single source of truth
    across wizard / dashboard / bridge / doctor. ``ready`` requires the
    binary on the user's shell PATH AND auth verified via the CLI's own
    status command (or token-expiry parse for CLIs without one). No more
    file-existence proxies that accept stale credentials as valid.
    """
    from okuro.system.cli_probe import detect_all

    states = detect_all()
    ready = [name for name, st in states.items() if st.ready]
    on_path_only = [
        f"{name} ({st.auth.state})"
        for name, st in states.items()
        if st.on_path and not st.ready
    ]

    if ready:
        # Ready providers are the load-bearing signal. Auth-degraded
        # providers (binary present but auth expired/missing) get a
        # parenthetical so the operator knows what to fix without
        # demoting a green install to yellow.
        detail = ", ".join(ready)
        if on_path_only:
            detail += f" · degraded: {', '.join(on_path_only)}"
        return CheckResult("Providers", "ok", detail, None, "ai")

    if on_path_only:
        return CheckResult(
            "Providers",
            "warn",
            f"installed but not authenticated: {', '.join(on_path_only)}",
            "Run the CLI's login command (e.g. `claude /login`)",
            "ai",
        )

    return CheckResult(
        "Providers",
        "warn",
        "no providers found",
        "Install claude-code, cursor, codex, or antigravity",
        "ai",
    )


def _check_design() -> CheckResult:
    """Design systems the engine can open. Counts KITS, not v0 profiles.

    SEVERITY CHANGED WITH THE STORE, and deliberately. A missing v0 profile was
    a `warn` because profiles were user-authored YAML and having none was a
    normal fresh install. A kit store cannot be empty: okuro-ds is shipped in
    the package, so zero means the install is broken, not bare.
    """
    try:
        from okuro.design_engine import store

        kits = store.list_kits()
        if kits:
            return CheckResult(
                "Design Systems", "ok", f"{len(kits)} design systems", None, "ui"
            )
        return CheckResult(
            "Design Systems",
            "fail",
            "no design systems found",
            "okuro-ds ships with the package — reinstall or check the install",
            "ui",
        )
    except Exception as e:
        return CheckResult("Design Systems", "fail", str(e), None, "ui")


def _check_roles() -> CheckResult:
    try:
        from okuro.roles import list_roles

        roles = list_roles()
        if roles:
            return CheckResult("Roles", "ok", f"{len(roles)} roles", None, "ai")
        return CheckResult("Roles", "warn", "no roles loaded", "Run: okuro init", "ai")
    except Exception as e:
        return CheckResult("Roles", "warn", str(e), "Run: okuro init", "ai")


def _check_git_safety() -> CheckResult:
    """Warn if ``~/.okuro/`` lives inside a git working tree that hasn't ignored it.

    Cheap probe (filesystem walk + a single `git check-ignore`). Surfaces
    the same risk the bootstrap packet flags, so users who run
    ``okuro doctor`` after seeing the bootstrap warning get a single
    consistent place to confirm/dismiss it.
    """
    try:
        from okuro.system.security import detect_okuro_home_in_git_tree

        risk = detect_okuro_home_in_git_tree()
        if risk is None:
            return CheckResult(
                "Git safety", "ok", "~/.okuro/ not inside a tracked git tree", None, "storage"
            )
        return CheckResult(
            "Git safety",
            "warn",
            f"~/.okuro/ is inside the git repo at {risk} and is not gitignored",
            f"Add '.okuro/' to {risk}/.gitignore — see SECURITY.md",
            "storage",
        )
    except Exception as e:
        return CheckResult("Git safety", "warn", str(e), None, "storage")


def _check_services() -> CheckResult:
    """Report the runtime state of every registered okuro service.

    An installed-but-dead unit (crashed past the H1 burst cap, linger
    disabled, plist never loaded) is otherwise invisible. This check
    closes that gap on systemd, launchd, and the foreground/nohup
    fallback (audit Sprint 3E #28 — ForegroundManager).
    """
    try:
        from okuro.system.install import get_service_registry
        from okuro.system.service_manager import (
            ForegroundManager,
            get_service_manager,
        )

        registry = get_service_registry()
        if not registry:
            return CheckResult("Services", "ok", "no services registered", None, "runtime")

        try:
            mgr = get_service_manager()
        except (NotImplementedError, RuntimeError) as exc:
            return CheckResult(
                "Services", "warn", f"service manager unavailable: {exc}", None, "runtime"
            )

        # Foreground services are PID-file based — delegate to the
        # dedicated probe so we surface the recipe path on warn.
        if isinstance(mgr, ForegroundManager):
            return _check_foreground_services(mgr, registry)

        active: list[str] = []
        inactive: list[str] = []
        for name in registry:
            try:
                st = mgr.status(name)
            except Exception as exc:
                inactive.append(f"{name} (status error: {exc})")
                continue
            if st.get("active"):
                active.append(name)
            else:
                state = st.get("state") or "inactive"
                inactive.append(f"{name} ({state})")

        total = len(registry)
        if not inactive:
            return CheckResult("Services", "ok", f"{len(active)}/{total} running", None, "runtime")
        msg = f"{len(active)}/{total} running — inactive: {', '.join(inactive)}"
        return CheckResult("Services", "warn", msg, "Start with: okuro service start <name>", "runtime")
    except Exception as e:
        return CheckResult("Services", "warn", str(e), None, "runtime")


def _check_foreground_services(mgr, registry: dict) -> "CheckResult":
    """Foreground/nohup variant of the service health probe.

    On non-systemd Linux (Alpine, WSL1, Devuan, bare Docker) okuro falls
    back to PID-file based service tracking. Status is determined by
    ``signal 0`` against the PID stored in
    ``~/.okuro/foreground/<service>.pid``. Where the systemd path
    surfaces ``systemctl is-active`` state, this one surfaces alive +
    PID + recipe-file location so the user can re-run the recipe.
    """
    active: list[str] = []
    inactive: list[str] = []
    for name in registry:
        try:
            st = mgr.status(name)
        except Exception as exc:  # noqa: BLE001
            inactive.append(f"{name} (status error: {exc})")
            continue
        if st.get("active"):
            active.append(f"{name} (pid {st.get('pid')})")
        else:
            inactive.append(name)

    total = len(registry)
    if not inactive:
        return CheckResult(
            "Services",
            "ok",
            f"{len(active)}/{total} running (foreground/nohup)",
            None,
            "runtime",
        )
    msg = (
        f"{len(active)}/{total} running (foreground/nohup) — "
        f"inactive: {', '.join(inactive)}"
    )
    return CheckResult(
        "Services",
        "warn",
        msg,
        "Start with: bash ~/.okuro/foreground/recipes/<name>.sh",
        "runtime",
    )


# ── Sprint 2F probes (audit findings #29 + #30) ────────────────────────
#
# Six new probes target the highest-impact silent failures the audit
# surfaced. Each runs in well under one second on a typical laptop:
#   - file-stat probes (instructions_deployed, telemetry_write_health,
#     subagent_artifact_health) walk a small fixed set of paths
#   - profile-row probe (canon_deploy_consent) reuses the same JSON the
#     onboarding API already loads on every page hit
#   - registry probe (mcp_responding) reads in-process state — no I/O
#   - keychain probe (os_keychain_reachable) is a single round-trip
#     against the OS credential store


def _check_instructions_deployed() -> CheckResult:
    """Did okuro actually write per-provider instruction files?

    The single most-impactful missing probe per the 2026-04-26 audit
    (finding #29). A user can finish onboarding, skip canon-deploy, and
    end up with zero okuro context inside their CLIs — yet every other
    probe stays green. This one surfaces that gap.

    A file is considered "deployed" only when it exists AND contains the
    okuro signature line — a stale user-authored CLAUDE.md from before
    okuro is not enough.
    """
    try:
        from pathlib import Path

        from okuro.sense.providers import list_providers

        home = Path.home()
        # Provider name -> instruction file path(s) under ``$HOME``.
        targets = {
            "claude": [home / ".claude" / "CLAUDE.md"],
            "codex": [home / ".codex" / "AGENTS.md"],
            "cursor": [home / ".cursor" / "rules" / "okuro.mdc"],
            # NOT a leftover: the Antigravity CLI reuses ~/.gemini as its
            # config dir. The retired gemini provider's GEMINI.md target was
            # dropped on 2026-07-18; this path is load-bearing for agy.
            "antigravity": [home / ".gemini" / "AGENTS.md"],
        }

        adapters = list_providers()
        detected = []
        for adapter in adapters:
            try:
                if adapter.detect():
                    detected.append(adapter.name)
            except Exception:
                continue

        if not detected:
            return CheckResult(
                "Instructions deployed",
                "ok",
                "no providers detected; nothing to deploy",
                None,
                "ai",
            )

        missing: list[str] = []
        deployed: list[str] = []
        for name in detected:
            paths = targets.get(name)
            if not paths:
                continue
            provider_missing: list[str] = []
            try:
                for path in paths:
                    if not path.is_file():
                        provider_missing.append(path.name)
                        continue
                    head = path.read_text(errors="ignore")[:4096]
                    if "# Auto-generated by okuro" not in head:
                        provider_missing.append(f"{path.name}: no okuro signature")
            except OSError:
                provider_missing.append("read error")

            if provider_missing:
                missing.append(f"{name} ({', '.join(provider_missing)})")
            else:
                deployed.append(name)

        if missing:
            return CheckResult(
                "Instructions deployed",
                "warn",
                f"missing for: {', '.join(missing)}",
                "Run okuro init or visit /onboarding/canon-deploy",
                "ai",
            )
        if not deployed:
            return CheckResult(
                "Instructions deployed",
                "ok",
                "no providers with instruction-file support",
                None,
                "ai",
            )
        return CheckResult(
            "Instructions deployed",
            "ok",
            f"{', '.join(deployed)}",
            None,
            "ai",
        )
    except Exception as e:
        return CheckResult("Instructions deployed", "warn", str(e), None, "ai")


def _check_canon_deploy_consent() -> CheckResult:
    """Did the user explicitly choose to skip canon-deploy?

    Pairs with ``_check_instructions_deployed`` to surface audit gap #30
    — silent skip mode. If onboarding completed but ``canon_deploy`` is
    not_done AND the user never recorded consent, the user almost
    certainly hit a wizard skip without realising the consequences.
    """
    try:
        from okuro.orchestrator.api.onboarding import _check_canon_deploy, _load_profile

        profile = _load_profile()
        onboarding = profile.get("onboarding") or {}
        if not isinstance(onboarding, dict):
            onboarding = {}
        completed_at = onboarding.get("completed_at")
        if not completed_at:
            # Onboarding still in progress — nothing to warn about yet.
            return CheckResult(
                "Canon-deploy consent",
                "ok",
                "onboarding not complete",
                None,
                "ai",
            )

        deployed, _ = _check_canon_deploy()
        if deployed:
            return CheckResult(
                "Canon-deploy consent", "ok", "instructions deployed", None, "ai"
            )

        if onboarding.get("canon_deploy_skipped") is True:
            return CheckResult(
                "Canon-deploy consent",
                "ok",
                "skipped with explicit consent",
                None,
                "ai",
            )

        return CheckResult(
            "Canon-deploy consent",
            "warn",
            "onboarding completed without canon-deploy",
            (
                "You skipped canon-deploy; agents have no okuro context. "
                "Visit /onboarding to deploy or run okuro init."
            ),
            "ai",
        )
    except Exception as e:
        return CheckResult("Canon-deploy consent", "warn", str(e), None, "ai")


def _check_telemetry_write_health() -> CheckResult:
    """Can okuro actually append to ``usage.jsonl``?

    The compliance score and orphan-session reaper both read from this
    file — silent write failure (perms, full disk, immutable bit) drives
    bogus low scores and missed cleanups. We pingback a single probe row
    and verify it round-trips.
    """
    try:
        import json
        import time

        from okuro.sense import telemetry as _tel

        path = _tel.USAGE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)

        probe = {
            "type": "doctor_probe",
            "ts": int(time.time() * 1000),
            "tool": "telemetry_write_health",
        }
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(probe) + "\n")
        except OSError as e:
            return CheckResult(
                "Telemetry write",
                "fail",
                f"append failed: {e}",
                "Check ~/.okuro/telemetry/ permissions and free disk space",
                "storage",
            )

        # Verify the row is readable back as valid JSON.
        try:
            with open(path, "rb") as fh:
                # Read just the tail — the file may be huge.
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 4096))
                tail = fh.read().decode("utf-8", errors="ignore")
            last = tail.strip().splitlines()[-1] if tail.strip() else ""
            json.loads(last)
        except (OSError, ValueError, IndexError) as e:
            return CheckResult(
                "Telemetry write",
                "fail",
                f"readback failed: {e}",
                None,
                "storage",
            )

        return CheckResult("Telemetry write", "ok", "append + readback ok", None, "storage")
    except Exception as e:
        return CheckResult("Telemetry write", "fail", str(e), None, "storage")


def _check_mcp_responding() -> CheckResult:
    """Is the MCP registry actually loaded?

    Uses the registry diagnostics added in Sprint 1D. Failed modules
    surface by name; a fully empty registry escalates to fail because no
    okuro tools are reachable from any agent in that case.
    """
    try:
        from okuro.mcp import _registry as reg

        # Audit fix 2026-04-27: previous version called reg._ensure_loaded()
        # which doesn't exist; the AttributeError was swallowed silently and
        # get_loaded_modules() then returned [] → "no MCP modules loaded"
        # fail in every fresh install even though the registry was healthy.
        # The actual loader is _load_all(); call it idempotently. If it
        # legitimately fails, the exception bubbles to the outer except so
        # the user sees the real reason instead of a misleading "no modules".
        if not reg.get_loaded_modules() and not reg.get_failed_modules():
            reg._load_all()

        loaded = reg.get_loaded_modules()
        failed = reg.get_failed_modules()

        # A loaded registry is not a serving server. Build the server the
        # daemon builds: this is where an incompatible `mcp` library shows
        # itself (2026-09-12: mcp 2.2.0 on a fresh install, Server had no
        # list_tools(), the orchestrator crashed, and this check said
        # "responding" because it only counted modules).
        try:
            reg.build_mcp_server()
        except Exception as exc:  # noqa: BLE001 — the message IS the finding
            return CheckResult(
                "MCP responding",
                "fail",
                f"{len(loaded)} modules loaded but the MCP server does not build: "
                f"{type(exc).__name__}: {exc}",
                "Check the installed `mcp` library version against pyproject "
                "(`pip show mcp`); re-run the installer to re-resolve dependencies",
            )

        if failed:
            names = ", ".join(f.get("name", "?") for f in failed)
            return CheckResult(
                "MCP responding",
                "warn",
                f"{len(loaded)} loaded; failed: {names}",
                "Inspect mcp_health tool output",
                "ai",
            )
        if not loaded:
            return CheckResult(
                "MCP responding",
                "fail",
                "no MCP modules loaded",
                "Run: okuro init; check ~/.okuro logs",
                "ai",
            )
        return CheckResult("MCP responding", "ok", f"{len(loaded)} modules", None, "ai")
    except Exception as e:
        return CheckResult("MCP responding", "warn", str(e), None, "ai")


# How many non-recurring task dirs the ghost-completion probe reads per run.
# The bound exists because the probe's cost is O(task dirs) x (task.yaml +
# plan.yaml parse) and the corpus never shrinks. Newest-first, so the bound
# drops history rather than current work.
_GHOST_SCAN_LIMIT = 200


def _check_subagent_artifact_health() -> CheckResult:
    """Walk completed subtasks and verify their artifact files exist + non-empty.

    Catches the ghost-completion class — subtask marked ``done`` but the
    promised ``artifacts/<id>-<name>.md`` is missing or zero-byte. Audit
    finding #12 is the upstream fix; this probe is the user-visible
    surface so the gap stays observable.

    BOUNDED, because unbounded it was the single most expensive thing this
    process did: it loaded and YAML-parsed every task.yaml + plan.yaml in a
    corpus that grows by at least one directory a day, and the whole doctor
    run (which the SPA polls) measured 291.95s because of it.

    Two bounds, and neither erases signal — the excluded counts are reported:

    * ``task-rec-*`` (auto-generated recurring runs) are skipped. Their
      subtasks are machine-made and nobody triages them individually.
    * the remaining non-recurring dirs are scanned newest-first, capped at
      ``_GHOST_SCAN_LIMIT``. Task dirs are named ``task-<YYYYMMDD>-<HHMMSS>``,
      so a reverse NAME sort is already chronological and costs no ``stat``
      per entry — cheaper than the mtime cutoff the reconciler uses, and
      equivalent here.
    """
    try:
        import os
        from pathlib import Path

        # The orchestrator state directory is configurable but defaults to
        # ``~/.okuro/orchestrator/tasks`` (see watchdog.py:176).
        tasks_dir = okuro_home() / "orchestrator" / "tasks"
        if not tasks_dir.exists():
            return CheckResult(
                "Subagent artifacts",
                "ok",
                "no orchestrator tasks yet",
                None,
                "runtime",
            )

        from okuro.orchestrator.state import load_task

        # One scandir, d_type instead of a stat per entry.
        candidates: list[str] = []
        recurring_excluded = 0
        with os.scandir(tasks_dir) as it:
            for entry in it:
                if not entry.name.startswith("task-"):
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.name.startswith("task-rec-"):
                    recurring_excluded += 1
                    continue
                candidates.append(entry.name)
        candidates.sort(reverse=True)
        older_excluded = max(0, len(candidates) - _GHOST_SCAN_LIMIT)
        names = candidates[:_GHOST_SCAN_LIMIT]

        triage = (
            f"scanned the {len(names)} most recent non-recurring task(s); "
            f"excluded {recurring_excluded} task-rec-* (auto-generated "
            f"recurring runs) and {older_excluded} older non-recurring"
        )

        ghosts: list[str] = []
        scanned = 0
        for task_name in names:
            task_dir = tasks_dir / task_name
            try:
                task = load_task(task_dir.name, tasks_dir)
            except Exception:
                continue
            artifacts_root = task_dir / "artifacts"
            for phase in task.phases:
                for st in phase.subtasks:
                    if st.status != "done":
                        continue
                    scanned += 1
                    name = getattr(st, "artifact_name", "") or ""
                    candidates = [
                        artifacts_root / f"{st.id}-{name}.md" if name else None,
                        artifacts_root / f"{st.id}.md",
                    ]
                    candidates = [c for c in candidates if c is not None]
                    found = False
                    for cand in candidates:
                        try:
                            if cand.is_file() and cand.stat().st_size > 0:
                                found = True
                                break
                        except OSError:
                            continue
                    if not found:
                        ghosts.append(f"{task_dir.name}/{st.id}")

        if ghosts:
            preview = ", ".join(ghosts[:5])
            suffix = "" if len(ghosts) <= 5 else f" (+{len(ghosts) - 5} more)"
            return CheckResult(
                "Subagent artifacts",
                "warn",
                f"{len(ghosts)} ghost completion(s): {preview}{suffix} — {triage}",
                "Inspect the listed task IDs; missing artifact = unverified work",
                "runtime",
            )
        return CheckResult(
            "Subagent artifacts",
            "ok",
            f"{scanned} done subtask(s), all backed by artifacts — {triage}",
            None,
            "runtime",
        )
    except Exception as e:
        return CheckResult("Subagent artifacts", "warn", str(e), None, "runtime")


def _check_os_keychain_reachable() -> CheckResult:
    """Is the OS keychain actually reachable?

    Okuro silently falls back to the encrypted file vault (HKDF-SHA256
    bound to the host machine-id) when the OS credential store is
    unavailable — no GNOME keyring on a headless Linux box, locked
    macOS Keychain after lid-close, macOS Tahoe -25308 ACL denial, etc.

    Secrets remain safe in either case. Since the fallback is a
    first-class supported path (not a degraded mode), this check only
    raises 'warn' when the user has neither the OS keychain NOR an
    initialised HKDF vault — in that case there really is no secret
    storage. When HKDF is the active backend we report 'ok' with a note.
    """
    try:
        try:
            import keyring  # type: ignore
        except ImportError:
            return CheckResult(
                "OS keychain",
                "warn",
                "python `keyring` package not installed",
                None,
                "storage",
            )

        service = "okuro-doctor-probe"
        os_keychain_works = False
        os_error: Optional[str] = None
        try:
            keyring.set_password(service, "exists", "1")
            value = keyring.get_password(service, "exists")
            try:
                keyring.delete_password(service, "exists")
            except Exception:
                pass
            os_keychain_works = (value == "1")
            if not os_keychain_works and value is not None:
                os_error = "round-trip mismatch"
        except Exception as e:
            os_error = str(e)

        if os_keychain_works:
            backend = type(keyring.get_keyring()).__name__
            return CheckResult(
                "OS keychain", "ok", f"reachable ({backend})", None, "storage"
            )

        # OS keychain unavailable. Inspect the HKDF fallback — if the vault
        # is initialised and decrypts, secret storage is healthy via the
        # machine-bound path and this is "ok" not "warn".
        try:
            from okuro.keyring import KeyringStorage
            store = KeyringStorage()
            if store.is_initialized:
                # Touch the fernet via _get_fernet() to confirm decryption works.
                store._get_fernet()
                return CheckResult(
                    "OS keychain",
                    "ok",
                    f"file fallback active ({os_error or 'OS keychain unavailable'})",
                    (
                        "OS keychain not used; okuro reads secrets from the "
                        "machine-bound HKDF vault at ~/.okuro/keyring/. "
                        "Secrets are encrypted and bound to this host."
                    ),
                    "storage",
                )
        except Exception:
            pass

        return CheckResult(
            "OS keychain",
            "warn",
            f"unreachable: {os_error or 'unknown'}; HKDF vault not yet initialised",
            (
                "Run `okuro keys init` (or finish the wizard's keyring step) "
                "to create the encrypted file fallback vault."
            ),
            "storage",
        )
    except Exception as e:
        return CheckResult("OS keychain", "warn", str(e), None, "storage")


def _check_probe_health() -> CheckResult:
    """Surface convention-probe rate-limit state.

    Audit Sprint-3F (#31): the probe used to spawn one 90-second background
    subprocess per changed-version provider with no cap, no cooldown, no
    opt-out — three concurrent CLI bumps could trigger 3 simultaneous
    billing-quota hits. The probe module now exposes a snapshot of:

      * ``OKURO_DISABLE_PROBE`` opt-out state
      * lock-file presence + age (mtime > 1h is suspicious)
      * per-provider cooldown + consecutive-failure count

    A stuck lock or a provider stuck in long-failure-backoff bubbles up
    as ``warn`` so the user notices before the next bootstrap session
    silently re-tries.
    """
    try:
        from okuro.sense.providers.probe import probe_health_snapshot

        snap = probe_health_snapshot()
        notes: list[str] = []

        if snap.get("disabled"):
            notes.append("disabled (OKURO_DISABLE_PROBE=1)")

        lock = snap.get("lock") or {}
        warn = False
        if lock.get("stuck"):
            age_min = (lock.get("age_seconds") or 0) // 60
            notes.append(f"stuck lock (age={age_min}m)")
            warn = True
        elif lock.get("held"):
            age_min = (lock.get("age_seconds") or 0) // 60
            notes.append(f"lock held ({age_min}m old)")

        providers = snap.get("providers") or {}
        cooling: list[str] = []
        backoff: list[str] = []
        for pid, pinfo in sorted(providers.items()):
            failures = int(pinfo.get("consecutive_failures", 0) or 0)
            if pinfo.get("in_cooldown"):
                cd_h = (pinfo.get("cooldown_seconds") or 0) // 3600
                cooling.append(f"{pid}({cd_h}h)")
            if failures >= 3:
                backoff.append(f"{pid}={failures}")
                warn = True

        if cooling:
            notes.append("cooldown: " + ", ".join(cooling))
        if backoff:
            notes.append("failures: " + ", ".join(backoff))

        if not notes:
            return CheckResult(
                "Probe health", "ok", "idle, no recent attempts", None, "ai"
            )

        message = "; ".join(notes)
        if warn:
            return CheckResult(
                "Probe health",
                "warn",
                message,
                "Inspect ~/.okuro/cache/probe.lock and probe-attempts.json; "
                "set OKURO_DISABLE_PROBE=1 to disable",
                "ai",
            )
        return CheckResult("Probe health", "ok", message, None, "ai")
    except Exception as e:
        return CheckResult("Probe health", "warn", str(e), None, "ai")


# Ordered so related checks cluster: env → storage → hardware → ai → ui → runtime.
# The CLI prints in list order; the web endpoint preserves the order too.
CHECKS: list[Callable[[], CheckResult]] = [
    _check_python,
    _check_database,
    _check_migrations,
    _check_keyring,
    _check_os_keychain_reachable,
    _check_telemetry_write_health,
    _check_git_safety,
    _check_gpu,
    _check_disk_smart,
    _check_embeddings,
    _check_cortex,
    _check_providers,
    _check_instructions_deployed,
    _check_canon_deploy_consent,
    _check_mcp_responding,
    _check_probe_health,
    _check_design,
    _check_roles,
    _check_services,
    _check_subagent_artifact_health,
]


def run_all() -> list[CheckResult]:
    """Run every registered check in sequence.

    Checks are independent — an exception in one does not short-circuit
    the others. Caller renders the full list.
    """
    results: list[CheckResult] = []
    for fn in CHECKS:
        try:
            results.append(fn())
        except Exception as e:
            # Defence in depth: each check already catches broadly, but
            # a fresh bug should still produce a row instead of crashing
            # the caller.
            results.append(CheckResult(
                name=fn.__name__.removeprefix("_check_").title() or "Unknown",
                status="fail",
                message=str(e),
                fix=None,
                category="runtime",
            ))
    return results


def to_dict(result: CheckResult) -> dict:
    """Serialize a CheckResult to the JSON shape the web endpoint returns."""
    return {
        "name": result.name,
        "status": result.status,
        "message": result.message,
        "fix": result.fix,
        "category": result.category,
    }
