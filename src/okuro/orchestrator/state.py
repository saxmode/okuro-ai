# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Okuro Orchestrator State Management
# index:
#   imports
#   imports
#   def _index_task_artifacts
#   def _atomic_write
#   class Subtask
#   class Phase
#   class Assignment
#   class DAGNode
#   class DAGEdge
#   class DAGGraph
#   class Deliberation
#   class Task
#   def create_task
#   def save_plan
#   def _serialize_graph
#   def _deserialize_graph
#   def load_task
#   def update_node
#   def save_task_meta
#   def update_subtask
#   def get_next_subtask
#   def get_ready_subtasks
#   def is_phase_complete
#   def is_task_complete
#   def apply_supersedes
#   def append_log
#   def rename_task
#   def append_phases
#   def save_artifact
#   def validate_and_warn
# AGENT_HEADER_END -->
"""
Okuro Orchestrator State Management

File-based task state with human-readable YAML/JSONL formats.
Every state change is logged for full auditability.
"""

from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from typing import Optional
from datetime import datetime
from okuro.clock import utc_now_naive
try:
    import fcntl  # POSIX (Linux, macOS)
except ImportError:  # Windows — no file locking, single-writer assumption
    fcntl = None
import logging
import os
import tempfile
import yaml
import json

from okuro.orchestrator.validation import _validate_status_write
from okuro.orchestrator.yamlfast import FastLoader, yload

logger = logging.getLogger("okuro.orchestrator.state")

# libyaml-backed loader — identical SafeLoader semantics, measured 13x faster
# on this corpus (673 task.yaml/plan.yaml, 4.78 MB: 2.59s pure-python vs
# 0.20s C). load_task() is the hottest reader in the process — the doctor's
# artifact check, /api/active-roles and the task-list endpoint all funnel
# through it once per task directory, and N grows by at least one dir a day
# forever. The definition lives in orchestrator/yamlfast.py so there is one
# loader for the package, not a constant copied per module; the local names
# stay so existing call sites and the equivalence test read unchanged.
_FastLoader = FastLoader
_yload = yload


_VALIDATION_WARNING_SEEN: dict[str, set[str]] = {}


# ---------------------------------------------------------------------------
# C7 — Typed event primitive + closed allowlist.
#
# Every emitter that writes to ``log.jsonl`` and changes consumer-visible
# state goes through ``emit_event(..., pool="state_change")``. Telemetry-only
# log lines (engine_version, verify_passed, gap, ...) declare pool="telemetry".
#
# At module import time we lazily fetch the literal ``_STATE_CHANGE_EVENTS``
# frozenset from api/main.py (the test harness extracts the literal by regex
# at that exact site, so it has to stay literal there). We then enforce that
# every state-change emission targets an event type listed in the allowlist
# — a developer that adds a new emitter without registering it gets a
# ``ValueError`` instead of a silent FE invalidation hole.
#
# Closes Gate 2 §C7 (V4 + D15) by construction.
# ---------------------------------------------------------------------------

# Events that are written to log.jsonl but intentionally NOT a state-change
# (engine/internal telemetry). Must match the test harness's hardcoded
# exemption set so adding to one without the other is a loud failure.
TELEMETRY_ONLY_EVENTS: frozenset[str] = frozenset({
    "engine_version", "compression_emitted",
    "verify_passed", "verify_failed",
    "reconciler_discrepancy",
    "subtasks_blocked_upstream",  # derived observability, not a state change
    "project_path_auto_inferred",
    "capabilities_harvested",
    "orchestrator_state",  # routed to /ws/activity, not state pool
    "gap",  # M2 producer-event trace
    # M3 reviewer activity markers — engine writes one row per real
    # subtask into .activity.jsonl when a phase enters / exits review.
    # They feed the per-subtask activity panel only (no state mutation,
    # no log.jsonl emit, no snapshot invalidation). Listed here so the
    # C7 source-scan invariant counts them as telemetry, not silent state.
    "review_starting", "review_complete",
})

_STATE_CHANGE_EVENTS_CACHE: Optional[frozenset[str]] = None


def _load_state_change_allowlist() -> frozenset[str]:
    """Lazily import the allowlist literal from api/main.py.

    api/main.py owns the literal frozenset surface because the Gate 4 test
    extracts it by regex from that exact file. state.py imports it here so
    runtime enforcement and the test contract share a single source.
    """
    global _STATE_CHANGE_EVENTS_CACHE
    if _STATE_CHANGE_EVENTS_CACHE is not None:
        return _STATE_CHANGE_EVENTS_CACHE
    try:
        from okuro.orchestrator.api.main import _STATE_CHANGE_EVENTS as allowlist
    except Exception as exc:  # pragma: no cover — import-order edge case
        logger.warning("could not load _STATE_CHANGE_EVENTS allowlist: %r", exc)
        return frozenset()
    _STATE_CHANGE_EVENTS_CACHE = frozenset(allowlist)
    return _STATE_CHANGE_EVENTS_CACHE


def emit_event(
    task_id: str,
    event_type: str,
    payload: dict,
    tasks_dir: Path,
    *,
    pool: str = "state_change",
):
    """Single primitive for every log.jsonl write.

    pool="state_change" — event must be in api/main.py _STATE_CHANGE_EVENTS;
    pool="telemetry"    — event must be in TELEMETRY_ONLY_EVENTS;
    pool="activity"     — event is intended for /ws/activity broadcasts only.

    The literal frozenset in api/main.py is the closed allowlist. Adding a
    new emitter without listing its type in the allowlist (or the telemetry
    set above) raises ValueError at call time.
    """
    if pool == "state_change":
        allowlist = _load_state_change_allowlist()
        if allowlist and event_type not in allowlist:
            raise ValueError(
                f"emit_event: '{event_type}' is not in _STATE_CHANGE_EVENTS allowlist. "
                f"Add it to api/main.py:_STATE_CHANGE_EVENTS or use pool='telemetry'."
            )
    elif pool == "telemetry":
        if event_type not in TELEMETRY_ONLY_EVENTS:
            raise ValueError(
                f"emit_event: telemetry event '{event_type}' must be listed in "
                f"state.TELEMETRY_ONLY_EVENTS."
            )
    elif pool != "activity":
        raise ValueError(f"emit_event: unknown pool {pool!r}")

    record = dict(payload or {})
    record["type"] = event_type
    append_log(task_id, record, tasks_dir)

    # C9 — invalidate the cached snapshot on every state-change emission.
    # The C9 invariant is: snapshot is cached, invalidated only on a
    # consumer-visible mutation. Telemetry-only events do NOT invalidate.
    # Lazy import dodges the state.py ↔ state_reader.py circular dep.
    if pool == "state_change":
        try:
            from okuro.orchestrator.api.state_reader import invalidate_snapshot_cache
            invalidate_snapshot_cache(task_id)
        except Exception as exc:  # pragma: no cover — import-order edge case
            logger.debug("snapshot cache invalidation skipped: %r", exc)


def set_phase_status(task, phase, new_status: str, tasks_dir: Path):
    """Canonical writer for ``phase.status``.

    Closes the silent phase-transition class (Gate 2 §C7 — engine.py:705 /
    2064 / 2535). The status flip is paired atomically with a
    ``phase_status_changed`` event so any FE consumer that gates on the
    allowlist sees every phase transition.

    Note: ``save_plan`` itself also diffs the on-disk plan and emits
    ``phase_status_changed`` for any phase whose status changed since the
    last serialize — that catches direct ``phase.status = X; save_plan(...)``
    paths that didn't migrate to this helper yet.
    """
    _validate_status_write("phase", new_status)
    phase.status = new_status
    save_plan(task, tasks_dir)


def _index_task_artifacts(
    task_path: Path,
    task_id: str = "",
    tasks_dir: Optional[Path] = None,
):
    """Trigger cortex indexing for a task's artifacts folder.

    Indexes all code files so follow-up subtask agents can discover
    prior work via cortex search. Runs in a background process.

    Wave-6 G14: stderr is now captured to ``{task_path}/.cortex-index.log``
    instead of being silently dropped to DEVNULL. Pre-fix every cortex
    re-index failure was invisible — cortex never re-indexed → next
    subagent's cortex_search_code returned nothing for fresh artifacts.
    Now operators can ``tail .cortex-index.log`` and the sentinel can
    flag stale logs.

    C12 — Gate 2 §C12. Pre-fix, any exception inside this function was
    swallowed with a ``logger.warning`` only; the engine continued and
    the next reviewer pass ran against a partial cortex index. The
    silent-fallback site is now closed by construction: when
    ``task_id`` + ``tasks_dir`` are supplied (the canonical caller
    :func:`save_artifact` does this) any exception is routed through
    the C7 typed-event primitive as ``cortex_index_failed`` so FE
    caches invalidate and operators see the failure without tailing
    ``.cortex-index.log``. The engine still continues — degraded
    indexing is not a halt condition — but it is observable.
    """
    artifacts_dir = task_path / "artifacts"
    if not artifacts_dir.exists():
        return
    try:
        import subprocess
        import sys

        script = (
            "import os; "
            "from pathlib import Path; "
            "from okuro.cortex.vectorstore import VectorStore, VectorConfig; "
            "c = VectorConfig(); "
            "s = VectorStore(c); "
            f"arts = Path('{artifacts_dir}'); "
            "EXTS = {'.md','.ts','.tsx','.py','.yaml','.yml','.json','.css','.prisma','.toml','.sh'}; "
            "SKIP = {'node_modules','.next','__pycache__','.git','.pnpm','dist','.turbo'}; "
            "count = 0; "
            "for dirpath, dirnames, filenames in os.walk(arts): "
            "    dirnames[:] = [d for d in dirnames if d not in SKIP]; "
            "    for fn in filenames: "
            "        if count >= 200: break\n"
            "        fp = Path(dirpath) / fn; "
            "        if fp.suffix in EXTS: "
            "            try: s.index_file(fp, force=True); count += 1\n"
            "            except Exception as ie: print('INDEX_FAIL', fp, ie)\n"
        )
        # Wave-6 G14 — open a log file in append mode so a long-running
        # task accumulates history; rotate is the operator's problem.
        log_path = task_path / ".cortex-index.log"
        try:
            log_handle = open(log_path, "ab")
        except OSError:
            log_handle = subprocess.DEVNULL
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        logger.info(f"cortex index triggered for {artifacts_dir} → {log_path}")
    except Exception as e:
        logger.warning(f"cortex index failed for {artifacts_dir}: {e}")
        # C12 — surface the failure as a state-change event so the FE
        # cache invalidates and operators see the failure live. Only
        # fires when the caller supplied task_id + tasks_dir; legacy
        # callers without those keep the historical log-only behaviour
        # (no regression). Any second-order failure inside emit_event
        # itself (e.g. log.jsonl lock contention) is demoted to a
        # ``logger.debug`` so the original cortex error stays the
        # dominant signal.
        if task_id and tasks_dir is not None:
            try:
                emit_event(
                    task_id,
                    "cortex_index_failed",
                    {"reason": str(e), "artifacts_dir": str(artifacts_dir)},
                    tasks_dir,
                    pool="state_change",
                )
            except Exception as emit_exc:  # pragma: no cover — defence in depth
                logger.debug(
                    "cortex_index_failed emit dropped for %s: %r",
                    task_id, emit_exc,
                )


def _atomic_write(path: Path, content: str):
    """Write to a temp file, fsync, then atomic rename.

    Umbrella audit fix #5 — pre-fix used ``path.with_suffix('.tmp')`` so
    concurrent writers on the same target (two engine threads finalizing
    sibling subtasks, or engine + reconciler) raced on the SAME temp
    path. Whichever ``tmp.rename(path)`` lost the race silently clobbered
    the winner. No fsync meant a crash between write and rename could
    leave a zero-byte ``.tmp`` next to a missing target.

    Post-fix uses ``tempfile.NamedTemporaryFile`` (unique name per
    writer) + explicit ``flush + fsync`` (durable on disk) +
    ``os.replace`` (atomic POSIX rename). Each writer gets its own
    temp file, so concurrent writes serialize cleanly via the
    final ``os.replace`` step.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # delete=False so we can rename after closing; the temp file lives
    # in the same directory as the target so os.replace stays
    # cross-filesystem-safe.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    try:
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup of the orphan temp on rename failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_dump_yaml(path: "Path", data, **dump_kwargs) -> None:
    """Atomically serialize ``data`` to ``path`` as YAML (tmp + fsync +
    os.replace via ``_atomic_write``).

    Use for EVERY task.yaml / plan.yaml write. A plain ``open(path, "w")``
    truncates the target to 0 bytes BEFORE the new content lands; a crash or
    SIGTERM (e.g. a service restart) in that window leaves an empty canonical
    state file, which bricks the task — ``load_task`` then reads ``None`` and
    raises ``'NoneType' object is not subscriptable`` on every poll. This
    helper makes the write all-or-nothing so the old file survives a failed
    write. Public mirror of ``_atomic_write`` for callers outside this module.
    """
    _atomic_write(path, yaml.dump(data, **dump_kwargs))


# ---------------------------------------------------------------------------
# C1 — Single locked writer per task directory.
#
# Pre-fix: task.yaml had 6+ unlocked writers (save_task_meta /
# update_task_status / set_awaiting / clear_awaiting / set_task_status /
# _reconcile_task_state / scattered API endpoints). Concurrent
# read-modify-write cycles raced; update_task_status silently preserved
# unknown keys (including stale ``awaiting``) — the D6 / RC3 root.
#
# Post-fix: the shared ``.plan.lock`` cohort is extended to cover task.yaml
# writes too — one lock per task directory protecting both YAML files.
# Every task.yaml writer goes through ``save_task_meta`` (the canonical
# closed-schema writer), which acquires this lock around its read-modify-
# write cycle. ``update_task_status`` is now a thin compatibility wrapper
# that delegates to ``save_task_meta`` so the silent-key-preservation
# behaviour is removed structurally — not by patching one merge function.
#
# Re-entrancy: fcntl flocks are scoped per fd, not per process — opening a
# second fd against the same lock file from the same process would
# DEADLOCK. The helper below tracks lock ownership in a thread-local set
# so nested writers within the same call stack reuse the outer lock
# instead of trying to re-acquire it. This makes it safe for
# ``set_task_status`` → ``save_task_meta`` and similar nestings.
# ---------------------------------------------------------------------------

import contextlib
import threading

_LOCK_TLS = threading.local()


def _held_locks() -> set[str]:
    held = getattr(_LOCK_TLS, "held", None)
    if held is None:
        held = set()
        _LOCK_TLS.held = held
    return held


@contextlib.contextmanager
def _locked_task_dir(task_id: str, tasks_dir: Path):
    """Acquire the shared ``.plan.lock`` for a task directory.

    Re-entrant within the same thread — nested ``with _locked_task_dir(...)``
    calls reuse the outer lock instead of deadlocking on a second fd.

    Used by every canonical task.yaml + plan.yaml writer (save_task_meta,
    update_subtask, update_node, save_plan-via-helpers). Direct callers
    of ``save_plan`` that don't take a lock (e.g. apply_supersedes' inner
    update_subtask paths) hold the lock at their own boundary.
    """
    task_path = tasks_dir / task_id
    task_path.mkdir(parents=True, exist_ok=True)
    lock_key = str(task_path.resolve())
    held = _held_locks()
    if lock_key in held:
        # Re-entrant: outer frame already holds the lock; yield without
        # re-acquiring (would deadlock on a separate fd).
        yield
        return

    lock_path = task_path / ".plan.lock"
    lock_path.touch(exist_ok=True)
    lock_file = open(lock_path, "r")
    try:
        if fcntl is not None:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        held.add(lock_key)
        try:
            yield
        finally:
            held.discard(lock_key)
            if fcntl is not None:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
                except OSError:
                    pass
    finally:
        try:
            lock_file.close()
        except OSError:
            pass


@dataclass
class Subtask:
    """Represents a single unit of work within a task."""
    id: str
    role: str
    description: str
    risk: str           # "LOW", "MED", "HIGH"
    complexity: str     # "fast", "standard", "strategic"
    status: str         # "pending", "running", "done", "failed", "skipped", "superseded"
    dependencies: list[str] = field(default_factory=list)
    # When a later continuation makes this subtask obsolete, set status to
    # "superseded" and record the superseding phase id(s) here. Superseded
    # subtasks are skipped by the dispatcher and excluded from task.status
    # aggregation — but kept in history for audit and retro metrics.
    superseded_by: list[str] = field(default_factory=list)
    artifact_name: str = ""
    phase: int = 1
    # Which DRAWN node produced this subtask, for a task compiled from a
    # workflow graph. Empty for every other planning path (an LLM plan has no
    # node behind it).
    #
    # Subtask ids cannot serve this purpose: they are derived from canvas
    # POSITION (sorted by y, then x, then id), so moving a node on the canvas
    # renames its subtask. Without a stable identity, live run status cannot be
    # attached back to the node it came from — which is the whole point of
    # rendering a run as the graph it was drawn as.
    node_id: str = ""
    cli_used: str = ""
    model_used: str = ""
    output_summary: str = ""
    started_at: str = ""
    completed_at: str = ""
    duration: float = 0.0
    retries: int = 0
    error: str = ""
    model_override: str = ""
    produces_capability: Optional[str] = None
    reuses_capabilities: list[str] = field(default_factory=list)
    # Wave-4 G8 — typed contract metadata. All optional; planners can fill
    # them when intent is concrete enough. Without these, subtasks rely on
    # prose `description` alone and downstream subagents re-derive contracts
    # from interpretation. Renderered into the assignment block when present.
    inputs: list[str] = field(default_factory=list)              # typed inputs the subtask consumes (e.g. "User schema", "JWT_SECRET env var")
    outputs: list[str] = field(default_factory=list)             # typed outputs the subtask must produce
    acceptance_criteria: list[str] = field(default_factory=list) # bullets the subagent's deliverable must satisfy
    target_paths: list[str] = field(default_factory=list)        # absolute project paths the subtask is expected to create or modify
    contract_id: str = ""                                        # FK to a contract artifact, when this subtask realizes a previously-defined contract
    # Review-ledger (Phase A): structured critic findings carried into the
    # NEXT dispatch so a retry is a SURGICAL fix of the flagged elements, not
    # a blind regeneration. Populated by the blocked_review "Fix" path from the
    # awaiting payload; rendered into the brief by build_role_prompt via
    # _render_retry_context. Empty on a clean (non-retry) dispatch.
    review_findings: list = field(default_factory=list)          # [{file,line,severity,evidence,summary,suggested_fix}, ...]
    # blocked_review "Decide" action: authoritative user adjudications of a
    # NEEDS_USER finding. Rendered FIRST in the recovery block (above findings)
    # so the agent APPLIES the decision and does not re-litigate it — the cure
    # for churn when the reviewer escalates a constraint only the user can call.
    decision_guidance: list = field(default_factory=list)        # [{decision, rationale, ts}, ...]
    # Quality-review outcome, kept STRICTLY separate from `status`.
    #
    # `status` answers "did the subagent produce a deliverable". `review_state`
    # answers "did quality review settle". They are orthogonal, and fusing them
    # is what corrupted whole runs: a review that could not settle used to
    # overwrite status with "failed" purely to stop re-dispatch, and every
    # downstream consumer then read that as "the work failed". On
    # task-20260724-110216 subtask 4.2 had shipped an artifact AND a handover;
    # the reviewer looped on one finding (its own subagent diagnosed the critic
    # as substring-matching URL slugs); the engine stamped `failed`; the user's
    # override then discarded real work and the run lost two further phases.
    #
    # A review verdict must NEVER write `status`. `failed` now means exactly
    # one thing: no deliverable was produced.
    #
    #   not_reviewed — no review has run (default)
    #   passed       — reviewer accepted the deliverable
    #   failed       — reviewer rejected it; the phase parks for the human.
    #                  Blocks re-dispatch WITHOUT lying about the work.
    #   overridden   — the human accepted it despite the reviewer. The subtask
    #                  is `done` and dependents run normally; the flagged
    #                  finding rides along in the brief so downstream neither
    #                  re-litigates it nor treats it as a blocker.
    review_state: str = "not_reviewed"


@dataclass
class DecisionGateOption:
    """One option presented at a decision gate."""
    id: str
    label: str
    description: str = ""
    pros: str = ""
    cons: str = ""
    risk: str = ""
    recommended: bool = False


@dataclass
class DecisionGate:
    """A blocking gate emitted before a phase to capture a user decision.

    Status flow: pending → resolved (option chosen + locked as ADR) | skipped.
    The engine pauses the orchestrator loop on a pending gate, emits an
    `await_user_decision` log event, polls plan.yaml for selected_option_id,
    locks the chosen option as an ADR in task.adrs, then resumes dispatch.
    M1 primitive — paired with Task.adrs + Task.gates_enabled.
    """
    id: str
    prompt: str
    options: list[DecisionGateOption] = field(default_factory=list)
    status: str = "pending"               # pending | resolved | skipped
    selected_option_id: str = ""
    selected_rationale: str = ""
    resolved_at: str = ""


@dataclass
class Phase:
    """Represents a phase containing multiple subtasks."""
    id: int
    name: str
    subtasks: list[Subtask] = field(default_factory=list)
    status: str = "pending"
    # IDs of prior subtasks this phase makes obsolete. Populated by the
    # continuation planner when the user's correction supersedes earlier
    # pending work (e.g. "deploy on fly, not netlify" → supersedes the
    # netlify-deploy subtask). Apply via apply_supersedes() before
    # append_phases() so the dispatcher never picks up stale work.
    supersedes: list[str] = field(default_factory=list)
    # M1 — when True the dispatcher will not run subtasks of this phase in
    # parallel even if multiple are ready. Used for decision-laden phases
    # where sibling drift would compound. Default False preserves legacy
    # parallel behaviour for every existing planner output.
    serialize: bool = False
    # M1 — optional blocking decision gate. When set and status=="pending",
    # the engine pauses before dispatching any subtask of this phase,
    # emits `await_user_decision`, and waits for the user's choice. On
    # resolve, the chosen option is appended to task.adrs and injected
    # into every downstream subagent brief.
    decision_gate: Optional["DecisionGate"] = None


@dataclass
class Intervention:
    """A user-supplied prompt that shaped the task's evolution.

    ``kind`` is ``"initial"`` for the original task description (one per task)
    or ``"continuation"`` for every Continue / Retry instruction the user
    submits afterwards. ``before_phase_id`` anchors the intervention to the
    flow chart: the card renders just above the phase whose id matches —
    i.e. "this prompt produced the phases starting at N". For the initial
    intervention this is always 1.

    ``source`` distinguishes free-text user prompts from agent-suggested
    continuations the user merely clicked-to-accept. The flow chart by
    default surfaces only ``initial`` and ``user_typed`` so the user
    sees their own voice, not the orchestrator's. Recognized values:

    - ``"initial"``           — synonym for ``kind="initial"``; the
                                 original task description.
    - ``"user_typed"``        — free-text continuation typed by the user.
    - ``"suggestion_accepted"`` — text comes verbatim from a
                                 ``continuation_suggestion`` the user
                                 picked from the suggestion list.
    - ``"unknown"``           — historical entries before sourcing was
                                 tracked; rendered as user_typed only when
                                 manually promoted.

    Continuations are persisted the moment the API receives them, BEFORE
    decomposition runs. That guarantees the prompt survives planner crashes
    (e.g. the E2BIG fix in commit fd7b5e4) so the user can always see what
    they asked for, even if no plan was ever produced.
    """
    id: str               # stable id, e.g. "int-{ts}"
    ts: str               # ISO 8601 created_at
    kind: str             # "initial" | "continuation"
    text: str             # full user-provided prompt, untruncated
    before_phase_id: int  # the first phase produced from this prompt
    source: str = "user_typed"
    # Per-continuation override for the deliberation-on-continuation toggle.
    # None = fall through to the task-level default, then the global config
    # default. True/False force a fresh deliberation council (or single-shot
    # decompose) for THIS continuation regardless of the task/global default.
    # Only meaningful for kind="continuation".
    deliberate: Optional[bool] = None
    # Set to the council round number once a deliberation round has been
    # SEEDED for this continuation (deliberate path). While non-None the
    # continuation is "in council, phases pending" — pending_continuations
    # excludes it so the drain loop does not re-seed on every engine tick.
    # Cleared implicitly once the council resolves and phases materialize
    # (before_phase_id <= max phase id).
    council_round: Optional[int] = None


# ── DAG Model (deliberation mode) ──────────────────────────────────────────


@dataclass
class Assignment:
    """Authority assignment at a discussion node."""
    role: str
    action: str          # "assign" | "acknowledge" | "dismiss"
    leads: str = ""
    reasoning: str = ""


@dataclass
class DAGNode:
    """A node in the deliberation DAG.

    Types:
      - position:   role presents a structured position artifact
      - discussion: convergence point where user arbitrates
      - execution:  normal subtask (post-deliberation)
    """
    id: str
    type: str                # "position" | "discussion" | "execution"
    status: str = "pending"
    role: str = ""
    source: str = "proposed"
    artifact: str = ""
    description: str = ""
    round: int = 1

    # Discussion node fields
    inputs: list[str] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)
    user_statement: str = ""
    inherits: str = ""

    # Execution node fields
    authority_from: str = ""

    # Context inheritance (round 2+)
    context_from: str = ""

    # Timing
    started_at: str = ""
    completed_at: str = ""
    duration: float = 0.0

    # Execution node fields (mirrors Subtask for dispatcher compatibility)
    risk: str = "LOW"
    complexity: str = "standard"
    artifact_name: str = ""
    cli_used: str = ""
    model_used: str = ""
    output_summary: str = ""
    error: str = ""
    retries: int = 0


@dataclass
class DAGEdge:
    """Directed edge in the deliberation DAG."""
    source: str
    target: str


@dataclass
class DAGGraph:
    """The full deliberation DAG."""
    nodes: list[DAGNode] = field(default_factory=list)
    edges: list[DAGEdge] = field(default_factory=list)

    def get_node(self, node_id: str) -> Optional[DAGNode]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def get_predecessors(self, node_id: str) -> list[str]:
        return [e.source for e in self.edges if e.target == node_id]

    def get_successors(self, node_id: str) -> list[str]:
        return [e.target for e in self.edges if e.source == node_id]

    def add_node(self, node: DAGNode):
        self.nodes = [n for n in self.nodes if n.id != node.id]
        self.nodes.append(node)

    def add_edge(self, source: str, target: str):
        for e in self.edges:
            if e.source == source and e.target == target:
                return
        self.edges.append(DAGEdge(source=source, target=target))

    def remove_node(self, node_id: str):
        self.nodes = [n for n in self.nodes if n.id != node_id]
        self.edges = [e for e in self.edges if e.source != node_id and e.target != node_id]


@dataclass
class Deliberation:
    """Deliberation state for a task."""
    current_round: int = 1
    strategy: str = "parallel"
    panel: list[str] = field(default_factory=list)


@dataclass
class Awaiting:
    """Single-source-of-truth record of what user input the engine is blocked on.

    Set by the engine whenever it exits the work loop because it cannot make
    progress without a user decision. Cleared by API handlers when the
    corresponding decision arrives. UI renders this as a foreground card on
    every task page — task.status='waiting_user' guarantees presence, and the
    payload tells the UI which component to render and where to POST.
    """
    kind: str                              # panel_confirmation | capability_gap | discussion_proceed | decision_gate | blocked_review | timeout_cap
    message: str                           # human-readable one-liner for the card header
    endpoint: str                          # API endpoint the UI should hit
    method: str = "POST"                   # HTTP method (POST/GET)
    payload: dict = field(default_factory=dict)  # opaque blob (proposed roles, gate diff, retry-cap subtasks)
    since: str = ""                        # ISO timestamp the wait started — UI shows "stuck for N min"


@dataclass
class Task:
    """Represents a complete task with multiple phases."""
    id: str
    description: str
    status: str
    created_at: str
    preferred_cli: str = ""
    title: str = ""
    phases: list[Phase] = field(default_factory=list)
    current_phase: int = 1
    links: list[dict] = field(default_factory=list)
    task_type: str = "oneshot"
    recurring_def_id: str = ""
    continuation_suggestions: list[dict] = field(default_factory=list)
    # User prompts that shaped this task — initial description + every
    # continuation. Persisted the moment they arrive so a planner crash
    # cannot silently swallow them. Surfaced in the pipeline view as
    # inline cards anchored to the phase they produced.
    interventions: list[Intervention] = field(default_factory=list)
    intelligence: str = ""
    # Which trigger fires the M3 review for this task's subtasks:
    # "" (inherit the global default) | "per_artifact" | "closeout".
    # Per-task like `intelligence`, so a closeout experiment can be scoped to
    # ONE task instead of flipping the installation-wide default — the bar for
    # flipping that default is two clean runs on the real stdio path, which
    # means two experiments, and a global flip would put every unrelated task
    # on the new trigger in between.
    review_trigger: str = ""
    required_roles: list[str] = field(default_factory=list)
    mode: str = "auto-execute"
    # A MANUALLY ARRANGED workflow (okuro.orchestrator.workflow_store) this task
    # runs instead of being decomposed by the LLM. Empty = the on-the-fly
    # decomposer plans it, which is the historical behaviour and stays the
    # default. ``workflow_params`` fills the {placeholders} in the drawn nodes'
    # prompts, outputs and acceptance criteria.
    workflow_id: str = ""
    workflow_params: dict = field(default_factory=dict)
    # Task-level default for the deliberation-on-continuation toggle. None =
    # fall through to the global config default; True/False sets the default
    # for every continuation on this task (a per-continuation Intervention.
    # deliberate still overrides this). Only effective for deliberate-mode
    # tasks (a fresh council needs a DAG graph).
    deliberate_continuations: Optional[bool] = None
    # F2 — task-level autopilot override. None = inherit the global
    # orchestrator.autopilot config default; True/False forces autopilot on/off
    # for THIS task. When on, gates that would park on a human decision are
    # auto-answered from the profile (resolver + confidence floor + abstain →
    # park fallback). See autopilot.autopilot_gate for the enable resolution.
    autopilot: Optional[bool] = None
    graph: Optional[DAGGraph] = None
    deliberation: Optional[Deliberation] = None
    # Set when this task was spawned from a registered todo (Now-page
    # Todos column). On task done the engine calls todo_done(id) so the
    # todo→task→done loop closes without manual cleanup.
    source_todo_id: str = ""
    # Set when this task was spawned from an action-item embedded in a
    # thought (Now-page Action Items column). On task done the engine
    # calls update_thought(id, status="resolved").
    source_thought_id: str = ""
    # Audit finding #23 — batched HIGH-risk approvals. When set to "medium"
    # or "high", the orchestrator auto-approves any subtask whose risk is
    # at or below the level (LOW always auto-approves regardless). Default
    # "none" preserves the current per-subtask wait-for-approval flow.
    auto_approve_risk: str = "none"
    # The `--yes` launch flag, persisted. Every other create-path flag (mode,
    # intelligence, preferred_cli, auto_approve_risk, …) already lives on this
    # dataclass; `--yes` used to exist ONLY in argv, and every engine park
    # respawns through `_resume_engine_if_idle` (api/main.py) which spawns a
    # BARE `--resume <id>` with no flags at all. Net effect before this field:
    # a task created with --yes ran unattended until its first park, then
    # started prompting for MED/HIGH approval forever after. Persisted here so
    # the flag survives respawn like every other launch decision.
    auto_approve_yes: bool = False
    # Filesystem path to the project this task is realizing (e.g. a
    # webapp under ~/workspace/.../some-project). Empty
    # string when unknown. Crucial for the preview "See result" feature:
    # tasks REALIZE their work in project directories, not in
    # ~/.okuro/orchestrator/tasks/{id}/artifacts (which only holds
    # bookkeeping — markdown reports, stdout dumps, debug screenshots).
    # The preview proposer scans project_path FIRST when set; without it
    # the auto-detector falls through to picking a random PNG from
    # artifacts and emitting kind=image, breaking "See result" for every
    # webapp task. Recover retroactively from plan.yaml subtask
    # descriptions for legacy tasks; surface in UI for new ones.
    project_path: str = ""
    # Stream C — audience-adapted delivery. When set, the engine fans out
    # peer.delivery.send(artifact_id, person_id=audience, channel=delivery_channel,
    # brand_id=delivery_brand_id) for every Stream B artifact a subtask
    # produces. Best-effort (HR-C3): a delivery failure never fails the
    # subtask. Empty string = no recipient (current default).
    audience: str = ""             # person_id of the recipient
    delivery_channel: str = ""     # markdown | marp | microsite | tts | podcast (default markdown). PPTX/DOCX/XLSX are banned (no MS formats).
    delivery_brand_id: str = ""    # brand override for theme.tokens_to_theme
    # Wave-3 G11 — per-task conventions block. Pinned verbatim into every
    # subagent prompt so folder layout, naming, error envelope, and other
    # cross-cutting rules don't drift across the 30-subagent build. Auto-
    # populated by create_task() from {project_path}/CONVENTIONS.md when
    # that file exists; otherwise empty (no pin → no enforcement, but
    # subagents still see the upstream-handover briefs).
    conventions: str = ""
    # Why this task was halted, in plain words, written by whoever halted it
    # (today: the maintenance sweep). PURELY FOR THE READER — nothing branches
    # on it; the halt DECISION is derived from status by
    # deliberation._task_halt_reason, which is a different thing with a
    # confusingly similar name.
    #
    # It existed on disk before it existed here: the 2026-08-03 sweep wrote a
    # careful explanation into 43 task.yaml files and NOTHING in src read the
    # key back — not the loader, not the API, not the UI, which went on showing
    # the generic failure banner. Write-only data that reads as thoroughness.
    halt_reason: str = ""
    # Build/test gate — when set, the engine runs this command in
    # project_path (or task.tasks_dir/{id} when project_path is empty)
    # AFTER all subtasks finalize, BEFORE flipping task.status to "done".
    # Non-zero exit fails the task with the verify_command output as the
    # error. This is the canonical answer to "the subagents claimed
    # success but does the code actually work?". Empty = no gate.
    verify_command: str = ""
    # M1 — locked ADRs from resolved decision gates. Each entry: {gate_id,
    # phase_id, prompt, options, selected_option_id, selected_label,
    # selected_rationale, resolved_at}. Persisted in task.yaml. Injected
    # verbatim into every downstream subagent brief as the "Locked
    # Decisions" block so the agent inherits the user-chosen direction
    # without re-deriving it.
    adrs: list[dict] = field(default_factory=list)
    # M1 — when True the orchestrator routes through gates_enabled
    # behaviour: phases with decision_gate.status=="pending" block dispatch
    # until resolved. intelligence=="max" defaults this to True unless
    # --auto-execute is passed. False preserves the legacy auto-execute
    # path (no gates honoured even if planner emits them).
    gates_enabled: bool = False
    # ROCK-SOLID v5 P6.7 — fast-track profile, per-task opt-in.
    #
    # When on, a phase whose subtasks are ALL risk=LOW passes on the
    # deterministic stage alone and skips the critic + scorer LLM calls.
    # Measured justification: on task-20260730-233206 the critic accounted
    # for 4007s of the review pipeline's 4121s, so the LLM stages ARE the
    # review cost — everything downstream of the critic was 114s.
    #
    # Opt-in and never inferred. A task the user did not mark fast is
    # reviewed in full, because the failure mode of guessing wrong here is
    # shipping unreviewed work, and that is not recoverable by being fast.
    fast_track: bool = False
    # M4 — force the legacy full-body role injection for THIS task. When
    # True, build_role_prompt re-inlines the full lean_prompt as before
    # M4. Default False routes through the metadata-only slice + lazy
    # roles_get() fetch. Honoured per-task so in-flight tasks keep their
    # original mode while new tasks default to M4. Mirrors the
    # OKURO_LEGACY_ROLE_INJECTION env var for one-off CLI overrides.
    legacy_roles: bool = False
    # Single-source-of-truth for "engine has handed control back to user".
    # Engine sets this before exit-on-wait; API handlers clear it when the
    # corresponding decision arrives. None == work in flight or done.
    awaiting: Optional[Awaiting] = None


def create_task(
    description: str,
    tasks_dir: Path,
    task_id: Optional[str] = None,
    preferred_cli: Optional[str] = None,
    intelligence: Optional[str] = None,
    review_trigger: Optional[str] = None,
    required_roles: list[str] | None = None,
    mode: str = "auto-execute",
    source_todo_id: Optional[str] = None,
    source_thought_id: Optional[str] = None,
    auto_approve_risk: str = "none",
    project_path: Optional[str] = None,
    verify_command: Optional[str] = None,
    gates_enabled: bool = False,
    fast_track: bool = False,
    deliberate_continuations: Optional[bool] = None,
    autopilot: Optional[bool] = None,
    workflow_id: Optional[str] = None,
    workflow_params: Optional[dict] = None,
    # ROCK-SOLID v5 P5.4 — Stream C had no writer.
    #
    # `Task.audience` / `delivery_channel` / `delivery_brand_id` have been
    # declared since Stream C landed, and `_finalize_subtask` reads them:
    # `if getattr(task, "audience", ""): _fan_out_deliveries(...)`. Nothing
    # ever set them. Every field, every reader, every fan-out branch was
    # present and correct, and the feature could not fire once — the guard's
    # input never arrived.
    #
    # Not a default. An unset audience means "no recipient", which is the
    # right behaviour for the overwhelming majority of tasks; forcing one
    # would mail every scratch task to somebody.
    audience: Optional[str] = None,
    delivery_channel: Optional[str] = None,
    delivery_brand_id: Optional[str] = None,
) -> Task:
    """Create a new task with directory structure and initial state files.

    ``deliberate_continuations`` sets the task-level default for the
    deliberation-on-continuation toggle (None = inherit the global config
    default; a per-continuation ``Intervention.deliberate`` still overrides).

    ``workflow_id`` binds this task to a MANUALLY ARRANGED workflow
    (``okuro.orchestrator.workflow_store``): its drawn graph compiles to the plan
    and the LLM decomposer is never called. ``workflow_params`` fills the
    ``{placeholders}`` in the drawn nodes. Omit both for the normal path.
    """
    if not task_id:
        task_id = f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    created_at = datetime.utcnow().isoformat()

    task_path = tasks_dir / task_id
    task_path.mkdir(parents=True, exist_ok=True)
    (task_path / "artifacts").mkdir(exist_ok=True)

    # Validate auto_approve_risk to prevent silent typos that would change
    # security posture (a typo'd "Hgih" would behave like "none" and the user
    # would never know their approval prompts were re-armed).
    risk_level = (auto_approve_risk or "none").lower()
    if risk_level not in ("none", "medium", "high"):
        risk_level = "none"

    # Validate required_roles against the roles DB at create-time. Previously,
    # invalid role ids were silently accepted and the task later wedged in
    # deliberation when the decomposer skipped them and semantic match also
    # failed. Fail loud here with a helpful list so the caller can fix the IDs.
    if required_roles:
        from okuro.roles.registry import get_role
        unknown = [r for r in required_roles if get_role(r) is None]
        if unknown:
            raise ValueError(
                f"required_roles contains unknown role id(s): {unknown}. "
                f"Use mcp__okuro__roles_list or roles_match to find valid ids."
            )

    # Wave-3 G11 — auto-load per-project conventions if CONVENTIONS.md
    # exists at the project root. Bounded read so a misplaced 100MB file
    # cannot blow the prompt budget; agents still see briefs from upstream.
    conventions_text = ""
    if project_path:
        try:
            conv_path = Path(project_path) / "CONVENTIONS.md"
            if conv_path.exists() and conv_path.is_file():
                raw = conv_path.read_text(encoding="utf-8", errors="replace")
                conventions_text = raw[:8000]  # 8KB cap → ~2k tokens
        except OSError:
            conventions_text = ""

    task = Task(
        id=task_id,
        description=description,
        status="pending",
        created_at=created_at,
        preferred_cli=preferred_cli or "",
        intelligence=intelligence or "",
        review_trigger=review_trigger or "",
        required_roles=required_roles or [],
        mode=mode,
        deliberate_continuations=deliberate_continuations,
        autopilot=autopilot,
        source_todo_id=source_todo_id or "",
        source_thought_id=source_thought_id or "",
        auto_approve_risk=risk_level,
        workflow_id=(workflow_id or "").strip(),
        workflow_params=dict(workflow_params or {}),
        project_path=project_path or "",
        conventions=conventions_text,
        verify_command=verify_command or "",
        gates_enabled=bool(gates_enabled),
        fast_track=bool(fast_track),
        audience=(audience or "").strip(),
        # A recipient with no channel is the common case — asking a caller to
        # name a format before it knows one is how an optional feature stops
        # being used. `markdown` matches what `_fan_out_deliveries` already
        # falls back to, so the stored value and the runtime value agree.
        delivery_channel=(delivery_channel or "").strip() or (
            "markdown" if (audience or "").strip() else ""
        ),
        delivery_brand_id=(delivery_brand_id or "").strip(),
    )
    if mode == "deliberate":
        task.graph = DAGGraph()
        task.deliberation = Deliberation()

    task_data = {
        "id": task.id,
        "description": task.description,
        "status": task.status,
        "created_at": task.created_at,
        "current_phase": task.current_phase,
    }
    if task.preferred_cli:
        task_data["preferred_cli"] = task.preferred_cli
    if task.intelligence:
        task_data["intelligence"] = task.intelligence
    if task.review_trigger:
        task_data["review_trigger"] = task.review_trigger
    if task.required_roles:
        task_data["required_roles"] = task.required_roles
    if task.source_todo_id:
        task_data["source_todo_id"] = task.source_todo_id
    if task.source_thought_id:
        task_data["source_thought_id"] = task.source_thought_id
    if task.auto_approve_risk and task.auto_approve_risk != "none":
        task_data["auto_approve_risk"] = task.auto_approve_risk
    if task.auto_approve_yes:
        task_data["auto_approve_yes"] = True
    if task.workflow_id:
        task_data["workflow_id"] = task.workflow_id
    if task.workflow_params:
        task_data["workflow_params"] = dict(task.workflow_params)
    if task.project_path:
        task_data["project_path"] = task.project_path
    if task.conventions:
        task_data["conventions"] = task.conventions
    if task.verify_command:
        task_data["verify_command"] = task.verify_command
    if task.gates_enabled:
        task_data["gates_enabled"] = True
    if task.fast_track:
        task_data["fast_track"] = True
    # P5.4 — persisted, or the engine reads a Task rebuilt from disk without
    # them and Stream C is inert again one process boundary later. The engine
    # never sees the object built above; it sees `load_task()`.
    if task.audience:
        task_data["audience"] = task.audience
        task_data["delivery_channel"] = task.delivery_channel
        if task.delivery_brand_id:
            task_data["delivery_brand_id"] = task.delivery_brand_id
    if task.deliberate_continuations is not None:
        task_data["deliberate_continuations"] = task.deliberate_continuations
    if task.adrs:
        task_data["adrs"] = list(task.adrs)
    if mode != "auto-execute":
        task_data["mode"] = mode
        task_data["deliberation"] = {
            "current_round": 1,
            "strategy": "parallel",
            "panel": [],
        }
    # Seed the initial intervention so every task — not just continued ones —
    # has at least one prompt-node in the flow chart.
    initial = Intervention(
        id=f"int-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}",
        ts=created_at,
        kind="initial",
        text=description,
        before_phase_id=1,
        source="initial",
    )
    task.interventions.append(initial)
    task_data["interventions"] = [asdict(initial)]
    _atomic_write(task_path / "task.yaml", yaml.dump(task_data, default_flow_style=False))

    append_log(task_id, {"type": "task_created", "detail": description}, tasks_dir)
    return task


# ---------------------------------------------------------------------------
# C3 — `plan.yaml.subtask` is the SoT for execution-type DAG nodes.
#
# Graph nodes of type "execution" carry no independent identity: their
# status / retries / started_at / completed_at / error mirror a subtask
# row in plan.yaml. To close the C3 violation class by construction we
# regenerate every execution node's status from the SoT pair (subtask,
# parent phase) at every plan.yaml write boundary. Position and
# discussion nodes have no subtask analog and are left untouched.
#
# Read path: `get_ready_nodes` continues to inspect `node.status` — the
# value it reads is now the derived view, so dispatch never picks a
# subtask whose parent phase is blocked_review even though the prior
# graph row said "done" (V3 / V8 root).
#
# Sites that mutate plan.yaml without going through save_plan
# (update_subtask's inline write at the bottom of that function) also
# call the derivation to keep the on-disk graph in lockstep.
# ---------------------------------------------------------------------------


def _derive_execution_node_status(
    subtask: "Subtask", phase: "Phase",
) -> str:
    """Compute the canonical graph-node status for an execution node.

    Rules:
      - phase.status == "blocked_review": the phase is gated by the user;
        every subtask in it is treated as non-terminal in the graph view
        so successors observe predecessor satisfaction = False. Closes
        V3 / V8 cascading dispatch.
      - subtask.status in {"approved", "waiting_approval"}: pre-dispatch
        states with no graph-side enum value — map to "pending" so the
        derived view stays within VALID_EXECUTION_STATUSES.
      - otherwise: mirror subtask.status (1:1). "superseded" is in
        VALID_EXECUTION_STATUSES so apply_supersedes mirroring lands on
        the graph node as "superseded" (D5 + continuation-loop class).
    """
    if phase.status == "blocked_review":
        return "pending"
    sst = subtask.status
    if sst in ("approved", "waiting_approval"):
        return "pending"
    return sst


def _apply_execution_node_derivation(task: "Task") -> None:
    """Regenerate every execution-type graph node's status in place.

    Mutates ``task.graph.nodes`` so the in-memory view matches what will
    be serialized. Called at every plan.yaml write boundary (save_plan +
    update_subtask). Idempotent — safe to call repeatedly.
    """
    if not task.graph or not task.graph.nodes:
        return
    # Index subtasks by id with their parent phase for O(1) lookup.
    subtask_index: dict[str, tuple[Subtask, Phase]] = {}
    for phase in task.phases:
        for st in phase.subtasks:
            subtask_index[st.id] = (st, phase)

    for node in task.graph.nodes:
        if node.type != "execution":
            continue
        pair = subtask_index.get(node.id)
        if pair is None:
            # Execution node without a matching subtask row. Leave it
            # alone — historical migration / partial state. Future writes
            # will catch up once the subtask appears.
            continue
        st, phase = pair
        derived = _derive_execution_node_status(st, phase)
        # Also mirror the rich metadata that lives only on the subtask
        # — keeps the FE-facing dump in sync even when callers read the
        # graph node directly (a few legacy consumers do).
        node.status = derived
        for field_name in ("started_at", "completed_at", "error", "retries"):
            if hasattr(st, field_name) and hasattr(node, field_name):
                setattr(node, field_name, getattr(st, field_name))


def _reject_late_done_write(
    current_status: str, current_retries: int, incoming: dict, kind: str, ident: str,
) -> bool:
    """Dispatch-generation guard for late phantom result events (D10).

    The dispatch flow always flips status to "running" BEFORE the agent
    starts; a subsequent transition pending → done can only originate
    from a result event tied to a prior dispatch generation that M3
    already reset. We detect that pattern via (current_status, retries):

      - status == "pending" AND retries > 0 AND incoming status == "done"
        → reject (a reset already advanced the retry counter; this write
        belongs to the prior generation).

    Fresh dispatches (retries == 0) and manual mark-done from human
    paths (handle_high_risk on a never-dispatched subtask) are not
    affected. Returns True when the caller MUST skip the write.
    """
    incoming_status = incoming.get("status", "")
    if incoming_status != "done":
        return False
    if current_status != "pending":
        return False
    if current_retries <= 0:
        return False
    logger.warning(
        "[C3] Rejecting late %s done write for %s — current state "
        "pending+retries=%d indicates a post-M3-reset generation "
        "(phantom emission from prior dispatch).",
        kind, ident, current_retries,
    )
    return True


def _phase_to_dict(phase: "Phase") -> dict:
    """Serialize ONE phase for plan.yaml. The single writer of the phase shape.

    Why this is extracted (2026-07-30). Two functions write plan.yaml:
    :func:`save_plan` and :func:`update_subtask`. Each built the phase dict
    inline, and ``update_subtask``'s copy emitted only
    ``{id, name, status, subtasks}`` plus ``supersedes`` — it silently dropped
    ``serialize`` and ``decision_gate``. Since ``update_subtask`` runs on every
    single subtask status change, any phase-level gate or serialization flag was
    erased within moments of being written.

    Measured over the 310 live plan.yaml files: 0 carried ``serialize`` and 1
    carried a ``decision_gate``. The erasure had disarmed both features
    wholesale — ``get_ready_subtasks`` reads exactly these two fields to decide
    whether a phase blocks on a gate or yields one subtask at a time, so it was
    reading defaults for every task in the system.

    Duplicated dict literals across two writers is the class here, so the fix is
    one builder, not a patch to the second copy. Do NOT instead route
    ``update_subtask`` through ``save_plan``: that function also runs the C6
    enum validation and the C7 prior-status diff that emits
    ``phase_status_changed``, so routing through it would double-emit on every
    subtask update.

    Forward-looking only. Historical plans whose flags were already erased are
    NOT repaired here — the information is gone, not hidden.
    """
    phase_dict = {
        "id": phase.id,
        "name": phase.name,
        "status": phase.status,
        "subtasks": [asdict(st) for st in phase.subtasks],
    }
    if getattr(phase, "supersedes", None):
        phase_dict["supersedes"] = list(phase.supersedes)
    if getattr(phase, "serialize", False):
        phase_dict["serialize"] = True
    gate = getattr(phase, "decision_gate", None)
    if gate is not None:
        phase_dict["decision_gate"] = asdict(gate)
    return phase_dict


def save_plan(task: Task, tasks_dir: Path):
    """Save the task plan with all phases and subtasks.

    Gate 2 §C6 — every phase.status / subtask.status / graph node status
    is validated against its enum before the file is written. A single
    invalid value aborts the whole save (ValueError) so the plan.yaml on
    disk is never inconsistent with the validator.

    Gate 2 §C3 — execution-type graph nodes are derived views: their
    status is regenerated from the matching subtask + parent phase
    BEFORE validation runs. This closes V3 / V8 / D5 by construction
    (the graph row can no longer drift from the SoT).
    """
    # C3 — derive execution-node status from the SoT subtask + parent
    # phase. Runs before validation so the validator sees the canonical
    # view, never a stale mirror.
    _apply_execution_node_derivation(task)

    # C6 — validate the full closed-enum surface before touching disk.
    for phase in task.phases:
        _validate_status_write("phase", phase.status)
        for st in phase.subtasks:
            _validate_status_write("subtask", st.status)
    if task.graph and task.graph.nodes:
        for node in task.graph.nodes:
            _validate_status_write(node.type, node.status)

    task_path = tasks_dir / task.id

    # C7 — diff phase.status against the on-disk plan so the silent
    # ``phase.status = X; save_plan(...)`` pattern still emits a canonical
    # phase_status_changed event. set_phase_status() is the documented
    # explicit helper for engine sites; this diff is the safety net.
    prior_phase_status: dict[int, str] = {}
    try:
        prior_plan_path = task_path / "plan.yaml"
        if prior_plan_path.exists():
            prior_plan = _yload(prior_plan_path.read_text()) or {}
            for p in prior_plan.get("phases", []) or []:
                pid = p.get("id")
                if pid is not None:
                    prior_phase_status[pid] = p.get("status", "")
    except Exception as exc:
        logger.debug("save_plan prior-diff read failed: %r", exc)

    phases_data = []
    subtask_count = 0
    for phase in task.phases:
        phases_data.append(_phase_to_dict(phase))
        subtask_count += len(phase.subtasks)

    plan_data = {"phases": phases_data}
    if task.graph and task.graph.nodes:
        plan_data["graph"] = _serialize_graph(task.graph)

    _atomic_write(task_path / "plan.yaml", yaml.dump(plan_data, default_flow_style=False))

    log_data = {"type": "plan_created", "phases": len(task.phases), "subtasks": subtask_count}
    if task.graph and task.graph.nodes:
        log_data["dag_nodes"] = len(task.graph.nodes)
        log_data["dag_edges"] = len(task.graph.edges)
    append_log(task.id, log_data, tasks_dir)

    # C7 — emit phase_status_changed for every phase that flipped status
    # since the prior on-disk plan. Catches engine.py:705 (blocked_review),
    # engine.py:2064 + 2535 (advance to done). Without this, FE consumers
    # gated on the state-change allowlist never learn about phase transitions.
    if prior_phase_status:
        for phase in task.phases:
            prior = prior_phase_status.get(phase.id)
            if prior is not None and prior != phase.status:
                try:
                    append_log(task.id, {
                        "type": "phase_status_changed",
                        "phase_id": phase.id,
                        "from": prior,
                        "to": phase.status,
                    }, tasks_dir)
                except Exception as exc:
                    logger.warning("phase_status_changed emit failed: %r", exc)


def _serialize_graph(graph: DAGGraph) -> dict:
    nodes = []
    for n in graph.nodes:
        nd = asdict(n)
        nd["assignments"] = [asdict(a) for a in n.assignments]
        nd = {k: v for k, v in nd.items()
              if v or v == 0 or k in ("id", "type", "status", "round")}
        nodes.append(nd)
    edges = [{"from": e.source, "to": e.target} for e in graph.edges]
    return {"nodes": nodes, "edges": edges}


def _deserialize_graph(data: dict) -> DAGGraph:
    graph = DAGGraph()
    for nd in data.get("nodes", []):
        assignments = [Assignment(**a) for a in nd.pop("assignments", [])]
        node = DAGNode(
            id=nd["id"],
            type=nd["type"],
            status=nd.get("status", "pending"),
            role=nd.get("role", ""),
            source=nd.get("source", "proposed"),
            artifact=nd.get("artifact", ""),
            description=nd.get("description", ""),
            round=nd.get("round", 1),
            inputs=nd.get("inputs", []),
            assignments=assignments,
            user_statement=nd.get("user_statement", ""),
            inherits=nd.get("inherits", ""),
            authority_from=nd.get("authority_from", ""),
            context_from=nd.get("context_from", ""),
            started_at=nd.get("started_at", ""),
            completed_at=nd.get("completed_at", ""),
            duration=nd.get("duration", 0.0),
            risk=nd.get("risk", "LOW"),
            complexity=nd.get("complexity", "standard"),
            artifact_name=nd.get("artifact_name", ""),
            cli_used=nd.get("cli_used", ""),
            model_used=nd.get("model_used", ""),
            output_summary=nd.get("output_summary", ""),
            error=nd.get("error", ""),
        )
        graph.nodes.append(node)

    for ed in data.get("edges", []):
        graph.edges.append(DAGEdge(
            source=ed.get("from", ed.get("source", "")),
            target=ed.get("to", ed.get("target", "")),
        ))
    return graph


def _subtask_from_dict(raw: dict) -> Subtask:
    """Build a Subtask, ignoring keys this build does not know.

    `Subtask(**raw)` is strict: one unrecognised key raises TypeError and
    load_task dies for the whole task. That makes every field addition a
    forward-compatibility trap — a plan written by a newer build becomes
    unreadable to an older one, so a daemon running the previous release
    cannot open the task at all.

    Hit for real on 2026-07-24: `review_state` was backfilled into 20 plans
    while the live daemon still ran the prior build, and every one of those
    tasks became unloadable until the write was rolled back. Dropping unknown
    keys costs nothing (the field is simply absent, so the dataclass default
    applies) and turns a hard failure into a logged degradation.
    """
    known = {f.name for f in fields(Subtask)}
    unknown = set(raw) - known
    if unknown:
        logger.warning(
            "subtask %s: ignoring unknown field(s) %s — plan.yaml was likely "
            "written by a newer build",
            raw.get("id", "?"), ", ".join(sorted(unknown)),
        )
    return Subtask(**{k: v for k, v in raw.items() if k in known})


def load_task(task_id: str, tasks_dir: Path) -> Task:
    """Load a task from disk with all its phases and subtasks."""
    task_path = tasks_dir / task_id

    with open(task_path / "task.yaml") as f:
        task_data = _yload(f)

    # A 0-byte / malformed task.yaml parses to None. Fail with a diagnosable
    # message instead of the cryptic "'NoneType' object is not subscriptable"
    # that every poll then 500-spammed. (Writers are now atomic, so this
    # should not recur — but a clear signal beats a confusing crash.)
    if not isinstance(task_data, dict):
        raise ValueError(
            f"task.yaml for {task_id} is empty or corrupt "
            f"(parsed to {type(task_data).__name__}); cannot load task"
        )

    _STATUS_ALIASES = {"complete": "done", "completed": "done", "finished": "done"}
    raw_status = task_data["status"]
    normalized_status = _STATUS_ALIASES.get(raw_status, raw_status)
    if normalized_status != raw_status:
        logger.info(f"[{task_id}] Normalized task status '{raw_status}' → '{normalized_status}'")

    task_mode = task_data.get("mode", "auto-execute")
    delib_data = task_data.get("deliberation")
    deliberation = None
    if delib_data:
        deliberation = Deliberation(
            current_round=delib_data.get("current_round", 1),
            strategy=delib_data.get("strategy", "parallel"),
            panel=delib_data.get("panel", []),
        )
    elif task_mode == "deliberate":
        deliberation = Deliberation()

    task = Task(
        id=task_data["id"],
        description=task_data["description"],
        status=normalized_status,
        created_at=task_data["created_at"],
        preferred_cli=task_data.get("preferred_cli", ""),
        title=task_data.get("title", ""),
        current_phase=task_data.get("current_phase", 1),
        links=task_data.get("links", []) or [],
        task_type=task_data.get("task_type", "oneshot"),
        recurring_def_id=task_data.get("recurring_def_id", ""),
        continuation_suggestions=task_data.get("continuation_suggestions", []) or [],
        intelligence=task_data.get("intelligence", ""),
        review_trigger=task_data.get("review_trigger", ""),
        deliberate_continuations=(
            bool(task_data["deliberate_continuations"])
            if task_data.get("deliberate_continuations") is not None
            else None
        ),
        mode=task_mode,
        deliberation=deliberation,
        source_todo_id=task_data.get("source_todo_id", "") or "",
        source_thought_id=task_data.get("source_thought_id", "") or "",
        auto_approve_risk=(task_data.get("auto_approve_risk") or "none"),
        auto_approve_yes=bool(task_data.get("auto_approve_yes") or False),
        workflow_id=(task_data.get("workflow_id") or ""),
        workflow_params=dict(task_data.get("workflow_params") or {}),
        project_path=(task_data.get("project_path") or ""),
        conventions=(task_data.get("conventions") or ""),
        halt_reason=(task_data.get("halt_reason") or ""),
        verify_command=(task_data.get("verify_command") or ""),
        adrs=list(task_data.get("adrs") or []),
        gates_enabled=bool(task_data.get("gates_enabled") or False),
        fast_track=bool(task_data.get("fast_track") or False),
        # P5.4 — the engine only ever sees a Task rebuilt here. Writing the
        # three Stream C fields in create_task without reading them back
        # would have moved the inert guard one layer down rather than
        # removing it: task.yaml would show a recipient and the fan-out
        # branch would still never be true.
        audience=(task_data.get("audience") or ""),
        delivery_channel=(task_data.get("delivery_channel") or ""),
        delivery_brand_id=(task_data.get("delivery_brand_id") or ""),
    )

    awaiting_raw = task_data.get("awaiting")
    if isinstance(awaiting_raw, dict) and awaiting_raw.get("kind"):
        try:
            task.awaiting = Awaiting(
                kind=str(awaiting_raw.get("kind", "")),
                message=str(awaiting_raw.get("message", "")),
                endpoint=str(awaiting_raw.get("endpoint", "")),
                method=str(awaiting_raw.get("method", "POST")),
                payload=dict(awaiting_raw.get("payload") or {}),
                since=str(awaiting_raw.get("since", "")),
            )
        except (TypeError, ValueError) as exc:
            logger.warning(f"[{task_id}] Dropping malformed awaiting block: {exc}")
            task.awaiting = None

    raw_interventions = task_data.get("interventions") or []
    for entry in raw_interventions:
        if not isinstance(entry, dict):
            continue
        try:
            kind = entry.get("kind", "continuation")
            default_source = "initial" if kind == "initial" else "user_typed"
            _delib = entry.get("deliberate", None)
            _croud = entry.get("council_round", None)
            task.interventions.append(Intervention(
                id=entry["id"],
                ts=entry["ts"],
                kind=kind,
                text=entry.get("text", ""),
                before_phase_id=int(entry.get("before_phase_id", 1)),
                source=entry.get("source") or default_source,
                deliberate=(bool(_delib) if _delib is not None else None),
                council_round=(int(_croud) if _croud is not None else None),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(f"[{task_id}] Skipping malformed intervention: {exc}")

    plan_file = task_path / "plan.yaml"
    if plan_file.exists():
        with open(plan_file) as f:
            plan_data = _yload(f)

        for phase_dict in plan_data.get("phases", []):
            for st in phase_dict.get("subtasks", []):
                if st.get("status") in _STATUS_ALIASES:
                    st["status"] = _STATUS_ALIASES[st["status"]]
            subtasks = [_subtask_from_dict(st) for st in phase_dict.get("subtasks", [])]
            raw_phase_status = phase_dict.get("status", "pending")
            gate_data = phase_dict.get("decision_gate")
            decision_gate = None
            if gate_data:
                options_raw = gate_data.get("options", []) or []
                opts = []
                for o in options_raw:
                    try:
                        opts.append(DecisionGateOption(**o))
                    except (TypeError, ValueError) as exc:
                        logger.warning(f"[{task_id}] skipping malformed gate option: {exc}")
                decision_gate = DecisionGate(
                    id=gate_data.get("id", ""),
                    prompt=gate_data.get("prompt", ""),
                    options=opts,
                    status=gate_data.get("status", "pending"),
                    selected_option_id=gate_data.get("selected_option_id", "") or "",
                    selected_rationale=gate_data.get("selected_rationale", "") or "",
                    resolved_at=gate_data.get("resolved_at", "") or "",
                )
            phase = Phase(
                id=phase_dict["id"],
                name=phase_dict["name"],
                status=_STATUS_ALIASES.get(raw_phase_status, raw_phase_status),
                subtasks=subtasks,
                supersedes=list(phase_dict.get("supersedes", []) or []),
                serialize=bool(phase_dict.get("serialize", False)),
                decision_gate=decision_gate,
            )
            task.phases.append(phase)

        if "graph" in plan_data:
            task.graph = _deserialize_graph(plan_data["graph"])

    warnings = validate_and_warn(task)
    if warnings:
        seen = _VALIDATION_WARNING_SEEN.setdefault(task_id, set())
        for w in warnings:
            if w in seen:
                continue
            seen.add(w)
            logger.warning(f"[{task_id}] {w}")

    return task


def update_node(task_id: str, node_id: str, updates: dict, tasks_dir: Path):
    """Update a DAG node and log the change. Thread-safe via file locking.

    Gate 2 §C1 — locks the shared ``.plan.lock`` cohort that also covers
    task.yaml writers (save_task_meta). Re-entrant within the same thread
    via ``_locked_task_dir`` so a caller already holding the lock can call
    this without deadlocking.

    Gate 2 §C6 — invalid status enum values raise InvalidStatusError before
    the node mutation runs. Node type (position / discussion / execution)
    selects the right enum; the validator is authoritative.

    Gate 2 §C3 — for execution-type nodes a late ``done`` arriving after
    an M3 reset (current state pending+retries>0) is rejected via the
    dispatch-generation guard. Position / discussion nodes are unaffected.
    """
    with _locked_task_dir(task_id, tasks_dir):
        task = load_task(task_id, tasks_dir)
        if not task.graph:
            raise ValueError(f"Task {task_id} has no DAG graph")

        node = task.graph.get_node(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found in task {task_id}")

        # C6 — validate status enum against the node's declared type.
        # Run AFTER node lookup so we know which enum applies; raises
        # before any mutation lands on disk.
        _validate_status_write(node.type, updates.get("status", ""))

        # C3 — late-write rejection (dispatch generation guard). For
        # execution nodes, check the SoT subtask's state, not the graph
        # node's (the graph row is a derived view and may already lag).
        if node.type == "execution":
            sot_status = node.status
            sot_retries = int(getattr(node, "retries", 0) or 0)
            for phase in task.phases:
                for st in phase.subtasks:
                    if st.id == node_id:
                        sot_status = st.status
                        sot_retries = int(getattr(st, "retries", 0) or 0)
                        break
                if sot_status != node.status:
                    break
            if _reject_late_done_write(
                sot_status, sot_retries, updates, "node", node_id,
            ):
                return

        for key, value in updates.items():
            if hasattr(node, key):
                setattr(node, key, value)

        # SYNC graph-node updates into the matching plan.yaml subtask so
        # the dashboard's PipelineView (which reads from task.phases via
        # state_reader.read_task_state) reflects running/done state.
        # Without this, execution nodes stayed "pending" in the UI forever
        # while actually running. Only applies to execution nodes — position
        # and discussion nodes don't have plan.yaml subtask counterparts.
        if node.type == "execution":
            for phase in task.phases:
                for st in phase.subtasks:
                    if st.id == node_id:
                        for key, value in updates.items():
                            if hasattr(st, key):
                                setattr(st, key, value)
                        break

        save_plan(task, tasks_dir)

    event_type = f"node_{updates.get('status', 'updated')}" if "status" in updates else "node_updated"
    append_log(task_id, {"type": event_type, "node": node_id, **{
        k: v for k, v in updates.items() if k != "assignments"
    }}, tasks_dir)


def save_task_meta(task: Task, tasks_dir: Path):
    """Save task.yaml metadata without touching plan.yaml.

    Gate 2 §C1 — canonical, single locked writer for ``task.yaml``. Every
    in-process task.yaml mutation routes through this function so the
    read-modify-write cycle is serialized across threads (and engine /
    reconciler / API endpoints in the same process). The closed write
    schema is enforced inline: every field this writer knows about is
    serialized explicitly from the in-memory ``Task`` object — unknown
    keys on disk are NOT silently preserved across writers (the D6 / RC3
    root: ``awaiting`` survived past an engine clear because a sibling
    writer carried it forward).

    Gate 2 §C6 — task.status is validated against VALID_TASK_STATUSES
    before any file I/O. Raises InvalidStatusError on drift; the previous
    behaviour was to write the bad value and emit a warning on next load.
    """
    _validate_status_write("task", task.status)
    task_path = tasks_dir / task.id / "task.yaml"

    with _locked_task_dir(task.id, tasks_dir):
        if task_path.exists():
            with open(task_path) as f:
                task_data = _yload(f) or {}
        else:
            task_data = {}

        # C2 — task.current_phase is a derived field. Every writer of task.yaml
        # recomputes it from the canonical helper so the persisted value can
        # never drift past the blocking phase (D1 root). The in-memory
        # task.current_phase is also re-aligned so engine code reading from
        # the task object sees the same value as readers consuming task.yaml.
        derived_phase = derive_current_phase(task)
        task.current_phase = derived_phase
        task_data["current_phase"] = derived_phase

        # ── Closed write schema ──────────────────────────────────────────
        # Required identity / creation metadata — always re-asserted from
        # the in-memory object so a stale on-disk row can never linger.
        task_data["id"] = task.id
        task_data["description"] = task.description
        task_data["status"] = task.status
        task_data["created_at"] = task.created_at
        # Deliberation-on-continuation task-level default (tri-state). Only
        # persisted when explicitly set so unaffected tasks stay clean.
        if task.deliberate_continuations is not None:
            task_data["deliberate_continuations"] = task.deliberate_continuations
        elif "deliberate_continuations" in task_data:
            del task_data["deliberate_continuations"]
        task_data["mode"] = task.mode

        # Optional fields — write iff truthy on the in-memory object,
        # delete from the dict otherwise. The disk reflects in-memory
        # state for EVERY known field; "silently preserved" is not a
        # valid state for any key in the closed schema.
        if task.title:
            task_data["title"] = task.title
        elif "title" in task_data:
            del task_data["title"]
        if task.preferred_cli:
            task_data["preferred_cli"] = task.preferred_cli
        elif "preferred_cli" in task_data:
            del task_data["preferred_cli"]
        if task.intelligence:
            task_data["intelligence"] = task.intelligence
        elif "intelligence" in task_data:
            del task_data["intelligence"]
        if task.review_trigger:
            task_data["review_trigger"] = task.review_trigger
        elif "review_trigger" in task_data:
            del task_data["review_trigger"]
        if task.links:
            task_data["links"] = list(task.links)
        elif "links" in task_data:
            del task_data["links"]
        if task.task_type and task.task_type != "oneshot":
            task_data["task_type"] = task.task_type
        elif "task_type" in task_data:
            del task_data["task_type"]
        if task.recurring_def_id:
            task_data["recurring_def_id"] = task.recurring_def_id
        elif "recurring_def_id" in task_data:
            del task_data["recurring_def_id"]
        if task.source_todo_id:
            task_data["source_todo_id"] = task.source_todo_id
        elif "source_todo_id" in task_data:
            del task_data["source_todo_id"]
        if task.source_thought_id:
            task_data["source_thought_id"] = task.source_thought_id
        elif "source_thought_id" in task_data:
            del task_data["source_thought_id"]
        if task.required_roles:
            task_data["required_roles"] = list(task.required_roles)
        elif "required_roles" in task_data:
            del task_data["required_roles"]
        if task.continuation_suggestions:
            task_data["continuation_suggestions"] = list(task.continuation_suggestions)
        elif "continuation_suggestions" in task_data:
            del task_data["continuation_suggestions"]
        if task.auto_approve_risk and task.auto_approve_risk != "none":
            task_data["auto_approve_risk"] = task.auto_approve_risk
        elif "auto_approve_risk" in task_data:
            del task_data["auto_approve_risk"]
        if task.auto_approve_yes:
            task_data["auto_approve_yes"] = True
        elif "auto_approve_yes" in task_data:
            del task_data["auto_approve_yes"]
        # The drawn-workflow binding. Absent rather than empty when unset, so a
        # task planned by the LLM decomposer stays visibly free of it.
        if task.workflow_id:
            task_data["workflow_id"] = task.workflow_id
        elif "workflow_id" in task_data:
            del task_data["workflow_id"]
        if task.workflow_params:
            task_data["workflow_params"] = dict(task.workflow_params)
        elif "workflow_params" in task_data:
            del task_data["workflow_params"]
        if task.project_path:
            task_data["project_path"] = task.project_path
        elif "project_path" in task_data:
            # Clearing project_path: remove rather than persist empty
            # string, so untouched tasks stay clean and the field's
            # absence remains a real signal ("never set") for the
            # proposer fallback.
            del task_data["project_path"]
        if task.conventions:
            task_data["conventions"] = task.conventions
        elif "conventions" in task_data:
            del task_data["conventions"]
        if task.verify_command:
            task_data["verify_command"] = task.verify_command
        elif "verify_command" in task_data:
            del task_data["verify_command"]
        # Part of the closed schema now, so it round-trips instead of merely
        # surviving as an unknown key nobody claims.
        if task.halt_reason:
            task_data["halt_reason"] = task.halt_reason
        elif "halt_reason" in task_data:
            del task_data["halt_reason"]
        if task.gates_enabled:
            task_data["gates_enabled"] = True
        elif "gates_enabled" in task_data:
            del task_data["gates_enabled"]
        # Legacy session-loop fork removed (converged on streaming-only).
        # Drop any persisted opt-out key from pre-cutover task.yaml so it
        # doesn't linger; the streaming path is now unconditional.
        if "session_loop_enabled" in task_data:
            del task_data["session_loop_enabled"]
        if task.adrs:
            task_data["adrs"] = list(task.adrs)
        elif "adrs" in task_data:
            del task_data["adrs"]
        if task.deliberation:
            task_data["deliberation"] = {
                "current_round": task.deliberation.current_round,
                "strategy": task.deliberation.strategy,
                "panel": task.deliberation.panel,
            }
        elif "deliberation" in task_data:
            del task_data["deliberation"]
        if task.interventions:
            task_data["interventions"] = [asdict(i) for i in task.interventions]
        elif "interventions" in task_data:
            del task_data["interventions"]
        # C1 + C8 root — task.awaiting is part of the closed schema. The
        # in-memory value (None or an Awaiting dataclass) is the truth.
        # Pre-fix: update_task_status read disk → merged known fields →
        # wrote back; the existing awaiting block survived even when the
        # engine's in-memory ``task.awaiting`` was None (D6 / RC3).
        #
        # Gate 2 §C8 — atomic coupling enforced at the canonical writer:
        # ``task.status != "waiting_user"`` ⇒ ``task.awaiting is None``.
        # The two fields collapse to one logical state at the persistence
        # boundary. Callers (engine.py gate-resolve sites, api endpoints)
        # that flip status to ``active`` without explicitly clearing the
        # in-memory awaiting block can no longer leave the disk in a
        # divergent state. The clear-on-write direction is chosen because
        # every observed caller's intent is "leave the wait" (engine.py
        # 2730/2744/2797/2801, update_task_status). The in-memory object
        # is also aligned so a subsequent read of ``task.awaiting`` sees
        # the same view the disk reflects.
        if task.status != "waiting_user" and getattr(task, "awaiting", None):
            task.awaiting = None
        if getattr(task, "awaiting", None):
            task_data["awaiting"] = asdict(task.awaiting)
        elif "awaiting" in task_data:
            del task_data["awaiting"]

        _atomic_write(task_path, yaml.dump(task_data, default_flow_style=False))


def mark_engine_exited(task_id: str, tasks_dir: Path, *, reason: str = "") -> None:
    """Stamp a sentinel file the liveness check honors over the spawn-grace
    window. Engine writes this on EVERY graceful return path (panel wait,
    capability gap, blocked review, decision gate, done, failed, halted).

    Without this, `_is_orchestrator_running` returns True for up to 15s
    after the engine cleanly exits, because launcher.sh's mtime is fresh
    and pid+heartbeat are absent (engine exited before the work loop wrote
    them). API endpoints that call `_ensure_engine_running` then skip the
    respawn, and the task wedges silently.

    Companion: `spawn_orchestrator` must delete this file before launching
    so a new spawn isn't treated as a dead engine. Idempotent.
    """
    try:
        path = tasks_dir / task_id / ".exited"
        path.write_text(reason or datetime.utcnow().isoformat())
    except Exception:
        pass


def set_awaiting(
    task: Task,
    tasks_dir: Path,
    *,
    kind: str,
    message: str,
    endpoint: str,
    method: str = "POST",
    payload: Optional[dict] = None,
) -> None:
    """Mark the task as blocked on user input. Persists to disk + log.

    Every engine code path that exits the work loop because it cannot
    progress without a user decision MUST call this — the alternative is
    the silent-deliberating wedge that previously left tasks stuck for
    hours with no UX surface. Status flips to 'waiting_user' so list views
    and filters can distinguish "engine working" from "engine waiting".

    SELF-HEALING PRESENTATION, and why it heals instead of refusing.
    ``payload["presentation"]`` is what the UI renders as the blocker card.
    A few call sites omit it, so those gates reach the user as a raw kind
    string. The tempting fix is to REFUSE such a payload — but 5 of the 9
    engine call sites wrap this function in ``try/except Exception`` that only
    ``logger.warning``s (e.g. "discussion set_awaiting failed: %r"). A raise
    there is swallowed, the task neither progresses nor shows a blocker, and
    the result is exactly the silent wedge the paragraph above says this
    function exists to prevent — worse than the missing card.

    So a missing presentation is BUILT here via ``humanize_gate`` and logged as
    a warning. This function must never raise for a presentation reason. The
    invariant is enforced by a TEST over the call sites
    (``tests/orchestrator/correctness/test_gate_presentation.py``), which is
    the right place for it: a test failure blocks the commit, a runtime raise
    blocks the user.
    """
    # C1 — single locked writer cohort. Both the in-memory mutation and
    # the persistence step happen under the shared task-dir lock so a
    # parallel reader (engine work-loop / reconciler) cannot observe
    # ``task.status="waiting_user"`` with ``task.awaiting=None`` or vice
    # versa on disk.
    with _locked_task_dir(task.id, tasks_dir):
        # Gate 2 §C8 — idempotency on the (kind, endpoint, message,
        # payload) tuple. Re-emitting the same wait inside the same task
        # MUST NOT append a duplicate ``awaiting_user`` event. Live
        # evidence: V4 row in 03-violation-map.md — cancelled task had
        # two ``awaiting_user blocked_review`` emits 36 s apart for the
        # same phase. The check is structural (writer-level), not
        # per-call-site, so every engine path that re-traverses a wait
        # site benefits.
        existing = getattr(task, "awaiting", None)
        new_payload = dict(payload or {})
        # Self-healing presentation — see the docstring for why this builds
        # rather than refuses. Runs BEFORE the idempotency compare so a healed
        # payload and a hand-authored one converge on the same value and a
        # re-emit still de-duplicates.
        if not new_payload.get("presentation"):
            try:
                from okuro.orchestrator.gate_messages import humanize_gate

                _healed = humanize_gate(kind, "", new_payload)
                new_payload["presentation"] = _healed.to_presentation()
                logger.warning(
                    "set_awaiting(%s) for task %s carried no presentation — "
                    "synthesized one via humanize_gate. The call site should "
                    "pass its own; see test_gate_presentation.py",
                    kind, task.id,
                )
            except Exception as exc:  # noqa: BLE001
                # NEVER raise from here: 5 of the 9 engine call sites swallow
                # exceptions into logger.warning, so a raise wedges the task
                # with no UX surface at all.
                logger.warning(
                    "set_awaiting(%s) presentation synthesis failed (%r) — "
                    "continuing without one; the gate still persists",
                    kind, exc,
                )
        if (
            existing is not None
            and existing.kind == kind
            and existing.endpoint == endpoint
            and existing.message == message
            and existing.method == method
            and existing.payload == new_payload
        ):
            # Already waiting on the same thing; keep the original
            # ``since`` timestamp so "stuck for N min" displays don't reset.
            return
        task.awaiting = Awaiting(
            kind=kind,
            message=message,
            endpoint=endpoint,
            method=method,
            payload=new_payload,
            since=datetime.utcnow().isoformat(),
        )
        task.status = "waiting_user"
        save_task_meta(task, tasks_dir)
        append_log(task.id, {
            "type": "awaiting_user",
            "kind": kind,
            "message": message,
            "endpoint": endpoint,
            "method": method,
        }, tasks_dir)
    # NOTE: do NOT call mark_engine_exited from here. Only 2 of 5 awaiting
    # kinds (panel_confirmation, capability_gap) actually precede an engine
    # return. The other 3 (discussion_proceed, blocked_review, decision_gate)
    # fire from the still-alive work loop, and marking exit there would
    # cause the API's next _ensure_engine_running call to spawn a SECOND
    # engine concurrent with the first → live evidence: task wedged with
    # two systemd scopes both decomposing. Engine sites that actually
    # return call mark_engine_exited explicitly.


def couple_task_dict(task_data: dict) -> dict:
    """Apply the C8 status/awaiting coupling to a RAW task.yaml dict (P4.10).

    ``save_task_meta`` is the canonical writer and enforces this: a task whose
    status is not ``waiting_user`` cannot carry an ``awaiting`` block. Six
    call sites flip status by loading task.yaml into a dict, editing it, and
    dumping it back — cancel, resume, resume-if-idle, timeout-cap resolve,
    the engine's exit reconciler and the watchdog. Every one of them goes
    around the invariant, and the failure is silent: the task reads as active
    while still carrying a gate, and the engine's ``_is_human_park`` parks it
    again on its first tick. That is exactly the shape P4.7 found in retry.

    Rewriting all six to construct Task objects would be a real refactor of
    working code; enforcing the same rule on the dict costs one call and
    leaves their style intact.

    Mutates and returns ``task_data`` for convenient inlining.
    """
    if task_data.get("status") != "waiting_user":
        task_data.pop("awaiting", None)
    return task_data


def refresh_awaiting_presentation(
    task: Task,
    tasks_dir: Path,
    *,
    reset_since: bool = False,
) -> dict:
    """Re-author a parked gate's card from the inputs it was built with (P2.4).

    THE PROBLEM. Gate copy is written once, by the engine, at park time — and
    a human park is engineless: the engine calls ``park_engine_exit`` and dies.
    Nothing is left running to re-render, so a card written by an older version
    of ``gate_messages`` stays on screen verbatim until the user answers it.
    Every copy fix this plan shipped reaches new gates only; the ones already
    waiting on a person — precisely the ones a person is reading — kept the old
    wording, and the only way to correct them was to re-park the task.

    WHY ``since`` IS KEPT BY DEFAULT, against the plan's literal wording.
    The plan says "fresh copy + fresh since". Fresh copy, yes. But ``since``
    answers "how long has this been waiting on you", and re-wording a card
    does not restart the wait — it is the same decision, better phrased.
    P2.2's TTL work reads ``since`` to stale-flag gates older than 48h, so a
    refresh that reset it would hand any caller a silent way to clear
    staleness. ``reset_since=True`` stays available for a genuine re-park,
    where the wait really did start over.

    Returns a small report: what changed, and whether the rebuild was possible
    at all. Gates parked before ``source`` existed cannot be rebuilt (the
    ``cause`` that selects the branch was never written down) — those come
    back ``rebuilt=False`` with the card untouched, rather than being
    re-rendered from a guessed cause into confidently wrong copy.
    """
    from okuro.orchestrator.gate_messages import rebuild

    with _locked_task_dir(task.id, tasks_dir):
        awaiting = getattr(task, "awaiting", None)
        if awaiting is None:
            return {"rebuilt": False, "reason": "no gate is open on this task"}

        payload = dict(awaiting.payload or {})
        before = payload.get("presentation") or {}
        message = rebuild(before)
        if message is None:
            return {
                "rebuilt": False,
                "reason": (
                    "this gate was parked before gates recorded what they were "
                    "built from, so its copy cannot be re-authored without "
                    "guessing why it opened"
                ),
            }

        after = message.to_presentation()
        changed = after != before
        if not changed and not reset_since:
            return {"rebuilt": True, "changed": False}

        payload["presentation"] = after
        task.awaiting = Awaiting(
            kind=awaiting.kind,
            message=message.to_message(),
            endpoint=awaiting.endpoint,
            method=awaiting.method,
            payload=payload,
            since=utc_now_naive().isoformat() if reset_since else awaiting.since,
        )
        # Status is NOT touched: the task was and remains waiting_user. Going
        # through save_task_meta keeps the C8 coupling invariant (status and
        # awaiting are written by one cohort) rather than reaching for a raw
        # YAML write — one of the six raw-writer sites P4.10 exists to close.
        save_task_meta(task, tasks_dir)

    # emit_event, NOT append_log: append_log is the raw file writer and does
    # nothing else. The C9 snapshot-cache invalidation (and with it the push
    # that reaches an open tab) is wired into emit_event's state_change pool.
    # Logging this with append_log would have left the corrected card sitting
    # in a cached snapshot — a stale-copy fix that the user cannot see is the
    # bug it set out to remove. Emitted OUTSIDE the task-dir lock: the
    # invalidation reaches into state_reader, and holding the lock across
    # that is how a cross-module deadlock starts.
    emit_event(task.id, "awaiting_refreshed", {
        "kind": awaiting.kind,
        "reset_since": reset_since,
    }, tasks_dir)

    return {"rebuilt": True, "changed": changed, "reset_since": reset_since}


def clear_awaiting(task: Task, tasks_dir: Path, *, new_status: str = "active") -> None:
    """Clear the awaiting block when user input arrives. Called by API handlers
    after they persist the user's decision and before respawning the engine.

    C1 — locks the shared task-dir cohort so the awaiting clear + status
    flip + persistence are observable as one atomic transition.
    """
    # Gate 2 §C8 sibling site — tolerate task objects without an
    # ``awaiting`` attribute. The deliberation API contract accepts any
    # task-shaped value (SimpleNamespace in tests, partial reloads after a
    # crash). Pre-fix: ``task.awaiting is None`` raised AttributeError on
    # SimpleNamespace lacking the attr, breaking test_capability_gap_api.
    current_awaiting = getattr(task, "awaiting", None)
    if current_awaiting is None and task.status != "waiting_user":
        return
    with _locked_task_dir(task.id, tasks_dir):
        prev_kind = current_awaiting.kind if current_awaiting else ""
        task.awaiting = None
        task.status = new_status
        save_task_meta(task, tasks_dir)
        append_log(task.id, {
            "type": "awaiting_user_cleared",
            "kind": prev_kind,
            "new_status": new_status,
        }, tasks_dir)
        # ROCK-SOLID v5 P1.2 — kill pill permanence at its single choke
        # point. Every resolve path funnels through this function, but none
        # of them told the pill the gate was answered: engine.py only calls
        # _emit_orchestrator_state from the WORK LOOP, and a resolve happens
        # in the API process while the engine may be mid-park (engineless)
        # or busy on another phase's work — nothing on the resolve path ever
        # wrote a fresh state, so the heartbeat thread (if the engine is
        # still alive for other phases) kept re-broadcasting the stale
        # waiting/error row every 3s forever (measured: 347 rows, 17 min,
        # on a task with no blocker). "idle" here is deliberately transient
        # — real work re-asserts "running"/"thinking" within moments once
        # the respawned or continuing engine picks the subtask back up.
        append_log(task.id, {
            "type": "orchestrator_state",
            "state": "idle",
            "label": "",
        }, tasks_dir)


def record_intervention(
    task_id: str,
    text: str,
    tasks_dir: Path,
    kind: str = "continuation",
    before_phase_id: Optional[int] = None,
    source: str = "user_typed",
    deliberate: Optional[bool] = None,
) -> Intervention:
    """Persist a user intervention to ``task.yaml`` under file lock.

    Called the moment a continuation arrives — BEFORE decomposition runs —
    so the prompt survives any planner crash. ``before_phase_id`` defaults
    to "next phase to be created", computed from the highest existing
    phase id + 1.

    ``source`` distinguishes free-text user prompts from agent-suggested
    continuations the user clicked-to-accept; only ``initial`` and
    ``user_typed`` surface as cards in the default flow chart view.
    """
    with _locked_task_dir(task_id, tasks_dir):
        task = load_task(task_id, tasks_dir)
        # UTC — every other task.yaml timestamp (created_at, awaiting.since,
        # subtask started_at/completed_at) is utcnow(). datetime.now() here
        # wrote LOCAL-naive ts, so interventions appeared offset from the UTC
        # SPAWNED markers in startup.log (a 2h skew in CEST), breaking any
        # cross-timeline ordering / "stuck for N min" math.
        now = datetime.utcnow()
        # Dedupe: continue endpoint + engine.py both call this for the
        # same prompt. Skip if the last recorded intervention has the
        # same kind+text and was written within the last 60 seconds.
        if task.interventions:
            last = task.interventions[-1]
            if last.kind == kind and last.text == text:
                try:
                    last_ts = datetime.fromisoformat(last.ts)
                    if (now - last_ts).total_seconds() < 60:
                        return last
                except ValueError:
                    pass
        if before_phase_id is None:
            before_phase_id = (max((p.id for p in task.phases), default=0) + 1)
        intervention = Intervention(
            id=f"int-{now.strftime('%Y%m%d-%H%M%S-%f')}",
            ts=now.isoformat(),
            kind=kind,
            text=text,
            before_phase_id=before_phase_id,
            source=source,
            deliberate=deliberate,
        )
        task.interventions.append(intervention)
        save_task_meta(task, tasks_dir)
        return intervention


def delete_intervention(
    task_id: str,
    intervention_id: str,
    tasks_dir: Path,
) -> Intervention:
    """Remove a not-yet-started continuation prompt from ``task.yaml``.

    Only *trailing* continuations are deletable — i.e. a continuation
    whose ``before_phase_id`` is higher than every materialized phase id,
    meaning the decomposer never turned it into a plan (the "queued, not
    yet started" prompt the user can still retract). Guards:

    - ``KeyError``   — no intervention with that id on the task.
    - ``ValueError`` — the intervention is the ``initial`` prompt, or it
                       already materialized into one or more phases
                       (``before_phase_id`` <= the highest phase id).

    Returns the removed :class:`Intervention` on success. Thread-safe via
    the per-task dir lock.
    """
    with _locked_task_dir(task_id, tasks_dir):
        task = load_task(task_id, tasks_dir)
        target = next(
            (i for i in task.interventions if i.id == intervention_id), None
        )
        if target is None:
            raise KeyError(intervention_id)
        if target.kind == "initial":
            raise ValueError("the initial task prompt cannot be deleted")
        max_phase_id = max((p.id for p in task.phases), default=0)
        if target.before_phase_id <= max_phase_id:
            raise ValueError(
                "intervention already produced phases and cannot be deleted"
            )
        task.interventions = [
            i for i in task.interventions if i.id != intervention_id
        ]
        save_task_meta(task, tasks_dir)

    # P3.4 — the deletion is consumer-visible state, so it needs a frame.
    # save_task_meta moves task.yaml's mtime, which means a cache READ
    # self-heals; what was missing is anything to trigger that read, so an
    # open page kept showing a retracted prompt until its next poll.
    #
    # Emitted at the state writer rather than at the endpoint: the API is not
    # the only caller this function can ever have, and putting it here means a
    # future caller cannot forget. OUTSIDE the lock — emit_event reaches into
    # state_reader to invalidate, and holding the task-dir lock across that
    # module boundary is a deadlock shape.
    emit_event(task_id, "intervention_deleted", {
        "intervention_id": target.id,
        "kind": target.kind,
        "before_phase_id": target.before_phase_id,
    }, tasks_dir)
    return target


def pending_continuations(task) -> list["Intervention"]:
    """Continuation interventions recorded but never decomposed into phases.

    A continuation is "queued, not yet started" when its ``before_phase_id``
    is higher than every materialized phase id — the decomposer never turned
    it into a plan. This is the durable on-disk signal used to close the
    continue-vs-resume race: when a continuation arrives for an already-`done`
    task, the ``--continue`` engine can be pre-empted by a daemon ``--resume``
    respawn before ``decompose_continuation`` runs, leaving the intervention
    orphaned and the task silently back at ``done``. Any engine that picks the
    task up drains these (see engine ``_drain_pending_continuations``), so the
    winner of the race no longer matters.

    Returns them in record order. Excludes the ``initial`` prompt.
    """
    max_phase_id = max((p.id for p in task.phases), default=0)
    return [
        i for i in task.interventions
        if i.kind == "continuation"
        and i.before_phase_id > max_phase_id
        # A continuation whose council round has been seeded is NOT pending:
        # its phases are deferred until the council resolves. Excluding it
        # stops the drain from re-seeding a fresh council on every engine
        # tick while positions/discussion are still running.
        and i.council_round is None
    ]


def update_subtask(task_id: str, subtask_id: str, updates: dict, tasks_dir: Path):
    """Update a subtask and log the changes. Thread-safe via file locking.

    Gate 2 §C6 — invalid status enum values raise InvalidStatusError BEFORE
    the lock is acquired. The validator is authoritative at this write
    boundary; previously the engine wrote arbitrary strings and the next
    load_task call emitted a warning-only validation error.

    Gate 2 §C3 — subtask is the SoT for execution nodes; this writer
    drives the derived graph view. A late ``done`` arriving after an
    M3 reset (current pending+retries>0) is rejected via the dispatch-
    generation guard; downstream callers see no event and the prior
    reset is preserved.
    """
    _validate_status_write("subtask", updates.get("status", ""))
    # review_state is validated at the same write boundary as status — it is a
    # closed enum too, and a typo there would silently unlock or wedge the
    # dispatch guard.
    _validate_status_write("review_state", updates.get("review_state", ""))
    task_path = tasks_dir / task_id
    plan_path = task_path / "plan.yaml"

    with _locked_task_dir(task_id, tasks_dir):
        task = load_task(task_id, tasks_dir)
        # C3 — late-write rejection BEFORE any mutation. Reads the
        # persisted subtask state under the lock so reset writes from
        # a sibling caller are visible.
        for _phase in task.phases:
            for _st in _phase.subtasks:
                if _st.id == subtask_id:
                    if _reject_late_done_write(
                        _st.status,
                        int(getattr(_st, "retries", 0) or 0),
                        updates,
                        "subtask",
                        subtask_id,
                    ):
                        return
                    break
            else:
                continue
            break

        found = False
        for phase in task.phases:
            for subtask in phase.subtasks:
                if subtask.id == subtask_id:
                    for key, value in updates.items():
                        if hasattr(subtask, key):
                            setattr(subtask, key, value)
                    found = True
                    break
            if found:
                break

        if not found:
            raise ValueError(f"Subtask {subtask_id} not found in task {task_id}")

        # C3 — graph nodes type=execution are derived views. Regenerate
        # status (and the mirrored timing/error fields) from the SoT
        # subtask + parent phase BEFORE writing. Replaces the prior
        # field-by-field mirror loop, which only copied updates and
        # never reconciled with phase.status.
        if task.graph and task.graph.nodes:
            _apply_execution_node_derivation(task)

        # Shared builder — see _phase_to_dict. This call site used to emit its
        # own literal that dropped serialize + decision_gate on every subtask
        # update, which disarmed phase gating across all 310 live plans.
        phases_data = [_phase_to_dict(phase) for phase in task.phases]

        plan_payload: dict = {"phases": phases_data}
        # Preserve the DAG block — deliberate-mode tasks store nodes
        # under plan.yaml:graph and update_subtask would otherwise drop
        # it on every write, leaving load_task with task.graph=None.
        if task.graph and task.graph.nodes:
            plan_payload["graph"] = _serialize_graph(task.graph)
        _atomic_write(plan_path, yaml.dump(plan_payload, default_flow_style=False))

    event_type = f"subtask_{updates['status']}" if "status" in updates else "subtask_updated"
    append_log(task_id, {"type": event_type, "subtask": subtask_id, **updates}, tasks_dir)


# Two ORTHOGONAL axes, deliberately NOT one set. The old single
# TERMINAL_STATUSES fused them, and every downstream pathology traced back to
# that fusion (see the module note below).
#
# SATISFIED — "this subtask's output can be depended on". Consumed by
# dependency resolution only. `failed` is absent: a dependent must NOT run on
# a failed input.
#
# FINISHED — "this subtask no longer needs the dispatcher's attention".
# Consumed by the phase barrier and completion checks. `failed` IS present: a
# permanently-failed subtask is done being worked on, whatever its outcome.
#
# Why the split exists (2026-07-24). `failed` used to be in neither role's set
# — it was simply missing from TERMINAL_STATUSES — so a permanently-failed
# subtask wedged the phase barrier and is_task_complete forever. Two separate
# workarounds papered over the wedge by force-writing `skipped` onto real
# state: the engine's cascade-skip (every transitive dependent) and the
# override endpoint (the failed subtask itself). That laundering made `skipped`
# mean three different things at once — never ran / user accepted / upstream
# dead — which in turn made recovery impossible: a cascade-skip was never
# reverted when its upstream was retried green, so a transient failure silently
# killed every later phase (task-20260724-110216 phases 5-6). Splitting the
# axes removes the wedge at its source, so neither workaround is needed and
# both were deleted.
SATISFIED_STATUSES: frozenset[str] = frozenset({"done", "skipped", "superseded"})
FINISHED_STATUSES: frozenset[str] = SATISFIED_STATUSES | frozenset({"failed"})

# Subtask statuses that can be made superseded by a later continuation.
# "done" and "failed" are historical truth and never get rewritten; "running"
# must be cancelled first (which resets to pending) before supersession.
SUPERSEDEABLE_STATUSES: frozenset[str] = frozenset({"pending", "approved"})


def get_next_subtask(task: Task) -> Optional[Subtask]:
    """Find the next subtask that's ready to run."""
    statuses = {}
    for phase in task.phases:
        for subtask in phase.subtasks:
            statuses[subtask.id] = subtask.status

    for phase in task.phases:
        for subtask in phase.subtasks:
            if subtask.status in ("pending", "approved"):
                deps_satisfied = all(
                    statuses.get(dep_id) in SATISFIED_STATUSES
                    for dep_id in subtask.dependencies
                )
                if deps_satisfied:
                    return subtask
    return None


def get_ready_subtasks(task: Task, *, max_retries: Optional[int] = None) -> list[Subtask]:
    """Find ALL subtasks that are ready to run (for parallel execution).

    M1 — when `task.gates_enabled`, two extra guards apply:
      1. A phase whose ``decision_gate.status`` is ``pending`` blocks all of
         its subtasks until the gate is resolved (the engine handles the
         pause/wait; this function just hides them from the dispatcher).
      2. A phase with ``serialize=True`` returns at most one ready subtask;
         later candidates are deferred until the running one terminates.
    Both guards are no-ops when ``gates_enabled`` is False so legacy
    auto-execute behaviour is preserved bit-for-bit.

    Gate 2 §C5 — task-level halts (``task.status`` in halt set OR
    ``task.awaiting`` of a halt-set kind) short-circuit the result. The
    parent-phase ``blocked_review`` check and the retries cap (when
    ``max_retries`` is supplied) are applied per subtask so this reader
    and the graph-side ``get_ready_nodes`` share one source of truth
    — see ``deliberation._task_halt_reason`` /
    ``deliberation.is_dispatchable``.
    """
    # C5 task-level halt — short-circuit before walking the plan. This
    # is the same predicate get_ready_nodes consults, hoisted here so
    # plan-side dispatch (auto-execute work loop) and graph-side dispatch
    # (deliberate work loop) cannot diverge on the halted / awaiting axes.
    # Local import keeps state.py and deliberation.py free of a circular
    # module dependency (deliberation already imports from state).
    from okuro.orchestrator.deliberation import (
        _task_halt_reason, _BLOCKED_PHASE_STATUSES, _DEFAULT_MAX_RETRIES,
    )

    if _task_halt_reason(task) is not None:
        return []

    cap = int(max_retries) if max_retries is not None else _DEFAULT_MAX_RETRIES

    done_ids = set()
    running_per_phase: dict[int, int] = {}
    for phase in task.phases:
        for subtask in phase.subtasks:
            # SATISFIED, not FINISHED — a `failed` upstream must keep its
            # dependents out of the ready set. They stay `pending` (never
            # written to disk as skipped) so retrying the upstream green makes
            # them runnable again with no revive step.
            if subtask.status in SATISFIED_STATUSES:
                done_ids.add(subtask.id)
            elif subtask.status == "running":
                running_per_phase[phase.id] = running_per_phase.get(phase.id, 0) + 1

    gates_enabled = bool(getattr(task, "gates_enabled", False))

    # Hard phase barrier (user contract, 2026-06-03). A phase may dispatch
    # ONLY when every earlier phase (lower id) is fully terminal. Phases are
    # sequential gates: a later-phase subtask never starts before all prior
    # phases complete, regardless of how sparse its own dependency edges are.
    # Without this the scheduler is purely dependency-driven, so an isolated
    # later-phase subtask (e.g. a freak-paper researcher wired only to phase 1)
    # dispatches into a free parallel slot mid-phase-1 — which breaks the
    # phase-by-phase mental model. A phase with no subtasks counts complete
    # (all([]) is True) so empty phases never stall the chain.
    # FINISHED, not SATISFIED — the barrier asks "is this phase still being
    # worked on", and a permanently-failed subtask is not. Using SATISFIED here
    # is what wedged the barrier forever behind a failed subtask.
    _phase_complete: dict[int, bool] = {
        p.id: all(st.status in FINISHED_STATUSES for st in p.subtasks)
        for p in task.phases
        if isinstance(getattr(p, "id", None), int)
    }

    def _earlier_phases_complete(pid: int) -> bool:
        return all(done for q, done in _phase_complete.items() if q < pid)

    ready: list[Subtask] = []
    phase_ready_count: dict[int, int] = {}
    for phase in task.phases:
        # C5 — a phase in blocked_review halts every subtask underneath
        # regardless of gates_enabled: the cap-fire reset path leaves
        # subtask.status="pending" with retries==cap, and dispatching
        # again would re-open V3 / V8.
        if phase.status in _BLOCKED_PHASE_STATUSES:
            continue
        # Phase barrier — defer this phase until all earlier phases are done.
        pid = getattr(phase, "id", None)
        if isinstance(pid, int) and not _earlier_phases_complete(pid):
            continue
        gate = getattr(phase, "decision_gate", None)
        gate_blocks = (
            gates_enabled
            and gate is not None
            and getattr(gate, "status", "pending") == "pending"
        )
        if gate_blocks:
            continue
        serialize = gates_enabled and bool(getattr(phase, "serialize", False))
        for subtask in phase.subtasks:
            # Mirror of deliberation.is_dispatchable's review lock — the two
            # readers agree by contract. A `failed` review holds re-dispatch
            # until the human resolves it; `overridden` does not.
            if getattr(subtask, "review_state", "") == "failed":
                continue
            if subtask.status in ("pending", "approved"):
                # Retry invariant (single source of truth — mirrored in
                # deliberation.is_dispatchable and engine.handle_failure):
                # a subtask gets ``cap`` correction attempts after its
                # first run = ``cap + 1`` total dispatches. ``retries``
                # is 0 on the first run and incremented on each FAIL.
                # handle_failure resets to ``pending`` while
                # ``retries < cap`` (final reset → retries == cap), so the
                # scheduler MUST dispatch the ``retries == cap`` attempt;
                # it refuses only ABOVE the cap. The next FAIL at
                # ``retries == cap`` hits handle_failure's cap branch,
                # which escalates via set_awaiting instead of resetting —
                # so ``retries > cap`` is a defensive backstop that, in
                # normal operation, never strands a subtask in pending.
                if int(getattr(subtask, "retries", 0) or 0) > cap:
                    continue
                if all(dep_id in done_ids for dep_id in subtask.dependencies):
                    if serialize:
                        already_running = running_per_phase.get(phase.id, 0)
                        already_picked = phase_ready_count.get(phase.id, 0)
                        if already_running + already_picked >= 1:
                            continue
                        phase_ready_count[phase.id] = already_picked + 1
                    ready.append(subtask)
    return ready


def pending_decision_gates(task: Task) -> list[tuple[int, "DecisionGate"]]:
    """Return (phase_id, gate) for EVERY phase whose decision_gate is
    pending — regardless of earlier-phase completion status. M1 strict
    semantics: a load-bearing decision the user has not yet locked blocks
    ALL dispatch, including upstream research that COULD have run without
    the answer. The user's framing in the contract is "USER GATE … then
    build" — that user reads "gate visible" as "I have to answer before
    anything happens", and the previous "earlier_terminal" gating let
    research dispatch silently behind a visible-but-not-blocking gate.

    Returned in plan order; the engine pauses on the first entry. As the
    user locks each gate, subsequent iterations surface the next pending
    one until the list is empty.

    Only returns gates when ``task.gates_enabled`` — disabled tasks
    short-circuit so the engine never blocks even if a planner
    accidentally emits a gate.
    """
    if not getattr(task, "gates_enabled", False):
        return []
    out: list[tuple[int, DecisionGate]] = []
    for phase in sorted(task.phases, key=lambda p: p.id):
        gate = getattr(phase, "decision_gate", None)
        if gate is None or getattr(gate, "status", "pending") != "pending":
            continue
        out.append((phase.id, gate))
    return out


def resolve_decision_gate(
    task_id: str,
    phase_id: int,
    selected_option_id: str,
    rationale: str,
    tasks_dir: Path,
    skipped: bool = False,
) -> dict:
    """Lock a user decision into the phase gate AND append an ADR to
    task.adrs under file lock. Emits `await_user_decision_resolved` or
    `await_user_decision_skipped` event. Returns the ADR dict that was
    appended (empty dict when skipped).

    Raises ValueError when the phase or gate is missing, when the gate is
    already resolved, or when ``selected_option_id`` does not match any
    option on the gate.
    """
    adr: dict = {}
    with _locked_task_dir(task_id, tasks_dir):
        task = load_task(task_id, tasks_dir)
        target_phase: Optional[Phase] = None
        for phase in task.phases:
            if phase.id == phase_id:
                target_phase = phase
                break
        if target_phase is None:
            raise ValueError(f"Phase {phase_id} not found in task {task_id}")
        gate = target_phase.decision_gate
        if gate is None:
            raise ValueError(f"Phase {phase_id} has no decision_gate")
        if gate.status != "pending":
            raise ValueError(f"Gate {gate.id} already {gate.status}")

        if skipped:
            gate.status = "skipped"
            gate.resolved_at = datetime.utcnow().isoformat()
        else:
            option = next(
                (o for o in gate.options if o.id == selected_option_id),
                None,
            )
            if option is None:
                raise ValueError(
                    f"Option {selected_option_id!r} not on gate {gate.id} "
                    f"(have: {[o.id for o in gate.options]})"
                )
            gate.status = "resolved"
            gate.selected_option_id = option.id
            gate.selected_rationale = rationale or ""
            gate.resolved_at = datetime.utcnow().isoformat()
            adr = {
                "gate_id": gate.id,
                "phase_id": phase_id,
                "prompt": gate.prompt,
                "options": [asdict(o) for o in gate.options],
                "selected_option_id": option.id,
                "selected_label": option.label,
                "selected_rationale": gate.selected_rationale,
                "resolved_at": gate.resolved_at,
            }
            task.adrs = list(task.adrs) + [adr]

        save_plan(task, tasks_dir)
        save_task_meta(task, tasks_dir)

    if skipped:
        append_log(task_id, {
            "type": "await_user_decision_skipped",
            "phase_id": phase_id,
            "gate_id": gate.id,
        }, tasks_dir)
    else:
        append_log(task_id, {
            "type": "await_user_decision_resolved",
            "phase_id": phase_id,
            "gate_id": gate.id,
            "selected_option_id": gate.selected_option_id,
        }, tasks_dir)
    return adr


def lock_blocked_review_decision(
    task_id: str,
    phase_id: int,
    subtask_id: str,
    decision: str,
    tasks_dir: Path,
    *,
    rationale: str = "",
    chosen_label: str = "",
    rejected_labels: Optional[list[str]] = None,
    findings: Optional[list] = None,
) -> dict:
    """Lock a free-form user adjudication of a blocked_review NEEDS_USER finding.

    Unlike :func:`resolve_decision_gate`, this needs NO pre-existing
    ``decision_gate`` on the phase — the reviewer can escalate any phase to
    NEEDS_USER, and the user's decision must be capturable there. It:

      1. Appends an ADR to ``task.adrs`` — so the decision is durable AND the
         deterministic term-consistency check can enforce it on every future
         round (``chosen_label``/``rejected_labels`` become the ADR options the
         check derives drift terms from). This is what closes the gap where a
         deliberation task never locked a decision to check against.
      2. Stamps the decision onto the implicated subtask's ``decision_guidance``
         so ``build_role_prompt`` LEADS the re-dispatch with it as authoritative
         — the agent applies the call instead of re-inventing one each round.
      3. Resets the implicated subtask + its phase to ``pending`` for a scoped,
         DECIDED re-dispatch (not a blind retry).

    All mutations happen under the task-dir file lock and persist via
    ``save_plan`` (subtask/phase + decision_guidance → plan.yaml) and
    ``save_task_meta`` (adrs → task.yaml). Returns the appended ADR dict.
    """
    rejected_labels = list(rejected_labels or [])
    findings = list(findings or [])
    adr: dict = {}
    with _locked_task_dir(task_id, tasks_dir):
        task = load_task(task_id, tasks_dir)
        target_phase: Optional[Phase] = next(
            (p for p in task.phases if p.id == phase_id), None,
        )
        if target_phase is None:
            raise ValueError(f"Phase {phase_id} not found in task {task_id}")

        # Options carry the chosen + rejected labels so
        # _superseded_terms_from_adrs can derive enforcement terms. Free-text
        # decisions with no chosen/rejected still record the ADR (durable +
        # rendered into the brief) — they just don't drive the term check.
        options: list[dict] = []
        if chosen_label:
            options.append({"id": "chosen", "label": chosen_label, "description": ""})
        for i, rl in enumerate(rejected_labels):
            options.append({
                "id": f"rejected-{i}", "label": rl,
                "description": "rejected by user decision",
            })
        resolved_at = datetime.utcnow().isoformat()
        adr = {
            "gate_id": f"blocked-review-decision-p{phase_id}-{subtask_id}",
            "phase_id": phase_id,
            "prompt": f"User decision on blocked_review (phase {phase_id}, {subtask_id})",
            "options": options,
            "selected_option_id": "chosen" if chosen_label else "",
            "selected_label": chosen_label or decision,
            "selected_rationale": rationale or decision,
            "resolved_at": resolved_at,
            "source": "blocked_review_decide",
        }
        task.adrs = list(task.adrs) + [adr]

        target_phase.status = "pending"
        for st in target_phase.subtasks:
            if st.id != subtask_id:
                continue
            st.decision_guidance = list(getattr(st, "decision_guidance", []) or []) + [{
                "decision": decision, "rationale": rationale, "ts": resolved_at,
            }]
            if findings:
                st.review_findings = list(findings)
            if st.status in ("done", "failed", "skipped"):
                st.status = "pending"
                st.retries = 0
                st.error = ""
                # Clear the review lock. get_ready_subtasks skips any subtask
                # with review_state == "failed" (and deliberation
                # .is_dispatchable mirrors it), so resetting status alone left
                # the subtask pending-but-undispatchable — the engine parked
                # again immediately and the user's decision achieved nothing.
                # The retry and override paths already do this; Decide was the
                # one revival path that did not. A DECIDED re-dispatch is
                # entitled to a fresh verdict, exactly like a retry.
                st.review_state = "not_reviewed"

        save_plan(task, tasks_dir)
        save_task_meta(task, tasks_dir)

    append_log(task_id, {
        "type": "blocked_review_decided",
        "phase_id": phase_id,
        "subtask": subtask_id,
        "decision": (decision or "")[:300],
        "chosen_label": chosen_label,
        "rejected_labels": rejected_labels,
    }, tasks_dir)
    return adr


def is_phase_complete(task: Task, phase_id: int) -> bool:
    for phase in task.phases:
        if phase.id == phase_id:
            return all(st.status in FINISHED_STATUSES for st in phase.subtasks)
    return False


def blocked_upstream_ids(task: Task) -> set[str]:
    """Subtask ids that cannot run because an ancestor finished unsatisfied.

    DERIVED, never persisted. This replaces the engine's old cascade-skip,
    which wrote ``status="skipped"`` onto every transitive dependent of a
    permanently-failed subtask. That write was the bug: it was a snapshot of a
    condition ("upstream is dead") stored as terminal fact, and nothing ever
    re-derived it. Retrying the upstream green left the dependents skipped
    forever, silently killing every later phase.

    Computing it per call means the blocked set follows upstream status for
    free — the instant a failed ancestor turns `done`, its dependents drop out
    of this set and the scheduler picks them up on the next loop. No revive
    step exists because none can be forgotten.

    A subtask is blocked when any transitive dependency is FINISHED but not
    SATISFIED (i.e. `failed`). Subtasks already finished are never blocked.
    """
    by_id = {st.id: st for phase in task.phases for st in phase.subtasks}
    dead = {
        sid for sid, st in by_id.items()
        if st.status in FINISHED_STATUSES and st.status not in SATISFIED_STATUSES
    }
    if not dead:
        return set()

    blocked: set[str] = set()
    # Fixpoint: a subtask blocked by a dead ancestor is itself an ancestor
    # that will never satisfy, so its own dependents are blocked too.
    frontier = set(dead)
    while frontier:
        newly: set[str] = set()
        for sid, st in by_id.items():
            if sid in blocked or sid in dead:
                continue
            if st.status in FINISHED_STATUSES:
                continue
            if any(dep in frontier for dep in st.dependencies):
                newly.add(sid)
        if not newly:
            break
        blocked |= newly
        frontier = newly
    return blocked


def is_task_complete(task: Task) -> bool:
    # A task with a blocked/halt phase is BY DEFINITION not complete —
    # even if that phase's subtasks all read "done". The retry-cap
    # escalation parks a capped subtask at status="done" and flips the
    # PHASE to blocked_review + set_awaiting; without this guard the loop
    # top would see all-done subtasks, declare success, and silently
    # override the escalation (false pass). Reuse the same blocked-phase
    # set the scheduler consults (_BLOCKED_PHASE_STATUSES) so completion
    # and dispatch share one definition of "this phase halts progress".
    from okuro.orchestrator.deliberation import _BLOCKED_PHASE_STATUSES
    # A subtask stranded behind a failed ancestor will never become runnable,
    # so it counts as complete-for-this-task — otherwise the engine spins on a
    # ready set that is permanently empty. It stays `pending` on disk (NOT
    # skipped), so retrying the ancestor revives it. See blocked_upstream_ids.
    stranded = blocked_upstream_ids(task)
    for phase in task.phases:
        if phase.status in _BLOCKED_PHASE_STATUSES:
            return False
        for st in phase.subtasks:
            if st.status not in FINISHED_STATUSES and st.id not in stranded:
                return False
    return True


# ---------------------------------------------------------------------------
# C2 — single canonical derivation for phase aggregate state + current_phase.
#
# `phase_aggregate_state(phase)` is the one place that combines
# `phase.status` (persisted) with the subtask roster to produce the FE-visible
# lifecycle label. Every reader (snapshot, summary, detail) imports it.
#
# `derive_current_phase(task)` is the one place that picks "which phase is the
# task focused on". A phase is treated as terminal-for-the-walk when its
# aggregate state is `done` or `skipped` — `blocked_review` counts as
# non-terminal so a blocked phase 2 anchors current_phase=2 instead of
# silently advancing to phase 4 just because every subtask is `done`
# (D1 — the cancelled-task replica reproduces exactly that drift).
# ---------------------------------------------------------------------------

# Phase-aggregate states that mean "this phase no longer needs the engine's
# attention" for the purposes of the current_phase walk. `blocked_review` is
# explicitly NOT in this set — the walk anchors on the blocking phase.
_CURRENT_PHASE_TERMINAL_STATES: frozenset[str] = frozenset({"done", "skipped"})


def phase_aggregate_state(phase: "Phase") -> str:
    """Return the canonical lifecycle label for a phase.

    Order matters:
      1. persisted `phase.status` of `blocked_review` or `done` wins outright
         (these are explicit writer decisions — see set_phase_status / the
         M3 cap-fire path)
      2. pending decision_gate → `awaiting_decision`
      3. any subtask running/approved/waiting_approval → `running`
      4. all subtasks done (+failed=0) → `done`
      5. any subtask failed and nothing else running → `failed`
      6. fallthrough → `pending`

    Returns one of: pending, running, blocked_review, awaiting_decision,
    done, failed.
    """
    subtasks = list(getattr(phase, "subtasks", []) or [])
    total = len(subtasks)
    running = sum(
        1 for s in subtasks if s.status in ("running", "approved", "waiting_approval")
    )
    done = sum(1 for s in subtasks if s.status in ("done", "skipped", "superseded"))
    failed = sum(1 for s in subtasks if s.status == "failed")
    pending = total - running - done - failed

    declared = getattr(phase, "status", "") or "pending"
    if declared == "blocked_review":
        return "blocked_review"
    if declared == "done":
        return "done"

    gate = getattr(phase, "decision_gate", None)
    if gate is not None and getattr(gate, "status", "") == "pending":
        return "awaiting_decision"
    if running > 0:
        return "running"
    if total > 0 and done == total:
        return "done"
    if failed > 0 and (running + pending) == 0:
        return "failed"
    return "pending"


def derive_current_phase(task: "Task") -> int:
    """Canonical derivation for `task.current_phase`.

    Walks phases in order; returns the id of the first phase whose
    aggregate state is non-terminal-for-the-walk. `blocked_review` counts
    as non-terminal so the current_phase anchors on the blocker rather
    than jumping past it (D1 / screenshot bug class).

    If all phases are terminal, returns the last phase id (the task is
    effectively done — `set_task_status` will flip task.status to "done"
    on the same write). If the task has no phases yet (planner in flight),
    returns 1 — the conventional bootstrap value.
    """
    phases = list(getattr(task, "phases", []) or [])
    if not phases:
        return 1
    for phase in phases:
        if phase_aggregate_state(phase) not in _CURRENT_PHASE_TERMINAL_STATES:
            return phase.id
    return phases[-1].id


# ---------------------------------------------------------------------------
# Derive canonical task.status from primitive state — STEP 1 of the canonical
# state migration. Pre-fix task.status was written in 30+ engine sites with no
# invariants, leading to "DELIBERATING during execution", "active with no
# subtasks running", and the blocked_review/parallel-phase contradiction
# documented in docs/audit-2026-05-26/01-backend-state-machine.md §2.4.
#
# Contract:
#   - Engine NEVER writes task.status directly. All writes go through
#     set_task_status() which calls derive_task_status() and persists.
#   - Derivation reads ONLY: task.awaiting, task.phases, gates, and the
#     prior status (for terminal stickiness — done/failed/halted/blocked
#     never auto-revert).
#   - Output is one of TaskLifecycle values — a closed enum FE switches on.
# ---------------------------------------------------------------------------

# `completed_partial` — the task ran to the end of what was reachable, but at
# least one subtask failed permanently or was stranded behind one. Distinct
# from `done` on purpose: before the FINISHED/SATISFIED split a task whose
# later phases had been cascade-skipped still reported a clean `done`, which
# is what let two hollow phases ship unnoticed (task-20260724-110216).
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    {"done", "failed", "halted", "completed_partial"}
)


def derive_task_status(task: Task) -> str:
    """Compute canonical task.status from primitive state. Pure function.

    Order matters — earlier conditions win:
      1. terminal stickiness (done/failed/halted never auto-revert)
      2. explicit user-input wait (task.awaiting) → 'waiting_user'
      3. all phases done → 'done'
      4. any phase has a running/active subtask → 'active'
      5. any phase has a pending decision_gate → 'awaiting_decision'
      6. all phases pending but planner already ran → 'active' (engine
         will dispatch on next tick)
      7. no phases yet → 'planning' (decomposer in flight) or
         'deliberating' (deliberate-mode panel/positions)
      8. fallthrough → keep prior status
    """
    prior = (task.status or "").strip()

    if prior in TERMINAL_TASK_STATUSES:
        return prior

    if task.awaiting is not None:
        return "waiting_user"

    has_phases = bool(task.phases)
    has_running_subtask = False
    has_pending_gate = False
    all_phases_done = True
    # Stranded = pending behind a permanently-failed ancestor. Derived, so it
    # follows upstream status; retrying that ancestor green un-strands them.
    stranded = blocked_upstream_ids(task)
    degraded = bool(stranded)
    for phase in task.phases or []:
        for st in getattr(phase, "subtasks", []) or []:
            if st.status in ("running", "approved", "waiting_approval"):
                has_running_subtask = True
            if st.status == "failed":
                degraded = True
            if st.status not in FINISHED_STATUSES and st.id not in stranded:
                all_phases_done = False
        gate = getattr(phase, "decision_gate", None)
        if gate is not None and getattr(gate, "status", "") == "pending":
            has_pending_gate = True

    if has_phases and all_phases_done:
        # Honest terminal: `done` only when nothing failed and nothing was
        # stranded. Otherwise the task finished, but not intact.
        return "completed_partial" if degraded else "done"
    if has_running_subtask:
        return "active"
    if has_pending_gate:
        return "awaiting_decision"
    if has_phases:
        return "active"
    if task.mode == "deliberate" and task.graph and task.graph.nodes:
        return "deliberating"
    if prior in ("planning", "deliberating"):
        return prior
    return prior or "pending"


def set_task_status(task: Task, tasks_dir: Path, *, force: Optional[str] = None) -> str:
    """Single writer for task.status. Returns the new value.

    Engine call sites that previously wrote ``task.status = "x"`` should
    call this instead. The ``force`` parameter is the escape hatch for
    explicit transitions the engine MUST guarantee (e.g. crash handler
    setting ``failed``); it bypasses the derivation but is still routed
    through this function so the event is emitted uniformly.

    C1 — locks the shared task-dir cohort so the in-memory flip + persist
    + event-emit are observable as one atomic transition (re-entrant if
    called from within another locked writer, e.g. clear_awaiting).
    """
    with _locked_task_dir(task.id, tasks_dir):
        new_status = force if force else derive_task_status(task)
        if new_status == task.status:
            return new_status
        prior = task.status
        task.status = new_status
        save_task_meta(task, tasks_dir)
        try:
            append_log(task.id, {
                "type": "status_changed",
                "from": prior,
                "to": new_status,
                "forced": bool(force),
            }, tasks_dir)
        except Exception:
            pass
        return new_status


def apply_supersedes(task, new_phases: list[Phase], tasks_dir: Path) -> list[str]:
    """Mark prior subtasks as superseded based on new_phases[].supersedes.

    Called by the continuation flow BEFORE ``append_phases`` so the
    dispatcher never picks up work that the user's correction made obsolete.
    Only subtasks in SUPERSEDEABLE_STATUSES (pending/approved) are
    marked — running subtasks must be cancelled first (which resets them
    to pending), and done/failed subtasks are historical truth.

    Returns the list of subtask IDs that were actually marked superseded
    (so callers can log a single audit event).

    Accepts either a ``Task`` object or a task_id string (Gate 2 §C3 —
    consistent with the other writer signatures, ``update_subtask`` /
    ``update_node`` / ``save_task_meta``). String IDs reload the task
    under the lock so external mutations don't get clobbered.

    Gate 2 §C3 — graph mirror is automatic: ``update_subtask`` writes
    "superseded" on the subtask SoT; the graph node for that id is a
    derived view regenerated at the same write boundary. No explicit
    graph-side mirror is needed here.
    """
    if not new_phases:
        return []

    if isinstance(task, str):
        task_id = task
        loaded = load_task(task_id, tasks_dir)
    else:
        task_id = task.id
        loaded = task

    existing_subtasks: dict[str, Subtask] = {}
    for phase in loaded.phases:
        for st in phase.subtasks:
            existing_subtasks[st.id] = st

    superseded_ids: list[str] = []
    for new_phase in new_phases:
        targets = list(getattr(new_phase, "supersedes", []) or [])
        if not targets:
            continue
        for target_id in targets:
            st = existing_subtasks.get(target_id)
            if st is None:
                logger.warning(
                    "[%s] supersedes target %r not found — ignored",
                    task_id, target_id,
                )
                continue
            if st.status not in SUPERSEDEABLE_STATUSES:
                logger.warning(
                    "[%s] supersedes target %s is %s (not pending/approved) — ignored",
                    task_id, target_id, st.status,
                )
                continue
            update_subtask(
                task_id, target_id,
                {
                    "status": "superseded",
                    "superseded_by": list(st.superseded_by or []) + [str(new_phase.id)],
                },
                tasks_dir,
            )
            st.status = "superseded"
            superseded_ids.append(target_id)

    if superseded_ids:
        append_log(task_id, {
            "type": "subtasks_superseded",
            "ids": superseded_ids,
            "by_phases": [p.id for p in new_phases if getattr(p, "supersedes", None)],
        }, tasks_dir)

    return superseded_ids


def append_log(task_id: str, event: dict, tasks_dir: Path):
    """Append an event to the task log, stamping a monotonic per-task ``seq``.

    ``seq`` is the 1-based line ordinal of the event in log.jsonl — strictly
    increasing per task. It is assigned under an exclusive file lock so the
    value is consistent across the engine main thread, the heartbeat thread,
    and the API process (all of which append here). The FE drops any frame
    whose ``seq`` is ``<=`` the last it applied, which kills the flicker /
    backwards-regression / stale-replay class (canonical-state migration
    Phase 0). Returns the assigned seq for callers that want it.
    """
    task_path = tasks_dir / task_id
    log_file = task_path / "log.jsonl"
    event["ts"] = datetime.utcnow().isoformat()
    # a+ so we can read existing line count then append within one lock hold.
    with open(log_file, "a+") as f:
        locked = False
        if fcntl is not None:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                locked = True
            except OSError:
                pass
        try:
            f.seek(0)
            existing = sum(1 for _ in f)
            event["seq"] = existing + 1
            f.seek(0, os.SEEK_END)
            f.write(json.dumps(event) + "\n")
        finally:
            if locked:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    return event["seq"]


def rename_task(task_id: str, new_description: str, tasks_dir: Path):
    """Update task description in task.yaml and log the change."""
    task_path = tasks_dir / task_id / "task.yaml"

    with open(task_path) as f:
        task_data = _yload(f)

    old_description = task_data["description"]
    task_data["description"] = new_description

    atomic_dump_yaml(task_path, task_data, default_flow_style=False)

    append_log(task_id, {
        "type": "task_renamed",
        "old_description": old_description[:200],
        "new_description": new_description[:200],
    }, tasks_dir)


def append_phases(task: Task, new_phases: list[Phase], tasks_dir: Path):
    """Append new phases to an existing task's plan.

    Theme C — when the task already has a DAG (deliberate-mode task),
    extend the graph with execution nodes for every new subtask so the
    ``deliberation_loop`` terminal check (``is_graph_complete``) does
    not declare the task COMPLETE just because the original d1.x set
    is all done. Without this hop the continuation's new phases sit
    forever pending while the engine cheerfully reports "All DAG
    nodes resolved" and exits — required manual ``task.mode`` flips
    were the only recovery.
    """
    task.phases.extend(new_phases)

    # Extend the DAG when present. Auto-execute tasks have no graph;
    # skip silently — phase iteration handles dispatch for them.
    nodes_added = 0
    edges_added = 0
    if task.graph is not None and task.graph.nodes:
        try:
            nodes_added, edges_added = _extend_graph_with_new_phases(
                task.graph, new_phases,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[%s] append_phases: graph extension failed (%r) — new "
                "subtasks dispatched via phases only", task.id, exc,
            )

    save_plan(task, tasks_dir)
    append_log(task.id, {
        "type": "phases_appended",
        "new_phases": len(new_phases),
        "total_phases": len(task.phases),
        "graph_nodes_added": nodes_added,
        "graph_edges_added": edges_added,
    }, tasks_dir)


def _extend_graph_with_new_phases(
    graph: "DAGGraph", new_phases: list[Phase]
) -> tuple[int, int]:
    """Add execution nodes (+ anchor edges) for every subtask in
    ``new_phases``. Returns (nodes_added, edges_added).

    Anchor selection: each new subtask's node is anchored to the
    last terminal-status execution node already in the graph so the
    DAG remains a single connected component. If no terminal node
    exists yet, anchor to the latest node by insertion order — the
    DAG still extends correctly because new nodes will be runnable
    once their prerequisite finishes.

    Internal dependencies declared on the new subtasks (Subtask.deps)
    are wired as edges in addition to the anchor, so the
    deliberation_loop's ``get_ready_nodes`` honours intra-continuation
    ordering.
    """
    if not new_phases:
        return (0, 0)

    # Pick anchor: prefer the last execution node that has terminal
    # status (done / skipped / dismissed / failed). Falling back to
    # the last node in insertion order keeps the graph connected even
    # for cases where no execution node yet exists (rare — the first
    # phase always seeds the dag).
    terminal_states = {"done", "skipped", "dismissed", "failed"}
    anchor_id: Optional[str] = None
    for node in reversed(graph.nodes):
        if node.type == "execution" and node.status in terminal_states:
            anchor_id = node.id
            break
    if anchor_id is None:
        anchor_id = graph.nodes[-1].id

    nodes_added = 0
    edges_added = 0
    new_ids: set[str] = set()

    for phase in new_phases:
        for st in phase.subtasks:
            if graph.get_node(st.id) is not None:
                # Already in the graph (shouldn't normally happen, but
                # protects against double-append).
                continue
            node = DAGNode(
                id=st.id,
                type="execution",
                role=getattr(st, "role", "") or "",
                description=getattr(st, "description", "") or "",
                authority_from=anchor_id,
                risk=getattr(st, "risk", "LOW") or "LOW",
                complexity=getattr(st, "complexity", "standard") or "standard",
                artifact_name=getattr(st, "artifact_name", "") or "",
                status="pending",
            )
            graph.add_node(node)
            graph.add_edge(anchor_id, st.id)
            nodes_added += 1
            edges_added += 1
            new_ids.add(st.id)

    # Wire intra-continuation dependencies — Subtask.deps may point at
    # other new subtasks. We add the edge unconditionally; the DAG's
    # add_edge skips nodes that don't exist so external typos are
    # harmless.
    for phase in new_phases:
        for st in phase.subtasks:
            for dep_id in getattr(st, "deps", []) or []:
                if dep_id and dep_id != st.id:
                    try:
                        graph.add_edge(dep_id, st.id)
                        edges_added += 1
                    except Exception:
                        pass

    return (nodes_added, edges_added)


def save_artifact(task_id: str, subtask_id: str, content: str, tasks_dir: Path,
                   artifact_name: str = ""):
    """Save subtask output as an artifact.

    If the agent already wrote the primary file, stdout goes to
    {subtask_id}.stdout.md to avoid overwriting.
    """
    task_path = tasks_dir / task_id
    artifacts_dir = task_path / "artifacts"

    if artifact_name:
        primary_file = artifacts_dir / f"{subtask_id}-{artifact_name}.md"
    else:
        primary_file = artifacts_dir / f"{subtask_id}.md"

    if primary_file.exists():
        stdout_file = artifacts_dir / f"{subtask_id}.stdout.md"
        with open(stdout_file, "w") as f:
            f.write(content)
    else:
        with open(primary_file, "w") as f:
            f.write(content)

    # C12 — pass task_id + tasks_dir so any cortex-index failure surfaces
    # as a typed cortex_index_failed event (state.emit_event) instead of
    # the historical silent logger.warning fallback.
    _index_task_artifacts(task_path, task_id=task_id, tasks_dir=tasks_dir)


def validate_and_warn(task: Task) -> list[str]:
    """Run validation on a loaded task and return warnings."""
    try:
        from okuro.orchestrator.validation import validate_task
        return validate_task(task)
    except Exception as e:
        logger.warning(f"State validation failed to run: {e}")
        return []
