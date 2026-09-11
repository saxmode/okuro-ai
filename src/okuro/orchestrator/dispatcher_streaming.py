# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Session-loop dispatch path (PR 3) — spawns subagent as a
#   long-lived bridge_stream session, runs critic+scorer PER artifact_write,
#   and publishes verdicts to the review-queue so the same subagent
#   patches in place instead of being respawned cold on FAIL.
# index:
#   imports
#   constants
#   def dispatch_subtask_streaming
#   helpers: _scan_for_artifact_writes / _make_artifact_phase / _publish_for_artifact
# AGENT_HEADER_END -->
"""Session-loop dispatcher.

PR 3 of 4 in the session-loop retry rework. Provides
:func:`dispatch_subtask_streaming` — a drop-in alternative to
:func:`okuro.orchestrator.dispatcher.dispatch_subtask` that keeps the
spawned subagent's session ALIVE across review rounds.

Flow (provider-agnostic — entirely behind the okuro bridge layer):

1. ``StreamRegistry.create()`` spawns the subagent CLI as a long-lived
   stream session and returns a ``session_id``.
2. The engine polls ``registry.events(session_id, since_seq=...)`` on
   a wall-clock cap. For each ``tool_call_start{tool_name="artifact_write"}``
   it remembers the ``call_id``; the matching ``tool_call_done`` carries
   the artifact id in ``result`` (``artifact_write`` returns the bare
   uuid string).
3. For each artifact id seen, the engine builds a per-artifact phase
   shim, runs the existing M3 ``run_review`` pipeline (deterministic +
   critic + scorer all collapse into ONE verdict — that is the
   reconciliation point), and calls
   :func:`okuro.sense.review_queue.publish_review` exactly once on the
   ``(subtask_id, artifact_id)`` key.
4. The subagent's MCP ``await_review`` call (PR 2) wakes on that
   publish, returns the verdict, and the subagent either patches and
   writes a new artifact (which we then review) or calls
   ``session_report`` + exits on PASS / CAP.
5. The engine cancels the stream session on teardown
   (``registry.cancel(session_id)``) and returns a result dict in the
   same shape as the legacy :func:`dispatch_subtask`.

Provider-agnostic — strict. No claude / codex / gemini conditionals
live in this file; the bridge adapter abstraction handles all of that.

TRANSPORT MAP — READ THIS BEFORE CHANGING ANY REVIEW TRIGGER
============================================================

A subagent does NOT speak to this dispatcher over one channel. It speaks
over four, and a trigger wired to only one of them is dead for real
subagents. Three production deadlocks in a single day (2026-07-30) were
all the same mistake in a different channel.

+---+---------------------+--------------------------------+-------------------+
| # | Channel             | Carries                        | Read by           |
+---+---------------------+--------------------------------+-------------------+
| 1 | Bridge stream       | tool_call_start / _done for    | the event loop in |
|   | registry            | calls made INSIDE the bridge   | dispatch_subtask_ |
|   |                     | stream session                 | streaming         |
+---+---------------------+--------------------------------+-------------------+
| 2 | tool_invocations    | EVERY okuro MCP call the       | _poll_db_for_     |
|   | table (stdio MCP)   | subagent makes. THIS IS THE    | await_review /    |
|   |                     | PATH REAL SUBAGENTS USE — the  | _poll_db_for_     |
|   |                     | okuro stdio server is attached | session_report    |
|   |                     | via --mcp-config and writes    |                   |
|   |                     | straight to the brain,         |                   |
|   |                     | BYPASSING channel 1 entirely   |                   |
+---+---------------------+--------------------------------+-------------------+
| 3 | artifacts table     | artifact rows channel 1 missed | _poll_db_for_new_ |
|   |                     |                                | artifacts         |
+---+---------------------+--------------------------------+-------------------+
| 4 | review_queue table  | the VERDICT, back to the       | the subagent's    |
|   |                     | subagent, keyed                | await_review      |
|   |                     | (subtask_id, artifact_id)      |                   |
+---+---------------------+--------------------------------+-------------------+

RULES THAT FALL OUT OF THIS — each one is a fixed production bug:

* Every signal a review trigger depends on needs a fallback on EVERY
  channel it can arrive on, tested per channel, INCLUDING round >= 2.
  A test that drives only channel 1 proves nothing; ten such tests
  passed while the real path was dead.
* Channel 4 is keyed on artifact_id and the SUBAGENT chooses which id it
  awaits. One review round must therefore publish to every artifact in
  the closed-out batch. Coalescing the review is correct; coalescing the
  publish deadlocks whoever awaited a different id.
* One-shot guards must be scoped per BATCH, not per session, or round 2
  finds every fallback branch skipped (see _synthetic_closeout_sent).
* A subagent cannot be PUSHED to. await_review long-polls: it blocks on
  an asyncio.Event and returns still_reviewing on a keep-alive so the CLI
  harness does not kill the call. Observed re-call gaps are 1-5 min
  (subagent turn latency), so a verdict published late is felt late.

SESSION OWNERSHIP: one subtask = one live subagent session. Two live
sessions on the same subtask race on channel 4 and the verdicts interleave
unpredictably (observed live).

ENGINE RESTART SEMANTICS: this dispatcher runs inside the ENGINE
subprocess, not the API. Restarting okuro-orchestrator does NOT move a
running task onto new code, and engine_restart_requested (source_change)
does not reliably re-exec. To move a live task: SIGKILL the launcher AND
the engine (SIGTERM is ignored), then POST /api/tasks/{id}/resume, and
confirm via the engine_version event, which carries the git sha the fresh
engine loaded.

TRANSPORT DECISION (ROCK-SOLID v5 P6.8) — long-poll IS the contract, and
this is the record of why, so it is not re-litigated as a latency finding
every time someone reads the timings.

A subagent CLI has no inbound channel. Nothing can call into a running
`claude`/`codex` process between turns; the only thing the harness will wait
on is a tool call the subagent itself made. So `await_review` blocking is not
a polling workaround for a missing push — it is the single point in the
process where the subagent is reachable at all. Replacing it with a push
transport would require an inbound channel that does not exist.

Two properties follow, and both are already enforced elsewhere:

* A keep-alive is NEVER a submission. `await_review` returns
  ``still_reviewing`` on the keep-alive path; the cursor invariant on the
  review watermark is what stops that return from advancing anything. A
  keep-alive that counted as a round would burn the retry budget on the
  transport rather than on the work.
* The verdict is felt late by exactly the re-call gap. Measured on
  task-20260730-233206: 82 `await_review` calls across 38 review rounds, so
  ~2.2 calls per round, and observed gaps of 1-5 min are subagent turn
  latency, not queue time. A verdict published one second after a keep-alive
  returns waits a full turn to be seen.

That second property is a real cost and it is NOT fixable at the transport.
It is fixable by having fewer rounds, which is what P6.5 (worklist sized by
what decides the verdict) and P6.7 (fast-track) address instead.

This is the sole execution-subtask dispatch path: both engine call
sites in ``engine.py`` submit ``dispatch_subtask_streaming`` directly.
"""

from __future__ import annotations

import json
import json as _json  # explicit alias for the activity-sink writer
import logging
import os
import re
import sqlite3
import time
from datetime import datetime as _dt
from okuro.clock import utc_now_naive
from pathlib import Path as _Path
from typing import Any, Callable, Dict, List, Optional

from okuro.orchestrator.config import Config, resolve_role, resolve_tier_model
from okuro.orchestrator.state import Subtask, Task
from okuro.db.engine import okuro_home

logger = logging.getLogger("okuro.orchestrator.dispatcher_streaming")


# ---------------------------------------------------------------------------
# Constants — kept distinct from the legacy ladder; tuned for session-loop
# semantics (the subagent can long-poll for up to 30 min inside one round,
# and may go several rounds, so the total wall-clock budget is higher).
# ---------------------------------------------------------------------------

_DEFAULT_SESSION_WALL_CLOCK_S: float = 3600.0  # 60 min total budget
_DEFAULT_POLL_INTERVAL_S: float = 0.5
_DEFAULT_PROVIDER: str = "claude"
# Maximum review rounds before we declare CAP and tear down even if the
# subagent keeps writing. Mirrors the legacy ``max_retries`` ladder so a
# runaway loop can't burn unbounded review cycles.
_DEFAULT_MAX_ROUNDS: int = 5
# D3a — GLOBAL per-subtask ceiling on REVISION rounds, across every deliverable.
#
# ``_DEFAULT_MAX_ROUNDS`` bounds ONE family. A subtask shipping several distinct
# deliverables therefore had no total bound: N families x 5 rounds, so the more
# a subagent wrote the more review budget it bought.
#
# COUNTED IN REVISIONS, NOT WRITES, and that distinction is load-bearing. The
# `rounds` counter increments per artifact_write, and the two-streams protocol
# REQUIRES at least two writes per subtask (user report + AC evidence). A
# ceiling of 2 applied to `rounds` would therefore fire on the first pass of
# every fully compliant subtask, before any review had failed. That is exactly
# the conflation the L3 change removed when it moved the cap trigger off
# `rounds` and onto the family; re-introducing it here would undo that fix.
#
# A revision is any review of a deliverable AFTER its first: sum over families
# of (rounds_in_family - 1). First looks are free, re-looks are budgeted.
#
# 2 because both existing convergence signals need two rounds to say anything —
# the convergence detector requires `rounds >= 2` before it can declare
# convergence, and the stall detector needs 2 non-improving rounds to trip.
# Past that the loop is buying rounds whose only reachable outcomes are
# "converged" or "stalled", and the escalation surface serves both better than
# another judge call.
_DEFAULT_MAX_SUBTASK_REVISIONS: int = 2
# Theme A — subagent-silent watchdog. Genuine-HANG protection only:
# force-CAP a session that has produced ZERO stream activity for this
# long after at least one artifact landed, so a blocked `await_review`
# wakes and the dispatch tears down instead of sitting until the 60-min
# wall-clock. Without it the engine sat in futex_do_wait 10-30 min.
#
# LIVENESS (WP2b): the watchdog idle timer (``last_observed_event_ts``)
# is reset on ANY non-empty registry event batch — which includes
# partial-message ``token`` deltas (the adapter spawns claude with
# ``--include-partial-messages``; ``token`` is in UNIFIED_EVENT_TYPES so
# it is surfaced by ``poll_events``), ``tool_call_start`` /
# ``tool_call_done``, assistant ``message_stop`` text, and ``usage``.
# Only TRUE silence — no stream event of any kind AND no new DB artifact
# — accrues toward the threshold. A subagent that is reasoning/streaming
# is therefore ALIVE-BUT-WORKING and never force-CAPped.
#
# Threshold: 60s was too tight — a single long reasoning/tool step
# (model thinking with no partial token flush, a slow MCP tool call,
# cortex re-index) routinely exceeds 60s of true quiet and tore down
# healthy subagents. 600s (10 min) sits well under the 3600s wall-clock
# budget (leaves the engine 6x headroom to still escape a real hang) but
# far above any plausible single quiet step, so normal long-reasoning
# is never mistaken for a hang. Overridable per-call via
# ``silent_watchdog_s``; kept a module constant (not an ExecutionConfig
# field) because the value is already an injectable function parameter
# and a config-field add would force config-parse + fixture changes
# across the suite for no behavioral gain.
_DEFAULT_SILENT_WATCHDOG_S: float = 600.0
# WP-F — the buffered-but-unreviewed alarm.
#
# All three production deadlocks of 2026-07-30 shared ONE signature: artifacts
# buffered, no review started, subagent polling await_review. None of the
# existing guards could see it. The silent watchdog cannot: the subagent's own
# await_review polls ARE stream activity, so `last_observed_event_ts` keeps
# advancing and it never fires. The only escape was the 3600 s wall clock,
# which burns a retry and reports the wrong cause.
#
# 300 s because a real review round completes in milliseconds (deterministic
# short-circuit) to a few minutes (LLM critic+scorer). Five minutes of
# artifacts-with-no-review is not slowness; it is a lost sentinel.
_DEFAULT_UNREVIEWED_BUFFER_ALARM_S: float = 300.0
# C3 — early-CAP on non-convergence. When a FAIL-looping deliverable's
# LOAD-BEARING finding count fails to strictly decrease across this many
# consecutive review rounds, it is oscillating (not improving) and the
# remaining rounds would burn LLM cost/latency for the same CAP outcome.
# 2 = require two consecutive non-improving rounds before giving up, so a
# deliverable still making progress (decreasing findings) never trips it.
_CONVERGENCE_STALL_ROUNDS: int = 2
# L3 — family key used when an artifact's deliverable identity cannot be
# resolved. Shared on purpose: every unresolved artifact lands in ONE family,
# reproducing the pre-L3 single-budget behaviour. Giving each artifact its own
# family would uncap the loop silently, which is the failure this must not have.
_FAMILY_FALLBACK: str = "__unresolved__"
# D1a — internal tool name for a closeout the dispatcher synthesized. Never
# emitted by a subagent; it exists so the review branch has ONE trigger name
# whatever channel the sentinel arrived on (await_review start, session_report,
# stream end, or the tool_invocations DB poll).
_CLOSEOUT_TOOL: str = "__closeout__"
# WP-C — how a review round is triggered.
#
#   "per_artifact"  every artifact_write is its own closeout: one review and
#                   one verdict per write. The behaviour that predates D1a.
#   "closeout"      artifacts buffer until the subagent signals its submission
#                   is complete (await_review); one review over the batch.
#
# DEFAULT IS per_artifact, deliberately. `closeout` judges a COMPLETE
# submission — strictly better, because reviewing on the first write can fail a
# report for missing evidence the subagent had not written yet. But it shipped
# with three production deadlocks in one day, so it stays opt-in until the
# WP-B gate (`pytest -m e2e`) has been green on the real stdio path twice
# running. Flipping this default is a one-line change AND a decision; do not
# take it silently.
#
# Both modes run the SAME review code path: per_artifact simply synthesizes a
# closeout per write. There is no second pipeline to keep in sync — that
# duplication is what a mode flag usually costs, and it is not paid here.
_REVIEW_TRIGGER_PER_ARTIFACT: str = "per_artifact"
_REVIEW_TRIGGER_CLOSEOUT: str = "closeout"
_DEFAULT_REVIEW_TRIGGER: str = _REVIEW_TRIGGER_PER_ARTIFACT
_PASS_VERDICTS: frozenset[str] = frozenset({"PASS"})
_TERMINAL_VERDICTS: frozenset[str] = frozenset({"PASS", "CAP", "NEEDS_USER"})

# Transient, server-side/API errors that are RETRY-able and must NOT be treated
# as content failures. A subagent (or reviewer) session that dies on an
# Anthropic 5xx / overloaded / rate-limit must not burn the small content-retry
# budget and then permanent-fail + cascade-skip the whole downstream subtree
# over a server blip. Observed: task-20260616 subtask 2.1 died 3x on
# "API Error: 500 Internal server error" → cascade-blocked the entire task.
# Mirrors the Critic-stage ``infra_error`` contract (one matcher — DP10).
_TRANSIENT_INFRA_MARKERS: tuple[str, ...] = (
    "api error: 5",            # API Error: 500/502/503/504/529
    "internal server error",
    "overloaded", "529",
    "rate limit", "rate_limit", "429",
    "502", "503", "504",
    "service unavailable",
    "bad gateway", "gateway timeout",
    "connection error", "connection reset", "econnreset",
    "server error",
)


def is_transient_infra_error(text: str | None) -> bool:
    """True when ``text`` looks like a transient server-side/API error that
    should be retried as infra — NOT counted as a content failure."""
    if not text:
        return False
    t = str(text).lower()
    return any(m in t for m in _TRANSIENT_INFRA_MARKERS)


def _finding_id(f: Dict[str, Any]) -> str:
    """Stable identity for one finding, so rounds can be compared by WHAT was
    said rather than by HOW MANY things were said.

    The count-based stall detector cannot tell "the same two findings again"
    from "two completely different findings" — both read as a flat count. That
    blindness is what let a review churn indefinitely: each round resolved some
    items and raised new ones, keeping the count level while never converging.
    """
    subject = (
        f.get("term") or f.get("summary") or f.get("suggested_fix")
        or f.get("title") or f.get("message") or ""
    )
    locus = str(f.get("file") or f.get("locus") or "")
    # Normalised so trivial rewording of the same complaint stays one identity.
    subject = re.sub(r"\s+", " ", str(subject).strip().lower())[:200]
    return f"{locus}|{f.get('line') or ''}|{subject}"


def _ledger_merge(
    ledger: Dict[str, Dict[str, Any]], findings: list, round_no: int,
) -> tuple[set, set]:
    """Fold one round's findings into the cumulative ledger.

    Returns ``(new_ids, reopened_ids)``:
      new_ids      — never seen in any prior round. An empty set means the
                     review has CONVERGED: it has no further information to
                     add, whether or not open items remain.
      reopened_ids — previously marked resolved and now raised again. That is
                     oscillation, and no amount of further rounds fixes it.

    Anything in the ledger but absent from this round is marked resolved: the
    critic ran a verification pass over it and did not re-flag it.
    """
    seen_now = {}
    for f in findings or []:
        if isinstance(f, dict):
            seen_now[_finding_id(f)] = f

    new_ids, reopened_ids = set(), set()
    for fid, f in seen_now.items():
        entry = ledger.get(fid)
        if entry is None:
            ledger[fid] = {
                "finding": f, "state": "open",
                "first_round": round_no, "times_raised": 1,
            }
            new_ids.add(fid)
            continue
        entry["finding"] = f
        entry["times_raised"] += 1
        if entry["state"] == "resolved":
            entry["state"] = "open"
            reopened_ids.add(fid)

    for fid, entry in ledger.items():
        if fid not in seen_now and entry["state"] == "open":
            entry["state"] = "resolved"
            entry["resolved_round"] = round_no
    return new_ids, reopened_ids


def _ledger_open(ledger: Dict[str, Dict[str, Any]]) -> list:
    """Every still-open finding — what the next round must verify.

    The loop previously handed the critic only the LAST round's findings, so an
    item resolved in round 2 fell out of scope by round 4 and could be re-raised
    as if new. Passing the full open set keeps every live item under
    verification for the whole loop.
    """
    return [e["finding"] for e in ledger.values() if e["state"] == "open"]


def _convergence_stall(
    prev_lb: Optional[int], lb: int, streak: int,
) -> tuple[int, bool]:
    """C3 — update the non-convergence streak for a FAIL-looping subtask.

    ``prev_lb`` / ``lb`` are the previous and current round's LOAD-BEARING
    finding counts; ``streak`` is the running count of consecutive
    non-improving rounds. A round whose load-bearing count did NOT strictly
    decrease extends the streak; a decrease resets it. Returns
    ``(new_streak, stalled)`` where ``stalled`` is True once the streak
    reaches :data:`_CONVERGENCE_STALL_ROUNDS` — the signal to force CAP
    early instead of burning the remaining rounds on oscillating findings.
    """
    if prev_lb is not None and lb >= prev_lb:
        streak += 1
    else:
        streak = 0
    return streak, streak >= _CONVERGENCE_STALL_ROUNDS


def _mirror_registry_event_to_activity(
    evt: Dict[str, Any],
    emit: Callable[[Dict[str, Any]], None],
) -> None:
    """Translate one bridge-registry event into an ActivityEvent row and
    append via ``emit``. Best-effort: unknown event types are skipped
    rather than raised — the FE feed must never block the dispatch loop.

    Registry → Activity shape mapping:

    | Registry type      | Activity type      | Preview source            |
    |--------------------|--------------------|---------------------------|
    | tool_call_start    | tool_use           | args (truncated path/cmd) |
    | tool_call_done     | (suppressed)       | start already shown       |
    | message_stop+text  | text               | full assistant text       |
    | token (delta)      | (suppressed)       | too noisy per-token       |
    | usage              | result             | input/output tokens       |
    | status / error     | text               | the message text          |
    | thinking_*         | thinking           | thinking text             |
    """
    et = evt.get("type")
    if et == "tool_call_start":
        args = evt.get("args") or {}
        # Best preview: file_path > command > pattern > query > summary
        # > content > path > url — matches dispatcher.py's fallback ladder
        # so the FE renders the same human-readable hint either way.
        if isinstance(args, dict):
            preview = (
                args.get("file_path")
                or args.get("command")
                or args.get("pattern")
                or args.get("query")
                or args.get("summary")
                or args.get("content")
                or args.get("path")
                or args.get("url")
                or ""
            )
        else:
            preview = str(args)[:1000]
        emit({
            "type": "tool_use",
            "name": evt.get("tool_name") or "?",
            "preview": preview,
            "call_id": evt.get("call_id"),
        })
    elif et == "message_stop":
        text = evt.get("text") or ""
        if text.strip():
            emit({"type": "text", "text": text})
    elif et in ("thinking", "thinking_delta"):
        text = evt.get("text") or evt.get("delta") or ""
        if text.strip():
            emit({"type": "thinking", "preview": text})
    elif et == "usage":
        emit({
            "type": "result",
            "success": True,
            "input_tokens": evt.get("input_tokens", 0),
            "output_tokens": evt.get("output_tokens", 0),
            "cost_usd": evt.get("cost_usd", 0),
        })
    elif et == "error":
        msg = evt.get("message") or evt.get("error") or "error"
        emit({"type": "text", "text": f"[error] {msg}"})
    # tool_call_done / token / status / message_start: intentionally skipped
    # tool_call_done is implied by the next tool_call_start or by the
    # final result; tokens are too noisy for the feed; status / message_start
    # are not user-visible.


def dispatch_subtask_streaming(
    subtask: Subtask,
    task: Task,
    config: Config,
    session_id: Optional[str] = None,
    effective_project_path: "Optional[Any]" = None,
    *,
    # Test/extension seams — never invoked by production engine callers.
    registry: Optional[Any] = None,
    review_runner: Optional[Any] = None,
    publish_review_fn: Optional[Any] = None,
    emit_event_fn: Optional[Any] = None,
    wall_clock_s: float = _DEFAULT_SESSION_WALL_CLOCK_S,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    max_rounds: int = _DEFAULT_MAX_ROUNDS,
    max_subtask_revisions: int = _DEFAULT_MAX_SUBTASK_REVISIONS,
    silent_watchdog_s: float = _DEFAULT_SILENT_WATCHDOG_S,
    unreviewed_buffer_alarm_s: float = _DEFAULT_UNREVIEWED_BUFFER_ALARM_S,
    provider: Optional[str] = None,
    clock: Optional[Any] = None,
    review_trigger: Optional[str] = None,
) -> Dict:
    """Session-loop dispatch path.

    Spawns the subagent as a long-lived ``bridge_stream`` session, watches
    for ``artifact_write`` tool calls in the unified event stream, runs the
    M3 reviewer pipeline per artifact, and publishes verdicts to the
    PR 1 review-queue so the subagent's ``await_review`` MCP call (PR 2)
    wakes and patches in place.

    Returns a dispatch result dict matching the legacy
    :func:`dispatch_subtask` shape::

        {"success": bool, "output": str, "duration": float,
         "cli": str, "model": str, "error": str,
         "rounds": int, "artifact_ids": list[str], "verdicts": list[dict]}

    The extra ``rounds`` / ``artifact_ids`` / ``verdicts`` keys are
    additive — legacy callers reading just the first six keys see no
    change.
    """
    result: Dict[str, Any] = {
        "success": False, "output": "", "duration": 0.0,
        "cli": "", "model": "", "error": "",
        "rounds": 0, "artifact_ids": [], "verdicts": [],
    }

    # --- P4.2: the subtask lease ------------------------------------------
    #
    # Refuse to spawn a second subagent for work that already has a live one.
    # This is the check P4.1 existed to make possible — before sessions carried
    # a work identity the question "is something already running for
    # (task, subtask)?" had no answer, so duplicate dispatch was prevented by
    # ordering and hope. Two live subagents on one subtask both write
    # artifacts, and the reviewer grades whichever landed last.
    #
    # Placed HERE rather than at the two engine call sites: this is the one
    # function both loops go through, so a third caller inherits the lease
    # instead of re-implementing it.
    #
    # LOUD, not silent. Per the plan's own rule that a manual unblock is a
    # diagnostic: if this ever refuses wrongly (a recycled pid reading as
    # live), the operator needs to see WHY rather than watch a subtask
    # quietly never start.
    try:
        from okuro.sense.telemetry import live_sessions_for_subtask

        _held = live_sessions_for_subtask(task.id, subtask.id)
    except Exception as _lease_exc:  # noqa: BLE001
        # A telemetry failure must not block real work. Degrades to the
        # pre-P4.2 behaviour (dispatch anyway), which is what it was.
        logger.warning(
            "[%s/%s] lease check unavailable (%r) — dispatching without it",
            task.id, subtask.id, _lease_exc,
        )
        _held = []

    if _held:
        _holder = _held[0]
        result["error"] = (
            f"subtask already has a live session "
            f"({_holder.get('session_id', '?')}, pid {_holder.get('pid', '?')}, "
            f"started {_holder.get('started_at', '?')}) — refusing to spawn a "
            "second one"
        )
        logger.error("[%s/%s] %s", task.id, subtask.id, result["error"])
        try:
            from okuro.orchestrator.state import emit_event as _emit

            _emit(task.id, "dispatch_refused_lease", {
                "subtask_id": subtask.id,
                "held_by_session": _holder.get("session_id", ""),
                "held_by_pid": _holder.get("pid"),
                "held_since": _holder.get("started_at", ""),
                "live_count": len(_held),
            }, config.tasks_dir)
        except Exception as _emit_exc:  # noqa: BLE001
            logger.warning("dispatch_refused_lease emit failed: %r", _emit_exc)
        result["duration"] = 0.0
        return result

    # --- resolve role / prompt ---------------------------------------------
    try:
        role_content = resolve_role(subtask.role, config)
    except (ValueError, FileNotFoundError) as e:
        result["error"] = f"Role resolution failed: {e}"
        logger.error(f"[{task.id}/{subtask.id}] {result['error']}")
        return result

    # Reuse the legacy prompt builder verbatim — PR 4 owns subagent prompt
    # changes (mandatory ``await_review`` directive etc.). PR 3 simply
    # changes the dispatch transport, not the prompt content.
    from okuro.orchestrator.dispatcher import build_role_prompt
    prompt = build_role_prompt(
        role_content, subtask, task, config,
        session_id=session_id,
        effective_project_path=effective_project_path,
    )

    # --- resolve provider + adapters --------------------------------------
    chosen_provider = provider or _resolve_provider(task, config)
    result["cli"] = chosen_provider

    # Resolve the per-tier model for the chosen provider from config's
    # tier_map (provider-agnostic). Without this registry.create() falls back
    # to _default_model_for(provider): harmless for claude (sonnet) / gemini
    # (gemini-2.5-pro), but for codex it yields `gpt-5-codex`, which a
    # ChatGPT-account login REJECTS ("model is not supported when using Codex
    # with a ChatGPT account") AND bypasses codex's reasoning-effort tiering.
    # Tier comes from the SUBTASK's own complexity (shared rule in
    # config.resolve_unit_tier), with intelligence=max upgrading but never
    # downgrading — so a drawn workflow's per-node tier is honoured. An explicit
    # subtask.model_override still wins over all of it. For the common standard
    # tier this resolves to exactly the registry default for claude/gemini — no
    # behavior change — while giving codex `model_reasoning_effort=<level>` as
    # the config intends.
    from okuro.orchestrator.config import resolve_unit_tier

    _tier = resolve_unit_tier(subtask, task)
    resolved_model = (getattr(subtask, "model_override", "") or "").strip() or (
        resolve_tier_model(config, _tier, chosen_provider)
    )
    result["model"] = resolved_model

    if registry is None:
        from okuro.bridge.streaming import get_registry
        registry = get_registry()

    if publish_review_fn is None:
        from okuro.sense.review_queue import publish_review as publish_review_fn  # type: ignore

    if review_runner is None:
        from okuro.orchestrator.reviewer.pipeline import run_review as review_runner  # type: ignore

    if emit_event_fn is None:
        from okuro.sense.task_events import append_event as emit_event_fn  # type: ignore

    if clock is None:
        clock = time.monotonic

    # --- activity sink ------------------------------------------------------
    # The legacy path (execute_streaming) wrote normalized rows to the
    # task's .activity.jsonl so the FE activity feed could render the
    # subagent's tool calls + thinking live. The session-loop path
    # bypasses execute_streaming and reads from the bridge registry — we
    # must mirror those events into .activity.jsonl ourselves or the FE
    # stays empty for the entire session.
    try:
        _activity_path = config.tasks_dir / task.id / ".activity.jsonl"
        _activity_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        _activity_path = None

    def _emit_activity(row: Dict[str, Any]) -> None:
        if _activity_path is None:
            return
        try:
            row.setdefault("ts", _dt.utcnow().isoformat())
            row.setdefault("subtask_id", subtask.id)
            row.setdefault("role", subtask.role)
            with open(_activity_path, "a") as af:
                af.write(_json.dumps(row, default=str) + "\n")
        except Exception:
            pass

    # Per-subtask "reviewing" flag. The M3 phase-gate writes a single
    # .review-active.json (engine.py); the in-session loop reviews ONE
    # artifact at a time, so it stamps a per-subtask
    # .review-active.<subtask_id>.json that state_reader unions into
    # review_active_subtasks → the EXISTING "Reviewing" tile badge lights up
    # while THIS node's artifact is being judged. Without it the node sat at
    # "running" with no clue it was mid review→correct loop. Per-subtask file
    # (not the shared phase flag) so parallel sibling sessions never race on
    # one path. Cleared in a finally + reaped by reconcile_on_startup.
    def _set_reviewing(active: bool) -> None:
        try:
            flag = config.tasks_dir / task.id / f".review-active.{subtask.id}.json"
            if active:
                flag.parent.mkdir(parents=True, exist_ok=True)
                try:
                    _phase_id = int(str(subtask.id).split(".")[0])
                except (ValueError, IndexError):
                    _phase_id = 0
                flag.write_text(_json.dumps({
                    "phase_id": _phase_id,
                    "subtask_ids": [subtask.id],
                    "since": _dt.utcnow().isoformat(),
                    "source": "session_loop",
                }))
            else:
                flag.unlink(missing_ok=True)
        except Exception:
            pass

    # --- spawn session ------------------------------------------------------
    stream_session_id: Optional[str] = None
    started_at = clock()
    try:
        stream_session_id = registry.create(
            provider=chosen_provider,
            model=resolved_model or None,
            messages=[{"role": "user", "content": prompt}],
            todo_id=None,
            # Orchestrator subagents have no human at the approval gate.
            # Without is_agentic=True, every write-tier MCP tool the
            # subagent calls (artifact_write / log_progress /
            # session_report / write_role_handover / write_memory) waits
            # 300s for human approval that never comes and returns
            # approval_denied — invisible to the engine, looks like the
            # subagent silently failed. Root cause of ~all 20 task
            # failures audited 2026-05-29.
            is_agentic=True,
            # ORCH-PARALLEL-TREE: when the engine assigned an isolated
            # worktree, the subagent's process cwd MUST be that worktree
            # — not just a path mentioned in the prompt. Without this the
            # subprocess inherits the orchestrator's cwd (the real working
            # tree) and parallel subagents race on a shared index. When no
            # worktree was assigned, leave cwd None (inherit) to preserve
            # prior single-tree behavior.
            cwd=str(effective_project_path) if effective_project_path else None,
            # ROCK-SOLID v5 P4.1/P4.4 — the work this session is dispatched to
            # do. Bound to the session ROW (and from there to the per-session
            # bearer token every MCP call carries), not only exported into the
            # CLI's environment: the daemon that answers those calls is shared
            # by every subagent and inherits no subagent's environment, which
            # is why the env-only version left the lease, the reap and the
            # epoch fence permanently inert. registry.create derives the env
            # vars from this same dict, so the two channels agree by
            # construction.
            #
            # The epoch is taken HERE, at spawn, not read from a shared
            # place: two subtasks dispatched concurrently need different
            # epochs, and a process-global would give them the same one.
            #
            # ROUND 2 (migration 128) adds subtask_role / agent_provider /
            # session_type. subtask_role is not decoration: it ARMS the M5+
            # assigned-role hard gate. The legacy dispatcher exported it as
            # OKURO_SUBTASK_ROLE on the subprocess; THIS path passed no env at
            # all, and the daemon that answers the subagent's calls has none
            # either — so the gate never armed for any subagent spawned here,
            # which is every subagent. The row is the only channel that works.
            work_identity={
                "task_id": task.id,
                "subtask_id": subtask.id,
                "dispatch_epoch": utc_now_naive().isoformat(),
                "subtask_role": getattr(subtask, "role", "") or "",
                "agent_provider": (
                    f"orch-{subtask.role}" if getattr(subtask, "role", "") else ""
                ),
                "session_type": "subagent",
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[%s/%s] streaming session spawn failed", task.id, subtask.id,
        )
        result["error"] = f"bridge_stream_start failed: {exc}"
        result["duration"] = clock() - started_at
        return result

    # Mark a session start so the FE feed shows the new attempt boundary.
    _emit_activity({
        "type": "subtask_start",
        "cmd": chosen_provider,
        "stream_session_id": stream_session_id,
    })

    # --- main event-poll loop ----------------------------------------------
    since_seq = 0
    # call_id -> tool_name; populated on tool_call_start so we can
    # interpret the matching tool_call_done.result without re-scanning.
    pending_calls: Dict[str, str] = {}
    seen_artifact_ids: list[str] = []
    # Wall-clock cutoff for the DB-poll fallback. Sampled BEFORE the
    # subagent stream is created so any artifact_write that races the
    # registry's first poll is still picked up. Pre-existing artifacts
    # from earlier attempts on the same (task, subtask) are excluded
    # so the reviewer doesn't re-judge work the current subagent never
    # produced. The space separator matches SQLite's ``datetime('now')``
    # output so the string comparison ordering is correct — Python's
    # default ``isoformat`` uses ``T`` which sorts AFTER space and would
    # silently exclude every row (artifact.created_at uses space).
    _db_poll_since_iso = _dt.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    # N2 — SEPARATE watermark for the closeout sentinel, advanced on every
    # sentinel this loop consumes.
    #
    # Sharing _db_poll_since_iso was a correctness bug, not untidiness. That
    # value is fixed at session start, so once round 1's await_review row
    # exists it matches the sentinel query FOREVER. With the N1 latch now
    # released per batch, round 2 would then synthesize its closeout the
    # instant the first artifact was buffered — before the subagent had
    # written the rest of its submission, let alone called await_review. That
    # is precisely the "judge an incomplete submission" defect
    # review-on-closeout exists to remove, reintroduced through a stale
    # watermark.
    #
    # Kept separate from the artifact watermark rather than advancing that
    # one: artifacts are polled continuously and moving their cursor at
    # closeout could drop a row written in the same second.
    _sentinel_since_iso = _db_poll_since_iso
    rounds = 0
    final_verdict: Optional[str] = None
    # Stateful verification: the prior round's findings handed to the next
    # reviewer pass so it verifies each (resolved/still-open) instead of
    # re-deriving. Seeded from the subtask's carried worklist so a retry's
    # FIRST pass verifies the exact findings the user was blocked on.
    prev_findings: list = list(getattr(subtask, "review_findings", None) or [])
    # Cumulative finding ledger, keyed by finding identity. `prev_findings` used
    # to be REPLACED each round, so the critic only ever saw the last round and
    # an item it accepted as resolved could silently return two rounds later —
    # the churn that made this loop non-terminating. The ledger keeps every
    # finding under verification for the whole loop and makes "did this round
    # say anything NEW" answerable.
    finding_ledger: Dict[str, Dict[str, Any]] = {}
    _ledger_merge(finding_ledger, prev_findings, 0)
    converged_open: list = []
    # C3 — non-convergence tracking across review rounds, keyed BY FAMILY
    # (L3). A subtask may ship several distinct deliverables; each gets its
    # own round budget and its own convergence trajectory.
    prev_lb_count: Dict[str, int] = {}
    stall_streak_by_family: Dict[str, int] = {}
    stall_streak = 0
    # artifact_id -> family key, and family key -> rounds spent on it.
    artifact_families: Dict[str, str] = {}
    family_rounds: Dict[str, int] = {}
    family = _FAMILY_FALLBACK
    output_lines: list[str] = []
    deadline = started_at + float(wall_clock_s)
    # Theme A — last observation timestamp = liveness signal. Reset on
    # ANY non-empty registry event batch (partial-message ``token``
    # deltas, ``tool_call_start`` / ``tool_call_done``, assistant text,
    # ``usage`` — every UNIFIED_EVENT_TYPE the adapter surfaces) OR any
    # new artifact picked up by the DB poll. A subagent that is
    # reasoning/streaming keeps this fresh and is ALIVE-BUT-WORKING; only
    # total silence accrues toward the silent-watchdog force-CAP below.
    last_observed_event_ts = started_at
    silent_watchdog_fired = False
    # M5+ per-spawn token + cache accounting. Filled from `usage` events and
    # emitted once, at the end, as a `spawn_usage` task_event.
    usage_totals: Dict[str, Any] = {}
    # WP8 — no-artifact pre-finalize reprompt guard. When the session
    # reaches a terminal/done state having NEVER written a Stream-B
    # artifact, we publish ONE corrective into the still-open session and
    # grant one more bounded round before falling through to the existing
    # "subagent never wrote an artifact" failure. One-shot flag → at most
    # one extra round, no infinite loop. The deadline extension is capped
    # at one poll budget so a dead/terminal session cannot stall the slot.
    no_artifact_corrective_sent = False
    # D1a — review fires on subtask CLOSEOUT, not per artifact_write.
    #
    # The two-streams protocol requires TWO artifacts (user report + AC
    # evidence). Reviewing on the FIRST write therefore judged an incomplete
    # submission: the critic saw a report with no evidence artifact and could
    # FAIL it for missing exactly the evidence the subagent was about to write,
    # burning a retry round against a deliverable that was never finished.
    #
    # Artifact ids are buffered here and drained when the closeout sentinel
    # arrives, so one complete submission costs one review round. This is a
    # TIMING change only — `artifact_id` never scoped what the reviewer reads
    # (it labels findings and log lines; the phase shim in
    # _run_reviewer_for_artifact hands the reviewer the whole subtask), so the
    # reviewer sees the same deliverable set either way. What changes is
    # WHETHER BOTH ARTIFACTS EXIST YET when it looks.
    #
    # THE SENTINEL IS await_review, not session_report. Deliberately NOT a new
    # MCP tool either — those need a Claude Code session restart and so cannot
    # be verified in the session that adds them.
    #
    # await_review is the right signal because it already MEANS "my submission
    # is complete, judge it": the dispatcher brief tells the subagent to write
    # its artifacts and then call it. So the trigger moves with zero prompt
    # changes, and the existing brief stays correct.
    #
    # session_report was the obvious first choice and is WRONG. The same brief
    # says "Do NOT call session_report yet. Wait for the verdict" and "NEVER
    # finish (session_report) immediately after artifact_write without first
    # awaiting a verdict" — so a subagent following it never emits that
    # sentinel before await_review, and would block forever on a review that
    # was never triggered. It also already carries terminal semantics further
    # down this function, where a session_report seen via DB poll is treated as
    # the subagent surrendering (final_verdict = PASS).
    #
    # It is triggered on tool_call_START, not done: await_review blocks until
    # the verdict it is waiting for exists, so waiting for its completion
    # would deadlock. session_report and stream-end remain soft fallbacks for
    # subagents that skip await_review entirely.
    # WP-C — resolve the trigger mode: explicit arg > config > module default.
    # Resolution order: explicit arg > THIS TASK > global config > default.
    # The task level is what makes a closeout experiment scopeable to one run;
    # without it the only way to try `closeout` is an installation-wide flip
    # that catches every unrelated task in between.
    _trigger = (
        review_trigger
        or getattr(task, "review_trigger", None)
        or getattr(getattr(config, "orchestrator", None), "review_trigger", None)
        or _DEFAULT_REVIEW_TRIGGER
    )
    if _trigger not in (_REVIEW_TRIGGER_PER_ARTIFACT, _REVIEW_TRIGGER_CLOSEOUT):
        logger.warning(
            "[%s/%s] unknown review_trigger %r — falling back to %r",
            task.id, subtask.id, _trigger, _DEFAULT_REVIEW_TRIGGER,
        )
        _trigger = _DEFAULT_REVIEW_TRIGGER
    per_artifact_mode = _trigger == _REVIEW_TRIGGER_PER_ARTIFACT

    pending_artifact_ids: list[str] = []
    # WP-F — when the CURRENT buffer first became non-empty, and a
    # one-shot so the alarm cannot spam a loop it already reported.
    pending_since: Optional[float] = None
    unreviewed_alarm_fired = False
    closeout_seen = False
    _synthetic_closeout_sent = False
    # Events the dispatcher injects into itself (a closeout the stream never
    # carried). Drained into the next batch so they run through exactly the
    # same handler as a real one — no second review code path to keep in sync.
    _pending_synthetic_events: list[dict] = []

    try:
        while clock() < deadline:
            try:
                payload = registry.events(stream_session_id, since_seq=since_seq)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[%s/%s] registry.events raised %r — retrying",
                    task.id, subtask.id, exc,
                )
                time.sleep(poll_interval_s)
                continue

            events = list(payload.get("events") or [])
            # Drain any closeout the dispatcher queued for itself (see
            # _pending_synthetic_events). Prepended so it is handled before
            # whatever the stream returned next.
            if _pending_synthetic_events:
                events = _pending_synthetic_events + events
                _pending_synthetic_events = []
            next_seq = int(payload.get("next_seq") or since_seq)
            done = bool(payload.get("done"))
            if events:
                last_observed_event_ts = clock()

            # Belt-and-suspenders: synthesize tool_call events for any
            # artifact_write rows the registry missed. The okuro stdio
            # MCP server (attached to subagents via --mcp-config) writes
            # rows directly to the brain, bypassing the bridge stream
            # registry. Without this synthesis the dispatcher waits for
            # tool_call_done events that never come, the review queue
            # stays empty, and the subagent's await_review polls until
            # the wall-clock cap.
            for _aid in _poll_db_for_new_artifacts(
                task.id, subtask.id, seen_artifact_ids,
                since_iso=_db_poll_since_iso,
            ):
                _cid = f"db-sync-{_aid}"
                events.append({
                    "type": "tool_call_start",
                    "tool_name": "artifact_write",
                    "call_id": _cid,
                    "_synthetic": True,
                })
                events.append({
                    "type": "tool_call_done",
                    "call_id": _cid,
                    "outcome": "ok",
                    "result": _aid,
                    "_synthetic": True,
                })
                # Theme A — DB-sourced artifact counts as evidence of
                # subagent activity for the silent-watchdog.
                last_observed_event_ts = clock()

            for evt in events:
                # Mirror every registry event into the task's
                # .activity.jsonl in the shape the FE activity feed
                # already consumes. Without this, the session-loop
                # dispatch surface is completely silent — the FE feed
                # ONLY reads .activity.jsonl, not the bridge's internal
                # event queue. Synthetic events skip the mirror so the
                # activity feed isn't duplicated for an artifact_write
                # the FE already saw via the subagent's own tool_use
                # event.
                if not evt.get("_synthetic"):
                    _mirror_registry_event_to_activity(evt, _emit_activity)

                etype = evt.get("type")
                if etype == "tool_call_start":
                    tool_name = (evt.get("tool_name") or "").strip()
                    call_id = evt.get("call_id") or ""
                    if tool_name and call_id:
                        pending_calls[call_id] = tool_name
                    # session_report signals the subagent is wrapping up
                    # cleanly. We let the natural "done" status close the
                    # loop; no special branch needed here, but track it for
                    # telemetry.
                    if tool_name == "session_report":
                        output_lines.append("[session_loop] session_report observed")
                    # D1a — await_review START is the closeout sentinel. Its
                    # DONE never arrives until the verdict exists, and the
                    # verdict is what this triggers, so waiting for it would
                    # deadlock.
                    if tool_name == "await_review" and pending_artifact_ids:
                        closeout_seen = True
                        _sid = f"closeout-{call_id or len(seen_artifact_ids)}"
                        pending_calls[_sid] = _CLOSEOUT_TOOL
                        # Same batch, so the verdict is published before the
                        # subagent's await_review poll comes back round.
                        events.append({
                            "type": "tool_call_done",
                            "call_id": _sid,
                            "outcome": "auto",
                            "_synthetic": True,
                        })
                elif etype == "tool_call_done":
                    call_id = evt.get("call_id") or ""
                    tool_name = pending_calls.pop(call_id, "")
                    if tool_name == "artifact_write":
                        # D1a — BUFFER, do not review. The submission is not
                        # complete until the subagent says it is.
                        if evt.get("outcome") == "error":
                            logger.info(
                                "[%s/%s] artifact_write call_id=%s errored — skipping review",
                                task.id, subtask.id, call_id,
                            )
                            continue
                        _aid = _extract_artifact_id(evt.get("result"))
                        if not _aid:
                            # N3 — the subagent believes it wrote something
                            # and will await an id we never learned. The
                            # artifacts table (channel 3) normally recovers it
                            # on the next poll, and the closeout publish fans
                            # out to every known id, so this is survivable —
                            # but it must be LOUD, because a silent skip here
                            # is indistinguishable from a subagent that never
                            # wrote at all.
                            logger.error(
                                "[%s/%s] artifact_write returned no parseable "
                                "id; result=%r — not buffered. Relying on the "
                                "artifacts DB poll to recover it; the subagent "
                                "may be awaiting an id this loop never saw.",
                                task.id, subtask.id, evt.get("result"),
                            )
                            output_lines.append(
                                "[session_loop] artifact_write result "
                                "unparseable — recovering via the artifacts DB"
                            )
                            continue
                        if not pending_artifact_ids:
                            pending_since = clock()   # WP-F
                        pending_artifact_ids.append(_aid)
                        # A SENTINEL MUST BE NEWER THAN THE NEWEST ARTIFACT.
                        #
                        # The brief tells the subagent to re-call await_review
                        # repeatedly while a verdict is pending ("Immediately
                        # re-call await_review with the SAME args"). EVERY one
                        # of those keep-alive polls writes a tool_invocations
                        # row, and a poll is byte-identical to a submission
                        # signal. Observed live: three polls at 21:44:47 while
                        # round 1's verdict was landing, then the next artifact
                        # buffered at 21:48:38 — the DB poll saw those rows,
                        # fired a closeout at 21:48:50, and reviewed a
                        # submission whose remaining artifacts were still being
                        # written at 21:48:50 and 21:48:54. Four reviews for two
                        # rounds.
                        #
                        # A genuine "my submission is complete" call happens
                        # AFTER the last write; a stale poll precedes it. So
                        # every buffered artifact pushes the sentinel cursor to
                        # the store's own now, and only await_review calls made
                        # after that count.
                        _newest = _artifact_max_created_at(pending_artifact_ids)
                        if _newest and _newest > _sentinel_since_iso:
                            _sentinel_since_iso = _newest
                        # Record it as SEEN at buffer time, not at closeout.
                        # _poll_db_for_new_artifacts dedupes against this list;
                        # while an artifact sat only in pending_artifact_ids the
                        # poller re-synthesized it on every iteration and the
                        # buffer grew without bound.
                        if _aid not in seen_artifact_ids:
                            seen_artifact_ids.append(_aid)
                        if per_artifact_mode:
                            # Each write IS the closeout. Synthesized rather
                            # than branched, so both modes run one review path
                            # and there is no second pipeline to keep in sync.
                            _sid = f"closeout-per-artifact-{_aid}"
                            pending_calls[_sid] = _CLOSEOUT_TOOL
                            events.append({
                                "type": "tool_call_done",
                                "call_id": _sid,
                                "outcome": "auto",
                                "_synthetic": True,
                            })
                            output_lines.append(
                                f"[session_loop] artifact {_aid} — reviewing "
                                "immediately (review_trigger=per_artifact)"
                            )
                        else:
                            output_lines.append(
                                f"[session_loop] artifact {_aid} buffered "
                                f"({len(pending_artifact_ids)} pending) — review "
                                "fires at closeout"
                            )
                        continue
                    if tool_name not in (_CLOSEOUT_TOOL, "session_report"):
                        continue
                    # ---- CLOSEOUT ----------------------------------------
                    closeout_seen = True
                    if not pending_artifact_ids:
                        # Nothing new since the last closeout. A session_report
                        # with no pending artifact is not a review trigger.
                        continue
                    # One review round for the whole submission, labelled with
                    # the newest artifact. The others are still recorded as
                    # seen so the no-artifact corrective and the result payload
                    # know they exist.
                    aid = pending_artifact_ids[-1]
                    # THE WHOLE BATCH gets the verdict, not just `aid`.
                    #
                    # review_queue is keyed on (subtask_id, artifact_id) and the
                    # subagent awaits on whichever artifact IT decided to name.
                    # Under the old per-write contract every artifact got its
                    # own review AND its own publish, so any key the subagent
                    # picked was covered. Coalescing to ONE review round broke
                    # that: the verdict landed on one key while the subagent
                    # blocked on another, and it polled await_review until the
                    # wall clock — observed live on 1.2, 19 minutes past a
                    # verdict that had already been published.
                    #
                    # So: one review of the complete submission, but the verdict
                    # is distributed to every artifact in it. Coalescing the
                    # REVIEW is the point; coalescing the PUBLISH was the bug.
                    batch_aids = list(pending_artifact_ids)
                    # Already recorded in seen_artifact_ids at buffer time.
                    pending_artifact_ids.clear()
                    # WP-F — the buffer drained, so the alarm re-arms for the
                    # next one. Per BATCH, exactly like the closeout latch.
                    pending_since = None
                    unreviewed_alarm_fired = False
                    # N2 — the sentinel watermark is advanced where the
                    # sentinel is CONSUMED (the DB-poll branch), to that row's
                    # own timestamp. Advancing here to our utcnow() would be
                    # the wrong clock domain: a row stamped slightly ahead of
                    # us would keep matching and fire round 2 early.
                    # N1 — RELEASE THE ONE-SHOT LATCH. It exists to stop the
                    # DB-poll branches re-synthesizing a closeout for a batch
                    # that already has one in flight; it is NOT a
                    # once-per-session guard. Left latched, round 2 on a
                    # stdio-only subagent finds both DB-poll branches skipped,
                    # the sentinel never arrives, and the subtask deadlocks to
                    # the 3600 s wall clock — the same class as the three
                    # stalls already fixed, displaced by one round. It also
                    # silently voids the D3a revision ceiling on that path,
                    # because a second revision can never be triggered.
                    # The batch is consumed here, so the next one may
                    # synthesize again.
                    _synthetic_closeout_sent = False
                    rounds += 1
                    # L3 — the ROUND BUDGET is per deliverable, not per write.
                    # `rounds` stays the write counter so the published
                    # `attempt` and its convergence telemetry keep their
                    # existing meaning; only the CAP trigger and the stall
                    # detector move onto the family, because both are
                    # statements about ONE deliverable's progress.
                    family = _artifact_family_key(aid, artifact_families)
                    family_rounds[family] = family_rounds.get(family, 0) + 1

                    # ---- per-artifact reviewer + publish -------------------
                    # Light the per-subtask "Reviewing" badge for the duration
                    # of the judge call (cleared in finally so it never sticks).
                    _set_reviewing(True)
                    try:
                        verdict_envelope = _run_reviewer_for_artifact(
                            task=task, subtask=subtask, artifact_id=aid,
                            config=config, review_runner=review_runner,
                            attempt=rounds, max_attempts=max_rounds,
                            prior_findings=prev_findings,
                        )
                    finally:
                        _set_reviewing(False)
                        # The reviewer call is legitimate work, not silence.
                        # A codex critic+scorer is spawn-per-turn, sequential,
                        # and multi-round (minutes-long); it blocks this loop
                        # so no subagent events are observed while it runs.
                        # Without resetting the idle clock here the
                        # silent-watchdog counts the reviewer's own duration as
                        # dead air and force-CAPs a perfectly live review the
                        # instant it returns. Reset so the 600s window measures
                        # quiet AFTER the verdict, not across it.
                        last_observed_event_ts = clock()
                    # Fold this round into the ledger and hand the NEXT round
                    # every still-open item — not just what this round happened
                    # to return.
                    _new_ids, _reopened = _ledger_merge(
                        finding_ledger, verdict_envelope.get("findings") or [], rounds,
                    )
                    prev_findings = _ledger_open(finding_ledger)

                    # Why this round ended, when it ended for a loop-control
                    # reason (convergence / budget) rather than the verdict
                    # alone. Rides this round's own telemetry row — see the
                    # one-row-per-publish contract in test_convergence_telemetry.
                    round_reason = ""
                    round_open = 0
                    round_resolved = 0

                    # CONVERGENCE. A round that raises nothing NEW has no further
                    # information to add: re-running it produces the same set
                    # again. Open items may remain — convergence is "the review
                    # stopped changing its mind", not "the review is happy".
                    #
                    # Advancing here is the point. Parking a converged review
                    # asks the user to adjudicate a set the loop already proved
                    # stable, which is what stalled whole runs behind one
                    # unresolvable detail. The open items ride along as caveats
                    # (result["unresolved_findings"]) so advancing is never
                    # silent, and a reopened finding is oscillation, which no
                    # further round fixes — so it stops the loop too.
                    if (
                        final_verdict is None
                        and rounds >= 2
                        and not _new_ids
                        and not _reopened
                    ):
                        converged_open = _ledger_open(finding_ledger)
                        logger.info(
                            "[%s/%s] review CONVERGED at round %s — no new "
                            "findings; %s open item(s) carried as caveats",
                            task.id, subtask.id, rounds, len(converged_open),
                        )
                        # This used to call emit_event_fn with a POSITIONAL
                        # dict. append_event is keyword-only, so every one of
                        # these raised TypeError into a bare except and no
                        # convergence row was ever written — the signal
                        # review_loop_stats needs most was silently absent.
                        #
                        # Recorded rather than emitted here, deliberately: this
                        # round still publishes below, and the telemetry
                        # contract is ONE row per publish_review. A second row
                        # with no publish behind it would break that. The
                        # reason rides the round's own row instead.
                        round_reason = (
                            f"converged (no new findings at round {rounds})"
                        )
                        round_open = len(converged_open)
                        round_resolved = sum(
                            1 for e in finding_ledger.values()
                            if e["state"] == "resolved"
                        )
                        final_verdict = "PASS"
                    try:
                        # N3 — fan the ONE verdict out to every artifact id
                        # KNOWN FOR THIS SUBTASK, not just the current batch.
                        #
                        # The subagent awaits on whichever id it named, and it
                        # can legitimately name one outside this batch:
                        #   * a write whose result the stream returned
                        #     unparseable — dropped at the buffer step, so it
                        #     never entered batch_aids, though the artifacts
                        #     table (channel 3) still knows it;
                        #   * an id from an earlier round it is still holding.
                        # Either way the verdict never reaches it and it polls
                        # to the wall clock. Publishing to the superset costs
                        # one idempotent row per stale id and removes the whole
                        # failure mode — a later verdict on an older key is the
                        # CURRENT verdict for that subtask, which is what a
                        # subagent holding that key should receive.
                        _pub_targets = list(dict.fromkeys(
                            list(batch_aids or [aid]) + list(seen_artifact_ids)
                        ))
                        for _pub_aid in _pub_targets:
                            publish_review_fn(
                                subtask_id=subtask.id,
                                artifact_id=_pub_aid,
                                verdict=verdict_envelope["verdict"],
                                findings=verdict_envelope["findings"],
                                implicated_acs=verdict_envelope["implicated_acs"],
                                attempt=rounds,
                                max_attempts=max_rounds,
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.exception(
                            "[%s/%s] publish_review failed for artifact=%s",
                            task.id, subtask.id, aid,
                        )
                        result["error"] = f"publish_review failed: {exc}"
                        final_verdict = "FAIL"
                        break

                    # PR 4 — convergence telemetry. One row per published
                    # verdict so the engine can observe rounds × findings
                    # decay across the retry loop. Best-effort: a failed
                    # emit logs but never blocks the dispatch.
                    _emit_convergence(
                        emit_event_fn=emit_event_fn,
                        task=task, subtask=subtask,
                        artifact_id=aid,
                        verdict=verdict_envelope["verdict"],
                        findings=verdict_envelope["findings"],
                        attempt=rounds,
                        reason=round_reason,
                        open_findings=round_open,
                        resolved_findings=round_resolved,
                        stage_timings=verdict_envelope.get("stage_timings"),
                    )

                    result["verdicts"].append({
                        "artifact_id": aid,
                        "verdict": verdict_envelope["verdict"],
                        "attempt": rounds,
                    })

                    # CAP / NEEDS_USER terminate the loop from the engine
                    # side — the subagent will receive the verdict via
                    # await_review and exit. PASS is also terminal but we
                    # keep polling for status="done" so the subagent gets
                    # a clean chance to call session_report.
                    if verdict_envelope["verdict"] in _PASS_VERDICTS:
                        final_verdict = "PASS"
                    elif verdict_envelope["verdict"] in _TERMINAL_VERDICTS:
                        final_verdict = verdict_envelope["verdict"]

                    # Reviewer transport fault surfaced as a terminal NEEDS_USER
                    # (see _run_reviewer_for_artifact). Tag the result as an INFRA
                    # cause so handle_failure's blocked_review surface reads
                    # "transient — retry" (engine.py:4294) instead of a reviewer
                    # decision. DP10: same infra contract as the subagent-stream
                    # path (result["infra_error"]); ``or`` preserves any earlier
                    # error and is not clobbered by a later stream-error event
                    # (line 680 also uses ``or``).
                    if verdict_envelope.get("reviewer_infra_error"):
                        result["infra_error"] = True
                        result["error"] = result["error"] or (
                            "reviewer stage unavailable (critic infra_error) — "
                            "deliverable not reviewed; escalated instead of "
                            "shipping unreviewed work"
                        )

                    # C3 — non-convergence detector. While still FAIL-looping
                    # (final_verdict is None) count this round's LOAD-BEARING
                    # findings; if that count fails to strictly decrease across
                    # _CONVERGENCE_STALL_ROUNDS consecutive rounds the work is
                    # oscillating, not improving. Does NOT change the verdict
                    # (still CAP → blocked_review) — only stops sooner so the
                    # remaining rounds don't burn cost/latency for nothing.
                    #
                    # L3 — tracked PER FAMILY. Comparing this round's count
                    # against a different deliverable's previous count is not
                    # a convergence signal at all: two unrelated artifacts
                    # with 2 findings each read as a flat, stalled sequence
                    # and force a premature escalation.
                    stalled = False
                    if final_verdict is None:
                        lb_count = sum(
                            1 for f in (verdict_envelope.get("findings") or [])
                            if str((f or {}).get("severity", "")).lower() == "load_bearing"
                        )
                        stall_streak, stalled = _convergence_stall(
                            prev_lb_count.get(family), lb_count,
                            stall_streak_by_family.get(family, 0),
                        )
                        stall_streak_by_family[family] = stall_streak
                        prev_lb_count[family] = lb_count

                    family_round_no = family_rounds.get(family, 0)
                    # D3a — the GLOBAL ceiling, in REVISIONS not writes: every
                    # review of a deliverable after its first, summed across
                    # families. Binds a subtask that spreads re-work over many
                    # deliverables and would otherwise buy N x max_rounds judge
                    # calls. First looks stay free, so the two artifact writes
                    # the two-streams protocol requires never trip this.
                    revisions = sum(max(0, n - 1) for n in family_rounds.values())
                    ceiling_hit = revisions >= max_subtask_revisions
                    if final_verdict is None and (
                        ceiling_hit or family_round_no >= max_rounds or stalled
                    ):
                        if ceiling_hit:
                            cap_reason = (
                                f"subtask_revision_ceiling ({revisions}/"
                                f"{max_subtask_revisions} revision rounds across "
                                f"all deliverables)"
                            )
                        elif family_round_no >= max_rounds:
                            cap_reason = "max_rounds"
                        else:
                            cap_reason = (
                                f"non_convergence (load-bearing findings stalled "
                                f"{stall_streak} round(s) at "
                                f"{prev_lb_count.get(family)})"
                            )
                        # A pure non-convergence STALL (subagent oscillating,
                        # not improving, with rounds still left) means the
                        # deliverable carries a finding the subagent cannot
                        # self-resolve — typically a genuine constraint or
                        # trade-off only the user can adjudicate (e.g. "HubSpot
                        # cannot meet the Swiss-data-residency MUSS"). Escalate
                        # to NEEDS_USER (a decision surface) instead of a dead
                        # CAP that reads as "the work failed". A true round-
                        # budget exhaustion (not a stall) stays CAP.
                        _stall_verdict = (
                            "NEEDS_USER"
                            if (stalled and family_round_no < max_rounds)
                            else "CAP"
                        )
                        logger.info(
                            "[%s/%s] forcing %s (%s) at family round %s/%s "
                            "(write %s, family=%s)",
                            task.id, subtask.id, _stall_verdict, cap_reason,
                            family_round_no, max_rounds, rounds, family,
                        )
                        try:
                            # N3 — same superset as the normal publish: an
                            # escalation must reach whoever is awaiting, too.
                            for _pub_aid in list(dict.fromkeys(
                                list(batch_aids or [aid]) + list(seen_artifact_ids)
                            )):
                                publish_review_fn(
                                    subtask_id=subtask.id,
                                    artifact_id=_pub_aid,
                                    verdict=_stall_verdict,
                                    findings=verdict_envelope["findings"],
                                    implicated_acs=verdict_envelope["implicated_acs"],
                                    attempt=rounds,
                                    max_attempts=max_rounds,
                                )
                            # D3a — carry WHY the loop stopped, plus the open /
                            # resolved split, so an escalation is not just a
                            # bare verdict the user has to reconstruct.
                            _emit_convergence(
                                emit_event_fn=emit_event_fn,
                                task=task, subtask=subtask,
                                artifact_id=aid,
                                verdict=_stall_verdict,
                                findings=verdict_envelope["findings"],
                                attempt=rounds,
                                reason=cap_reason,
                                open_findings=len(_ledger_open(finding_ledger)),
                                resolved_findings=sum(
                                    1 for e in finding_ledger.values()
                                    if e["state"] == "resolved"
                                ),
                                stage_timings=verdict_envelope.get("stage_timings"),
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.exception(
                                "[%s/%s] publish_review %s escalation failed",
                                task.id, subtask.id, _stall_verdict,
                            )
                        final_verdict = _stall_verdict
                elif etype == "status":
                    state = evt.get("state")
                    if state in {"done", "cancelled", "error"}:
                        # D1a SOFT PERIOD. The stream is closing with artifacts
                        # still buffered and no closeout sentinel — an in-flight
                        # or resumed subtask running an older brief, or one that
                        # died before session_report. Absence of the sentinel
                        # must NOT fail the subtask and must NOT silently drop
                        # the review, so synthesize the closeout and review
                        # exactly as if the sentinel had arrived.
                        #
                        # Injected as a synthetic tool_call_done rather than
                        # handled inline because the review block lives in the
                        # tool_call_done branch; appending to `events` during
                        # iteration is picked up by the same for-loop, and this
                        # file already uses synthetic tool_call events for the
                        # artifact_writes the registry misses.
                        if (
                            pending_artifact_ids
                            and not _synthetic_closeout_sent
                            and final_verdict is None
                        ):
                            _synthetic_closeout_sent = True
                            if not closeout_seen:
                                logger.warning(
                                    "[%s/%s] stream closed with %d artifact(s) "
                                    "buffered and no session_report sentinel — "
                                    "falling back to stream-end review (soft "
                                    "period; not a failure)",
                                    task.id, subtask.id, len(pending_artifact_ids),
                                )
                                output_lines.append(
                                    "[session_loop] no closeout sentinel — "
                                    "reviewing at stream end (soft period)"
                                )
                            _sid = f"synthetic-closeout-{len(seen_artifact_ids)}"
                            pending_calls[_sid] = _CLOSEOUT_TOOL
                            events.append({
                                "type": "tool_call_done",
                                "call_id": _sid,
                                "outcome": "auto",
                                "_synthetic": True,
                            })
                        # Stream is closing — let the outer ``done`` flag
                        # break the loop after we drain remaining events.
                        if state == "error":
                            # Capture the REAL session error (the bridge carries
                            # it in detail.result / detail.error) instead of the
                            # generic placeholder, and flag transient API/infra
                            # errors so handle_failure routes them to a
                            # recoverable blocked_review instead of a permanent
                            # cascade-fail over a server blip.
                            _detail = evt.get("detail") or {}
                            _msg = ""
                            if isinstance(_detail, dict):
                                _msg = (
                                    _detail.get("result")
                                    or _detail.get("error")
                                    or _detail.get("message")
                                    or ""
                                )
                            _msg = (
                                _msg
                                or evt.get("error")
                                or evt.get("text")
                                or "stream session reported error"
                            )
                            result["error"] = result["error"] or _msg
                            if is_transient_infra_error(_msg):
                                result["infra_error"] = True
                elif etype == "usage":
                    # M5+ spawn_usage accounting. See _accumulate_usage for why
                    # this is not simply a running sum.
                    _accumulate_usage(usage_totals, evt)
                elif etype == "message_stop":
                    text = (evt.get("text") or "").strip()
                    if text:
                        output_lines.append(text)

            since_seq = max(since_seq, next_seq)

            if done:
                # WP8 — pre-finalize no-artifact reprompt. The subagent
                # reached a terminal state having NEVER written a Stream-B
                # artifact → the existing fall-through would stamp
                # "subagent never wrote an artifact" and fail. Before
                # accepting that, publish ONE corrective into the session
                # and grant a single bounded extra round. Guarded by a
                # one-shot flag so it fires at most once (no infinite
                # loop). A genuinely-exited session refuses send_input
                # (SessionAlreadyTerminal / RuntimeError) — caught, the
                # flag is set, and we fall through to the failure path
                # rather than spin.
                if (
                    not seen_artifact_ids
                    and not no_artifact_corrective_sent
                    and final_verdict not in _TERMINAL_VERDICTS
                ):
                    no_artifact_corrective_sent = True
                    if _send_no_artifact_corrective(
                        registry, stream_session_id, subtask, task,
                        emit_activity=_emit_activity,
                    ):
                        # Reprompt accepted by a still-live session — extend
                        # the deadline by ONE poll budget and keep looping
                        # for the corrective round. Reset liveness so the
                        # silent-watchdog clock starts fresh for this round.
                        deadline = clock() + float(silent_watchdog_s)
                        last_observed_event_ts = clock()
                        time.sleep(poll_interval_s)
                        continue
                # The session terminated naturally (subagent exited or the
                # adapter shut down). Stop polling.
                break

            if final_verdict in _TERMINAL_VERDICTS and final_verdict != "PASS":
                # CAP / NEEDS_USER — no point waiting for the subagent to
                # idle; the verdict has been published and await_review
                # will surface it. Tear down to free the slot.
                break

            # D1a — await_review DB-poll fallback. THE ONE THAT MATTERS.
            #
            # The review trigger is await_review's tool_call_start in the
            # bridge stream, but subagents reach okuro through the stdio MCP
            # server, which writes tool_invocations rows directly and never
            # touches that stream. On a live run the artifact arrived (already
            # synthesized from the DB above) while the sentinel did not: the
            # subagent polled await_review 10 times over 24 minutes and the
            # subtask deadlocked until the wall clock.
            #
            # Polled every iteration once something is buffered, and drained
            # through the SAME synthetic closeout the other channels use.
            if pending_artifact_ids and not _synthetic_closeout_sent:
                _seen_sentinel_at = _poll_db_for_await_review(
                    stream_session_id, since_iso=_sentinel_since_iso,
                )
                if _seen_sentinel_at:
                    # N2 — consume it immediately: only a sentinel written
                    # AFTER this one may trigger the next round.
                    if isinstance(_seen_sentinel_at, str):
                        _sentinel_since_iso = _seen_sentinel_at
                    _synthetic_closeout_sent = True
                    closeout_seen = True
                    logger.info(
                        "[%s/%s] await_review seen via DB poll with %d "
                        "artifact(s) buffered — running the closeout review",
                        task.id, subtask.id, len(pending_artifact_ids),
                    )
                    _sid = f"closeout-db-{len(seen_artifact_ids)}"
                    pending_calls[_sid] = _CLOSEOUT_TOOL
                    # _pending_synthetic_events, NOT events: this runs AFTER
                    # the per-event loop has finished, so anything appended to
                    # `events` here is never iterated. The queue is drained at
                    # the top of the next poll.
                    _pending_synthetic_events.append({
                        "type": "tool_call_done",
                        "call_id": _sid,
                        "outcome": "auto",
                        "_synthetic": True,
                    })
                    continue

            # Theme J — session_report DB-poll fallback. The subagent's
            # stdio-MCP records a tool_invocations row when it calls
            # session_report as part of its close-out. If the bridge
            # registry missed the matching tool_call_done event (any
            # reason — adapter buffer flush mid-line, registry hiccup,
            # etc.) the dispatcher would otherwise never see the
            # terminal signal and the wall-clock would fire with
            # "subagent never wrote an artifact" even though work
            # shipped. Polling tool_invocations closes the gap.
            if (
                (seen_artifact_ids or pending_artifact_ids)
                and final_verdict not in _TERMINAL_VERDICTS
                and _poll_db_for_session_report(
                    stream_session_id,
                    since_iso=_db_poll_since_iso,
                )
            ):
                # D1a — the DB row IS a closeout sentinel, just delivered by a
                # different channel. If artifacts are still buffered they have
                # never been reviewed (the stream never carried the
                # tool_call_done), so queue the closeout and let the next pass
                # review it rather than declaring PASS over unreviewed work.
                # One-shot via _synthetic_closeout_sent, so the second time
                # round the buffer is empty and we fall through to the break.
                if pending_artifact_ids and not _synthetic_closeout_sent:
                    _synthetic_closeout_sent = True
                    logger.info(
                        "[%s/%s] session_report seen via DB poll with %d "
                        "artifact(s) buffered — running the closeout review "
                        "before finalizing",
                        task.id, subtask.id, len(pending_artifact_ids),
                    )
                    _sid = f"synthetic-closeout-db-{len(seen_artifact_ids)}"
                    pending_calls[_sid] = _CLOSEOUT_TOOL
                    _pending_synthetic_events.append({
                        "type": "tool_call_done",
                        "call_id": _sid,
                        "outcome": "auto",
                        "_synthetic": True,
                    })
                    continue
                _emit_activity({
                    "type": "session_report_observed",
                    "source": "db_poll",
                    "session_id": stream_session_id,
                })
                output_lines.append(
                    "[session_loop] session_report observed via DB poll"
                )
                # session_report is the subagent's terminal "I am done"
                # signal — treat as PASS regardless of the last review
                # verdict. The subagent's choice to call session_report
                # after FAIL means it has accepted the verdict and is
                # surrendering; engine handle_failure will pick up the
                # FAIL details from the published review_queue row.
                # Without this short-circuit the wall-clock would fire
                # with "subagent never wrote an artifact" misdiagnosing
                # the real ending.
                final_verdict = "PASS"
                break

            # WP8 — no-artifact silent reprompt. The session is still
            # alive (not `done`) but has gone quiet for `silent_watchdog_s`
            # without EVER writing a Stream-B artifact. Rather than wait
            # out the full wall-clock and fail with "never wrote an
            # artifact", publish ONE corrective into the live session and
            # give it a bounded extra round. One-shot via
            # `no_artifact_corrective_sent`; a refused send_input falls
            # through to the existing silent path below (no loop).
            if (
                not no_artifact_corrective_sent
                and not seen_artifact_ids
                and final_verdict is None
                and (clock() - last_observed_event_ts) > float(silent_watchdog_s)
            ):
                no_artifact_corrective_sent = True
                if _send_no_artifact_corrective(
                    registry, stream_session_id, subtask, task,
                    emit_activity=_emit_activity,
                ):
                    last_observed_event_ts = clock()
                    time.sleep(poll_interval_s)
                    continue

            # Theme A — silent-watchdog. If the bridge registry has been
            # silent AND the DB poll has produced nothing for
            # WP-F — BUFFERED BUT UNREVIEWED. The signature all three
            # production deadlocks shared: artifacts buffered, no review, the
            # subagent polling await_review. The silent watchdog below cannot
            # catch it — those polls ARE stream activity, so its idle clock
            # keeps resetting and it never fires. Left alone the only exit was
            # the 3600 s wall clock, which burns a retry and misreports the
            # cause as a silent subagent.
            #
            # Loud FIRST, then self-heal: emit the alarm so the failure is
            # visible even if the fallback also fails, then synthesize a
            # closeout through the same path every other channel uses.
            if (
                pending_artifact_ids
                and not unreviewed_alarm_fired
                and final_verdict is None
                and pending_since is not None
                and (clock() - pending_since) > float(unreviewed_buffer_alarm_s)
            ):
                unreviewed_alarm_fired = True
                _stuck_s = round(clock() - pending_since, 1)
                logger.error(
                    "[%s/%s] %d artifact(s) buffered %.0fs with no review — "
                    "the closeout sentinel never arrived on any channel. "
                    "Forcing a review; see the transport map in this module.",
                    task.id, subtask.id, len(pending_artifact_ids), _stuck_s,
                )
                _emit_activity({
                    "type": "review_sentinel_lost",
                    "elapsed_s": _stuck_s,
                    "pending_artifacts": list(pending_artifact_ids),
                    "rounds": rounds,
                })
                # ROCK-SOLID v5 P3.1 — ALSO into log.jsonl, not only
                # .activity.jsonl. The activity file feeds the per-subtask
                # panel; no state consumer tails it and the EventWatcher does
                # not read it, so this recovery was invisible to the task page.
                # From the user's side that reads as a stall followed by work
                # resuming, both unexplained — while the system is in fact
                # handling it correctly. Saying so is the whole point of the
                # calm-state layer.
                try:
                    from okuro.orchestrator.state import emit_event as _emit_state

                    _emit_state(task.id, "review_sentinel_lost", {
                        "subtask_id": subtask.id,
                        "elapsed_s": _stuck_s,
                        "pending_artifacts": list(pending_artifact_ids),
                        "rounds": rounds,
                    }, config.tasks_dir)
                except Exception as _exc:  # pragma: no cover - defensive
                    logger.warning(
                        "review_sentinel_lost state emit failed: %r", _exc,
                    )
                output_lines.append(
                    f"[session_loop] sentinel lost — {len(pending_artifact_ids)}"
                    f" artifact(s) unreviewed for {_stuck_s}s; forcing review"
                )
                _sid = f"closeout-alarm-{len(seen_artifact_ids)}"
                pending_calls[_sid] = _CLOSEOUT_TOOL
                _pending_synthetic_events.append({
                    "type": "tool_call_done",
                    "call_id": _sid,
                    "outcome": "auto",
                    "_synthetic": True,
                })
                continue

            # `silent_watchdog_s` while a subagent process is presumed
            # alive AND we've already seen at least one artifact (i.e.
            # the subagent demonstrably ran), force-publish a synthetic
            # CAP verdict for the most recent artifact. This wakes any
            # blocked `await_review` and lets the dispatch tear down
            # cleanly instead of sitting until the 60-min wall-clock.
            if (
                not silent_watchdog_fired
                and seen_artifact_ids
                and final_verdict is None
                and (clock() - last_observed_event_ts) > float(silent_watchdog_s)
            ):
                silent_watchdog_fired = True
                _last_aid = seen_artifact_ids[-1]
                _emit_activity({
                    "type": "subagent_silent",
                    "elapsed_s": round(clock() - last_observed_event_ts, 1),
                    "artifact_id": _last_aid,
                    "rounds": rounds,
                })
                try:
                    publish_review_fn(
                        subtask_id=subtask.id,
                        artifact_id=_last_aid,
                        verdict="CAP",
                        findings=[{
                            "severity": "load_bearing",
                            "summary": (
                                "silent-watchdog: bridge registry and "
                                "artifacts DB both quiet for "
                                f"{clock() - last_observed_event_ts:.0f}s "
                                "after artifact published. Forcing CAP to "
                                "unblock await_review."
                            ),
                            "artifact_id": _last_aid,
                        }],
                        implicated_acs=[],
                        attempt=rounds,
                        max_attempts=max_rounds,
                    )
                    _emit_convergence(
                        emit_event_fn=emit_event_fn,
                        task=task, subtask=subtask,
                        artifact_id=_last_aid,
                        verdict="CAP",
                        findings=[],
                        attempt=rounds,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "[%s/%s] silent-watchdog publish_review failed",
                        task.id, subtask.id,
                    )
                final_verdict = "CAP"
                break

            time.sleep(poll_interval_s)
        else:
            # while-loop fell through without break — wall-clock cap hit.
            result["error"] = result["error"] or (
                f"session-loop wall-clock timeout after {wall_clock_s:.0f}s"
            )
    finally:
        # Always tear down the stream session — even on exceptions —
        # so the slot is freed for the next dispatch. cancel() is
        # idempotent on already-terminal sessions.
        if stream_session_id:
            try:
                registry.cancel(stream_session_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[%s/%s] registry.cancel raised %r — leaking session %s",
                    task.id, subtask.id, exc, stream_session_id,
                )

    result["duration"] = clock() - started_at

    # M5+ spawn_usage — one row per spawn, the per-spawn cost instrument.
    #
    # This emitter was born inside the legacy `dispatch_subtask` and was
    # deleted with it on 2026-05-30; the streaming dispatcher never had one.
    # What survived for two months was the schema entry, two migrations
    # admitting the event type, a comment in dispatcher.py claiming the event
    # attributes billable input, and a scorecard that FAILS on its absence —
    # a checker with no producer. The m5 ratchet bisect wants this data.
    _emit_spawn_usage(
        emit_event_fn=emit_event_fn,
        task=task,
        subtask=subtask,
        provider=chosen_provider,
        usage=usage_totals,
        duration_s=result["duration"],
    )

    result["rounds"] = rounds
    result["artifact_ids"] = seen_artifact_ids
    result["output"] = "\n".join(output_lines)[:8000]
    # Theme F — surface the terminal verdict so engine handle_failure
    # can distinguish session-loop CAP / NEEDS_USER (phase →
    # blocked_review, awaiting user decision) from a plain FAIL
    # (retry-then-permanent-fail). Without this surface, CAP exits
    # the dispatcher silently → handle_failure runs the retry ladder
    # → exhausts → flips subtask.status=failed → phase.status STAYS
    # pending → /override-blocked-review returns 409.
    result["final_verdict"] = final_verdict or ""
    # Gap #1 — carry the last round's STRUCTURED findings out of the
    # session loop so the engine's blocked_review awaiting payload can
    # include them (CAP / NEEDS_USER alike). Without this the session-loop
    # terminal path (_flip_phase_to_blocked_review_for_session_loop) wrote
    # an awaiting block with NO critic_findings → the user-facing retry had
    # nothing to build a surgical worklist from → blind re-run → identical
    # re-fail. prev_findings tracks verdict_envelope["findings"] every round
    # (line ~536), so at terminal it holds the freshest finding set.
    result["critic_findings"] = list(prev_findings or [])
    # Advanced despite open findings — the caller marks review_state and lets
    # the run end `completed_partial` rather than a clean `done`.
    if converged_open:
        result["unresolved_findings"] = list(converged_open)
        result["converged"] = True
    # Success when at least one artifact was reviewed AND the final
    # verdict is PASS. CAP / NEEDS_USER / no-artifact-write all fail —
    # they surface through ``error`` and the engine's handle_failure
    # picks them up.
    if final_verdict == "PASS" and seen_artifact_ids:
        result["success"] = True
    elif final_verdict in {"CAP", "NEEDS_USER"} and not result["error"]:
        # Provide an explicit error string so handle_failure has a
        # human-readable explanation for the blocked_review event. Plain
        # words only — this string surfaces verbatim in the user-facing
        # blocked-review card, so no machine ids / internal vocabulary.
        if final_verdict == "NEEDS_USER":
            result["error"] = (
                f"the AI could not resolve the flagged issue on its own after "
                f"{rounds} quality-check attempt(s) and needs your decision"
            )
        else:  # CAP
            result["error"] = (
                f"the AI hit its retry limit after {rounds} quality-check "
                f"attempt(s) without passing"
            )
    elif not seen_artifact_ids and not result["error"]:
        result["error"] = "subagent never wrote an artifact"
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _send_no_artifact_corrective(
    registry: Any,
    session_id: Optional[str],
    subtask: Subtask,
    task: Task,
    *,
    emit_activity: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> bool:
    """WP8 — push ONE no-artifact corrective into a still-open session.

    Returns True when the corrective was accepted by a live session
    (caller grants the bounded extra round), False when the session is
    already terminal / refused input or no session id exists (caller
    falls through to the existing failure path). NEVER raises — a refused
    send_input is the expected signal that re-prompting is impossible, not
    an error to propagate.
    """
    if not session_id or registry is None:
        return False
    from okuro.orchestrator.dispatcher import _render_no_artifact_corrective

    message = (
        _render_no_artifact_corrective(subtask, task)
        + "\n(This is an automated corrective: the engine detected your "
        "turn ended with NO artifact_write call. Call it now, then "
        "await_review.)"
    )
    try:
        registry.send_input(session_id, message)
    except Exception as exc:  # noqa: BLE001 — terminal/refused is expected
        logger.info(
            "[%s/%s] no-artifact corrective not delivered (%r) — session "
            "not re-promptable; falling through to failure path",
            task.id, subtask.id, exc,
        )
        return False
    if emit_activity is not None:
        try:
            emit_activity({
                "type": "no_artifact_corrective_sent",
                "session_id": session_id,
            })
        except Exception:  # noqa: BLE001
            pass
    logger.info(
        "[%s/%s] no-artifact corrective delivered — granting one bounded "
        "extra round before failing",
        task.id, subtask.id,
    )
    return True


_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _accumulate_usage(totals: Dict[str, Any], evt: Dict[str, Any]) -> None:
    """Fold one ``usage`` event into the running per-spawn totals.

    NOT a plain sum, because the two kinds of usage event mean different
    things and adding them double-counts:

    * per-message usage (claude ``assistant``) is that message's slice;
    * terminal usage (claude ``result``, codex ``turn.completed``) is the
      CLI's OWN cumulative total for the whole session, and the only place
      cost lives.

    So a ``final`` event REPLACES the accumulated sum rather than adding to it,
    and once one has arrived later per-message events cannot dilute it. Without
    a terminal event the sum is the best available answer — accurate for
    tokens, and cost stays 0, which is correct for subscription providers
    anyway (contract §9).
    """
    is_final = bool(evt.get("final"))
    if totals.get("_final") and not is_final:
        return
    if is_final:
        for key in _USAGE_FIELDS:
            totals[key] = int(evt.get(key) or 0)
        totals["_final"] = True
    else:
        for key in _USAGE_FIELDS:
            totals[key] = int(totals.get(key) or 0) + int(evt.get(key) or 0)
    cost = evt.get("cost_usd") or 0
    if cost:
        totals["cost_usd"] = float(cost)
    duration = evt.get("duration_ms") or 0
    if duration:
        totals["duration_ms"] = int(duration)


def _emit_spawn_usage(
    *,
    emit_event_fn: Any,
    task: Task,
    subtask: Subtask,
    provider: str,
    usage: Dict[str, Any],
    duration_s: float,
) -> None:
    """Emit the one ``spawn_usage`` task_event for this spawn.

    EMITTED EVEN WHEN EVERY COUNT IS ZERO. A missing row and a zero row are
    different facts — "this spawn was never instrumented" versus "this spawn
    genuinely read nothing from cache" — and the scorecard's cold-cache check
    cannot tell them apart if silence is the encoding for both. Silence is
    exactly what the last two months looked like.

    Best-effort: a failed emit logs and returns, like ``_emit_convergence``.
    Cost telemetry must never fail a dispatch.
    """
    from okuro.clock import utc_now

    try:
        body = {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_input_tokens": int(
                usage.get("cache_read_input_tokens") or 0
            ),
            "cache_creation_input_tokens": int(
                usage.get("cache_creation_input_tokens") or 0
            ),
            "cost_usd": float(usage.get("cost_usd") or 0.0),
            # The CLI's own duration when it reported one, else the
            # dispatcher's wall clock — never nothing.
            "duration_ms": int(
                usage.get("duration_ms") or max(0, round(duration_s * 1000))
            ),
            "provider": str(provider or "unknown")[:64],
            # okuro.clock, not datetime.utcnow() — the latter is deprecated,
            # naive, and ratcheted against in tests/system/test_clock_discipline.
            "ts": utc_now().isoformat(),
        }
        emit_event_fn(
            task_id=task.id,
            subtask_id=subtask.id,
            event_type="spawn_usage",
            body=body,
            from_role=getattr(subtask, "role", "") or "",
            created_by="dispatcher_streaming",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[%s/%s] spawn_usage emit failed: %r", task.id, subtask.id, exc,
        )


def _emit_convergence(
    *,
    emit_event_fn: Any,
    task: Task,
    subtask: Subtask,
    artifact_id: str,
    verdict: str,
    findings: list,
    attempt: int,
    reason: str = "",
    open_findings: int = 0,
    resolved_findings: int = 0,
    stage_timings: Optional[dict] = None,
) -> None:
    """PR 4 — emit one ``convergence_telemetry`` task_event per publish.

    Best-effort: a failed emit logs and returns. Never blocks the
    dispatcher's main loop or surfaces to the subagent. The row pairs
    ``(subtask_id, artifact_id, attempt)`` with the live findings count
    so the engine + analytics can observe whether the session-loop is
    converging (findings → 0 across attempts) or oscillating.

    ``reason`` / ``open_findings`` / ``resolved_findings`` are set only when
    the loop stopped for a BUDGET reason rather than a verdict, so
    review_loop_stats can tell "converged with items open" apart from "ran out
    of budget with items open". They are also the reason this function is now
    the single emit path: two call sites used to pass a positional dict
    (``emit_event_fn({...})``), which ``append_event`` — keyword-only — rejects
    with TypeError straight into a bare ``except``. Those rows never existed.

    ``stage_timings`` (ROCK-SOLID v5 P0#5) is ``run_review``'s per-round
    breakdown (queue_wait_s / deterministic_s / critic_s / scorer_s), passed
    through verbatim from the verdict envelope. Omitted (not an empty dict)
    when the caller has none, so a schema check over historical rows can
    tell "measured, all zero" apart from "not instrumented yet".
    """
    from datetime import datetime as _dt
    try:
        body = {
            "subtask_id": subtask.id,
            "artifact_id": artifact_id,
            "attempt": int(attempt),
            "verdict": verdict,
            "findings_count": len(list(findings or [])),
            "reason": str(reason)[:200],
            "open_findings": int(open_findings),
            "resolved_findings": int(resolved_findings),
            "ts": _dt.utcnow().isoformat(),
        }
        if stage_timings:
            body["stage_timings"] = dict(stage_timings)
        emit_event_fn(
            task_id=task.id,
            subtask_id=subtask.id,
            event_type="convergence_telemetry",
            body=body,
            from_role=getattr(subtask, "role", "") or "",
            created_by="dispatcher_streaming",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[%s/%s] convergence_telemetry emit failed (verdict=%s attempt=%s): %r",
            task.id, subtask.id, verdict, attempt, exc,
        )


def _resolve_provider(task: Task, config: Config) -> str:
    """Return the stream-adapter provider name for a dispatch.

    Honours an explicit ``task.preferred_cli`` when set; otherwise falls
    back to ``config.cli_default`` and finally to the wave-3b-supported
    set's first member.
    """
    candidate = (getattr(task, "preferred_cli", "") or "").strip()
    if not candidate:
        candidate = (getattr(config, "cli_default", "") or "").strip()
    return candidate or _DEFAULT_PROVIDER


def _resolve_okuro_db_path() -> _Path:
    """Locate the okuro.db sqlite file the brain writes to.

    Honours the same env override the rest of okuro respects so dev /
    test installs that point elsewhere keep working. Falls back to the
    canonical ``~/.okuro/okuro.db`` location.
    """
    env = os.environ.get("OKURO_DB_PATH") or os.environ.get("OKURO_DB")
    if env:
        return _Path(env)
    return okuro_home() / "okuro.db"


def _poll_db_for_session_report(
    session_id: str,
    *,
    since_iso: Optional[str] = None,
    db_path: Optional[_Path] = None,
) -> bool:
    """Theme J — return True if the subagent's stdio-MCP recorded a
    ``session_report`` tool_invocations row for ``session_id`` after
    ``since_iso``. The subagent calls session_report as part of its
    close-out; if the bridge missed the matching tool_call_done event
    (registry hiccup, adapter buffer flush mid-line, etc.) the
    dispatcher would otherwise never see the terminal signal and the
    600s wall-clock would fire with "subagent never wrote an artifact"
    even though work shipped.

    Best-effort: any DB error returns False so the registry path stays
    load-bearing.
    """
    if not session_id:
        return False
    db = db_path or _resolve_okuro_db_path()
    if not db.exists():
        return False
    try:
        conn = sqlite3.connect(str(db), timeout=2.0)
        try:
            if since_iso:
                row = conn.execute(
                    "SELECT 1 FROM tool_invocations "
                    "WHERE session_id = ? AND tool_name = 'session_report' "
                    "AND (executed_at > ? OR approved_at > ?) "
                    "LIMIT 1",
                    (session_id, since_iso, since_iso),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM tool_invocations "
                    "WHERE session_id = ? AND tool_name = 'session_report' "
                    "LIMIT 1",
                    (session_id,),
                ).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[session=%s] session_report DB poll failed (%r) — skipping",
            session_id, exc,
        )
        return False
    return row is not None


def _artifact_max_created_at(
    artifact_ids: list[str],
    *,
    db_path: Optional[_Path] = None,
) -> Optional[str]:
    """Newest ``created_at`` among these artifacts, as the store recorded it.

    Used to advance the closeout-sentinel cursor. Deliberately row-to-row and
    NOT clock-based: the artifacts and the tool_invocations rows are written by
    the same process (the stdio MCP server), so their timestamps are directly
    comparable. Any reading of "now" — ours or the database's — belongs to a
    third clock, and a row stamped ahead of it keeps matching forever. That
    mistake has now been made twice on this cursor; comparing rows to rows is
    the version with no clock in it at all.

    Returns None when nothing matches, in which case the caller must LEAVE the
    cursor alone rather than guess.
    """
    ids = [a for a in (artifact_ids or []) if a]
    if not ids:
        return None
    db = db_path or _resolve_okuro_db_path()
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(str(db), timeout=2.0)
        try:
            marks = ",".join("?" for _ in ids)
            row = conn.execute(
                f"SELECT MAX(created_at) FROM artifacts WHERE id IN ({marks})",
                ids,
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "artifact created_at lookup failed (%r) — sentinel cursor not "
            "advanced", exc,
        )
        return None
    return (row[0] if row else None) or None


def _poll_db_for_await_review(
    session_id: str,
    *,
    since_iso: Optional[str] = None,
    db_path: Optional[_Path] = None,
) -> Optional[str]:
    """Return True if the subagent recorded an ``await_review`` invocation.

    THE CLOSEOUT SENTINEL MUST BE POLLED, NOT ONLY WATCHED (regression fixed
    2026-07-30, found on a live task). D1a moved the review trigger onto
    ``await_review``'s ``tool_call_start`` in the bridge stream. But subagents
    reach okuro through the stdio MCP server attached via ``--mcp-config``,
    which writes ``tool_invocations`` rows straight to the brain and BYPASSES
    the bridge stream registry — the very reason ``_poll_db_for_new_artifacts``
    exists for ``artifact_write``.

    So on a live run the artifact arrived (synthesized from the DB) but the
    sentinel never did: the subagent called ``await_review`` and polled it 10
    times across 24 minutes while the dispatcher waited for a stream event that
    the stdio path never emits. Measured: subtask running 45 min, zero review
    events, deadlock until the 60-minute wall clock.

    The lesson generalises: any signal the review trigger depends on needs a DB
    fallback, because the stream is not the only channel a subagent speaks
    through. Best-effort — any DB error returns False so the registry path
    stays load-bearing.
    """
    if not session_id:
        return False
    db = db_path or _resolve_okuro_db_path()
    if not db.exists():
        return False
    try:
        conn = sqlite3.connect(str(db), timeout=2.0)
        try:
            # N2 — return the NEWEST matching stamp, not a bool. The caller
            # advances its watermark to exactly what it consumed, which is
            # robust however the writer's clock relates to ours. Advancing to
            # our own utcnow() instead would leave a row stamped slightly
            # ahead of us matching forever, and round 2 would fire early.
            if since_iso:
                row = conn.execute(
                    "SELECT MAX(COALESCE(executed_at, approved_at)) "
                    "FROM tool_invocations "
                    "WHERE session_id = ? AND tool_name = 'await_review' "
                    "AND (executed_at > ? OR approved_at > ?)",
                    (session_id, since_iso, since_iso),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT MAX(COALESCE(executed_at, approved_at)) "
                    "FROM tool_invocations "
                    "WHERE session_id = ? AND tool_name = 'await_review'",
                    (session_id,),
                ).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[session=%s] await_review DB poll failed (%r) — skipping",
            session_id, exc,
        )
        return None
    return (row[0] if row else None) or None


def _poll_db_for_new_artifacts(
    task_id: str,
    subtask_id: str,
    seen: list[str],
    *,
    since_iso: Optional[str] = None,
    db_path: Optional[_Path] = None,
) -> List[str]:
    """Return brain ``artifacts.id`` rows tagged (task_id, subtask_id) not in seen.

    Belt-and-suspenders for the bridge-registry observer. The registry
    only emits ``tool_call_done{tool_name="artifact_write"}`` when the
    subagent's ``artifact_write`` happens inside the bridge-spawned
    stream session. Subagents that call ``artifact_write`` via their
    own stdio MCP connection (the okuro mcp server attached via
    ``--mcp-config``) bypass the registry entirely — their rows land
    in ``artifacts`` but no ``tool_call_done`` event fires — the
    review queue stays empty — ``await_review`` polls forever — the
    dispatcher returns "subagent never wrote an artifact".

    This poll closes the gap by treating ``artifacts`` as the canonical
    source of truth. Best-effort: any DB error degrades to an empty
    list so the registry path stays load-bearing.
    """
    if not task_id or not subtask_id:
        return []
    db = db_path or _resolve_okuro_db_path()
    if not db.exists():
        return []
    seen_set = set(seen)
    try:
        conn = sqlite3.connect(str(db), timeout=2.0)
        try:
            if since_iso:
                rows = conn.execute(
                    "SELECT id FROM artifacts "
                    "WHERE task_id = ? AND subtask_id = ? AND created_at > ? "
                    "ORDER BY created_at ASC",
                    (task_id, subtask_id, since_iso),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM artifacts "
                    "WHERE task_id = ? AND subtask_id = ? "
                    "ORDER BY created_at ASC",
                    (task_id, subtask_id),
                ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[%s/%s] artifacts-table poll failed (%r) — skipping",
            task_id, subtask_id, exc,
        )
        return []
    return [r[0] for r in rows if r and r[0] and r[0] not in seen_set]


_ARTIFACT_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _extract_artifact_id(raw: Any) -> str:
    """Pull the artifact id out of a tool_call_done.result payload.

    ``artifact_write`` returns the bare uuid string. The bridge layer
    may wrap it as a plain string OR as a list of ``{type:"text",
    text:"..."}`` blocks (MCP TextContent shape). Both shapes degrade
    to the empty string on failure — the caller logs and skips.

    The okuro MCP middleware PREPENDS a compliance banner to every text tool
    result, so the artifact_write result the codex stream carries is
    ``[okuro·rules] …\n\n---\n\n<uuid>`` (claude returns the bare uuid). Some
    clients instead wrap it as a JSON object
    ``{"artifact_id": "<uuid>", "_compliance_warning": "[okuro·rules] …"}``.
    Left un-parsed, the banner-prefixed blob becomes the review key,
    mismatching the subagent's await_review key (the bare uuid it recovered)
    → the verdict never wakes the poll → the subagent polls to CAP and the
    task parks. So: unwrap a JSON object to its ``artifact_id``/``id`` field,
    else recover the trailing uuid embedded in the (banner-prefixed) text. A
    bare uuid matches itself and passes through (claude path unchanged).
    """

    def _unwrap(text: str) -> str:
        text = text.strip()
        if text.startswith("{"):
            try:
                obj = json.loads(text)
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict):
                got = obj.get("artifact_id") or obj.get("id")
                if got:
                    return str(got).strip()
        # Recover the artifact uuid from a banner-prefixed text result. The
        # bare uuid is the trailing token; take the last match so a uuid that
        # happens to appear earlier in the banner can't win.
        if not _ARTIFACT_UUID_RE.fullmatch(text):
            found = _ARTIFACT_UUID_RE.findall(text)
            if found:
                return found[-1]
        return text

    if raw is None:
        return ""
    if isinstance(raw, str):
        return _unwrap(raw)
    if isinstance(raw, list):
        parts: list[str] = []
        for piece in raw:
            if isinstance(piece, dict) and piece.get("type") == "text":
                parts.append(str(piece.get("text") or ""))
        candidate = "".join(parts).strip()
        if candidate:
            return _unwrap(candidate)
    if isinstance(raw, dict):
        # Already-parsed result object — prefer the explicit id field.
        got = raw.get("artifact_id") or raw.get("id")
        if got:
            return str(got).strip()
        candidate = str(raw.get("text") or "").strip()
        if candidate:
            return _unwrap(candidate)
    return ""


def _collect_implicated_acs(review: Dict[str, Any]) -> list[int]:
    """Extract the 1-based AC numbers implicated by a review verdict.

    The structured AC identity lives on deterministic ``ac_evidence_shape``
    findings, each of which carries ``ac_number`` (see
    :func:`okuro.orchestrator.reviewer.deterministic._check_ac_evidence_shape`).
    Critic findings do not expose an AC field, so the deterministic stage is
    the authoritative source. We scan every deterministic check's findings,
    collect each ``ac_number``, and return a de-duplicated, sorted list of
    ints — exactly the shape ``review_queue.publish_review`` coerces to.
    """
    acs: list[int] = []
    for result in review.get("deterministic_results") or []:
        if not isinstance(result, dict):
            continue
        for f in result.get("findings") or []:
            if not isinstance(f, dict):
                continue
            n = f.get("ac_number")
            if n is None:
                continue
            try:
                acs.append(int(n))
            except (TypeError, ValueError):
                continue
    return sorted(set(acs))


def _artifact_family_key(
    artifact_id: str,
    known: Dict[str, str],
    *,
    db_path: Optional[_Path] = None,
) -> str:
    """Identity of the DELIVERABLE an artifact is a revision of.

    ``rounds`` used to be a per-``artifact_write`` counter compared against
    ``max_rounds``, so a subtask shipping several distinct deliverables spent
    one budget across all of them and could CAP without ever revising
    anything. Measured: 60/275 subtask-instances over 30 days wrote more than
    one distinct-title artifact, 22 wrote four or more. Observed live on a
    real task — an AC-evidence artifact was reviewed as round 3 and the main
    deliverable as round 4, two deliverables consuming one budget.

    Family resolution, in order:

    1. ``supersedes`` pointing at an artifact whose family is already known —
       a revision inherits its predecessor's family. This is the strongest
       signal because ``artifact_supersede`` writes it explicitly.
    2. Normalised ``title`` — revisions keep their title, distinct
       deliverables do not.
    3. On ANY failure, the shared sentinel ``_FAMILY_FALLBACK``. That
       collapses every artifact into one family, which is the PRE-EXISTING
       behaviour: one budget for the subtask. Fail CLOSED — a fallback that
       gave each artifact its own family would silently uncap the loop.

    ``known`` is the running ``artifact_id -> family`` map for this dispatch
    and is updated in place.
    """
    if not artifact_id:
        return _FAMILY_FALLBACK
    if artifact_id in known:
        return known[artifact_id]

    db = db_path or _resolve_okuro_db_path()
    family = _FAMILY_FALLBACK
    try:
        if db.exists():
            conn = sqlite3.connect(str(db), timeout=2.0)
            try:
                row = conn.execute(
                    "SELECT title, supersedes FROM artifacts WHERE id = ?",
                    (artifact_id,),
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                title, supersedes = row[0], row[1]
                if supersedes and supersedes in known:
                    family = known[supersedes]
                elif title and str(title).strip():
                    family = "title:" + re.sub(
                        r"\s+", " ", str(title).strip().lower(),
                    )[:200]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "artifact family lookup failed for %s (%r) — falling back to a "
            "single shared round budget",
            artifact_id, exc,
        )
        family = _FAMILY_FALLBACK

    known[artifact_id] = family
    return family


def _run_reviewer_for_artifact(
    *,
    task: Task,
    subtask: Subtask,
    artifact_id: str,
    config: Config,
    review_runner: Any,
    attempt: int,
    max_attempts: int,
    prior_findings: Optional[list] = None,
) -> Dict[str, Any]:
    """Run M3 critic+scorer against a single artifact.

    The existing :func:`okuro.orchestrator.reviewer.pipeline.run_review`
    operates on a phase. For session-loop we want PER ARTIFACT, so we
    synthesize a single-subtask phase shim that scopes the reviewer's
    ``_collect_deliverables_text`` to just this artifact. The critic +
    scorer pipeline then collapses into one verdict — that single value
    IS the reconciliation point (see PR 2 handover open questions).

    Returns ``{"verdict", "findings", "implicated_acs"}`` with the
    response shape :func:`publish_review` expects.
    """
    # Build a one-subtask phase shim. Use SimpleNamespace so we don't
    # pollute the canonical Phase dataclass; the reviewer only reads
    # ``id`` and ``subtasks`` off the phase.
    from types import SimpleNamespace
    # WP-H — the phase id here decides how the FE attributes every reviewer
    # activity row. Subtask has a `phase` field (state.py); it has NO
    # `phase_id`, so the old getattr fell through to 0 and EVERY reviewer row
    # was tagged `reviewer:phase0:*`. activity-feed.tsx attributes those rows
    # by matching that number against the selected subtask's parent phase
    # (parentPhaseId("1.2") == 1), so nothing ever matched and the whole
    # reviewer stream was filtered out of the feed. That is why the reviewer
    # was invisible in the UI — not a missing component, a wrong field name.
    phase_shim = SimpleNamespace(
        id=(
            getattr(subtask, "phase", None)
            or getattr(subtask, "phase_id", None)
            or 0
        ),
        subtasks=[subtask],
        serialize=False,
        status="running",
        decision_gate=None,
    )

    try:
        # PR 3 originally passed persist_event=False to avoid "drowning M2"
        # with per-artifact verdict events. That suppression ALSO killed
        # the FE-visible reviewer signal — .activity.jsonl rows under
        # subtask_id="reviewer:phase<N>:deterministic|critic|scorer" and
        # log.jsonl markers (review_started, critic_finding,
        # scorer_decision, verdict_published, review_complete_event) that
        # the FE Reviewer Panel + activity feed read. Flipping back: a
        # noisy event log is recoverable; an invisible reviewer pipeline
        # erodes user trust. Volume is bounded by max_rounds (5) per
        # artifact × seen_artifact_ids — not unbounded.
        # WP7 — thread the engine's resolved tasks_dir so the reviewer's
        # emit/deterministic/activity-sink resolution matches the dispatch
        # config (not the DEFAULT tasks_dir). Only pass it when the runner
        # accepts it: the real run_review does; rigid test stubs with a
        # fixed (task, phase, persist_event, strict) signature do not, and
        # must stay callable without it (backward-compat).
        review_kwargs: Dict[str, Any] = {
            "task": task, "phase": phase_shim,
            "persist_event": True, "strict": False,
        }
        try:
            import inspect
            _rparams = inspect.signature(review_runner).parameters
            if "tasks_dir" in _rparams:
                review_kwargs["tasks_dir"] = config.tasks_dir
            # ROCK-SOLID v5 P0#5 — queue_wait_s instrumentation. completed_at
            # is stamped when the subagent's work span ended, i.e. the moment
            # this artifact became reviewable; the gap to review actually
            # starting is dispatch-loop scheduling delay.
            if "queued_at" in _rparams and getattr(subtask, "completed_at", ""):
                review_kwargs["queued_at"] = subtask.completed_at
            # Stateful verification: hand the reviewer the PRIOR round's
            # findings so it verifies each (resolved/still-open) instead of
            # re-deriving a fresh set every round. Guarded so rigid test stubs
            # without the param stay callable.
            if "prior_findings" in _rparams and prior_findings:
                review_kwargs["prior_findings"] = prior_findings
            # Thread the configured reviewer tier. Without this the critic +
            # scorer silently fell back to ``model or "haiku"`` (critic.py /
            # scorer.py) — a haiku-class reviewer judging a max-tier strategy
            # brief. run_review escalates conceptual deliverables to the
            # strategic tier internally.
            _orch = getattr(config, "orchestrator", None)
            if "review_model" in _rparams:
                review_kwargs["review_model"] = getattr(_orch, "review_model", None)
            if "review_model_conceptual" in _rparams:
                review_kwargs["review_model_conceptual"] = getattr(
                    _orch, "review_model_conceptual", None
                )
            # Reviewer runs on the TASK's provider (cli_default, set by
            # apply_cli_preference), matching the DAG-path gate. Without this
            # the streaming reviewer threaded the codex review MODEL
            # (model_reasoning_effort=<tier>) but left provider=None → the
            # critic fell back to claude and fed CLAUDE a codex model string
            # → "model may not exist" → empty output → critic infra_error →
            # NEEDS_USER. The provider and the tier model MUST come from the
            # same config so preferred_cli=codex actually reviews on codex.
            _rev_provider = getattr(config, "cli_default", None) or None
            if "provider" in _rparams:
                review_kwargs["provider"] = _rev_provider
            # Codex is spawn-per-turn and SLOW: a strategic-tier (high effort)
            # critic+scorer runs minutes/round and the subagent's await_review
            # patience (~2-3 min observed) runs out before the verdict lands,
            # so the task parks at blocked_review. Pin the CODEX reviewer to
            # the FAST effort tier so critic+scorer return before the subagent
            # gives up. Explicit critic_model/scorer_model win over
            # run_review's internal tier escalation. claude/gemini keep their
            # resolved (higher) reviewer tier — they judge in seconds.
            if _rev_provider == "codex":
                try:
                    from okuro.orchestrator.config import resolve_tier_model
                    _fast = resolve_tier_model(config, "fast", "codex")
                except Exception:  # noqa: BLE001
                    _fast = ""
                if _fast:
                    if "critic_model" in _rparams:
                        review_kwargs["critic_model"] = _fast
                    if "scorer_model" in _rparams:
                        review_kwargs["scorer_model"] = _fast
        except (TypeError, ValueError):
            pass
        review = review_runner(**review_kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[%s/%s] reviewer crashed on artifact=%s — defaulting to FAIL",
            task.id, subtask.id, artifact_id,
        )
        return {
            "verdict": "FAIL",
            "findings": [{
                "severity": "load_bearing",
                "summary": f"reviewer crashed: {exc}",
                "artifact_id": artifact_id,
            }],
            "implicated_acs": [],
            "stage_timings": {},
        }

    raw_verdict = (review.get("verdict") or "FAIL").upper()

    # Tag every finding with the artifact id so the subagent's
    # await_review response can point at the right blob.
    findings = list(review.get("critic_findings") or [])
    for f in findings:
        if isinstance(f, dict) and "artifact_id" not in f:
            f["artifact_id"] = artifact_id

    # Critic-stage infra error: the critic's bridge call timed out or its
    # output stayed unparseable even after the hardened parser AND its internal
    # bridge retries (critic.py:542). The deliverable was therefore NOT actually
    # reviewed. The pre-fix behaviour accepted it as PASS — but on a phase that
    # is not M3-gated this lenient PASS is the ONLY verdict, so a single
    # PERSISTENT reviewer outage silently shipped unreviewed work (audit defect
    # #3). Fail CLOSED instead, mirroring the phase-gate (engine.py:1497) which
    # escalates the same critic_infra_error to a recoverable blocked_review:
    # return a terminal NEEDS_USER and flag the envelope so the dispatch loop
    # tags the session-loop result as an INFRA cause. The user then sees the
    # "transient — just retry, no work changes needed" surface (engine.py:4294),
    # never a false accept. Findings are kept honest (load_bearing NOT demoted).
    if review.get("critic_infra_error"):
        logger.warning(
            "[%s/%s] critic infra_error on artifact=%s — escalating to "
            "blocked_review (reviewer unavailable); NOT shipping unreviewed work",
            task.id, subtask.id, artifact_id,
        )
        return {
            "verdict": "NEEDS_USER",
            "findings": findings,
            "implicated_acs": _collect_implicated_acs(review),
            "reviewer_infra_error": True,
            "stage_timings": review.get("stage_timings") or {},
        }

    # PR 1's review_queue accepts only PASS/FAIL/CAP/NEEDS_USER.
    # ``run_review`` may emit CONDITIONAL. INVARIANT (verified at
    # reviewer/pipeline.py:363-373 + scorer.py:94-95): CONDITIONAL is
    # reachable ONLY when there are ZERO load_bearing findings — any
    # load_bearing finding (deterministic short_circuit OR critic) is
    # forced to FAIL during verdict reconciliation. So CONDITIONAL means
    # "acceptable, cosmetic notes only" and MUST NOT trigger a retry round.
    # Map it to PASS for the session-loop; the cosmetic findings are still
    # surfaced as advisory notes below (never dropped).
    if raw_verdict == "CONDITIONAL":
        verdict = "PASS"
    elif raw_verdict in {"PASS", "FAIL", "CAP", "NEEDS_USER"}:
        verdict = raw_verdict
    else:
        verdict = "FAIL"

    return {
        "verdict": verdict,
        "findings": findings,
        # AC identity comes from deterministic ac_evidence_shape findings
        # (each carries a 1-based ``ac_number``). Critic findings have no AC
        # field, so the deterministic stage is the authoritative source.
        "implicated_acs": _collect_implicated_acs(review),
        "stage_timings": review.get("stage_timings") or {},
    }


__all__ = ["dispatch_subtask_streaming"]
