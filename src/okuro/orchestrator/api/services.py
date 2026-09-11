# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Services API — list/start/stop/restart/install okuro background services.
# index:
#   imports
#   router
#   models
#   def _require_localhost_sv
#   def _mgr
#   def _systemd_show
#   def _parse_activeenter
#   def _enriched_status
#   def _journal
#   def list_services
#   def get_service
#   def start_service
#   def stop_service
#   def restart_service
#   def install_service
#   def enable_service
#   def disable_service
# AGENT_HEADER_END -->
"""Services API — control okuro background services from the web UI.

Wraps ``okuro.system.service_manager`` and
``okuro.cli.cmd_service.get_okuro_service_registry`` so the SPA can:
- list every known service + live state
- start/stop/restart (mutating → localhost-only)
- install services that ship a spec but no unit file yet
- enable/disable boot persistence
- read the last ~50 lines of journal for one service

Mutating endpoints are restricted to localhost via the same helper used by
``/api/keyring`` and ``/api/cortex``. Read endpoints are unauthed — the
state they return is already visible via ``systemctl --user status``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from okuro.cli.cmd_service import get_okuro_service_registry
from okuro.system.service_manager import (
    DarwinServiceManager,
    LinuxServiceManager,
    ServiceSpec,
    get_service_manager,
)

logger = logging.getLogger("okuro.orchestrator.api.services")

router = APIRouter(prefix="/api/services", tags=["services"])


# ── Helpers ──────────────────────────────────────────────────────────


def _require_localhost_sv(request: Request) -> None:
    """Reuse main's loopback guard, same pattern as keyring + cortex."""
    from okuro.orchestrator.api.main import _require_loopback
    _require_loopback(request)


def _mgr():
    """Return the platform service manager or raise HTTPException(500)."""
    try:
        return get_service_manager()
    except (NotImplementedError, RuntimeError) as exc:
        raise HTTPException(500, f"Service manager unavailable: {exc}")


def _systemd_show(unit: str) -> dict[str, str]:
    """Run ``systemctl --user show <unit>`` and return the key=value map.

    Returns an empty dict on any failure so callers can fall back to the
    bare status dict. Output is intentionally capped — ``show`` prints a
    few hundred lines at most and we only care about a handful of keys.
    """
    if not shutil.which("systemctl"):
        return {}
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", unit, "--no-pager"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _parse_activeenter(ts: str) -> Optional[float]:
    """Parse ``ActiveEnterTimestamp`` (systemd RFC) → epoch seconds or None."""
    if not ts or ts == "n/a":
        return None
    # systemd formats: "Thu 2026-04-17 21:03:22 CEST" — %Z is unreliable on Python,
    # so we strip the trailing tz abbrev and assume local time.
    try:
        parts = ts.rsplit(" ", 1)
        bare = parts[0] if len(parts) == 2 else ts
        dt = datetime.strptime(bare, "%a %Y-%m-%d %H:%M:%S")
        return dt.timestamp()
    except ValueError:
        return None


def _enriched_status(name: str, spec: ServiceSpec) -> dict:
    """Build a UI-friendly status row for one service.

    Shape matches the TS ``ServiceRow`` on the frontend. Every field is
    optional from the SPA's perspective — backend failures degrade to
    ``installed=False`` + ``state="unknown"`` rather than raising.
    """
    mgr = _mgr()
    base: dict = {
        "name": name,
        "description": spec.description,
        "state": "unknown",
        "active": False,
        "running": False,
        "backend": "unknown",
        "installed": False,
        "uptime_seconds": None,
        "memory_bytes": None,
        "restart_count": None,
        "enabled": None,
        "pid": None,
        "started_at": None,
    }
    try:
        status = mgr.status(name)
    except Exception as exc:
        logger.warning("status(%s) failed: %s", name, exc)
        return base

    base.update({
        "state": status.get("state", "unknown"),
        "active": bool(status.get("active")),
        "running": bool(status.get("active")),
        "backend": status.get("backend", "unknown"),
    })

    # --- systemd enrichment ---
    if isinstance(mgr, LinuxServiceManager):
        unit_path = mgr._service_path(name)  # noqa: SLF001 — intentional
        base["installed"] = unit_path.exists()

        if base["installed"]:
            show = _systemd_show(f"{name}.service")
            # LoadState=not-found means systemd doesn't know this unit yet
            if show.get("LoadState") == "not-found":
                base["installed"] = False
            else:
                mem = show.get("MemoryCurrent")
                if mem and mem.isdigit() and mem != "0":
                    base["memory_bytes"] = int(mem)
                rc = show.get("NRestarts")
                if rc and rc.isdigit():
                    base["restart_count"] = int(rc)
                pid = show.get("MainPID")
                if pid and pid.isdigit() and pid != "0":
                    base["pid"] = int(pid)
                # UnitFileState: enabled / disabled / static / masked / ...
                ufs = show.get("UnitFileState")
                if ufs:
                    base["enabled"] = ufs == "enabled"
                started = _parse_activeenter(show.get("ActiveEnterTimestamp", ""))
                if started and base["active"]:
                    base["started_at"] = started
                    base["uptime_seconds"] = max(
                        0, int(datetime.now().timestamp() - started)
                    )

    # --- launchd enrichment ---
    elif isinstance(mgr, DarwinServiceManager):
        plist_path = mgr._plist_path(name)  # noqa: SLF001 — intentional
        base["installed"] = plist_path.exists()

        if base["installed"]:
            # `launchctl list <label>` emits a plist-flavoured text blob
            # when the service is loaded; parse PID + enabled from it.
            label = mgr._label(name)  # noqa: SLF001
            try:
                r = subprocess.run(
                    ["launchctl", "list", label],
                    capture_output=True, text=True, timeout=3,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.warning("launchctl list %s failed: %s", label, exc)
                r = None

            if r and r.returncode == 0:
                # loaded → enabled (bootstrap clears the disabled flag)
                base["enabled"] = True
                for line in r.stdout.splitlines():
                    stripped = line.strip()
                    if stripped.startswith('"PID" ='):
                        try:
                            pid_str = stripped.split("=", 1)[1].strip().rstrip(";").strip()
                            base["pid"] = int(pid_str)
                        except (IndexError, ValueError):
                            pass
            else:
                # plist on disk but not loaded → treat as installed-but-disabled
                base["enabled"] = False

    return base


_JOURNAL_LINES_MAX = 200


def _journal(name: str, lines: int = 50) -> list[str]:
    """Return the last N journal lines for a service, empty list on failure.

    ``lines`` is capped at 200 to prevent huge response bodies.
    """
    lines = max(1, min(lines, _JOURNAL_LINES_MAX))
    if not shutil.which("journalctl"):
        return []
    try:
        r = subprocess.run(
            [
                "journalctl",
                "--user",
                "-u",
                f"{name}.service",
                "-n",
                str(lines),
                "--no-pager",
                "--output=short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("journalctl failed for %s: %s", name, exc)
        return []
    if r.returncode != 0:
        # Unit may not be installed yet — surface empty list, not an error.
        return []
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


# ── Models ───────────────────────────────────────────────────────────


class ServiceRow(BaseModel):
    name: str
    description: str
    state: str
    active: bool
    running: bool
    backend: str
    installed: bool
    uptime_seconds: Optional[int] = None
    memory_bytes: Optional[int] = None
    restart_count: Optional[int] = None
    enabled: Optional[bool] = None
    pid: Optional[int] = None
    started_at: Optional[float] = None


class ServiceListResponse(BaseModel):
    services: list[ServiceRow]
    backend: str


class ServiceDetail(BaseModel):
    service: ServiceRow
    exec_start: list[str]
    working_directory: Optional[str] = None
    environment: dict[str, str]
    logs: list[str]


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("", response_model=ServiceListResponse)
def list_services() -> ServiceListResponse:
    """List every service in the built-in registry + live status.

    Read endpoint — no localhost guard. Same data is visible via
    ``systemctl --user list-units`` on the host anyway.
    """
    registry = get_okuro_service_registry()
    try:
        mgr = _mgr()
        backend = (
            "systemd" if isinstance(mgr, LinuxServiceManager) else "launchd"
        )
    except HTTPException:
        backend = "unknown"
    rows = [
        ServiceRow(**_enriched_status(name, spec))
        for name, spec in registry.items()
    ]
    return ServiceListResponse(services=rows, backend=backend)


@router.get("/{name}", response_model=ServiceDetail)
def get_service(name: str) -> ServiceDetail:
    """Full detail for one service — status + spec + last 50 journal lines."""
    registry = get_okuro_service_registry()
    if name not in registry:
        raise HTTPException(404, f"Unknown service: {name}")
    spec = registry[name]
    row = ServiceRow(**_enriched_status(name, spec))
    logs = _journal(name, 50)
    return ServiceDetail(
        service=row,
        exec_start=spec.exec_start,
        working_directory=str(spec.working_directory) if spec.working_directory else None,
        environment=spec.environment or {},
        logs=logs,
    )


def _is_self_action(name: str, action: str) -> bool:
    """True when a stop/restart on ``name`` would kill THIS process.

    Stopping the persistent orchestrator from inside the orchestrator is a
    self-stop race: launchctl/systemd sends SIGTERM to the very process
    serving the request, the connection drops mid-response, and the UI
    surfaces a generic 502/timeout — looks like "the services panel is
    broken" even though the stop succeeded. Refuse it at the API layer
    and tell the user the working alternative.
    """
    if action not in {"stop", "restart"}:
        return False
    # The orchestrator API is the only okuro service that runs uvicorn over
    # this app. Embed and daemon are separate processes, so action on them
    # never crosses this boundary.
    if name != "okuro-orchestrator":
        return False
    # A separate uvicorn launched by the user under a non-default OKURO_PORT
    # is fine to stop from here — only the SAME-process case is dangerous.
    # We detect it by checking whether the running orchestrator's PID is
    # ours (psutil compares argv tail / pid file path).
    try:
        import os as _os
        from okuro.system.service_manager import get_service_manager
        mgr = get_service_manager()
        status = mgr.status(name)
        running_pid = status.get("pid") or status.get("main_pid")
        return running_pid == _os.getpid() or running_pid is None
    except Exception:
        # When we can't tell, assume same-process and refuse — false positives
        # are recoverable (use CLI), false negatives leave the user with the
        # broken-panel symptom we set out to fix.
        return name == "okuro-orchestrator"


def _action(name: str, action: str, request: Request) -> ServiceRow:
    """Shared handler for start/stop/restart/enable/disable."""
    _require_localhost_sv(request)
    registry = get_okuro_service_registry()
    if name not in registry:
        raise HTTPException(404, f"Unknown service: {name}")
    if _is_self_action(name, action):
        raise HTTPException(
            409,
            f"Cannot {action} '{name}' through its own API — that would kill "
            "this process mid-request and the UI would see a connection drop "
            "instead of a clean response. Run from a terminal instead: "
            f"`okuro service {action} {name}` (the CLI talks to launchd/"
            "systemd directly without going through the orchestrator).",
        )
    mgr = _mgr()
    try:
        fn = getattr(mgr, action)
        fn(name)
    except subprocess.CalledProcessError as exc:
        # systemctl/launchctl failure — surface stderr when available
        msg = (exc.stderr or exc.stdout or str(exc)).strip() if hasattr(exc, "stderr") else str(exc)
        raise HTTPException(500, f"{action} failed: {msg}")
    except FileNotFoundError as exc:
        raise HTTPException(
            409,
            f"{action} failed — service '{name}' is not installed. Install it first.",
        ) from exc
    except Exception as exc:
        logger.exception("%s(%s) failed", action, name)
        raise HTTPException(500, f"{action} failed: {exc}")
    return ServiceRow(**_enriched_status(name, registry[name]))


@router.post("/{name}/start", response_model=ServiceRow)
def start_service(name: str, request: Request) -> ServiceRow:
    return _action(name, "start", request)


@router.post("/{name}/stop", response_model=ServiceRow)
def stop_service(name: str, request: Request) -> ServiceRow:
    return _action(name, "stop", request)


@router.post("/{name}/restart", response_model=ServiceRow)
def restart_service(name: str, request: Request) -> ServiceRow:
    return _action(name, "restart", request)


@router.post("/{name}/enable", response_model=ServiceRow)
def enable_service(name: str, request: Request) -> ServiceRow:
    return _action(name, "enable", request)


@router.post("/{name}/disable", response_model=ServiceRow)
def disable_service(name: str, request: Request) -> ServiceRow:
    return _action(name, "disable", request)


@router.post("/{name}/install", response_model=ServiceRow)
def install_service(name: str, request: Request) -> ServiceRow:
    """Install the unit file for a known service.

    Required before start/stop can succeed for services that ship a spec
    but aren't yet written to ``~/.config/systemd/user/``.
    """
    _require_localhost_sv(request)
    registry = get_okuro_service_registry()
    if name not in registry:
        raise HTTPException(404, f"Unknown service: {name}")
    spec = registry[name]
    mgr = _mgr()
    try:
        mgr.install(spec)
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or str(exc)).strip() if hasattr(exc, "stderr") else str(exc)
        raise HTTPException(500, f"install failed: {msg}")
    except Exception as exc:
        logger.exception("install(%s) failed", name)
        raise HTTPException(500, f"install failed: {exc}")
    return ServiceRow(**_enriched_status(name, spec))
