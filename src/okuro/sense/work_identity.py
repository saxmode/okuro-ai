# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Work identity of the caller — resolved at the transport boundary, never from process env.
# index:
#   class WorkIdentity
#   def bind_work_identity
#   def resolve_work_identity
#   def resolve_subtask_role
#   def resolve_agent_provider
#   def resolve_session_type
#   def identity_for_session
#   def verify_binding
# AGENT_HEADER_END -->
"""Which dispatched work is the current MCP call doing?

Three v5 mechanisms need that answer: the P4.2 dispatch lease, the
successor-engine reap, and the C12 write-epoch fence. Each used to ask
``os.environ`` in the process handling the call, which is correct only when
every subagent owns a private stdio MCP subprocess that inherited the
dispatcher's ``extra_env``.

It does not. The live transport is HTTP to ONE shared daemon
(``http://127.0.0.1:13333/mcp/v1/``). The subagent CLI's environment carries
the vars; the daemon's does not. Measured 2026-08-01 over the whole table:
``SELECT count(*) FROM sessions WHERE task_id IS NOT NULL`` -> 0, forever. All
three mechanisms failed silently and open, and nothing in the logs said so.

The repair is one boundary, not three call sites. Identity is bound to the
thing that already rides every request — the per-session bearer token, whose
``sessions_inline`` row is resolved by the transport before any tool runs — and
every consumer asks :func:`resolve_work_identity`. A future mechanism that
needs identity inherits a working answer instead of re-deriving the same bug.

Resolution order, and why:

1. ``current_work_identity`` — set by the transport from the authenticated
   session row. Per-request, so one shared daemon serving many subagents gives
   each the right answer.
2. process environment — the stdio / in-process case, where the environment
   genuinely IS the caller's. Kept so ``okuro mcp`` over stdio and direct
   Python callers keep working.

The ordering matters: the contextvar wins because on the shared daemon the
environment belongs to the daemon, not to the caller.

``agent_pid`` travels with the identity for the same reason the task id does.
``reap_predecessor_sessions`` SIGTERMs the pid on the session row; on the HTTP
path ``os.getpid()`` is the shared daemon, so recording it would aim the reaper
at okuro itself. Only the spawning process knows the CLI's pid, so it binds it
here at spawn.

ROUND 2 (2026-08-03, migration 128) brought three more facts through the same
boundary, after an adversarial verification found them still on os.environ:

* ``subtask_role`` — and this one is not telemetry. It ARMS the M5+
  assigned-role hard gate in ``mcp_middleware``. Read from the environment,
  ``_assigned`` was empty on every HTTP call, the gate concluded "not a
  subagent", and the M4 lazy-load bypass it was written to close stayed open on
  the only transport that runs. The stdio tests passed the whole time, which is
  exactly how it survived. Worse than the round-1 three: the streaming
  dispatcher — how subagents actually spawn — passes no ``extra_env`` at all,
  so the variable was not even set on the CLI. The row is the ONLY channel.
* ``agent_provider`` — per-provider attribution. Distinct from
  ``sessions_inline.provider``, which names the stream ADAPTER (claude/codex);
  this is the telemetry label (``orch-<role>``).
* ``session_type`` — "subagent" / "direct".

All three keep the process-environment fallback, for the same reason the ids
do: under stdio the environment genuinely IS the caller's.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkIdentity:
    """The dispatch a call belongs to.

    Every field is optional, including ``task_id``. The C12 epoch fence needs
    only the dispatch epoch, and the legacy stdio dispatcher exports the epoch
    without always exporting the ids — refusing to build an identity from a
    partial environment would silently disable the fence on that path, which is
    the failure this module exists to end, not to relocate.
    """

    task_id: str = ""
    subtask_id: Optional[str] = None
    dispatch_epoch: Optional[str] = None
    agent_pid: Optional[int] = None
    agent_host: Optional[str] = None
    # Round 2 (migration 128). ``subtask_role`` is the gate input, not a label.
    subtask_role: Optional[str] = None
    agent_provider: Optional[str] = None
    session_type: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
            "dispatch_epoch": self.dispatch_epoch,
            "agent_pid": self.agent_pid,
            "agent_host": self.agent_host,
            "subtask_role": self.subtask_role,
            "agent_provider": self.agent_provider,
            "session_type": self.session_type,
        }


# Set by the transport once per request, from the authenticated session row.
# Default None = "this caller is not doing dispatched work" (a human session).
current_work_identity: ContextVar[Optional[WorkIdentity]] = ContextVar(
    "okuro_work_identity", default=None
)


def bind_work_identity(identity: Optional[WorkIdentity]):
    """Bind for the current context. Returns the token to reset with."""
    return current_work_identity.set(identity)


def _from_env() -> Optional[WorkIdentity]:
    task_id = os.environ.get("OKURO_TASK_ID") or ""
    if not (task_id or os.environ.get("OKURO_SUBTASK_ID")
            or os.environ.get("OKURO_DISPATCH_EPOCH")):
        return None
    pid_raw = os.environ.get("OKURO_AGENT_PID") or ""
    try:
        pid = int(pid_raw) if pid_raw else os.getpid()
    except ValueError:
        pid = os.getpid()
    return WorkIdentity(
        task_id=task_id,
        subtask_id=os.environ.get("OKURO_SUBTASK_ID") or None,
        dispatch_epoch=os.environ.get("OKURO_DISPATCH_EPOCH") or None,
        agent_pid=pid,
        agent_host=_hostname(),
        subtask_role=os.environ.get("OKURO_SUBTASK_ROLE") or None,
        agent_provider=os.environ.get("OKURO_PROVIDER") or None,
        session_type=os.environ.get("OKURO_SESSION_TYPE") or None,
    )


def _hostname() -> Optional[str]:
    import socket

    try:
        return socket.gethostname()
    except Exception:  # noqa: BLE001
        return None


def resolve_work_identity() -> Optional[WorkIdentity]:
    """The dispatched work this call belongs to, or None.

    Transport binding first, process environment second. Never raises — a
    caller with no identity is a normal human session, not an error.
    """
    bound = current_work_identity.get()
    if bound is not None:
        return bound
    return _from_env()


def _resolve_field(field: str, *env_names: str) -> Optional[str]:
    """One field of the caller's identity: transport first, environment second.

    Deliberately NOT routed through :func:`resolve_work_identity`. That
    function's environment fallback returns None unless a task/subtask/epoch is
    present, which is right for a DISPATCH identity and wrong here — a stdio
    session can carry ``OKURO_PROVIDER`` alone and must still be attributed.
    """
    bound = current_work_identity.get()
    if bound is not None:
        value = getattr(bound, field, None)
        if value:
            return str(value)
        # A BOUND identity is authoritative for the request. Falling through to
        # the environment here would hand a subagent the DAEMON's answer — the
        # exact substitution this module exists to stop.
        return None
    for name in env_names:
        raw = os.environ.get(name)
        if raw:
            return raw
    return None


def resolve_subtask_role() -> Optional[str]:
    """The role the orchestrator assigned this session, or None.

    THE GATE INPUT. ``mcp_middleware`` refuses every tool call until
    ``roles_get(<this>)`` has been called, and an empty answer means "not a
    subagent, skip the gate" — so a wrong None here does not fail loudly, it
    disables a safety mechanism in silence. That is what it did on HTTP from
    the day the streaming dispatcher became the live path.
    """
    return _resolve_field("subtask_role", "OKURO_SUBTASK_ROLE")


def resolve_agent_provider() -> Optional[str]:
    """Provider label for attribution (``orch-<role>``, ``claude-code``, …).

    ``TM_PROVIDER`` is the pre-okuro alias, kept as a second env name so a
    long-lived stdio config does not silently lose its attribution.
    """
    return _resolve_field("agent_provider", "OKURO_PROVIDER", "TM_PROVIDER")


def resolve_session_type() -> Optional[str]:
    """``"subagent"`` / ``"direct"``, or None when nobody declared one."""
    return _resolve_field("session_type", "OKURO_SESSION_TYPE", "TM_SESSION_TYPE")


def identity_for_session(session_id: str) -> Optional[WorkIdentity]:
    """Read the identity bound to an inline session row at spawn.

    Used by the transport after the bearer token resolves to a row. Returns
    None when the session is not dispatched work, or when the columns are not
    there yet (a daemon running ahead of migration 122).
    """
    if not session_id:
        return None
    try:
        from okuro.db import get_db

        row = get_db().fetchone(
            """
            SELECT task_id, subtask_id, dispatch_epoch, agent_pid, agent_host,
                   subtask_role, agent_provider, session_type
              FROM sessions_inline
             WHERE id = ?
            """,
            (session_id,),
        )
    except Exception:  # noqa: BLE001
        logger.debug("work_identity: session lookup failed", exc_info=True)
        return None
    return from_row(row)


def from_row(row: Any) -> Optional[WorkIdentity]:
    """Build an identity from a ``sessions_inline`` row (dict or sqlite3.Row)."""
    if not row:
        return None
    try:
        get = row.get if hasattr(row, "get") else lambda k: row[k]  # noqa: E731
        task_id = get("task_id")
    except (KeyError, IndexError, TypeError):
        return None
    if not task_id:
        return None

    def _opt(key: str):
        try:
            return get(key)
        except (KeyError, IndexError, TypeError):
            return None

    pid = _opt("agent_pid")
    try:
        pid = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid = None
    return WorkIdentity(
        task_id=str(task_id),
        subtask_id=_opt("subtask_id"),
        dispatch_epoch=_opt("dispatch_epoch"),
        agent_pid=pid,
        agent_host=_opt("agent_host"),
        subtask_role=_opt("subtask_role"),
        agent_provider=_opt("agent_provider"),
        session_type=_opt("session_type"),
    )


class WorkIdentityNotBound(RuntimeError):
    """A dispatch wrote a work identity and the session row did not keep it."""


def verify_binding(session_id: str, expected: WorkIdentity) -> None:
    """Read the binding back after a spawn; raise if it did not take.

    The mechanisms this identity feeds all fail SILENTLY AND OPEN — an unbound
    session produces an empty lease, an empty reap and an unstamped write, none
    of which look like an error anywhere. That is how the previous version of
    this went unnoticed from the day it shipped. So the write is verified at the
    only moment the truth is still local: immediately after it, by the process
    that knows what it asked for.
    """
    actual = identity_for_session(session_id)
    if actual is None:
        raise WorkIdentityNotBound(
            f"session {session_id} carries no work identity after dispatch "
            f"(expected task={expected.task_id} subtask={expected.subtask_id}) "
            "— the lease, the reap and the write-epoch fence are all inert for "
            "this session"
        )
    if actual.task_id != expected.task_id or actual.subtask_id != expected.subtask_id:
        raise WorkIdentityNotBound(
            f"session {session_id} bound to task={actual.task_id} "
            f"subtask={actual.subtask_id}, expected task={expected.task_id} "
            f"subtask={expected.subtask_id}"
        )


__all__ = [
    "WorkIdentity",
    "WorkIdentityNotBound",
    "bind_work_identity",
    "current_work_identity",
    "from_row",
    "identity_for_session",
    "resolve_agent_provider",
    "resolve_session_type",
    "resolve_subtask_role",
    "resolve_work_identity",
    "verify_binding",
]
