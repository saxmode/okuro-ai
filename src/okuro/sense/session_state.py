# SPDX-License-Identifier: Apache-2.0
"""Per-session MCP state, keyed by session id.

Every module that tracks compliance (bootstrap called, tools used, flags set,
etc.) MUST get its state dict from here — no private copies.

Enforced by tests/sense/test_session_state.py which greps the codebase for
duplicate `_state = {` / `_session_state = {` literals.

Transport is not identity
-------------------------
One transport can carry several agents in sequence. Under stdio MCP the
server is a child of the client process, so a nested agent the client spawns
in-process reuses the parent's connection and therefore the "stdio" dict.
Keying compliance purely on the transport made those agents share one
counter; ``_rotate_for_new_agent`` re-keys on the bootstrap that mints a new
telemetry session id, which is the only agent-level boundary okuro can
observe without knowing which provider spawned what.

HTTP transport wiring
---------------------
Under HTTP MCP, a single long-running daemon serves many clients. The
transport middleware sets ``current_session_id`` (a contextvar) from the
``Mcp-Session-Id`` header; every compliance lookup in this request's call
chain then sees the right per-client dict automatically without threading
the id through every signature.

The module-level ``_state`` dict IS the "stdio" session's state. Code that
imported it before the HTTP rework (and tests that grep for its literal) keep
working; new HTTP sessions get sibling dicts registered in ``_sessions``.
"""

import os
import time as _time
from contextvars import ContextVar


# The active session id for the current request. Stdio callers never touch it
# and get the "stdio" default. HTTP callers set it from the MCP header.
current_session_id: ContextVar[str] = ContextVar(
    "okuro_session_id", default="stdio"
)

#: True when the TRANSPORT bound ``current_session_id`` for this call — set by
#: the MCP server factory from the ``mcp-session-id`` header, reset after.
#:
#: WHAT IT PROTECTS. ``adopt_caller_identity`` overwrites ``current_session_id``
#: from a file in the real HOME. That is correct under stdio, where one
#: connection carries the parent and every nested agent and the file is the only
#: signal separating them. Under HTTP it was wrong and measured wrong: the
#: transport had already bound the authenticated per-session id, and adoption
#: replaced it with whatever the LOCAL CLI's hook last wrote — "stdio" for an
#: empty file. Every concurrent HTTP session then shared one state dict, which
#: is precisely the collapse the per-call header binding was written to fix
#: (session B riding session A's bootstrap).
#:
#: A value-shape heuristic cannot separate the two cases: after one stdio
#: adoption the contextvar holds ``agent:<id>``, which is indistinguishable from
#: a transport-bound id by inspection, and skipping adoption there would break
#: the nested-agent isolation adoption exists for. So the transport says so
#: explicitly instead.
transport_bound_session: ContextVar[bool] = ContextVar(
    "okuro_transport_bound_session", default=False
)

#: Where the provider hook publishes the calling agent's id. Overridable so the
#: test suite can be HERMETIC with respect to it.
#:
#: Why this seam exists: ``adopt_caller_identity`` reads a file in the real
#: HOME, and ``pre_tool_call`` calls it FIRST, before reading any state. The
#: suite sandboxes ``OKURO_HOME`` and did not sandbox this, so the result of a
#: test run depended on whether any okuro-hooked agent had recently made an MCP
#: call on the same machine. Measured: flipping only this file, with no ordering
#: change at all, flips 9 tests across test_middleware, test_per_turn_rule_prefix,
#: test_session_state, test_subagent_policy — and two full-suite runs at
#: effectively the same commit differed by 13 failures. A suite that reads live
#: host state is not a gate; it is a coin flip that usually lands the same way.
#:
#: SCOPE, stated because getting this wrong is silent: the WRITER is the
#: provider hook (Claude Code's ``check-agent-bootstrap.py``), which lives
#: outside this repo and hardcodes the default directory. So this is a TEST
#: seam. Pointing it somewhere else in production redirects the reader without
#: moving the writer, which costs ISOLATION, never correctness — the same
#: degradation ``adopt_caller_identity`` already documents for a missing or
#: unreadable file, because the value only ever SELECTS a state dict and never
#: grants anything.
AGENT_STATE_DIR_ENV = "OKURO_AGENT_STATE_DIR"
_DEFAULT_AGENT_STATE_DIR = "~/.cache/okuro-agent-bootstrap"


def agent_state_dir() -> str:
    """Directory the provider hook publishes caller identity into."""
    return os.path.expanduser(
        os.environ.get(AGENT_STATE_DIR_ENV) or _DEFAULT_AGENT_STATE_DIR
    )


def caller_identity_path() -> str:
    """The file :func:`adopt_caller_identity` reads. Resolved per call, never
    cached at import, so a test may redirect it after this module is loaded."""
    return os.path.join(agent_state_dir(), ".current")


# Module-level canonical dict — the "stdio" session. Historical name preserved
# so external importers and the test_session_state grep guard keep working.
_state: dict = {
    "bootstrapped": False,
    "session_id": None,
    "provider": "unknown",
    "task_hint": "",
    "project": None,
    "tool_calls": 0,
    "memory_written": False,
    "progress_logged": False,
    "cortex_used": False,
    # Set when read_memory SUCCEEDS. Gates the consult-before-claim refusal in
    # mcp_middleware: a session may not persist a claim about the system until
    # it has asked the brain in this session.
    #
    # Only read_memory sets it. NOT bootstrap: bootstrap surfaces a generic
    # top-N against a task hint, and the failure this gate exists for happened
    # WITH bootstrap having run — the correcting memory existed, was indexed,
    # and was never queried. NOT cortex_*: cortex returns CODE, and the whole
    # point of the P0 requirement is that a line of code may be a fallback,
    # dead, or overridden upstream, so only the brain or the runtime knows
    # which.
    "memory_consulted": False,
    "session_reported": False,
    # Stream A telemetry — set when a write_role_handover call succeeds.
    # Surfaces in compliance_scorecard alongside memory_written /
    # progress_logged. R4 (atomic switch): same PR as the dispatcher
    # cutover so the switch produces signal from day one.
    "handover_written": False,
    # Stream C telemetry — set when delivery_send produces a successful
    # row. Telemetry-only; not a hard requirement (most subagents don't
    # have a recipient).
    "delivery_sent": False,
    # Set when artifact_write succeeds. Consumed by the session-end mandate
    # so a session that already shipped its deliverable is not told to ship
    # one again — and one that has not is reminded by name.
    "artifact_written": False,
    # The project slug whose bootstrap HALF this session still owes. Set by
    # mark_bootstrapped when the core bootstrap resolved a project; cleared by
    # mark_project_half_fetched. While it is set, mcp_middleware refuses every
    # tool outside a small allow-list.
    #
    # WHY A GATE AND NOT A SUGGESTION. Measured over 1,383 sessions: cortex is
    # instructed in the bootstrap packet, in TOOL-PROTOCOL.md and in every
    # provider file, and was used in 194. The gated bootstrap, instructed in the
    # same places, was called in 845. Instruction is not a delivery mechanism;
    # the refusal is. Splitting the packet without gating the second half would
    # have deleted the project context from the session rather than moved it.
    #
    # None when the hint resolved to no project — there is nothing to fetch, so
    # there is nothing to hold.
    "project_half_pending": None,
    # Telemetry-only evidence of a shared transport: the session id that
    # occupied this dict before the current agent bootstrapped over it.
    # None for a root session. See `_rotate_for_new_agent`.
    "predecessor_session_id": None,
    "start_time": _time.time(),
    "compliance_gaps": [],
    "cortex_nudged": False,
    # How many times the strict-freshness gate has refused this session.
    # Surfaced in the refusal message so a repeated refusal reads
    # differently from the first one. UNPROVEN: the A/B that validated the
    # rest of that message could not isolate the counter's effect — every
    # single-shot scenario has to state the refusal history, which hands
    # the control arm the same information. Kept on judgement, not
    # evidence; drop it if it ever costs anything.
    "stale_refusals": 0,
    # Counter consumed by the rule-prefix throttle in mcp_middleware.
    # The prefix anchors agent attention at session start; after the
    # cap is reached, the prefix only fires when an episodic compliance
    # nudge also fires (so re-anchoring rides the corrective signal
    # rather than tagging every routine tool call).
    "prefix_emit_count": 0,
}


# session_id -> state dict. "stdio" aliases the module-level `_state` above.
_sessions: dict[str, dict] = {"stdio": _state}


def _new_state() -> dict:
    """Fresh state dict for a newly-seen session id."""
    return {
        "bootstrapped": False,
        "session_id": None,
        "provider": "unknown",
        "task_hint": "",
        "project": None,
        "tool_calls": 0,
        "memory_written": False,
        "progress_logged": False,
        "cortex_used": False,
        "memory_consulted": False,
        "session_reported": False,
        "handover_written": False,
        "delivery_sent": False,
        "artifact_written": False,
        "project_half_pending": None,
        "predecessor_session_id": None,
        "start_time": _time.time(),
        "compliance_gaps": [],
        "cortex_nudged": False,
        "stale_refusals": 0,
        "prefix_emit_count": 0,
        "_behavioral_pool": None,
        "_per_turn_rule_prefix": None,
    }


def cortex_used_marker_path(session_key: str | None = None) -> str:
    """The file that tells a PROVIDER HOOK this session has already used cortex.

    THE REVERSE CHANNEL, and it exists because the routing rule changed shape.
    The owner, 2026-09-09: "grep can be used if cortex has been tried first." That
    is a SEQUENCE, so enforcing it needs one bit the enforcer cannot currently
    see — ``cortex_used`` lives in this process's session state, while
    ``check-grep.py`` is a separate process spawned per tool call by the
    provider. Without this file the hook can only block unconditionally, which
    is stricter than the rule it enforces: a gate contradicting its own contract
    is the defect class the 2026-09-08 surface audit was written about.

    DIRECTION. ``adopt_caller_identity`` reads what the hook WRITES
    (``.current``). This is the other way: the server writes, the hook reads.
    Same directory on purpose — one channel, one lifecycle, one thing to reason
    about — and the key is the same identity the hook already publishes, so the
    two ends cannot disagree about whose session it is.

    STALENESS IS BOUNDED BY BOOTSTRAP, not by time. ``clear_cortex_used`` runs
    when a session bootstraps, so a marker can never let a LATER session skip
    cortex on the strength of an earlier one's work. That matters: the per-agent
    bootstrap markers in this same directory are months old, and a permission
    marker with that lifetime would quietly disarm the gate for good.

    FAILS CLOSED. Every error path here returns as if the marker were absent, so
    the hook keeps blocking. A missing or unreadable file costs an agent one
    redirect to cortex; a wrongly-present one costs the rule.
    """
    key = session_key or current_session_id.get() or "stdio"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)[:80]
    return os.path.join(agent_state_dir(), f"cortex-used.{safe}")


def mark_cortex_used(session_key: str | None = None) -> None:
    """Publish 'this session has tried cortex' for the provider hooks."""
    try:
        os.makedirs(agent_state_dir(), exist_ok=True)
        with open(cortex_used_marker_path(session_key), "w"):
            pass
    except OSError:
        # Best-effort: the in-process flag is still set, and a missing marker
        # only means the hook keeps redirecting. Never break a tool call.
        pass


def clear_cortex_used(session_key: str | None = None) -> None:
    """Drop the marker so a new session starts owing cortex its first look."""
    try:
        os.unlink(cortex_used_marker_path(session_key))
    except OSError:
        pass


def adopt_caller_identity() -> str:
    """Point this request at the CALLING AGENT's state dict, not the transport's.

    Under stdio the MCP server is a child of the client, so the top-level
    session and every nested agent share one connection — and therefore one
    state dict. ``_rotate_for_new_agent`` stopped a child inheriting the
    parent's counters, but left the mirror defect: when the parent resumes
    after the child exits it is holding the CHILD's state. Its own
    ``memory_consulted``, ``tool_calls`` and compliance flags are simply gone.
    Forcing subagents to bootstrap (the check-agent-bootstrap gate) turned that
    from rare into routine.

    The prior analysis concluded the fix "needs a parent signal stdio does not
    carry". The TRANSPORT does not carry one — but the provider hook layer
    does: Claude Code's PreToolUse payload has ``agent_id`` for a nested agent
    and omits it for the top-level session. The hook publishes it to a file
    before the server sees the call, and this reads it back.

    Degrades to today's behaviour in every failure mode — no hook installed,
    another provider, unreadable file, unexpected content — because the value
    is only ever used to SELECT a dict, never to grant anything. A wrong or
    missing read costs isolation, not correctness.
    """
    import re as _re

    # The transport already bound an authenticated per-session identity for
    # this call. It outranks the hook file unconditionally: the file describes
    # whichever local CLI wrote it last, not the client on the other end of
    # this request. Overwriting here collapsed every concurrent HTTP session
    # onto one state dict — see `transport_bound_session`.
    if transport_bound_session.get():
        return current_session_id.get()

    path = caller_identity_path()
    try:
        with open(path) as fh:
            raw = fh.read().strip()
    except OSError:
        return current_session_id.get()

    # Empty file = the top-level session called last. Anything not matching the
    # expected shape is treated the same way: fall back rather than mint a key
    # from unvalidated bytes.
    if not raw or not _re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw):
        current_session_id.set("stdio")
        return "stdio"

    key = f"agent:{raw}"
    current_session_id.set(key)
    return key


def get_session_state(session_id: str | None = None) -> dict:
    """Return the live state dict for a session (mutable reference).

    With no argument, reads the current request's session from the contextvar;
    under stdio this is always "stdio" so behaviour is unchanged. Pass an
    explicit session_id only when you need to inspect another session's state
    (e.g. in diagnostics).
    """
    sid = session_id if session_id is not None else current_session_id.get()
    state = _sessions.get(sid)
    if state is None:
        state = _new_state()
        _sessions[sid] = state
    return state


def all_session_states() -> dict[str, dict]:
    """All per-session dicts. Used by the HTTP daemon's /health endpoint."""
    return _sessions


def current_audit_session_id() -> str | None:
    """Return the bootstrap-minted telemetry session id for the active session.

    Audit-table writers (role_knowledge, memories, progress entries, …) call
    this to default ``session_id`` when the caller didn't pass one. Without
    this default, the audit column relies on agent prompt-discipline — every
    role-maintenance bulk run before 2026-04-26 wrote ``session_id = NULL``
    because the maintenance mandate didn't instruct the agent to forward it.
    """
    return get_session_state().get("session_id")


def drop_session(session_id: str) -> None:
    """Release a session's state. Call when the transport closes the session."""
    if session_id == "stdio":
        # Don't let anyone evict the canonical dict.
        return
    _sessions.pop(session_id, None)


def _rotate_for_new_agent(state: dict, session_id: str | None) -> str | None:
    """Hand a freshly-bootstrapped agent a clean slate on a shared transport.

    One transport does not mean one agent. Under stdio MCP the server is a
    child of the client process, so EVERY agent that client runs — the root
    session and any nested agent it spawns in-process — speaks over the same
    connection and lands on the same state dict. Before this rotation a
    nested agent inherited whatever its predecessor had accumulated:

    * ``tool_calls`` was already past ``_SESSION_END_THRESHOLD``, so the
      session-end mandate fired on the nested agent's FIRST okuro call —
      an instruction to wrap up, delivered before it had done any work;
    * ``memory_written`` / ``progress_logged`` / ``session_reported`` were
      already satisfied by work it never did, so its own gaps went unnudged;
    * ``session_id`` was overwritten, so both agents' rows were attributed
      to whichever bootstrapped last.

    The rotation trigger is a second bootstrap carrying a DIFFERENT telemetry
    session id. That is protocol-level evidence read from okuro's own
    bootstrap contract — every agent of every provider calls bootstrap — so
    it holds without knowing, or asking, which CLI is on the other end.
    Contrast the convention it replaces: an env var only okuro's own
    dispatcher set, which by construction could only ever see okuro's own
    children.

    Cleared IN PLACE, never by rebinding: ``mcp_middleware`` (and the
    invariant test that guards it) alias this dict at import time, and
    swapping the object detaches them silently. Same trap ``reset()``
    documents at length.

    Returns the predecessor's session id, or None when nothing rotated.
    """
    if not state.get("bootstrapped") or session_id is None:
        return None
    previous = state.get("session_id")
    if previous is None or previous == session_id:
        return None

    state.clear()
    state.update(_new_state())
    state["predecessor_session_id"] = previous
    return previous


def mark_bootstrapped(
    provider: str = "unknown",
    task_hint: str = "",
    project: str | None = None,
    session_id: str | None = None,
) -> None:
    """Record that the agent has bootstrapped.

    ``session_id`` (when provided) is the telemetry session id minted by
    ``write_bootstrap_marker()`` — it's stored in the state dict so
    ``session_report`` uses the in-process id and doesn't get clobbered by a
    parallel process's bootstrap.

    A bootstrap that arrives with a NEW session id on an already-bootstrapped
    dict means a different agent now owns this transport; its counters and
    compliance flags are reset first — see ``_rotate_for_new_agent``.
    """
    # Provider from the RESOLVER, not os.environ — on the HTTP transport one
    # shared daemon serves every caller and its environment names none of them,
    # so an env read collapsed every subagent into the argument default. The
    # resolver checks the per-request session binding first (migration 128) and
    # falls back to the environment for stdio, where it is genuinely the
    # caller's.
    from okuro.sense.work_identity import resolve_agent_provider

    resolved_provider = resolve_agent_provider() or provider or "unknown"
    state = get_session_state()
    _rotate_for_new_agent(state, session_id)
    state["bootstrapped"] = True
    state["task_hint"] = task_hint
    state["provider"] = resolved_provider
    state["project"] = project
    # Arm the project-half gate. The packet this call marks as delivered is the
    # CORE half only; the project half is a second call, and the gate is what
    # makes it happen. No project resolved => nothing owed => no block (the
    # bootstrap protocol must not invent a hurdle for a task that has no
    # project).
    state["project_half_pending"] = project or None
    if session_id is not None:
        state["session_id"] = session_id
    # A NEW SESSION OWES CORTEX ITS FIRST LOOK. `_rotate_for_new_agent` resets
    # the in-process `cortex_used` flag; this drops the on-disk marker the
    # provider hooks read, so the two cannot disagree. Without it a marker from
    # an earlier session would let a later one hunt with grep having never
    # asked cortex — the gate would look armed and be disarmed. The per-agent
    # bootstrap markers in the same directory are months old, which is exactly
    # the lifetime a permission marker must not have.
    clear_cortex_used()


def mark_project_half_fetched() -> None:
    """Clear the project-half gate. Called by the ``bootstrap_project`` handler
    after the half is successfully assembled — the same actor model as
    ``mark_bootstrapped``: the AGENT clears it, in one call, by doing the thing
    the gate asks for."""
    get_session_state()["project_half_pending"] = None


def reset() -> None:
    """Reset the CURRENT request's session to initial values. Tests only.

    Clears IN PLACE, then repopulates. Both halves matter:

    * clear() first, because ``update()`` alone MERGES — keys not in
      ``_new_state()`` survived a "reset to initial values". ``role_fetched_for``
      is the one that bit: the role-gate fixture could not get a clean slate
      from this function, so it reached for ``_sessions.clear()`` instead.
    * in place, never by rebinding or clearing ``_sessions``, because other
      modules alias the live dict at import time (mcp_middleware.py does
      ``_session_state = get_session_state()``). Dropping the registry entry
      DETACHES those aliases rather than resetting them: get_session_state()
      mints a fresh dict while importers keep the orphan, and writes through
      one become invisible to the other.

    That pair is a small trap with a long fuse — the autouse fixture's
    detach leaked into every later test in the run and surfaced as an
    unrelated failure under full-suite ordering only. A reset that does not
    fully reset invites callers to reach past it for something that does.
    """
    state = get_session_state()
    state.clear()
    state.update(_new_state())
    state["_behavioral_pool"] = None
