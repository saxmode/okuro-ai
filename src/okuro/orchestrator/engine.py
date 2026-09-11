# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Okuro Orchestrator Engine
# index:
#   imports
#   imports
#   def signal_handler
#   def _is_process_alive
#   def _is_orchestrator_running
#   def main
#   def _finalize_task
#   def deliberation_loop
#   def orchestrator_loop
#   def handle_approval
#   def handle_high_risk
#   def wait_for_approval
#   def handle_failure
#   def print_plan
#   def print_summary
#   def _generate_continuation_suggestions
#   def list_tasks
#   def update_task_status
#   def format_duration
#   def _brain_session_start
#   def _brain_session_end
#   def _log_to_brain
#   def _harvest_learnings
# AGENT_HEADER_END -->
"""
Okuro Orchestrator Engine

Main entry point for the autonomous workforce orchestrator.
Receives tasks, decomposes, dispatches subtasks, tracks state.
"""

import argparse
import atexit
import json
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from typing import Optional

from okuro.clock import utc_now_naive

import logging

logger = logging.getLogger("okuro.orchestrator")

from okuro.orchestrator.config import (
    apply_cli_preference, load_config, validate_cli_binaries, Config,
    DEFAULT_MAX_RETRIES,
)
from okuro.orchestrator.state import (
    create_task, save_plan, load_task, update_subtask,
    get_next_subtask, get_ready_subtasks, is_phase_complete, is_task_complete,
    save_artifact, append_log, append_phases, apply_supersedes, Task, Subtask,
    pending_decision_gates, resolve_decision_gate, blocked_upstream_ids,
)
from okuro.orchestrator.validation import validate_task
from okuro.orchestrator.checkpoint import create_checkpoint, get_latest_checkpoint, CheckpointType
from okuro.orchestrator.decomposer import decompose_task, decompose_continuation
from okuro.orchestrator.dispatcher import dispatch_position
from okuro.orchestrator.dispatcher_streaming import (
    dispatch_subtask_streaming,
    is_transient_infra_error,
)
from okuro.orchestrator.sentinel import Sentinel
from okuro.orchestrator.channels import notify_channel
from okuro.orchestrator.decision_brief import brief_payload as _brief_payload
# ROCK-SOLID v5 D-A — user-facing plan nouns come from the vocabulary module,
# never from a literal at the emit site. Module-level (the humanize_gate
# imports further down are function-local for historical reasons only;
# gate_messages imports nothing from this package, so there is no cycle).
from okuro.orchestrator.gate_messages import step_label as _step, part_label as _part
from okuro.orchestrator.yamlfast import yload

# Global flag for graceful shutdown
shutdown_requested = False

# ROCK-SOLID v5 P4.5 — set by the source watcher THREAD, acted on by the MAIN
# loop between dispatches.
#
# The watcher used to call sys.exit(0) directly. It runs in a daemon thread,
# and sys.exit() off the main thread raises SystemExit in THAT thread only —
# the thread died, the process carried on with the stale modules it had
# already imported, and the `engine_restart_requested` event it had just
# written was a lie. Every "the engine picked up my fix" belief resting on
# that event was wrong; only SIGKILL + respawn ever worked.
#
# A flag instead. The main loop already polls `shutdown_requested` at the top
# of each iteration, which is a natural boundary: no dispatch is mid-flight,
# so the exit is clean and the launcher's respawn loop re-execs against fresh
# modules. Deliberately NOT a threading.Event — the loops read a plain bool
# for shutdown and consistency beats novelty here.
restart_requested = False

# Track the task this engine instance owns so abnormal exits (SIGTERM from
# `okuro service stop` / cancel API, OOM kill, uncaught exception, parent
# shell closing) leave task.yaml in a terminal state instead of stuck on
# "active" / "planning". Without this the dashboard reads stale state and
# the stop button errors with 409 because the process is gone but the file
# still claims active.
_ACTIVE_TASK_ID: Optional[str] = None
_ACTIVE_TASKS_DIR: Optional[Path] = None


# ---------------------------------------------------------------------------
# PR A — orchestrator_state heartbeat. Pre-fix `_emit_orchestrator_state`
# fired at transition boundaries only. Long bridge_invoke calls (reviewer +
# decomposer, 30s–5min) blocked the work loop with zero re-emit, so the
# OkuroThinker pill stayed glued to the last label until the next state
# transition. A separate daemon thread re-emits the *last known state* at
# a fixed cadence so the FE WS pool sees a steady heartbeat even when the
# work loop is blocked on a synchronous LLM call.
#
# Idempotence: we only re-emit the same shape (state + label) the engine
# last called ``_emit_orchestrator_state`` with — never invent new state.
# If the last state is "idle"/"" we skip, so the heartbeat never overrides
# a deliberate idle.
# ---------------------------------------------------------------------------
_HEARTBEAT_INTERVAL_S = 3.0
_LAST_STATE_STATE: str = "idle"
_LAST_STATE_LABEL: str = ""
_HEARTBEAT_THREAD = None
_HEARTBEAT_STOP = None  # threading.Event — set by stop_heartbeat


def _record_last_state(state: str, label: str) -> None:
    """Update module-level cache so the heartbeat thread can re-emit it."""
    global _LAST_STATE_STATE, _LAST_STATE_LABEL
    _LAST_STATE_STATE = state or "idle"
    _LAST_STATE_LABEL = label or ""


def start_orchestrator_heartbeat(task_id: str, tasks_dir: Path) -> None:
    """Spawn the heartbeat daemon thread for this engine instance.

    Safe to call more than once: the second call exits silently because
    the global thread reference is already set. Stop via ``stop_orchestrator_heartbeat``.
    """
    global _HEARTBEAT_THREAD, _HEARTBEAT_STOP
    if _HEARTBEAT_THREAD is not None and _HEARTBEAT_THREAD.is_alive():
        return
    import threading

    _HEARTBEAT_STOP = threading.Event()

    def _loop():
        while not _HEARTBEAT_STOP.wait(_HEARTBEAT_INTERVAL_S):
            # PR C — refresh the engine lease on every heartbeat tick so
            # the TTL stays ahead of any new engine probing for ownership.
            try:
                write_engine_lease(task_id, tasks_dir)
            except Exception:
                pass

            state = _LAST_STATE_STATE
            label = _LAST_STATE_LABEL
            # Skip emitting idle states — they would re-light the badge on
            # tasks that legitimately reached idle/waiting_user.
            if not state or state in ("idle", "complete"):
                continue
            # ROCK-SOLID v5 P1.2 — tie a "waiting" (or genuine-fault "error")
            # re-emit to awaiting still being present on disk. This in-memory
            # cache (_LAST_STATE_STATE) only updates when THIS process calls
            # _emit_orchestrator_state; a resolve happens in the API process
            # via clear_awaiting and this engine process is never told —
            # without this check the heartbeat re-broadcasts a cleared gate
            # forever (measured: 347 rows, 17 min, on a resolved gate).
            if state in ("waiting", "error"):
                try:
                    if getattr(load_task(task_id, tasks_dir), "awaiting", None) is None:
                        continue
                except Exception:
                    pass  # can't confirm either way — keep the existing behaviour
            try:
                from okuro.orchestrator.state import append_log
                append_log(task_id, {
                    "type": "orchestrator_state",
                    "state": state,
                    "label": label,
                    "heartbeat": True,
                }, tasks_dir)
            except Exception:
                pass

    _HEARTBEAT_THREAD = threading.Thread(
        target=_loop, name=f"okuro-orchestrator-heartbeat-{task_id}",
        daemon=True,
    )
    _HEARTBEAT_THREAD.start()


def stop_orchestrator_heartbeat() -> None:
    """Signal the heartbeat thread to exit. Idempotent."""
    global _HEARTBEAT_THREAD
    if _HEARTBEAT_STOP is not None:
        try:
            _HEARTBEAT_STOP.set()
        except Exception:
            pass
    _HEARTBEAT_THREAD = None


def _phase_is_serialized(task, subtask) -> bool:
    """True when the subtask's phase has serialize=True or task.gates_enabled."""
    try:
        if bool(getattr(task, "gates_enabled", False)):
            return True
        for phase in getattr(task, "phases", []) or []:
            for st in getattr(phase, "subtasks", []) or []:
                if getattr(st, "id", None) == getattr(subtask, "id", None):
                    return bool(getattr(phase, "serialize", False))
    except Exception:
        return False
    return False


def _maybe_compress(task, subtask, config) -> None:
    """Run the compressor before dispatch when the task is decision-laden.

    M2 pre-dispatch hook. Conditions:
      1. phase.serialize or task.gates_enabled (drift-cost is high)
      2. at least one new task_event since the last compression
    Otherwise no-op. Skip on failure — never block dispatch.
    """
    if not _phase_is_serialized(task, subtask):
        return
    try:
        from okuro.sense.task_events import list_events, latest_compression
        from okuro.orchestrator.compressor import run_compression
    except Exception:
        return

    prior = latest_compression(task_id=task.id)
    since_seq = int((prior.get("body") or {}).get("covers_seq_to") or 0) if prior else 0
    new_events = list_events(task_id=task.id, since_seq=since_seq, limit=2)
    if not new_events:
        return  # nothing to compress yet

    result = run_compression(
        task_id=task.id,
        task_description=task.description,
        project_path=getattr(task, "project_path", "") or "",
        adrs=list(getattr(task, "adrs", []) or []),
    )
    if result.get("ok"):
        try:
            append_log(task.id, {
                "type": "compression_emitted",
                "subtask": subtask.id,
                "event_id": result.get("event_id"),
                "artifact_id": result.get("artifact_id"),
                "prompt_chars": result.get("prompt_chars"),
                "output_chars": result.get("output_chars"),
            }, config.tasks_dir)
        except Exception:
            pass


def _parse_workflow_params(pairs: list[str] | None) -> dict[str, str] | None:
    """``["artifact=abc", "who=the board"]`` -> ``{"artifact": "abc", ...}``.

    Split on the FIRST ``=`` only, so a value may contain more of them. A pair
    with no ``=`` is a typo the user needs to see, not a silently dropped
    parameter — an unfilled ``{placeholder}`` would otherwise reach a subagent
    verbatim and quietly poison the brief.
    """
    if not pairs:
        return None
    out: dict[str, str] = {}
    for raw in pairs:
        key, sep, value = raw.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"--workflow-param expects KEY=VALUE, got {raw!r}")
        out[key.strip()] = value
    return out


def _set_active_task(task_id: str, tasks_dir: Path) -> None:
    """Register the task this engine owns so atexit can reconcile its state."""
    global _ACTIVE_TASK_ID, _ACTIVE_TASKS_DIR
    _ACTIVE_TASK_ID = task_id
    _ACTIVE_TASKS_DIR = tasks_dir


# C4 — engine startup reconciler. .review-active.json is engine-owned: the
# engine writes it in _run_phase_review_gates and unlinks it on the same
# pass. When the engine crashes (SIGKILL, OOM, parent gone) mid-review the
# sentinel survives forever and state_reader keeps reporting "Reviewing"
# (UI3 root). On every engine entry, drop a stale sentinel before the work
# loop sees it. Under C4's spawn-lock cohort only one engine per task can
# exist at a time; any sentinel observed at startup belongs to a dead
# predecessor and must be cleared.
_REVIEW_ACTIVE_TTL_S = 30.0 * 60.0  # 30 min — matches audit-07 UI3 wedge ceiling


def reap_predecessor_sessions(task_id: str, tasks_dir: Path) -> int:
    """Cancel whatever a previous engine left running for this task (P4.2/P4.3).

    Runs at engine boot, BEFORE ``reconcile_on_startup``. The ordering is the
    whole point: reconcile revives orphaned subtasks, so reviving before
    reaping hands the new engine work that a still-live predecessor subagent
    is also writing — two sessions on one subtask, both producing artifacts,
    and the reviewer grading whichever landed last. P4.2's dispatch lease
    prevents a SECOND spawn; this clears the FIRST one when the engine that
    owned it is gone.

    Keys on session ROWS, not plan status, per the plan: a subtask can read
    "running" with nothing alive, and "pending" with a subagent still writing.
    The rows are the only record of what is actually out there.

    SAFETY — why this does not simply killpg. A process group kill is correct
    only if the subagent leads its own group. The legacy dispatcher has spawned
    with start_new_session=True for a long time; the streaming adapters did NOT
    until this change, so any subagent started by an older engine still alive
    right now shares THIS engine's process group. Signalling that group would
    kill the engine doing the reaping. So the group is verified to be the
    process's own before it is signalled, and otherwise only the single pid is.

    Returns the number of sessions signalled — 0 is the normal case.
    """
    import os as _os
    import signal as _signal

    try:
        from okuro.sense.telemetry import open_sessions_for_task
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[%s] reap: telemetry unavailable (%r)", task_id, exc)
        return 0

    try:
        rows = open_sessions_for_task(task_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[%s] reap: session enumeration failed (%r)", task_id, exc)
        return 0

    me = _os.getpid()
    my_group = _os.getpgrp()
    reaped = 0

    for row in rows:
        pid = row.get("pid")
        sid = row.get("session_id", "")
        if not pid or int(pid) == me:
            continue
        pid = int(pid)
        try:
            _os.kill(pid, 0)
        except (OSError, ValueError):
            continue  # already gone; the row is stale, not a live predecessor

        target_group = None
        try:
            pgid = _os.getpgid(pid)
            # Its OWN group, and not the group this engine lives in.
            if pgid == pid and pgid != my_group:
                target_group = pgid
        except OSError:
            target_group = None

        try:
            if target_group is not None:
                _os.killpg(target_group, _signal.SIGTERM)
            else:
                # Shares our group (a pre-P4.1 spawn) — the single process
                # only. Its children leak, which is strictly better than
                # signalling a group containing this engine.
                logger.warning(
                    "[%s] reap: session %s pid %s is not its own group leader "
                    "— signalling the process alone", task_id, sid, pid,
                )
                _os.kill(pid, _signal.SIGTERM)
            reaped += 1
            logger.warning(
                "[%s] reaped predecessor session %s (pid %s, subtask %s)",
                task_id, sid, pid, row.get("subtask_id", "?"),
            )
        except OSError as exc:
            logger.warning("[%s] reap: signalling pid %s failed: %r", task_id, pid, exc)

    if reaped:
        try:
            from okuro.orchestrator.state import emit_event as _emit

            _emit(task_id, "predecessor_sessions_reaped", {
                "count": reaped,
                "subtask_ids": [r.get("subtask_id", "") for r in rows][:20],
            }, tasks_dir)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[%s] reap emit failed: %r", task_id, exc)
    return reaped


def reconcile_on_startup(task_id: str, tasks_dir: Path) -> None:
    """Clear stale per-task sentinels before the work loop starts.

    Called once at engine entry (--resume, --continue, new-task). Idempotent
    and best-effort: any failure logs and proceeds. Closes UI3 (stale
    .review-active.json reads forever) by construction — the sentinel is
    engine-owned and the only correct moment to clear it is at the start of
    the next engine that owns the task.
    """
    try:
        task_dir = Path(tasks_dir) / task_id
    except Exception:
        return
    if not task_dir.exists():
        return

    # Auto-heal halt-interrupted subtasks. A crash / force-halt marks the
    # running subtasks failed with "task halted before subtask completed".
    # On the next engine (respawned by the daemon or /resume) those were
    # INTERRUPTED, not genuinely failed — reset them to pending so the task
    # SELF-HEALS to completion without a human retry-click (the last gap to
    # fully-autonomous transient-crash recovery). A genuine permanent fail
    # carries a different error (and lives under a blocked_review phase the
    # user must resolve), so it is left alone; the per-subtask retry cap still
    # bounds re-runs.
    try:
        plan_yaml = task_dir / "plan.yaml"
        if plan_yaml.exists():
            pdata = yload(plan_yaml.read_text()) or {}
            healed: list[str] = []
            for ph in pdata.get("phases") or []:
                if ph.get("status") == "blocked_review":
                    continue  # user must resolve this phase — don't revive
                for st in ph.get("subtasks") or []:
                    status = st.get("status")
                    halt_interrupted = (
                        status == "failed"
                        and "task halted" in (st.get("error") or "").lower()
                    )
                    # Orphaned 'running' subtask: an engine that just booted
                    # has dispatched nothing yet, so anything still 'running'
                    # on disk was left by a DEAD engine (crash / force-kill /
                    # parallel-sibling state clobber). It will never complete
                    # on its own and deadlocks every later-phase dependant
                    # (no_subtasks_ready / blocked_review). Revive it so the
                    # plan self-heals to completion — same contract as the
                    # halt-interrupt reset; the per-subtask retry cap still
                    # bounds re-runs.
                    orphaned_running = status == "running"
                    if halt_interrupted or orphaned_running:
                        sid = st.get("id")
                        if sid:
                            healed.append(str(sid))
            for sid in healed:
                try:
                    update_subtask(task_id, sid, {"status": "pending", "error": ""}, tasks_dir)
                except Exception as exc:
                    logger.warning("[%s] orphan/halt reset of %s failed: %r", task_id, sid, exc)
            if healed:
                logger.info(
                    "[%s] reconcile_on_startup auto-reset %d orphaned/halt-interrupted "
                    "subtask(s) to pending (self-heal): %s",
                    task_id, len(healed), ", ".join(healed),
                )
    except Exception as exc:
        logger.warning("[%s] orphan/halt reconcile failed: %r", task_id, exc)

    # Reap per-subtask in-session review flags first (engine-owned, like the
    # phase flag): a crashed session-loop can leave .review-active.<id>.json
    # behind, which would keep the tile's "Reviewing" badge lit forever.
    try:
        for _sub_flag in task_dir.glob(".review-active.*.json"):
            try:
                _sub_flag.unlink()
                logger.info(
                    "[%s] reconcile_on_startup cleared %s", task_id, _sub_flag.name,
                )
            except OSError:
                pass
    except Exception as exc:
        logger.warning("[%s] per-subtask review-flag reap failed: %r", task_id, exc)

    flag = task_dir / ".review-active.json"
    if not flag.exists():
        return
    try:
        age = time.time() - flag.stat().st_mtime
    except OSError:
        age = float("inf")
    try:
        flag.unlink()
    except OSError as exc:
        logger.warning("[%s] reconcile_on_startup unlink failed: %r", task_id, exc)
        return
    if age < _REVIEW_ACTIVE_TTL_S:
        logger.info(
            "[%s] reconcile_on_startup cleared .review-active.json (age=%.1fs)",
            task_id, age,
        )
    else:
        logger.info(
            "[%s] reconcile_on_startup cleared stale .review-active.json "
            "(age=%.1fs > TTL=%.0fs)",
            task_id, age, _REVIEW_ACTIVE_TTL_S,
        )


def _exit_is_a_restart() -> bool:
    """Is this process leaving on purpose, to come back on fresh code?

    P4.5's source-change restart exits through the SAME DOOR as a crash, and
    the two atexit handlers below could not tell them apart. Measured live
    2026-08-01 (task-20260801-184853): a touched source file restarted the
    engine, ``_reconcile_on_exit`` read status=active and reset a subtask that
    had published verdict PASS 40 seconds earlier to failed/not_reviewed, and
    ``_stop_own_scope_on_exit`` systemctl-stopped the cgroup the launcher's
    respawn loop lives in — so nothing came back. Task halted, work discarded.

    Two independently-correct safety mechanisms that destroy work when
    composed. One flag both consult, per the class fix: not a third handler,
    which would only add a third thing to compose.
    """
    return bool(restart_requested)


def _reconcile_on_exit() -> None:
    """If we exit while task.yaml still says active/planning, mark it halted.

    Three layers healed in one shot so the dashboard reflects truth on the
    next read without waiting for a passive reconcile:
      1. task.yaml.status:    active|planning → halted
      2. plan.yaml subtasks:  running|waiting_approval → halted
      3. log.jsonl:           emit orchestrator_state=idle so the API's
                              EventWatcher relays it to OkuroThinker

    Idempotent — only writes when in a transient state. Best-effort:
    swallows everything because this runs during interpreter teardown.

    Skipped entirely on a restart exit: the states it would "heal" are VALID
    — the engine is leaving on purpose and a successor is coming for exactly
    this task. Halting there does not describe reality, it destroys it.
    """
    if _ACTIVE_TASK_ID is None or _ACTIVE_TASKS_DIR is None:
        return
    if _exit_is_a_restart():
        # Leave a trace, because "no halt event" and "no engine" look the
        # same in log.jsonl, and this exit is the one an operator most needs
        # to be able to see.
        try:
            from okuro.clock import utc_now_naive

            with open(_ACTIVE_TASKS_DIR / _ACTIVE_TASK_ID / "log.jsonl", "a") as f:
                f.write(json.dumps({
                    "type": "engine_restart_exit",
                    "ts": utc_now_naive().isoformat(),
                    "reason": "source_change — task state left intact for the "
                              "successor engine",
                }) + "\n")
        except Exception:
            pass
        return
    # Ownership guard — do NOT stamp halt if a DIFFERENT, still-alive engine
    # holds the lease. An evicted duplicate (a --continue engine that lost the
    # continue-vs-resume race, or a reconciler double-spawn) exiting must not
    # flip the live owner's task to `halted` or reset its running subtasks to
    # failed. Only the lease-owner — or an engine when no live lease exists —
    # may write the truthful terminal state. Without this a losing duplicate's
    # teardown stamped a spurious `halted` on a task that was actively running.
    try:
        _lease = read_engine_lease(_ACTIVE_TASK_ID, _ACTIVE_TASKS_DIR)
        if _lease:
            _lpid = int(_lease.get("pid", 0) or 0)
            if _lpid and _lpid != os.getpid() and _is_process_alive(_lpid):
                return
    except Exception:
        pass
    # Make the truthful-state write uninterruptible. SIGTERM is also disarmed
    # in _sigterm_handler, but this covers the non-signal exit paths
    # (normal sys.exit from finalize, an uncaught exception) where an
    # async SIGTERM — e.g. _stop_own_scope_on_exit's `systemctl stop` on our
    # own cgroup — could otherwise raise SystemExit mid-yaml-parse and wedge
    # the task in active/planning. Left blocked: the process is exiting.
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    except (ValueError, OSError, AttributeError):
        pass
    try:
        task_yaml = _ACTIVE_TASKS_DIR / _ACTIVE_TASK_ID / "task.yaml"
        if not task_yaml.exists():
            return
        with open(task_yaml) as f:
            data = yload(f) or {}
        if data.get("status") not in ("active", "planning"):
            return
        data["status"] = "halted"
        # Atomic write — this runs inside the SIGTERM handler, so a service
        # restart can interrupt it; a plain truncating write would leave a
        # 0-byte task.yaml and brick the task.
        from okuro.orchestrator.state import atomic_dump_yaml, couple_task_dict
        # P4.10 — an abnormal exit (SIGTERM, OOM, crash) marked the task
        # halted and kept any gate it was carrying, so the dashboard offered
        # a decision that no engine was alive to receive.
        atomic_dump_yaml(
            task_yaml, couple_task_dict(data), default_flow_style=False,
        )

        plan_yaml = _ACTIVE_TASKS_DIR / _ACTIVE_TASK_ID / "plan.yaml"
        reset_count = 0
        if plan_yaml.exists():
            try:
                with open(plan_yaml) as f:
                    plan_data = yload(f) or {}
                touched = False
                # Subtask schema doesn't allow "halted"; "failed" with an
                # explanatory error is the closest terminal state. Phase
                # schema is even tighter (pending|active|done) so phase
                # status is left alone — the per-subtask render is what
                # users see.
                for phase in plan_data.get("phases") or []:
                    for st in phase.get("subtasks") or []:
                        if st.get("status") in ("running", "waiting_approval"):
                            st["status"] = "failed"
                            if not st.get("error"):
                                st["error"] = "task halted before subtask completed"
                            reset_count += 1
                            touched = True
                if touched:
                    from okuro.orchestrator.state import atomic_dump_yaml
                    atomic_dump_yaml(plan_yaml, plan_data, default_flow_style=False)
            except Exception:
                pass

        log_path = _ACTIVE_TASKS_DIR / _ACTIVE_TASK_ID / "log.jsonl"
        ts = datetime.utcnow().isoformat()
        with open(log_path, "a") as f:
            f.write(json.dumps({
                "type": "task_halted_on_exit",
                "ts": ts,
                "reason": "engine exited before terminal status write",
                "subtasks_reset": reset_count,
            }) + "\n")
            # Emit orchestrator_state=idle — the API's EventWatcher tails
            # log.jsonl and relays orchestrator_state events to /ws/activity,
            # so OkuroThinker hides without needing a direct WS broadcast
            # (which we can't do from a separate process anyway).
            f.write(json.dumps({
                "type": "orchestrator_state",
                "state": "idle",
                "label": "",
                "ts": ts,
            }) + "\n")
    except Exception:
        pass


atexit.register(stop_orchestrator_heartbeat)


def _clear_lease_on_exit() -> None:
    """PR C — release the engine lease on clean Python exit so a fast
    follow-up spawn doesn't have to wait for the TTL to age out."""
    if _ACTIVE_TASK_ID is None or _ACTIVE_TASKS_DIR is None:
        return
    try:
        clear_engine_lease(_ACTIVE_TASK_ID, _ACTIVE_TASKS_DIR)
    except Exception:
        pass


atexit.register(_clear_lease_on_exit)


def _stop_own_scope_on_exit() -> None:
    """Reap our own systemd-run --user --scope on engine exit.

    Without this, anything the subagent backgrounded (dev servers,
    MCP servers, http file servers it spawned for QA) keeps the
    cgroup non-empty after the engine exits, leaving the scope
    "active running" forever. Over days, accumulated orphan helpers
    hold okuro.db file handles open and starve new tasks of DB write
    throughput. ``systemctl stop`` on a transient scope is an atomic
    cgroup kill — takes the whole tree out, no race, idempotent.

    Best-effort: if we can't derive our scope name we silently no-op
    so this never blocks a clean exit. Discovery via /proc/self/cgroup
    handles both the systemd-run path and the fallback subprocess path
    where the scope name is absent (no-op).

    NOT on a restart exit. The launcher whose respawn loop brings the engine
    back runs INSIDE this scope (api/main.py spawns `systemd-run --user
    --scope --unit=okuro-<task>-<ts> -- launcher.sh`), and stopping a
    transient scope is an atomic cgroup kill of the whole tree. Cleaning up
    here would kill the supervisor that is the entire point of restarting —
    measured: no `[LAUNCHER] … respawning` line, launcher gone, task halted.
    The successor engine's own exit does this cleanup when it leaves for real.
    """
    if _exit_is_a_restart():
        return
    try:
        with open("/proc/self/cgroup") as f:
            cgline = f.read().strip()
    except Exception:
        return
    # cgroup v2 single line shape:
    #   0::/user.slice/.../app.slice/okuro-task-<id>-<spawn>.scope
    scope = None
    for token in cgline.split("/"):
        if token.endswith(".scope") and token.startswith("okuro-task-"):
            scope = token
            break
    if not scope:
        return
    try:
        import subprocess as _sp
        _sp.Popen(
            ["systemctl", "--user", "stop", "--no-block", scope],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


atexit.register(_stop_own_scope_on_exit)
# Registered LAST so it runs FIRST (atexit is LIFO): the truthful-state
# write must complete BEFORE _stop_own_scope_on_exit tears down our own
# cgroup via `systemctl stop` (whose async SIGTERM previously raced into
# the reconcile and wedged the task in active/planning).
atexit.register(_reconcile_on_exit)


# PR C — subagent wedge detector. When the okuro MCP server restarts
# during a long subagent stream the session can lose its registry
# connection briefly and end up emitting nothing for minutes while
# still "alive". The engine cannot auto-reconnect the session (that's
# a larger refactor, deferred), but it CAN observe the silence and
# emit ``subagent_wedged`` so the FE can surface a "stuck — kill the
# scope?" affordance. ``_WEDGE_TTL_S`` is the silence threshold; one
# event per subtask per wedge episode.
_WEDGE_TTL_S = 120.0
_WEDGE_EMITTED: set[str] = set()


def check_subagent_wedged(task_id: str, subtask_id: str,
                           tasks_dir: Path) -> bool:
    """One-shot wedge probe. Returns True iff we just emitted a
    ``subagent_wedged`` event for this (task_id, subtask_id).

    Idempotent: subsequent calls for the same subtask return False until
    ``reset_wedge_tracking`` clears the entry (e.g. on subtask done /
    new activity row).
    """
    key = f"{task_id}::{subtask_id}"
    if key in _WEDGE_EMITTED:
        return False
    try:
        activity = Path(tasks_dir) / task_id / ".activity.jsonl"
        if not activity.exists():
            return False
        try:
            mtime = activity.stat().st_mtime
        except OSError:
            return False
        age = time.time() - mtime
        if age < _WEDGE_TTL_S:
            return False
        append_log(task_id, {
            "type": "subagent_wedged",
            "subtask_id": subtask_id,
            "silence_s": int(age),
            "ttl_s": int(_WEDGE_TTL_S),
        }, tasks_dir)
        _WEDGE_EMITTED.add(key)
        return True
    except Exception as exc:
        logger.warning(
            "[%s/%s] check_subagent_wedged raised: %r",
            task_id, subtask_id, exc,
        )
        return False


def reset_wedge_tracking(task_id: str, subtask_id: str) -> None:
    """Clear the wedge sentinel so a re-dispatched subtask gets a fresh
    silence budget."""
    _WEDGE_EMITTED.discard(f"{task_id}::{subtask_id}")


def signal_handler(signum, frame):
    """Handle SIGINT (Ctrl+C) gracefully."""
    global shutdown_requested
    print("\n\n[SHUTDOWN] Received interrupt signal. Saving state and exiting...")
    shutdown_requested = True


def _sigterm_handler(signum, frame):
    """SIGTERM from `okuro service stop` / launchd / cancel API.

    sys.exit triggers atexit handlers (including _reconcile_on_exit) so
    task.yaml gets marked halted before the process dies. Without this,
    SIGTERM bypasses Python cleanup and the file stays "active".

    Self-disarm on first entry. A SECOND SIGTERM — our own
    ``_stop_own_scope_on_exit`` issuing ``systemctl stop`` on our cgroup,
    or a service-stop sending repeated TERMs — must NOT re-enter while the
    atexit chain is mid-write. Re-entry previously raised ``SystemExit``
    out of ``_reconcile_on_exit``'s ``yaml.safe_load``, aborting the
    truthful-state write and leaving the task wedged in active/planning
    (the continuation-halt bug). Ignoring further TERMs guarantees the
    reconcile runs to completion.
    """
    global shutdown_requested
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    shutdown_requested = True
    sys.exit(143)


signal.signal(signal.SIGTERM, _sigterm_handler)


def _is_process_alive(pid: int) -> bool:
    """Cross-platform check if a process is alive.

    PR C — two-shot probe with a short cooldown. The prior implementation
    used a single ``os.kill(pid, 0)`` which raced with brief kernel-side
    contention (signal queue full, transient PID-table refresh during
    fork): a dying process could read as "dead" for one nanosecond and
    "alive" again the next. The reconciler then flipped task.yaml.status
    to ``halted`` while the engine was still running, and the engine
    itself crashed on the next plan.yaml write. We now probe twice with
    a 200 ms cooldown and require BOTH probes to agree before declaring
    the process dead. False-positive halts disappear because a truly
    dead process is dead forever; a transient blip rebounds on the second
    probe.
    """
    def _probe() -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    first = _probe()
    if first:
        return True
    # First probe says dead — confirm with a second probe after a cooldown.
    # The kernel rarely produces a transient "dead" reading more than once
    # in close succession; if it does, the second probe catches it.
    time.sleep(_PID_PROBE_COOLDOWN_S)
    return _probe()


_PID_PROBE_COOLDOWN_S = 0.2
# PR C — engine lease lifetime. Set comfortably above the work loop's
# tightest non-blocking tick (a few hundred ms). 300s buys ~5 minutes of
# sleep/suspend tolerance: a laptop that sleeps mid-task (e.g. the CEO's
# Mac closing its lid) must not have reconcile_on_startup reset its live
# subtasks the instant it wakes. The trade-off is that a hard-crashed
# engine's lease takes up to 5 minutes to age out — acceptable, since a
# crashed engine is reclaimed on the next startup reconcile anyway.
_ENGINE_LEASE_TTL_S = 300.0


def _engine_lease_path(task_id: str, tasks_dir: Path) -> Path:
    return Path(tasks_dir) / task_id / ".engine.lease"


def write_engine_lease(task_id: str, tasks_dir: Path) -> None:
    """PR C — atomic write of the current engine's lease.

    Contents: ``{pid, started_at, expires_at}`` (JSON). Called once at
    engine entry and refreshed on the heartbeat thread tick. Atomic via
    ``os.replace`` so a reader never observes a half-written file.
    """
    try:
        path = _engine_lease_path(task_id, tasks_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        body = {
            "pid": os.getpid(),
            "started_at": now,
            "expires_at": now + _ENGINE_LEASE_TTL_S,
        }
        tmp = path.with_suffix(".lease.tmp")
        tmp.write_text(json.dumps(body))
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("[%s] write_engine_lease failed: %r", task_id, exc)


def read_engine_lease(task_id: str, tasks_dir: Path) -> Optional[dict]:
    """Return the parsed lease dict or None when absent / malformed."""
    try:
        path = _engine_lease_path(task_id, tasks_dir)
        if not path.exists():
            return None
        body = json.loads(path.read_text() or "{}")
        if not isinstance(body, dict) or "pid" not in body:
            return None
        return body
    except Exception:
        return None


def is_engine_lease_alive(task_id: str, tasks_dir: Path) -> bool:
    """PR C — true iff a fresh lease names a still-alive PID.

    Trusts the lease's expires_at over a one-shot PID probe: a fresh
    lease (within TTL) AND a live PID together prove the engine owns
    the task. An expired lease OR a dead PID releases ownership and
    a new engine may take over.
    """
    body = read_engine_lease(task_id, tasks_dir)
    if not body:
        return False
    try:
        if float(body.get("expires_at", 0)) < time.time():
            return False
        pid = int(body["pid"])
    except (KeyError, TypeError, ValueError):
        return False
    if pid == os.getpid():
        return True  # the current process IS the lease-holder
    return _is_process_alive(pid)


def clear_engine_lease(task_id: str, tasks_dir: Path) -> None:
    """Remove the lease (on clean exit / explicit takeover)."""
    try:
        path = _engine_lease_path(task_id, tasks_dir)
        if path.exists():
            path.unlink()
    except Exception as exc:
        logger.warning("[%s] clear_engine_lease failed: %r", task_id, exc)


def park_engine_exit(task_id: str, tasks_dir: Path, *, reason: str) -> None:
    """Park-and-exit: a human-blocking gate has been reached — release the
    engine process cleanly instead of polling indefinitely.

    The lifecycle fix for the stale-engine / auto-restart churn: a task
    parked on a HUMAN (decision gate, blocked_review, waiting_user,
    subtask approval, discussion_proceed) must NOT keep a live process
    polling for hours/days. Such long-lived processes die on reboots,
    orchestrator-api restarts, and source-change self-restart; the dead
    process leaves a stale lease that the reconciler then respawns →
    the user sees "engine not responding — auto-restarted" on resume.

    Contract — three coordinated effects so liveness reads the park as
    NORMAL (engine_state="idle"), the launcher does NOT respawn, and the
    next /resume picks up cleanly:
      1. ``mark_engine_exited`` writes the ``.exited`` sentinel → the
         launcher's respawn loop terminates (present = intentional exit)
         AND ``_is_orchestrator_running`` reports the engine as gone.
      2. ``stop_orchestrator_heartbeat`` halts the lease-refresh thread so
         no fresh lease lingers after the process returns.
      3. ``clear_engine_lease`` removes ``.engine.lease`` so a follow-up
         spawn doesn't wait out the TTL and the snapshot's
         ``_derive_engine_state`` sees no stale lease.

    Caller MUST have already persisted the awaiting/parked state (via
    ``set_awaiting`` / a phase ``blocked_review`` flip) BEFORE calling this
    so the resume endpoint has a resolve surface. After this returns the
    caller should ``return`` / break out of the work loop so the engine
    process ends; the atexit handlers (``_reconcile_on_exit`` etc.) are
    idempotent with the writes above.
    """
    try:
        from okuro.orchestrator.state import mark_engine_exited
        mark_engine_exited(task_id, tasks_dir, reason=reason)
    except Exception as exc:
        logger.warning("[%s] park_engine_exit mark_exited failed: %r", task_id, exc)
    try:
        stop_orchestrator_heartbeat()
    except Exception:
        pass
    try:
        clear_engine_lease(task_id, tasks_dir)
    except Exception:
        pass
    try:
        append_log(task_id, {
            "type": "engine_parked",
            "reason": reason,
        }, tasks_dir)
    except Exception:
        pass


# Awaiting kinds that block on a HUMAN — reaching one of these means the
# engine should EXIT (park=engineless), not poll. Kept in sync with
# deliberation._HALT_AWAITING_KINDS minus nothing: every current halt-awaiting
# kind is a human gate. A future kind that is an internal/transient wait must
# be excluded here explicitly.
_HUMAN_PARK_AWAITING_KINDS = frozenset({
    "blocked_review", "panel_confirmation", "capability_gap",
    "discussion_proceed", "decision_gate", "timeout_cap", "subtask_approval",
})


def _is_human_park(task: Task) -> bool:
    """True when the task is parked on a human-blocking awaiting gate.

    The single predicate both work loops consult before deciding to
    park-and-exit. status=="waiting_user" alone is sufficient (set_awaiting
    always pairs it with an awaiting record), but we also accept an awaiting
    record whose kind is human-blocking in case the status projection lags.
    """
    awaiting = getattr(task, "awaiting", None)
    if awaiting is not None and getattr(awaiting, "kind", "") in _HUMAN_PARK_AWAITING_KINDS:
        return True
    return getattr(task, "status", "") == "waiting_user"


def _park_on_human_wait(task: Task, config: Config) -> None:
    """Emit the park log + release the engine for a human-blocking wait.

    Assumes the caller has already persisted the awaiting state. Derives the
    reason from the awaiting kind for telemetry, then calls park_engine_exit.
    Caller must ``raise SystemExit(0)`` (or break + return) immediately after.
    """
    awaiting = getattr(task, "awaiting", None)
    kind = getattr(awaiting, "kind", "") or "waiting_user"
    print(f"[PARKED] {kind} — engine exiting; resolve via API to resume")
    park_engine_exit(task.id, config.tasks_dir, reason=f"park:{kind}")


def _terminate_recurring_run_failed(task: Task, config: Config) -> None:
    """Fix A — a recurring run must never wedge on a human gate.

    Reaching the "no ready subtasks, nothing running, not complete" state
    means a subtask failed/blocked with its retries exhausted. Parking a
    recurring run on blocked_review/waiting_user wedges it forever: it runs
    unattended on a schedule, so there is no human watching to resolve the
    gate, and every engine restart's reconciler re-parks the same stale run
    (observed 11 days after the original failure). The schedule self-heals —
    the next scheduled run supersedes this one — so mark the run terminally
    failed, record the outcome on the def (best-effort, so an adaptive def's
    next tick learns from it), then park engineless so the launcher does not
    respawn. Oneshot tasks keep the existing blocked_review/waiting_user
    surface; only ``task_type == "recurring"`` routes here.
    """
    err = ""
    for phase in task.phases:
        for st in phase.subtasks:
            if st.status in ("failed", "blocked_review") and getattr(st, "error", ""):
                err = st.error
                break
        if err:
            break

    task.status = "failed"
    try:
        update_task_status(task, config.tasks_dir)
    except Exception as exc:
        logger.warning("[%s] recurring terminal-fail status write failed: %r", task.id, exc)

    # Best-effort: record the failure on the recurring def so its
    # outcome_history / adaptive template sees it. The def may be gone
    # (e.g. media jobs migrated to the deterministic pipeline) — skip
    # silently in that case; the terminal-fail + park still stands.
    try:
        from okuro.orchestrator.recurring import load_recurring_defs, record_outcome
        rdir = getattr(config, "recurring_dir", None)
        def_id = getattr(task, "recurring_def_id", "")
        if rdir and def_id:
            for d in load_recurring_defs(rdir):
                if d.id == def_id:
                    record_outcome(
                        d, run_id=task.id, outcome="failed",
                        summary=(err or "run stuck — no subtasks ready")[:500],
                    )
                    break
    except Exception as exc:
        logger.warning("[%s] recurring record_outcome failed: %r", task.id, exc)

    print("\n[RECURRING-FAILED] No subtasks ready — terminal fail; "
          "next scheduled run supersedes.")
    try:
        append_log(task.id, {
            "type": "orchestrator_state",
            # ROCK-SOLID v5 P1.1/1.3 — this is designed supersession, not a
            # failure: the next scheduled run handles it, no action needed.
            # Was "error" + a "Task Failed" push (mismatch #13, evidence
            # inventory 2026-07-31) for something that isn't a failure.
            "state": "idle",
            "label": "Recurring run superseded — next schedule will pick it up",
        }, config.tasks_dir)
    except Exception:
        pass
    # No push notification — nothing for the user to act on; the next
    # scheduled run supersedes this one automatically.
    park_engine_exit(task.id, config.tasks_dir, reason="park:recurring_failed")


def _reap_ghost_subagent_sessions(task_id: str, tasks_dir: Path,
                                   prior_pid: int) -> int:
    """PR C — reap any subagent kill-flags whose parent engine PID is the
    prior (now-dead) engine.

    On warm-start the new engine's plan.yaml writes may collide with a
    subagent subprocess that's still streaming output into .activity.jsonl.
    The dispatcher polls ``.kill-requests/<subtask_id>`` for SIGTERM
    requests; dropping a flag is enough to make the dispatcher tear down
    its session. Returns the number of subtasks reaped.
    """
    try:
        plan_path = Path(tasks_dir) / task_id / "plan.yaml"
        if not plan_path.exists():
            return 0
        try:
            plan = yload(plan_path.read_text()) or {}
        except Exception:
            return 0
        kill_dir = Path(tasks_dir) / task_id / ".kill-requests"
        kill_dir.mkdir(parents=True, exist_ok=True)
        reaped = 0
        for phase in plan.get("phases", []) or []:
            for st in phase.get("subtasks", []) or []:
                if st.get("status") == "running":
                    flag = kill_dir / str(st.get("id", "?"))
                    try:
                        flag.write_text(
                            f"engine_warm_start prior_pid={prior_pid}"
                        )
                        reaped += 1
                    except OSError:
                        pass
        return reaped
    except Exception as exc:
        logger.warning(
            "[%s] _reap_ghost_subagent_sessions raised: %r", task_id, exc,
        )
        return 0


def _enforce_engine_lease(task: Task, tasks_dir: Path) -> None:
    """PR C — implement the 3-case lease takeover protocol.

    Raises SystemExit(0) when another live engine already owns the task —
    safer than racing on plan.yaml writes. Emits ``engine_resumed`` when
    this is a warm start (prior lease present but dead).
    """
    prior = read_engine_lease(task.id, tasks_dir)
    if prior:
        prior_pid = int(prior.get("pid", 0) or 0)
        # Don't fight ourselves on retry-resume.
        if prior_pid and prior_pid != os.getpid() and is_engine_lease_alive(
            task.id, tasks_dir
        ):
            logger.warning(
                "[%s] engine lease held by PID %s — refusing duplicate spawn",
                task.id, prior_pid,
            )
            raise SystemExit(0)

        # Prior lease present but expired/dead → warm start.
        try:
            reaped = _reap_ghost_subagent_sessions(
                task.id, tasks_dir, prior_pid,
            )
        except Exception:
            reaped = 0
        try:
            append_log(task.id, {
                "type": "engine_resumed",
                "prior_pid": prior_pid,
                "new_pid": os.getpid(),
                "ghost_subtasks_reaped": reaped,
            }, tasks_dir)
        except Exception as exc:
            logger.warning("[%s] engine_resumed emit failed: %r", task.id, exc)

    # Cold or warm — write our own lease now.
    write_engine_lease(task.id, tasks_dir)


def _is_orchestrator_running(task_id: str) -> bool:
    """Check if an orchestrator process is running for this task."""
    try:
        config = load_config()
        pid_path = config.tasks_dir / task_id / ".orchestrator.pid"
    except Exception:
        pid_path = Path("tasks") / task_id / ".orchestrator.pid"

    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            if pid == os.getpid():
                return False
            return _is_process_alive(pid)
        except (ValueError, OSError):
            pass

    # Fallback: scan processes
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "cmdline"]):
            if proc.pid == os.getpid():
                continue
            try:
                cmdline = proc.info.get("cmdline") or []
                if any("okuro" in str(c) and "orchestrat" in str(c) for c in cmdline) and task_id in str(cmdline):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        pass
    return False


def _make_parser() -> argparse.ArgumentParser:
    """Build the engine CLI argparse. Extracted from main() so tests can
    pin the front-door contract (esp. --intelligence choices) without
    spawning a task. The MCP layer's _ORCH_VALID_INTELLIGENCE must be a
    subset of the choices declared here, otherwise default values from
    the MCP tool crash the engine on spawn."""
    parser = argparse.ArgumentParser(
        description="Okuro Orchestrator",
        epilog="Examples:\n"
               '  %(prog)s "rebuild the website"\n'
               "  %(prog)s --resume task-20260210-140000\n"
               '  %(prog)s --dry-run "test task"\n'
               "  %(prog)s --list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("task", nargs="?", help="Task description string")
    parser.add_argument("--resume", metavar="PATH", help="Resume from existing task directory")
    parser.add_argument("--dry-run", action="store_true", help="Decompose and show plan but do not execute")
    parser.add_argument("--list", action="store_true", help="List existing tasks")
    parser.add_argument("--continue", metavar="TASK_ID", dest="continue_task",
                        help="Continue existing task with new instructions")
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="Path to config.yaml (default: auto-detect)")
    parser.add_argument("--yes", action="store_true", help="Skip plan confirmation prompt")
    parser.add_argument("--brief", metavar="PATH", help="Path to briefing file (markdown)")
    parser.add_argument("--task-id", metavar="ID", help="Specify ID for a new task")
    parser.add_argument("--preferred-cli", metavar="CLI",
                        help="Override provider for this task")
    parser.add_argument("--intelligence", metavar="LEVEL",
                        choices=["", "low", "balanced", "high", "max"],
                        default="", help='Intelligence level: low/balanced/high are aliases for "" (default tiers); "max" forces strategic planner')
    parser.add_argument("--review-trigger", metavar="MODE",
                        choices=["", "per_artifact", "closeout"], default="",
                        help='Review trigger for this task: "per_artifact" '
                             '(one review per artifact_write) or "closeout" '
                             '(one review per complete submission). Empty '
                             'inherits the global default.')
    parser.add_argument("--mode", choices=["auto-execute", "deliberate"],
                        default="auto-execute", help="Task mode")
    parser.add_argument("--auto-execute", action="store_true", dest="auto_execute",
                        help="Explicit opt-out of M1 decision gates. Without "
                             "this flag, --intelligence max enables gates by "
                             "default so the orchestrator pauses for a user "
                             "decision before each gate-bearing phase.")
    parser.add_argument("--enable-gates", action="store_true", dest="enable_gates",
                        help="Force-enable decision gates regardless of "
                             "intelligence tier. Useful for testing M1.")
    parser.add_argument("--required-roles", metavar="ROLES",
                        help="Comma-separated role IDs the decomposer must include")
    # Run a HAND-DRAWN workflow instead of decomposing with an LLM. The engine
    # has been able to EXECUTE one since the workflow feature landed
    # (workflow_run.phases_for_task), but nothing could START one on demand —
    # `recurring.py` was the only caller that ever set workflow_id, so an
    # ad-hoc run had no entry point at all. These two flags are that entry point.
    parser.add_argument("--workflow", metavar="ID", default=None,
                        help="Run the drawn workflow with this id (see /workflows). "
                             "The plan is COMPILED from the graph — no LLM decomposition.")
    parser.add_argument("--workflow-param", metavar="K=V", action="append", default=None,
                        dest="workflow_param",
                        help="Fill a {placeholder} in the drawn nodes' prompts. "
                             "Repeatable, e.g. --workflow-param artifact=<id> "
                             "--workflow-param who='the board'")
    parser.add_argument("--source-todo-id", metavar="UUID", default=None,
                        help="If this task was spawned from a registered todo, the todo id "
                             "to mark done on task completion")
    parser.add_argument("--source-thought-id", metavar="UUID", default=None,
                        help="If this task was spawned from an action-item embedded in a "
                             "thought, the thought id to mark resolved on task completion")
    parser.add_argument("--auto-approve-risk", metavar="LEVEL",
                        choices=["none", "medium", "high"], default="none",
                        help="Skip per-subtask approval prompts up to this risk level "
                             "(default 'none' preserves the wait_for_approval flow)")
    # ROCK-SOLID v5 P5.4 — the Stream C writer, at the layer that actually
    # builds the Task. The API shells out to this parser, so a flag here is
    # what makes the field reachable from the web UI and from MCP; adding it
    # to `create_task` alone would have left the whole chain inert.
    parser.add_argument("--fast-track", action="store_true",
                        help="Fast-track profile. A phase whose subtasks are "
                             "ALL risk=LOW and whose deterministic checks pass "
                             "clean skips the critic+scorer LLM stages. "
                             "Deterministic checks are NEVER skipped; MED and "
                             "HIGH risk are always LLM-reviewed; every skip is "
                             "logged in the activity feed.")
    parser.add_argument("--audience", metavar="PERSON_ID", default="",
                        help="Stream C recipient. When set, each Stream B "
                             "artifact a subtask produces is delivered to this "
                             "person via delivery.send. Unset = no delivery, "
                             "which stays the default.")
    parser.add_argument("--delivery-channel", metavar="CHANNEL", default="",
                        help="How the delivery is rendered: markdown | marp | "
                             "microsite | tts | podcast. Defaults to markdown "
                             "when --audience is set. Ignored without it.")
    parser.add_argument("--delivery-brand", metavar="BRAND_ID", default="",
                        help="Brand override for the delivery theme. "
                             "Ignored without --audience.")
    parser.add_argument("--verify-command", metavar="CMD", default="",
                        help="Build/test gate. When set, runs in project_path "
                             "after all subtasks finalize, before flipping task "
                             "status to 'done'. Non-zero exit fails the task. "
                             "Example: 'pytest -q' or 'npm test --silent'")
    parser.add_argument("--deliberate-continuations", metavar="BOOL",
                        choices=["true", "false"], default=None,
                        help="Task-level default for the deliberation-on-"
                             "continuation toggle. 'true' runs a fresh council "
                             "for later continuations; 'false' forces single-shot. "
                             "Omit to inherit the global config default.")
    parser.add_argument("--autopilot", metavar="BOOL",
                        choices=["true", "false"], default=None,
                        help="Task-level autopilot override. 'true' auto-answers "
                             "every human gate from the profile (high-risk gates "
                             "included); 'false' forces manual gates. Omit to "
                             "inherit the global orchestrator.autopilot default.")
    return parser


# ---------------------------------------------------------------------------
# Theme E — source-change self-restart.
#
# Pre-fix: the engine subprocess is spawned by the API once and holds onto
# its loaded modules for the lifetime of the task. Editing
# dispatcher_streaming.py / engine.py / review_queue.py and restarting the
# API surface had no effect on the running engine — only SIGKILL + respawn
# picked up the new code. The launcher (api/main.py spawn_orchestrator)
# now wraps the engine in a respawn-on-clean-exit loop and we expose a
# daemon thread here that detects mtime changes on the engine's source
# footprint, writes an `engine_restart_requested` log event, and cleanly
# exits the process. The launcher's outer loop then re-execs the engine
# against the fresh modules.
#
# Acceptance gate: the `.exited` sentinel under the task directory must be
# absent when the engine exits via source-change. The launcher checks this
# sentinel BEFORE respawning — present (set by mark_engine_exited on done/
# failed/cancelled / panel return / capability gap) → terminate; absent
# → respawn.
# ---------------------------------------------------------------------------


def _default_source_watcher_paths() -> list[Path]:
    """Files whose mtime change should trigger a self-restart.

    Per the brief: src/okuro/orchestrator/*.py + src/okuro/bridge/streaming/*.py
    + src/okuro/sense/review_queue.py. Anything else in the package would
    cause spurious restarts (memory_index, role catalog files, etc.).
    """
    pkg_root = Path(__file__).resolve().parent
    okuro_root = pkg_root.parent  # .../src/okuro
    paths: list[Path] = []
    # P4.5 — RECURSIVE over orchestrator/. The previous non-recursive glob
    # covered orchestrator/*.py only, which excluded the two subpackages the
    # engine spends most of its time in: reviewer/ (the critic+scorer
    # pipeline) and api/ (state_reader, the snapshot the UI reads). A fix to
    # the reviewer could NEVER reach a live engine — it would run the old
    # pipeline until the task ended, and the source watcher would report
    # nothing to restart for.
    for p in pkg_root.rglob("*.py"):
        if p.is_file() and "__pycache__" not in p.parts:
            paths.append(p)
    streaming_dir = okuro_root / "bridge" / "streaming"
    if streaming_dir.is_dir():
        for p in streaming_dir.glob("*.py"):
            if p.is_file():
                paths.append(p)
    # Named files outside the two trees above. artifacts.py is the Stream-B
    # write path (audience inference, the dispatch-epoch guard); review_queue
    # is the verdict transport. Both are engine behaviour living in sense/.
    for rel in ("sense/review_queue.py", "sense/artifacts.py"):
        f = okuro_root / rel
        if f.is_file():
            paths.append(f)
    return paths


def _snapshot_mtimes(paths: list[Path]) -> dict[str, float]:
    """Capture (str(path), mtime) tuples in a single sweep."""
    out: dict[str, float] = {}
    for p in paths:
        try:
            out[str(p)] = p.stat().st_mtime
        except OSError:
            # File gone between glob and stat — skip; next poll picks it up.
            continue
    return out


def start_source_watcher(
    task: Optional[Task] = None,
    config: Optional[Config] = None,
    *,
    paths: Optional[list[Path]] = None,
    interval_s: float = 30.0,
    on_change: Optional[callable] = None,
    initial_snapshot: Optional[dict[str, float]] = None,
    stop_event: "Optional[threading.Event]" = None,
) -> "threading.Thread":
    """Spawn a daemon thread that watches engine source-file mtimes.

    Test seam: ``initial_snapshot`` lets unit tests inject a known
    starting state; ``on_change`` lets tests substitute a callback that
    doesn't actually ``sys.exit``. The default ``on_change`` writes the
    ``engine_restart_requested`` log event and calls ``sys.exit(0)`` so
    the launcher's respawn loop picks up the new code.

    The thread is daemon=True — engine shutdown does not wait for it.

    ``stop_event`` terminates the loop. Without it the thread was
    UNSTOPPABLE: ``while True`` with no exit condition other than a source
    change firing the callback, so a watcher that never observes one runs
    for the life of the process. That is a leak in production and, in a test
    process, an escaped thread that outlives the test which created it —
    which is exactly how it corrupted an unrelated test: the leaked thread
    sat in ``time.sleep`` while another test monkeypatched the shared
    ``time`` module, so it began driving that test's virtual clock from a
    second thread. Always pass a stop_event from a test.
    """
    import threading

    watched_paths = paths if paths is not None else _default_source_watcher_paths()
    baseline = (
        initial_snapshot if initial_snapshot is not None
        else _snapshot_mtimes(watched_paths)
    )

    def _default_on_change(changed: list[str]) -> None:
        # Best-effort log write — the launcher's outer respawn loop is
        # the real exit-route guard, the log event is for telemetry.
        if task is not None and config is not None:
            try:
                append_log(task.id, {
                    "type": "engine_restart_requested",
                    "reason": "source_change",
                    "changed_files": changed[:20],
                }, config.tasks_dir)
            except Exception as exc:
                logger.warning("source_watcher: log emit failed: %s", exc)
        logger.warning(
            "Source change detected (%d file(s)) — exiting engine for "
            "respawn against fresh modules. Changed: %s",
            len(changed), ", ".join(changed[:5]),
        )
        # Do NOT mark_engine_exited — the absence of `.exited` is the
        # launcher's signal that a respawn is intended.
        #
        # P4.5 — request, do not exit. sys.exit() here would raise SystemExit
        # in this daemon thread and end nothing else; the engine would keep
        # running the modules it already imported, having just logged that it
        # was restarting.
        global restart_requested
        restart_requested = True

    callback = on_change or _default_on_change

    def _loop() -> None:
        snapshot = dict(baseline)
        while True:
            try:
                if stop_event is not None:
                    # Interruptible wait — returns as soon as the event is set
                    # instead of sleeping out the full interval first.
                    if stop_event.wait(interval_s):
                        return
                else:
                    time.sleep(interval_s)
                current = _snapshot_mtimes(watched_paths)
                changed: list[str] = []
                for path_str, mtime in current.items():
                    prior = snapshot.get(path_str)
                    if prior is None or mtime > prior + 0.001:
                        changed.append(path_str)
                if changed:
                    callback(changed)
                    return  # callback usually sys.exit's; if not, stop watching
                snapshot = current
            except Exception as exc:  # never crash the watcher
                logger.warning("source_watcher loop error: %s", exc)

    t = threading.Thread(
        target=_loop, name="okuro-engine-source-watcher", daemon=True,
    )
    t.start()
    return t


def main():
    """Parse arguments and route to appropriate action."""
    parser = _make_parser()
    args = parser.parse_args()
    signal.signal(signal.SIGINT, signal_handler)

    try:
        config_path = Path(args.config) if args.config else None
        config = load_config(config_path)
    except Exception as e:
        print(f"[ERROR] Failed to load config: {e}")
        sys.exit(1)

    binary_warnings = validate_cli_binaries(config)
    if binary_warnings:
        for warning in binary_warnings:
            print(f"[WARNING] {warning}")
        print("[WARNING] Some CLI tools are missing. Subtask dispatch may fail.\n")

    if args.list:
        list_tasks(config)
        return

    if args.resume:
        task_path = Path(args.resume)
        if not task_path.is_absolute():
            task_path = config.tasks_dir / task_path
        task_id = task_path.name

        try:
            task = load_task(task_id, config.tasks_dir)
            _set_active_task(task.id, config.tasks_dir)
            config = apply_cli_preference(config, args.preferred_cli or task.preferred_cli)
            print(f"[RESUME] Loading task: {task.id}")
            print(f"Description: {task.description}")
            print(f"Status: {task.status}")

            reset_count = 0
            for phase in task.phases:
                for st in phase.subtasks:
                    if st.status in ("running", "waiting_approval"):
                        st.status = "pending"
                        st.error = ""
                        reset_count += 1
            # DAG graph nodes (deliberate mode) — when the prior engine crashed
            # mid-dispatch, position/execution nodes can be stuck in 'running'.
            # The work loop treats 'running' as in-flight and never re-dispatches,
            # so d1.1-style orphans never make progress. Reset them alongside
            # subtasks so resume picks up where the crash left off.
            graph_reset = 0
            if task.graph:
                for n in task.graph.nodes:
                    if n.type in ("position", "execution") and n.status == "running":
                        n.status = "pending"
                        n.started_at = ""
                        n.error = ""
                        graph_reset += 1
            if reset_count or graph_reset:
                print(
                    f"[RESUME] Reset {reset_count} orphaned subtask(s) + "
                    f"{graph_reset} graph node(s) to pending"
                )
                save_plan(task, config.tasks_dir)

            warnings = validate_task(task)
            if warnings:
                print(f"[WARN] State validation found {len(warnings)} issue(s):")
                for w in warnings:
                    print(f"  - {w}")
            print()
        except Exception as e:
            print(f"[ERROR] Failed to load task: {e}")
            sys.exit(1)

        # Theme E — daemon source-watcher.
        start_source_watcher(task, config)
        orchestrator_loop(task, config, args)

    elif args.continue_task:
        task_id = args.continue_task
        if not args.task:
            print("[ERROR] Must provide continuation instructions as task argument")
            sys.exit(1)

        try:
            task = load_task(task_id, config.tasks_dir)
            _set_active_task(task.id, config.tasks_dir)
        except Exception as e:
            print(f"[ERROR] Failed to load task: {e}")
            sys.exit(1)

        if args.preferred_cli:
            task.preferred_cli = args.preferred_cli
        config = apply_cli_preference(config, task.preferred_cli)

        if task.status in ("active", "planning"):
            if _is_orchestrator_running(task_id):
                print(f"[ERROR] Task {task_id} is currently {task.status} with a running orchestrator.")
                sys.exit(1)
            else:
                print(f"[WARN] Task {task_id} was '{task.status}' but no orchestrator running — overriding stale status")

        print(f"[CONTINUE] Task: {task.id}")
        print(f"[CONTINUE] Original: {task.description[:100]}")
        print(f"[CONTINUE] New instructions: {args.task}\n")

        # Persist BEFORE decomposition runs — if the planner crashes, the
        # user can still see what they asked for in the flow chart. Anchor
        # the intervention to the next phase id that decomposition will
        # produce.
        from okuro.orchestrator.state import record_intervention
        try:
            record_intervention(task.id, args.task, config.tasks_dir, kind="continuation")
        except Exception as exc:
            print(f"[WARN] Failed to record continuation intervention: {exc}")

        task.status = "planning"
        update_task_status(task, config.tasks_dir)

        # Bound the continuation-decompose context: run the M2 compressor
        # so a fresh decision-trace artifact exists for the task BEFORE
        # decompose_continuation assembles its prior-work block. The
        # decomposer inlines that trace + top-K relevance-ranked handovers
        # instead of dumping every subtask. Same compressor entry the
        # serial-dispatch brief path uses (_maybe_compress); best-effort,
        # never blocks continuation.
        try:
            from okuro.sense.task_events import list_events, latest_compression
            from okuro.orchestrator.compressor import run_compression

            _prior = latest_compression(task_id=task.id)
            _since = int((_prior.get("body") or {}).get("covers_seq_to") or 0) if _prior else 0
            if list_events(task_id=task.id, since_seq=_since, limit=1):
                _cres = run_compression(
                    task_id=task.id,
                    task_description=task.description,
                    project_path=getattr(task, "project_path", "") or "",
                    adrs=list(getattr(task, "adrs", []) or []),
                )
                if _cres.get("ok"):
                    print(
                        f"[COMPRESS] Decision-trace refreshed "
                        f"({_cres.get('output_chars')} chars) before continuation decompose"
                    )
        except Exception as exc:
            print(f"[WARN] Pre-decompose compression skipped: {exc}")

        # Record-and-defer — root-cause fix for the continue-vs-resume double
        # decompose. The intervention is already recorded above; DO NOT
        # decompose / append / flip mode inline here. Hand off to
        # orchestrator_loop, which drains pending continuations UNDER the engine
        # lease (_drain_pending_continuations, before the completion check).
        #
        # Why the old inline path was wrong: it decomposed + appended + flipped
        # mode (deliberate→auto-execute) OUTSIDE the lease, so a concurrent
        # --resume/drain engine decomposed the SAME intervention. Result:
        # conflicting phases with broken cross-phase deps, and the mid-flight
        # mode flip made the losing engine exit and stamp a spurious `halted`.
        # append_phases already extends the DAG, so a deliberate task runs the
        # appended phases via the deliberation-loop drain guard — no mode flip.
        task.status = "active"
        update_task_status(task, config.tasks_dir)

        if args.dry_run:
            # Preview only — decompose WITHOUT persisting so the user sees the
            # plan; the authoritative decompose happens in the loop's drain.
            try:
                for _ph in decompose_continuation(args.task, task, config):
                    task.phases.append(_ph)
            except Exception as e:
                print(f"[DRY-RUN] decompose preview failed: {e}")
            print_plan(task)
            print("\n[DRY-RUN] Continuation plan preview. Not executing.")
            return

        # Theme E — daemon source-watcher.
        start_source_watcher(task, config)
        orchestrator_loop(task, config, args)

    elif args.task:
        task_description = args.task
        if args.brief:
            try:
                brief_path = Path(args.brief)
                if brief_path.is_file():
                    briefing = brief_path.read_text().strip()
                    task_description = f"{args.task}\n\n---\n{briefing}"
                    print(f"[BRIEFING] Loaded from {args.brief}")
                else:
                    print(f"[WARN] Briefing file not found: {args.brief}")
            except Exception as e:
                print(f"[WARN] Failed to read briefing: {e}")

        print(f"[NEW TASK] {task_description}\n")

        required_roles = [r.strip() for r in args.required_roles.split(",")] if args.required_roles else None
        # M1 — intelligence=max routes through gates by default; --auto-execute
        # is the explicit opt-out, --enable-gates forces gates on for testing.
        gates_enabled = bool(
            getattr(args, "enable_gates", False)
            or (args.intelligence == "max" and not getattr(args, "auto_execute", False))
        )
        task = create_task(
            task_description, config.tasks_dir,
            task_id=args.task_id,
            preferred_cli=args.preferred_cli,
            intelligence=args.intelligence,
            review_trigger=args.review_trigger,
            required_roles=required_roles,
            mode=args.mode,
            source_todo_id=args.source_todo_id,
            source_thought_id=args.source_thought_id,
            auto_approve_risk=args.auto_approve_risk,
            verify_command=args.verify_command,
            gates_enabled=gates_enabled,
            fast_track=bool(getattr(args, "fast_track", False)),
            audience=getattr(args, "audience", ""),
            delivery_channel=getattr(args, "delivery_channel", ""),
            delivery_brand_id=getattr(args, "delivery_brand", ""),
            deliberate_continuations=(
                None if getattr(args, "deliberate_continuations", None) is None
                else args.deliberate_continuations == "true"
            ),
            autopilot=(
                None if getattr(args, "autopilot", None) is None
                else args.autopilot == "true"
            ),
            workflow_id=getattr(args, "workflow", None),
            workflow_params=_parse_workflow_params(getattr(args, "workflow_param", None)),
        )
        _set_active_task(task.id, config.tasks_dir)
        config = apply_cli_preference(config, task.preferred_cli)
        print(f"[CREATED] Task ID: {task.id}\n")

        if args.dry_run:
            from okuro.orchestrator.workflow_run import phases_for_task

            phases = phases_for_task(task)
            if phases is None:
                print("[DRY-RUN] Decomposing task...")
                phases = decompose_task(task_description, config, required_roles=task.required_roles or None, intelligence=task.intelligence, task_id=task.id)
            else:
                print("[DRY-RUN] Compiling drawn workflow...")
            task.phases = phases
            task.status = "planning"
            save_plan(task, config.tasks_dir)
            print_plan(task)
            print(f"\n[DRY-RUN] Plan created. Not executing.")
            print(f"To execute: okuro orchestrator --resume {task.id}")
            return

        # Theme E — daemon source-watcher.
        start_source_watcher(task, config)
        orchestrator_loop(task, config, args)
    else:
        parser.print_help()
        sys.exit(1)


def _expected_artifact_path(task_id: str, subtask: Subtask, tasks_dir: Path) -> Path:
    """Resolve the legacy disk path for non-document subagent outputs.

    Stream B (subagent reports) moved to ``artifact_write(kind='report',
    task_id=…, subtask_id=…)`` in migration 035; the document is no
    longer expected on disk. This helper still resolves the historical
    file path so ``_finalize_subtask`` can fall back to a disk artifact
    when one happens to exist (e.g. older recurring tasks finishing
    mid-cutover, or a subagent that wrote screenshots / binaries via
    ``state.save_artifact`` without calling ``artifact_write``).

    Returns ``{tasks_dir}/{task_id}/artifacts/{subtask_id}-{artifact_name}.md``
    when ``artifact_name`` is set, otherwise ``{subtask_id}.md``.
    """
    artifacts_dir = tasks_dir / task_id / "artifacts"
    if subtask.artifact_name:
        return artifacts_dir / f"{subtask.id}-{subtask.artifact_name}.md"
    return artifacts_dir / f"{subtask.id}.md"


def _has_brain_artifact(task_id: str, subtask_id: str) -> bool:
    """Return True if the subagent produced at least one Stream B row.

    Replaces the disk-only ghost-completion check. Brain artifacts are
    keyed by (task_id, subtask_id) since migration 035; ``artifact_list``
    with both filters returns all reports the subagent emitted.
    """
    try:
        from okuro.sense.artifacts import artifact_list

        rows = artifact_list(
            task_id=task_id,
            subtask_id=subtask_id,
            limit=1,
            order="created_at_desc",
        )
        return bool(rows)
    except Exception as exc:
        logger.warning(
            "[%s] brain-artifact verification raised %r — treating as missing",
            subtask_id, exc,
        )
        return False


def _read_handover_outcome(
    task_id: str, subtask_id: str
) -> tuple[bool, str | None, dict | None]:
    """Return (handover_present, outcome, brief).

    outcome ∈ {"success","partial","failed"} or None when no row exists.
    brief is the full dict for downstream error messages / open_questions.
    Cross-process-safe equivalent of session_state["handover_written"] —
    the subagent's flag lives in its own process; the row is durable.
    """
    try:
        from okuro.sense.role_handover import read_role_handover
        ho = read_role_handover(subtask_id=subtask_id, task_id=task_id)
        if not ho:
            return (False, None, None)
        brief = ho.get("brief") or {}
        outcome = (brief.get("outcome") or "").strip().lower() or None
        return (True, outcome, brief)
    except Exception as exc:
        logger.warning(
            "[%s] role-handover verification raised %r — treating as missing",
            subtask_id, exc,
        )
        return (False, None, None)


def _maybe_run_m3_review_gate(task: Task, phase, config: Config) -> list[str]:
    """Run the M3 reviewer pipeline as a post-phase gate.

    Returns the list of subtask ids that were reset to pending because
    the verdict was FAIL on load-bearing findings. Empty list means the
    phase passed (or had a planned reviewer subtask — backward-compat
    bypass — or the bridge was unavailable). Caller advances the phase
    only when the returned list is empty.

    Best-effort: any exception is swallowed and an empty list returned so
    the engine never blocks on a reviewer crash. Tests can call
    ``okuro.orchestrator.reviewer.pipeline.run_review`` directly with
    ``strict=True`` to surface failures.
    """
    from okuro.orchestrator.reviewer.pipeline import (
        run_review, has_planned_reviewer, rerun_subtasks_from_verdict,
    )
    if has_planned_reviewer(phase):
        logger.info(
            "M3 review gate skipped for phase %s — planned reviewer subtask present (backward-compat)",
            getattr(phase, "id", "?"),
        )
        return []
    # P0-5 — skip M3 on divergent-by-design phases. Multiperspective
    # research is supposed to surface disagreements across siblings (4
    # role positions on the same 7 decision axes will not agree by
    # construction). Gating such a phase with a reviewer that scores
    # cross-deliverable consistency forces 3 retries + retries-cap
    # advance — wasted LLM spend for zero forward progress. The
    # *next* phase (solution-architect synthesis) is where convergence
    # is the contract; review fires there.
    #
    # Detection heuristic: every subtask in the phase has empty
    # `dependencies` AND there are 2+ subtasks. A phase with even one
    # internal dep is sequential — convergence is part of its contract
    # — fire as usual.
    subtasks = list(getattr(phase, "subtasks", []) or [])
    if len(subtasks) >= 2 and all(
        not (getattr(st, "dependencies", []) or []) for st in subtasks
    ):
        logger.info(
            "M3 review gate skipped for phase %s — divergent-by-design "
            "(parallel siblings with no inter-subtask deps); convergence "
            "is the next phase's contract.",
            getattr(phase, "id", "?"),
        )
        print(
            f"\n[M3 REVIEW] phase {phase.id} ({phase.name}) — "
            f"SKIPPED (parallel/divergent; review fires at synthesis)"
        )
        return []
    print(f"\n[M3 REVIEW] phase {phase.id} ({phase.name}) — running deterministic→critic→scorer")

    # Inline marker — emit one ``review_starting`` row per real subtask of
    # the phase under review into ``.activity.jsonl``. The FE activity feed
    # interleaves reviewer:phase{N}:* rows under each subtask of phase N, so
    # the user sees the reviewer judging their work in the same panel as
    # the subtask's own activity. Tagged with the subtask's own id+role so
    # the feed's per-subtask filter (selectedSubtaskId) picks it up.
    task_dir = config.tasks_dir / task.id
    for st in subtasks:
        _append_activity_row(task_dir, {
            "type": "review_starting",
            "subtask_id": getattr(st, "id", "?"),
            "role": getattr(st, "role", "?"),
            "review_kind": "m3",
            "phase_id": phase.id,
            "message": (
                f"M3 reviewer (critic + scorer) is reviewing phase {phase.id} "
                "— your deliverables are being judged."
            ),
        })

    # WP7 — thread the engine's resolved tasks_dir so the reviewer's emit
    # helpers + deterministic file checks resolve against the SAME tasks
    # directory the engine dispatched under (not the DEFAULT). Under a
    # non-default --config this is what stops the review path from emitting
    # to / checking the wrong tasks_dir.
    # P5 (#6) — arm the phase-gate critic's verification block. On a re-review
    # (after rerun_subtasks_from_verdict reset + re-dispatch), the subtasks carry
    # the prior round's findings on review_findings (P5 reset stamp); pass them as
    # prior_findings so the critic VERIFIES each (resolved/still-open) instead of
    # re-hunting a fresh set — the DAG-path equivalent of the session loop's
    # prev_findings. Empty on a clean first review (no carried findings → fresh).
    # The verify pass self-corrects any stale item (resolved → dropped).
    _phase_prior_findings: list = []
    for _st in getattr(phase, "subtasks", []) or []:
        _phase_prior_findings.extend(getattr(_st, "review_findings", None) or [])
    review_result = run_review(
        task=task, phase=phase, persist_event=True, strict=False,
        tasks_dir=config.tasks_dir,
        prior_findings=_phase_prior_findings or None,
        # Reviewer runs on the task's provider (cli_default, set by
        # apply_cli_preference) so `preferred_cli=codex` reviews on openai too —
        # not hard-pinned to claude. None → run_review's claude default.
        provider=(getattr(config, "cli_default", None) or None),
        # getattr-guarded: production Config carries .orchestrator; partial
        # test/legacy configs degrade to None → run_review keeps legacy fallback.
        review_model=getattr(getattr(config, "orchestrator", None), "review_model", None),
        review_model_conceptual=getattr(
            getattr(config, "orchestrator", None), "review_model_conceptual", None
        ),
    )
    verdict = review_result.get("verdict", "FAIL")
    # Closing marker — emit one ``review_complete`` row per subtask, tagged
    # with the verdict so the per-subtask feed shows the outcome inline.
    # load_bearing_findings count comes from the review_result (already
    # surfaced in the verbose print below).
    _load_bearing = len([
        f for f in (review_result.get("critic_findings") or [])
        if (f or {}).get("severity") == "load_bearing"
    ])
    for st in subtasks:
        _append_activity_row(task_dir, {
            "type": "review_complete",
            "subtask_id": getattr(st, "id", "?"),
            "role": getattr(st, "role", "?"),
            "review_kind": "m3",
            "phase_id": phase.id,
            "verdict": verdict,
            "load_bearing_findings": _load_bearing,
            "message": f"Reviewer verdict: {verdict}.",
        })
    # P3.9 — the phase-gate driver joins the round timeline.
    #
    # `convergence_telemetry` is the ONLY store keyed by (subtask, attempt);
    # everything else this reviewer emits is phase-scoped, and the durable
    # `verdict` event is filed under a synthetic `reviewer-phase-N` id. Until
    # now only the SESSION-LOOP driver (dispatcher_streaming) published these
    # rows, so a phase-gate review left no per-subtask round trace at all and
    # the timeline would have silently covered one driver of two.
    #
    # One row per subtask in the phase, because that is the grain the timeline
    # reads — a phase gate reviews them together, so they share a verdict and
    # an attempt. `stage_timings` rides along from run_review (P0.5), which is
    # what lets a 30 ms deterministic round leave the same visible trace as a
    # 4-minute LLM one.
    try:
        from okuro.sense.task_events import append_event as _append_event

        _timings = review_result.get("stage_timings") or {}
        for st in subtasks:
            _sid = getattr(st, "id", "") or ""
            if not _sid:
                continue
            _body = {
                "subtask_id": _sid,
                # min_length=1 on the model. A phase gate reviews the phase's
                # whole output rather than one artifact, so it is named for
                # what it is instead of being left empty (which the schema
                # rejects — silently, into the best-effort except below).
                "artifact_id": f"phase-{phase.id}",
                "attempt": int(getattr(st, "retries", 0) or 0) + 1,
                "verdict": verdict,
                "findings_count": len(review_result.get("critic_findings") or []),
                "open_findings": _load_bearing,
                "resolved_findings": 0,
                "reason": "phase gate",
                "ts": utc_now_naive().isoformat(),
            }
            if _timings:
                _body["stage_timings"] = dict(_timings)
            _append_event(
                task_id=task.id, subtask_id=_sid,
                event_type="convergence_telemetry", body=_body,
                from_role=getattr(st, "role", "") or "",
                created_by="engine-phase-gate",
            )
    except Exception as _exc:  # pragma: no cover - telemetry is best-effort
        logger.warning("phase-gate convergence_telemetry emit failed: %r", _exc)
    det = review_result.get("deterministic", {})
    print(
        f"[M3 REVIEW] verdict={verdict} "
        f"det_failed={det.get('failed_checks', 0)} "
        f"det_load_bearing={det.get('load_bearing_failures', 0)} "
        f"critic_findings={len(review_result.get('critic_findings') or [])} "
        f"event={review_result.get('verdict_event_id') or 'none'}"
    )
    if verdict not in ("FAIL", "NEEDS_USER"):
        return []
    # Escalate-only for the auto-execute + session-loop path (post-WP3).
    # Session-loop is the authoritative per-artifact reviewer there — it
    # reviewed/patched every artifact in-session before the subtask was marked
    # done. A FAIL from the M3 phase-gate (which runs only on synthesis phases)
    # is then a cross-subtask concern, NOT a per-artifact defect to re-dispatch:
    # the old reset+re-dispatch oscillated against the session-loop verdict (a
    # passed subtask got reset, re-ran, passed session-loop again, M3-FAILed
    # again — observed: one subtask re-dispatched 14x). So for that path we do
    # NOT reset; we surface the finding to the user via blocked_review below.
    # DELIBERATE/DAG mode has no session-loop review, so its reset+re-dispatch
    # retry is the only review loop it has — keep it unchanged there.
    _has_dag = bool(getattr(task, "graph", None) and getattr(getattr(task, "graph", None), "nodes", None))
    # NEEDS_USER is a user-decision request, not a per-artifact defect — never
    # reset+re-dispatch it; fall straight through to blocked_review so the user
    # is asked. Only a plain FAIL drives the DAG rerun loop.
    if _has_dag and verdict == "FAIL":
        rerun_ids = rerun_subtasks_from_verdict(
            task=task, phase=phase, review_result=review_result, config=config,
        )
    else:
        rerun_ids = []
    # P0-A — retries-cap → blocked_review, NOT silent advance. Audit
    # problem 8: rerun_subtasks_from_verdict skips subtasks at max_retries,
    # so a FAIL verdict on capped subtasks returns []. Pre-fix the engine
    # treated [] as "OK to advance" — phase silently advanced past a FAIL
    # verdict, downstream phases ran against broken artifacts. Now: if
    # any subtask on this phase is already at the cap, the gate has
    # exhausted its retry budget; flip phase.status to "blocked_review"
    # and surface to the user instead of advancing.
    # P0 — a reviewer TRANSPORT fault (critic_infra_error: bridge timeout /
    # unparseable JSON) previously fell through this guard (`and not
    # critic_infra_error`) and let the phase advance SILENTLY — shipping work
    # that was never actually reviewed, with no escalation to the user. That
    # violates "honest about needing human intervention". Now a
    # reviewer-unavailable phase ALSO escalates to blocked_review (recoverable
    # via override/retry once the reviewer is healthy) rather than advancing.
    reviewer_unavailable = bool(review_result.get("critic_infra_error"))
    if not rerun_ids:
        max_retries = getattr(getattr(config, "execution", None), "max_retries", DEFAULT_MAX_RETRIES)
        # Escalate on ANY phase-level FAIL. Session-loop already reviewed each
        # artifact and we no longer re-dispatch (above), so there is no per-phase
        # retry budget to exhaust first — surface every subtask in the phase.
        capped = [
            getattr(st, "id", "") for st in getattr(phase, "subtasks", []) or []
            if getattr(st, "id", "")
        ]
        if capped:
            # F1 — continue-on-exhausted-review. When the operator opts in, a
            # FAIL verdict whose retry budget is spent does NOT park for a
            # human: classify the failure, emit a `review_overridden` event so
            # it is logged (never silently dropped), and advance the phase like
            # a PASS. NEEDS_USER is a genuine decision request — it is never
            # auto-overridden here; it still parks below.
            if (
                getattr(getattr(config, "execution", None),
                        "continue_on_review_exhausted", False)
                and verdict == "FAIL"
            ):
                load_bearing = int((det or {}).get("load_bearing_failures", 0) or 0)
                classification = {
                    "class": "reviewer_unavailable" if reviewer_unavailable else "reviewer_fail",
                    "severity": "high" if (load_bearing or reviewer_unavailable) else "medium",
                    "reviewer_unavailable": reviewer_unavailable,
                    "load_bearing_failures": load_bearing,
                    "critic_findings": len(review_result.get("critic_findings") or []),
                }
                append_log(task.id, {
                    "type": "review_overridden",
                    "phase_id": phase.id,
                    "verdict": verdict,
                    "capped_subtasks": capped,
                    "classification": classification,
                    "reason": "continue_on_review_exhausted",
                }, config.tasks_dir)
                print(
                    f"\n[REVIEW-OVERRIDE] phase {phase.id} — FAIL after "
                    f"{max_retries} retries on {', '.join(capped)}; "
                    f"continue_on_review_exhausted ON → classified "
                    f"{classification['class']}/{classification['severity']}, advancing."
                )
                try:
                    _emit_orchestrator_state(
                        task, state="idle",
                        label=(f"{_step(phase.id)} review overridden "
                               f"({classification['severity']}) — continuing"),
                        tasks_dir=config.tasks_dir,
                    )
                except Exception:
                    pass
                # Advance exactly like a PASS verdict: no park, no reset.
                return []
            from okuro.orchestrator.state import set_awaiting, set_phase_status
            # C7 — set_phase_status flips phase.status, calls save_plan, AND
            # emits a canonical phase_status_changed event so FE consumers
            # see the transition through the state-change allowlist.
            try:
                set_phase_status(task, phase, "blocked_review", config.tasks_dir)
            except Exception as exc:
                logger.warning("blocked_review set_phase_status failed: %r", exc)
            append_log(task.id, {
                "type": "phase_blocked_by_review_cap",
                "phase_id": phase.id,
                "verdict": verdict,
                "capped_subtasks": capped,
                "critic_findings": len(review_result.get("critic_findings") or []),
            }, config.tasks_dir)
            append_log(task.id, {
                "type": "orchestrator_state",
                # ROCK-SOLID v5 P1.1 — healthy-but-waiting, not a failure.
                # "error" made this the stuck red pill (347 post-resolution
                # red rows measured on task-20260730-233206).
                "state": "waiting",
                "label": (
                    f"{_step(phase.id)} blocked — reviewer FAIL after "
                    f"{max_retries} retries on {', '.join(capped)}"
                ),
            }, config.tasks_dir)
            print(
                f"\n[BLOCKED-REVIEW] phase {phase.id} — retries cap hit on "
                f"{', '.join(capped)}; user override required."
            )
            try:
                from okuro.orchestrator.gate_messages import humanize_gate as _hg
                _gm = _hg(
                    "blocked_review",
                    "infra" if reviewer_unavailable else "reviewer",
                    {"phase_id": phase.id, "rounds": 1,
                     "raw_reason": ("phase-level reviewer unavailable (transport fault)"
                                    if reviewer_unavailable
                                    else "phase-level reviewer flagged an issue across the completed work")},
                )
                set_awaiting(
                    task, config.tasks_dir,
                    kind="blocked_review",
                    message=_gm.to_message(),
                    endpoint=f"/api/tasks/{task.id}/phases/{phase.id}/override-blocked-review",
                    payload={
                        "phase_id": phase.id,
                        "presentation": _gm.to_presentation(),
                        "capped_subtasks": capped,
                        "verdict": verdict,
                        "critic_findings": review_result.get("critic_findings") or [],
                        # Raw findings are the reviewer's vocabulary, not the
                        # user's. Attach a phrased version so the UI can ask a
                        # question a person can actually answer; absent on any
                        # failure, and the UI falls back to the raw list.
                        **_brief_payload(
                            review_result.get("critic_findings") or [],
                            task, capped,
                        ),
                    },
                )
                _emit_orchestrator_state(
                    task, state="waiting", label=f"{_step(phase.id)} blocked — needs you",
                    tasks_dir=config.tasks_dir,
                )
            except Exception as exc:
                logger.warning("blocked_review set_awaiting failed: %r", exc)
            # Return the capped IDs so the caller's `if rerun_ids: continue`
            # branch keeps the loop from advancing the phase. They aren't
            # actually being re-dispatched — they're parked.
            return list(capped)
    # DP10 — when a DAG is present (deliberate mode) mirror each subtask
    # reset to its execution node. rerun_subtasks_from_verdict calls
    # update_subtask which only rewrites plan.yaml; the graph node would
    # otherwise stay "done" and the deliberate loop would never re-pick
    # the reset subtask. The auto-execute path has no graph so the block
    # is a no-op there.
    if rerun_ids and getattr(task, "graph", None) and getattr(task.graph, "nodes", None):
        from okuro.orchestrator.state import update_node
        graph_node_ids = {n.id for n in task.graph.nodes}
        for sid in rerun_ids:
            if sid not in graph_node_ids:
                continue
            try:
                update_node(task.id, sid, {
                    "status": "pending",
                    "error": "",
                    "started_at": "",
                    "completed_at": "",
                }, config.tasks_dir)
            except (ValueError, OSError) as exc:
                logger.warning(
                    "M3 review gate: DAG node %s reset failed (%s) — "
                    "subtask is pending but DAG status may be stale",
                    sid, exc,
                )
    return rerun_ids


# ────────────────────────────────────────────────────────────────────────────
# Shared loop primitives
#
# Both `orchestrator_loop` (auto-execute) and `deliberation_loop` (deliberate
# mode) iterate over work units, check gates, dispatch, and review. Pre-fix
# every milestone primitive lived in one loop but not the other — M1 lived in
# orchestrator_loop alone (deliberate never paused on gates), handle_failure
# retry+cascade lived in orchestrator_loop alone (deliberate timeouts were
# permanent), orchestrator_state running events lived in orchestrator_loop
# alone (deliberate's InlineThinker pill stayed silent). Every new feature
# required two writes and every audit found the gap on retry.
#
# These helpers centralise the primitives: one call site per loop body,
# one implementation. Future milestones land here and apply to both modes.
# ────────────────────────────────────────────────────────────────────────────


def _pause_on_pending_gate(task: Task, config: Config) -> bool:
    """M1 — block dispatch when any decision gate is pending in the plan.

    Returns True when the engine paused (caller should `continue` its loop).
    Returns False when no pending gates exist (caller proceeds to dispatch).
    Idempotent — if called repeatedly with the same pending gate, each call
    re-enters wait_for_decision which polls until status changes.

    Strict semantics from the user contract: any pending gate parks the
    whole task. Phase-N gate blocks phase 1..N-1 too. See
    `pending_decision_gates` for the rationale.
    """
    if not task.phases:
        return False
    blocking = pending_decision_gates(task)
    if not blocking:
        return False
    phase_id, gate = blocking[0]
    wait_for_decision(phase_id, gate, task, config)
    return True


def _run_phase_review_gates(task: Task, config: Config) -> bool:
    """M3 — run the reviewer on every newly-complete phase.

    Returns True if any phase had a FAIL verdict that reset subtasks (the
    caller MUST skip phase-advance this iteration). Returns False when
    every complete phase passed (or had no gate-eligible subtasks) — the
    caller is free to advance.

    Handles blocked_review per audit P0-A: when FAIL hits the retries cap,
    `_maybe_run_m3_review_gate` flips phase.status to blocked_review AND
    returns the capped ids, which this helper surfaces as "reset" so the
    caller doesn't advance.
    """
    any_reset = False
    for phase in task.phases:
        if phase.status in ("done", "blocked_review"):
            continue
        if not is_phase_complete(task, phase.id):
            continue
        # Surface reviewer activity — the critic + scorer LLM call takes
        # several minutes and was previously invisible: the UI showed
        # "no subtasks running" + frozen status badge while the engine
        # blocked on bridge_invoke. Emit a clear "reviewing" pulse so the
        # user sees the engine is doing work, not stuck.
        #
        # File flag is per-task .review-active.json so state_reader can
        # set phase.review_in_progress=True + subtask.review_in_progress=True
        # on every subtask of the phase being reviewed. FE renders an inline
        # "REVIEWING" badge on those cards so the activity is co-located
        # with the work being judged, not just in the global thinker.
        _review_flag = config.tasks_dir / task.id / ".review-active.json"
        try:
            import json as _json
            subtask_ids = [st.id for st in (getattr(phase, "subtasks", []) or [])]
            _review_flag.write_text(_json.dumps({
                "phase_id": phase.id,
                "phase_name": getattr(phase, "name", ""),
                "subtask_ids": subtask_ids,
                "since": datetime.utcnow().isoformat(),
            }))
        except Exception:
            pass
        try:
            _emit_orchestrator_state(
                task,
                state="reviewing",
                label=f"Reviewing {_step(phase.id).lower()} ({phase.name}) — critic + scorer…",
                tasks_dir=config.tasks_dir,
            )
        except Exception:
            pass
        try:
            rerun_ids = _maybe_run_m3_review_gate(task, phase, config)
        except Exception as exc:
            logger.warning(
                "M3 review gate raised %r on phase %s — proceeding without verdict",
                exc, phase.id,
            )
            rerun_ids = []
        if rerun_ids:
            any_reset = True
            print(
                f"\n[PHASE {phase.id} REVIEW] reset/blocked "
                f"{len(rerun_ids)} subtask(s): {', '.join(rerun_ids)}"
            )
        # Always clear the review flag — pass, fail+retry, or cap+blocked.
        # FE relies on absence-of-flag to switch the inline badge off so it
        # never sticks past the actual review window.
        try:
            _review_flag.unlink(missing_ok=True)
        except Exception:
            pass
        # Emit idle so the global thinker doesn't keep the stale "Reviewing…"
        # label on screen — next dispatch (running) or next set_awaiting
        # (waiting_user) will re-light it with the right context.
        try:
            _emit_orchestrator_state(
                task, state="idle", label="",
                tasks_dir=config.tasks_dir,
            )
        except Exception:
            pass
    return any_reset


def _emit_orchestrator_state(
    task: Task, *, state: str, label: str, tasks_dir: Path,
) -> None:
    """Centralised orchestrator_state emit. Pre-consolidation each loop had
    its own emit lines at dispatch / idle / decompose / gate-wait boundaries,
    and `deliberation_loop` simply omitted most of them — the InlineThinker
    pill stayed silent through deliberate runs. One helper, one call shape,
    no drift."""
    # Dedupe: skip a no-op re-emit of the same (state,label). Lets callers
    # emit idle inside a polling park (e.g. the deliberate wait-for-deps
    # loop) without spamming log.jsonl every tick — only the transition is
    # written. The heartbeat re-emits via append_log directly, bypassing
    # this, so its intentional keep-alive re-emits are unaffected.
    if (state or "idle") == _LAST_STATE_STATE and (label or "") == _LAST_STATE_LABEL:
        return
    append_log(task.id, {
        "type": "orchestrator_state",
        "state": state,
        "label": label,
    }, tasks_dir)
    # PR A — record the last state so the heartbeat daemon can re-emit it
    # while a long bridge_invoke (reviewer / decomposer) blocks the work
    # loop. Without this the OkuroThinker pill freezes on the last label.
    _record_last_state(state, label)


def _append_activity_row(task_dir: Path, row: dict) -> None:
    """Append one tagged row to ``<task_dir>/.activity.jsonl``.

    Single writer for telemetry-style rows the FE activity feed tails
    (review_starting / review_complete markers). Mirrors the
    bridge.executor / dispatcher ``_emit`` shape: caller pre-tags
    ``subtask_id`` + ``role`` + ``type``; helper stamps ``ts`` if missing
    and serialises atomically. Best-effort — never raises on the engine
    hot path (a missing tasks-dir or read-only volume is not a halt).
    """
    try:
        import json as _json
        row = dict(row)
        row.setdefault("ts", datetime.utcnow().isoformat())
        path = task_dir / ".activity.jsonl"
        with open(path, "a") as af:
            af.write(_json.dumps(row) + "\n")
    except Exception:
        pass


def _reap_subagent_subprocess(task: Task, subtask: Subtask, config: Config,
                              reason: str) -> None:
    """C4 — engine-side reap of any in-flight subagent subprocess for this
    subtask. Drops the kill-request flag the dispatcher polls (dispatcher.py
    ``_watch_kill_request``) — that watcher SIGTERMs the process group with
    a 5 s grace before SIGKILL. Safe to call when no duplicate dispatch is
    in flight: the watcher exits when ``proc.poll() is not None``.

    Belt-and-suspenders alongside the EventWatcher ``is_phantom_event``
    filter — between them, no event from a post-terminal (task, subtask)
    can reach FE clients (D11 root).
    """
    try:
        kill_dir = config.tasks_dir / task.id / ".kill-requests"
        kill_dir.mkdir(parents=True, exist_ok=True)
        flag = kill_dir / subtask.id
        # Idempotent — overwriting the flag is fine; the watcher reads-once
        # on the first observation.
        flag.write_text(reason)
    except OSError as exc:
        logger.warning(
            "[%s] subagent reap kill-flag write failed: %r", subtask.id, exc,
        )


# ROCK-SOLID v5 P5.7 — role hints, mirroring decomposer._is_build_shaped.
#
# Imported rather than redefined would be cleaner, but the decomposer pulls in
# the LLM bridge at import time and the engine already carries enough startup
# weight. Kept as a tuple in both places with this note in both, so a drift
# between them is at least visible; a shared constants module for two tuples
# would be the third place to look.
_BUILD_SHAPED_ROLE_HINTS = (
    "engineer", "developer", "frontend", "backend", "fullstack",
    "designer", "writer", "author", "builder", "implementer",
)


def _is_build_shaped_role(role: str | None) -> bool:
    """True when a role implies file-producing work. Shared by the P5.7
    finalize warning and the P2.2 gate decision — one vocabulary, so the two
    cannot disagree about which subtasks are expected to write."""
    r = str(role or "").lower()
    return bool(r) and any(h in r for h in _BUILD_SHAPED_ROLE_HINTS)


def _warn_if_artifact_only_points_at(task: Task, subtask: Subtask,
                                     files: list[str]) -> None:
    """Warn when the artifact body summarises a file instead of carrying it.

    Measured 2026-08-01 across 200 user-audience reports: 11 (6%) have a body
    under 2.5 KB that names a deliverable file. One of them shipped live — a
    briefing whose artifact body read "Deliverable: report.html
    (self-contained, inline CSS)" while the actual content sat on disk.

    That shape degrades three consumers unequally, which is why it is worth a
    warning rather than a shrug:

      * the M3 reviewer reads BOTH artifact bodies and files declared by
        deterministic checks, so it grades the summary AND the file — but the
        critic's judgement is anchored on the body it was told is canonical;
      * the preview proposer resolves declared paths at rank 0, so "See
        result" opens the real file — this consumer is fine;
      * Stream C delivers the artifact BODY ONLY. The recipient receives a
        summary of a document they do not have.

    The delivery asymmetry is the real cost and it is NOT fixed here: making
    `peer/delivery` read declared files would invert a layer — that module is
    generic and must not import orchestrator state. Surfacing it at finalize
    puts the warning where the knowledge already lives.

    Best-effort throughout, like its caller: this runs between "the work
    succeeded" and "mark it done".
    """
    try:
        from okuro.sense.artifacts import artifact_list, artifact_get

        rows = artifact_list(
            task_id=task.id, subtask_id=subtask.id,
            audience="user", include_superseded=False, limit=1,
        )
        if not rows:
            return
        full = artifact_get(str(rows[0].get("id")), include_body=True) or {}
        body_len = len((full.get("body") or "").strip())
        if not body_len:
            return

        # Compare against the LARGEST declared file: a subtask shipping a
        # 4 KB page plus a 200-byte config is not summarising anything.
        largest = 0
        biggest_name = ""
        for f in files:
            try:
                size = os.path.getsize(f)
            except OSError:
                continue
            if size > largest:
                largest, biggest_name = size, f
        if largest < 1024:
            return
        # 2x is deliberately loose. A body genuinely containing the
        # deliverable is usually LARGER than the file (it quotes it plus
        # context); a pointer is a small fraction. The gap between those two
        # is wide, so a tight threshold buys nothing and costs false alarms.
        if body_len * 2 >= largest:
            return

        msg = (
            f"{subtask.id}: the user-facing artifact ({body_len} chars) is much "
            f"smaller than its declared deliverable {biggest_name} ({largest} "
            "bytes) — it likely POINTS AT the file rather than containing it. "
            "The preview opens the file, but Stream C delivers artifact bodies "
            "only, so a recipient would receive the summary instead of the work."
        )
        logger.warning("[%s] %s", subtask.id, msg)
        from okuro.sense.task_events import append_event

        append_event(
            task_id=task.id,
            subtask_id=subtask.id,
            from_role="orchestrator-detector",
            event_type="gap",
            body={"summary": msg, "affects": [subtask.id]},
            confidence=0.7,
            created_by="orchestrator-detector",
        )
    except Exception as exc:  # pragma: no cover — advisory must never fail
        logger.debug("pointer-artifact check raised %r", exc)


def _warn_if_no_user_deliverable(task: Task, subtask: Subtask, brief: dict | None) -> None:
    """Warn when build-shaped work finishes with nothing addressed to the user.

    "Nothing" means both of: no declared file that exists on disk (outputs,
    target_paths, or brief.produced_files), and no user-audience artifact row.
    Either one alone is enough to preview, which is the bar this checks
    against — it asks the same question the preview proposer asks, so a
    warning here predicts a miss there rather than approximating it.

    Best-effort in full: every failure path returns silently. A finalize that
    crashed inside its own advisory check would turn a completed subtask into
    a failed one, which is exactly the trade this is written to avoid.
    """
    try:
        role = str(getattr(subtask, "role", "") or "").lower()
        if not role or not any(h in role for h in _BUILD_SHAPED_ROLE_HINTS):
            return

        declared: list[str] = []
        for src in (
            getattr(subtask, "outputs", None),
            getattr(subtask, "target_paths", None),
            (brief or {}).get("produced_files"),
        ):
            if isinstance(src, (list, tuple)):
                declared.extend(str(x) for x in src if isinstance(x, str))
        # Existence, not declaration: Check 4 above already refuses a brief
        # whose produced_files vanished, but `outputs` is a plan-time promise
        # that nothing verifies, and a promise of a file is not a file.
        existing = [x.strip() for x in declared
                    if x.strip() and os.path.exists(x.strip())]
        if existing:
            _warn_if_artifact_only_points_at(task, subtask, existing)
            return

        from okuro.sense.artifacts import artifact_list

        rows = artifact_list(
            task_id=task.id,
            subtask_id=subtask.id,
            audience="user",
            include_superseded=False,
            limit=1,
        )
        if rows:
            return

        msg = (
            f"{subtask.id} ({role}) finished with no user-facing deliverable: "
            "no declared file exists on disk and no audience='user' artifact "
            "was written. The work is recorded, but 'See result' has nothing "
            "to open — declare outputs, or write the report with audience='user'."
        )
        logger.warning("[%s] %s", subtask.id, msg)
        # Same shape and same table as the decision-silence detector below —
        # a `gap` event from `orchestrator-detector`. A second event type for
        # the second detector would be the "two notions of one thing" mistake
        # this plan keeps deleting; the body says which gap it is.
        from okuro.sense.task_events import append_event

        append_event(
            task_id=task.id,
            subtask_id=subtask.id,
            from_role="orchestrator-detector",
            event_type="gap",
            body={"summary": msg, "affects": [subtask.id]},
            confidence=0.9,
            created_by="orchestrator-detector",
        )
    except Exception as exc:  # pragma: no cover — advisory must never fail
        logger.debug("deliverable check raised %r", exc)


def _finalize_subtask(task: Task, subtask: Subtask, result: dict, config: Config) -> bool:
    """Mark a successful subtask done — but only after verifying all three:
    Stream B (artifact row), Stream A (role-handover row), brief.outcome.

    Verification chain (each step gates the next, all surface via handle_failure):
      1. save_artifact — legacy stdout fallback so cortex re-indexing picks
         up the file under the historical name.
      2. Stream B — at least one ``artifacts(task_id, subtask_id)`` row, OR
         the legacy disk path exists (covers binaries / screenshots / brief
         crossover window).
      3. Stream A — at least one ``role_handovers(task_id, subtask_id)``
         row. Without it, downstream subagents hit the dispatcher's HR9
         "no handover" placeholder and inherit an explicit absence note
         instead of this subtask's context.
      4. brief.outcome == "success" — subagents that ship valid A+B with
         outcome="partial" or "failed" are honestly reporting incomplete
         work; they MUST NOT be silently marked done. This was the largest
         remaining ghost-completion vector after migration 035.

    Returns True when the subtask was marked done, False when it was kicked
    back into the failure path.
    """
    # Wave-6 G24 — only persist legacy disk artifact when Stream B is
    # absent. Pre-fix this ran unconditionally and produced a duplicate
    # `.md` next to every brain row → cortex returned both → contract
    # drift. Now we check Stream B first; if present, skip the legacy
    # save (cortex already has the canonical version via vec_artifacts).
    # If absent, the disk write still fires as a safety net for
    # binaries / screenshots / stdout-only subagents.
    brain_ok = _has_brain_artifact(task.id, subtask.id)
    handover_ok, outcome, brief = _read_handover_outcome(task.id, subtask.id)

    if not brain_ok:
        save_artifact(task.id, subtask.id, result["output"], config.tasks_dir,
                      artifact_name=subtask.artifact_name)

    expected = _expected_artifact_path(task.id, subtask, config.tasks_dir)
    try:
        disk_size = expected.stat().st_size if expected.exists() else 0
    except OSError:
        disk_size = 0
    disk_ok = expected.exists() and disk_size > 0

    # Check 1 — Stream B present (artifact row OR legacy disk fallback).
    if not (brain_ok or disk_ok):
        synthetic_error = (
            f"Subagent reported success but produced no artifact (no Stream B "
            f"row in artifacts(task_id={task.id}, subtask_id={subtask.id}) "
            f"and no file at {expected}). Expected artifact_write(kind='report')."
        )
        logger.warning("[%s] artifact verification failed: %s", subtask.id, synthetic_error)
        synthetic_result = {
            **result,
            "success": False,
            "error": synthetic_error,
        }
        handle_failure(subtask, task, synthetic_result, config)
        return False

    # Check 2 — Stream A present. Cross-process equivalent of the subagent's
    # session_state["handover_written"] flag. Without a handover row, every
    # downstream subagent depending on this one will receive the dispatcher's
    # HR9 absence placeholder instead of upstream context — silent context
    # loss across the DAG.
    if not handover_ok:
        synthetic_error = (
            f"Subagent reported success but produced no role-handover row "
            f"(no Stream A in role_handovers(task_id={task.id}, "
            f"subtask_id={subtask.id})). Expected write_role_handover(...). "
            f"Without it, every downstream subagent depending on {subtask.id} "
            f"will receive an HR9 absence note instead of upstream context."
        )
        logger.warning("[%s] handover verification failed: %s", subtask.id, synthetic_error)
        synthetic_result = {
            **result,
            "success": False,
            "error": synthetic_error,
        }
        handle_failure(subtask, task, synthetic_result, config)
        return False

    # Check 3 — brief.outcome must be "success". Honest partial/failed
    # outcomes route to handle_failure; the subtask must either resolve
    # the open questions on retry, surface them, or cascade-skip dependents.
    if outcome != "success":
        open_qs = (brief or {}).get("open_questions") or []
        open_qs_str = (
            "; ".join(str(q) for q in open_qs[:5])
            if open_qs else "(none declared)"
        )
        synthetic_error = (
            f"Subagent self-reported outcome='{outcome or 'missing'}' in "
            f"role-handover. Open questions: {open_qs_str}. Per HR9, this "
            f"subtask cannot be marked done — the orchestrator must either "
            f"(a) re-dispatch with the open questions resolved, "
            f"(b) surface to user, or (c) cascade-skip dependents."
        )
        logger.warning("[%s] outcome verification failed: %s", subtask.id, synthetic_error)
        synthetic_result = {
            **result,
            "success": False,
            "error": synthetic_error,
        }
        handle_failure(subtask, task, synthetic_result, config)
        return False

    # Check 4 — produced_files exist on disk at finalize-time. Belt-and-
    # suspenders complement to middleware V11 (handover-write-time check):
    # files can be deleted/renamed in a sibling worktree between handover
    # write and finalize, and audit A3 explicitly flagged this. Cheap
    # re-check closes the worktree-merge window.
    produced_files_final = (brief or {}).get("produced_files") or []
    missing_at_finalize: list[str] = []
    for p in produced_files_final:
        if not isinstance(p, str):
            continue
        p_stripped = p.strip()
        if p_stripped and not os.path.exists(p_stripped):
            missing_at_finalize.append(p_stripped)
    if missing_at_finalize:
        sample = "; ".join(missing_at_finalize[:5])
        synthetic_error = (
            f"Subagent reported success but {len(missing_at_finalize)} "
            f"brief.produced_files path(s) no longer exist on disk: {sample}"
            f"{' (and more)' if len(missing_at_finalize) > 5 else ''}. "
            "Files may have been deleted/renamed in a sibling worktree "
            "before merge, or the brief listed phantom paths. Per umbrella "
            "audit fix #1."
        )
        logger.warning("[%s] produced_files disk-verify failed: %s",
                       subtask.id, synthetic_error)
        synthetic_result = {
            **result,
            "success": False,
            "error": synthetic_error,
        }
        handle_failure(subtask, task, synthetic_result, config)
        return False

    # ROCK-SOLID v5 P5.7 — the other half of the plan-time outputs warning.
    #
    # Check 1 above already refuses a subtask that shipped NOTHING. This is
    # the narrower, quieter failure it cannot see: a build-shaped subtask that
    # shipped only a PROCESS-audience artifact and no file. Check 1 passes —
    # `_has_brain_artifact` filters on (task, subtask) and takes any audience —
    # while the preview proposer's rank-1 detector filters `audience="user"`.
    # So the subtask is done, the reviewer is satisfied, and the user presses
    # "See result" and gets P5.6's chooser. Nothing is broken; there is simply
    # nothing addressed to them.
    #
    # Deliberately a warning. The subagent has already finished and its work
    # is real, so failing here would destroy a good result over a routing
    # mistake — and re-dispatching does not reliably fix routing. It logs, and
    # it emits so the reader sees it at the time rather than in a log they
    # will never open.
    _warn_if_no_user_deliverable(task, subtask, brief)

    # C4 — reap any duplicate subagent subprocess for this subtask BEFORE
    # writing node_done. Closes LF2 / V5: when two engines (or two
    # dispatches from the same engine after a reconciler reset) have
    # running subprocesses for the same (task_id, subtask_id), the
    # dispatcher's _watch_kill_request poller picks up this flag and tears
    # the duplicate down with SIGTERM → SIGKILL grace. Belt-and-suspenders
    # for is_phantom_event filtering on the API side.
    _reap_subagent_subprocess(task, subtask, config, reason="node_done")

    update_subtask(task.id, subtask.id, {
        "status": "done",
        "completed_at": datetime.utcnow().isoformat(),
        "duration": result["duration"],
        "cli_used": result["cli"],
        "model_used": result["model"],
        "output_summary": result["output"][:500],
        "error": "",
    }, config.tasks_dir)

    # P0-2 — M2 producer-event silence detector. Stream A + B capture the
    # deliverable surface; M2 captures cross-subtask reasoning. When a
    # subagent finalizes with zero producer events (decision / contract /
    # gap / open_question / supersedes), the silence itself is a finding —
    # downstream subagents lose the Decision-Trace / Active-Decisions
    # context block. Emit a `gap` event so the silence is surfaced in
    # the M2 log instead of being invisible. Best-effort: telemetry, not
    # a failure mode for the subtask.
    _emit_decision_silent_gap_if_needed(task, subtask)

    # Stream B tailoring — rewrite this subtask's user-facing report/plan
    # artifacts from the M3 reviewer-evidence shape into the user's own
    # communication voice, preserving the reviewer body as a process-audience
    # evidence copy. Runs post-finalize (review already passed). Best-effort:
    # any failure leaves the raw artifact user-visible, never fails the subtask.
    _maybe_tailor_user_artifact(task, subtask, config)

    # Stream C — audience-adapted delivery. Fan out per Stream B artifact
    # this subtask produced. HR-C3: best-effort, never fails the subtask.
    if getattr(task, "audience", ""):
        _fan_out_deliveries(task, subtask, config.tasks_dir)

    return True


def _maybe_tailor_user_artifact(task: Task, subtask: Subtask, config: Config) -> None:
    """Run the tailor pass over this subtask's user-facing deliverables.

    Gated by ``config.orchestrator.tailor_user_artifacts`` (default True).
    Best-effort by design — mirrors ``_emit_decision_silent_gap_if_needed``:
    a missing module, a bridge outage, or a translate failure must never turn
    a successful subtask into a failure. On any problem the raw reviewer-shaped
    artifact simply stays user-visible.
    """
    try:
        if not getattr(config.orchestrator, "tailor_user_artifacts", True):
            return
    except Exception:
        return
    try:
        from okuro.orchestrator.tailor import run_tailor
    except Exception as exc:
        logger.debug("[%s] tailor: import failed (%s)", subtask.id, exc)
        return
    try:
        res = run_tailor(task_id=task.id, subtask_id=subtask.id)
    except Exception as exc:
        logger.warning("[%s] tailor pass skipped: %s", subtask.id, exc)
        return
    if res.get("ok") and res.get("tailored"):
        logger.info(
            "[%s] tailored %d user artifact(s) into reader voice (skipped %d)",
            subtask.id, len(res["tailored"]), res.get("skipped", 0),
        )
    elif res.get("error"):
        logger.debug("[%s] tailor: %s", subtask.id, res["error"])


_PRODUCER_EVENT_TYPES: frozenset[str] = frozenset({
    "decision", "supersedes", "contract", "gap", "open_question",
})


def _emit_decision_silent_gap_if_needed(task: Task, subtask: Subtask) -> None:
    """Append a `gap` event when the subtask finalized without producing
    any decision/contract/gap/open_question/supersedes event. Logs the
    detection at WARNING; the gap event itself is the persisted record.

    Best-effort — any failure in the M2 path is swallowed so a missing
    detector cannot turn a successful subtask into a failure.
    """
    try:
        from okuro.sense.task_events import list_events, append_event
    except Exception as exc:
        logger.debug("[%s] decision-silent detector: import failed (%s)", subtask.id, exc)
        return
    try:
        events = list_events(task_id=task.id, limit=500)
    except Exception as exc:
        logger.debug("[%s] decision-silent detector: list_events failed (%s)", subtask.id, exc)
        return
    has_producer = any(
        (e.get("subtask_id") == subtask.id and e.get("event_type") in _PRODUCER_EVENT_TYPES)
        for e in events
    )
    if has_producer:
        return
    logger.warning(
        "[%s] decision_silent — subtask finalized with zero producer events "
        "(decision/contract/gap/open_question/supersedes). Emitting synthetic gap.",
        subtask.id,
    )
    try:
        append_event(
            task_id=task.id,
            subtask_id=subtask.id,
            from_role="orchestrator-detector",
            event_type="gap",
            body={
                "summary": (
                    f"Subtask {subtask.id} (role={subtask.role}) finalized "
                    f"without emitting any M2 producer event. Downstream "
                    f"subagents lose the Decision-Trace/Active-Decisions "
                    f"context for this work."
                ),
                "affects": [subtask.id],
            },
            confidence=0.9,
            created_by="orchestrator-detector",
        )
    except Exception as exc:
        logger.debug(
            "[%s] decision-silent detector: append_event(gap) failed (%s)",
            subtask.id, exc,
        )


# Extensions a recipient can actually read. A .png or .zip in a markdown
# delivery is bytes in a text field; the preview already opens those, and
# this path is text-shaped by construction.
_DELIVERABLE_TEXT_EXTS = (".md", ".html", ".htm", ".txt", ".csv", ".json", ".yaml", ".yml")
_MAX_EXTRA_DOCUMENTS = 3


def _declared_document_texts(subtask: Subtask) -> list[tuple[str, str]]:
    """The subtask's declared deliverables, as (label, text) for delivery.

    Closes the consumer asymmetry measured 2026-08-01: the M3 reviewer reads
    artifact bodies AND files declared by deterministic checks; the preview
    proposer resolves declared paths at rank 0; the delivery pipeline read
    BODIES ONLY. So a subagent that wrote a pointer-style artifact ("Deliverable:
    report.html") handed its recipient a summary of a document they did not
    have — observed live, on a real person.

    Resolution lives HERE and not in `peer/delivery` on purpose. That module
    serves any artifact from any source, including manual delivery_send calls
    with no orchestrator behind them, and must not learn what a subtask's
    declared outputs are. It takes content; this supplies it.

    Best-effort: any failure yields fewer documents, never an exception. A
    delivery that loses an attachment is worse than one that gains nothing,
    and both are far better than a subtask that fails at finalize.
    """
    out: list[tuple[str, str]] = []
    try:
        declared: list[str] = []
        for src in (getattr(subtask, "outputs", None),
                    getattr(subtask, "target_paths", None)):
            if isinstance(src, (list, tuple)):
                declared.extend(str(x) for x in src if isinstance(x, str) and x.strip())

        seen: set[str] = set()
        for raw in declared:
            if len(out) >= _MAX_EXTRA_DOCUMENTS:
                break
            path = Path(raw.strip())
            if path.suffix.lower() not in _DELIVERABLE_TEXT_EXTS:
                continue
            try:
                if not path.is_file():
                    continue
                key = str(path.resolve())
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if text.strip():
                out.append((path.name, text))
    except Exception as exc:  # pragma: no cover — never block a delivery
        logger.debug("declared-document collection raised %r", exc)
    return out


def _fan_out_deliveries(task: Task, subtask: Subtask, tasks_dir=None) -> None:
    """Run delivery_send per Stream B artifact this subtask produced.

    Best-effort by design — every step is wrapped so a missing person,
    a translate failure, or a channel renderer crash leaves a row with
    success=0 instead of failing the subtask. Logs at INFO on success
    and WARNING on errors; the deliveries table itself is the audit
    log.
    """
    try:
        from okuro.sense.artifacts import artifact_list
        from okuro.peer.delivery.pipeline import send as _delivery_send
    except Exception as exc:
        logger.warning("[%s] delivery imports failed: %s", subtask.id, exc)
        return

    try:
        # Both filters were missing, and the first live Stream C run
        # (2026-08-01) shipped three deliveries for ONE subtask because of it:
        # the draft, the revision that superseded it, and the reviewer's
        # AC-evidence artifact.
        #
        # include_superseded=False — a superseded revision is by definition
        # not what the subtask shipped. Delivering it sends the recipient a
        # draft they then have to reconcile against the real one, and the
        # more review rounds a subtask takes the more copies they get.
        #
        # audience="user" — belt to the braces of the _infer_audience fix in
        # sense/artifacts.py. That fix stops the AC-evidence artifact being
        # BORN as audience="user"; this stops anything not addressed to a
        # person being delivered to one, whatever produced it. The preview
        # proposer has filtered on audience since P5.1 — the fan-out reading
        # the same store through a wider filter was the discrepancy.
        rows = artifact_list(
            task_id=task.id,
            subtask_id=subtask.id,
            audience="user",
            include_superseded=False,
            limit=20,
        )
    except Exception as exc:
        logger.warning("[%s] artifact_list for delivery raised: %s", subtask.id, exc)
        return

    if not rows:
        logger.debug("[%s] no Stream B rows; skipping delivery fan-out", subtask.id)
        return

    channel = getattr(task, "delivery_channel", "") or "markdown"
    brand_id = getattr(task, "delivery_brand_id", "") or None

    for row in rows:
        aid = row.get("id") if isinstance(row, dict) else None
        if not aid:
            continue
        try:
            envelope = _delivery_send(
                aid,
                person_id=task.audience,
                channel=channel,
                brand_id=brand_id,
                created_by=f"orchestrator/{task.id}/{subtask.id}",
                extra_documents=_declared_document_texts(subtask),
            )
        except Exception as exc:
            logger.warning("[%s] delivery_send raised on artifact %s: %s",
                           subtask.id, aid, exc)
            continue
        if envelope.get("success"):
            logger.info("[%s] delivered %s -> %s (channel=%s, person=%s)",
                        subtask.id, aid, envelope.get("delivery_id"),
                        channel, task.audience)
            # P3.3 — the ONLY signal that Stream C moved. Best-effort like the
            # rest of this function: a delivery that happened must not be
            # undone by a logging failure.
            try:
                if tasks_dir is None:
                    raise RuntimeError("no tasks_dir passed to _fan_out_deliveries")
                from okuro.orchestrator.state import emit_event as _emit

                _emit(task.id, "delivery_sent", {
                    "subtask_id": subtask.id,
                    "artifact_id": aid,
                    "delivery_id": envelope.get("delivery_id"),
                    "channel": channel,
                }, tasks_dir)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("[%s] delivery_sent emit failed: %r",
                               subtask.id, exc)
        else:
            logger.warning("[%s] delivery for %s failed: %s",
                           subtask.id, aid, envelope.get("error"))


def _persist_declared_project_path(task: Task, config: Config, *, source: str) -> None:
    """Write ``task.project_path`` back into ``task.yaml`` and emit a
    ``project_path_declared`` log event. ``source`` identifies the
    caller (``finalize_handovers`` / ``user_picker`` / etc.) so the
    log row is auditable. Best-effort: caller catches OSError."""
    import yaml as _yaml
    task_yaml = config.tasks_dir / task.id / "task.yaml"
    with open(task_yaml) as f:
        td = yload(f) or {}
    td["project_path"] = task.project_path
    td.pop("project_path_inferred_none", None)
    with open(task_yaml, "w") as f:
        _yaml.safe_dump(td, f, default_flow_style=False)
    append_log(task.id, {
        "type": "project_path_declared",
        "project_path": task.project_path,
        "source": source,
    }, config.tasks_dir)


def _adopt_declared_project_path(task: Task, config: Config) -> None:
    """If ``task.project_path`` is empty, look for a declared path in
    role-handover briefs and adopt it. Called once on task finalize so
    the preview "See result" feature can resolve the project root
    without heuristics."""
    if task.project_path:
        return
    try:
        from okuro.sense.role_handover import read_declared_project_path
        declared = read_declared_project_path(task.id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[%s] handover project_path lookup raised: %s", task.id, exc)
        declared = None
    if not declared:
        return
    task.project_path = declared
    try:
        _persist_declared_project_path(task, config, source="finalize_handovers")
        logger.info("[%s] adopted declared project_path=%s", task.id, declared)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] could not persist declared project_path: %s",
                       task.id, exc)


def _run_verify_command(task: Task, config: Config) -> tuple[bool, str]:
    """Execute task.verify_command as the build/test gate.

    Runs in ``task.project_path`` (the actual repo where subagents
    wrote the code). The path is now sourced declaratively from
    role-handover briefs (see ``_adopt_declared_project_path``); when
    a task reaches verify with no project_path declared, we fail with
    a clear message rather than mining activity.jsonl heuristically.
    10-minute hard cap — verify is expected to be a fast
    ``pytest -q``/``npm test``/``make check``-class command, not a
    full CI run. Returns (passed, combined_output).
    """
    import subprocess
    from pathlib import Path

    if not task.project_path:
        return False, ("verify_command could not run: task.project_path is "
                       "unset. Subagents must declare the realized project "
                       "root via brief['project_path'] in their role-handover; "
                       "alternatively set project_path explicitly via "
                       "PUT /api/tasks/{id}/project-path before verify.")
    cwd_path = Path(task.project_path)
    if not cwd_path.exists() or not cwd_path.is_dir():
        return False, f"verify_command cwd does not exist: {cwd_path}"

    try:
        proc = subprocess.run(
            task.verify_command,
            shell=True,
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return False, f"verify_command timed out after 600s: {task.verify_command}"
    except OSError as exc:
        return False, f"verify_command failed to launch: {exc}"

    output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode == 0, output


def _should_deliberate_continuation(task: Task, intervention, config: Config) -> bool:
    """Resolve the deliberation-on-continuation toggle for ONE continuation.

    Precedence (most specific wins):
      1. ``Intervention.deliberate``           — per-continuation override
      2. ``Task.deliberate_continuations``     — task-level default
      3. ``config.orchestrator.deliberate_continuations`` — global default (False)
    """
    if getattr(intervention, "deliberate", None) is not None:
        return bool(intervention.deliberate)
    if getattr(task, "deliberate_continuations", None) is not None:
        return bool(task.deliberate_continuations)
    orch = getattr(config, "orchestrator", None)
    return bool(getattr(orch, "deliberate_continuations", False))


def _seed_continuation_council(task: Task, intervention, config: Config) -> bool:
    """Seed a fresh deliberation council round for a continuation.

    Reuses the initial-deliberation machinery: propose a panel keyed on the
    NEW instructions, create position + discussion nodes for the next round,
    and anchor them (``context_from``) on the latest resolved discussion so
    panellists inherit the prior authority map + user direction. The
    continuation text rides on ``directive`` → each node's ``description`` →
    surfaced by ``build_position_prompt``.

    Marks ``intervention.council_round`` so ``pending_continuations`` stops
    treating it as un-decomposed (else the drain re-seeds a council every
    tick while positions/discussion are still running). Phases are NOT created
    here — they materialize when the council resolves and the deliberation
    loop's auto-decompose step runs. Returns True when a council was seeded.
    """
    from okuro.orchestrator.deliberation import propose_panel, create_position_nodes
    from okuro.orchestrator.state import save_plan, save_task_meta

    if task.graph is None or not task.graph.nodes:
        return False

    try:
        panel = propose_panel(intervention.text, max_roles=5)
    except Exception as e:
        print(f"[CONTINUE-COUNCIL] panel proposal error: {e}")
        panel = []
    roles: list[str] = []
    seen: set = set()
    for p in panel:
        r = p.get("role") if isinstance(p, dict) else None
        if r and r not in seen:
            seen.add(r)
            roles.append(r)
    if not roles:
        return False

    resolved = [n for n in task.graph.nodes
                if n.type == "discussion" and n.status == "resolved"]
    context_from = max(resolved, key=lambda n: n.round).id if resolved else ""
    next_round = max(
        (n.round for n in task.graph.nodes if n.type == "discussion"),
        default=0,
    ) + 1

    _, discuss_id = create_position_nodes(
        task.graph, roles=roles, round_num=next_round,
        context_from=context_from,
        sources={r: "continuation" for r in roles},
        directive=intervention.text,
    )
    if task.deliberation:
        task.deliberation.current_round = next_round

    intervention.council_round = next_round
    save_plan(task, config.tasks_dir)
    save_task_meta(task, config.tasks_dir)
    append_log(task.id, {
        "type": "continuation_council_seeded",
        "intervention": intervention.id,
        "round": next_round,
        "discussion": discuss_id,
        "roles": roles,
    }, config.tasks_dir)
    print(
        f"[CONTINUE-COUNCIL] seeded round {next_round} council "
        f"({len(roles)} roles: {', '.join(roles)}) for {intervention.id}"
    )
    return True


def _drain_pending_continuations(task: Task, config: Config) -> bool:
    """Decompose any continuation recorded but never turned into phases.

    Closes the continue-vs-resume race. When a continuation arrives for an
    already-`done` task, ``continue_task`` records the intervention and spawns
    a ``--continue`` engine — but that engine can stall in the pre-decompose
    compression step long enough for the daemon engine-reconciler to respawn a
    ``--resume`` engine, which finds every original phase done, finalizes to
    ``done`` + writes ``.exited``, and the scope teardown kills ``--continue``
    before ``decompose_continuation`` ever runs. The intervention survives on
    disk (``before_phase_id`` > highest phase id); this drain lets WHICHEVER
    engine reaches the work loop materialize it, so the race winner no longer
    matters.

    Idempotent: ``decompose_continuation`` numbers new phases from
    ``max(phase.id)+1``, so once appended, ``before_phase_id <= max`` and a
    second pass returns no pending items. Mutates ``task`` in place with the
    reloaded phases/graph/interventions. Returns True if phases were appended.
    """
    from okuro.orchestrator.state import pending_continuations

    pend = pending_continuations(task)
    if not pend:
        return False

    # Split by the deliberation-on-continuation toggle. A fresh council needs a
    # DAG (deliberate mode); auto-execute tasks always take the single-shot
    # path regardless of the toggle.
    can_deliberate = task.graph is not None and bool(task.graph.nodes)
    council_pend = [
        i for i in pend
        if can_deliberate and _should_deliberate_continuation(task, i, config)
    ]
    plain_pend = [i for i in pend if i not in council_pend]

    changed = False

    # (A) Deliberate continuations — seed a fresh council round each. Phases are
    # NOT created here; the council resolves later (positions → discussion →
    # user proceed → auto-decompose). Seeding marks intervention.council_round
    # so this drain does not re-seed on the next tick.
    for i in council_pend:
        try:
            if _seed_continuation_council(task, i, config):
                changed = True
        except Exception as e:
            print(f"[CONTINUE-COUNCIL] seeding failed for {i.id} ({e}) — "
                  f"falling back to single-shot decompose")
            plain_pend.append(i)

    # (B) Single-shot continuations — decompose + append immediately (the
    # original race-recovery path).
    if plain_pend:
        instructions = "\n\n".join(i.text for i in plain_pend)
        print(
            f"[CONTINUE-DRAIN] {len(plain_pend)} un-decomposed continuation(s) "
            f"— decomposing (single-shot)"
        )
        task.status = "planning"
        update_task_status(task, config.tasks_dir)
        try:
            new_phases = decompose_continuation(instructions, task, config)
        except Exception as e:
            print(f"[ERROR] Pending-continuation decomposition failed: {e}")
            new_phases = []
        if new_phases:
            superseded = apply_supersedes(task, new_phases, config.tasks_dir)
            if superseded:
                print(
                    f"[SUPERSEDED] {len(superseded)} prior subtask(s) marked "
                    f"obsolete by drained correction: {', '.join(superseded)}"
                )
            append_phases(task, new_phases, config.tasks_dir)
            append_log(task.id, {
                "type": "continuation_drained_on_resume",
                "interventions": [i.id for i in plain_pend],
                "new_phases": [p.id for p in new_phases],
            }, config.tasks_dir)
            print(
                f"[CONTINUE-DRAIN] appended {len(new_phases)} phase(s): "
                f"{', '.join(str(p.id) for p in new_phases)}"
            )
            changed = True

    # Reload the mutated task in place ONLY when something wrote to disk
    # (council seeding or append_phases). When nothing was produced, avoid a
    # disk read (the task may be purely in-memory) and simply restore `active`
    # so the loop can finalize cleanly rather than parking in `planning`.
    if changed:
        fresh = load_task(task.id, config.tasks_dir)
        task.phases = fresh.phases
        task.graph = fresh.graph
        task.interventions = fresh.interventions
    task.status = "active"
    update_task_status(task, config.tasks_dir)

    return changed


def _finalize_task(task: Task, config: Config, active_futures: dict,
                   brain_session_id: Optional[str] = None):
    """Drain in-flight futures, run the optional verify gate, then mark
    the task done. A non-zero verify exit short-circuits to status=failed
    so harvest/continuation/source-todo cleanup do not fire on broken
    code."""
    if active_futures:
        for future in as_completed(active_futures):
            subtask = active_futures[future]
            try:
                result = future.result()
                if result["success"]:
                    # Route through _finalize_subtask so the disk-verify check
                    # also applies to drained futures (otherwise a subagent that
                    # finished while the loop was exiting could ghost-complete).
                    if _finalize_subtask(task, subtask, result, config):
                        logger.info("Drained future %s completed successfully", subtask.id)
                else:
                    logger.warning("Drained future %s failed: %s", subtask.id, result.get("error", "unknown"))
            except Exception as e:
                logger.warning("Drained future %s raised exception: %s", subtask.id, e)

    # Adopt declared project_path from role-handover briefs BEFORE the
    # verify gate fires. Subagents that realized work in a project root
    # set brief["project_path"]; the freshest non-empty wins. Without
    # this, verify_command (which needs a cwd) would fail and the
    # preview "See result" feature would fall back to scanning
    # task_dir/artifacts.
    _adopt_declared_project_path(task, config)

    # Build/test gate. The reconciler's existing checks are structural
    # (Stream A+B presence, brief.outcome=="success") and trust the
    # subagent's self-attestation. verify_command is the answer to "the
    # subagents claimed success but does the code actually work?".
    if task.verify_command:
        print(f"\n[VERIFY] running: {task.verify_command}")
        passed, output = _run_verify_command(task, config)
        if not passed:
            task.status = "failed"
            update_task_status(task, config.tasks_dir)
            append_log(task.id, {
                "type": "verify_failed",
                "command": task.verify_command,
                "output": output[:8000],
            }, config.tasks_dir)
            print(f"\n[VERIFY FAILED] {task.verify_command}")
            tail = output[-2000:] if len(output) > 2000 else output
            print(tail)
            # Theme E — mark exited so the launcher's respawn loop stops.
            try:
                from okuro.orchestrator.state import mark_engine_exited
                mark_engine_exited(task.id, config.tasks_dir, reason="verify_failed")
            except Exception:
                pass
            return
        append_log(task.id, {
            "type": "verify_passed",
            "command": task.verify_command,
        }, config.tasks_dir)
        print(f"[VERIFY OK] {task.verify_command} → exit 0")

    # Honest terminal status. `done` is reserved for a run where every subtask
    # actually succeeded. If any subtask failed permanently, or any was left
    # stranded behind one, the task finished but is NOT intact — say so.
    # Pre-split this line hard-wrote "done" unconditionally, so a task whose
    # later phases had been cascade-skipped still reported a clean success
    # (task-20260724-110216 shipped two hollow phases that way).
    _stranded = sorted(blocked_upstream_ids(task))
    _failed = [
        st.id for ph in task.phases for st in ph.subtasks if st.status == "failed"
    ]
    if _failed or _stranded:
        print(
            f"\n[COMPLETE — PARTIAL] failed: {', '.join(_failed) or 'none'} · "
            f"never ran (blocked upstream): {', '.join(_stranded) or 'none'}"
        )
        task.status = "completed_partial"
        append_log(task.id, {
            "type": "task_completed_partial",
            "failed_ids": _failed,
            "blocked_upstream_ids": _stranded,
        }, config.tasks_dir)
    else:
        print("\n[COMPLETE] All subtasks finished!")
        task.status = "done"
    update_task_status(task, config.tasks_dir)

    try:
        from okuro.orchestrator.capabilities.harvester import harvest_capabilities
        caps = harvest_capabilities(str(config.tasks_dir / task.id), config)
        if caps:
            append_log(task.id, {
                "type": "capabilities_harvested",
                "count": len(caps),
                "ids": [c.id for c in caps],
            }, config.tasks_dir)
            print(f"[HARVEST] Extracted {len(caps)} reusable capabilities")
    except Exception as e:
        logger.warning("Capability harvesting failed (non-blocking): %s", e)

    # Auto-generate continuation suggestions at the canonical completion
    # boundary. Previously this only fired at the very end of orchestrator_loop
    # (line ~806) — but the normal exit path breaks out of the loop from
    # _finalize_task, and any exception after that skipped suggestion
    # generation entirely. Calling here makes suggestions a first-class
    # completion side-effect that the UI can rely on.
    append_log(task.id, {
        "type": "orchestrator_state",
        "state": "generating",
        "label": "Generating 3 next steps…",
    }, config.tasks_dir)
    try:
        _generate_continuation_suggestions(task, config)
    except Exception as e:
        logger.warning("Continuation suggestion generation failed (non-blocking): %s", e)
    append_log(task.id, {
        "type": "orchestrator_state",
        "state": "idle",
        "label": "",
    }, config.tasks_dir)

    # Close the todo→task→done loop: if this task was spawned from a
    # registered todo (Now-page click), mark the todo done now that
    # execution wrapped up. Failures are non-blocking — the todo stays
    # open for manual handling.
    if task.source_todo_id:
        try:
            from okuro.sense.todos import todo_done
            todo_done(task.source_todo_id)
            print(f"[TODO] marked done: {task.source_todo_id}")
        except Exception as exc:
            logger.warning("Failed to close source todo %s: %s",
                           task.source_todo_id, exc)

    # Close the thought→task→resolved loop: same pattern for action-items
    # that were rendered inside a parent thought (Now-page Action Items
    # column). update_thought sets status='resolved' so the thought
    # drops out of the daily-digest unresolved bucket.
    if task.source_thought_id:
        try:
            from okuro.sense.thoughts import update_thought
            update_thought(task.source_thought_id, status="resolved")
            print(f"[THOUGHT] marked resolved: {task.source_thought_id}")
        except Exception as exc:
            logger.warning("Failed to resolve source thought %s: %s",
                           task.source_thought_id, exc)

    notify_channel("task_complete", task.id, task.description)

    # Theme E — write `.exited` so the launcher's respawn loop terminates.
    # Mirrors the existing panel-confirmation / capability-gap exit paths.
    try:
        from okuro.orchestrator.state import mark_engine_exited
        mark_engine_exited(task.id, config.tasks_dir, reason=f"finalize_{task.status}")
    except Exception:
        pass


def _persist_capability_gap(task, gap, phase0_subtasks: list[dict], tasks_dir: Path) -> None:
    """Write the gap + Phase 0 plan to ``capability_gap.json`` for the API.

    The orchestrator UI / API surfaces this file so the user can approve
    the Phase 0 plan before it executes. Atomic via os.replace.
    """
    target = tasks_dir / task.id / "capability_gap.json"
    payload = {
        "task_id": task.id,
        "kind": gap.kind,
        "summary": gap.summary,
        "creator_roles": gap.creator_roles,
        "slot_descriptions": gap.slot_descriptions,
        "payload": gap.payload,
        "phase0": phase0_subtasks,
        "status": "pending_approval",
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, target)


def _autopilot_accept_capability_gap(
    task, config, *, gap_kind: str, phase0: list[dict], roles, summary: str,
) -> Optional[str]:
    """F2 — try to auto-accept a capability_gap in-process.

    On a confident 'accept' from the profile: attach the Phase-0 subgraph via
    the SAME core the /capability-gap/accept endpoint uses (DP10, no
    HTTP-to-self), mark the gap json accepted (UI parity), flip the task to
    ``deliberating`` and persist — so the caller falls through into the
    deliberate while-loop and runs Phase 0 (mirrors the required_roles
    fast-path's "populate graph + fall through" pattern). Returns 'accept' when
    applied, else None (caller parks via set_awaiting exactly as today).
    """
    from okuro.orchestrator.autopilot import autopilot_gate
    roles_list = [str(r) for r in (roles or [])]
    chosen = autopilot_gate(
        config, task, config.tasks_dir,
        kind="capability_gap", options=["accept"],
        payload={
            "summary": summary,
            "phase0_count": len(phase0),
            "demanded_roles": roles_list,
            "message": (
                "This task needs new expertise "
                f"({', '.join(roles_list) or 'some specialists'}): {summary}"
            ),
        },
        ctx={"phase_id": 0},
    )
    if chosen != "accept":
        return None
    from okuro.orchestrator.deliberation import attach_capability_gap_phase0
    from okuro.orchestrator.state import (
        save_plan, save_task_meta, DAGGraph, Deliberation,
    )
    if not task.graph:
        task.graph = DAGGraph()
    if not task.deliberation:
        task.deliberation = Deliberation()
    attach_capability_gap_phase0(task.graph, gap_kind, phase0)
    try:
        gap_path = config.tasks_dir / task.id / "capability_gap.json"
        if gap_path.exists():
            data = json.loads(gap_path.read_text())
            data["status"] = "accepted"
            gap_path.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        logger.warning("autopilot capability_gap json mark failed: %r", exc)
    task.status = "deliberating"
    save_plan(task, config.tasks_dir)
    save_task_meta(task, config.tasks_dir)
    print("[AUTOPILOT] capability_gap accepted — Phase 0 injected in-process")
    return "accept"


def _autopilot_confirm_panel(task, config, panel_payload: list[dict]) -> Optional[str]:
    """F2 — try to auto-confirm the proposed expert panel in-process.

    On a confident 'confirm': create the round-1 position + discussion nodes via
    the SAME core the /panel endpoint uses (create_position_nodes, DP10), flip
    to ``deliberating`` and persist so the caller falls through into the
    deliberate while-loop. 'edit' (or abstain) returns None → caller parks (a
    human edit cannot be synthesized). Returns 'confirm' when applied.
    """
    from okuro.orchestrator.autopilot import autopilot_gate
    roles = [p.get("role_id", "") for p in panel_payload if p.get("role_id")]
    if not roles:
        return None
    chosen = autopilot_gate(
        config, task, config.tasks_dir,
        kind="panel_confirmation", options=["confirm", "edit"],
        payload={
            "proposed_roles": roles,
            "message": (
                f"Confirm the {len(roles)}-role expert panel "
                f"({', '.join(roles)}) or edit it before work begins."
            ),
        },
        ctx={"phase_id": 0},
    )
    if chosen != "confirm":
        return None
    from okuro.orchestrator.deliberation import create_position_nodes
    from okuro.orchestrator.state import (
        save_plan, save_task_meta, DAGGraph, Deliberation,
    )
    if not task.graph:
        task.graph = DAGGraph()
    if not task.deliberation:
        task.deliberation = Deliberation()
    task.deliberation.strategy = "parallel"
    task.deliberation.panel = list(roles)
    create_position_nodes(task.graph, roles=roles, round_num=1)
    task.status = "deliberating"
    save_plan(task, config.tasks_dir)
    save_task_meta(task, config.tasks_dir)
    print(f"[AUTOPILOT] panel confirmed in-process ({len(roles)} roles)")
    return "confirm"


def _autopilot_proceed_discussion(task, config, node) -> Optional[str]:
    """F2 — try to auto-proceed a council discussion in-process.

    On a confident 'proceed', resolve the discussion via the SAME core the
    /discussion/{id}/proceed endpoint uses — default assignments (every done
    position role as ``acknowledge``) + resolve_discussion (DP10) — so the
    deliberate loop's auto-decompose picks up the resolved node with no park.
    Returns None (caller parks) when the resolver abstains, when any input
    position is still non-terminal, or when there is no done position to draw
    authority from (mirrors the endpoint's 409/400 guards). 'proceed' when
    applied.
    """
    from okuro.orchestrator.autopilot import autopilot_gate
    chosen = autopilot_gate(
        config, task, config.tasks_dir,
        kind="discussion_proceed", options=["proceed"],
        payload={
            "discussion_id": node.id,
            "round": int(getattr(node, "round", 1) or 1),
            "message": (
                "The expert panel is ready — each expert shared a position. "
                "Proceed to continue."
            ),
        },
        ctx={"subtask_id": node.id},
    )
    if chosen != "proceed":
        return None
    terminal = {"done", "skipped", "dismissed"}
    done_roles: list[str] = []
    pending: list[str] = []
    for pred_id in task.graph.get_predecessors(node.id):
        pred = task.graph.get_node(pred_id)
        if pred is None or pred.type != "position":
            continue
        if pred.status not in terminal:
            pending.append(pred_id)
        elif pred.status == "done" and pred.role:
            done_roles.append(pred.role)
    if pending or not done_roles:
        # Not all positions terminal, or no authority signal — park (the
        # endpoint would 409/400 here too).
        return None
    assignments_raw = [
        {"role": r, "action": "acknowledge", "leads": "", "reasoning": ""}
        for r in done_roles
    ]
    from okuro.orchestrator.deliberation import resolve_discussion
    resolve_discussion(
        task, node.id, assignments_raw,
        getattr(node, "user_statement", "") or "", config.tasks_dir,
    )
    print(
        f"[AUTOPILOT] discussion {node.id} proceeded in-process "
        f"({len(done_roles)} position(s))"
    )
    return "proceed"


def _autopilot_resolve_timeout_cap(
    task, config, subtask, timeout_budget: int, raw_err: str,
) -> Optional[str]:
    """F2 — try to auto-resolve a timeout-cap in-process.

    Applies the SAME action the /timeout-cap-resolve endpoint performs (DP10):
      * extend_and_retry — stage a one-shot wall-clock override sidecar +
        reset the subtask to pending/retries=0 so the C5 guard releases it.
      * mark_done        — flip to done.
      * skip             — flip to skipped (a SATISFIED status: dependents
        still run, matching the pre-split behaviour of an explicit skip).
      * permanent_fail   — flip to failed. Dependents are left `pending` and
        become derivably blocked (state.blocked_upstream_ids); they revive
        automatically if this subtask is later retried green.
    Returns the chosen action when applied, else None (caller parks).
    """
    from okuro.orchestrator.autopilot import autopilot_gate
    options = ["extend_and_retry", "mark_done", "skip", "permanent_fail"]
    attempts = config.execution.max_retries + 1
    chosen = autopilot_gate(
        config, task, config.tasks_dir,
        kind="timeout_cap", options=options,
        payload={
            "subtask_id": subtask.id,
            "role": subtask.role,
            "retries": subtask.retries,
            "last_timeout_s": int(timeout_budget),
            "last_error": raw_err[:500],
            "message": (
                f"{subtask.role} ({subtask.id}) timed out {attempts}× at "
                f"{timeout_budget}s. Extend & retry, mark done, skip, or "
                "permanent-fail?"
            ),
        },
        ctx={"subtask_id": subtask.id, "phase_id": getattr(subtask, "phase_id", None)},
    )
    if chosen not in options:
        return None
    now_iso = datetime.utcnow().isoformat()
    if chosen == "extend_and_retry":
        try:
            from okuro.orchestrator.dispatcher import (
                _PER_ROLE_TIMEOUT_BASE as _BASE,
                _DEFAULT_TIMEOUT_BASE as _DEF,
                _RETRY_TIMEOUT_BUMP_CAP as _CAP,
            )
            new_timeout = int(_BASE.get(subtask.role or "", _DEF) + 2 * _CAP)
        except Exception:
            new_timeout = max(int(timeout_budget) * 2, 1800)
        try:
            sidecar = config.tasks_dir / task.id / ".timeout-overrides.json"
            existing: dict = {}
            if sidecar.exists():
                existing = json.loads(sidecar.read_text(encoding="utf-8") or "{}")
                if not isinstance(existing, dict):
                    existing = {}
            existing[subtask.id] = new_timeout
            sidecar.write_text(json.dumps(existing), encoding="utf-8")
        except Exception as exc:
            logger.warning("autopilot timeout extend sidecar failed: %r", exc)
            return None
        update_subtask(task.id, subtask.id, {
            "status": "pending", "retries": 0, "error": "",
            "started_at": "", "completed_at": "",
        }, config.tasks_dir)
    elif chosen == "mark_done":
        update_subtask(task.id, subtask.id, {
            "status": "done", "completed_at": now_iso, "error": "",
        }, config.tasks_dir)
    elif chosen == "skip":
        update_subtask(task.id, subtask.id, {
            "status": "skipped", "completed_at": now_iso,
            "error": "autopilot skipped via timeout_cap",
        }, config.tasks_dir)
    else:  # permanent_fail
        update_subtask(task.id, subtask.id, {
            "status": "failed", "completed_at": now_iso,
            "error": "autopilot permanent_fail via timeout_cap",
        }, config.tasks_dir)
    print(f"[AUTOPILOT] timeout_cap {subtask.id} → {chosen} in-process")
    return chosen


def _check_phase0_completion(task, tasks_dir: Path) -> bool:
    """Return True iff Phase 0 just resolved a capability gap.

    Reads ``capability_gap.json``; if the user previously accepted the
    gap (status='accepted') and every Phase 0 execution node now lives
    in a terminal-success state, flips gap status to ``phase0_complete``
    and persists. The caller breaks out of the deliberation loop so the
    process exits cleanly — the next GET /proposed-panel re-runs match
    against the now-updated role registry.

    Idempotent: returns False once status is already ``phase0_complete``.
    """
    gap_path = tasks_dir / task.id / "capability_gap.json"
    if not gap_path.exists():
        return False
    try:
        gap = json.loads(gap_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if gap.get("status") != "accepted":
        return False

    phase0_ids = {p.get("id") for p in gap.get("phase0", []) if p.get("id")}
    if not phase0_ids or not task.graph:
        return False

    terminal_success = {"done", "completed", "succeeded"}
    for node in task.graph.nodes:
        if node.id in phase0_ids and node.status not in terminal_success:
            return False

    gap["status"] = "phase0_complete"
    tmp = gap_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(gap, indent=2))
    os.replace(tmp, gap_path)

    # PR A — emit role_drafted for any roles the designer subagent
    # registered with maturity='draft' during phase 0. The FE listens for
    # this event in _STATE_CHANGE_EVENTS and refetches /api/roles?
    # maturity=draft so the user can promote each draft. Without this
    # the only signal is the closing of the phase, which doesn't tell the
    # FE which roles landed.
    try:
        from okuro.db import get_db
        db = get_db()
        rows = db.fetchall(
            "SELECT role_id, domain, description FROM roles "
            "WHERE maturity = 'draft'"
        ) or []
        for row in rows:
            try:
                append_log(task.id, {
                    "type": "role_drafted",
                    "role_id": row["role_id"],
                    "domain": row["domain"],
                    "description": (row["description"] or "")[:300],
                    "source": "designer",
                }, tasks_dir)
            except Exception as exc:
                logger.warning("role_drafted emit failed: %r", exc)
    except Exception as exc:
        logger.warning("role_drafted DB scan failed: %r", exc)

    return True


def _emit_capability_gap_event(task, gap) -> None:
    """Append a blocking ``open_question`` event flagging the gap.

    Uses the existing M2 ``task_events`` log so downstream consumers
    (compressor, reviewer, UI) discover the gap through the same
    append-only surface used by every other deliberation signal.
    """
    try:
        from okuro.sense.task_events import append_event
        append_event(
            task_id=task.id,
            subtask_id=f"capability-gap-{gap.kind}",
            from_role="orchestrator",
            event_type="open_question",
            body={
                "question": gap.summary,
                "blocking": True,
            },
            created_by="capability_gap.escalate",
        )
    except Exception as exc:
        logger.warning("capability_gap event emit failed: %s", exc)


def deliberation_loop(task: Task, config: Config, args):
    """DAG-based orchestration loop for deliberation-mode tasks."""
    global shutdown_requested

    # C4 — clear any sentinel left over from a crashed predecessor (UI3).
    # deliberation_loop is reached via orchestrator_loop AND directly via
    # resume paths; the call is idempotent and cheap at both entry points.
    try:
        # P4.3 — reap BEFORE revive. Reconcile resets orphaned subtasks to
        # pending so this engine can dispatch them; doing that while a
        # predecessor's subagent is still writing is how one subtask ends up
        # with two live sessions.
        reap_predecessor_sessions(task.id, config.tasks_dir)
        reconcile_on_startup(task.id, config.tasks_dir)
    except Exception as exc:
        logger.warning("[%s] reconcile_on_startup raised: %r", task.id, exc)

    # PR C — enforce engine lease for deliberate-mode resume paths too
    # (some entry routes skip orchestrator_loop and land here directly).
    # The check is idempotent when orchestrator_loop already ran it.
    try:
        _enforce_engine_lease(task, config.tasks_dir)
    except SystemExit:
        raise
    except Exception as exc:
        logger.warning("[%s] engine lease check raised: %r", task.id, exc)

    # PR A — heartbeat for deliberate-mode entry. Idempotent: if
    # orchestrator_loop already started it, this is a no-op.
    try:
        start_orchestrator_heartbeat(task.id, config.tasks_dir)
    except Exception as exc:
        logger.warning("[%s] heartbeat start raised: %r", task.id, exc)

    from okuro.orchestrator.deliberation import (
        get_ready_nodes, is_graph_complete, is_graph_blocked,
        advance_discussion_state, build_execution_brief,
        propose_panel, create_position_nodes, create_execution_nodes,
    )
    from okuro.orchestrator.state import update_node, save_plan, save_task_meta, set_awaiting

    if not task.graph or not task.graph.nodes:
        # NAMED-ROLE DEMAND GAP (priority over panel match / required_roles).
        # When the user explicitly demands roles the registry cannot fill
        # (e.g. "use a legal advisor", "a media specialist"), offer to create
        # them BEFORE proceeding — even if OTHER roles match the task. The
        # whole-task zero-match detector below cannot catch this, because the
        # task still matches some existing role. Guarded by capability_gap.json:
        # runs only on first entry (no gap file yet), so accepted/declined/
        # completed gaps never re-trigger and the created roles match normally.
        _gap_file = config.tasks_dir / task.id / "capability_gap.json"
        if not _gap_file.exists():
            from okuro.orchestrator.capability_gap import (
                detect_named_role_gaps,
                build_phase0_subtasks,
            )
            try:
                from okuro.orchestrator.config import load_role_index
                from okuro.orchestrator.decomposer import format_role_index
                _role_index_text = format_role_index(load_role_index())
            except Exception:
                _role_index_text = ""
            named_gap = detect_named_role_gaps(
                task.description, role_index_text=_role_index_text
            )
            if named_gap is not None:
                phase0 = build_phase0_subtasks(named_gap)
                _persist_capability_gap(task, named_gap, phase0, config.tasks_dir)
                _emit_capability_gap_event(task, named_gap)
                print(
                    f"\n[DELIBERATE] NAMED-ROLE GAP: "
                    f"{', '.join(named_gap.slot_descriptions)} — "
                    f"{len(phase0)} Phase 0 step(s) proposed."
                )
                from okuro.orchestrator.gate_messages import humanize_gate as _hg
                _gm = _hg("capability_gap", "", {
                    "phase_id": 0,
                    "roles": named_gap.slot_descriptions,
                    "summary": named_gap.summary,
                    "options": ["accept"],
                })
                # F2 autopilot — auto-accept in-process + fall through to the
                # deliberate loop; else park exactly as today.
                if _autopilot_accept_capability_gap(
                    task, config, gap_kind=named_gap.kind, phase0=phase0,
                    roles=named_gap.slot_descriptions, summary=named_gap.summary,
                ) != "accept":
                    set_awaiting(
                        task, config.tasks_dir,
                        kind="capability_gap",
                        message=_gm.to_message(),
                        endpoint=f"/api/tasks/{task.id}/capability-gap/accept",
                        payload={
                            "summary": named_gap.summary,
                            "phase0_count": len(phase0),
                            "creator_roles": [p.get("role", "") for p in phase0],
                            "demanded_roles": named_gap.slot_descriptions,
                            "presentation": _gm.to_presentation(),
                        },
                    )
                    _emit_orchestrator_state(
                        task, state="idle", label="",
                        tasks_dir=config.tasks_dir,
                    )
                    try:
                        from okuro.orchestrator.state import mark_engine_exited
                        mark_engine_exited(
                            task.id, config.tasks_dir,
                            reason="capability_gap_return",
                        )
                    except Exception:
                        pass
                    return

        # FAST-PATH: if user supplied valid required_roles, build the panel
        # directly without waiting for external panel confirmation. This honors
        # the "MUST include" contract of orchestrator_create.required_roles and
        # unblocks tasks where semantic role-match returns nothing. Validation
        # at create-time guarantees every id is real, but we re-check defensively.
        if task.required_roles:
            from okuro.roles.registry import get_role
            invalid = [r for r in task.required_roles if get_role(r) is None]
            if invalid:
                print(f"[DELIBERATE] required_roles contains unknown ids: {invalid}")
                print(f"[DELIBERATE] falling back to semantic role match.")
            else:
                print(f"[DELIBERATE] honoring required_roles directly ({len(task.required_roles)} roles): {task.required_roles}")
                from okuro.orchestrator.state import DAGGraph, Deliberation, save_plan
                if not task.graph:
                    task.graph = DAGGraph()
                if not task.deliberation:
                    task.deliberation = Deliberation()
                task.deliberation.strategy = "default"
                task.deliberation.panel = list(task.required_roles)
                create_position_nodes(task.graph, roles=task.required_roles, round_num=1)
                task.status = "deliberating"
                save_plan(task, config.tasks_dir)
                save_task_meta(task, config.tasks_dir)
                print(f"[DELIBERATE] panel auto-confirmed; deliberation graph created.")
                # fall through into the while-loop below so position nodes start running

        if not task.graph or not task.graph.nodes:
            print("\n[DELIBERATE] No panel confirmed yet.")
            print(f"[DELIBERATE] Proposing roles for: {task.description[:100]}")

            suggestions = propose_panel(task.description)
            if suggestions:
                print(f"[DELIBERATE] Suggested panel ({len(suggestions)} roles):")
                for s in suggestions:
                    print(
                        f"  - {s.get('role_id', '?')} "
                        f"[{s.get('match_type', '?')}, sim={s.get('similarity', 0.0):.3f}]: "
                        f"{s.get('why', '')[:60]}"
                    )
            else:
                print("[DELIBERATE] No role suggestions returned by resolver.")

            # CAPABILITY-GAP ESCALATION (S3/S4).
            # When no role crosses the similarity threshold the engine must
            # not silently stall — emit a blocking open_question event and
            # persist a Phase 0 plan that the API/UI can surface for user
            # approval. The engine itself stays kind-agnostic — only
            # capability_gap knows about missing_role / missing_tool / etc.
            from okuro.orchestrator.capability_gap import (
                detect_capability_gap_from_panel,
                build_phase0_subtasks,
                PANEL_QUALITY_FLOOR,
            )
            # P-ROLE-1 — apply the quality floor so a weak-but-above-0.40 panel
            # (off-target roles) offers the create-role card instead of seeding
            # a poor deliberation. User can still decline and proceed.
            gap = detect_capability_gap_from_panel(
                suggestions, task_description=task.description,
                quality_floor=PANEL_QUALITY_FLOOR,
                llm_gate=True,
            )
            if gap is not None:
                phase0 = build_phase0_subtasks(gap)
                _persist_capability_gap(task, gap, phase0, config.tasks_dir)
                _emit_capability_gap_event(task, gap)
                print(
                    f"\n[DELIBERATE] CAPABILITY GAP: kind={gap.kind} — "
                    f"{len(phase0)} Phase 0 step(s) proposed."
                )
                print(f"[DELIBERATE] Approve / decline via API:")
                print(f"  GET  /api/tasks/{task.id}/capability-gap")
                print(f"  POST /api/tasks/{task.id}/capability-gap/accept")
                from okuro.orchestrator.gate_messages import humanize_gate as _hg
                _gm = _hg("capability_gap", "", {
                    "phase_id": 0,
                    "count": len(phase0),
                    "summary": gap.summary,
                    "options": ["accept"],
                })
                # F2 autopilot — auto-accept in-process + fall through to the
                # deliberate loop (the `else` panel path is skipped because gap
                # was not None); else park exactly as today.
                if _autopilot_accept_capability_gap(
                    task, config, gap_kind=gap.kind, phase0=phase0,
                    roles=getattr(gap, "slot_descriptions", None) or gap.creator_roles,
                    summary=gap.summary,
                ) != "accept":
                    set_awaiting(
                        task, config.tasks_dir,
                        kind="capability_gap",
                        message=_gm.to_message(),
                        endpoint=f"/api/tasks/{task.id}/capability-gap/accept",
                        payload={
                            "summary": gap.summary,
                            "phase0_count": len(phase0),
                            "creator_roles": [p.get("role", "") for p in phase0],
                            "presentation": _gm.to_presentation(),
                        },
                    )
                    _emit_orchestrator_state(
                        task, state="idle", label="",
                        tasks_dir=config.tasks_dir,
                    )
                    # capability_gap path returns from the deliberate loop —
                    # mark exit so liveness check honors it over the spawn-
                    # grace window.
                    try:
                        from okuro.orchestrator.state import mark_engine_exited
                        mark_engine_exited(task.id, config.tasks_dir, reason="capability_gap_return")
                    except Exception:
                        pass
                    return

            else:
                print(f"\n[DELIBERATE] Waiting for panel confirmation via API:")
                print(f"  POST /api/tasks/{task.id}/panel")
                print(f"  GET  /api/tasks/{task.id}/proposed-panel")
                panel_payload = [
                    {
                        "role_id": s.get("role_id", ""),
                        "similarity": float(s.get("similarity", 0.0)),
                        "match_type": s.get("match_type", ""),
                        "why": (s.get("why", "") or "")[:200],
                    }
                    for s in (suggestions or [])
                ]
                # Quality signals — UI surfaces a weak-match warning and shows
                # explicit role-name mentions found in the task body. Fixes the
                # "musician + system-documenter for an audio-engineering task"
                # class of bad picks: when the description literally names a role,
                # the user can add it with one click instead of typing it out.
                top_sim = max((p["similarity"] for p in panel_payload), default=0.0)
                quality_weak = top_sim < 0.20
                mentioned_roles: list[str] = []
                try:
                    from okuro.roles.registry import list_roles as _list_roles
                    desc_low = (task.description or "").lower()
                    proposed_ids = {p["role_id"] for p in panel_payload}
                    for r in _list_roles():
                        rid = r.get("id", "") if isinstance(r, dict) else getattr(r, "id", "")
                        if not rid or rid in proposed_ids:
                            continue
                        if rid.lower() in desc_low:
                            mentioned_roles.append(rid)
                        if len(mentioned_roles) >= 5:
                            break
                except Exception as exc:
                    logger.warning("mentioned-role detection failed: %r", exc)
                from okuro.orchestrator.gate_messages import humanize_gate as _hg
                _gm = _hg("panel_confirmation", "", {
                    "count": len(panel_payload),
                    "options": ["confirm", "edit"],
                })
                # F2 autopilot — auto-confirm the panel in-process + fall through;
                # else park exactly as today ('edit' cannot be synthesized).
                if _autopilot_confirm_panel(task, config, panel_payload) != "confirm":
                    set_awaiting(
                        task, config.tasks_dir,
                        kind="panel_confirmation",
                        message=_gm.to_message(),
                        endpoint=f"/api/tasks/{task.id}/panel",
                        payload={
                            "proposed": panel_payload,
                            "presentation": _gm.to_presentation(),
                            "strategy_default": "parallel",
                            "quality_weak": quality_weak,
                            "top_similarity": top_sim,
                            "mentioned_roles": mentioned_roles,
                        },
                    )
                    _emit_orchestrator_state(
                        task, state="idle", label="",
                        tasks_dir=config.tasks_dir,
                    )
                    # panel_confirmation path returns from the deliberate loop —
                    # mark exit so the API's next _ensure_engine_running call
                    # honors clean exit over the spawn-grace window.
                    try:
                        from okuro.orchestrator.state import mark_engine_exited
                        mark_engine_exited(task.id, config.tasks_dir, reason="panel_confirmation_return")
                    except Exception:
                        pass
                    return

    cli_short = {"claude": "cc", "antigravity": "agy", "codex": "cdx"}.get(config.cli_default, config.cli_default[:3])
    brain_session_id = _brain_session_start(task.id, task.description, cli_short=cli_short)

    max_parallel = config.execution.max_parallel
    pid_path = config.tasks_dir / task.id / ".orchestrator.pid"
    heartbeat_path = config.tasks_dir / task.id / ".heartbeat"
    pid_path.write_text(str(os.getpid()))

    try:
        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            active_futures = {}

            while not shutdown_requested:
                # P4.5 — a source change was seen by the watcher thread. Exit
                # from the MAIN thread, between dispatches, so the launcher
                # respawns against fresh modules. No mark_engine_exited: the
                # absence of `.exited` is what tells the launcher to respawn.
                if restart_requested:
                    logger.warning(
                        "[%s] restarting for fresh source (deliberation loop)",
                        task.id,
                    )
                    return
                heartbeat_path.write_text(datetime.utcnow().isoformat())
                task = load_task(task.id, config.tasks_dir)

                # Park = engineless (deliberate mode). When the loop has set a
                # HUMAN-blocking awaiting (discussion_proceed, blocked_review,
                # decision_gate, …) and nothing is in-flight, EXIT cleanly
                # instead of looping forever on a 2s sleep. The resolution
                # endpoint (/discussion/{id}/proceed, /override-blocked-review,
                # …) calls _ensure_engine_running to respawn. A transient wait
                # (futures still running) is NOT a human park — keep going.
                if not active_futures and _is_human_park(task):
                    _park_on_human_wait(task, config)
                    raise SystemExit(0)

                if not task.graph:
                    print("[DELIBERATE] Graph disappeared — halting.")
                    task.status = "failed"
                    save_task_meta(task, config.tasks_dir)
                    break

                for node in task.graph.nodes:
                    if node.type == "discussion" and node.status in ("proposed", "positions_running"):
                        new_status = advance_discussion_state(task.graph, node.id)
                        if new_status:
                            update_node(task.id, node.id, {"status": new_status}, config.tasks_dir)
                            if new_status == "awaiting_user":
                                print(f"\n[AWAITING USER] Discussion {node.id} — all positions ready")
                                print(f"  Resolve via: POST /api/tasks/{task.id}/discussion/{node.id}/proceed")
                                try:
                                    # F2 autopilot — auto-proceed in-process;
                                    # else park exactly as today.
                                    if _autopilot_proceed_discussion(task, config, node) != "proceed":
                                        position_ids = task.graph.get_predecessors(node.id)
                                        from okuro.orchestrator.gate_messages import (
                                            humanize_gate as _hg_disc,
                                        )
                                        _gm = _hg_disc(
                                            "discussion_proceed", "",
                                            {"discussion_id": node.id,
                                             "position_ids": position_ids},
                                        )
                                        set_awaiting(
                                            task, config.tasks_dir,
                                            kind="discussion_proceed",
                                            message=_gm.to_message(),
                                            endpoint=f"/api/tasks/{task.id}/discussion/{node.id}/proceed",
                                            payload={
                                                "discussion_id": node.id,
                                                "position_ids": position_ids,
                                                "round": int(getattr(node, "round", 1) or 1),
                                                "presentation": _gm.to_presentation(),
                                            },
                                        )
                                        _emit_orchestrator_state(
                                            task, state="idle", label="",
                                            tasks_dir=config.tasks_dir,
                                        )
                                except Exception as exc:
                                    logger.warning("discussion set_awaiting failed: %r", exc)

                # AUTO-DECOMPOSE EXECUTION AFTER COUNCIL.
                # When a discussion resolves the deliberation has produced an
                # execution brief — but historically the loop went straight to
                # is_graph_complete and marked the task done with zero phases.
                # Detect any resolved discussion that has no execution children
                # yet, build the brief, decompose into phases, and inject the
                # subtasks as execution nodes downstream of that discussion.
                for node in task.graph.nodes:
                    if node.type != "discussion" or node.status != "resolved":
                        continue
                    successor_ids = task.graph.get_successors(node.id)
                    has_execution_children = any(
                        (s := task.graph.get_node(sid)) and s.type == "execution"
                        for sid in successor_ids
                    )
                    if has_execution_children:
                        continue
                    brief = build_execution_brief(task.graph, node.id)
                    if not brief:
                        continue
                    # A continuation council carries the follow-up instructions
                    # on the discussion node's `description` (set at seed time).
                    # When present AND the task already has phases, this is a
                    # LATER round on an existing plan: decompose via the
                    # continuation path (renumbers from max+1, appends) instead
                    # of decompose_task (which numbers from 1 and would collide
                    # with / replace the original phases).
                    _cont_directive = (node.description or "").strip()
                    _is_cont_council = bool(_cont_directive) and bool(task.phases)
                    full_brief = f"{task.description}\n\n{brief}"
                    print(f"\n[DECOMPOSE] discussion {node.id} resolved — generating execution plan...", flush=True)
                    # Emit a log event BEFORE the synchronous LLM call so the
                    # dashboard can render a "decomposing..." banner. Without
                    # this the user sees zero feedback between Proceed and the
                    # phases appearing (which can take several minutes).
                    append_log(task.id, {
                        "type": "decompose_started",
                        "discussion": node.id,
                    }, config.tasks_dir)
                    # Surface live status to InlineThinker — without this the
                    # user sees zero indication that an LLM call is in flight
                    # for 30s–5min after Proceed.
                    append_log(task.id, {
                        "type": "orchestrator_state",
                        "state": "generating",
                        "label": "Decomposing council into execution steps…",
                    }, config.tasks_dir)
                    try:
                        if _is_cont_council:
                            # Council outcome (authority map + user direction)
                            # feeds the continuation planner alongside the new
                            # instructions so the plan reflects the debate.
                            phases = decompose_continuation(
                                f"{_cont_directive}\n\n{brief}", task, config,
                            )
                        else:
                            phases = decompose_task(
                                full_brief, config,
                                required_roles=task.required_roles or None,
                                intelligence=task.intelligence,
                                task_id=task.id,
                            )
                    except Exception as exc:
                        print(f"[DECOMPOSE] failed: {exc}", flush=True)
                        append_log(task.id, {
                            "type": "decompose_failed",
                            "discussion": node.id,
                            "error": str(exc),
                        }, config.tasks_dir)
                        append_log(task.id, {
                            "type": "orchestrator_state",
                            "state": "error",
                            "label": f"Decompose failed: {str(exc)[:80]}",
                        }, config.tasks_dir)
                        continue
                    subtasks_for_dag = []
                    for phase in phases:
                        for st in phase.subtasks:
                            subtasks_for_dag.append({
                                "id": st.id,
                                "role": st.role,
                                "description": st.description,
                                "risk": st.risk,
                                "complexity": st.complexity,
                                "artifact_name": st.artifact_name,
                                "dependencies": st.dependencies,
                            })
                    create_execution_nodes(task.graph, node.id, subtasks_for_dag)
                    if _is_cont_council:
                        # Append — preserve the original plan's phases.
                        task.phases.extend(phases)
                    else:
                        task.phases = phases
                    save_plan(task, config.tasks_dir)
                    print(f"[DECOMPOSE] {len(subtasks_for_dag)} execution nodes added downstream of {node.id}")

                # R4 — Phase 0 completion: when role-researcher +
                # role-designer have persisted new role drafts, exit the
                # deliberation loop so the orchestrator process ends.
                # The user re-fetches /proposed-panel, sees the now-
                # matched roles, and POSTs /panel as usual. We MUST run
                # this check before is_graph_complete, otherwise a fully
                # done Phase 0 sub-DAG would mark the task done with no
                # actual deliberation having happened.
                if _check_phase0_completion(task, config.tasks_dir):
                    task.status = "deliberating"
                    save_task_meta(task, config.tasks_dir)
                    print(
                        "\n[DELIBERATE] Phase 0 complete — role registry "
                        "updated. Awaiting fresh panel confirmation via "
                        f"GET /api/tasks/{task.id}/proposed-panel."
                    )
                    _brain_session_end(brain_session_id, task=task)
                    break

                # Continue-vs-resume race recovery (DELIBERATE mode). The
                # phase loop drains pending continuations before its
                # completion check; this loop historically did NOT, so a
                # --resume respawn that inherited an all-done DAG fell
                # straight through to is_graph_complete, marked the task
                # `done`, and stranded the queued continuation forever
                # (the daemon reconciler never resumes `done`). Mirror the
                # phase-loop guard here so deliberate-mode continuations
                # materialize instead of vanishing. Idempotent + no-op when
                # empty; append_phases extends the DAG for deliberate tasks.
                if not active_futures and _drain_pending_continuations(task, config):
                    task = load_task(task.id, config.tasks_dir)
                    continue

                if is_graph_complete(task.graph):
                    task.status = "done"
                    save_task_meta(task, config.tasks_dir)
                    print("\n[COMPLETE] All DAG nodes resolved.")
                    # Clear the busy pill — terminal. Without an explicit
                    # frame the InlineThinker stuck on the last running label.
                    _emit_orchestrator_state(
                        task, state="complete", label="", tasks_dir=config.tasks_dir,
                    )
                    _brain_session_end(brain_session_id, task=task)
                    break

                # Gate 2 §C5 — pass the task + retries cap so awaiting /
                # halted / phase.blocked_review / retries-cap halts are
                # enforced in one composite guard (deliberation.is_dispatchable).
                ready = get_ready_nodes(
                    task.graph, task,
                    max_retries=getattr(getattr(config, "execution", None), "max_retries", DEFAULT_MAX_RETRIES),
                )
                running_ids = {n.id for n in active_futures.values()}
                ready = [n for n in ready if n.id not in running_ids]

                if not ready and not active_futures:
                    if is_graph_blocked(task.graph):
                        # C6 — a deliberate-mode DAG deadlock previously set a
                        # bare status="blocked" with NO awaiting surface: the
                        # daemon won't auto-resume `blocked` and the UI had no
                        # actionable CTA (silent dead-end). Surface it via
                        # set_awaiting (mirrors the auto-execute fallback) so the
                        # user SEES it needs them and can override/abort — honest
                        # about needing intervention.
                        print("\n[BLOCKED] DAG is blocked — no nodes ready or waiting.")
                        from okuro.orchestrator.gate_messages import humanize_gate as _hg
                        _gm = _hg("blocked_review", "stuck", {
                            "phase_id": "?",
                            "raw_reason": ("deliberate-mode DAG blocked — no graph "
                                           "nodes ready or running (unmet dependency "
                                           "or all remaining nodes failed)"),
                        })
                        blocked_reason = _gm.to_message()
                        try:
                            from okuro.orchestrator.state import set_awaiting as _set_awaiting
                            _set_awaiting(
                                task, config.tasks_dir,
                                kind="blocked_review",
                                message=blocked_reason,
                                endpoint=f"/api/tasks/{task.id}/override-blocked-review",
                                payload={"reason": "dag_blocked",
                                         "presentation": _gm.to_presentation()},
                            )
                        except Exception as exc:
                            logger.warning(
                                "deliberate blocked set_awaiting failed: %r", exc,
                            )
                            task.status = "blocked"
                            save_task_meta(task, config.tasks_dir)
                        # Parked for the user — drop the busy pill so it
                        # doesn't read "running" while we wait on a human.
                        _emit_orchestrator_state(
                            task, state="idle", label="", tasks_dir=config.tasks_dir,
                        )
                        # Park = engineless: write .exited so the launcher
                        # does NOT respawn this parked engine; the user's
                        # /override-blocked-review respawns a fresh one.
                        park_engine_exit(
                            task.id, config.tasks_dir, reason="park:blocked_review",
                        )
                        break
                    else:
                        # Waiting on in-flight deps / futures with nothing new
                        # to dispatch — emit idle ONCE (dedupe-guarded) so the
                        # pill reflects "waiting", not a stale "running", then
                        # park. The next dispatch re-lights it with a label.
                        _emit_orchestrator_state(
                            task, state="idle", label="", tasks_dir=config.tasks_dir,
                        )
                        time.sleep(2)
                        continue

                # M1 strict — shared helper. See _pause_on_pending_gate.
                if not active_futures and _pause_on_pending_gate(task, config):
                    task = load_task(task.id, config.tasks_dir)
                    continue

                slots = max_parallel - len(active_futures)
                for node in ready[:slots]:
                    if node.type == "position":
                        print(f"\n[POSITION] {node.id} | {node.role} | dispatching position...")
                        update_node(task.id, node.id, {
                            "status": "running", "started_at": datetime.utcnow().isoformat(),
                        }, config.tasks_dir)
                        append_log(task.id, {
                            "type": "orchestrator_state",
                            "state": "running",
                            "label": f"Position · {node.role} ({node.id})",
                        }, config.tasks_dir)
                        future = executor.submit(dispatch_position, node, task, config, session_id=brain_session_id)
                        active_futures[future] = node

                    elif node.type == "discussion":
                        update_node(task.id, node.id, {"status": "awaiting_user"}, config.tasks_dir)
                        print(f"\n[AWAITING USER] Discussion {node.id}")
                        try:
                            # F2 autopilot — auto-proceed in-process; else park.
                            if _autopilot_proceed_discussion(task, config, node) != "proceed":
                                position_ids = task.graph.get_predecessors(node.id)
                                from okuro.orchestrator.gate_messages import (
                                    humanize_gate as _hg_disc,
                                )
                                _gm = _hg_disc(
                                    "discussion_proceed", "",
                                    {"discussion_id": node.id,
                                     "position_ids": position_ids},
                                )
                                set_awaiting(
                                    task, config.tasks_dir,
                                    kind="discussion_proceed",
                                    message=_gm.to_message(),
                                    endpoint=f"/api/tasks/{task.id}/discussion/{node.id}/proceed",
                                    payload={
                                        "discussion_id": node.id,
                                        "position_ids": position_ids,
                                        "round": int(getattr(node, "round", 1) or 1),
                                        "presentation": _gm.to_presentation(),
                                    },
                                )
                                # Pulse view tracks the last orchestrator_state event;
                                # without an explicit idle the OkuroThinker pill stays
                                # stuck on the last "Position · …" label forever.
                                _emit_orchestrator_state(
                                    task, state="idle", label="",
                                    tasks_dir=config.tasks_dir,
                                )
                        except Exception as exc:
                            logger.warning("discussion(auto) set_awaiting failed: %r", exc)

                    elif node.type == "execution":
                        # First execution dispatch in deliberate mode flips
                        # task.status: positions phase is over, the user has
                        # proceeded, decomposer ran, real work is starting.
                        # Without this the badge stays DELIBERATING forever.
                        if task.status in ("deliberating", "waiting_user"):
                            task.status = "active"
                            save_task_meta(task, config.tasks_dir)
                        brief = build_execution_brief(task.graph, node.authority_from) if node.authority_from else ""
                        # Carry plan.yaml subtask state into the dispatch
                        # struct so the dispatcher's per-role timeout
                        # bump (_wall_clock_seconds in dispatcher.py:99)
                        # sees the real retry count. Pre-fix this
                        # constructor omitted `retries`, so it defaulted
                        # to 0 on every dispatch — the +300 s/retry
                        # budget bump never engaged. Live evidence:
                        # task-20260527-233731 subtask 1.1 timed out at
                        # 600 s on retries 0 AND 1 AND 2; "Timed out
                        # after 600s" written every time.
                        _retries_for_dispatch = 0
                        for _ph in task.phases:
                            for _st in _ph.subtasks:
                                if _st.id == node.id:
                                    _retries_for_dispatch = int(getattr(_st, "retries", 0) or 0)
                                    break
                            if _retries_for_dispatch:
                                break
                        subtask = Subtask(
                            id=node.id, role=node.role, description=node.description,
                            risk=node.risk, complexity=node.complexity, status="running",
                            artifact_name=node.artifact_name,
                            retries=_retries_for_dispatch,
                        )
                        if brief:
                            subtask.description = f"{node.description}\n\n{brief}"
                        print(f"\n[EXECUTE] {node.id} | {node.role} | {node.description[:60]}")
                        # Surface dispatch on InlineThinker — orchestrator_loop
                        # emits the same event at its dispatch site (line ~2002);
                        # deliberation_loop omitted it, so the global activity
                        # pill stayed idle while exec subtasks were running.
                        _desc_preview = (node.description or "")[:48]
                        if node.description and len(node.description) > 48:
                            _desc_preview += "…"
                        append_log(task.id, {
                            "type": "orchestrator_state",
                            "state": "running",
                            "label": f"Running {node.id} · {node.role} — {_desc_preview}",
                        }, config.tasks_dir)
                        update_node(task.id, node.id, {
                            "status": "running", "started_at": datetime.utcnow().isoformat(),
                        }, config.tasks_dir)
                        future = executor.submit(dispatch_subtask_streaming, subtask, task, config, session_id=brain_session_id)
                        active_futures[future] = node

                if active_futures:
                    done_futures = set()
                    try:
                        for future in as_completed(active_futures, timeout=2):
                            done_futures.add(future)
                            node = active_futures[future]
                            try:
                                result = future.result()
                            except Exception as e:
                                result = {"success": False, "output": "", "duration": 0, "cli": "", "model": "", "error": str(e)}

                            if result["success"]:
                                artifact_path = f"artifacts/{node.id}-{node.role}-position.md" if node.type == "position" else ""
                                update_node(task.id, node.id, {
                                    "status": "done",
                                    "completed_at": datetime.utcnow().isoformat(),
                                    "duration": result["duration"],
                                    "cli_used": result["cli"],
                                    "model_used": result["model"],
                                    "output_summary": result["output"][:500],
                                    "artifact": artifact_path,
                                    "error": "",
                                }, config.tasks_dir)
                                # P0-6 — only persist legacy disk artifact when the
                                # brain registry doesn't already have it. Subagents
                                # that called artifact_write() under the Stream B
                                # directive already produced the canonical row;
                                # writing the same content to artifacts/ as a
                                # `.md` file creates a duplicate, double-indexes
                                # cortex, and clutters the UI. Mirrors the same
                                # check _finalize_subtask runs for execution
                                # subtasks. Defensive disk-fallback only on
                                # missing brain row.
                                if not _has_brain_artifact(task.id, node.id):
                                    save_artifact(
                                        task.id, node.id, result["output"],
                                        config.tasks_dir,
                                        artifact_name=(
                                            f"{node.role}-position"
                                            if node.type == "position"
                                            else node.artifact_name
                                        ),
                                    )
                                print(f"[DONE] {node.id} ({node.type}) completed in {result['duration']:.1f}s")
                                # P1-1 — compressor between serial steps in
                                # deliberate mode. _maybe_compress gates on
                                # phase.serialize or task.gates_enabled and
                                # short-circuits when no new events have
                                # accumulated, so calling it on every node
                                # resolution is cheap. Auto-execute has its
                                # own pre-dispatch hook which covers the
                                # equivalent boundary; deliberate had no
                                # call site at all, hence 0 compression
                                # events on every deliberate run.
                                if node.type == "execution":
                                    try:
                                        _maybe_compress(task, node, config)
                                    except Exception as exc:
                                        logger.warning(
                                            "[compressor] post-resolution hook "
                                            "raised %r for %s — continuing without trace",
                                            exc, node.id,
                                        )
                            else:
                                # Execution nodes route through the same
                                # handle_failure logic as orchestrator_loop —
                                # retries up to max_retries on transient
                                # errors (timeout, rate-limit, signal kill),
                                # cascade-skip on permanent failure. Pre-fix
                                # this branch only retried signal kills and
                                # never cascade-skipped, so a timeout in
                                # phase 3 left the whole task blocked with
                                # the rest of the DAG orphaned.
                                if node.type == "execution":
                                    plan_subtask = None
                                    for ph in task.phases:
                                        for st in ph.subtasks:
                                            if st.id == node.id:
                                                plan_subtask = st
                                                break
                                        if plan_subtask:
                                            break
                                    if plan_subtask is None:
                                        # Defensive fallback — synthesize a
                                        # Subtask shim so handle_failure can
                                        # still retry/skip. Without this a
                                        # node-without-plan-row blocks the
                                        # task forever.
                                        plan_subtask = Subtask(
                                            id=node.id, role=node.role,
                                            description=node.description,
                                            risk=node.risk, complexity=node.complexity,
                                            status="failed",
                                            artifact_name=node.artifact_name,
                                            retries=getattr(node, "retries", 0) or 0,
                                        )
                                    handle_failure(plan_subtask, task, result, config)
                                    # Mirror the plan.yaml decision back onto
                                    # the DAG node so deliberation's
                                    # get_ready_nodes picks up the retry /
                                    # skip transitions on the next iteration.
                                    task_after = load_task(task.id, config.tasks_dir)
                                    for ph in task_after.phases:
                                        for st in ph.subtasks:
                                            if st.id != node.id:
                                                continue
                                            if st.status == "pending":
                                                update_node(task.id, node.id, {
                                                    "status": "pending",
                                                    "error": "",
                                                    "started_at": "",
                                                    "retries": getattr(st, "retries", 0) or 0,
                                                }, config.tasks_dir)
                                            elif st.status in ("failed", "skipped"):
                                                update_node(task.id, node.id, {
                                                    "status": st.status,
                                                    "error": st.error or result.get("error", "unknown"),
                                                    "output_summary": result.get("output", "")[:500],
                                                }, config.tasks_dir)
                                else:
                                    # Position / discussion failures keep
                                    # legacy semantics — no retry/cascade
                                    # primitives are wired through the
                                    # council subgraph.
                                    update_node(task.id, node.id, {
                                        "status": "failed",
                                        "error": result.get("error", "unknown"),
                                        "output_summary": result.get("output", "")[:500],
                                    }, config.tasks_dir)
                                    print(f"[FAILED] {node.id} ({node.type}): {result.get('error', 'unknown')}")
                    except TimeoutError:
                        pass
                    for future in done_futures:
                        del active_futures[future]
                else:
                    # Surface idle so the global activity pill hides between
                    # subtasks even in deliberate mode (auto-execute had this
                    # at orchestrator_loop ~line 2089). Without it the
                    # InlineThinker stays stuck on its last "Running …" label.
                    append_log(task.id, {
                        "type": "orchestrator_state",
                        "state": "idle",
                        "label": "",
                    }, config.tasks_dir)
                    time.sleep(1)

                # M3 review gate — deliberate mode. Phases are populated by
                # decompose_task above; each execution node is bound to a
                # subtask in task.phases by id. When every subtask in a
                # phase is terminal, run the reviewer pipeline. On FAIL
                # the gate resets implicated subtasks AND their DAG nodes
                # to pending, so this loop re-picks them on the next
                # iteration. The verdict event is persisted regardless of
                # PASS / CONDITIONAL / FAIL so M2 always has one row per
                # phase per task (closes the deliberate-mode 0-verdict
                # gap that auto-execute already covered).
                # M3 phase review — shared helper. See _run_phase_review_gates.
                task = load_task(task.id, config.tasks_dir)
                if not _run_phase_review_gates(task, config):
                    # Every complete phase passed — advance each as done.
                    # C7 — set_phase_status pairs the flip with a
                    # phase_status_changed event so the FE state pool sees
                    # the transition (closes Gate 2 §C7 silent-transition
                    # class; engine.py:2064 used to flip + save_plan with
                    # no event).
                    from okuro.orchestrator.state import set_phase_status
                    for phase in task.phases:
                        if phase.status in ("done", "blocked_review"):
                            continue
                        if not is_phase_complete(task, phase.id):
                            continue
                        set_phase_status(task, phase, "done", config.tasks_dir)
                        print(f"\n[PHASE {phase.id} COMPLETE]")

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"\n[FATAL] Deliberation loop crashed: {e}\n{tb}")
        try:
            task = load_task(task.id, config.tasks_dir)
            task.status = "failed"
            save_task_meta(task, config.tasks_dir)
        except Exception:
            pass
        raise
    finally:
        try:
            pid_path.unlink(missing_ok=True)
            heartbeat_path.unlink(missing_ok=True)
        except OSError:
            pass


_RISK_RANK = {"LOW": 1, "MED": 2, "HIGH": 3}
_AUTO_APPROVE_THRESHOLDS = {"none": 0, "medium": 2, "high": 3}


def _risk_auto_threshold(auto_approve_risk: str, subtask_risk: str) -> bool:
    """Return True when ``subtask_risk`` falls at or below the configured ceiling.

    auto_approve_risk levels (audit #23):
      - "none"   → never auto-approve via this mechanism (legacy behavior).
      - "medium" → auto-approve LOW + MED. HIGH still gates on user.
      - "high"   → auto-approve LOW + MED + HIGH (full unattended mode).
    """
    ceiling = _AUTO_APPROVE_THRESHOLDS.get((auto_approve_risk or "none").lower(), 0)
    rank = _RISK_RANK.get((subtask_risk or "").upper(), 0)
    return ceiling > 0 and rank <= ceiling


def _engine_version() -> str:
    """Best-effort engine commit SHA — falls back to 'unknown' if git is unavailable.

    Surfaced via engine_version log event so the Process view can show
    which code revision actually ran the task. Spawn-time-pinned engine
    subprocesses keep their original code for the entire task lifetime,
    even when the orchestrator-api restarts onto a newer commit — without
    this banner, mismatched-engine bugs look identical to working-engine
    bugs in the dashboard.
    """
    import subprocess as _sp
    try:
        result = _sp.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip() or "unknown"
    except Exception:
        pass
    return "unknown"


def orchestrator_loop(task: Task, config: Config, args):
    """The core orchestration loop."""
    global shutdown_requested

    # Persist `--yes` onto the task the FIRST time an engine observes it.
    # `--yes` was the only launch flag with no home on the Task dataclass, so
    # it lived in argv and died there: every park respawns via
    # `_resume_engine_if_idle` (api/main.py) with a BARE `--resume <id>`, and
    # the MED/HIGH risk gate below reads the flag. A task launched unattended
    # therefore began prompting for approval after its first park — a user
    # interaction produced by nothing the user did. Persisting here rather
    # than at the create call sites covers EVERY spawner in one place (create,
    # --continue, and the recurring scheduler's `--resume … --yes`).
    if getattr(args, "yes", False) and not task.auto_approve_yes:
        task.auto_approve_yes = True
        try:
            from okuro.orchestrator.state import save_task_meta as _save_task_meta

            _save_task_meta(task, config.tasks_dir)
        except Exception as exc:  # noqa: BLE001 — never fail a run over telemetry
            logger.warning("[%s] persisting --yes failed: %r", task.id, exc)

    # C4 — clear any sentinel left over from a crashed predecessor before
    # the work loop runs. Must happen before any read of state_reader so
    # snapshot consumers don't observe a stale "Reviewing" badge.
    try:
        # P4.3 — reap BEFORE revive. Reconcile resets orphaned subtasks to
        # pending so this engine can dispatch them; doing that while a
        # predecessor's subagent is still writing is how one subtask ends up
        # with two live sessions.
        reap_predecessor_sessions(task.id, config.tasks_dir)
        reconcile_on_startup(task.id, config.tasks_dir)
    except Exception as exc:
        logger.warning("[%s] reconcile_on_startup raised: %r", task.id, exc)

    # PR C — engine-lease takeover protocol. Three cases:
    #
    #   1. No prior lease → cold start, no resume signal.
    #   2. Prior lease present but TTL expired AND its PID is dead →
    #      warm start. Emit ``engine_resumed`` so the FE shows a
    #      "engine resumed — recovering" banner; also scan for orphaned
    #      subagent sessions left over from the dead engine and reap
    #      them via the kill-flag mechanism the dispatcher polls.
    #   3. Lease alive → another engine owns this task. We MUST exit
    #      rather than race over plan.yaml writes. (Stage B G9: the
    #      prior single-shot probe missed this case under restart-cascade.)
    try:
        _enforce_engine_lease(task, config.tasks_dir)
    except SystemExit:
        raise
    except Exception as exc:
        logger.warning("[%s] engine lease check raised: %r", task.id, exc)

    # PR A — spin up the orchestrator_state heartbeat so long bridge_invoke
    # calls (reviewer + decomposer) keep the FE OkuroThinker pill alive.
    try:
        start_orchestrator_heartbeat(task.id, config.tasks_dir)
    except Exception as exc:
        logger.warning("[%s] heartbeat start raised: %r", task.id, exc)

    try:
        append_log(task.id, {
            "type": "engine_version",
            "sha": _engine_version(),
            "mode": task.mode or "auto-execute",
        }, config.tasks_dir)
    except Exception:
        pass

    if task.mode == "deliberate":
        deliberation_loop(task, config, args)
        return

    # Liveness — write the PID file + an initial heartbeat BEFORE the
    # (blocking, multi-minute) agentic decompose runs. Without this, the
    # auto-execute path had no .orchestrator.pid / .heartbeat during
    # decompose (they were only written once the work loop began, below),
    # so any status GET after the 15s spawn grace force-halted a live
    # decompose. Mirrors deliberation_loop, which already writes both before
    # its decompose. Belt-and-suspenders with the .engine.lease check in
    # api.main._is_orchestrator_running. The work loop re-touches both.
    try:
        (config.tasks_dir / task.id / ".orchestrator.pid").write_text(str(os.getpid()))
        (config.tasks_dir / task.id / ".heartbeat").write_text(datetime.utcnow().isoformat())
    except Exception as exc:
        logger.warning("[%s] early pid/heartbeat write raised: %r", task.id, exc)

    # Wave-5 G20 — read-only state↔brain reconciler at startup. Logs any
    # disk-YAML vs DB discrepancies for this task before resuming work.
    # Never auto-repairs (operator inspection only). Wrapped so a
    # reconciler bug cannot block the orchestrator loop from running.
    try:
        from okuro.orchestrator.reconciler import reconcile_task
        for d in reconcile_task(task.id, config.tasks_dir):
            logger.warning(
                "[RECONCILER] %s/%s status=%s — %s",
                d.task_id, d.subtask_id, d.status, d.note,
            )
            try:
                append_log(task.id, {
                    "type": "reconciler_discrepancy",
                    "subtask": d.subtask_id,
                    "status": d.status,
                    "has_artifact": d.has_artifact,
                    "has_handover": d.has_handover,
                    "note": d.note,
                }, config.tasks_dir)
            except Exception:
                pass
    except Exception as exc:
        logger.warning("[RECONCILER] startup pass raised %r — skipped", exc)

    if not task.phases:
        task.status = "planning"
        update_task_status(task, config.tasks_dir)
        append_log(task.id, {
            "type": "orchestrator_state",
            "state": "planning",
            "label": "Decomposing task…",
        }, config.tasks_dir)

        try:
            # A task may name a MANUALLY ARRANGED workflow, in which case its
            # drawn graph compiles to the plan and no LLM is involved. Returns
            # None when there is no workflow, which is the normal path.
            from okuro.orchestrator.workflow_run import describe_workflow, phases_for_task

            phases = phases_for_task(task)
            if phases is None:
                print("\n[DECOMPOSING] Sending task to LLM for decomposition...")
                phases = decompose_task(task.description, config, required_roles=task.required_roles or None, intelligence=task.intelligence, task_id=task.id)
            else:
                print(f"\n[WORKFLOW] Compiled plan from {describe_workflow(task)} — no LLM decomposition.")
            task.phases = phases
            save_plan(task, config.tasks_dir)
            # Decomposition done — clear the "Decomposing task…" banner.
            # Subtask execution events will re-light the widget with their
            # own labels below.
            append_log(task.id, {
                "type": "orchestrator_state",
                "state": "idle",
                "label": "",
            }, config.tasks_dir)
            if not task.title:
                import threading

                def _generate_title_async(task_id, description, tasks_dir, cfg):
                    try:
                        from okuro.orchestrator.decomposer import call_llm_api
                        title = call_llm_api(
                            "Generate a short title (3-5 words, no quotes) for this task:\n\n" + description[:500],
                            cfg,
                        ).strip().strip("\"'")
                        if title and len(title) < 60:
                            t = load_task(task_id, tasks_dir)
                            t.title = title
                            update_task_status(t, tasks_dir)
                            print(f"[TITLE] {title}")
                    except Exception as e:
                        logger.warning(f"Title generation failed: {e}")

                threading.Thread(
                    target=_generate_title_async,
                    args=(task.id, task.description, config.tasks_dir, config),
                    daemon=True, name="title-gen",
                ).start()
        except Exception as e:
            print(f"\n[ERROR] Decomposition failed: {e}")
            append_log(task.id, {
                "type": "orchestrator_state",
                "state": "error",
                "label": f"Decomposition failed: {str(e)[:60]}",
            }, config.tasks_dir)
            task.status = "failed"
            update_task_status(task, config.tasks_dir)
            return

        print()
        print_plan(task)

        if not args.yes and sys.stdin.isatty():
            print()
            response = input("Proceed with this plan? [Y/n/edit]: ").strip().lower()
            if response in ("n", "no"):
                print("\n[HALTED] Plan rejected by user.")
                task.status = "halted"
                update_task_status(task, config.tasks_dir)
                return
            if response == "edit":
                print(f"\n[INFO] Edit: {config.tasks_dir / task.id / 'plan.yaml'}")
                print(f"Then resume with: okuro orchestrator --resume {task.id}")
                task.status = "halted"
                update_task_status(task, config.tasks_dir)
                return
        elif not args.yes:
            print("\n[HEADLESS] No TTY detected — auto-approving plan")

        print("\n[APPROVED] Starting execution...")
        task.status = "active"
        update_task_status(task, config.tasks_dir)

    cli_short = {"claude": "cc", "antigravity": "agy", "codex": "cdx"}.get(config.cli_default, config.cli_default[:3])
    brain_session_id = _brain_session_start(task.id, task.description, cli_short=cli_short)

    sentinel = Sentinel(config)
    config.tasks_dir.mkdir(parents=True, exist_ok=True)

    warnings = validate_task(task)
    if warnings:
        logger.warning(f"Task {task.id} has {len(warnings)} validation warning(s)")
        for w in warnings:
            logger.warning(f"  [{task.id}] {w}")

    if not task.phases:
        print("\n[ERROR] Task has no phases. Cannot execute.")
        task.status = "failed"
        update_task_status(task, config.tasks_dir)
        return

    total_subtasks = sum(len(p.subtasks) for p in task.phases)
    if total_subtasks == 0:
        print("\n[ERROR] Task has phases but no subtasks. Cannot execute.")
        task.status = "failed"
        update_task_status(task, config.tasks_dir)
        return

    start_time = datetime.now()
    max_parallel = config.execution.max_parallel

    pid_path = config.tasks_dir / task.id / ".orchestrator.pid"
    pid_path.write_text(str(os.getpid()))
    heartbeat_path = config.tasks_dir / task.id / ".heartbeat"

    # Wave-5b G18 — when running in parallel, prune any leftover worktree
    # bookkeeping from a prior crashed run before the loop starts. Quick,
    # safe, no-op on non-git project_paths.
    if max_parallel > 1 and getattr(task, "project_path", ""):
        try:
            from okuro.orchestrator.worktree import (
                cleanup_stale_worktrees,
                cleanup_stale_branches,
            )
            pruned = cleanup_stale_worktrees(task.project_path)
            if pruned:
                logger.info("[worktree] pruned %d stale entries before run", pruned)
            # Umbrella audit fix #12 — also delete orphan okuro/* branches
            # whose worktrees no longer exist. Without this, branches
            # accumulate across orchestrator runs indefinitely.
            deleted = cleanup_stale_branches(task.project_path)
            if deleted:
                logger.info("[worktree] deleted %d stale okuro/* branches", deleted)
        except Exception as exc:
            logger.warning("[worktree] startup prune raised %r — continuing", exc)

    try:
        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            active_futures = {}
            # Wave-5b G18 — track per-future worktree paths so cleanup runs
            # exactly when the future completes, not at task end.
            worktree_paths: dict = {}

            while not shutdown_requested:
                # P4.5 — see the deliberation loop's twin above.
                if restart_requested:
                    logger.warning(
                        "[%s] restarting for fresh source (orchestrator loop)",
                        task.id,
                    )
                    return
                heartbeat_path.write_text(datetime.utcnow().isoformat())
                task = load_task(task.id, config.tasks_dir)

                # Park = engineless (auto-execute mode). If the task is parked
                # on a HUMAN gate (decision_gate, blocked_review, waiting_user,
                # subtask_approval) and nothing is in-flight, EXIT cleanly —
                # never poll a human for hours. The resolution endpoint
                # (/gates/{id}/resolve, /override-blocked-review, /approve, …)
                # respawns a fresh --resume engine. A transient wait with
                # futures still running is NOT a human park — keep going.
                if not active_futures and _is_human_park(task):
                    _park_on_human_wait(task, config)
                    raise SystemExit(0)

                # Continue-vs-resume race recovery: drain any continuation
                # recorded but never decomposed (before_phase_id > max phase id)
                # BEFORE the completion check, so a --resume respawn that
                # inherited an all-done plan materializes the queued phases
                # instead of finalizing to done. Idempotent + no-op when empty.
                if not active_futures and _drain_pending_continuations(task, config):
                    task = load_task(task.id, config.tasks_dir)
                    continue

                if is_task_complete(task):
                    _finalize_task(task, config, active_futures, brain_session_id)
                    break

                # M1 — shared helper. See _pause_on_pending_gate.
                if not active_futures and _pause_on_pending_gate(task, config):
                    task = load_task(task.id, config.tasks_dir)
                    continue

                # Gate 2 §C5 — auto-execute work loop also consumes the
                # composite halt set. max_retries threads through so the
                # cap is checked pre-dispatch (V2 root) instead of post-
                # dispatch in handle_failure.
                ready = get_ready_subtasks(
                    task,
                    max_retries=getattr(getattr(config, "execution", None), "max_retries", DEFAULT_MAX_RETRIES),
                )
                running_ids = {st.id for st in active_futures.values()}
                ready = [st for st in ready if st.id not in running_ids]

                if not ready and not active_futures:
                    if is_task_complete(task):
                        _finalize_task(task, config, active_futures, brain_session_id)
                        break
                    # Fix A — recurring runs never wedge on a human gate.
                    # Reaching here means no ready work, nothing running, not
                    # complete: a failed/blocked subtask with retries
                    # exhausted. A recurring run has no human watching to
                    # resolve a blocked_review, and the schedule's next run
                    # supersedes it — so terminal-fail instead of parking on
                    # blocked_review/waiting_user (which the reconciler would
                    # otherwise re-park on every restart). Oneshot tasks keep
                    # the awaiting/blocked_review surface below.
                    if getattr(task, "task_type", "oneshot") == "recurring":
                        _terminate_recurring_run_failed(task, config)
                        break
                    # Distinguish "awaiting the user" from "genuinely stuck".
                    # A retries-cap hit (reviewer FAIL or handle_failure)
                    # routes through set_awaiting, which flips task.status to
                    # "waiting_user" and populates task.awaiting. In that case
                    # get_ready_subtasks short-circuits to [] (the awaiting
                    # kind is in deliberation._HALT_AWAITING_KINDS) and we land
                    # here — but the task is NOT a silent failure: the user has
                    # a resolve endpoint. Surface it and stop the loop WITHOUT
                    # marking the task failed/blocked.
                    awaiting = getattr(task, "awaiting", None)
                    if awaiting is not None or task.status == "waiting_user":
                        reason = getattr(awaiting, "message", "") or (
                            "Task is awaiting your input."
                        )
                        kind = getattr(awaiting, "kind", "") or "awaiting_user"
                        print(
                            f"\n[AWAITING USER] Loop paused — {kind}: {reason}"
                        )
                        append_log(task.id, {
                            "type": "orchestrator_state",
                            # ROCK-SOLID v5 P1.1 — this site's own comment
                            # above says "the task is NOT a silent failure:
                            # the user has a resolve endpoint" — it was
                            # still emitting "error". Not in the evidence
                            # inventory's 12-site list; found by a live
                            # sweep for remaining state="error" emissions.
                            "state": "waiting",
                            "label": f"Awaiting you — {reason}"[:300],
                        }, config.tasks_dir)
                        # Park = engineless: write .exited + clear lease so the
                        # launcher doesn't respawn and the snapshot reads idle.
                        # The resolve endpoint respawns a fresh --resume engine.
                        park_engine_exit(
                            task.id, config.tasks_dir, reason=f"park:{kind}",
                        )
                        break
                    # Genuinely stuck — no ready work, nothing running, no
                    # awaiting surface. This is a real block (e.g. an
                    # unsatisfiable dependency). Surface it via set_awaiting so
                    # the user sees a reason + can act, instead of a silent
                    # status="blocked" kill with nothing on screen.
                    print("\n[BLOCKED] No subtasks ready. Remaining subtasks are blocked or failed.")
                    from okuro.orchestrator.gate_messages import humanize_gate as _hg
                    _gm = _hg("blocked_review", "stuck", {
                        "phase_id": "?",
                        "raw_reason": ("no subtasks ready and none running — unmet "
                                       "dependency or a subtask the scheduler refuses "
                                       "to dispatch"),
                    })
                    blocked_reason = _gm.to_message()
                    try:
                        from okuro.orchestrator.state import set_awaiting as _set_awaiting
                        _set_awaiting(
                            task, config.tasks_dir,
                            kind="blocked_review",
                            message=blocked_reason,
                            endpoint=f"/api/tasks/{task.id}/override-blocked-review",
                            payload={"reason": "no_subtasks_ready",
                                     "presentation": _gm.to_presentation()},
                        )
                    except Exception as exc:
                        logger.warning("blocked set_awaiting failed: %r", exc)
                        task.status = "blocked"
                        update_task_status(task, config.tasks_dir)
                    append_log(task.id, {
                        "type": "orchestrator_state",
                        # ROCK-SOLID v5 P1.1 — recoverable park, not a
                        # failure (evidence inventory mismatch #12/#13).
                        "state": "waiting",
                        "label": "Blocked — no parts ready",
                    }, config.tasks_dir)
                    notify_channel("needs_decision", task.id, task.description,
                                   error="No subtasks are ready to run")
                    # Park = engineless: write .exited so the launcher does not
                    # respawn this parked engine; /override-blocked-review
                    # respawns a fresh one when the user acts.
                    park_engine_exit(
                        task.id, config.tasks_dir, reason="park:blocked_review",
                    )
                    break

                slots_available = max_parallel - len(active_futures)
                for subtask in ready[:slots_available]:
                    # task.auto_approve_risk (audit #23) lets the caller
                    # short-circuit per-subtask approval prompts at create-time
                    # — "medium" auto-approves LOW+MED, "high" auto-approves
                    # everything (LOW+MED+HIGH). Default "none" preserves the
                    # existing wait_for_approval / handle_approval flow so the
                    # current behavior is intact for callers that don't opt in.
                    auto_at_or_below = _risk_auto_threshold(task.auto_approve_risk, subtask.risk)
                    if subtask.status == "approved":
                        pass
                    elif auto_at_or_below:
                        print(f"[AUTO-APPROVED] {subtask.id} ({subtask.risk}, auto_approve_risk={task.auto_approve_risk})")
                    elif (config.execution.risk_enforcement
                          and subtask.risk in ("MED", "HIGH")
                          and not _writes_outside_its_workspace(
                              subtask, task, config)):
                        # P2.2 — blast radius, not risk score. The subtask
                        # scored MED or HIGH but declares nothing outside its
                        # own task folder, so there is no change for a human
                        # to approve. This is the two-day comprehension-step
                        # stall class, closed.
                        print(f"[NO-GATE] {subtask.id} ({subtask.risk} risk) — "
                              "writes nothing outside its own task folder")
                        _emit_no_gate_decision(task, subtask, config)
                    elif config.execution.risk_enforcement and subtask.risk in ("MED", "HIGH"):
                        # Read the PERSISTED flag, not argv: a respawned engine
                        # (every park → `_resume_engine_if_idle` → bare
                        # `--resume`) has args.yes False even for a task the
                        # user launched with --yes. task.auto_approve_yes is
                        # stamped at loop entry, so both spellings agree.
                        _yes = getattr(args, "yes", False) or task.auto_approve_yes
                        if _yes and subtask.risk == "MED":
                            print(f"[AUTO-APPROVED] {subtask.id} (MED risk, --yes flag)")
                        elif _yes and subtask.risk == "HIGH":
                            print(f"[SKIPPED] {subtask.id} (HIGH risk, --yes flag)")
                            update_subtask(task.id, subtask.id, {"status": "skipped"}, config.tasks_dir)
                            continue
                        elif sys.stdin.isatty():
                            if subtask.risk == "MED":
                                if not handle_approval(subtask, task):
                                    print(f"[SKIPPED] {subtask.id} - User declined approval")
                                    update_subtask(task.id, subtask.id, {"status": "skipped"}, config.tasks_dir)
                                    continue
                            else:
                                handle_high_risk(subtask, task, config)
                                task = load_task(task.id, config.tasks_dir)
                                continue
                        else:
                            skipped = wait_for_approval(subtask, task, config)
                            task = load_task(task.id, config.tasks_dir)
                            if skipped:
                                continue

                    create_checkpoint(
                        task.id, config.tasks_dir,
                        checkpoint_type=CheckpointType.DISPATCH,
                        reason=f"Before dispatch {subtask.id}",
                        subtask_id=subtask.id,
                    )

                    print(f"\n[DISPATCH] {subtask.id} | {subtask.role} | {subtask.description}")
                    update_subtask(task.id, subtask.id, {
                        "status": "running",
                        "started_at": datetime.utcnow().isoformat(),
                    }, config.tasks_dir)

                    # Surface this subtask on the InlineThinker. Truncated
                    # description keeps the badge readable even for long
                    # subtask labels.
                    _desc_preview = (subtask.description or "")[:48]
                    if subtask.description and len(subtask.description) > 48:
                        _desc_preview += "…"
                    append_log(task.id, {
                        "type": "orchestrator_state",
                        "state": "running",
                        "label": f"Running {subtask.id} · {subtask.role} — {_desc_preview}",
                    }, config.tasks_dir)

                    # M2 — compressor pre-dispatch hook. Distills the
                    # task event log into a decision-trace artifact + a
                    # 'compression' event so the next subtask's brief
                    # inherits cross-subtask context without reading raw
                    # history. Gated on (a) gates_enabled or serialized
                    # phase (decision-laden tasks where drift cost is
                    # high) and (b) presence of new events since last
                    # compression (skip-cheap when log is empty).
                    try:
                        _maybe_compress(task, subtask, config)
                    except Exception as exc:
                        logger.warning(
                            "[compressor] pre-dispatch hook raised %r for %s — "
                            "continuing without trace", exc, subtask.id,
                        )

                    # Wave-5b G18 — create per-subagent git worktree when
                    # parallel + git repo. setup_worktree returns None on
                    # any failure (no git, dirty tree, branch race) and we
                    # fall through to shared-tree dispatch (pre-G18 behavior).
                    wt_path = None
                    try:
                        from okuro.orchestrator.worktree import (
                            should_use_worktree, setup_worktree,
                        )
                        if should_use_worktree(getattr(task, "project_path", ""),
                                                 max_parallel):
                            wt_path = setup_worktree(
                                task.project_path, task.id, subtask.id,
                            )
                            if wt_path is not None:
                                print(f"[WORKTREE] {subtask.id} → {wt_path}")
                    except Exception as exc:
                        logger.warning(
                            "[worktree] setup raised %r for %s — using shared tree",
                            exc, subtask.id,
                        )
                        wt_path = None

                    future = executor.submit(
                        dispatch_subtask_streaming, subtask, task, config,
                        session_id=brain_session_id,
                        effective_project_path=wt_path,
                    )
                    active_futures[future] = subtask
                    worktree_paths[future] = wt_path

                if active_futures:
                    done_futures = set()
                    try:
                        for future in as_completed(active_futures, timeout=2):
                            done_futures.add(future)
                            subtask = active_futures[future]
                            try:
                                result = future.result()
                            except Exception as e:
                                result = {"success": False, "output": "", "duration": 0, "cli": "", "model": "", "error": str(e)}

                            if result["success"]:
                                # _finalize_subtask disk-verifies the artifact
                                # before marking done. Returns False when the
                                # subagent ghost-completed (return code 0 but
                                # missing/empty artifact) — handle_failure was
                                # invoked internally so we just bookkeep.
                                if _finalize_subtask(task, subtask, result, config):
                                    print(f"[DONE] {subtask.id} completed in {result['duration']:.1f}s")
                                    sentinel.record_success()
                                    _log_to_brain(task, subtask, result)
                                else:
                                    print(f"[GHOST] {subtask.id} returned 0 but produced no artifact — routed to retry/escalation")
                                    sentinel.record_failure()
                            else:
                                handle_failure(subtask, task, result, config)
                                sentinel.record_failure()
                    except TimeoutError:
                        pass

                    for future in done_futures:
                        # Wave-5b G18 — cleanup the worktree (if any) the
                        # moment its subagent's future completes. Done
                        # before del-ing from active_futures so we keep
                        # the path in worktree_paths until cleanup runs.
                        wt_path = worktree_paths.pop(future, None)
                        if wt_path is not None:
                            try:
                                from okuro.orchestrator.worktree import cleanup_worktree
                                cleanup_worktree(wt_path)
                            except Exception as exc:
                                logger.warning(
                                    "[worktree] cleanup raised %r for %s",
                                    exc, wt_path,
                                )
                        del active_futures[future]

                    # When the in-flight set drains, surface idle so the
                    # widget hides between subtasks even if the next
                    # dispatch hasn't fired yet.
                    if not active_futures:
                        append_log(task.id, {
                            "type": "orchestrator_state",
                            "state": "idle",
                            "label": "",
                        }, config.tasks_dir)
                else:
                    time.sleep(1)

                if sentinel.should_check():
                    task = load_task(task.id, config.tasks_dir)
                    task_dir = config.tasks_dir / task.id
                    suggestions = sentinel.check(task, task_dir)
                    for suggestion in suggestions:
                        print(f"\n[SENTINEL] {suggestion}")
                    # Operational telemetry → the task's own activity log, NOT
                    # `thoughts`. See Sentinel.log_observations for the measured
                    # damage this used to do to the user's inbox.
                    sentinel.log_observations(task_dir, suggestions)

                task = load_task(task.id, config.tasks_dir)
                phase_advanced = False
                # M3 phase review — shared helper. Returns True if any
                # phase was reset/blocked, in which case this iteration
                # skips advance. Otherwise advance every newly-complete
                # phase to done with a checkpoint.
                m3_reset_any = False
                if getattr(task, "gates_enabled", False):
                    m3_reset_any = _run_phase_review_gates(task, config)
                    if m3_reset_any:
                        task = load_task(task.id, config.tasks_dir)
                if not m3_reset_any:
                    # C7 — set_phase_status pairs the flip with a canonical
                    # phase_status_changed event; engine.py:2535 used to
                    # mutate phase.status in-memory + save_plan at the bottom
                    # with no event. The helper persists + emits per phase.
                    from okuro.orchestrator.state import set_phase_status
                    for phase in task.phases:
                        if phase.status in ("done", "blocked_review"):
                            continue
                        if not is_phase_complete(task, phase.id):
                            continue
                        create_checkpoint(
                            task.id, config.tasks_dir,
                            checkpoint_type=CheckpointType.PHASE,
                            reason=f"Phase {phase.id} complete",
                            phase_id=phase.id,
                        )
                        set_phase_status(task, phase, "done", config.tasks_dir)
                        print(f"\n[PHASE {phase.id} COMPLETE]")
                        for next_phase in task.phases:
                            if next_phase.status != "done":
                                task.current_phase = next_phase.id
                                phase_advanced = True
                                break
                        else:
                            task.current_phase = phase.id
                            phase_advanced = True
                # A drawn workflow's fan-out cannot be planned up front — its
                # item count only exists once an upstream phase has produced the
                # list. Now that a phase has closed, expand any fan-out whose
                # source artifact has appeared. No-op for every task without a
                # workflow, and for a workflow whose list is not written yet.
                try:
                    from okuro.orchestrator.workflow_run import auto_expand

                    if auto_expand(task, config.tasks_dir):
                        task = load_task(task.id, config.tasks_dir)
                        phase_advanced = True
                except Exception as exc:  # noqa: BLE001 — expansion must not kill the run
                    print(f"\n[WORKFLOW] fan-out expansion failed: {exc}")
                    append_log(task.id, {
                        "type": "workflow_fanout_failed",
                        "error": str(exc)[:500],
                    }, config.tasks_dir)

                if phase_advanced:
                    update_task_status(task, config.tasks_dir)
                    save_plan(task, config.tasks_dir)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"\n[FATAL] Orchestrator crashed: {e}\n{tb}")
        try:
            task = load_task(task.id, config.tasks_dir)
            task.status = "failed"
            update_task_status(task, config.tasks_dir)
            append_log(task.id, {
                "type": "orchestrator_crashed",
                "error": str(e),
                "traceback": tb[:2000],
            }, config.tasks_dir)

            latest_cp = get_latest_checkpoint(task.id, config.tasks_dir)
            if latest_cp:
                print(f"\n[RECOVERY] Latest checkpoint: {latest_cp.id} ({latest_cp.reason})")
                print(f"  Restore via API: POST /api/tasks/{task.id}/restore/{latest_cp.id}")
        except Exception:
            pass
        raise
    finally:
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            heartbeat_path.unlink(missing_ok=True)
        except OSError:
            pass

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    _brain_session_end(brain_session_id, task=task)

    if task.status == "done":
        _harvest_learnings(task, config)

    if shutdown_requested:
        print(f"\n[SHUTDOWN] Task state saved to: {config.tasks_dir / task.id}")
        print(f"To resume: okuro orchestrator --resume {task.id}")
    else:
        print_summary(task, duration, config)
        # Note: _generate_continuation_suggestions already ran in
        # _finalize_task on the normal exit path; this call is a
        # safety net for exotic completion paths (deliberation mode,
        # retry-drain) that bypass _finalize_task.
        if task.status == "done":
            task_yaml = config.tasks_dir / task.id / "task.yaml"
            needs_suggest = True
            try:
                with open(task_yaml) as _f:
                    _td = yload(_f) or {}
                if (_td.get("continuation_suggestions") or []):
                    needs_suggest = False
            except Exception:
                pass
            if needs_suggest:
                _generate_continuation_suggestions(task, config)


def handle_approval(subtask: Subtask, task: Task) -> bool:
    print()
    print("=" * 70)
    print("[APPROVAL REQUIRED]")
    print("=" * 70)
    print(f"Subtask: {subtask.id} - {subtask.description}")
    print(f"Role: {subtask.role}")
    print(f"Risk: {subtask.risk}")
    print(f"Complexity: {subtask.complexity}")
    print("=" * 70)
    print()
    response = input("Approve execution? [y/N]: ").strip().lower()
    return response in ("y", "yes")


def handle_high_risk(subtask: Subtask, task: Task, config: Config):
    print()
    print("=" * 70)
    print("[HIGH RISK - MANUAL EXECUTION REQUIRED]")
    print("=" * 70)
    print(f"Subtask: {subtask.id} - {subtask.description}")
    print(f"Role: {subtask.role}")
    print()
    print("This subtask requires manual execution.")
    print("Suggested command:")
    print()
    cli_name = config.cli_default
    tool = config.cli_tools[cli_name]
    print(f"  {tool.binary} -p --model {config.orchestrator.decompose_model} \\")
    print(f'    "{subtask.description}"')
    print()
    print("=" * 70)
    print()

    response = input("Mark as [done/skip/fail]: ").strip().lower()
    if response == "done":
        update_subtask(task.id, subtask.id, {
            "status": "done", "completed_at": datetime.utcnow().isoformat(),
        }, config.tasks_dir)
        print(f"[DONE] {subtask.id} marked as completed")
    elif response == "skip":
        update_subtask(task.id, subtask.id, {"status": "skipped"}, config.tasks_dir)
        print(f"[SKIPPED] {subtask.id}")
    else:
        update_subtask(task.id, subtask.id, {
            "status": "failed", "error": "User marked as failed",
        }, config.tasks_dir)
        print(f"[FAILED] {subtask.id}")


def wait_for_decision(phase_id: int, gate, task: Task, config: Config) -> bool:
    """M1 — block dispatch until the user resolves a phase decision gate.

    Three paths:
      - TTY present: render the options table inline and prompt for a choice
        (1..N, "skip", or "abort"). The selection is locked into task.adrs
        and the gate via ``resolve_decision_gate`` (atomic under file lock).
      - Autopilot: answer from the profile via ``autopilot_gate`` and apply
        it through the same ``resolve_decision_gate`` core. Abstain or low
        confidence falls through to the park below.
      - Headless: persist the awaiting (status -> waiting_user) and EXIT the
        engine. ``POST /api/tasks/{id}/gates/{gate_id}/resolve`` resumes a
        fresh engine that reads the locked ADR.

    THE HEADLESS PATH NO LONGER POLLS. It did — plan.yaml every 2s,
    indefinitely — and this docstring went on describing that for long enough
    that ROCK-SOLID v5 raised "make decision_gate park like every other gate"
    as an open DECISION (D-B) against a behaviour the code had already
    stopped having. A stale docstring does not just fail to help; it
    manufactures work. Verified 2026-08-01: zero ``time.sleep`` calls in this
    function, and the single ``while`` is the TTY prompt loop.

    Returns True when the gate was skipped (engine should mark the phase
    accordingly and move on), False when an option was locked. On the
    headless path it does not return at all — it raises SystemExit(0).
    """
    global shutdown_requested

    # Always emit the event so the web UI / log consumers can react.
    append_log(task.id, {
        "type": "await_user_decision",
        "phase_id": phase_id,
        "gate_id": gate.id,
        "prompt": gate.prompt,
        "options": [
            {"id": o.id, "label": o.label, "recommended": bool(o.recommended)}
            for o in gate.options
        ],
    }, config.tasks_dir)

    # Live status — the global InlineThinker pill shows this as soon as
    # the engine blocks, so off-panel views also know what's happening.
    append_log(task.id, {
        "type": "orchestrator_state",
        "state": "planning",
        "label": f"Awaiting ADR lock — gate {gate.id} ({_step(phase_id).lower()})",
    }, config.tasks_dir)

    # Status banner — the dashboard reads task.status for the "what is the
    # orchestrator doing right now" pill.
    task.status = "awaiting_decision"
    update_task_status(task, config.tasks_dir)

    print(f"\n[GATE] phase {phase_id} — {gate.prompt}")
    for idx, opt in enumerate(gate.options, 1):
        marker = " (recommended)" if opt.recommended else ""
        print(f"  {idx}. [{opt.id}] {opt.label}{marker}")
        if opt.description:
            print(f"       {opt.description}")
        if opt.pros:
            print(f"       pros: {opt.pros}")
        if opt.cons:
            print(f"       cons: {opt.cons}")

    if sys.stdin.isatty():
        while not shutdown_requested:
            raw = input(
                f"\n[DECIDE] choose option [1-{len(gate.options)}], "
                "'s' to skip, 'a' to abort: "
            ).strip().lower()
            if raw in ("a", "abort"):
                print("[ABORTED] gate decision aborted by user")
                task.status = "halted"
                update_task_status(task, config.tasks_dir)
                shutdown_requested = True
                return True
            if raw in ("s", "skip"):
                resolve_decision_gate(
                    task.id, phase_id, "", "", config.tasks_dir, skipped=True,
                )
                task.status = "active"
                update_task_status(task, config.tasks_dir)
                print(f"[SKIPPED] gate {gate.id} — phase will proceed without ADR")
                return True
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(gate.options):
                    option = gate.options[idx]
                    rationale = input(
                        "[DECIDE] rationale (optional, enter to skip): "
                    ).strip()
                    resolve_decision_gate(
                        task.id, phase_id, option.id, rationale, config.tasks_dir,
                    )
                    task.status = "active"
                    update_task_status(task, config.tasks_dir)
                    print(f"[LOCKED] ADR for gate {gate.id} → {option.label}")
                    return False
            except ValueError:
                pass
            print("[?] invalid selection — try again")
        return True

    # F2 autopilot — answer the gate from the profile instead of parking. The
    # chosen option is a gate option id; apply it via the SAME core the resolve
    # endpoint uses (resolve_decision_gate) and continue in-process. Abstain /
    # low confidence falls through to the headless park below.
    try:
        from okuro.orchestrator.autopilot import autopilot_gate as _autopilot_gate
        _opt_ids = [o.id for o in gate.options]
        _chosen = _autopilot_gate(
            config, task, config.tasks_dir,
            kind="decision_gate",
            options=_opt_ids,
            payload={
                "gate_id": gate.id,
                "phase_id": phase_id,
                "prompt": gate.prompt,
                "message": gate.prompt,
                "options": [
                    {"id": o.id, "label": o.label, "description": o.description,
                     "pros": o.pros, "cons": o.cons, "recommended": o.recommended}
                    for o in gate.options
                ],
            },
            ctx={"phase_id": phase_id},
        )
        if _chosen:
            resolve_decision_gate(
                task.id, phase_id, _chosen,
                "autopilot: auto-answered from profile", config.tasks_dir,
            )
            task.status = "active"
            update_task_status(task, config.tasks_dir)
            print(f"[AUTOPILOT] gate {gate.id} → {_chosen} (phase {phase_id})")
            return False
    except Exception as exc:
        logger.warning("autopilot decision_gate resolve failed: %r", exc)

    # Headless: poll plan.yaml for the gate to leave "pending".
    print(
        f"[HEADLESS] parking for decision — resolve via "
        f"POST /api/tasks/{task.id}/gates/{gate.id}/resolve"
    )
    try:
        from okuro.orchestrator.state import set_awaiting as _set_awaiting
        from okuro.orchestrator.gate_messages import humanize_gate as _hg
        _gm = _hg("decision_gate", "", {
            "phase_id": phase_id,
            "prompt": gate.prompt,
            "raw_reason": f"gate {gate.id}",
        })
        _set_awaiting(
            task, config.tasks_dir,
            kind="decision_gate",
            message=_gm.to_message(),
            endpoint=f"/api/tasks/{task.id}/gates/{gate.id}/resolve",
            payload={
                "gate_id": gate.id,
                "phase_id": phase_id,
                "presentation": _gm.to_presentation(),
                "prompt": gate.prompt,
                "options": [
                    {
                        "id": o.id,
                        "label": o.label,
                        "description": o.description,
                        "pros": o.pros,
                        "cons": o.cons,
                        "recommended": o.recommended,
                    }
                    for o in gate.options
                ],
            },
        )
        _emit_orchestrator_state(
            task, state="idle", label="",
            tasks_dir=config.tasks_dir,
        )
    except Exception as exc:
        logger.warning("decision_gate set_awaiting failed: %r", exc)

    # Park = engineless. A decision gate blocks on a HUMAN; the prior
    # implementation polled plan.yaml every 2s indefinitely, keeping a live
    # process alive for hours/days until a reboot / orchestrator-restart /
    # source-change killed it and left a stale lease the reconciler then
    # respawned ("engine not responding — auto-restarted"). The awaiting
    # state is now persisted (set_awaiting above flips status→waiting_user),
    # so exit cleanly. POST /api/tasks/{id}/gates/{gate_id}/resolve calls
    # _resume_engine_if_idle → a fresh --resume engine picks up the locked
    # ADR. SystemExit(0) so the launcher honors the `.exited` sentinel and
    # does NOT respawn.
    print(
        f"[PARKED] decision gate {gate.id} (phase {phase_id}) — engine "
        "exiting; resolve via API to resume"
    )
    park_engine_exit(
        task.id, config.tasks_dir, reason=f"decision_gate:{gate.id}",
    )
    raise SystemExit(0)


def _emit_no_gate_decision(subtask_task, subtask: Subtask, config: Config) -> None:
    """Record a skipped approval gate as a `decision`, visibly.

    A `decision` and not a `gap`: nothing is missing, the system made a
    choice on the user's behalf. It uses the M2 producer-event vocabulary
    (topic/choice/rationale/alternatives), so it lands in the same decision
    trace every other cross-subtask choice does.

    The rule this obeys is P6.7's, restated: a gate that silently does not
    happen is speed bought by hiding something. If the system decides not to
    ask, the reader must be able to see that it decided, and why.

    Best-effort — a telemetry failure must never block dispatch.
    """
    try:
        from okuro.sense.task_events import append_event

        append_event(
            task_id=subtask_task.id,
            subtask_id=subtask.id,
            from_role="orchestrator",
            event_type="decision",
            body={
                "topic": f"approval gate for {subtask.id}",
                "choice": "ran without asking",
                "rationale": (
                    f"scored {subtask.risk} risk, but declares no output outside "
                    "its own task folder — there is no change to approve"
                ),
                "alternatives": ["ask for approval as the risk score implies"],
            },
            confidence=0.9,
            created_by="orchestrator",
        )
    except Exception as exc:  # pragma: no cover — telemetry must not block
        logger.debug("no-gate decision event failed: %r", exc)


def _writes_outside_its_workspace(subtask: Subtask, task: Task,
                                  config: Config) -> bool:
    """P2.2 — does this subtask write anything outside its own task folder?

    THE DECISION THIS REPLACES. Approval used to gate on a risk SCORE, so a
    read-only comprehension step scored MED and parked for two days waiting
    on a human. Approval exists to protect against CHANGES; a step that
    changes nothing outside its own workspace has nothing to protect.

    "Read-only" turned out to be the wrong property to chase — there is no
    trustworthy signal for it. Measured 2026-08-01: the role catalogue's
    `domain: research` is not one (researcher.yaml grants `filesystem` and
    `tm-launcher`), and `load_role_index` returns `tools=None` for all 93
    roles, so tool grants are not visible at plan time either.

    BLAST RADIUS is visible. Since P5.7 the decomposer requires
    outputs/target_paths on build-shaped subtasks at plan time, so what a
    subtask intends to write is declared, checkable, and exactly what
    approval protects.

    THE UNKNOWN CASE, stated plainly because it is where this could go wrong.
    A subtask that declares nothing is ambiguous: for a researcher that is
    normal and correct, and for an engineer it is the plan defect P5.7
    already warns about. So the role shape breaks the tie — build-shaped and
    silent still asks; anything else does not. A subtask that lies about its
    outputs defeats this, and that is a real limitation, not a covered case.

    Returns True when the gate should be kept.
    """
    declared: list[str] = []
    for src in (getattr(subtask, "outputs", None),
                getattr(subtask, "target_paths", None)):
        if isinstance(src, (list, tuple)):
            declared.extend(str(x) for x in src if isinstance(x, str) and x.strip())

    if not declared:
        return _is_build_shaped_role(getattr(subtask, "role", ""))

    workspace = (Path(config.tasks_dir) / task.id).resolve()
    project_path = str(getattr(task, "project_path", "") or "").strip()
    for raw in declared:
        candidate = Path(raw.strip())
        # A relative path belongs to the task's own workspace — that is where
        # a subagent's cwd puts it, and where the preview proposer looks.
        if not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            return True  # unresolvable: treat as outside, ask
        if resolved == workspace or workspace in resolved.parents:
            continue
        # The task's declared project_path is the user's OWN target for this
        # task. Writing there is the point of the task, and it is exactly
        # what a human should still be asked about.
        if project_path:
            return True
        return True
    return False


def wait_for_approval(subtask: Subtask, task: Task, config: Config) -> bool:
    """Park the task on a HUMAN approval — set awaiting, do NOT poll.

    Park = engineless. The prior implementation polled plan.yaml every 2s
    until the user approved/skipped/rejected, keeping a live process alive
    on a human gate. That process dies on reboot / orchestrator-restart /
    source-change and leaves a stale lease the reconciler respawns
    ("engine not responding — auto-restarted"). Instead: set the subtask to
    waiting_approval and persist a ``subtask_approval`` ``awaiting`` block so
    the snapshot lifecycle reads waiting_user (engine_state=idle, no false
    wedge). The caller ``continue``s; the work loop's top-of-loop human-park
    guard then exits the engine cleanly ONCE in-flight futures have drained
    (so parallel siblings aren't abandoned mid-run). POST
    /api/tasks/{id}/approve (or /approve-batch) clears the awaiting and
    respawns a fresh --resume engine that re-evaluates the approved subtask.

    Returns True so the dispatch loop skips dispatching THIS subtask now.
    """
    # F2 autopilot — auto-answer the approval gate from the profile instead of
    # parking. "approve" ⇒ let the loop dispatch this subtask now (return
    # False); "skip" ⇒ mark it skipped + skip it (return True). Abstain / low
    # confidence falls through to the human-park flow below.
    try:
        from okuro.orchestrator.autopilot import autopilot_gate as _autopilot_gate
        _chosen = _autopilot_gate(
            config, task, config.tasks_dir,
            kind="subtask_approval",
            options=["approve", "skip"],
            payload={
                "subtask_id": subtask.id,
                "risk": subtask.risk,
                "description": subtask.description,
                "message": (
                    f"Approve {subtask.id} (risk {subtask.risk}): "
                    f"{subtask.description}"
                ),
            },
            ctx={"subtask_id": subtask.id},
        )
        if _chosen == "approve":
            update_subtask(task.id, subtask.id, {"status": "approved"}, config.tasks_dir)
            print(f"[AUTOPILOT] approved {subtask.id} (risk {subtask.risk})")
            return False
        if _chosen == "skip":
            update_subtask(task.id, subtask.id, {"status": "skipped"}, config.tasks_dir)
            print(f"[AUTOPILOT] skipped {subtask.id} (risk {subtask.risk})")
            return True
    except Exception as exc:
        logger.warning("autopilot subtask_approval resolve failed: %r", exc)

    update_subtask(task.id, subtask.id, {"status": "waiting_approval"}, config.tasks_dir)
    print(f"\n[WAITING] {subtask.id} requires approval (risk: {subtask.risk})")
    print(f"  Description: {subtask.description}")
    print(f"  Approve via web UI or: POST /api/tasks/{task.id}/approve")

    try:
        from okuro.orchestrator.state import set_awaiting as _set_awaiting
        from okuro.orchestrator.gate_messages import humanize_gate as _hg
        _gm = _hg("subtask_approval", "", {
            "subtask_id": subtask.id,
            "risk": subtask.risk,
            "description": subtask.description,
            # Named work + named deliverable. Without these the card can only
            # say "this step", which tells the reader nothing about what they
            # are approving or what skipping costs them.
            "role": getattr(subtask, "role", "") or "",
            "outputs": list(getattr(subtask, "outputs", []) or []),
            "options": ["approve", "skip"],
        })
        _set_awaiting(
            task, config.tasks_dir,
            kind="subtask_approval",
            message=_gm.to_message(),
            endpoint=f"/api/tasks/{task.id}/approve",
            payload={
                "subtask_id": subtask.id,
                "risk": subtask.risk,
                "presentation": _gm.to_presentation(),
            },
        )
        _emit_orchestrator_state(
            task, state="idle", label="", tasks_dir=config.tasks_dir,
        )
    except Exception as exc:
        logger.warning("subtask_approval set_awaiting failed: %r", exc)

    print(
        f"[PARKED] {subtask.id} awaiting approval — engine will exit once "
        "in-flight work drains; approve via API to resume"
    )
    return True


def _flip_phase_to_blocked_review_for_session_loop(
    subtask: Subtask, task: Task, result: dict, config: Config, *,
    verdict: str,
) -> None:
    """Theme F — terminal session-loop verdict (CAP / NEEDS_USER) routes
    to blocked_review.

    Pre-fix: session-loop hit max_rounds CAP → engine retries hit
    max_retries → task flipped to ``blocked`` but phase.status stayed
    ``pending``. POST /override-blocked-review returned 409 "expected
    blocked_review". The retry ladder also burned time on the
    permanent-fail path that the subagent had already declared
    unreachable.

    Behaviour now: mark subtask failed (terminal), flip the owning
    phase to blocked_review, write the blocked_review awaiting block,
    emit the canonical phase_blocked_review event. Matches the
    deterministic-check path's contract so the existing
    /override-blocked-review endpoint accepts the resolve.
    """
    from okuro.orchestrator.state import set_awaiting, set_phase_status

    # Find the phase that owns this subtask.
    owning_phase = None
    for phase in task.phases:
        if any(st.id == subtask.id for st in phase.subtasks):
            owning_phase = phase
            break

    err = result.get("error") or f"session-loop {verdict}"

    # F2 autopilot — answer the blocked_review gate from the profile instead of
    # parking. "override" ⇒ accept the capped work as-is + advance the phase;
    # "skip" ⇒ skip the subtask + advance. "retry" is intentionally NOT applied
    # here: this path is only reached AFTER the retry budget is exhausted, so a
    # re-run is what already failed — the guard maps it to abstain → park.
    # Abstain / low confidence also falls through to the human-park flow below.
    if owning_phase is not None:
        try:
            from okuro.orchestrator.autopilot import autopilot_gate as _autopilot_gate
            from okuro.orchestrator.state import set_phase_status as _set_phase_status
            _chosen = _autopilot_gate(
                config, task, config.tasks_dir,
                kind="blocked_review",
                options=["override", "skip"],
                payload={
                    "phase_id": owning_phase.id,
                    "subtask_id": subtask.id,
                    "verdict": verdict,
                    "reason": err[:500],
                    "message": (
                        f"{_step(owning_phase.id)} blocked ({verdict}) on "
                        f"{_part(subtask.id)}: {err[:300]}"
                    ),
                },
                ctx={"phase_id": owning_phase.id, "subtask_id": subtask.id},
            )
            if _chosen in ("override", "skip"):
                _new_status = "done" if _chosen == "override" else "skipped"
                _note = (
                    "[autopilot: reviewer FAIL accepted, deliverable retained]"
                    if _chosen == "override"
                    else "[autopilot: subtask skipped, task continued]"
                )
                _completed = datetime.utcnow().isoformat()
                update_subtask(task.id, subtask.id, {
                    "status": _new_status,
                    "review_state": "overridden",
                    "output_summary": _note,
                    "completed_at": _completed,
                }, config.tasks_dir)
                subtask.status = _new_status
                subtask.review_state = "overridden"
                subtask.completed_at = _completed
                try:
                    _fresh = load_task(task.id, config.tasks_dir)
                    _fp = next(
                        (p for p in _fresh.phases
                         if any(st.id == subtask.id for st in p.subtasks)),
                        None,
                    )
                    if _fp is not None and all(
                        st.status in ("done", "skipped", "failed")
                        for st in _fp.subtasks
                    ):
                        _set_phase_status(_fresh, _fp, "done", config.tasks_dir)
                except Exception as exc:
                    logger.warning("autopilot blocked_review advance failed: %r", exc)
                print(
                    f"[AUTOPILOT] blocked_review phase {owning_phase.id} → "
                    f"{_chosen} ({subtask.id})"
                )
                return
        except Exception as exc:
            logger.warning("autopilot blocked_review resolve failed: %r", exc)

    print(
        f"\n[SESSION-LOOP-{verdict}] {subtask.id} — phase → blocked_review"
    )

    # Stop re-dispatch until the user resolves the blocked_review block — but
    # WITHOUT lying about the work. If the subagent shipped a deliverable, the
    # work succeeded and only the REVIEW is unresolved: status stays `done` and
    # review_state carries the verdict. The dispatch guard reads review_state,
    # so the lock no longer needs to borrow `status` to do its job.
    #
    # Pre-fix this wrote status="failed" unconditionally, with the comment
    # "so the dispatch guard refuses re-dispatch" — a dispatch concern
    # overwriting a work-outcome field. Everything downstream then read a
    # shipped subtask as failed: dependents were blocked, the override endpoint
    # discarded real artifacts, and the run reported hollow phases as done.
    #
    # Mirror to the in-memory subtask so the subsequent set_phase_status
    # (which calls save_plan with the in-memory task) doesn't write the
    # stale 'pending' status back over the disk update.
    completed_at = datetime.utcnow().isoformat()
    shipped = _has_brain_artifact(task.id, subtask.id)
    new_status = "done" if shipped else "failed"
    update_subtask(task.id, subtask.id, {
        "status": new_status,
        "review_state": "failed",
        "error": err,
        "completed_at": completed_at,
    }, config.tasks_dir)
    subtask.status = new_status
    subtask.review_state = "failed"
    subtask.error = err
    subtask.completed_at = completed_at
    append_log(task.id, {
        # A shipped subtask whose review could not settle is NOT a permanent
        # subtask failure — name the event for what it is so the log stops
        # asserting something the plan no longer says.
        "type": "subtask_review_unresolved" if shipped else "subtask_failed_permanently",
        "subtask": subtask.id,
        "error": err,
        "shipped_deliverable": shipped,
        "retries": int(getattr(subtask, "retries", 0) or 0),
    }, config.tasks_dir)

    if owning_phase is None:
        logger.warning(
            "[%s] Theme F: subtask %s not found on any phase — "
            "blocked_review skipped, task may still wedge",
            task.id, subtask.id,
        )
        return

    # CLOBBER GUARD — set_phase_status calls save_plan(task), which serializes
    # the FULL in-memory task (every asdict(subtask)). The work loop reloads
    # `task` from disk each iteration (engine.py: `task = load_task()`), so the
    # active_futures subtask object handed to this function is a DIFFERENT
    # instance than `task.phases[...]`; the mirror above (`subtask.status =
    # "failed"`) touches the future's object, NOT the one save_plan writes. The
    # live `task.phases` copy of this subtask was loaded BEFORE the targeted
    # update_subtask(failed) above, so it still reads 'running' — and save_plan
    # would overwrite the disk 'failed' back to 'running', orphaning the subtask
    # (running with no engine → no_subtasks_ready deadlock). Re-load from disk so
    # save_plan serializes the targeted write instead of clobbering it, and
    # re-resolve the owning phase on the fresh snapshot.
    try:
        fresh_task = load_task(task.id, config.tasks_dir)
        fresh_phase = next(
            (p for p in fresh_task.phases
             if any(st.id == subtask.id for st in p.subtasks)),
            None,
        )
        if fresh_phase is None:
            fresh_task, fresh_phase = task, owning_phase
    except Exception as exc:
        # Reload is best-effort: if disk can't be read, fall back to the
        # in-memory task. Crashing here would skip the phase flip entirely,
        # leaving phase.status != 'blocked_review' → /override-blocked-review
        # 409s, which is the dead-end this whole path exists to prevent.
        logger.warning("[%s] Theme F: clobber-guard reload failed %r — "
                       "using in-memory task", task.id, exc)
        fresh_task, fresh_phase = task, owning_phase
    try:
        set_phase_status(
            fresh_task, fresh_phase, "blocked_review", config.tasks_dir,
        )
    except Exception as exc:
        logger.warning(
            "[%s] Theme F: set_phase_status raised %r — "
            "phase still pending, /override-blocked-review will 409",
            task.id, exc,
        )

    append_log(task.id, {
        "type": "phase_blocked_review",
        "phase_id": owning_phase.id,
        "verdict": verdict,
        "subtask": subtask.id,
        "rounds": int(result.get("rounds", 0) or 0),
        "source": "session_loop",
    }, config.tasks_dir)
    # Cause-aware user surface — a transient API/server error and a reviewer
    # FAIL are DIFFERENT problems with DIFFERENT fixes, and the block must say
    # which one it is + what to do. Pre-fix both read "session-loop <verdict>
    # after N review round(s)", so an API 500 looked like a reviewer problem.
    _rounds = int(result.get("rounds", 0) or 0)
    _reason = (result.get("error") or "").strip()
    _is_infra = (verdict == "INFRA") or bool(result.get("infra_error"))
    _is_decision = verdict == "NEEDS_USER"
    _is_subagent_fail = verdict == "SUBAGENT_FAIL"
    _max = int(getattr(getattr(config, "execution", None), "max_retries", 0) or 0)
    # Coarse cause bucket → plain-language surface. The raw reviewer/error string
    # (_reason) is engineer diagnostics; it is quarantined into the gate's
    # technical_details and NEVER spliced into the sentence the user reads.
    if _is_infra:
        cause = "infra"
    elif _is_decision:
        cause = "decision"
    elif _is_subagent_fail:
        cause = "subagent_fail"
    else:
        cause = "reviewer"
    from okuro.orchestrator.gate_messages import humanize_gate
    _gate_msg = humanize_gate(
        "blocked_review", cause,
        {
            "phase_id": owning_phase.id,
            "subtask_id": subtask.id,
            "rounds": _rounds,
            "attempts": _max + 1,
            "raw_reason": _reason,
        },
    )
    _message = _gate_msg.to_message()
    _options = _gate_msg.options
    append_log(task.id, {
        "type": "orchestrator_state",
        # ROCK-SOLID v5 P1.1 — healthy-but-waiting, not a failure.
        "state": "waiting",
        "label": (
            f"{_step(owning_phase.id)} blocked — "
            + ("transient API error" if _is_infra
               else "the part failed" if _is_subagent_fail
               else f"reviewer {verdict}")
            + f" on {_part(subtask.id)}"
        ),
    }, config.tasks_dir)

    try:
        set_awaiting(
            task, config.tasks_dir,
            kind="blocked_review",
            message=_message,
            endpoint=(
                f"/api/tasks/{task.id}/phases/{owning_phase.id}"
                "/override-blocked-review"
            ),
            payload={
                "phase_id": owning_phase.id,
                "capped_subtasks": [subtask.id],
                "verdict": verdict,
                "cause": cause,
                "reason": _reason[:500],
                "recommended_action": "retry",
                "options": _options,
                # Structured plain-language surface for the UI: headline +
                # explanation + action + collapsed technical_details. The flat
                # `message` above stays in sync (gm.to_message()) for consumers
                # that don't read this yet.
                "presentation": _gate_msg.to_presentation(),
                "source": "session_loop",
                "rounds": _rounds,
                # Gap #1 — carry the reviewer's structured findings into the
                # blocked_review payload so the user-facing retry can build a
                # surgical worklist (dispatcher reads awaiting.payload
                # .critic_findings). Pre-fix this path wrote NO findings, so
                # retry re-ran blind and re-failed identically; the live
                # blocked task had to be hand-stamped from .activity.jsonl.
                # dispatcher_streaming stashes the last round's findings on
                # result["critic_findings"].
                "critic_findings": result.get("critic_findings") or [],
                # Same phrasing pass as the phase gate — one question the user
                # can answer, instead of the reviewer's raw finding list.
                **_brief_payload(
                    result.get("critic_findings") or [], task, [subtask.id],
                ),
            },
        )
        _emit_orchestrator_state(
            task, state="waiting",
            label=f"{_step(owning_phase.id)} blocked — needs you",
            tasks_dir=config.tasks_dir,
        )
    except Exception as exc:
        logger.warning(
            "[%s] Theme F: set_awaiting raised %r — block UI may not surface",
            task.id, exc,
        )


def handle_failure(subtask: Subtask, task: Task, result: dict, config: Config):
    # Wave-6 G16 — kill-after-ship recovery. The subagent may have shipped
    # both streams just before the harness killed it (timeout, SIGKILL,
    # post-finalize-but-before-exit-code-0). Pre-fix every such case
    # spent a full retry cycle re-doing already-shipped work, then
    # supersede-on-conflict (G13) deduped silently. Now: detect the case
    # and mark done with an explanatory note instead.
    try:
        ks_brain_ok = _has_brain_artifact(task.id, subtask.id)
        ks_handover_ok, ks_outcome, _ks_brief = _read_handover_outcome(
            task.id, subtask.id,
        )
        if ks_brain_ok and ks_handover_ok and ks_outcome == "success":
            note = (
                f"Subagent self-reported failure ({result.get('error', '?')[:160]}) "
                "but BOTH Stream A and Stream B are present with outcome=success. "
                "Marking done — subagent shipped before the harness killed it (G16)."
            )
            print(f"\n[KILL-AFTER-SHIP] {subtask.id} — {note[:120]}…")
            update_subtask(task.id, subtask.id, {
                "status": "done",
                "completed_at": datetime.utcnow().isoformat(),
                "duration": result.get("duration", 0.0),
                "cli_used": result.get("cli", ""),
                "model_used": result.get("model", ""),
                "output_summary": (result.get("output", "") or "")[:500],
                "error": "",
            }, config.tasks_dir)
            append_log(task.id, {
                "type": "subtask_kill_after_ship",
                "subtask": subtask.id,
                "original_error": result.get("error", "")[:500],
                "note": "marked done; both streams present with outcome=success",
            }, config.tasks_dir)
            return
    except Exception as exc:
        logger.warning("[%s] G16 kill-after-ship check raised %r — falling through to retry",
                        subtask.id, exc)

    print(f"\n[FAILED] {subtask.id} - {result['error']}")

    # Theme F — session-loop terminal verdict (CAP / NEEDS_USER)
    # short-circuits the retry ladder and routes straight to
    # blocked_review. The subagent already declared "this is as far as
    # the session-loop can get" — spinning up retries-then-fail wastes
    # the budget and leaves phase.status='pending' while task.status
    # flips to blocked, breaking the /override-blocked-review contract
    # (which requires phase.status='blocked_review' first).
    final_verdict = (result.get("final_verdict") or "").upper()
    if final_verdict in {"CAP", "NEEDS_USER"}:
        _flip_phase_to_blocked_review_for_session_loop(
            subtask, task, result, config, verdict=final_verdict,
        )
        return

    # Retry invariant (single source of truth — see state.get_ready_subtasks
    # and deliberation.is_dispatchable): a subtask gets ``max_retries``
    # CORRECTION attempts after its first run = ``max_retries + 1`` total
    # attempts. ``retries`` is 0 on the first run and incremented on each
    # FAIL. handle_failure resets to ``pending`` while ``retries <
    # max_retries`` (so the last reset is to ``retries == max_retries``);
    # the scheduler dispatches that final attempt (it allows
    # ``retries <= max_retries``) and the NEXT failure lands here with
    # ``retries == max_retries`` → the cap branch fires and escalates.
    if subtask.retries < config.execution.max_retries:
        print(f"[RETRY] Attempt {subtask.retries + 1}/{config.execution.max_retries}")
        # P1 — CARRY the last round's critic findings into the next dispatch so
        # the retry VERIFIES against them (surgical fix) instead of re-hunting
        # from scratch (audit defect #2 — the non-convergence root). Pre-fix this
        # reset wrote only {status,retries,error}, dropping review_findings, so
        # the re-dispatch re-seeded prev_findings=[] (dispatcher_streaming.py:409)
        # → verification_block='' → a fresh adversarial pass each round → the
        # observed 2→5 load_bearing climb. The session loop already surfaces the
        # freshest finding set as result["critic_findings"] (dispatcher_streaming
        # .py:903). Persist it (only when non-empty — an empty set must PRESERVE
        # any worklist already stamped, never blank it, matching _retry_reset_phase).
        _patch = {
            "status": "pending",
            "retries": subtask.retries + 1,
            "error": result["error"],
        }
        _carry_findings = list(result.get("critic_findings") or [])
        if _carry_findings:
            _patch["review_findings"] = _carry_findings
        update_subtask(task.id, subtask.id, _patch, config.tasks_dir)
        return

    # ------------------------------------------------------------------
    # Retries-cap branch — timeout cause routes to ``timeout_cap``
    # awaiting; non-timeout causes route to permanent-fail + cascade-skip
    # as before. Pre-fix every retries-cap hit went silently to the
    # permanent-fail path even when the cause was "subagent needed a
    # longer wall clock" — the user had no surface to extend, retry,
    # mark done, or skip.
    # ------------------------------------------------------------------
    raw_err = (result.get("error") or "")
    is_timeout = (
        raw_err.startswith("Timed out")
        or "timeout" in raw_err.lower()
        or "timed out" in raw_err.lower()
    )
    if is_timeout:
        # Resolve the timeout that *would* have been applied next so the
        # UI can show "timed out N× at Ms" honestly. Falls back to
        # config.execution.subtask_timeout if the per-role helper is
        # unimportable for some reason (defensive).
        try:
            from okuro.orchestrator.dispatcher import _wall_clock_seconds
            timeout_budget = _wall_clock_seconds(subtask.role or "", subtask.retries)
        except Exception:
            timeout_budget = getattr(config.execution, "subtask_timeout", 600)
        attempts = config.execution.max_retries + 1
        from okuro.orchestrator.gate_messages import humanize_gate as _hg
        _gm = _hg("timeout_cap", "", {
            "phase_id": getattr(subtask, "phase_id", "?"),
            "subtask_id": subtask.id,
            "seconds": int(timeout_budget),
            "raw_reason": f"{subtask.role} timed out {attempts}x at {timeout_budget}s",
        })
        message = _gm.to_message()
        # F2 autopilot — auto-resolve the timeout cap in-process; else park.
        if _autopilot_resolve_timeout_cap(
            task, config, subtask, int(timeout_budget), raw_err,
        ):
            return
        try:
            from okuro.orchestrator.state import set_awaiting as _set_awaiting
            _set_awaiting(
                task, config.tasks_dir,
                kind="timeout_cap",
                message=message,
                endpoint=(
                    f"/api/tasks/{task.id}/subtasks/{subtask.id}/timeout-cap-resolve"
                ),
                payload={
                    "subtask_id": subtask.id,
                    "role": subtask.role,
                    "retries": subtask.retries,
                    "max_retries": config.execution.max_retries,
                    "last_timeout_s": int(timeout_budget),
                    "last_error": raw_err[:500],
                    "options": ["extend_and_retry", "mark_done", "skip", "permanent_fail"],
                    "presentation": _gm.to_presentation(),
                },
            )
        except Exception as exc:
            logger.warning("timeout_cap set_awaiting failed: %r", exc)
        append_log(task.id, {
            "type": "subtask_timeout_cap",
            "subtask": subtask.id,
            "role": subtask.role,
            "retries": subtask.retries,
            "max_retries": config.execution.max_retries,
            "last_timeout_s": int(timeout_budget),
            "error": raw_err[:500],
        }, config.tasks_dir)
        try:
            _emit_orchestrator_state(
                task, state="waiting",
                label=f"{_part(subtask.id).capitalize()} timed out — needs you",
                tasks_dir=config.tasks_dir,
            )
        except Exception as exc:
            logger.warning("timeout_cap orchestrator_state emit failed: %r", exc)
        print(
            f"\n[TIMEOUT-CAP] {subtask.id} hit retries cap on timeouts — "
            f"user resolution required."
        )
        # DO NOT mark status=failed. The subtask stays pending; C5
        # dispatch guard refuses re-dispatch because awaiting.kind is in
        # _HALT_AWAITING_KINDS. User decision via the resolve endpoint
        # picks the next state.
        return

    # Transient infra / API errors (server-side 5xx, overloaded, rate-limit)
    # must NOT permanent-fail + cascade-skip a whole subtree over a server
    # blip. Park the owning phase in blocked_review (recoverable via
    # /override-blocked-review action=retry) so the work resumes when the API
    # is healthy. Mirrors the Critic-stage infra_error contract + the
    # timeout_cap recoverable routing above. (task-20260616: subtask 2.1 died
    # 3x on "API Error: 500 Internal server error" and cascade-blocked the task.)
    is_infra = bool(result.get("infra_error")) or is_transient_infra_error(raw_err)
    if is_infra:
        print(
            f"[INFRA-CAP] {subtask.id} — transient infra/API error after "
            f"{config.execution.max_retries + 1} attempts; phase → "
            f"blocked_review (recoverable, no cascade-skip)"
        )
        _flip_phase_to_blocked_review_for_session_loop(
            subtask, task, result, config, verdict="INFRA",
        )
        return

    print(f"[MAX RETRIES] {subtask.id} failed after {config.execution.max_retries} attempts")
    update_subtask(task.id, subtask.id, {
        "status": "failed",
        "error": result["error"],
        "completed_at": datetime.utcnow().isoformat(),
    }, config.tasks_dir)
    notify_channel("task_failed", task.id, task.description,
                   error=f"Subtask {subtask.id} failed after {config.execution.max_retries} retries: {result['error']}")

    # Dependents are NOT written to disk here. `failed` is in FINISHED_STATUSES
    # (not SATISFIED_STATUSES), so the phase barrier and completion check clear
    # while dependency resolution still refuses — the dependents simply stay
    # `pending` and are reported blocked via state.blocked_upstream_ids, which
    # is re-derived every loop. Retrying this subtask green therefore revives
    # the whole subtree with no revive step to forget.
    #
    # This replaces Wave-2 G4 cascade-skip (+ its fallback sweep), which wrote
    # `skipped` onto every transitive dependent. That write was permanent:
    # nothing reverted it when the trigger was retried successfully, so one
    # transient failure silently killed every later phase for the rest of the
    # run (task-20260724-110216 — 4.3 failed, was retried green, and phases
    # 5-6 stayed skipped anyway).
    blocked_now = blocked_upstream_ids(load_task(task.id, config.tasks_dir))
    if blocked_now:
        print(
            f"[BLOCKED-UPSTREAM] {len(blocked_now)} dependent subtask(s) "
            f"held pending behind {subtask.id}: {', '.join(sorted(blocked_now))}"
        )
        append_log(task.id, {
            "type": "subtasks_blocked_upstream",
            "trigger": subtask.id,
            "blocked_count": len(blocked_now),
            "blocked_ids": sorted(blocked_now),
            "note": "derived, not persisted — retrying the trigger revives these",
        }, config.tasks_dir)

    # P0 — a plain permanent content/subagent failure previously left the
    # owning phase `pending` with a `failed` subtask: is_task_complete blocks
    # forever (failed ∉ TERMINAL_STATUSES) and the engine's no-ready fallback
    # parked the task in waiting_user behind an /override-blocked-review CTA
    # that 409'd (the raw error carries no review-cap marker). The task wedged
    # non-terminal with no working CTA (task-20260618-175644 class). Escalate
    # the owning phase to blocked_review so the user is HONESTLY asked: Retry
    # the failed subtask, or Override to accept the failure and continue. Both
    # the override and retry endpoints resolve a blocked_review phase, and
    # _override_accept_capped_subtasks now terminalizes the failed subtask so
    # the task can finish.
    _flip_phase_to_blocked_review_for_session_loop(
        subtask, task, result, config, verdict="SUBAGENT_FAIL",
    )


def print_plan(task: Task):
    print("=" * 70)
    print("TASK PLAN")
    print("=" * 70)
    print(f"Task: {task.description}")
    print(f"Status: {task.status}")
    print()

    for phase in task.phases:
        print(f"PHASE {phase.id}: {phase.name}")
        print("-" * 70)
        for subtask in phase.subtasks:
            icons = {"done": "[v]", "running": "[>]", "failed": "[x]", "skipped": "[-]"}
            icon = icons.get(subtask.status, "[ ]")
            deps = f" (depends: {', '.join(subtask.dependencies)})" if subtask.dependencies else ""
            print(f"  {icon} {subtask.id:<6} {subtask.role:<20} | {subtask.description}{deps}")
            print(f"           Risk: {subtask.risk:<4} | Complexity: {subtask.complexity}")
        print()
    print("=" * 70)


def print_summary(task: Task, duration: float, config: Config):
    done_count = sum(1 for p in task.phases for st in p.subtasks if st.status == "done")
    failed_count = sum(1 for p in task.phases for st in p.subtasks if st.status == "failed")
    skipped_count = sum(1 for p in task.phases for st in p.subtasks if st.status == "skipped")

    print()
    print("=" * 70)
    print("TASK SUMMARY")
    print("=" * 70)
    print(f"Task: {task.description}")
    print(f"Status: {task.status}")
    print(f"Duration: {format_duration(duration)}")
    print()
    print(f"Subtasks: {done_count} done, {failed_count} failed, {skipped_count} skipped")
    print()
    print(f"Artifacts: {config.tasks_dir / task.id / 'artifacts'}")
    print(f"Task Directory: {config.tasks_dir / task.id}")
    print("=" * 70)


def _generate_continuation_suggestions(task: Task, config: Config):
    """Generate 3 structured next-step suggestions via workforce-reviewer role.

    Delegates to ``orchestrator.suggestions.generate_continuation_suggestions``
    so the on-demand ``POST /api/tasks/{id}/suggest`` endpoint produces the
    same output shape with the same prompt. Role prompt + output schema
    live in ``src/okuro/roles/catalog/workforce-reviewer.yaml`` and are
    editable without a code change.
    """
    try:
        import yaml
        from okuro.orchestrator.suggestions import (
            generate_continuation_suggestions as _gen,
        )

        artifacts_dir = config.tasks_dir / task.id / "artifacts"
        suggestions = _gen(
            task_id=task.id,
            task_description=task.description,
            phase_count=len(task.phases),
            artifacts_dir=artifacts_dir,
            config=config,
        )

        if suggestions:
            task_yaml_path = config.tasks_dir / task.id / "task.yaml"
            with open(task_yaml_path) as f:
                task_data = yload(f)
            task_data["continuation_suggestions"] = suggestions
            tmp = task_yaml_path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                yaml.dump(task_data, f, default_flow_style=False)
            tmp.rename(task_yaml_path)

            print(f"\n[SUGGEST] {len(suggestions)} continuation suggestion(s)")
            for s in suggestions:
                print(f"  [{s['category']}] {s['suggestion_text'][:80]}")
            # P3.1 — state_change, not telemetry: this row is the only
            # signal that the next-steps panel has something to show.
            from okuro.orchestrator.state import emit_event as _emit
            _emit(task.id, "continuation_suggested", {
                "count": len(suggestions),
                "suggestions": [s["suggestion_text"] for s in suggestions],
            }, config.tasks_dir)

    except Exception as e:
        logger.warning(f"Failed to generate continuation suggestions: {e}")


def list_tasks(config: Config):
    tasks_dir = config.tasks_dir
    if not tasks_dir.exists():
        print(f"No tasks directory found at: {tasks_dir}")
        return

    task_dirs = [d for d in tasks_dir.iterdir() if d.is_dir() and d.name.startswith("task-")]
    if not task_dirs:
        print("No tasks found.")
        return

    print("=" * 70)
    print("EXISTING TASKS")
    print("=" * 70)
    print()

    task_dirs.sort(reverse=True)
    for task_dir in task_dirs:
        try:
            task = load_task(task_dir.name, tasks_dir)
            total = sum(len(p.subtasks) for p in task.phases)
            done = sum(1 for p in task.phases for st in p.subtasks if st.status == "done")
            print(f"ID: {task.id}")
            print(f"Description: {task.description}")
            print(f"Status: {task.status}")
            print(f"Progress: {done}/{total} subtasks")
            print(f"Created: {task.created_at}")
            print()
        except Exception as e:
            print(f"ID: {task_dir.name}")
            print(f"Error: Failed to load task - {e}")
            print()

    print("=" * 70)


def update_task_status(task: Task, tasks_dir: Path):
    """Retired writer — thin compatibility wrapper around ``save_task_meta``.

    Pre-fix (Gate 2 §C1 violation root): this function read task.yaml
    from disk, merged the small set of fields it knew about, and wrote
    back — silently preserving every unknown key including stale
    ``awaiting``. That carried the D6 / RC3 bug: engine cleared
    ``task.awaiting = None`` in memory and flipped ``task.status =
    "active"``, but the disk retained the awaiting block because this
    writer never serialized it. FE saw "WAITING ON YOU header + agent
    activity".

    Post-fix: every call delegates to ``save_task_meta`` — the canonical
    closed-schema writer that takes the shared task-dir lock and
    serializes the FULL in-memory ``Task`` (awaiting included). Callers
    that previously did ``task.status = X; update_task_status(task, ...)``
    keep working unchanged; the silent-key-preservation behaviour is
    structurally removed, not patched.

    The ``status_changed`` log event is still appended so subscribers
    that gated on this writer don't regress.
    """
    from okuro.orchestrator.state import save_task_meta

    save_task_meta(task, tasks_dir)
    append_log(task.id, {"type": "status_changed", "status": task.status}, tasks_dir)


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h}h {m}m {s}s"


# ── Brain integration (direct imports, no subprocess) ──────────────────────


def _brain_session_start(task_id: str, description: str, cli_short: str = "cc") -> Optional[str]:
    """Register orchestrator as an active session."""
    try:
        from okuro.telemetry.logger import write_bootstrap_marker
        session_id = write_bootstrap_marker(
            provider=f"orchestrator({cli_short})",
            task_hint=description[:100],
        )
        return session_id
    except Exception as e:
        logger.warning(f"Failed to start brain session for {task_id}: {e}")
        return None


def _brain_session_end(session_id: Optional[str], task=None):
    """End brain session with tool ratings derived from subtask outcomes."""
    if not session_id:
        return
    try:
        from okuro.telemetry.logger import mark_session_reported
        mark_session_reported()
    except Exception as e:
        logger.warning(f"Failed to end brain session {session_id}: {e}")


def _log_to_brain(task, subtask, result):
    """Log subtask completion to sense for cross-session memory."""
    try:
        from okuro.sense.progress import log_progress
        log_progress(
            project="orchestrator",
            status="implementing",
            summary=f"{subtask.id} ({subtask.role}/{result.get('model', '?')}): {subtask.description[:80]}",
            files_touched=list(result.get("files", [])),
        )
    except Exception as e:
        logger.warning(f"Failed to log to brain for {subtask.id}: {e}")


def _harvest_learnings(task, config: Config):
    """Post-task knowledge harvest via okuro.sense.memory."""
    if not config.sense.enabled or not config.sense.harvest:
        return

    try:
        from okuro.sense.memory import read_memory, write_memory

        raw = read_memory(project="orchestrator", limit=20)
        if not raw:
            return

        # Extract learnings from memory results
        learnings = []
        entries = raw if isinstance(raw, list) else []
        for entry in entries:
            content = entry.get("content", "") if isinstance(entry, dict) else str(entry)
            for marker in ("gotcha:", "convention:", "learning:", "decision:"):
                if marker in content.lower():
                    learnings.append(content)
                    break

        if not learnings:
            return

        roles_used = set()
        for phase in task.phases:
            for st in phase.subtasks:
                if st.status == "done" and st.role:
                    roles_used.add(st.role)

        for role in roles_used:
            written = 0
            for learning in learnings:
                topic = "learning"
                lower = learning.lower()
                if "gotcha:" in lower:
                    topic = "gotcha"
                elif "convention:" in lower:
                    topic = "convention"
                elif "decision:" in lower:
                    topic = "decision"

                try:
                    write_memory(
                        topic=topic,
                        content=learning,
                        project="orchestrator",
                        role=role,
                    )
                    written += 1
                except Exception as e:
                    logger.warning(f"write_memory failed for role {role}: {e}")
            if written:
                logger.info(f"Harvested {written} learnings for role {role}")

    except Exception as e:
        logger.warning(f"Knowledge harvest failed for {task.id}: {e}")




if __name__ == "__main__":
    try:
        import setproctitle
        setproctitle.setproctitle("okuro-orchestrator")
    except ImportError:
        pass
    main()
