# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: State Reader - Reads okuro orchestrator file-based state for API consumption.
# index:
#   imports
#   def _resolve_planned_model
#   def read_task_summary
#   def read_task_detail
#   def read_task_logs
#   def _extract_artifact_title
#   def read_task_artifacts
#   def read_task_state
#   def list_all_tasks
# AGENT_HEADER_END -->
"""
State Reader - Reads okuro orchestrator file-based state for API consumption.

Translates task.yaml, plan.yaml, and log.jsonl into Pydantic models.
"""

import hashlib
import json
import logging
import re as _re
import time
from datetime import datetime, timezone
from okuro.clock import utc_now_naive
from pathlib import Path
from typing import List, Optional

# Match the leading numeric subtask id of an artifact filename.
# Convention used by orchestration templates:
#   "{subtask_id}-{description}.{ext}"  (dash separator)
#   "{subtask_id}.{tag}.{ext}"          (dot separator before a non-digit tag)
# Examples:
#   1.1-findings.md      -> "1.1"
#   14.2-spec.md         -> "14.2"
#   10.1.stdout.md       -> "10.1"   (dot+non-digit → "stdout" tag)
#   1.2.3-deep.md        -> "1.2.3"  (dash, deep id wins)
#   1.1-1.1-findings.md  -> "1.1"    (first id wins; historical quirk)
#   10-thing.md          -> "10"
#   spa-1280-rooms.png   -> None     (leading non-digit)
#   README.md            -> None
#   12345.md             -> None     (no separator after the number)
_SUBTASK_PREFIX_RE = _re.compile(r"^(\d+(?:\.\d+)*)(?:-|\.(?=\D))")

from okuro.orchestrator.api.models import AwaitingState
from okuro.orchestrator.api.models import (
    TaskSummary, TaskDetail, PhaseSummary, SubtaskSummary,
    LogEntry, ArtifactInfo, TaskState
)
from okuro.orchestrator.state import (
    derive_current_phase,
    load_task,
    phase_aggregate_state,
)
from okuro.orchestrator.validation import validate_task
# ROCK-SOLID v5 D-A — the plan nouns a person reads live in ONE module.
from okuro.orchestrator.gate_messages import PART_NOUN, PART_NOUN_CAP, step_label
from okuro.orchestrator.yamlfast import FastLoader, yload

logger = logging.getLogger(__name__)

# libyaml-backed loader — identical SafeLoader semantics, ~5-8x faster than
# the pure-python fallback. The task list scans hundreds of task.yaml /
# plan.yaml files per request; the C loader cuts that parse cost by ~5x.
# One definition for the package lives in orchestrator/yamlfast.py; the local
# names stay so existing call sites and the equivalence test read unchanged.
_FastLoader = FastLoader
_yload = yload


# Per-task summary cache, keyed by absolute task_dir path → (mtime_sig,
# TaskSummary). The list endpoint re-reads every task on each request; this
# turns repeat scans (and the reconcile re-scan) into near-zero work. The
# signature is the (task.yaml, plan.yaml) mtimes, so any write — by the
# engine, the reconciler, or a hand-edit — self-invalidates the entry. The
# write path additionally clears entries via invalidate_snapshot_cache().
_SUMMARY_CACHE: dict[str, tuple] = {}

# Statuses that never advance phase again — for these the costly
# load_task()+derive_current_phase() round-trip (which re-parses both YAML
# files and runs validation) is wasted. current_phase is derived cheaply
# from the already-parsed plan instead. Live statuses still use the
# canonical resolver so in-flight phase display stays exact.
_TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    {"done", "failed", "halted", "blocked", "cancelled", "skipped",
     "completed_partial"}
)

_PHASE_TOUCHED_STATES: frozenset[str] = frozenset(
    {"running", "executing", "done", "skipped", "failed", "waiting_approval", "blocked"}
)


def _summary_sig(task_yaml: Path, plan_yaml: Path) -> tuple:
    try:
        t = task_yaml.stat().st_mtime_ns
    except OSError:
        t = 0
    try:
        p = plan_yaml.stat().st_mtime_ns
    except OSError:
        p = 0
    return (t, p)


def _review_flag_sig(task_dir: Path) -> tuple:
    """Presence + mtime of the review-active flag files (ROCK-SOLID v5 P3.6).

    The "Reviewing" badge is derived from ``.review-active.json`` and the
    per-subtask ``.review-active.<id>.json`` files. Those are the ONLY record
    that a review is in flight — the engine writes one when review starts and
    removes it when the verdict lands, touching neither task.yaml nor
    plan.yaml.

    The snapshot cache keyed on that yaml pair alone, so a cached snapshot
    survived both edges: the badge did not appear when review started, and —
    the reported symptom — it STUCK after the verdict, because nothing about
    deleting a flag file invalidated the entry that still said review_in_
    progress. Including the flags in the signature makes both edges
    cache-visible.

    A glob rather than the directory mtime: the dir mtime moves for any file
    created or removed in the task dir, so caching would be silently
    weakened by unrelated churn, and a reader would have no way to tell.
    """
    try:
        return tuple(sorted(
            (fp.name, fp.stat().st_mtime_ns)
            for fp in task_dir.glob(".review-active*.json")
        ))
    except OSError:
        return ()


def _cheap_current_phase(plan_phases: list) -> int:
    """1-based index of the last phase that has any started/finished subtask.

    A cheap stand-in for derive_current_phase() used only on terminal tasks,
    whose phase no longer moves. Floors at 1; for a fully-done task this
    lands on the last phase, matching the canonical resolver's result.
    """
    touched = 1
    for idx, phase in enumerate(plan_phases, start=1):
        for st in phase.get("subtasks", []):
            if st.get("status") in _PHASE_TOUCHED_STATES:
                touched = idx
                break
    return touched

# Default reviewer round ceiling — mirrors dispatcher_streaming's
# _DEFAULT_MAX_ROUNDS (5). Used to project ``review_max_attempts`` onto
# the subtask tile ("attempt N of 5"). Kept as a local constant rather
# than importing the dispatcher to avoid pulling the streaming stack into
# the read path.
_REVIEW_MAX_ATTEMPTS = 5


def _resolve_current_phase(task_dir: Path, plan_present: bool) -> int:
    """PR B — single canonical current_phase resolver.

    Stage B G11: state_reader had two derivation paths — one preferred
    ``derive_current_phase(task_obj)``, the other fell back to the
    ``task.yaml.current_phase`` persisted column. Snapshot, task detail,
    task summary, and the engine all wrote to or read from the column at
    different times → values diverged during phase transitions.

    Contract:
      * When ``plan_present`` is True, the plan-derived value is the
        only truth. Any exception inside ``derive_current_phase`` is
        logged at warning level and we return phase 1 as a defensive
        floor — we MUST NOT reach for the persisted task.yaml field,
        because that's the field this resolver is trying to make
        obsolete.
      * When no plan exists yet (early deliberation), phase 1 is the
        floor — the orchestrator hasn't decomposed yet.

    Returns the canonical current_phase integer (always >= 1).
    """
    if not plan_present:
        return 1
    try:
        task_obj = load_task(task_dir.name, task_dir.parent)
        return int(derive_current_phase(task_obj) or 1)
    except Exception as exc:
        logger.warning(
            "PR B canonical current_phase resolver failed for %s: %s "
            "(returning floor=1 — DO NOT fall back to persisted "
            "task.yaml.current_phase, which is the column being deprecated)",
            task_dir.name, exc,
        )
        return 1

# Provider-NEUTRAL fallback — the tier NAME itself, never a hardcoded model.
# Orchestration is provider-agnostic: actual model names come from the
# configured CLI's tier_map (claude|gemini|codex — each exposes fast/standard/
# strategic). Hardcoding claude model names here is what caused UI-MODEL-1.
_TIER_FALLBACK = {"fast": "fast", "standard": "standard", "strategic": "strategic"}
_TIER_CACHE: dict = {"sig": None, "map": _TIER_FALLBACK}


def _live_tier_map() -> dict:
    """The DEFAULT CLI's tier_map from config.yaml — the SAME source the
    dispatcher resolves the runtime model from. Provider-agnostic: works for
    whichever CLI is `cli_default` (claude/gemini/codex). A tier whose value is
    empty (the provider uses its own default model) displays the tier name, not
    a foreign provider's model. mtime-cached; falls back to tier-name identity
    if config is unreadable."""
    from pathlib import Path as _P

    try:
        sig = (_P.home() / ".okuro" / "orchestrator" / "config.yaml").stat().st_mtime_ns
    except Exception:
        return _TIER_CACHE["map"]
    if _TIER_CACHE["sig"] == sig:
        return _TIER_CACHE["map"]
    m = dict(_TIER_FALLBACK)
    try:
        from okuro.orchestrator.config import load_config

        cfg = load_config()
        cli = getattr(cfg, "cli_default", "") or ""
        tool = (getattr(cfg, "cli_tools", {}) or {}).get(cli)
        tm = getattr(tool, "tier_map", None) if tool else None
        if tm:
            # Empty value = provider's own default model → show the tier name.
            m = {k: (str(v).strip() or k) for k, v in tm.items()}
    except Exception:
        pass
    _TIER_CACHE["sig"] = sig
    _TIER_CACHE["map"] = m
    return m


def _resolve_planned_model(
    complexity: str, model_override: str, intelligence: str = ""
) -> str:
    """Resolve the model from tier or override — reads the LIVE config tier_map
    so the UI mirrors the actual dispatched --model (UI-MODEL-1).

    Mirrors dispatcher.py: intelligence="max" forces tier="strategic"; an
    explicit per-subtask model_override always wins.
    """
    if model_override:
        return model_override
    effective_complexity = "strategic" if intelligence == "max" else complexity
    tm = _live_tier_map()
    # Provider-neutral final fallback: the tier name, never a hardcoded model.
    return tm.get(effective_complexity) or effective_complexity or "standard"


def read_task_summary(task_dir: Path) -> Optional[TaskSummary]:
    """Read task.yaml and plan.yaml to build a TaskSummary.

    Cached per task_dir on the (task.yaml, plan.yaml) mtime signature — the
    list endpoint re-reads every task on each request, so unchanged tasks
    return from cache without re-parsing or re-deriving the phase.
    """
    task_yaml = task_dir / "task.yaml"
    if not task_yaml.exists():
        return None

    plan_yaml = task_dir / "plan.yaml"
    cache_key = str(task_dir)
    sig = _summary_sig(task_yaml, plan_yaml)
    hit = _SUMMARY_CACHE.get(cache_key)
    if hit is not None and hit[0] == sig:
        return hit[1]

    try:
        with open(task_yaml) as f:
            task_data = _yload(f)
    except Exception as e:
        logger.warning(f"Failed to read {task_yaml}: {e}")
        return None

    # Count subtasks from plan.yaml
    phases_total = 0
    subtasks_done = 0
    subtasks_total = 0
    plan_phases = []

    if plan_yaml.exists():
        try:
            with open(plan_yaml) as f:
                plan_data = _yload(f)
            plan_phases = plan_data.get("phases", [])
            phases_total = len(plan_phases)
            for phase in plan_phases:
                for st in phase.get("subtasks", []):
                    subtasks_total += 1
                    if st.get("status") in ("done", "skipped"):
                        subtasks_done += 1
        except Exception as e:
            logger.warning(f"Failed to read {plan_yaml}: {e}")

    status_val = task_data.get("status", "pending")

    # PR B — single canonical current_phase resolver (plan + DAG are the only
    # truth; never the persisted task.yaml.current_phase). For TERMINAL tasks
    # the phase no longer moves, so skip the costly load_task()+derive round
    # trip (re-parses both YAMLs + runs validation) and derive cheaply from
    # the plan we already parsed. Live tasks keep the exact resolver.
    if status_val in _TERMINAL_TASK_STATUSES and plan_phases:
        current_phase = _cheap_current_phase(plan_phases)
    else:
        current_phase = _resolve_current_phase(task_dir, bool(plan_phases))

    awaiting_present = bool(isinstance(task_data.get("awaiting"), dict) and task_data["awaiting"].get("kind"))
    if awaiting_present:
        lifecycle_state = "waiting_user"
    else:
        lifecycle_state = status_val
    summary = TaskSummary(
        id=task_data.get("id", task_dir.name),
        title=task_data.get("title") or "",
        description=task_data.get("description") or "",
        status=status_val,
        created_at=task_data.get("created_at", ""),
        current_phase=current_phase,
        phases_total=phases_total,
        subtasks_done=subtasks_done,
        subtasks_total=subtasks_total,
        task_type=task_data.get("task_type", ""),
        recurring_def_id=task_data.get("recurring_def_id", ""),
        intelligence=task_data.get("intelligence", ""),
        color_class=_color_for(_LIFECYCLE_COLOR, lifecycle_state),
        label=_label_for(_LIFECYCLE_LABEL, lifecycle_state),
    )
    _SUMMARY_CACHE[cache_key] = (sig, summary)
    return summary


def read_task_detail(task_dir: Path) -> Optional[TaskDetail]:
    """Read full task detail including phases and subtasks."""
    task_yaml = task_dir / "task.yaml"
    if not task_yaml.exists():
        return None

    try:
        with open(task_yaml) as f:
            task_data = _yload(f)
    except Exception as e:
        logger.warning(f"Failed to read {task_yaml}: {e}")
        return None

    # ``project_path`` is now sourced declaratively: subagents emit it
    # in their role-handover brief, and ``engine._adopt_declared_project_path``
    # writes it to ``task.yaml`` on finalize. The previous auto-inference
    # at read time (which mined plan.yaml + activity.jsonl with a sticky
    # negative sentinel) is gone — see commit history for details.
    # Legacy tasks where project_path is empty surface an inline picker
    # in the SPA fed by ``GET /api/preview/{id}/recipe/suggest``; the
    # picker calls ``PUT /api/tasks/{id}/project-path`` to persist the
    # user's choice.

    phases: List[PhaseSummary] = []
    subtasks_done = 0
    subtasks_total = 0
    _intelligence = task_data.get("intelligence", "") or ""

    plan_yaml = task_dir / "plan.yaml"
    if plan_yaml.exists():
        try:
            with open(plan_yaml) as f:
                plan_data = _yload(f)
            for phase_data in plan_data.get("phases", []):
                subtasks = []
                for st in phase_data.get("subtasks", []):
                    subtasks_total += 1
                    if st.get("status") in ("done", "skipped"):
                        subtasks_done += 1
                    # Coerce None -> default. dict.get(k, default) returns the
                    # default ONLY when the key is ABSENT; a key present with a
                    # null value (plan.yaml writes `error:` / `model_override:`
                    # as null) returns None, which fails SubtaskSummary's
                    # `str`-typed fields → "1 validation error" on EVERY state
                    # read → warning-flood hot loop that pegs the API event loop
                    # and starves the inline /mcp/v1 mount (subagents then fail
                    # artifact_write → "subagent never wrote an artifact"). Use
                    # `or default` so any null value collapses to the default.
                    _complexity = st.get("complexity") or "standard"
                    _model_override = st.get("model_override") or ""
                    subtasks.append(SubtaskSummary(
                        id=st.get("id") or "",
                        role=st.get("role") or "",
                        description=st.get("description") or "",
                        risk=st.get("risk") or "LOW",
                        complexity=_complexity,
                        status=st.get("status") or "pending",
                        duration=st.get("duration") or 0.0,
                        error=st.get("error") or "",
                        cli_used=st.get("cli_used") or "",
                        model_used=st.get("model_used") or "",
                        planned_model=_resolve_planned_model(
                            _complexity, _model_override, _intelligence
                        ),
                        model_override=_model_override,
                        started_at=st.get("started_at") or "",
                        retries=int(st.get("retries", 0) or 0),
                    ))
                phases.append(PhaseSummary(
                    id=phase_data.get("id", 0),
                    name=phase_data.get("name", step_label(phase_data.get("id", "?"))),
                    status=phase_data.get("status", "pending"),
                    subtasks=subtasks,
                ))
        except Exception as e:
            logger.warning(f"Failed to read {plan_yaml}: {e}")

    # Map artifacts to subtasks — find all files matching the subtask ID prefix
    artifacts_dir = task_dir / "artifacts"
    if artifacts_dir.exists():
        for p in phases:
            for st in p.subtasks:
                matched = []
                # Primary: {id}.md
                primary = artifacts_dir / f"{st.id}.md"
                if primary.exists():
                    matched.append(primary.name)
                # Named: {id}-*.md (exclude .stdout.md)
                for f in sorted(artifacts_dir.glob(f"{st.id}-*.md")):
                    if not f.name.endswith(".stdout.md"):
                        matched.append(f.name)
                # Subdirectory: {id}/
                subdir = artifacts_dir / st.id
                if subdir.is_dir():
                    matched.append(f"{st.id}/")
                if matched:
                    st.artifact = matched[0]  # backward compat: first match
                    st.artifacts = matched

    progress = int((subtasks_done / subtasks_total * 100)) if subtasks_total > 0 else 0

    # PR B — single canonical current_phase resolver (see _resolve_current_phase).
    # The prior fallback to ``task_data["current_phase"]`` allowed snapshot
    # vs detail vs summary to diverge during phase transitions.
    current_phase = _resolve_current_phase(task_dir, bool(phases))

    return TaskDetail(
        id=task_data.get("id", task_dir.name),
        title=task_data.get("title", ""),
        description=task_data.get("description", ""),
        status=task_data.get("status", "pending"),
        created_at=task_data.get("created_at", ""),
        current_phase=current_phase,
        phases=phases,
        progress_percent=progress,
        intelligence=task_data.get("intelligence", ""),
        project_path=task_data.get("project_path", "") or "",
    )


def read_task_logs(task_dir: Path, limit: int = 100) -> List[LogEntry]:
    """Read log.jsonl and return structured log entries."""
    log_path = task_dir / "log.jsonl"
    if not log_path.exists():
        return []

    entries = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    entries.append(LogEntry(
                        timestamp=event.get("ts", ""),
                        type=event.get("type", "unknown"),
                        data={k: v for k, v in event.items() if k not in ("ts", "type")},
                    ))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.warning(f"Failed to read {log_path}: {e}")

    # Return most recent entries
    return entries[-limit:]


def _extract_artifact_title(path: Path) -> str:
    """Extract a human-readable title from an artifact file.

    Priority: AGENT_HEADER purpose > first markdown heading.
    """
    if path.suffix != ".md":
        return ""
    try:
        with open(path) as f:
            lines = []
            for i, line in enumerate(f):
                lines.append(line)
                if i > 20:
                    break

        in_header = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("<!-- AGENT_HEADER") or stripped == "# <!-- AGENT_HEADER":
                in_header = True
                continue
            if in_header:
                if stripped.startswith("AGENT_HEADER_END"):
                    in_header = False
                    continue
                if stripped.startswith("purpose:"):
                    purpose = stripped[len("purpose:"):].strip()
                    # Clean up common prefixes like "provides "
                    if purpose.startswith("provides "):
                        purpose = purpose[len("provides "):]
                    return purpose[:100]
            elif stripped.startswith("# ") and not stripped.startswith("# <!--"):
                return stripped[2:].strip()[:100]
    except Exception:
        pass
    return ""


def _parse_subtask_id_from_name(name: str) -> Optional[str]:
    """Return the leading subtask id from an artifact filename, or None."""
    m = _SUBTASK_PREFIX_RE.match(name)
    return m.group(1) if m else None


def read_task_artifacts(task_dir: Path) -> List[ArtifactInfo]:
    """List artifacts the UI should render for this task.

    Stream B reports live in the brain (``artifacts`` table, keyed by
    task_id/subtask_id since migration 035). Non-document outputs
    (binaries, screenshots, .stdout.md fallbacks) still land on disk.
    This function merges both — brain rows first (newest), disk
    survivors second — so ArtifactsViewer can render either source.
    """
    # Build subtask description map from plan.yaml — used to fall back
    # to the subtask description when an artifact has no title of its own.
    subtask_map: dict[str, str] = {}
    plan_path = task_dir / "plan.yaml"
    if plan_path.exists():
        try:
            with open(plan_path) as f:
                plan_data = _yload(f)
            for phase in plan_data.get("phases", []):
                for st in phase.get("subtasks", []):
                    sid = st.get("id", "")
                    desc = st.get("description", "")
                    if sid and desc:
                        subtask_map[sid] = desc[:100]
        except Exception:
            pass

    task_id = task_dir.name
    artifacts: List[ArtifactInfo] = []

    # Brain — Stream B (artifact_write rows tagged with task_id).
    try:
        from okuro.sense.artifacts import artifact_list as _artifact_list

        rows = _artifact_list(task_id=task_id, limit=200,
                              order="created_at_desc")
        for row in rows:
            row_id = str(row.get("id") or "")
            if not row_id:
                continue
            kind = str(row.get("kind") or "report")
            title = str(row.get("title") or "").strip()
            if not title:
                title = subtask_map.get(str(row.get("subtask_id") or ""), "")
            modified = str(
                row.get("updated_at") or row.get("created_at") or ""
            )
            if modified and not modified.endswith("Z"):
                # SQLite datetime('now') is naive UTC; tag it explicitly.
                modified = modified.replace(" ", "T") + "Z"
            artifacts.append(ArtifactInfo(
                # name column is what the legacy /{name} URL uses; for
                # brain artifacts we route on the id directly.
                name=row_id,
                path=f"brain/{row_id}",
                # body_len is projected by artifact_list as
                # length(body)+length(body_blob); falls back to 0 only
                # for genuinely empty rows.
                size_bytes=int(row.get("body_len") or 0),
                title=title,
                modified_at=modified,
                subtask_id=row.get("subtask_id"),
                source="brain",
                id=row_id,
                kind=kind,
                media_type=row.get("media_type") or "text/markdown",
                # Supersede chain — lets the viewer collapse N review-round
                # artifacts (same title) down to the active one. Losers come
                # back with confidence≈0.1; the active row keeps ~0.8/0.9 and
                # carries ``supersedes`` pointing at the prior loop's id.
                confidence=(
                    float(row["confidence"])
                    if row.get("confidence") is not None
                    else None
                ),
                supersedes=row.get("supersedes"),
                audience=row.get("audience") or "user",
                created_by=row.get("created_by"),
            ))
    except Exception as e:
        logger.warning(f"Failed to list brain artifacts for {task_id}: {e}")

    # Disk — non-document outputs and legacy survivors.
    artifacts_dir = task_dir / "artifacts"
    if artifacts_dir.exists():
        try:
            for f in sorted(artifacts_dir.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    title = _extract_artifact_title(f)
                    if not title:
                        stem = f.stem  # "1.1" from "1.1.md"
                        title = subtask_map.get(stem, "")
                    st = f.stat()
                    mtime_iso = datetime.fromtimestamp(
                        st.st_mtime, tz=timezone.utc
                    ).isoformat()
                    artifacts.append(ArtifactInfo(
                        name=f.name,
                        path=str(f.relative_to(task_dir)),
                        size_bytes=st.st_size,
                        title=title,
                        modified_at=mtime_iso,
                        subtask_id=_parse_subtask_id_from_name(f.name),
                        source="disk",
                    ))
        except Exception as e:
            logger.warning(f"Failed to list disk artifacts in {artifacts_dir}: {e}")

    return artifacts


def _resolve_project_slug(project_path: str | None) -> str:
    """Map a filesystem project_path → projects.id (slug), else "".

    Mirrors okuro.sense.commitments._resolve_project. The inbox table keys
    rows by this slug, so resolving it here lets the task-detail UI scope a
    project-filtered inbox strip without re-deriving the slug client-side.
    Tolerant — any DB error yields "" (no slug, strip hidden).
    """
    if not project_path:
        return ""
    try:
        from okuro.db import get_db

        row = get_db().fetchone(
            "SELECT id FROM projects WHERE path = ? LIMIT 1",
            (project_path,),
        )
        return row["id"] if row else ""
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("project slug resolution failed for %s: %s", project_path, exc)
        return ""


def read_task_state(task_dir: Path) -> Optional[TaskState]:
    """Build real-time task state by aggregating all state files."""
    detail = read_task_detail(task_dir)
    if not detail:
        return None

    logs = read_task_logs(task_dir, limit=20)
    artifacts = read_task_artifacts(task_dir)

    # Read links, intelligence, mode, continuation suggestions, and
    # interventions from task.yaml.
    links = []
    intelligence = ""
    mode = ""
    continuation_suggestion = ""
    continuation_suggestions = []
    interventions: list[dict] = []
    awaiting_raw: Optional[dict] = None
    task_yaml = task_dir / "task.yaml"
    try:
        with open(task_yaml) as f:
            td = _yload(f)
        links = td.get("links", []) or []
        intelligence = td.get("intelligence", "") or ""
        mode = td.get("mode", "") or ""
        aw = td.get("awaiting")
        if isinstance(aw, dict) and aw.get("kind"):
            awaiting_raw = {
                "kind": str(aw.get("kind", "")),
                "message": str(aw.get("message", "")),
                "endpoint": str(aw.get("endpoint", "")),
                "method": str(aw.get("method", "POST")),
                "payload": dict(aw.get("payload") or {}),
                "since": str(aw.get("since", "")),
            }
        continuation_suggestion = td.get("continuation_suggestion", "") or ""
        # Structured suggestions (list of dicts)
        raw_suggestions = td.get("continuation_suggestions") or []
        if isinstance(raw_suggestions, list):
            continuation_suggestions = [s for s in raw_suggestions if isinstance(s, dict) and s.get("suggestion_text")]
        # Legacy fallback: wrap single string into list
        if not continuation_suggestions and continuation_suggestion:
            continuation_suggestions = [{
                "id": "sugg-legacy",
                "suggestion_text": continuation_suggestion,
                "category": "followup",
                "effort": "small",
            }]
        raw_interventions = td.get("interventions") or []
        if isinstance(raw_interventions, list):
            for entry in raw_interventions:
                if not isinstance(entry, dict):
                    continue
                if not entry.get("id") or not entry.get("text"):
                    continue
                kind = entry.get("kind", "continuation")
                default_source = "initial" if kind == "initial" else "user_typed"
                interventions.append({
                    "id": entry["id"],
                    "ts": entry.get("ts", ""),
                    "kind": kind,
                    "text": entry["text"],
                    "before_phase_id": int(entry.get("before_phase_id", 1)),
                    "source": entry.get("source") or default_source,
                })
        # Legacy backfill: any task created before interventions existed
        # gets a synthetic initial intervention from its description so
        # the flow chart is never empty of prompt nodes. Stable id derived
        # from task_id keeps the React key stable across refetches.
        if not interventions and td.get("description"):
            interventions.append({
                "id": f"int-initial-{td.get('id', task_dir.name)}",
                "ts": td.get("created_at", ""),
                "kind": "initial",
                "text": td["description"],
                "before_phase_id": 1,
                "source": "initial",
            })
    except Exception:
        pass

    # State validation — advisory warnings for the UI
    warnings: list[str] = []
    try:
        tasks_dir = task_dir.parent
        task_id = task_dir.name
        task_obj = load_task(task_id, tasks_dir)
        warnings = validate_task(task_obj)
    except Exception as e:
        logger.warning(f"Validation failed for {task_dir.name}: {e}")

    # Mark subtasks that are blocked behind a pending gate so the pipeline
    # view can render "Awaiting ADR" instead of plain "pending". Without
    # this annotation the user can't tell whether the engine is parked on
    # an unlocked decision or just out of worker slots.
    try:
        from okuro.orchestrator.state import pending_decision_gates
        pending = pending_decision_gates(task_obj)
        if pending:
            first_phase, first_gate = min(pending, key=lambda x: x[0])
            for ph in detail.phases:
                if ph.id < first_phase:
                    continue
                for st in ph.subtasks:
                    if st.status in ("pending", "approved", "waiting_approval"):
                        st.blocked_by_gate = True
                        st.blocking_gate_id = getattr(first_gate, "id", "")
                        st.blocking_gate_phase = first_phase
    except Exception as exc:
        logger.warning(f"blocked_by_gate annotation skipped: {exc}")

    # Step 2 — populate color_class + label on every phase and subtask so
    # any FE component (not just the snapshot consumer) renders consistently.
    # The snapshot endpoint computes the same values via read_task_snapshot;
    # both call sites read from the same maps + helpers.
    # Read the engine's review-active flag (file present iff M3 reviewer is
    # judging a phase right now). Used to overlay an inline "REVIEWING" badge
    # on the relevant phase + subtask cards.
    review_active_phase: Optional[int] = None
    review_active_subtasks: set[str] = set()
    import json as _json
    import time as _time
    # 30 min — matches engine._REVIEW_ACTIVE_TTL_S. A flag older than this is
    # from a crashed session; ignore it so the badge never sticks (reconcile_
    # on_startup reaps the file itself).
    _REVIEW_FLAG_TTL_S = 30.0 * 60.0
    review_flag_path = task_dir / ".review-active.json"
    if review_flag_path.exists():
        try:
            ra = _json.loads(review_flag_path.read_text())
            review_active_phase = int(ra.get("phase_id", 0) or 0) or None
            review_active_subtasks = set(ra.get("subtask_ids") or [])
        except Exception:
            pass
    # Per-subtask in-session review flags (.review-active.<subtask_id>.json),
    # written by the dispatcher session-loop while THIS subtask's artifact is
    # being judged. Union them in so the tile's "Reviewing" badge lights up for
    # the in-session review→correct loop, not just the M3 phase gate. Stale
    # (crashed-session) flags past the TTL are ignored.
    try:
        for _p in task_dir.glob(".review-active.*.json"):
            try:
                if (_time.time() - _p.stat().st_mtime) > _REVIEW_FLAG_TTL_S:
                    continue
                _ra = _json.loads(_p.read_text())
            except Exception:
                continue
            for _sid in _ra.get("subtask_ids") or []:
                review_active_subtasks.add(str(_sid))
            if review_active_phase is None:
                review_active_phase = int(_ra.get("phase_id", 0) or 0) or None
    except Exception:
        pass

    # Real per-subtask review verdict from convergence_telemetry. The session
    # loop publishes one row per (subtask, attempt) carrying the ACTUAL verdict,
    # so this is the truth for the tile: a subtask that LOOPED but whose latest
    # attempt PASSED must NOT read "reviewer fail" just because retries>0, and a
    # done+passed subtask must read PASS. Latest attempt wins. Falls back to the
    # retries/status heuristic below only when no telemetry exists.
    review_by_subtask: dict[str, tuple[int, str]] = {}
    try:
        from okuro.sense.task_events import list_events as _list_events
        for _e in _list_events(
            task_id=task_dir.name, event_type="convergence_telemetry", limit=500,
        ):
            _b = _e.get("body") or {}
            _sid = str(_b.get("subtask_id") or "")
            _v = str(_b.get("verdict") or "")
            if not _sid or not _v:
                continue
            _att = int(_b.get("attempt") or 0)
            _prev = review_by_subtask.get(_sid)
            if _prev is None or _att >= _prev[0]:
                review_by_subtask[_sid] = (_att, _v)
    except Exception:
        pass

    try:
        for ph in detail.phases:
            agg = _phase_lifecycle(ph)
            ph.state = agg["state"]
            ph.color_class = _color_for(_PHASE_COLOR, agg["state"])
            ph.label = _label_for(_PHASE_LABEL, agg["state"])
            ph.review_in_progress = (
                review_active_phase is not None and ph.id == review_active_phase
            )
            # When reviewer is judging, override the phase color/label so the
            # user sees the activity instead of seeing all-subtasks-done green.
            if ph.review_in_progress:
                ph.color_class = "warning"
                ph.label = "Reviewing — critic + scorer"
            phase_is_blocked = agg["state"] == "blocked_review"
            for st in ph.subtasks:
                st.review_in_progress = st.id in review_active_subtasks
                # FIX B — durable per-subtask review summary for the tile.
                # ``retries`` counts reviewer FAIL loops, so attempts reached
                # = retries + 1. Verdict is derived from terminal state:
                #   in-progress      → "" (pending, badge shows "reviewing")
                #   blocked_review   → "FAIL"  (rejected, loops exhausted/parked)
                #   done after loops → "PASS"  (converged)
                #   plain done       → "PASS"  (passed first time)
                # Only stamp when there was review activity (a retry loop,
                # an in-progress judge, or a completed subtask) so pending /
                # never-run tiles stay clean.
                _retries = int(st.retries or 0)
                _real = review_by_subtask.get(st.id)
                # A verdict belongs ONLY to the current cycle's output. A subtask
                # reset to pending (e.g. retries>0 after a sibling/dep change, or
                # a resume) will produce NEW output — any prior telemetry verdict
                # is STALE and must NOT show (else a 'pending' tile reads
                # "reviewed 2× · PASS"). Surface a verdict only for done (final)
                # or while a review is actively running.
                _reviewed = st.review_in_progress or st.status == "done"
                if _reviewed:
                    st.review_max_attempts = _REVIEW_MAX_ATTEMPTS
                    if st.review_in_progress:
                        # A review is running NOW — show "reviewing", no verdict.
                        st.review_attempt = (_real[0] if _real else _retries) + 1
                        st.review_verdict = ""
                    elif _real is not None:
                        # TRUTH: the actual latest review verdict for this subtask.
                        st.review_attempt = _real[0] or (_retries + 1)
                        st.review_verdict = _real[1]
                    else:
                        # Legacy fallback — no telemetry (pre-session-loop tasks).
                        st.review_attempt = _retries + 1
                        if phase_is_blocked and _retries > 0:
                            st.review_verdict = "FAIL"
                        else:
                            st.review_verdict = "PASS"
                # Done-but-reviewer-rejected → warning, not success.
                if st.review_in_progress and st.status == "done":
                    st.color_class = "warning"
                    st.label = "Reviewing — awaiting verdict"
                elif (
                    phase_is_blocked and st.status == "done" and (st.retries or 0) > 0
                    and (st.review_verdict or "").upper() in ("FAIL", "CAP")
                ):
                    # Only call it "rejected" when THIS subtask's own latest
                    # verdict actually failed — a passed subtask in a phase
                    # blocked by a sibling must not read as rejected.
                    st.color_class = "warning"
                    st.label = f"Reviewer rejected (after {st.retries} loop{'s' if st.retries != 1 else ''})"
                elif st.blocked_by_gate:
                    st.color_class = "warning"
                    st.label = "Waiting on ADR decision"
                else:
                    st.color_class = _color_for(_SUBTASK_COLOR, st.status)
                    st.label = _label_for(_SUBTASK_LABEL, st.status)
    except Exception as exc:
        logger.warning(f"phase/subtask color annotation skipped: {exc}")

    parallel_phases = [
        ph.id for ph in detail.phases
        if (ph.state == "running") or (ph.state == "" and any(
            s.status in ("running", "approved", "waiting_approval")
            for s in ph.subtasks
        ))
    ]

    return TaskState(
        task_id=detail.id,
        title=detail.title,
        description=detail.description,
        created_at=detail.created_at,
        status=detail.status,
        current_phase=detail.current_phase,
        progress_percent=detail.progress_percent,
        phases=detail.phases,
        recent_logs=logs,
        artifacts=artifacts,
        links=links,
        warnings=warnings,
        intelligence=intelligence,
        project_path=detail.project_path,
        project=_resolve_project_slug(detail.project_path),
        continuation_suggestion=continuation_suggestion,
        continuation_suggestions=continuation_suggestions,
        interventions=interventions,
        mode=mode,
        awaiting=(AwaitingState(**awaiting_raw) if awaiting_raw else None),
        parallel_phases=parallel_phases,
    )


# ---------------------------------------------------------------------------
# Canonical task snapshot — STEP 1 of the canonical-state migration.
# Single endpoint the FE reads + a future patch stream subscribes to.
# Replaces 6 polling hooks (taskState, gates, awaiting, capabilityGap,
# positions, discussion, graph) which were each producing 3–5 s skew.
# See docs/audit-2026-05-26/02-frontend-state-audit.md §6.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# color_class — closed enum the FE switches on. BE decides which colour every
# status maps to; FE renders it as-is. Adding a new BE state value means
# adding a row here, NOT touching FE code.
# Values: neutral | info | warning | error | success
# ---------------------------------------------------------------------------

# ROCK-SOLID v5 P1.3 — "a gate is open, needs your decision" had 13 distinct
# spellings across this file alone (evidence inventory, synonym cluster #1),
# three of which directly contradicted the calm gate copy the user actually
# reads (_PHASE_LABEL said "reviewer FAIL", _LIFECYCLE_LABEL said "needs
# override", _BLOCKER_KIND_LABEL — the card HEADER — said "OVERRIDE BLOCKED
# PHASE" while the card BODY says "Your work is fine — nothing was changed").
# One shared phrase for the status-badge pair below; the card header (a
# different UI role — an action prompt, not a status word) stays worded
# separately but equally calm.
_NEEDS_YOUR_CALL = "Needs your call"

_PHASE_COLOR: dict[str, str] = {
    "pending": "neutral",
    "running": "info",
    "active": "info",
    "done": "success",
    "failed": "error",
    "skipped": "neutral",
    "blocked_review": "warning",
    "awaiting_decision": "warning",
}

_PHASE_LABEL: dict[str, str] = {
    "pending": "Pending",
    "running": "Running",
    "active": "Running",
    "done": "Done",
    "failed": "Failed",
    "skipped": "Skipped",
    "blocked_review": _NEEDS_YOUR_CALL,
    "awaiting_decision": "Waiting on decision",
}

_SUBTASK_COLOR: dict[str, str] = {
    "pending": "neutral",
    "running": "info",
    "done": "success",
    "failed": "error",
    "skipped": "neutral",
    "approved": "info",
    "waiting_approval": "warning",
    "superseded": "neutral",
}

_SUBTASK_LABEL: dict[str, str] = {
    "pending": "Pending",
    "running": "Running",
    "done": "Done",
    "failed": "Failed",
    "skipped": "Skipped",
    "approved": "Approved · queued",
    "waiting_approval": "Awaiting approval",
    "superseded": "Superseded",
}

_LIFECYCLE_COLOR: dict[str, str] = {
    "pending": "neutral",
    "planning": "info",
    "deliberating": "info",
    "active": "info",
    "executing": "info",
    "reviewing": "warning",
    "awaiting_decision": "warning",
    "waiting_user": "warning",
    "blocked_review": "warning",
    "blocked": "warning",
    "halted": "warning",
    "done": "success",
    "failed": "error",
}

_LIFECYCLE_LABEL: dict[str, str] = {
    "pending": "Pending",
    "planning": "Planning",
    "deliberating": "Deliberating — gathering positions",
    "active": "Running",
    "executing": "Executing",
    "reviewing": "Reviewing",
    "awaiting_decision": "Waiting on decision",
    "waiting_user": "Waiting on you",
    "blocked_review": _NEEDS_YOUR_CALL,
    "blocked": "Blocked",
    "halted": "Halted",
    "done": "Done",
    "failed": "Failed",
}

_BLOCKER_KIND_LABEL: dict[str, str] = {
    "panel_confirmation": "Confirm research panel",
    "capability_gap": "Approve role-creation plan",
    "discussion_proceed": "Review positions + proceed",
    "decision_gate": "Lock decision",
    # ROCK-SOLID v5 P1.3 — was "Override blocked phase", which directly
    # contradicted the card's own body copy ("Your work is fine — nothing
    # was changed"). "Override" frames the user as bypassing a safety
    # system; they're answering a question. See _NEEDS_YOUR_CALL above.
    "blocked_review": "Review needed",
    "timeout_cap": f"{PART_NOUN_CAP} timed out — choose action",
    # A real awaiting kind the engine emits (subtask-level approval before a
    # risky step runs) that was never registered here, so the blocker rendered
    # with the raw machine string as its label. Keep this map in sync with
    # BlockerCard's KNOWN_KINDS — an unregistered kind degrades the card on
    # both sides at once.
    "subtask_approval": f"Approve {PART_NOUN}",
}


# ROCK-SOLID v5 P2.2 — a gate nobody has answered in this long is very
# probably forgotten rather than under consideration. Two days, not hours: a
# person is entitled to sleep on a real decision, and a flag that fires
# overnight teaches people to ignore it.
GATE_STALE_AFTER_HOURS = 48


def _gate_is_stale(since: str) -> bool:
    """Has this gate been open long enough to look forgotten?

    Derived on read, never persisted — staleness is a function of the clock,
    so a stored flag would be wrong the moment after it was written and would
    need a sweeper to keep honest.

    NOT a warning, and deliberately not an escalation: nothing is broken, and
    the recurring tasks that genuinely cannot afford to wait never park at
    all now (see autopilot.never_parks). This is for the interactive gate the
    user meant to come back to. Any unparseable timestamp answers False —
    inventing staleness from a bad clock reading would be its own false alarm.
    """
    if not since:
        return False
    try:
        opened = datetime.fromisoformat(str(since))
    except (TypeError, ValueError):
        return False
    # `since` is written by set_awaiting as a NAIVE utcnow() isoformat, so it
    # is compared against utcnow() rather than now(). Mixing the two here
    # would add a whole timezone offset to every gate's age — the same class
    # of bug that made a 90-second-old gate render as "waiting 2h 1m".
    if opened.tzinfo is not None:
        opened = opened.replace(tzinfo=None)
    return (utc_now_naive() - opened).total_seconds() > GATE_STALE_AFTER_HOURS * 3600


def _color_for(table: dict[str, str], key: str) -> str:
    return table.get(key, "neutral")


def _label_for(table: dict[str, str], key: str) -> str:
    return table.get(key, key.replace("_", " ").title())


def _phase_lifecycle(phase) -> dict:
    """Compute phase aggregate state + counts — presentation adapter.

    Delegates the state label to `state.phase_aggregate_state` (the
    canonical helper consumed by `derive_current_phase` too). The counts
    are presentation-only — the FE renders running/done/total badges
    directly off this block.
    """
    subtasks = list(getattr(phase, "subtasks", []) or [])
    total = len(subtasks)
    running = sum(1 for s in subtasks if s.status in ("running", "approved", "waiting_approval"))
    done = sum(1 for s in subtasks if s.status in ("done", "skipped", "superseded"))
    failed = sum(1 for s in subtasks if s.status == "failed")
    pending = total - running - done - failed
    return {
        "state": phase_aggregate_state(phase),
        "running": running,
        "done": done,
        "failed": failed,
        "pending": pending,
        "total": total,
    }


# ---------------------------------------------------------------------------
# C9 — Snapshot cache.
#
# Every /api/tasks/{id}/snapshot GET used to re-read task.yaml + plan.yaml +
# log.jsonl + .review-active.json from disk (50 concurrent reads = 50 disk
# reads). C9 invariant: the snapshot is cached in memory, served from cache,
# and invalidated only when a state-change event for that task is emitted
# via the C7 emit primitive (state.emit_event). On a cache miss the snapshot
# is recomputed and re-cached.
#
# Invalidation is wired in by state.emit_event(): every state_change
# event_type triggers `invalidate_snapshot_cache(task_id)`. Telemetry-only
# events do NOT invalidate — they are not consumer-visible state changes.
# ---------------------------------------------------------------------------

# Keyed by absolute task_dir path → (mtime_sig, snapshot). The signature is
# the (task.yaml, plan.yaml) mtimes, so any write — including one by the
# separate engine subprocess — self-heals on the next read. The path-only
# key + in-process invalidate_snapshot_cache() could not see cross-process
# writes, so a snapshot cached before an engine-driven transition (e.g. a
# phase flipping to blocked_review + set_awaiting) was served stale forever
# while /state and /awaiting (uncached) read fresh. Mirrors _SUMMARY_CACHE.
_SNAPSHOT_CACHE: dict[str, tuple] = {}

# P4.6 — the API's own sha, resolved once per process.
_API_VERSION_CACHE: Optional[str] = None


# Lifecycle states the snapshot projects beyond the persisted task.status enum.
# These are FE-facing labels — `derive_task_status` in state.py is the SoT for
# the persisted enum (`task.status`); this projection layer extends it with
# {executing, blocked_review, waiting_user} when the phase aggregate offers
# more specific information than the persisted status alone.
_TERMINAL_LIFECYCLE_STATUSES: frozenset[str] = frozenset(
    {"done", "failed", "halted", "blocked"}
)


# Liveness (Phase 1). engine_state is meaningful only while the engine is
# *supposed* to be actively working; parked/terminal lifecycles legitimately
# have no engine → "idle". The lease (.engine.lease) carries expires_at,
# refreshed by the engine heartbeat every ~3s with a 15s TTL, so a live
# engine always has expires_at > now. Grace ~= 3× TTL: a daemon reconciler
# respawn window before we call the engine wedged.
_ENGINE_LEASE_FILE = ".engine.lease"
_ENGINE_RECOVER_GRACE_S = 45.0
_ACTIVE_LIFECYCLES: frozenset[str] = frozenset(
    # Lifecycles where the engine is SUPPOSED to be working, so liveness is
    # meaningful. "active" is the raw working status that _derive_lifecycle_state
    # returns when status=="active" but no phase is mid-run (between dispatches);
    # it is non-terminal and shows Stop (isWorking), so a wedged "active" task
    # must surface the liveness banner instead of looking alive. A live engine
    # keeps its lease fresh → "live"; only a genuinely-gone engine → recovering/
    # wedged, so adding "active" raises no false alarms.
    {"active", "planning", "deliberating", "executing"}
)


def _read_engine_lease_expiry(task_dir: Path) -> Optional[float]:
    """Return the lease expires_at epoch, or None when absent/malformed."""
    try:
        p = task_dir / _ENGINE_LEASE_FILE
        if not p.exists():
            return None
        body = json.loads(p.read_text() or "{}")
        if not isinstance(body, dict):
            return None
        return float(body.get("expires_at", 0)) or None
    except Exception:
        return None


def _derive_engine_state(
    task_dir: Path, lifecycle_state: str, last_updated: Optional[object]
) -> str:
    """Derive {live, recovering, wedged, idle} from the engine lease.

    - Not actively working → "idle" (no engine expected; no banner).
    - Lease fresh (expires_at > now) → "live".
    - Lease expired / absent within grace → "recovering" (reconciler window).
    - Beyond grace → "wedged" (engine should be running but isn't — the
      "endless running / wedged-looks-alive" class made visible).

    Computed at READ time (never cached) so a dead engine surfaces even
    though it can no longer mutate task.yaml / emit events.
    """
    if lifecycle_state not in _ACTIVE_LIFECYCLES:
        return "idle"
    now = time.time()
    expires = _read_engine_lease_expiry(task_dir)
    if expires is not None and expires > now:
        return "live"
    # Lease expired or absent while work is in-flight. Use the lease expiry
    # if present, else the last state mutation, as the "stale since" anchor.
    stale_since = expires
    if stale_since is None:
        lu = last_updated
        if isinstance(lu, str) and lu:
            try:
                lu = datetime.fromisoformat(lu)
            except ValueError:
                lu = None
        if isinstance(lu, datetime):
            # last_updated is persisted as naive UTC; .timestamp() on a naive
            # datetime assumes LOCAL tz, which skews the grace comparison vs
            # time.time() (UTC epoch) by the local offset. Pin it to UTC.
            if lu.tzinfo is None:
                lu = lu.replace(tzinfo=timezone.utc)
            stale_since = lu.timestamp()
    if stale_since is not None and (now - stale_since) <= _ENGINE_RECOVER_GRACE_S:
        return "recovering"
    return "wedged"


def _derive_lifecycle_state(
    task_status: str, awaiting, phases_out: list[dict]
) -> str:
    """Project the persisted `task.status` (set by state.derive_task_status)
    into the snapshot's `lifecycle.state` FE label.

    `task.status` is the writer-side SoT (state.set_task_status →
    derive_task_status). This function is the read-side projection that
    adds {executing, blocked_review} when the phase roster carries more
    specific information than the persisted status. C2 — there is exactly
    one place that picks the FE label; both BE and FE consume it.
    """
    if awaiting is not None:
        return "waiting_user"
    if task_status in _TERMINAL_LIFECYCLE_STATUSES:
        return task_status
    if task_status in ("deliberating", "awaiting_decision", "planning"):
        return task_status
    if any(p["state"] == "running" for p in phases_out):
        return "executing"
    if any(p["state"] == "blocked_review" for p in phases_out):
        return "blocked_review"
    return task_status or "pending"


def invalidate_snapshot_cache(task_id: str | None = None) -> None:
    """Drop the cached snapshot for ``task_id`` (or every task when None).

    Called by state.emit_event() on every state-change emission. Idempotent;
    a miss is a no-op. When ``task_id`` is given, every cached entry whose
    key ends with ``/<task_id>`` is dropped — the cache is keyed by the
    absolute task_dir path so the production OKURO_ROOT and concurrent
    test tmp_paths never collide on bare task_id.
    """
    if task_id is None:
        _SNAPSHOT_CACHE.clear()
        _SUMMARY_CACHE.clear()
        return
    needle = f"/{task_id}"
    stale = [k for k in _SNAPSHOT_CACHE if k.endswith(needle) or k == task_id]
    for k in stale:
        _SNAPSHOT_CACHE.pop(k, None)
    # Summary cache is keyed by the full task_dir path (…/<task_id>); drop the
    # matching entry too. mtime-keying already self-heals, but clearing on the
    # mutation event keeps reads correct the instant a write lands.
    sstale = [k for k in _SUMMARY_CACHE if k.endswith(needle) or k == task_id]
    for k in sstale:
        _SUMMARY_CACHE.pop(k, None)


def _api_version() -> str:
    """The API process's own commit sha. Cached — git is a subprocess and this
    is called on every snapshot build."""
    global _API_VERSION_CACHE
    if _API_VERSION_CACHE is not None:
        return _API_VERSION_CACHE
    import subprocess as _sp

    sha = "unknown"
    try:
        r = _sp.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse",
             "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            sha = r.stdout.strip() or "unknown"
    except Exception:
        pass
    _API_VERSION_CACHE = sha
    return sha


def _engine_staleness(task_dir: Path) -> Optional[dict]:
    """Is the engine that owns this task running older code than the API?

    ROCK-SOLID v5 P4.6. The engine logs an `engine_version` row with its git
    sha at boot and NOTHING has ever read it — an engine subprocess is pinned
    to the code it was spawned with for the whole task lifetime, so after an
    API restart onto a newer commit the two silently disagree and a
    mismatched-engine bug looks exactly like a working-engine bug.

    Deliberately INFO, not a warning. Nothing is broken: a pinned engine is
    the designed behaviour (P4.5's restart exists precisely so it can pick up
    fixes at a pause). The chip exists so the disagreement is legible, not so
    it looks like a fault — the same rule that took the red pill off healthy
    gates in P1.

    Returns None when the shas match, either is unknown, or no engine_version
    row exists (an older task, or one whose engine never booted).
    """
    api_sha = _api_version()
    if api_sha == "unknown":
        return None
    engine_sha = ""
    try:
        log_file = task_dir / "log.jsonl"
        if not log_file.exists():
            return None
        # Last one wins: a respawned engine writes a fresh row, and the
        # CURRENT engine is the one whose staleness matters.
        for line in log_file.read_text().splitlines():
            if '"engine_version"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") == "engine_version":
                engine_sha = str(row.get("sha") or "")
    except OSError:
        return None

    if not engine_sha or engine_sha == "unknown" or engine_sha == api_sha:
        return None
    return {
        "engine_sha": engine_sha,
        "api_sha": api_sha,
        "color_class": "info",
        "label": "engine running older code — picks up fixes on next pause",
    }


def _generation_mtime(task_dir: Path) -> float:
    """Newest mtime across the files that hold persisted task state."""
    newest = 0.0
    for name in ("task.yaml", "plan.yaml"):
        try:
            newest = max(newest, (task_dir / name).stat().st_mtime)
        except OSError:
            continue
    return newest


def _generation_iso(task_dir: Path, last_updated) -> str:
    """When persisted state last changed, ISO-8601. See _generation_seq."""
    newest = _generation_mtime(task_dir)
    if newest:
        return datetime.utcfromtimestamp(newest).isoformat()
    try:
        return last_updated.isoformat() if last_updated else ""
    except AttributeError:
        return ""


def _generation_seq(task_dir: Path, last_updated) -> int:
    """Monotonic generation cursor: the newest persisted-state mtime, in ms.

    It used to be ``last_updated`` in epoch-ms — and NO task persists that
    field. ``save_task_meta`` never writes it, so ``TaskState.last_updated``
    fell through to its ``default_factory=datetime.utcnow`` and seq was
    simply "now, at read time": strictly increasing on every read, of every
    task, whether or not anything had changed. (Checked against the live
    tasks_dir: 0 of 6 recent real tasks carry the key.)

    Everything downstream trusted it. The frontend guard keeps a snapshot
    when ``seq(next) > seq(prev)``, which was therefore ALWAYS true — so the
    anti-flicker mechanism it exists for never rejected anything, and the
    subscribe-replay cursor (``seq > client_last_seq``) always fired.

    The (task.yaml, plan.yaml) mtime pair is the honest source: it is what
    the snapshot cache already keys on, it moves exactly when persisted state
    moves, and mtimes only go forward. ``last_updated`` is still honoured
    when a task really carries one, so a future writer that persists it wins.
    """
    newest = _generation_mtime(task_dir)
    if newest:
        return int(newest * 1000)
    # No files to stat (a task mid-creation). Fall back to last_updated if it
    # is a REAL persisted value; utcnow-default would reintroduce the bug.
    try:
        return int(last_updated.timestamp() * 1000) if last_updated else 0
    except (AttributeError, ValueError, OSError):
        return 0


def _content_hash(snapshot: dict) -> str:
    """Stable digest of a snapshot's content (ROCK-SOLID v5 P3.5).

    Paired with `seq`: seq orders generations, this detects a re-derived one.
    Any exception yields "" — a missing hash degrades to seq-only ordering,
    which is exactly the pre-P3.5 behaviour, never an error.
    """
    try:
        body = {k: v for k, v in snapshot.items() if k != "content_hash"}
        return hashlib.blake2b(
            json.dumps(body, sort_keys=True, default=str).encode(),
            digest_size=8,
        ).hexdigest()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("snapshot content hash failed: %r", exc)
        return ""


def _snapshot_cache_key(task_dir: Path) -> str:
    """Resolve to an absolute path string so concurrent tasks_dir parents
    (production OKURO_ROOT vs. test tmp_paths) never collide."""
    try:
        return str(task_dir.resolve())
    except OSError:
        return str(task_dir)


def _synthesize_stranded_blocker(task_dir: Path) -> dict:
    """A revival action for a task blocked with nothing to answer (P4.9).

    Deliberately NOT a decision card: there is no question to put to the
    user, because whatever set `blocked` never recorded one. It is an
    explanation plus the one action that revives the shape.
    """
    from okuro.orchestrator.gate_messages import humanize_gate

    gm = humanize_gate("blocked_review", "stuck", {"phase_id": 0})
    return {
        "kind": "blocked_review",
        "label": _label_for(_BLOCKER_KIND_LABEL, "blocked_review"),
        "color_class": "warning",
        "summary": gm.to_message(),
        "endpoint": f"/api/tasks/{task_dir.name}/retry",
        "method": "POST",
        "payload": {"presentation": gm.to_presentation()},
        "since": "",
        "stale": False,
    }


def _synthesize_blocked_review_blocker(
    task_dir: Path, phases_out: list[dict],
) -> dict:
    """Build a `blocked_review` blocker for a phase parked awaiting override.

    Used only when no `awaiting` record was persisted but a phase sits in
    `blocked_review`. Returns the same dict shape the FE BlockerCard
    consumes (kind / label / color_class / summary / endpoint / method /
    payload / since), wiring the phase-scoped override endpoint plus the
    capped subtasks and reviewer FAIL reason mined from log.jsonl.
    """
    # First blocked phase drives the override — the engine resolves them
    # one at a time, so the earliest is the actionable one.
    blocked_phase = next(
        (p for p in phases_out if p["state"] == "blocked_review"), None,
    )
    phase_id = int(blocked_phase["id"]) if blocked_phase else 0

    # Authoritative source: the phase_blocked_by_review_cap log row carries
    # the reviewer verdict, the capped subtask ids, and the finding count.
    verdict = "FAIL"
    capped: list[str] = []
    critic_findings = 0
    cap_ts = ""
    for entry in read_task_logs(task_dir, limit=400):
        if entry.type != "phase_blocked_by_review_cap":
            continue
        if int(entry.data.get("phase_id", -1)) != phase_id:
            continue
        verdict = str(entry.data.get("verdict") or "FAIL")
        capped = list(entry.data.get("capped_subtasks") or [])
        critic_findings = int(entry.data.get("critic_findings") or 0)
        cap_ts = entry.timestamp or cap_ts

    # Fallback when the log row is missing (older tasks): mine the blocked
    # phase's subtasks — a done subtask with retries>0 is reviewer-rejected.
    if not capped and blocked_phase:
        capped = [
            s["id"]
            for s in blocked_phase.get("subtasks", [])
            if s.get("status") == "done" and (s.get("retries") or 0) > 0
        ]

    # Reviewer FAIL reason — the error stamped on the capped subtasks.
    reason = ""
    if blocked_phase:
        for s in blocked_phase.get("subtasks", []):
            if s["id"] in capped and s.get("error"):
                reason = str(s["error"])
                break

    # ROCK-SOLID v5 P1.5 — was a hand-spliced sentence with raw subtask ids
    # inline ("...ran out on 2.1, 2.3...") and no `presentation`, so this
    # synthesized path (fires when no `awaiting` record was persisted, only
    # an on-disk blocked_review phase) rendered a degraded card next to the
    # real set_awaiting sites' humanize_gate copy. Route through the same
    # vocabulary: subtask ids move into technical_details, the summary
    # becomes gm.to_message() for any caller still reading the flat string.
    from okuro.orchestrator.gate_messages import humanize_gate as _hg
    gm = _hg("blocked_review", "", {
        "phase_id": phase_id,
        "rounds": len(capped) or 1,
        "raw_reason": (
            f"verdict={verdict}; capped_subtasks={', '.join(capped) or 'none'}"
            + (f"; last reviewer note: {reason[:300]}" if reason else "")
        ),
    })

    return {
        "kind": "blocked_review",
        "label": _label_for(_BLOCKER_KIND_LABEL, "blocked_review"),
        "color_class": "warning",
        "summary": gm.to_message(),
        "endpoint": (
            f"/api/tasks/{task_dir.name}/phases/{phase_id}"
            "/override-blocked-review"
        ),
        "method": "POST",
        "payload": {
            "phase_id": phase_id,
            "verdict": verdict,
            "capped_subtasks": capped,
            "critic_findings": critic_findings,
            "reason": reason,
            "presentation": gm.to_presentation(),
        },
        "since": cap_ts,
    }


def read_task_snapshot(task_dir: Path, *, use_cache: bool = True) -> Optional[dict]:
    """Build the canonical TaskSnapshot dict. See FE audit §6 for shape.

    Returns plain dicts (not Pydantic) so step 2 can extend the shape with
    color_class + label without churning models.py. Step 5 wraps this in a
    Pydantic model once the shape is stable.

    C9 — Serves from `_SNAPSHOT_CACHE` keyed by the absolute task_dir path.
    The cache is invalidated by `invalidate_snapshot_cache(task_id)` which
    is wired into `state.emit_event` so every state-change transition
    refreshes the snapshot on the next read. Pass ``use_cache=False`` to
    force a re-read (engine-internal callers that need a guaranteed-fresh
    view).
    """
    cache_key = _snapshot_cache_key(task_dir)
    # P3.6 — the review flags join the signature. Without them a review
    # starting or finishing was invisible to a cached read.
    sig = (
        _summary_sig(task_dir / "task.yaml", task_dir / "plan.yaml"),
        _review_flag_sig(task_dir),
    )
    if use_cache:
        cached = _SNAPSHOT_CACHE.get(cache_key)
        if cached is not None and cached[0] == sig:
            snap = cached[1]
            # Liveness is NOT part of the cache signature (the lease ticks
            # every 3s without touching task.yaml/plan.yaml) — recompute it
            # on every read so a dead/wedged engine surfaces from cache too.
            snap["engine_state"] = _derive_engine_state(
                task_dir, snap.get("lifecycle", {}).get("state", ""),
                snap.get("last_updated"),
            )
            return snap

    state = read_task_state(task_dir)
    if state is None:
        return None

    # Reconcile phase view — each phase carries an aggregate state computed
    # from its subtasks AND the persisted phase.status. The FE renders
    # phase.state directly; no derivation in component code.
    try:
        task_obj = load_task(task_dir.name, task_dir.parent)
    except Exception:
        task_obj = None

    phases_out: list[dict] = []
    parallel_running: list[int] = []
    for phase in (state.phases or []):
        agg = _phase_lifecycle(phase)
        if agg["running"] > 0:
            parallel_running.append(phase.id)
        # Subtask color override: when phase is blocked_review AND subtask
        # is 'done' with retries > 0, it's done-but-reviewer-rejected — must
        # render WARNING, not SUCCESS. This is the screenshot-bug structural
        # fix per docs/audit-2026-05-26/02-frontend-state-audit.md §4.1.
        phase_is_blocked = agg["state"] == "blocked_review"

        def _sub_color(s) -> str:
            base = _color_for(_SUBTASK_COLOR, s.status)
            if phase_is_blocked and s.status == "done" and (getattr(s, "retries", 0) or 0) > 0:
                return "warning"
            if getattr(s, "blocked_by_gate", False):
                return "warning"
            return base

        def _sub_label(s) -> str:
            if phase_is_blocked and s.status == "done" and (getattr(s, "retries", 0) or 0) > 0:
                return f"Reviewer rejected (after {s.retries} retries)"
            if getattr(s, "blocked_by_gate", False):
                return "Waiting on ADR decision"
            return _label_for(_SUBTASK_LABEL, s.status)

        phases_out.append({
            "id": phase.id,
            "name": phase.name,
            "status": phase.status,
            "state": agg["state"],
            "color_class": _color_for(_PHASE_COLOR, agg["state"]),
            "label": _label_for(_PHASE_LABEL, agg["state"]),
            "progress": {
                "running": agg["running"],
                "done": agg["done"],
                "failed": agg["failed"],
                "pending": agg["pending"],
                "total": agg["total"],
            },
            "subtasks": [
                {
                    "id": s.id,
                    "role": s.role,
                    "description": s.description,
                    "status": s.status,
                    "color_class": _sub_color(s),
                    "label": _sub_label(s),
                    "duration": s.duration,
                    "error": s.error,
                    "started_at": getattr(s, "started_at", "") or "",
                    "retries": getattr(s, "retries", 0) or 0,
                    "model": (
                        getattr(s, "model_override", "")
                        or getattr(s, "planned_model", "")
                        or getattr(s, "model_used", "")
                    ),
                    # FE SubtaskCard reads model_override || planned_model ||
                    # model_used (not the folded `model` above, which isn't in
                    # the FE SubtaskSummary type). Emit the three discrete
                    # fields so the tile always shows a badge. planned_model is
                    # resolved from the complexity tier (+ task intelligence)
                    # so DONE subtasks — whose persisted model_used is often ""
                    # — still render a model. Mirrors read_task_detail (~L272).
                    "planned_model": (
                        getattr(s, "planned_model", "")
                        or _resolve_planned_model(
                            getattr(s, "complexity", "standard") or "standard",
                            getattr(s, "model_override", "") or "",
                            state.intelligence or "",
                        )
                    ),
                    "model_used": getattr(s, "model_used", "") or "",
                    "model_override": getattr(s, "model_override", "") or "",
                    "risk": s.risk,
                    "artifacts": list(getattr(s, "artifacts", []) or []),
                    "artifact": getattr(s, "artifact", "") or "",
                    "blocked_by_gate": bool(getattr(s, "blocked_by_gate", False)),
                    "blocking_gate_phase": int(getattr(s, "blocking_gate_phase", 0) or 0),
                    # Review state — computed in read_task_state but previously
                    # dropped here, so the canonical snapshot the pipeline view
                    # consumes never carried it and the per-subtask "Reviewing"
                    # badge / verdict chip could never fire. Surface all four.
                    "review_in_progress": bool(getattr(s, "review_in_progress", False)),
                    "review_verdict": getattr(s, "review_verdict", "") or "",
                    "review_attempt": int(getattr(s, "review_attempt", 0) or 0),
                    "review_max_attempts": int(getattr(s, "review_max_attempts", 0) or 0),
                }
                for s in (phase.subtasks or [])
            ],
            "decision_gate": (
                {
                    "id": phase.decision_gate.id if getattr(phase, "decision_gate", None) else "",
                    "status": phase.decision_gate.status if getattr(phase, "decision_gate", None) else "",
                    "prompt": phase.decision_gate.prompt if getattr(phase, "decision_gate", None) else "",
                }
                if getattr(phase, "decision_gate", None) else None
            ),
        })

    # Lifecycle block — what is the engine doing right now.
    awaiting = state.awaiting
    lifecycle_state = _derive_lifecycle_state(state.status, awaiting, phases_out)

    blocker: Optional[dict] = None
    if awaiting is not None:
        blocker = {
            "kind": awaiting.kind,
            "label": _label_for(_BLOCKER_KIND_LABEL, awaiting.kind),
            "color_class": "warning",
            "summary": awaiting.message,
            "endpoint": awaiting.endpoint,
            "method": awaiting.method,
            "payload": dict(awaiting.payload or {}),
            "since": awaiting.since,
            "stale": _gate_is_stale(awaiting.since),
        }
    elif any(p["state"] == "blocked_review" for p in phases_out):
        # No awaiting record was persisted, but a phase is parked in
        # blocked_review (M3 FAIL with every implicated subtask at
        # max_retries). The engine flips the phase status without always
        # writing an awaiting blocker, which left these tasks rendering
        # blocker=null — the user only got a generic Retry, never the
        # targeted phase-scoped override. Synthesize the blocker the FE
        # BlockerCard expects so the override action is reachable.
        blocker = _synthesize_blocked_review_blocker(task_dir, phases_out)
    elif state.status == "blocked":
        # ROCK-SOLID v5 P4.9 — the stranded shape: status "blocked", no
        # `awaiting`, and no phase in blocked_review either. Nothing in the
        # two branches above matches, so the task rendered blocker=null: a
        # page that says the run is blocked and offers no way to unblock it.
        # The plan counted four live tasks sitting like this.
        #
        # A task reaches it when something set `blocked` without writing a
        # gate — a dependency that never resolved, a halt-layer disagreement
        # (five different terminal/halting sets, see the module note), or a
        # crash between the status write and the set_awaiting.
        #
        # Synthesised, not invented: it offers RETRY, which is the action
        # that genuinely revives this shape now that P4.7 makes retry clear
        # state and re-dispatch rather than 202-and-nothing.
        blocker = _synthesize_stranded_blocker(task_dir)

    snapshot: dict = {
        "task_id": state.task_id,
        "title": state.title,
        "description": state.description,
        "mode": state.mode,
        "intelligence": state.intelligence,
        "project_path": state.project_path,
        "created_at": state.created_at,
        "current_phase": state.current_phase,
        "progress_percent": state.progress_percent,
        "phases_total": len(phases_out),
        "lifecycle": {
            "state": lifecycle_state,
            "label": _label_for(_LIFECYCLE_LABEL, lifecycle_state),
            "color_class": _color_for(_LIFECYCLE_COLOR, lifecycle_state),
            "progress_percent": state.progress_percent,
            "current_phase": state.current_phase,
            # Why it halted, in the halter's own words. The 2026-08-03 sweep
            # wrote this into 43 task.yaml files and NOTHING read it back — the
            # UI kept showing the generic banner while a specific explanation
            # sat on disk. Empty string for every non-halted task, so the FE
            # can render on truthiness alone.
            "halt_reason": (
                getattr(task_obj, "halt_reason", "") or ""
            ) if task_obj is not None else "",
        },
        "blocker": blocker,
        # Liveness as a first-class rendered fact (Phase 1): {live,
        # recovering, wedged, idle}. Derived from the engine lease at read
        # time so a crashed/wedged engine no longer renders identically to a
        # working one.
        "engine_state": _derive_engine_state(
            task_dir, lifecycle_state, state.last_updated
        ),
        "phases": phases_out,
        "parallel_phases": parallel_running,
        "interventions": [i.model_dump() if hasattr(i, "model_dump") else dict(i) for i in (state.interventions or [])],
        "warnings": list(state.warnings or []),
        "continuation_suggestions": list(state.continuation_suggestions or []),
        # Same source as `seq` below, and for the same reason: nothing
        # persists last_updated, so reading it off TaskState returned
        # utcnow() — a timestamp that said "just now" for a task untouched
        # for a week, and that changed on every read, which also made the
        # content hash unstable. Derived from the persisted-state mtimes it
        # is both honest and stable.
        "last_updated": _generation_iso(task_dir, state.last_updated),
        # ORDERING cursor — monotonic, and NOT content-stable. Derived from
        # last_updated (epoch ms), which is a persisted task.yaml field, so
        # the 8 s poll and the WS push agree for the same underlying state and
        # a newer state always carries a higher seq. That kills the
        # poll-regresses-a-fresher-push flicker (Phase 0) and is the only
        # thing seq is good for.
        #
        # It was documented as "content-stable". It is not, and the gap is not
        # theoretical: several snapshot fields are computed at READ time and
        # move with the clock or the filesystem while task.yaml sits still —
        # `engine_state` (the liveness overlay, "never cached" by design) and
        # `blocker.stale` (P2.2, flips at 48 h). Two reads can therefore carry
        # the same seq and different content, and the FE's seq guard would
        # drop the newer one. The FE had already grown a per-field exemption
        # for engine_state; `blocker.stale` had none, so a gate crossing 48 h
        # on an open page would never have shown its note.
        #
        # `content_hash` closes that WITHOUT giving up the ordering: same seq
        # + different hash means "same generation, re-derived content, apply
        # it"; a LOWER seq is still refused however the hash differs.
        "seq": _generation_seq(task_dir, state.last_updated),
        # P4.6 — None when the engine and API agree, which is the normal case,
        # so the FE renders nothing rather than a permanent chip.
        "engine_staleness": _engine_staleness(task_dir),
    }
    # Computed last, over the finished snapshot, so no field can be forgotten
    # — the failure mode of a hand-listed digest is exactly the per-field
    # exemption this replaces. sort_keys makes it order-independent; default=str
    # keeps datetimes from raising.
    snapshot["content_hash"] = _content_hash(snapshot)
    # C9 — populate cache. Even when called with use_cache=False the result
    # is cached so the next default-cache reader hits memory.
    _SNAPSHOT_CACHE[cache_key] = (sig, snapshot)
    return snapshot


def list_all_tasks(tasks_dir: Path) -> List[TaskSummary]:
    """List all tasks from the tasks directory, newest-first by created_at.

    Pre-fix this sorted by directory name reverse-alphabetical. Because
    `task-rec-*` (recurring jobs) sort after `task-202*` in ASCII, those
    rows dominated the first 50-result page and buried every regular
    task — including in-flight deliberate-mode runs — past the default
    list limit. Switching to created_at_desc puts the most recent work
    at the top regardless of name prefix.
    """
    if not tasks_dir.exists():
        return []

    tasks: List[TaskSummary] = []
    try:
        task_dirs = list(tasks_dir.iterdir())
    except Exception as e:
        logger.warning(f"Failed to list tasks in {tasks_dir}: {e}")
        task_dirs = []

    # Per-task isolation: a single malformed task.yaml (e.g. a null
    # description failing TaskSummary validation) must NOT abort the whole
    # loop — pre-fix, one bad task silently dropped every task after it in
    # iteration order from the list (and from the home/sidebar that depend
    # on it). Catch per directory so one bad row can't hide the rest.
    for task_dir in task_dirs:
        if task_dir.is_dir() and not task_dir.name.startswith("."):
            try:
                summary = read_task_summary(task_dir)
            except Exception as e:
                logger.warning(f"Skipping unreadable task {task_dir.name}: {e}")
                continue
            if summary:
                tasks.append(summary)

    # Sort newest-first by created_at; fall back to directory name for ties
    # (and tasks with no created_at, which would otherwise drift past real
    # work).
    tasks.sort(
        key=lambda t: (t.created_at or "", t.id),
        reverse=True,
    )
    return tasks
