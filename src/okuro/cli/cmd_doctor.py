# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro doctor CLI — thin Click wrapper around okuro.system.doctor.
# index:
#   imports
#   def _fetch_via_orchestrator
#   def doctor
# AGENT_HEADER_END -->
"""okuro doctor — health check with actionable fixes.

Post-H10 this module is a thin wrapper. All check logic lives in
``okuro.system.doctor`` so the web endpoint (``/api/doctor``) and the CLI
both see the same 10 probes in the same order.

Source preference (Finding #17):
1. Running orchestrator's ``GET /api/doctor`` — Aqua-bound on macOS,
   inherits the user's GUI/login PATH from the launchd plist, so probes
   like ``claude`` keychain access give the same answer the wizard and
   dashboard render. The single source of truth for live state.
2. Local ``run_all()`` fallback — used when the orchestrator is not
   running, unreachable, or auth fails. Still correct in Aqua context
   (Terminal.app on Mac, any Linux user session); can produce false
   negatives in degraded contexts (SSH, automation, cron) because
   keychain access is session-scoped.
"""

import json
import logging
import urllib.error
import urllib.request

import click

from okuro.system.doctor import CheckResult, run_all

from .output import console, ok, warn, fail, info


logger = logging.getLogger(__name__)


# Connect+read deadline. Generous because doctor probes can chain multiple
# subprocess calls server-side (e.g., per-CLI auth probes); on a cold
# orchestrator the first call may take a few seconds.
_ORCHESTRATOR_PROBE_TIMEOUT_SECONDS = 30.0


def _fetch_via_orchestrator() -> list[CheckResult] | None:
    """Try the running orchestrator's ``/api/doctor`` endpoint.

    Returns parsed CheckResult list on success, ``None`` on any failure
    (orchestrator not running, network error, auth missing, malformed
    response). Callers fall back to local ``run_all()`` on None — the
    fallback path is correct in any Aqua/login-session context, just not
    in degraded contexts (SSH, headless cron). Finding #17.
    """
    try:
        from okuro.keyring.storage import KeyringStorage
        from okuro.system.port_registry import orchestrator_port
    except Exception as exc:  # pragma: no cover — import-time only
        logger.debug("doctor: orchestrator probe unavailable (imports): %s", exc)
        return None

    try:
        token = KeyringStorage().get_key("okuro/api_token")
    except Exception as exc:
        logger.debug("doctor: keyring unavailable for orchestrator probe: %s", exc)
        return None
    if not token:
        return None

    url = f"http://127.0.0.1:{orchestrator_port()}/api/doctor"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=_ORCHESTRATOR_PROBE_TIMEOUT_SECONDS) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        logger.debug("doctor: orchestrator probe failed (%s); using local fallback", exc)
        return None

    raw_checks = payload.get("checks") if isinstance(payload, dict) else None
    if not isinstance(raw_checks, list):
        return None

    results: list[CheckResult] = []
    for entry in raw_checks:
        if not isinstance(entry, dict):
            continue
        results.append(
            CheckResult(
                name=str(entry.get("name", "")),
                status=str(entry.get("status", "fail")),  # type: ignore[arg-type]
                message=str(entry.get("message", "")),
                fix=entry.get("fix"),
                category=str(entry.get("category", "")),
            )
        )
    return results


@click.command()
@click.option("-v", "--verbose", is_flag=True, help="Show detailed diagnostics.")
@click.option(
    "--local",
    is_flag=True,
    help="Skip the orchestrator probe and run checks in this CLI process.",
)
def doctor(verbose, local):
    """Health check — shows what's running, what's missing, with fixes."""
    del verbose  # reserved; individual checks don't consume it yet.

    console.print()
    passed = 0
    warned = 0
    failed = 0

    results: list[CheckResult] | None = None
    source = "local"
    if not local:
        results = _fetch_via_orchestrator()
        if results is not None:
            source = "orchestrator"
    if results is None:
        results = list(run_all())

    for result in results:
        if result.status == "ok":
            ok(f"{result.name}: {result.message}")
            passed += 1
        elif result.status == "warn":
            warn(f"{result.name}: {result.message}")
            if result.fix:
                info(f"  Fix: {result.fix}")
            warned += 1
        else:
            fail(f"{result.name}: {result.message}")
            if result.fix:
                info(f"  Fix: {result.fix}")
            failed += 1

    console.print()
    parts = []
    if passed:
        parts.append(f"[green]{passed} passed[/green]")
    if warned:
        parts.append(f"[yellow]{warned} warnings[/yellow]")
    if failed:
        parts.append(f"[red]{failed} failed[/red]")
    console.print(f"  {' | '.join(parts)}")
    if source == "local":
        console.print(
            "  [dim](probed locally — orchestrator not reachable; "
            "auth states for keychain-backed CLIs may be wrong in non-Aqua "
            "contexts)[/dim]"
        )
    else:
        console.print("  [dim](via orchestrator)[/dim]")
    console.print()

    if failed:
        raise SystemExit(1)
