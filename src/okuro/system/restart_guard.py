# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Dev-mode restart policy — may an agent restart this service right now?
# index: imports | def dev_restart_enabled | def check_restart_safe
#   | def wait_until_safe | class Verdict
# AGENT_HEADER_END -->
"""Dev-mode restart policy — may an agent restart this service right now?

Separate from ServiceManager on purpose: that class is *mechanism* (shell out to
systemctl/launchctl, four platform subclasses) and this is *policy* (should we,
right now). Putting the question inside restart() would mean answering it four
times and coupling a systemd wrapper to MCP session state.

The rule this encodes: an agent may restart a service unattended IFF it can
prove nobody is mid-call and nothing is mid-run. "Can't tell" is not proof, so
every unknown fails CLOSED. A guard that guesses is worse than no guard — it
converts a human's caution into a machine's false confidence.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from okuro.db.engine import okuro_home

log = logging.getLogger(__name__)

CONFIG_PATH = okuro_home() / "config.yaml"

# Services whose in-flight state we can actually inspect. Anything absent here
# is not "assumed safe" — it has no probe, so the guard refuses it. Adding a
# service means writing its probe, not adding its name.
_PROBE_DAEMON = "okuro-daemon"
_PROBE_ORCHESTRATOR = "okuro-orchestrator"
_PROBE_EMBED = "okuro-embed"

# Timer-fired oneshot jobs. They are not long-lived, so "restart" means "run it
# again now" — but systemd will kill an in-progress run to do that, and
# okuro-db-backup mid-write leaves a truncated backup: the one file you reach
# for after a disaster. Their probe needs no HTTP; systemd already knows whether
# the job is executing.
ONESHOT_JOBS = frozenset({"okuro-db-backup", "okuro-db-dedup", "okuro-db-vacuum"})

PROBEABLE = frozenset(
    {_PROBE_DAEMON, _PROBE_ORCHESTRATOR, _PROBE_EMBED} | ONESHOT_JOBS
)


@dataclass
class Verdict:
    """Answer to 'may I restart this now', with the reason and the evidence."""

    safe: bool
    reason: str
    detail: dict = field(default_factory=dict)


def dev_restart_enabled() -> bool:
    """True when ~/.okuro/config.yaml has `dev.agent_may_restart: true`.

    Named for what it gates — an AGENT's permission to act — not for a mode.
    OKURO_DEV=1 already means "this process is in dev mode", and the two must
    not merge: OKURO_DEV is read once at import, so gating restarts on it would
    mean restarting a service to enable the flag that permits restarting it.
    It also travels by env inheritance, silently granting the permission to
    every child process. This one lives in a file, is read fresh per command,
    and can be read back to see what is true.

    Opt-in and absent by default: on a box doing real work, an agent bouncing
    services on its own initiative is a bug, not a feature. And it does NOT mean
    "restart without checking" — it only unlocks the guarded path below.
    """
    if not CONFIG_PATH.is_file():
        return False
    try:
        import yaml

        data = yaml.safe_load(CONFIG_PATH.read_text())
    except Exception as exc:  # noqa: BLE001 — an unreadable config means "no"
        log.debug("restart_guard: config read failed (%s)", exc)
        return False
    if not isinstance(data, dict):
        return False
    dev = data.get("dev")
    if not isinstance(dev, dict):
        return False
    return dev.get("agent_may_restart") is True


def _daemon_health(timeout: float = 2.0) -> Optional[dict]:
    """GET the daemon's /health, or None if it can't be reached/parsed.

    ``configured_port``, not ``resolve_port``: the latter allocates a port and
    writes ~/.okuro/config.yaml when unset, so a read-only health probe could
    mutate config on a machine that had never started the daemon. No port
    configured means nothing has ever listened — report unreachable.
    """
    try:
        import json
        import urllib.request

        from okuro.mcp.http_server import configured_port

        port = configured_port()
        if port is None:
            log.debug("restart_guard: no mcp.http.port configured — daemon never booted")
            return None
        url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        log.debug("restart_guard: health probe failed (%s)", exc)
        return None


def _orchestrator_health(timeout: float = 2.0) -> Optional[dict]:
    """GET the orchestrator's /api/health, or None if unreachable/unparsable.

    /api/health, NOT /health: the SPA owns /health and answers it with
    index.html, so a probe pointed there would read HTTP 200 and call the API
    alive even when it is dead. Guard against that by requiring JSON with the
    field we came for.
    """
    try:
        import json
        import urllib.request

        from okuro.orchestrator.api.main import PORT

        url = f"http://127.0.0.1:{PORT}/api/health"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read())
        return body if isinstance(body, dict) else None
    except Exception as exc:  # noqa: BLE001
        log.debug("restart_guard: orchestrator health probe failed (%s)", exc)
        return None


def _check_orchestrator() -> Verdict:
    """Would restarting the orchestrator break anything unrecoverable?

    Mostly no, and the probe says so rather than inventing caution:
      * engines run in their own transient systemd scopes, so they survive —
        an active task is NOT a reason to refuse, and engine-reconciler heals
        anything orphaned within 2m;
      * uvicorn's graceful shutdown waits out in-flight HTTP requests;
      * dropped SSE streams are re-established by the browser's EventSource.

    Two things do not heal:
      * an inline-MCP session — /mcp/v1 runs with no event store, so a restart
        404s attached agents and each must re-initialize;
      * a chat turn mid-stream — chat CLIs are plain subprocess children (no
        systemd-run --scope), so the reply dies mid-sentence in front of whoever
        is reading it. Idle chat sessions are NOT counted: they respawn on the
        next turn, and _IDLE_TTL keeps them alive 10 minutes after the last
        message, so refusing on existence would block for 10 minutes after
        anyone typed anything.
    """
    health = _orchestrator_health()
    if health is None:
        return Verdict(
            False,
            "orchestrator /api/health unreachable or not JSON — cannot tell "
            "'already stopped' from 'wedged'. Use --force if you know it is down.",
        )
    if "mcp_sessions" not in health:
        # An older build predates the field. Absence is not idleness.
        return Verdict(
            False,
            "orchestrator /api/health has no mcp_sessions field (running code "
            "predates the probe) — restart it once with --force to deploy it",
            {"health": health},
        )
    sessions = health.get("mcp_sessions")
    # Absent (not None) on a build that has mcp_sessions but predates this
    # field. 0 is the right read there: the missing-field refusal above already
    # catches genuinely old builds, and treating "no chat subsystem reporting"
    # as a permanent block would wedge the guard on any partial deploy.
    turns = health.get("chat_turns_in_flight", 0)
    detail = {"mcp_sessions": sessions, "chat_turns_in_flight": turns}
    if sessions is None:
        return Verdict(
            False,
            "inline MCP session count is unknown (the MCP SDK's internals moved) "
            "— refusing rather than restarting on top of a possible live agent",
            detail,
        )
    if sessions:
        return Verdict(
            False,
            f"{sessions} agent(s) attached to /mcp/v1 — a restart 404s them with "
            f"no resume, so they would have to re-initialize",
            detail,
        )
    if turns:
        return Verdict(
            False,
            f"{turns} chat turn(s) streaming — the reply would die mid-sentence "
            f"(chat CLIs are not in their own scope, unlike engines)",
            detail,
        )
    return Verdict(
        True,
        "idle: no agents attached to /mcp/v1, no chat turns streaming (engines "
        "survive in their own scopes; in-flight requests are waited out)",
        detail,
    )


def _embed_health(timeout: float = 2.0) -> Optional[dict]:
    """GET the embed service's /health, or None if unreachable/unparsable."""
    try:
        import json
        import urllib.request

        # Resolve the URL the same way the client does, so the probe always
        # asks the service the callers actually talk to (env override included).
        from okuro.embed.client import _resolve_embed_url

        url = _resolve_embed_url().rstrip("/") + "/health"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read())
        return body if isinstance(body, dict) else None
    except Exception as exc:  # noqa: BLE001
        log.debug("restart_guard: embed health probe failed (%s)", exc)
        return None


def _check_embed() -> Verdict:
    """Would restarting the embedding service interrupt anything?

    Cheapest of the three to interrupt — a killed encode fails one caller's embed
    and cortex retries on its next tick. But "recoverable" is not "free", and the
    service reports exactly what it is doing, so there is no reason to guess.

    Both counters matter and mean different things: `encoding` is work that dies
    on restart; `queued` is work that never started, whose caller is still
    blocked waiting for it.
    """
    health = _embed_health()
    if health is None:
        return Verdict(
            False,
            "embed /health unreachable — cannot tell 'already stopped' from "
            "'busy but not answering'. Use --force if you know it is down.",
        )
    if "encoding" not in health:
        return Verdict(
            False,
            "embed /health has no encoding field (running code predates the "
            "probe) — restart it once with --force to deploy it",
            {"health": health},
        )
    encoding = health.get("encoding") or 0
    queued = health.get("queued") or 0
    detail = {"encoding": encoding, "queued": queued}
    if encoding or queued:
        return Verdict(
            False,
            f"{encoding} encode(s) running, {queued} queued — a restart fails "
            f"those callers (recoverable: cortex retries next tick)",
            detail,
        )
    # A model still loading is not busy, but it is not useful either — restarting
    # mid-load just pays the load cost twice, so say so rather than call it idle.
    if not health.get("loaded", True):
        return Verdict(
            False,
            "embed model is still loading — a restart would pay the load cost "
            "again for nothing",
            detail,
        )
    return Verdict(True, "idle: nothing encoding, nothing queued", detail)


def _check_oneshot(name: str) -> Verdict:
    """Is this timer-fired job currently executing?

    Asks the `.service` unit explicitly rather than going through
    ServiceManager.status(), which resolves a name to its `.timer` when one
    exists. The timer reads "active" whenever it is armed — i.e. always — so
    routing through it would refuse every run forever while looking like it had
    checked something.
    """
    try:
        import subprocess

        r = subprocess.run(
            ["systemctl", "--user", "show", f"{name}.service",
             "-p", "ActiveState", "-p", "SubState", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return Verdict(False, f"cannot read {name}.service state from systemd")
        lines = [ln.strip() for ln in r.stdout.strip().splitlines() if ln.strip()]
    except Exception as exc:  # noqa: BLE001 — non-systemd host, or systemd down
        log.debug("restart_guard: oneshot probe failed (%s)", exc)
        return Verdict(False, f"cannot determine whether {name} is running")

    state = lines[0] if lines else ""
    detail = {"active_state": state, "sub_state": lines[1] if len(lines) > 1 else ""}
    if state == "active":
        return Verdict(
            False,
            f"{name} is running right now — restarting kills the in-progress run"
            + (" and leaves a truncated backup" if name == "okuro-db-backup" else ""),
            detail,
        )
    if state in ("inactive", "failed"):
        return Verdict(True, f"idle: {name} is not running ({state})", detail)
    # activating / deactivating / reloading — mid-transition is not idle.
    return Verdict(False, f"{name} is {state!r} — not a settled state", detail)


def check_restart_safe(name: str) -> Verdict:
    """Is it safe to restart *name* right now?

    Each probeable service has its OWN notion of busy — the daemon's is cron
    handlers plus MCP transports, the orchestrator's is only attached agents.
    Sharing one rule across both would either refuse the orchestrator for
    running engines it cannot hurt, or wave the daemon through mid-handler.
    """
    if name not in PROBEABLE:
        return Verdict(
            False,
            f"no in-flight probe exists for {name!r} — cannot prove a restart is "
            f"safe, so refusing (probeable: {', '.join(sorted(PROBEABLE))})",
        )

    if name == _PROBE_ORCHESTRATOR:
        return _check_orchestrator()
    if name == _PROBE_EMBED:
        return _check_embed()
    if name in ONESHOT_JOBS:
        return _check_oneshot(name)

    health = _daemon_health()
    if health is None:
        # Unreachable cuts both ways: already down (safe) or wedged mid-write
        # (not). Without evidence we do not get to pick the convenient one.
        return Verdict(
            False,
            "daemon /health unreachable — cannot tell 'already stopped' from "
            "'busy but not answering'. Use --force if you know it is down.",
        )

    sessions = health.get("session_count")
    handlers = health.get("running_handlers") or []
    detail = {"session_count": sessions, "running_handlers": handlers}

    if sessions is None:
        return Verdict(
            False,
            "live session count is unknown (the MCP SDK's internals moved) — "
            "refusing rather than restarting on top of a possible live agent",
            detail,
        )
    if sessions:
        return Verdict(
            False,
            f"{sessions} agent session(s) connected — a restart 404s them with "
            f"no resume (no event store), so they would have to re-initialize",
            detail,
        )
    if handlers:
        return Verdict(
            False,
            f"handler(s) mid-run: {', '.join(handlers)} — shutdown does not wait "
            f"for them, so they would be SIGKILLed after ~90s",
            detail,
        )
    return Verdict(True, "idle: no agent sessions, no handlers running", detail)


def wait_until_safe(name: str, timeout: float, poll: float = 5.0) -> Verdict:
    """Poll until *name* is safe to restart, or give up after *timeout* seconds.

    Waiting beats refusing for the common case: cortex occupies ~200s of every
    300s window, so "not now" is usually "yes, in two minutes" rather than a
    real conflict. A connected agent session, by contrast, may never clear —
    hence a bounded wait that reports why it gave up.
    """
    deadline = time.monotonic() + timeout
    verdict = check_restart_safe(name)
    while not verdict.safe and time.monotonic() < deadline:
        time.sleep(poll)
        verdict = check_restart_safe(name)
    return verdict
