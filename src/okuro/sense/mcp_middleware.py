# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Sense compliance GATES — the refusals. pre_tool_call decides whether a
#   tool runs at all; post_tool_call sets compliance flags on success only.
# index:
#   imports (session_state is the single source of truth)
#   def _text
#   def _prepend_warning
#   def _is_non_interactive_session
#   def _build_stale_code_reject_message
#   def _build_unconsulted_claim_message
#   def _build_bootstrap_reject_message
#   def _build_project_half_reject_message
#   def _build_role_fetch_reject_message
#   def _log
#   def pre_tool_call
#   def post_tool_call
#   (nudges + rule prefix -> nudges.py; handover validators -> handover_validators.py)
# AGENT_HEADER_END -->
"""Sense compliance gates — the refusals, and the flags set after a call.

Session state lives in `okuro.sense.session_state` as the single source of truth.

WHAT IS AND IS NOT IN THIS FILE, since it used to be all four: this module owns
the GATES — the things that can return allow=False and stop a tool running.
The advisory half (nudges, the per-turn rule prefix) is in `nudges.py`, and
`write_role_handover`'s payload schema is in `handover_validators.py`. Both are
re-exported below so existing imports keep working. The split line is "can this
refuse a call?" — a nudge that must actually be obeyed is a gate in the wrong
file, and that is the most likely way this boundary erodes.

Architectural contract (A4 + A8):
  - `pre_tool_call` returns (allow, message). When `allow=False`, the caller MUST
    NOT dispatch the tool; the `message` is returned to the client as an error.
    This is the hard gate: unbootstrapped sessions cannot run any tool except
    `bootstrap` itself, and a session that bootstrapped INTO a project runs
    nothing but `bootstrap_project` until it has fetched that half.
  - Compliance flags (`memory_written`, `progress_logged`, `session_reported`)
    are set in `post_tool_call` ONLY when `ok=True`. A failed write_memory/
    log_progress/session_report therefore does NOT mark the flag — the old
    pre-handler pattern produced silent false-positives.
  - At most ONE nudge is emitted per tool call, chosen by priority. The 4-nudge
    stack from ad19d54 saturated tool envelopes; this collapses it.
  - The mandatory-3 session footer is appended here (unbudgeted), not in the
    bootstrap packet's `build_session()` section. Budget compression can no
    longer truncate it.
"""

import json
import os

from mcp.types import TextContent

from okuro.sense.guard_log import guard_failed
from okuro.sense.session_state import (
    get_session_state,
    mark_bootstrapped as _mark_bootstrapped,
    mark_project_half_fetched as _mark_project_half_fetched,
)

# Backward-compat alias: module-level reference to the canonical "stdio"
# session's dict. Tests and legacy callers use this name. All runtime
# helpers below call ``get_session_state()`` per access so the HTTP
# transport sees the per-request session via the session_state contextvar.
_session_state = get_session_state()


# Guards in this module swallow their own failures on purpose — a broken
# best-effort check must never break tool dispatch. `guard_failed` is what
# keeps that from being SILENT; see okuro.sense.guard_log for why that matters.
# Aliased under the module-private name every call site here already uses.
_guard_failed = guard_failed


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _text(data) -> list[TextContent]:
    """Convert any data to MCP TextContent list."""
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _prepend_warning(result, warning: str):
    """Attach `warning` to a tool result of ANY shape.

    dict -> a `_compliance_warning` key, so the payload stays machine-readable.
    Everything else -> prepended as text, the same contract a `str` result
    already had.

    The final branch used to `return result` untouched, which silently dropped
    the warning for every tool returning a JSON **array** — `todo_list`,
    `artifact_list`, `roles_list` and friends. That made the enforcement this
    module calls un-skippable skippable: an agent whose late-session calls all
    return lists never saw the close-out mandate, never read "FINISH WITH YOUR
    OWN REPLY, IN PLAIN TEXT", and its caller got silence. Worse, `tool_calls`
    still incremented on those calls, so the turns burned invisibly — observed
    live on 2026-07-29, calls 15 and 17 of a subagent probe past the threshold
    carried nothing while 16 and 18 carried the footer.

    Keyed on the SHAPE rather than on a list of tool names on purpose: a new
    array-returning tool must not silently re-open this hole.
    """
    if isinstance(result, dict):
        result["_compliance_warning"] = warning
        return result
    if isinstance(result, str):
        return f"{warning}\n\n---\n\n{result}"
    return f"{warning}\n\n---\n\n{json.dumps(result, indent=2, default=str)}"


# ---------------------------------------------------------------------------
# Hard-gate allow-list — A8 strict mode, G1 default
# ---------------------------------------------------------------------------

# Tools that may run before `bootstrap`. Rationale: `bootstrap` is required;
# `list_projects`/`get_profile` are included so an onboarding wizard can
# prepare before first bootstrap (e.g. pick the right project). Everything
# else rejects until bootstrapped.
_PRE_BOOTSTRAP_ALLOWLIST = {
    "bootstrap",
    "get_profile",
    "list_projects",
    "get_project",
}


# Tools that may run between the CORE bootstrap and the PROJECT half.
#
# THE SPLIT THIS ENFORCES. `bootstrap` used to return one packet carrying both
# the session-invariant core and the project-scoped half. Measured 2026-08-27
# that packet was 51,056 chars for this repo's own project and 57,314 for the
# largest registered one, against a 50,000-char host result envelope — over
# the line the host throws the whole result away and substitutes a ~2,000-char
# preview. `bootstrap` now returns the core; `bootstrap_project` the other half.
#
# WHY THE SECOND CALL IS GATED AND NOT SUGGESTED. Same corpus, 1,383 sessions:
# cortex — instructed in the packet, in TOOL-PROTOCOL.md, and in every provider
# file — was used in 194. The GATED bootstrap was called in 845. A pull the
# agent is merely told to make is a pull that mostly does not happen, so
# splitting without gating would have deleted the project half from the session
# rather than moved it. This is the same mechanism that produces the 845.
#
# DENY-ALL-BUT-ALLOWLIST, deliberately, rather than a list of "project-scoped"
# tools. A gate scoped to a SHAPE is bypassed by another shape — the cortex hard
# block matched the tool name `Grep` and never fired once because 103 greps went
# through `Bash`. There is no shape here to get wrong.
#
# `bootstrap_project` clears it. `bootstrap` is present because re-bootstrapping
# must never be blocked. `session_report` so a stuck session can still close out
# its telemetry, matching every other gate in this file. The pre-bootstrap
# onboarding reads stay allowed for the same reason they are allowed there.
_PRE_PROJECT_HALF_ALLOWLIST = {
    "bootstrap",
    "bootstrap_project",
    "session_report",
    "get_profile",
    "list_projects",
    "get_project",
}


# Consult-before-claim. Tools that make a claim about the system DURABLE, and
# are therefore refused until this session has called read_memory at least once.
#
# WHY THESE TWO AND NOT log_progress: log_progress reports what THIS session
# did — firsthand knowledge the brain does not hold, so requiring a lookup
# first buys nothing and only adds friction. write_memory and artifact_write
# persist claims ABOUT THE SYSTEM, which is exactly where an unconsulted
# assertion becomes a durable false belief for every later session.
#
# WHY A REFUSAL AND NOT A REMINDER: measured in this corpus, a message returned
# inside a tool result produces no detectable behaviour change (cortex nudge
# n=80 p=0.215; regression discontinuity on the session-end reminder, no step
# at the cutoff). Refusals are the only channel with a positive signal. See the
# artifact "Agentic behaviour control — the enforcement strategy (v2)".
#
# THE INCIDENT THIS EXISTS FOR: an agent grepped `model or ("haiku" ...)` in
# critic.py, reported it as what RAN, and told the user a safeguard they had
# built days earlier had catastrophically failed. read_memory refuted it in one
# call — after the fact. The memory was indexed and retrievable the whole time.
# It has happened at least twice on that same claim, and again on 2026-07-27
# when an investigating session wrote a falsified memory about memory recall.
#
# Satisfiable in ONE call. Bypass: OKURO_ALLOW_UNCONSULTED_WRITE=1, matching
# the OKURO_ALLOW_* convention the git guards already use.
#
# THIS COMMENT USED TO CLAIM the gate "can never deadlock" because read_memory
# is absent from the set. That is true of this gate ALONE and false of the
# composition, and the composition is what ran. read_memory was not on
# _STALE_CODE_ALLOWLIST, so under freshness drift it was refused; the flag
# below is set only by a SUCCESSFUL read_memory (post_tool_call); and both
# tools in this set ARE stale-allowlisted, deliberately, so a finished result
# is never lost to a refusal. Composed: the two tools that allowlist exists to
# protect were the two this gate refused, and the session had no durable write
# channel left — exactly the record-loss the allowlist was written to prevent.
# Reproduced 2026-08-04; it blocked a full critique subagent until a reconnect.
#
# read_memory is now stale-allowlisted, which closes it. The invariant, since
# an individually-correct gate is not enough: EVERY armed gate must leave at
# least one legal move that clears it. tests/sense/test_gate_composition.py
# asserts that across the set, so the next pair is caught before it ships.
_CONSULT_BEFORE_CLAIM = {
    "write_memory",
    "artifact_write",
}


# Tools allowed before the assigned role's body is loaded via roles_get.
# Bootstrap is already past at this gate. roles_* tools must be allowed so
# the subagent can actually discover + fetch its role. session_report is
# allowed as an escape hatch so a stuck subagent can still close out
# telemetry cleanly without violating compliance.
_PRE_ROLE_FETCH_ALLOWLIST = {
    "bootstrap",
    # Both halves of one bootstrap. Without this the two gates deadlock: the
    # role gate would refuse `bootstrap_project`, and the project-half gate
    # would refuse `roles_get`, leaving a dispatched subagent with no legal
    # move at all.
    "bootstrap_project",
    "roles_get", "roles_list", "roles_match",
    "roles_domains", "roles_knowledge", "roles_maintenance",
    "session_report", "session_score",
    "get_profile", "get_principles",
}


# Tools allowed through the strict-freshness gate. `bootstrap` must run so
# the agent actually receives the drift warning describing why everything
# else is refused; `session_report` so a blocked session can still close out
# its telemetry cleanly instead of dying silently.
_STALE_CODE_ALLOWLIST = {
    "bootstrap",
    # The second half of the SAME bootstrap. Allowing the first and refusing
    # the second would leave a session permanently mid-bootstrap: the packet
    # already delivered the drift warning this gate exists to communicate, and
    # every other tool stays refused either way.
    "bootstrap_project",
    "session_report",
    # Environment/diagnostic reads: output is host state (GPU, disk, docker,
    # ports, systemd, transport health) produced by thin wrappers around
    # nvidia-smi/psutil/systemctl — it does NOT depend on okuro's frozen logic,
    # so a stale process answers them the same as a fresh one. The read-vs-write
    # axis is deliberately NOT used: cortex_search and roles_match are read-only
    # yet run okuro's own (stale) ranking/query/format code and stay gated.
    # recheck_disk_health is excluded too — it triggers a SMART scan (an
    # action), not a plain read. read_memory used to be named here as the third
    # example and is now allowlisted below — not because the argument stopped
    # applying to it, but because it is the precondition of two tools this list
    # already exempts, and gating a precondition while exempting what it gates
    # loses the record without buying correctness.
    "mcp_health",
    "sysinfo_gpu_status",
    "sysinfo_storage_status",
    "sysinfo_docker_status",
    "sysinfo_port_status",
    "sysinfo_service_health",
    "sysinfo_system_overview",
    "sysinfo_disk_health",
    # read_memory — the tool that UNBLOCKS the two writes directly below.
    #
    # Its absence here is what made the two gates compose into a trap: the
    # consult gate refuses write_memory and artifact_write until a read_memory
    # succeeds, and a stale-refused read_memory never succeeds. The session was
    # then left with no durable write channel at all, which is precisely the
    # record-loss the recording exemption below exists to prevent.
    #
    # The read-vs-write argument in the comment above still applies to it — a
    # stale process runs stale ranking code — and it is outweighed here, not
    # forgotten. Allowing the writes while refusing their precondition is not a
    # stricter position than allowing both; it is an incoherent one that costs
    # the record and buys no correctness. A slightly-old ranking that surfaces
    # a real memory beats a refusal that discards a finished result.
    "read_memory",
    # roles_get — the same shape as read_memory above, found by asking the
    # question generally instead of one gate at a time.
    #
    # THE ASSIGNED-ROLE GATE REFUSES EVERYTHING until roles_get succeeds. With
    # roles_get missing from this list, a session that is BOTH drifted and
    # role-assigned had no legal move at all: this gate refused roles_get, and
    # the role gate refused everything else — including read_memory, since the
    # role gate sits before the consult gate. Reproduced 2026-09-09.
    #
    # It looked safe for years only by ACCIDENT of two unrelated conditions
    # correlating: the role gate arms for orchestrator-dispatched subagents,
    # and those are usually classified non-interactive, for which this gate
    # degrades to a warning. Nothing enforced that correlation. An assigned
    # role on a session read as interactive deadlocks, which is what the
    # reproduction did.
    #
    # A stale role body is worth strictly more than no legal move, and the gate
    # this satisfies is about ADOPTING a role, not computing an answer.
    "roles_get",
    # RECORDING what already happened, on the same rationale as session_report
    # above: writing down a finished result is not the same risk as computing a
    # new one, and losing the record is strictly worse than keeping a record
    # written by a slightly old process.
    #
    # Observed cost of NOT having these: task-20260721-091632 subtask 5.3
    # finished and verified its deliverable, then had write_role_handover /
    # artifact_write / emit_task_event / await_review all refused because ONE
    # file changed. Both mandatory streams are required for outcome=success, so
    # the subtask was retried — re-spending on work that was already correct.
    "write_role_handover",
    "artifact_write",
    "emit_task_event",
    "await_review",
    "log_progress",
    "write_memory",
}


# Spawn contract — how ANY orchestrator, of any vendor, declares that the
# session it is launching has no human on the other end. These names are
# okuro's public interface, not a provider's: nothing below knows or asks
# which CLI is running, and adding a provider must never mean adding a name
# to this tuple. A spawner that wants its children treated as non-interactive
# sets one of these; that is the whole contract.
#
# Two of the four have NO producer in this repo, and that is deliberate — do
# not delete them as dead code. They are the half of the interface pointed
# OUTWARD, for spawners okuro does not own; a contract only okuro's own
# dispatcher can satisfy is precisely the defect this replaced. They go live
# the day a third-party orchestrator sets one, with no change here.
_NONINTERACTIVE_ENV = (
    # Explicit and self-describing — the name a third-party spawner reaches
    # for first. NO in-tree producer: outward-facing by design.
    ("OKURO_AGENT_NONINTERACTIVE", None),
    # Pre-existing generic declaration. Set by okuro's own dispatcher
    # (orchestrator/dispatcher.py:281) on every telemetry-tagged spawn,
    # including roleless ones — so this is the input that carries okuro's own
    # subagents in the general case.
    ("OKURO_SESSION_TYPE", "subagent"),
    # "I have a parent" — the same fact stated as a lineage pointer. NO
    # in-tree producer: outward-facing by design.
    ("OKURO_PARENT_SESSION_ID", None),
    # okuro's own spawn convention (orchestrator/dispatcher.py:293, set only
    # when a role is assigned). Kept as ONE input among several, no longer the
    # definition: it was being the definition that made this okuro-only.
    ("OKURO_SUBTASK_ROLE", None),
)


def _is_non_interactive_session() -> bool:
    """True when no human can act on a remedy addressed to this session.

    This is the question the gates below actually need answered, and it is
    deliberately not "is this a subagent". A gate's rejection text tells the
    reader to "restart this session" or run `/mcp reconnect`; what decides
    whether that text is useful is whether a human is reading it, not who
    spawned the process.

    The previous implementation asked a narrower question — "did okuro's own
    dispatcher spawn me?" — by testing a single env var okuro alone set. It
    answered correctly for okuro's children and wrongly for everyone else's:
    any other orchestrator's subagent was classified as a human at a terminal
    and handed a remedy it could not perform. Detection keyed on your own
    spawn convention can only ever recognise your own spawns.

    For a declared non-interactive session the freshness gate degrades to the
    drift WARNING (already emitted by bootstrap) rather than a refusal. That
    is a deliberate trade: a stale-but-labelled result costs less than a
    correct result thrown away, and every row such a session writes already
    carries `loaded_code_version()` (migration 104), so a result produced by a
    drifted process stays attributable after the fact.

    Interactive sessions — where a human CAN act on the remedy — keep the hard
    gate unchanged.

    KNOWN GAP, stated rather than papered over: a client that spawns nested
    agents INSIDE its own process, passing no environment, is invisible here.
    okuro sees one transport and no declaration. The signal that would catch
    it — a second bootstrap on a live transport — is also what a human
    starting a fresh task in the same process produces, so acting on it would
    degrade the human gate to catch the machine one. The nesting IS recorded
    (`predecessor_session_id`, session_state) so the frequency is measurable,
    and the damage it used to do is fixed at the source instead: state no
    longer carries across agents, and every gate message is written to be
    actionable by a reader who must relay upward rather than to a user.
    """
    for name, expected in _NONINTERACTIVE_ENV:
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        if expected is not None:
            if raw.lower() == expected:
                return True
            continue
        if raw.lower() not in ("0", "false", "no"):
            return True

    # Second signal, and the one that actually covers okuro's own subagents.
    #
    # The env contract above can only be read from the process that OWNS the
    # env — true for a stdio MCP server spawned as a child of the subagent.
    # It is structurally blind on the streaming path, which is how every
    # orchestrator subagent now runs: bridge/streaming/registry.py:create()
    # takes no env at all and wires the subagent to the SHARED daemon over
    # http://127.0.0.1:13333/mcp/v1/. One daemon serves N subagents, so its
    # process env is constant across all of them and describes none of them.
    #
    # The per-session answer already exists and is already trusted by the
    # approval gate: registry.create(is_agentic=True) persists it on the
    # sessions_inline row, and inline_http sets current_inline_session_id per
    # request. Asking THAT is what makes the question answerable per caller.
    #
    # Measured cost of not doing this: task-20260727-005417 subtask 3.1 and
    # task-20260727-233927 subtasks 2.2 + 2.3 were all hard-refused mid-work,
    # each recorded as "subagent never wrote an artifact" — a diagnosis that
    # named neither the gate nor the file that tripped it.
    try:
        from okuro.mcp._registry import _session_is_agentic
        from okuro.sense.approval_gate import current_inline_session_id

        inline_id = current_inline_session_id.get()
        if inline_id and _session_is_agentic(inline_id):
            return True
    except Exception as exc:  # noqa: BLE001 — a guard that can fail its own
        _guard_failed("agentic_session_lookup", exc)   # lookup must not
        pass                                          # decide the gate for it
    return False


def _build_stale_code_reject_message(name: str, drift: dict) -> str:
    """Rejection when this server is running code older than the disk.

    The remedy needs the USER — only they can reconnect or restart the
    client this server is a child of. So the message has to read as a
    handover, not as an explanation.

    Measured 2026-07-26, A/B over 5 models (n=10). The previous wording
    led with why-this-exists prose and buried `/mcp reconnect okuro` in a
    parenthetical beside two remedies aimed at the wrong actor. Result:
    claude opus, sonnet AND haiku all silently bypassed the gate via
    direct Python imports; gemini-3.1-pro and codex escalated. With the
    wording below, 5/5 relayed the ask and 0/5 bypassed — the failure was
    specific to the provider family okuro is used with most.

    What earned its place: naming the actor, supplying the exact words to
    relay, and banning the workaround outright (two runs cited that line
    unprompted). The refusal counter did NOT — see session_state.
    """
    behind = drift.get("behind", 0)
    dirty = drift.get("dirty") or []

    causes = []
    if behind:
        causes.append(
            f"{behind} commit{'s' if behind != 1 else ''} landed after boot "
            f"(running `{drift.get('loaded')}`, disk is at `{drift.get('head')}`)"
        )
    if dirty:
        shown = ", ".join(dirty[:3])
        more = f" +{len(dirty) - 3} more" if len(dirty) > 3 else ""
        causes.append(
            f"{len(dirty)} file{'s' if len(dirty) != 1 else ''} changed since "
            f"this process loaded them: {shown}{more}"
        )

    state = get_session_state()
    state["stale_refusals"] = state.get("stale_refusals", 0) + 1
    n = state["stale_refusals"]
    tally = f" Refusal #{n} this session."
    if n > 1:
        tally += f" You have been told {n - 1} time{'s' if n > 2 else ''} already."

    return (
        f"REJECTED: `{name}` — this okuro MCP server is running STALE CODE."
        f"{tally}\n\n"
        ">> ASK THE USER NOW. Say exactly:\n"
        '   "okuro\'s tools are gated on stale code — run `/mcp reconnect okuro` '
        "and I'll have memory and cortex for this.\"\n\n"
        ">> Do NOT work around this. No direct Python imports of okuro, no\n"
        "   reading the DB yourself. A workaround defeats the gate and yields\n"
        "   results computed by code that is no longer on disk.\n\n"
        # "no grep instead of cortex" was REMOVED from the line above, because
        # it stopped being true and a refusal that lies is a refusal agents
        # learn to argue with. While this gate is up cortex cannot answer at
        # all, so grep is not a way around the gate — it is the only remaining
        # way to read code, and the grep hook now releases it for exactly this
        # case. The bans that remain are the ones that still bypass the gate:
        # importing okuro's Python API or reading okuro.db both produce answers
        # from the stale process, which is the harm. Reading a FILE does not.
        ">> Locating code: cortex is refused while this is up, so grep and rg\n"
        "   are available to you here. That is the documented fallback, not a\n"
        "   workaround — but what you find is source text, not okuro's answer.\n\n"
        ">> You cannot fix this yourself: this server is a child of your client,\n"
        "   not of systemd, so restarting the okuro daemon does nothing.\n\n"
        ">> No human is reading you? Relay the same words to whoever spawned\n"
        "   you, AS YOUR FINAL PLAIN-TEXT REPLY. A nested agent still has an\n"
        "   actor upstream; going silent, or ending on a tool call, reaches\n"
        "   nobody and the ask dies with you.\n\n"
        + "\n".join(f"  - {c}" for c in causes)
        + f"\n  - booted {drift.get('booted')}; Python imports once at startup\n\n"
        # DELETED, three maintainer-addressed lines that made this refusal
        # negotiable: the "read this before proposing an exemption" paragraph
        # (which taught the agent that exemptions are a thing it may argue
        # for), the `_STALE_CODE_ALLOWLIST` source pointer, and the
        # `dev.strict_freshness` config key — the switch that disarms the very
        # gate being read. Naming the off-switch inside the refusal hands the
        # constrained party the key. The allowlist and the config key both
        # still exist; they are simply not advertised to the agent.
        #
        # KEPT: why a read-only tool is refused, because that is the one part
        # the agent needs in order to stop arguing with the refusal.
        "Read-only tools are gated too: cortex_search and roles_match run THIS\n"
        "process's ranking and formatting code, so their output can be wrong\n"
        "even though they only read."
    )


def _build_unconsulted_claim_message(name: str) -> str:
    """Rejection when a session tries to persist a claim without asking the brain.

    Same actor model as the bootstrap gate: the AGENT clears this itself in one
    call, so there is no escalation to the user. The routing ban is carried over
    verbatim because working around a gate is the measured failure mode, not a
    hypothetical one.
    """
    return (
        f"REJECTED: `{name}` is not available until this session has consulted "
        f"the brain.\n\n"
        ">> CALL THIS NOW:\n"
        "   mcp__okuro__read_memory(query='<the subsystem you are about to "
        "claim something about>', project='<slug>')\n\n"
        # The war story named a file, a code fragment and a past session's
        # mistake — okuro's own incident history, which the agent cannot act
        # on. What it CAN act on is the consequence for the claim it is about
        # to write.
        ">> WHY: you are about to make this claim durable. A previous session "
        "may have already established that it is wrong, and a wrong claim in "
        "the brain misleads every session after yours. One read_memory call is "
        "the check.\n\n"
        ">> Do NOT work around this by reading ~/.okuro/okuro.db yourself or "
        "importing okuro's Python API. The gate is what makes the claim "
        "checkable; routing around it produces exactly the unverified assertion "
        "it exists to stop.\n\n"
        ">> SOURCE CODE IS NOT EVIDENCE OF BEHAVIOUR. A line may be a fallback, "
        "dead, or overridden upstream. Only the brain or the runtime knows "
        "which — so a claim needs a memory consultation AND evidence from the "
        "run (plan.yaml, .activity.jsonl, or a tool result).\n\n"
        # "Bypass: OKURO_ALLOW_UNCONSULTED_WRITE=1" DELETED. A refusal that
        # names its own bypass has already granted it — the gate and the key
        # arrived in the same message, addressed to the party the gate exists
        # to constrain. The env var still works for the owner and for scripted
        # runs; it is no longer advertised to the agent it constrains.
        "One read_memory call clears this for the whole session."
    )


def _build_bootstrap_reject_message(name: str) -> str:
    """Human-readable rejection when tool call is blocked by the hard gate.

    Unlike the freshness gate, the actor here is the AGENT — it can clear
    this itself in one call, so no escalation to the user is needed. The
    only element borrowed from the freshness rewrite is the explicit ban
    on routing around the gate, which was the failure mode measured
    2026-07-26. NOT independently A/B tested: this gate was not observed
    failing, and the line is cheap.
    """
    return (
        f"REJECTED: Tool `{name}` is not available before bootstrap.\n\n"
        ">> CALL THIS NOW:\n"
        "   mcp__okuro__bootstrap(task_hint='<your task>', provider='<your provider>')\n\n"
        ">> Do NOT work around this by importing okuro's Python API directly or\n"
        "   reading ~/.okuro/okuro.db yourself. Bootstrap is what loads the\n"
        "   behavioural contract you are required to follow — skipping it means\n"
        "   working without the rules, not merely without the data.\n\n"
        "Session contract: every session MUST begin with bootstrap so profile, "
        "behavioural contract, tool protocol and session tracking are loaded. "
        "It returns the CORE half; if your task_hint resolves to a project, "
        "bootstrap_project() then returns that project's memory and context, "
        "and this gate stays closed until it does."
    )


def _build_project_half_reject_message(name: str, slug: str) -> str:
    """Rejection when the PROJECT half of the bootstrap packet is still owed.

    Same actor model as the bootstrap gate: the AGENT clears it in one call, so
    no escalation to the user. The message states WHY the packet arrives in two
    pieces, because an agent that thinks it already has everything has no reason
    to make a second call it was not gated into.
    """
    return (
        f"REJECTED: Tool `{name}` is not available until you have fetched the "
        f"PROJECT half of your bootstrap packet.\n\n"
        ">> CALL THIS NOW:\n"
        f"   mcp__okuro__bootstrap_project(slug='{slug}')\n\n"
        "WHY THERE ARE TWO CALLS: the full packet is larger than the 50,000-"
        "character result envelope your host enforces, and over that line the "
        "host discards the ENTIRE result and shows a ~2,000-char preview "
        "instead. So the packet is split. `bootstrap` gave you the "
        "session-invariant core — who the user is, how to behave, what tools "
        f"exist. `bootstrap_project` gives you everything about `{slug}`: "
        "memory, todos, reminders, progress, people, your role assignment, the "
        "codebase map.\n\n"
        ">> You are NOT bootstrapped yet. Half a packet is not a briefing —\n"
        "   proceeding now means working on this project with no memory of it.\n\n"
        "One call clears this for the whole session."
    )


def _build_role_fetch_reject_message(name: str, role_id: str) -> str:
    """Rejection when the assigned-role body has not been loaded via
    roles_get. M5+ — closes the silent lazy-load bypass surfaced by the
    tm-token-cost end-to-end test (0 role_body_fetched events on a 4-min
    spawn that operated on the metadata card alone).

    The actor is the AGENT — one call clears it. As with the bootstrap
    gate, the only element carried over from the freshness rewrite is the
    explicit ban on working around it; not independently A/B tested.
    """
    return (
        f"REJECTED: Tool `{name}` is not available before you adopt your "
        f"assigned role `{role_id}`.\n\n"
        ">> CALL THIS NOW:\n"
        f"   roles_get('{role_id}')\n\n"
        ">> Do NOT work around this by acting on the metadata card in your\n"
        "   prompt. That card is a pointer, not the operating directive — it is\n"
        "   exactly the M4 silent-bypass this gate exists to close (measured: a\n"
        "   4-minute spawn with 0 role_body_fetched events).\n\n"
        "After roles_get returns the role body, all other tools become "
        "available again."
    )


# ---------------------------------------------------------------------------
# Nudges + per-turn rule prefix — moved to nudges.py
# ---------------------------------------------------------------------------
#
# THE SPLIT LINE IS "CAN THIS REFUSE A CALL?". Gates stay in this module and
# can return allow=False; nudges only ride along with a result the caller
# already earned. Re-exported because mcp_tools.py and the middleware tests
# import these names from here.
from okuro.sense.nudges import (  # noqa: F401  (re-export)
    _build_compliance_nudges,
    _build_one_nudge,
    _get_behavioral_pool,
    _nudge_behavioral,
    _nudge_bootstrap_missing,
    _nudge_cortex,
    _nudge_session_end,
    _per_turn_rule_prefix,
    _RULE_PREFIX_ANCHOR_TURNS,
    _RULE_PREFIX_MAX_CHARS,
    _RULE_PREFIX_TAG,
    # Thresholds. Re-exported for the same reason as the functions: tests
    # arrange a session AT a threshold by reading it from here, so dropping
    # them would break callers that never asked for the move. Missing
    # _SESSION_END_THRESHOLD is exactly what the suite caught.
    _CORTEX_NUDGE_THRESHOLD,
    _NUDGE_CHAIN,
    _SESSION_END_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Telemetry logging
# ---------------------------------------------------------------------------

def _log(tool_name: str, arguments: dict, latency_ms: int | None = None, ok: bool = True, preview: str | None = None):
    try:
        from okuro.telemetry.logger import log_call
        log_call(
            server="okuro",
            tool=tool_name,
            latency_ms=latency_ms,
            ok=ok,
            arguments=arguments,
            preview=preview,
        )
    except Exception as exc:
        _guard_failed("telemetry_log_call", exc)


# ---------------------------------------------------------------------------
# Role-handover validators V1-V11 — moved to handover_validators.py
# ---------------------------------------------------------------------------
#
# Re-exported, not re-implemented. Two test modules and any external caller
# import these names from HERE, and a file move is not a reason to break an
# import — so this is the seam that keeps `pre_tool_call` the only place the
# handover contract is enforced while the ~400 lines of it live next door.
from okuro.sense.handover_validators import (  # noqa: F401  (re-export)
    _has_fenced_code,
    _produced_files_for,
    _validate_role_handover,
)


# ---------------------------------------------------------------------------
# Public middleware interface
# ---------------------------------------------------------------------------

# The order the gates in `pre_tool_call` refuse in, declared so it can be read
# and tested in one place instead of inferred from statement sequence.
#
# THE ORDER IS LOAD-BEARING, and its reasons were only ever in six scattered
# comment blocks:
#   * bootstrap first — every later gate's message assumes a briefed reader.
#   * stale_code before project_half — a stale server's refusal must dominate;
#     being told to fetch a half FROM a poisoned process is the wrong
#     instruction.
#   * project_half before assigned_role — the role ASSIGNMENT ships in the
#     project half, so the role gate cannot be satisfied before it lands.
#   * assigned_role before consult — a subagent adopts its role before it is
#     asked to have consulted the brain.
#   * consult before handover_validators — a malformed handover should still be
#     rejected on its own terms, not masked by a gate about something else.
#
# Swapping two of these passes every per-gate test in the suite, because each
# one tests its own gate in isolation. tests/sense/test_gate_composition.py
# asserts this sequence against the source, so a reorder has to be deliberate.
_GATE_ORDER = (
    "bootstrap",
    "stale_code",
    "project_half",
    "assigned_role",
    "consult_before_claim",
    "handover_validators",
)

#: The message builder each gate refuses with, in `_GATE_ORDER` order. The
#: handover validators build their own text inline, hence None.
_GATE_REJECTORS = (
    "_build_bootstrap_reject_message",
    "_build_stale_code_reject_message",
    "_build_project_half_reject_message",
    "_build_role_fetch_reject_message",
    "_build_unconsulted_claim_message",
    None,
)


def pre_tool_call(name: str, arguments: dict) -> tuple[bool, str | None]:
    """Runs before every tool dispatch.

    Returns (allow, message):
      - allow=False: the caller MUST NOT invoke the handler. `message` is the
        rejection text to return to the client. Hit by:
          * the A8 hard gate for unbootstrapped sessions,
          * the role-handover validator chain (V1-V8) which rejects
            malformed write_role_handover calls BEFORE the DB insert.
      - allow=True, message=str: the tool runs; caller prepends `message` to
        the result as a nudge.
      - allow=True, message=None: the tool runs normally.

    Compliance flags are NOT set here — see `post_tool_call`. This prevents
    silent "success" readings when handlers raise.
    """
    # FIRST — decide WHOSE state this call belongs to, before anything reads
    # it. Under stdio the parent and every nested agent share one transport;
    # the provider hook publishes the calling agent_id and this adopts it, so
    # a parent no longer resumes holding a child's flags. No-ops to today's
    # behaviour when the hook is absent.
    try:
        from okuro.sense.session_state import adopt_caller_identity

        adopt_caller_identity()
    except Exception as exc:  # noqa: BLE001 — identity is an optimisation,
        _guard_failed("adopt_caller_identity", exc)         # never a gate

    get_session_state()["tool_calls"] += 1

    # A8 hard gate — reject anything not on the allow-list until bootstrapped.
    if not get_session_state()["bootstrapped"] and name not in _PRE_BOOTSTRAP_ALLOWLIST:
        return False, _build_bootstrap_reject_message(name)

    # Strict-freshness gate (dev only, opt-in). Refuses every tool call from a
    # server whose loaded code is older than the disk. Sits here — before the
    # role gate and before any handler — because a stale process produces
    # confidently wrong results, and a warning was already tried: the drift
    # banner shipped 2026-07-15, fires once at session start, and a 7-hour
    # session still ran an audit against 6-commit-old code. Prevention is the
    # only thing that has not been tried.
    #
    # Wrapped: a freshness check that can raise would break every tool call
    # on the box the moment git misbehaves.
    if name not in _STALE_CODE_ALLOWLIST:
        try:
            from okuro.system.code_version import (
                cached_drift,
                strict_freshness_enabled,
            )

            if strict_freshness_enabled():
                _drift = cached_drift()
                if _drift and not _is_non_interactive_session():
                    # A REFUSED cortex call still counts as having TRIED
                    # cortex, and the grep hook's own rule says so: "grep is
                    # legal AFTER cortex has been tried -- an agent that
                    # already asked and got nothing needs a next move rather
                    # than a wall." It only ever saw the marker written on
                    # SUCCESS, so a drifted session had neither cortex (refused
                    # here) nor grep (hook still blocking) and no sanctioned
                    # way to locate code at all. Measured: it blocked four
                    # independent subagents on 2026-09-09.
                    #
                    # cortex itself stays gated on purpose — it would answer
                    # with this process's frozen ranking code, which is the one
                    # thing the gate exists to prevent. Releasing the FALLBACK
                    # is not the same as un-gating the tool.
                    #
                    # Scoped to this refusal deliberately. A cortex call that
                    # fails for its own reasons must not hand out grep; only a
                    # refusal by a gate the agent cannot clear does.
                    if name.startswith("cortex_"):
                        try:
                            from okuro.sense.session_state import (
                                mark_cortex_used,
                            )

                            mark_cortex_used()
                        except Exception as exc:  # noqa: BLE001
                            _guard_failed("cortex_marker_on_stale", exc)
                    return False, _build_stale_code_reject_message(name, _drift)
        except Exception as exc:  # noqa: BLE001 — never block on the guard's
            _guard_failed("strict_freshness", exc)         # own failure

    # Project-half hard gate — the CORE bootstrap landed but the PROJECT half
    # was not fetched. Sits AFTER the stale-code gate (a stale server's refusal
    # must dominate; being told to fetch a half from a poisoned process would be
    # the wrong instruction) and BEFORE the role gate, because the role
    # ASSIGNMENT itself ships in the project half.
    #
    # Armed only when the core bootstrap resolved a project — see
    # session_state.mark_bootstrapped. No project, no block.
    _pending_half = get_session_state().get("project_half_pending")
    if _pending_half and name not in _PRE_PROJECT_HALF_ALLOWLIST:
        return False, _build_project_half_reject_message(name, _pending_half)

    # M5+ assigned-role hard gate — when this call comes from a session the
    # orchestrator dispatched with an assigned role, reject tool calls until
    # roles_get(<assigned_role>) has been called. Closes the silent M4
    # lazy-load bypass observed during the tm-token-cost end-to-end test.
    # Skipped silently for non-subagent sessions (no role) and for legacy mode.
    #
    # THE ROLE COMES FROM THE RESOLVER, NEVER os.environ. This gate read
    # OKURO_SUBTASK_ROLE off the process environment until 2026-08-03, and the
    # live transport is HTTP to ONE SHARED DAEMON whose environment belongs to
    # no subagent — so `_assigned` was empty on every real call, the gate
    # concluded "not a subagent", and the bypass it exists to close was open on
    # the only path that runs. Nothing reported it: an unarmed gate is
    # indistinguishable from a gate with nothing to catch. The stdio tests kept
    # passing because stdio is the one transport where the environment IS the
    # caller's. Bound per request from the sessions_inline row (migration 128).
    from okuro.sense.work_identity import resolve_subtask_role as _rsr

    _assigned = (_rsr() or "").strip()
    _legacy_roles = os.environ.get("OKURO_LEGACY_ROLE_INJECTION", "").lower() in (
        "1", "true", "yes"
    )
    if (
        _assigned
        and not _legacy_roles
        and not get_session_state().get("assigned_role_fetched")
        and name not in _PRE_ROLE_FETCH_ALLOWLIST
    ):
        return False, _build_role_fetch_reject_message(name, _assigned)

    # Consult-before-claim gate — refuse to make a claim DURABLE until this
    # session has asked the brain. Sits after the role gate and before the
    # handover validators, so a subagent still fetches its role first and a
    # malformed handover is still rejected on its own terms.
    #
    # This is the P0 "ENFORCE: ask the BRAIN before claiming" requirement. Its
    # todo is explicit that gating the TOOL (shell-vs-cortex) is secondary:
    # "Fixing the tool without fixing the consult-before-claim rule leaves the
    # actual defect live." A resource gate cannot see an omission; this catches
    # the omission at the only moment it becomes observable — persistence.
    if (
        name in _CONSULT_BEFORE_CLAIM
        and not get_session_state().get("memory_consulted")
        and os.environ.get("OKURO_ALLOW_UNCONSULTED_WRITE", "").lower()
        not in ("1", "true", "yes")
    ):
        return False, _build_unconsulted_claim_message(name)

    # Role-handover validators (V1-V8) — fire before the storage call so a
    # rejected handover is never persisted and never inserts KG triples.
    # The handler in role_handover.py keeps a thin defence-in-depth check
    # but the canonical contract lives here.
    if name == "write_role_handover":
        rejection = _validate_role_handover(arguments)
        if rejection:
            return False, rejection

    # On bootstrap itself, return None — the bootstrap response already
    # contains the full behavioral section verbatim, so a compact prefix
    # would just duplicate budget. Both layers fire from call 2 onward.
    if name == "bootstrap":
        return True, None

    # Per-turn rule prefix (compact, profile-driven). Throttled: emits on
    # the first _RULE_PREFIX_ANCHOR_TURNS non-bootstrap calls (anchors
    # rules at session start), then only when an episodic nudge also
    # fires. Stacks ABOVE the nudge so the rules stay visually anchored.
    prefix = _per_turn_rule_prefix()
    nudge = _build_one_nudge()

    if prefix:
        state = get_session_state()
        emitted = state.get("prefix_emit_count", 0)
        if emitted < _RULE_PREFIX_ANCHOR_TURNS or nudge:
            state["prefix_emit_count"] = emitted + 1
        else:
            prefix = None

    if prefix and nudge:
        return True, f"{prefix}\n\n{nudge}"
    if prefix:
        return True, prefix
    return True, nudge


def post_tool_call(name: str, arguments: dict, latency_ms: int, ok: bool):
    """Runs after every tool dispatch.

    Logs telemetry and sets compliance flags ONLY on handler success. The
    previous pre-handler pattern set flags before the handler ran, so failed
    writes appeared successful on the scorecard.
    """
    _log(name, arguments, latency_ms=latency_ms, ok=ok)

    # Baseline whatever this call lazily imported, WHILE the bytes on disk are
    # still the bytes it compiled. Runs regardless of ok — a handler that raised
    # may still have completed its imports first.
    #
    # This is the honest moment and the only one: most of okuro's tool surface
    # imports lazily inside handle_tool, so a module first becomes visible to
    # the drift machinery here. Leaving it to the next call's pre_tool_call
    # meant the agent could edit that module in between, and the check would
    # then baseline the POST-EDIT bytes — recording code the process never
    # loaded and blinding the gate to that edit permanently. Measured, and the
    # reason canon_validate and memory_stale both served pre-fix results while
    # the gate reported clean.
    try:
        from okuro.system.code_version import note_imports
        note_imports()
    except Exception as exc:
        _guard_failed("note_imports", exc)

    # Agent heartbeat — stamp on every tool call regardless of ok. The process
    # is alive whether or not the handler succeeded. No-op if provider unknown.
    # Must never raise — wrapped so a DB hiccup cannot break tool dispatch.
    try:
        from okuro.sense.agents import upsert_heartbeat
        provider = get_session_state().get("provider", "unknown")
        if provider and provider != "unknown":
            upsert_heartbeat(provider=provider)
    except Exception as exc:
        _guard_failed("agent_heartbeat", exc)

    if not ok:
        return

    if name == "read_memory":
        # Clears the consult-before-claim gate for the rest of the session.
        # Set here (post-handler, ok=True only) rather than in pre_tool_call so
        # a read that RAISED cannot satisfy the gate — the same reason the
        # compliance flags moved here.
        get_session_state()["memory_consulted"] = True
    elif name == "write_memory":
        get_session_state()["memory_written"] = True
    elif name == "log_progress":
        get_session_state()["progress_logged"] = True
    elif name == "session_report":
        get_session_state()["session_reported"] = True
    elif name == "write_role_handover":
        get_session_state()["handover_written"] = True
    elif name == "delivery_send":
        get_session_state()["delivery_sent"] = True
    elif name == "artifact_write":
        get_session_state()["artifact_written"] = True
    elif name == "roles_get":
        # M5+ — lift the assigned-role hard gate when the requested role
        # matches the role the orchestrator assigned this session. Same
        # resolver as the arm site above, and for the same reason: read from
        # os.environ this was inert on HTTP, so the gate could neither arm nor
        # lift and the pair looked consistent while doing nothing.
        from okuro.sense.work_identity import resolve_subtask_role as _rsr

        assigned = (_rsr() or "").strip()
        requested = (arguments or {}).get("role_id", "")
        if assigned and requested == assigned:
            get_session_state()["assigned_role_fetched"] = True
    elif name.startswith("cortex_"):
        get_session_state()["cortex_used"] = True
        # Publish it for the provider hooks. check-grep.py is a separate
        # process and cannot read this dict, so without the marker it can only
        # block unconditionally — stricter than the rule, which permits grep
        # AFTER cortex has been tried. See session_state.cortex_used_marker_path.
        from okuro.sense.session_state import mark_cortex_used

        mark_cortex_used()
