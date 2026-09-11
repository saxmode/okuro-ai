# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Async approval gate + tool_invocations audit writer for inline MCP sessions.
# index:
#   imports
#   contextvars (current_inline_session_id)
#   constants
#   _Pending dataclass
#   class ApprovalGate
#   def get_gate / reset_gate_for_tests
#   helpers (record_*)
# AGENT_HEADER_END -->
"""Approval gate — pauses MCP tool calls during inline web sessions.

The gate is invoked from :mod:`okuro.sense.mcp_middleware.pre_tool_call`
whenever the request belongs to an inline session (the contextvar
:data:`current_inline_session_id` is non-None — set by the FastAPI MCP
mount when the session-scoped bearer token matches a live session).

Semantics:

  auto       — execute immediately, write 'auto' audit row, return ok.
  ask-once   — first time this session asks for the tool, suspend on an
               asyncio Event until the browser approves / denies / edits.
               Subsequent calls for the same tool re-use the cached
               decision in the session state ("approved-once").
  ask-every  — suspend on every call; the cache is bypassed.
  deny       — never suspend; write a 'denied' row + return a deny error.

The 5-minute timeout (configurable via ``OKURO_INLINE_APPROVAL_TIMEOUT_SEC``)
flips the row to ``timeout`` and rejects the call. The contract calls
this default-deny on timeout — implemented here.

The browser-facing resolver lives in :mod:`okuro.bridge.mcp_tools` as
``bridge_stream_approve`` which calls :meth:`ApprovalGate.resolve` to
fire the event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-request contextvar — set by the FastAPI inline-MCP mount.
# ---------------------------------------------------------------------------
#
# When non-None, the active request is an inline-session HTTP MCP call;
# pre_tool_call routes through the gate. When None (stdio MCP, direct
# claude/codex/gemini terminal sessions), the gate is bypassed entirely so
# legacy behaviour is preserved.

current_inline_session_id: ContextVar[Optional[str]] = ContextVar(
    "okuro_inline_session_id", default=None
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT_SEC = 300.0


def _timeout_sec() -> float:
    raw = os.environ.get("OKURO_INLINE_APPROVAL_TIMEOUT_SEC", "")
    try:
        return float(raw) if raw else _DEFAULT_TIMEOUT_SEC
    except ValueError:
        return _DEFAULT_TIMEOUT_SEC


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Pending-approval record
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    """A tool call paused awaiting human decision."""

    invocation_id: str
    session_id: str
    tool_name: str
    tier: str
    policy: str
    args: dict[str, Any]
    created_at: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    # Loop that owns ``event`` — captured when consult() starts so that
    # resolve() can wake the waiter even when called from a different
    # thread / loop (the FastAPI request loop vs. the consult's loop).
    loop: Optional[asyncio.AbstractEventLoop] = None
    decision: Optional[str] = None  # 'approve' | 'deny' | 'edit' | 'timeout'
    edited_args: Optional[dict[str, Any]] = None


@dataclass
class GateDecision:
    """Result returned from :meth:`ApprovalGate.consult`.

    The dispatcher consumes this to know whether to execute, with which
    args, and what approval status to stamp on the audit row.
    """

    invocation_id: str
    status: str  # 'auto' | 'approved' | 'denied' | 'edited' | 'timeout'
    args: dict[str, Any]
    tier: str
    policy: str

    @property
    def allow(self) -> bool:
        return self.status in {"auto", "approved", "edited"}


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class ApprovalGate:
    """Process-local approval-gate.

    Holds:

    * a map of pending invocations keyed by ``invocation_id`` so
      ``bridge_stream_approve`` can resolve them by id.
    * a per-session set of tool names already approved this session (for
      the ``ask-once`` cache).
    * a per-session list of recent invocations the SSE stream / audit
      tool can read.

    Thread-safety: every mutation goes through ``self._lock`` (a regular
    threading.Lock — the gate is called from both the HTTP dispatcher
    coroutine and the in-memory MCP tool entry point). The asyncio
    primitives (Event) are only awaited from the dispatch coroutine; the
    resolve path sets the event via ``loop.call_soon_threadsafe`` so it
    works even when the resolver is on a different loop.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _Pending] = {}
        self._approved_once: dict[str, set[str]] = {}  # session_id -> tool_name set
        self._lock = threading.Lock()
        self._listeners: list[Any] = []

    # -- listeners ----------------------------------------------------------

    def add_event_listener(self, callback: Any) -> None:
        """Register a synchronous callback fired on every gate event.

        Fires for: ``tool_approval_required``, ``tool_call_done`` (with
        approval status), ``tool_call_start`` (auto-approved). The
        registry uses this hook in wave 3a to mirror gate events onto
        the unified-event stream. Callbacks must NOT raise — the gate
        swallows + logs.
        """
        with self._lock:
            self._listeners.append(callback)

    def remove_event_listener(self, callback: Any) -> None:
        with self._lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    def _emit(self, payload: dict) -> None:
        listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(payload)
            except Exception:  # noqa: BLE001
                logger.exception("approval_gate: listener crashed")

    # -- core consult coroutine --------------------------------------------

    async def consult(
        self,
        *,
        session_id: str,
        tool_name: str,
        args: Mapping[str, Any],
        tier: str,
        policy: str,
        timeout_sec: Optional[float] = None,
    ) -> GateDecision:
        """Decide whether ``tool_name`` may run inside ``session_id``.

        Writes a ``tool_invocations`` row at decision time so the audit
        ledger reflects every gated tool call regardless of whether it
        eventually executes. Status is one of ``auto``, ``approved``,
        ``denied``, ``edited``, ``timeout``.
        """
        invocation_id = uuid.uuid4().hex
        created_at = _utc_iso()
        args_dict: dict[str, Any] = dict(args)

        # ----- auto / deny short-circuits -----
        if policy == "auto":
            self._record_invocation(
                invocation_id=invocation_id,
                session_id=session_id,
                tool_name=tool_name,
                args=args_dict,
                approval_status="auto",
                approved_at=None,
            )
            self._emit(
                {
                    "type": "tool_call_start",
                    "session_id": session_id,
                    "invocation_id": invocation_id,
                    "tool_name": tool_name,
                    "args": args_dict,
                    "tier": tier,
                    "policy": policy,
                    "approval_status": "auto",
                    "ts": created_at,
                }
            )
            return GateDecision(
                invocation_id=invocation_id,
                status="auto",
                args=args_dict,
                tier=tier,
                policy=policy,
            )

        if policy == "deny":
            self._record_invocation(
                invocation_id=invocation_id,
                session_id=session_id,
                tool_name=tool_name,
                args=args_dict,
                approval_status="denied",
                approved_at=_utc_iso(),
            )
            self._emit(
                {
                    "type": "tool_call_done",
                    "session_id": session_id,
                    "invocation_id": invocation_id,
                    "tool_name": tool_name,
                    "outcome": "denied",
                    "tier": tier,
                    "policy": policy,
                    "ts": _utc_iso(),
                }
            )
            return GateDecision(
                invocation_id=invocation_id,
                status="denied",
                args=args_dict,
                tier=tier,
                policy=policy,
            )

        # ----- ask-once cache check -----
        if policy == "ask-once" and self._session_already_approved(
            session_id, tool_name
        ):
            self._record_invocation(
                invocation_id=invocation_id,
                session_id=session_id,
                tool_name=tool_name,
                args=args_dict,
                approval_status="approved",
                approved_at=_utc_iso(),
            )
            self._emit(
                {
                    "type": "tool_call_start",
                    "session_id": session_id,
                    "invocation_id": invocation_id,
                    "tool_name": tool_name,
                    "args": args_dict,
                    "tier": tier,
                    "policy": policy,
                    "approval_status": "approved-cached",
                    "ts": created_at,
                }
            )
            return GateDecision(
                invocation_id=invocation_id,
                status="approved",
                args=args_dict,
                tier=tier,
                policy=policy,
            )

        # ----- suspend on event -----
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        pending = _Pending(
            invocation_id=invocation_id,
            session_id=session_id,
            tool_name=tool_name,
            tier=tier,
            policy=policy,
            args=args_dict,
            created_at=created_at,
            loop=running_loop,
        )
        with self._lock:
            self._pending[invocation_id] = pending

        # Insert a placeholder audit row so the audit list reflects the
        # pause state. Status is filled in by _finalise_pending below.
        self._record_invocation(
            invocation_id=invocation_id,
            session_id=session_id,
            tool_name=tool_name,
            args=args_dict,
            approval_status="auto",  # temporary; overwritten on resolve
            approved_at=None,
            placeholder=True,
        )
        self._emit(
            {
                "type": "tool_approval_required",
                "session_id": session_id,
                "invocation_id": invocation_id,
                "tool_name": tool_name,
                "args": args_dict,
                "tier": tier,
                "policy": policy,
                "expires_at": _expires_at(timeout_sec or _timeout_sec()),
                "default_action": "deny",
                "ts": created_at,
            }
        )

        # Wait for resolve (browser approval) or timeout.
        try:
            await asyncio.wait_for(
                pending.event.wait(), timeout=timeout_sec or _timeout_sec()
            )
        except asyncio.TimeoutError:
            pending.decision = "timeout"

        # Remove from pending map regardless of outcome.
        with self._lock:
            self._pending.pop(invocation_id, None)

        decision = pending.decision or "deny"
        status_map = {
            "approve": "approved",
            "deny": "denied",
            "edit": "edited",
            "timeout": "timeout",
        }
        status = status_map.get(decision, "denied")

        if status == "edited" and pending.edited_args is not None:
            args_dict = dict(pending.edited_args)

        # Update the audit row to the final state.
        self._update_invocation_status(
            invocation_id=invocation_id,
            approval_status=status,
            approved_at=_utc_iso(),
            edited_args=args_dict if status == "edited" else None,
        )

        # Cache the approval for ask-once policies.
        if status in {"approved", "edited"} and policy == "ask-once":
            with self._lock:
                self._approved_once.setdefault(session_id, set()).add(tool_name)

        self._emit(
            {
                "type": "tool_call_done"
                if status in {"denied", "timeout"}
                else "tool_call_start",
                "session_id": session_id,
                "invocation_id": invocation_id,
                "tool_name": tool_name,
                "args": args_dict,
                "outcome": status,
                "tier": tier,
                "policy": policy,
                "ts": _utc_iso(),
            }
        )

        return GateDecision(
            invocation_id=invocation_id,
            status=status,
            args=args_dict,
            tier=tier,
            policy=policy,
        )

    # -- resolve from the browser -------------------------------------------

    def resolve(
        self,
        *,
        invocation_id: str,
        decision: str,
        edited_args: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Resolve a pending invocation.

        ``decision`` must be one of ``approve`` | ``deny`` | ``edit``.
        Edited args replace the original args before execution. Returns
        ``{"ok": True}`` on success or a structured error otherwise.
        """
        if decision not in {"approve", "deny", "edit"}:
            return {"ok": False, "error": "bad_decision", "decision": decision}

        with self._lock:
            pending = self._pending.get(invocation_id)
        if pending is None:
            return {"ok": False, "error": "unknown_invocation"}

        pending.decision = decision
        if decision == "edit":
            pending.edited_args = dict(edited_args or {})
        # Cross-loop safety: when the consult coroutine is awaiting on a
        # loop different from the caller's (e.g. the consult ran on the
        # MCP HTTP dispatcher loop and resolve() arrives from the
        # FastAPI request loop on a different thread), setting the
        # asyncio.Event directly is undefined. Schedule the .set() onto
        # the consult's loop via call_soon_threadsafe — falls back to a
        # direct set() when the caller IS on the consult's loop.
        target_loop = pending.loop
        if target_loop is not None and target_loop.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is target_loop:
                pending.event.set()
            else:
                target_loop.call_soon_threadsafe(pending.event.set)
        else:
            pending.event.set()
        return {
            "ok": True,
            "invocation_id": invocation_id,
            "decision": decision,
            "session_id": pending.session_id,
        }

    # -- audit + cache --------------------------------------------------

    def _session_already_approved(self, session_id: str, tool_name: str) -> bool:
        with self._lock:
            return tool_name in self._approved_once.get(session_id, set())

    def reset_session(self, session_id: str) -> None:
        with self._lock:
            self._approved_once.pop(session_id, None)
            for inv_id in list(self._pending.keys()):
                if self._pending[inv_id].session_id == session_id:
                    pending = self._pending.pop(inv_id)
                    pending.decision = "deny"
                    loop = pending.loop
                    if loop is not None and loop.is_running():
                        try:
                            running = asyncio.get_running_loop()
                        except RuntimeError:
                            running = None
                        if running is loop:
                            pending.event.set()
                        else:
                            loop.call_soon_threadsafe(pending.event.set)
                    else:
                        pending.event.set()

    def pending_for_session(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "invocation_id": p.invocation_id,
                    "tool_name": p.tool_name,
                    "tier": p.tier,
                    "policy": p.policy,
                    "args": dict(p.args),
                    "created_at": p.created_at,
                }
                for p in self._pending.values()
                if p.session_id == session_id
            ]

    # -- DB writers ----------------------------------------------------------

    def _record_invocation(
        self,
        *,
        invocation_id: str,
        session_id: str,
        tool_name: str,
        args: Mapping[str, Any],
        approval_status: str,
        approved_at: Optional[str],
        placeholder: bool = False,
    ) -> None:
        """Insert one tool_invocations row.

        ``placeholder=True`` is used for ask-* policies: the row gets a
        provisional ``approval_status='auto'`` so we have a stable PK
        when the SSE event fires; the final status is patched by
        :meth:`_update_invocation_status` once the user decides. We pin
        to 'auto' temporarily because the schema CHECK constraint pins
        the closed vocabulary — anything outside the 5 valid values
        would fail the INSERT.
        """
        # When the placeholder is being written, we still record it so
        # the audit reflects 'something was asked'. The final state is
        # patched in _update_invocation_status. To respect the CHECK
        # constraint we record provisional 'auto' and then update.
        record_invocation(
            invocation_id=invocation_id,
            session_id=session_id,
            tool_name=tool_name,
            args=args,
            approval_status=approval_status if not placeholder else "auto",
            approved_at=approved_at,
            executed_at=None,
            result=None,
            error=None,
            cost_ms=None,
        )

    def _update_invocation_status(
        self,
        *,
        invocation_id: str,
        approval_status: str,
        approved_at: Optional[str],
        edited_args: Optional[Mapping[str, Any]] = None,
    ) -> None:
        update_invocation(
            invocation_id=invocation_id,
            approval_status=approval_status,
            approved_at=approved_at,
            args=edited_args,
        )


# ---------------------------------------------------------------------------
# Stand-alone audit-writer helpers (also used from the dispatcher to
# stamp the final executed_at / result / error / cost_ms).
# ---------------------------------------------------------------------------


def record_invocation(
    *,
    invocation_id: str,
    session_id: str,
    tool_name: str,
    args: Mapping[str, Any],
    approval_status: str,
    approved_at: Optional[str] = None,
    executed_at: Optional[str] = None,
    result: Optional[str] = None,
    error: Optional[str] = None,
    cost_ms: Optional[int] = None,
) -> None:
    """Insert one ``tool_invocations`` row. Idempotent on PK collision."""
    try:
        from okuro.db import get_db
    except Exception:  # noqa: BLE001
        logger.debug("approval_gate: db unavailable, skipping audit write")
        return

    try:
        payload = json.dumps(dict(args), default=str)
    except Exception:  # noqa: BLE001
        payload = json.dumps({"_unencodable": True})

    db = get_db()
    try:
        with db.write():
            db.execute(
                """
                INSERT OR REPLACE INTO tool_invocations (
                    id, session_id, tool_name, args,
                    approval_status, approved_at, executed_at,
                    result, error, cost_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invocation_id,
                    session_id,
                    tool_name,
                    payload,
                    approval_status,
                    approved_at,
                    executed_at,
                    result,
                    error,
                    cost_ms,
                ),
            )
    except Exception:  # noqa: BLE001
        logger.exception("approval_gate: insert invocation failed")


def update_invocation(
    *,
    invocation_id: str,
    approval_status: Optional[str] = None,
    approved_at: Optional[str] = None,
    executed_at: Optional[str] = None,
    result: Optional[str] = None,
    error: Optional[str] = None,
    cost_ms: Optional[int] = None,
    args: Optional[Mapping[str, Any]] = None,
) -> None:
    """Patch a tool_invocations row by id. Only non-None fields are written."""
    try:
        from okuro.db import get_db
    except Exception:  # noqa: BLE001
        return

    fields: dict[str, Any] = {}
    if approval_status is not None:
        fields["approval_status"] = approval_status
    if approved_at is not None:
        fields["approved_at"] = approved_at
    if executed_at is not None:
        fields["executed_at"] = executed_at
    if result is not None:
        fields["result"] = result
    if error is not None:
        fields["error"] = error
    if cost_ms is not None:
        fields["cost_ms"] = cost_ms
    if args is not None:
        try:
            fields["args"] = json.dumps(dict(args), default=str)
        except Exception:  # noqa: BLE001
            fields["args"] = "{}"
    if not fields:
        return

    cols = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [invocation_id]
    db = get_db()
    try:
        with db.write():
            db.execute(
                f"UPDATE tool_invocations SET {cols} WHERE id = ?",
                tuple(params),
            )
    except Exception:  # noqa: BLE001
        logger.exception("approval_gate: update invocation failed")


def list_invocations(
    session_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Return the tool_invocations rows for ``session_id`` (newest first)."""
    try:
        from okuro.db import get_db
    except Exception:  # noqa: BLE001
        return []
    db = get_db()
    try:
        rows = db.fetchall(
            """
            SELECT id, session_id, tool_name, args, approval_status,
                   approved_at, executed_at, result, error, cost_ms
            FROM tool_invocations
            WHERE session_id = ?
            ORDER BY COALESCE(executed_at, approved_at, id) DESC
            LIMIT ?
            """,
            (session_id, max(1, min(limit, 500))),
        )
    except Exception:  # noqa: BLE001
        logger.exception("approval_gate: list_invocations failed")
        return []
    return [dict(r) for r in rows]


def _expires_at(timeout_sec: float) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + timeout_sec)
    )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_gate: Optional[ApprovalGate] = None
_gate_lock = threading.Lock()


def get_gate() -> ApprovalGate:
    """Return the process-local ApprovalGate, creating on first call."""
    global _gate
    with _gate_lock:
        if _gate is None:
            _gate = ApprovalGate()
    return _gate


def reset_gate_for_tests() -> None:
    global _gate
    with _gate_lock:
        _gate = None


__all__ = [
    "ApprovalGate",
    "GateDecision",
    "current_inline_session_id",
    "get_gate",
    "list_invocations",
    "record_invocation",
    "reset_gate_for_tests",
    "update_invocation",
]
