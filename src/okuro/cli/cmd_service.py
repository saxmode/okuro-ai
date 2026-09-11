# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro service — install and control okuro background services.
# index:
#   imports
#   def _mgr_or_exit
#   def _spec_or_exit
#   def service
#   def service_list
#   def service_status
#   def _mgr_or_exit
#   def _spec_or_exit
#   def service_install
#   def service_uninstall
#   def service_start
#   def service_stop
#   def service_restart
#   def service_enable
#   def service_disable
#   def service_run_now
# AGENT_HEADER_END -->
"""okuro service — install and control okuro background services.

Uses ``okuro.system.service_manager`` under the hood; dispatches to
systemd on Linux and launchd on macOS automatically.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click

from okuro.system.service_manager import (
    ServiceSpec,
    get_service_manager,
)

from .output import console, data_table, fail, info, ok, warn


# Service registry moved to okuro.system.install so the onboarding
# completion handler (web API layer) can install services without
# importing Click-tainted cli modules.
from okuro.system.install import (
    get_service_registry as get_okuro_service_registry,
    project_root as _project_root,
)


# --- Click commands ----------------------------------------------------


@click.group()
def service():
    """Install and control okuro background services (systemd/launchd)."""


@service.command("list")
def service_list():
    """List known okuro services and their current state."""
    try:
        mgr = get_service_manager()
    except (NotImplementedError, RuntimeError) as e:
        fail(str(e))
        raise SystemExit(1)

    registry = get_okuro_service_registry()
    rows: list[list[str]] = []
    for name, spec in registry.items():
        try:
            status = mgr.status(name)
        except Exception as e:
            status = {"active": False, "state": f"error: {e}"}
        state = status.get("state", "unknown")
        active = "active" if status.get("active") else "inactive"
        color = "[green]" if status.get("active") else "[dim]"
        rows.append([name, f"{color}{state}[/]", active, spec.description])

    table = data_table(
        columns=["name", "state", "running?", "description"],
        rows=rows,
        title="okuro services",
    )
    console.print(table)
    info(f"backend: {mgr.status(next(iter(registry))).get('backend', 'unknown')}")


@service.command("status")
@click.argument("name")
def service_status(name: str):
    """Show detailed status for a service."""
    try:
        mgr = get_service_manager()
    except (NotImplementedError, RuntimeError) as e:
        fail(str(e))
        raise SystemExit(1)

    status = mgr.status(name)
    for k, v in status.items():
        console.print(f"  [bold]{k:10}[/bold]  {v}")


def _mgr_or_exit():
    try:
        return get_service_manager()
    except (NotImplementedError, RuntimeError) as e:
        fail(str(e))
        raise SystemExit(1)


def _spec_or_exit(name: str, working_dir: Optional[Path]) -> ServiceSpec:
    registry = get_okuro_service_registry(project_root_override=working_dir)
    if name not in registry:
        fail(f"Unknown service: {name}")
        info(f"known services: {', '.join(sorted(registry.keys()))}")
        raise SystemExit(1)
    return registry[name]


@service.command("install")
@click.argument("name")
@click.option(
    "--working-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Override the service WorkingDirectory (default: okuro project root).",
)
@click.option("--start/--no-start", default=False, help="Start after install.")
@click.option("--enable/--no-enable", default=False, help="Enable on boot after install.")
def service_install(name: str, working_dir: Optional[Path], start: bool, enable: bool):
    """Install a known okuro service from the built-in registry."""
    mgr = _mgr_or_exit()
    spec = _spec_or_exit(name, working_dir)

    unit_path = mgr.install(spec)
    ok(f"Installed: {unit_path}")

    if enable:
        mgr.enable(name)
        ok(f"Enabled: {name}")
    if start:
        mgr.start(name)
        ok(f"Started: {name}")


@service.command("uninstall")
@click.argument("name")
@click.confirmation_option(prompt="Uninstall this service?")
def service_uninstall(name: str):
    """Stop, disable, and remove the unit file for a service."""
    mgr = _mgr_or_exit()
    mgr.uninstall(name)
    ok(f"Uninstalled: {name}")


@service.command("start")
@click.argument("name")
def service_start(name: str):
    """Start a service."""
    mgr = _mgr_or_exit()
    mgr.start(name)
    ok(f"Started: {name}")


@service.command("stop")
@click.argument("name")
def service_stop(name: str):
    """Stop a service."""
    mgr = _mgr_or_exit()
    mgr.stop(name)
    ok(f"Stopped: {name}")


@service.command("restart")
@click.argument("name")
@click.option("--when-safe", is_flag=True, hidden=True,
              help="Deprecated: the safety check is the default. Kept so existing "
                   "callers don't break.")
@click.option("--wait", default=0, metavar="SECONDS",
              help="Poll up to SECONDS for a safe window instead of refusing "
                   "immediately. Unattended, so it needs dev.agent_may_restart.")
@click.option("--force", is_flag=True,
              help="Skip the safety check and restart now. You are asserting "
                   "nothing is in flight.")
def service_restart(name: str, when_safe: bool, wait: int, force: bool):
    """Restart a service, refusing if it would interrupt live work.

    The check is the DEFAULT, not an opt-in. It was --when-safe, and that made it
    decoration: every restart in practice went around it, because the safe path
    was the one you had to know to ask for by name. A seatbelt you opt into
    protects nobody. Now the refusal is what you get for free and --force is what
    you type deliberately.

    Refusing to break things needs no permission, so the check protects everyone,
    flag or no flag. dev.agent_may_restart gates only --wait: blocking until a
    window opens and then acting is unattended behaviour, which is an agent's
    move, not a human's.

    Services with no probe (see restart_guard.PROBEABLE) refuse by default —
    "we never wrote the busy-check" is not evidence that nothing is busy.
    """
    mgr = _mgr_or_exit()

    if not force:
        from okuro.system.restart_guard import (
            check_restart_safe, dev_restart_enabled, wait_until_safe,
        )

        if wait and not dev_restart_enabled():
            fail("--wait is unattended, so it needs dev.agent_may_restart: true "
                 "in ~/.okuro/config.yaml. Without it, run the plain restart and "
                 "decide yourself, or pass --force.")
            return
        verdict = (
            wait_until_safe(name, timeout=float(wait)) if wait
            else check_restart_safe(name)
        )
        if not verdict.safe:
            fail(f"Refusing to restart {name}: {verdict.reason}")
            info("Pass --force to restart anyway, or --wait SECONDS to hold for "
                 "a safe window.")
            return
        ok(f"Precondition met — {verdict.reason}")

    mgr.restart(name)
    ok(f"Restarted: {name}")


@service.command("enable")
@click.argument("name")
def service_enable(name: str):
    """Enable a service at boot."""
    mgr = _mgr_or_exit()
    mgr.enable(name)
    ok(f"Enabled: {name}")


@service.command("disable")
@click.argument("name")
def service_disable(name: str):
    """Disable a service at boot."""
    mgr = _mgr_or_exit()
    mgr.disable(name)
    ok(f"Disabled: {name}")


@service.command("run-now")
@click.argument("name")
def service_run_now(name: str):
    """Fire a oneshot service immediately, bypassing the timer schedule."""
    mgr = _mgr_or_exit()
    registry = get_okuro_service_registry()
    if name not in registry:
        fail(f"Unknown service: {name}")
        info(f"known services: {', '.join(sorted(registry.keys()))}")
        raise SystemExit(1)
    spec = registry[name]
    if spec.service_type != "oneshot":
        warn(f"{name} is not a oneshot service — use 'okuro service start' instead")
        raise SystemExit(1)
    mgr.run_now(name)
    ok(f"Ran: {name}")
