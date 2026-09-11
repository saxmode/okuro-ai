# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Okuro Orchestrator Recurring Task Engine.
# index:
#   imports
#   class RecurringTaskDef
#   def load_recurring_defs
#   def next_cron_after
#   def compute_next_run
#   def get_overdue_defs
#   def create_recurring_run
#   def update_recurring_after_run
#   def _atomic_write
# AGENT_HEADER_END -->
"""Okuro Orchestrator Recurring Task Engine."""

from __future__ import annotations

import json
import logging
import yaml
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from okuro.orchestrator.yamlfast import yload

logger = logging.getLogger("okuro.orchestrator.recurring")

# Tier a recurring run asks its subagents for. Only two values are reachable:
# the dispatcher's tier logic is binary (dispatcher_streaming.py — "strategic"
# when task.intelligence == "max", else "standard"), so there is no `fast` here
# even though okuro.bridge (and therefore the daemon-task registry) has one.
# Spelled in the bridge's vocabulary — `quality` maps to the orchestrator's
# `strategic` on the way down, and the API normalizes the same way on the way
# out, so the UI shows one word for opus across both panels.
TIER_STANDARD = "standard"
TIER_QUALITY = "quality"
RECURRING_TIERS = frozenset({TIER_STANDARD, TIER_QUALITY})


@dataclass
class OutcomeEntry:
    """Record of a single recurring task run outcome."""
    run_id: str
    outcome: str = "unknown"   # useful | empty | failed
    summary: str = ""
    completed_at: str = ""


@dataclass
class RecurringTaskDef:
    id: str
    title: str
    description: str
    role: str                   # primary role (backward compat)
    scheduler: str              # cron expression
    status: str = "scheduled"
    last_run_at: str = ""
    next_run_at: str = ""
    run_count: int = 0
    # --- Intelligent recurring extensions ---
    description_template: str = ""   # template with {last_run_summary} placeholder
    roles: List[str] = field(default_factory=list)  # multi-role (overrides single `role`)
    adaptive: bool = False           # if True, each run incorporates previous learnings
    outcome_history: List[OutcomeEntry] = field(default_factory=list)
    tier: str = TIER_STANDARD        # standard | quality — see RECURRING_TIERS
    # --- Drawn-workflow binding ---
    # Names a MANUALLY ARRANGED workflow (okuro.orchestrator.workflow_store): its
    # drawn graph compiles to the run's plan and the LLM decomposer is never
    # called. Empty = the historical behaviour (plan from the description).
    workflow_id: str = ""
    # Fills the {placeholders} in that workflow's node prompts. Fixed per
    # definition — every run of the schedule compiles the same graph the same way.
    workflow_params: dict = field(default_factory=dict)
    path: Optional[Path] = field(default=None, repr=False)


def load_recurring_defs(recurring_dir: Path) -> List[RecurringTaskDef]:
    if not recurring_dir.exists():
        return []
    defs = []
    for yaml_file in sorted(recurring_dir.glob("*.yaml")):
        try:
            with open(yaml_file) as f:
                data = yload(f)
            if not data:
                continue
            def_id = yaml_file.stem
            tier = str(data.get("tier") or TIER_STANDARD).strip().lower()
            if tier not in RECURRING_TIERS:
                # Fall back rather than raise: one typo shouldn't stop the whole
                # schedule. Falling back DOWN is the safe direction — a typo can
                # never silently escalate a daily task onto the expensive tier.
                logger.warning(
                    f"Recurring def {def_id} has unknown tier {tier!r} — using {TIER_STANDARD}"
                )
                tier = TIER_STANDARD
            outcomes = []
            for o in data.get("outcome_history", []):
                outcomes.append(OutcomeEntry(
                    run_id=o.get("run_id", ""),
                    outcome=o.get("outcome", "unknown"),
                    summary=o.get("summary", ""),
                    completed_at=o.get("completed_at", ""),
                ))
            defs.append(RecurringTaskDef(
                id=def_id,
                title=data.get("title", def_id),
                description=data.get("description", ""),
                role=data.get("role", ""),
                scheduler=data.get("scheduler", ""),
                status=data.get("status", "scheduled"),
                last_run_at=data.get("last_run_at") or "",
                next_run_at=data.get("next_run_at") or "",
                run_count=data.get("run_count", 0),
                description_template=data.get("description_template", ""),
                roles=data.get("roles", []),
                adaptive=data.get("adaptive", False),
                outcome_history=outcomes,
                tier=tier,
                workflow_id=(data.get("workflow_id") or "").strip(),
                workflow_params=dict(data.get("workflow_params") or {}),
                path=yaml_file,
            ))
        except Exception as e:
            logger.warning(f"Failed to load recurring def {yaml_file}: {e}")
    return defs


def next_cron_after(cron_expr: str, after: datetime) -> datetime:
    from croniter import croniter
    if after.tzinfo is not None:
        after = after.replace(tzinfo=None)
    c = croniter(cron_expr, after)
    return c.get_next(datetime)


def compute_next_run(cron_expr: str, last_run_at: str = "") -> str:
    # All recurring timestamps are stored UTC-naive (last_run_at is written via
    # datetime.utcnow()). Compare in the same zone or the def becomes "overdue"
    # for tz_offset hours every day.
    base = datetime.utcnow()
    if last_run_at:
        try:
            base = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
            if base.tzinfo is not None:
                base = base.replace(tzinfo=None)
        except ValueError:
            base = datetime.utcnow()
    return next_cron_after(cron_expr, base).isoformat()


def get_overdue_defs(defs: List[RecurringTaskDef]) -> List[RecurringTaskDef]:
    now = datetime.utcnow()
    overdue = []
    for def_ in defs:
        if def_.status != "scheduled":
            continue
        if not def_.scheduler:
            logger.warning(f"Recurring def {def_.id} has no scheduler expression — skipping")
            continue
        if not def_.next_run_at:
            overdue.append(def_)
            continue
        try:
            next_run = datetime.fromisoformat(def_.next_run_at.replace("Z", "+00:00"))
            if next_run.tzinfo is not None:
                next_run = next_run.replace(tzinfo=None)
            if next_run <= now:
                overdue.append(def_)
        except ValueError:
            logger.warning(f"Invalid next_run_at for {def_.id}: {def_.next_run_at!r} — triggering")
            overdue.append(def_)
    return overdue


def _resolve_description(def_: RecurringTaskDef) -> str:
    """Build the run description, incorporating adaptive template if set."""
    if def_.adaptive and def_.description_template:
        last_summary = ""
        if def_.outcome_history:
            last = def_.outcome_history[-1]
            last_summary = last.summary or f"({last.outcome})"
        return def_.description_template.replace("{last_run_summary}", last_summary)
    return def_.description


def create_recurring_run(def_: RecurringTaskDef, tasks_dir: Path) -> str:
    task_id = f"task-rec-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{def_.id}"
    task_dir = tasks_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "artifacts").mkdir(exist_ok=True)

    # The description is resolved the same way whether or not a workflow is
    # bound. It is the human-readable description of the run (title bar, logs,
    # continuation prompts) and, when adaptive, still the only channel carrying
    # last run's learnings. What a workflow replaces is the PLAN, not the prose:
    # a drawn node's prompt comes from the node and its {placeholders} come from
    # workflow_params (flow_compiler._subtask), never from this string.
    description = _resolve_description(def_)
    active_roles = def_.roles if def_.roles else [def_.role]
    now_iso = datetime.utcnow().isoformat()

    task_data: dict = {
        "id": task_id,
        "task_type": "recurring",
        "recurring_def_id": def_.id,
        "title": def_.title,
        "description": description,
        "status": "pending",
        "created_at": now_iso,
        "current_phase": 1,
    }
    if def_.roles:
        task_data["required_roles"] = def_.roles
    # Written only when set, so an absent key stays the honest "no workflow"
    # signal all the way down to workflow_run.phases_for_task — the same rule
    # state.create_task follows for its own task.yaml.
    if def_.workflow_id:
        task_data["workflow_id"] = def_.workflow_id
        if def_.workflow_params:
            task_data["workflow_params"] = dict(def_.workflow_params)
    # Tier → the dispatcher's only knob. Before this, create_recurring_run never
    # set intelligence, so dispatcher_streaming's
    #   _tier = "strategic" if task.intelligence == "max" else "standard"
    # pinned EVERY recurring run to standard, permanently and invisibly.
    #
    # Writing intelligence into task.yaml is tier-only here: the engine's
    # intelligence→gates_enabled coupling lives in its --new CLI path, and this
    # task is resumed, so no gate is armed. No LLM decomposition is re-run
    # either — either the plan is pre-written below, or it is compiled from a
    # drawn graph, which is deterministic. An unattended recurring task
    # therefore cannot be left blocking on an approval it has no one to ask.
    if def_.tier == TIER_QUALITY:
        task_data["intelligence"] = "max"
    _atomic_write(task_dir / "task.yaml", yaml.dump(task_data, default_flow_style=False))

    # The plan. Which of the two shapes we write is load-bearing:
    #
    #   no workflow — write the one-phase stub below, as recurring runs always
    #                 have. The engine resumes against it and never plans.
    #   workflow    — write NO plan.yaml at all. load_task leaves task.phases
    #                 empty, and an empty task.phases is the ONE condition under
    #                 which the resume path calls workflow_run.phases_for_task
    #                 and compiles the drawn graph. Writing the stub anyway would
    #                 silently win: the workflow would sit bound in task.yaml,
    #                 unused, and the schedule would quietly run the wrong thing.
    if not def_.workflow_id:
        # Build subtasks — one per role for multi-role, or single subtask for single role
        subtasks = []
        for i, role in enumerate(active_roles, 1):
            subtasks.append({
                "id": f"1.{i}",
                "role": role,
                "description": description,
                "risk": "LOW",
                "complexity": "standard",
                "status": "pending",
                "dependencies": [],
                "artifact_name": f"1.{i}-findings",
                "phase": 1,
                "cli_used": "", "model_used": "", "output_summary": "",
                "started_at": "", "completed_at": "",
                "duration": 0.0, "retries": 0, "error": "",
            })

        plan_data = {
            "phases": [{
                "id": 1,
                "name": "Recurring Run",
                "status": "pending",
                "subtasks": subtasks,
            }],
        }
        _atomic_write(task_dir / "plan.yaml", yaml.dump(plan_data, default_flow_style=False))

    with open(task_dir / "log.jsonl", "w") as f:
        f.write(json.dumps({"timestamp": now_iso, "type": "task_created",
                            "detail": f"Recurring run for {def_.id}"}) + "\n")

    logger.info(f"Created recurring run {task_id} for def {def_.id}")
    return task_id


def update_recurring_after_run(def_: RecurringTaskDef) -> None:
    if def_.path is None:
        logger.warning(f"Cannot update def {def_.id} — no path set")
        return
    try:
        with open(def_.path) as f:
            data = yload(f) or {}
        now_iso = datetime.utcnow().isoformat()
        data["last_run_at"] = now_iso
        data["run_count"] = data.get("run_count", 0) + 1
        data["next_run_at"] = compute_next_run(def_.scheduler, now_iso)
        _atomic_write(def_.path, yaml.dump(data, default_flow_style=False))
        logger.info(f"Updated {def_.id}: next_run_at={data['next_run_at']}")
    except Exception as e:
        logger.error(f"Failed to update recurring def {def_.id}: {e}")


def record_outcome(def_: RecurringTaskDef, run_id: str, outcome: str, summary: str = "") -> None:
    """Record a run outcome in the recurring def's history."""
    if def_.path is None:
        return
    try:
        with open(def_.path) as f:
            data = yload(f) or {}
        history = data.get("outcome_history", [])
        history.append({
            "run_id": run_id,
            "outcome": outcome,
            "summary": summary,
            "completed_at": datetime.utcnow().isoformat(),
        })
        # Keep last 50 entries
        data["outcome_history"] = history[-50:]
        _atomic_write(def_.path, yaml.dump(data, default_flow_style=False))
    except Exception as e:
        logger.error(f"Failed to record outcome for {def_.id}: {e}")


def update_recurring_def(def_: RecurringTaskDef, updates: dict) -> bool:
    """Update a recurring def's YAML with arbitrary fields."""
    if def_.path is None:
        return False
    try:
        with open(def_.path) as f:
            data = yload(f) or {}
        for k, v in updates.items():
            if k in ("id", "path"):
                continue  # immutable
            data[k] = v
        _atomic_write(def_.path, yaml.dump(data, default_flow_style=False))
        return True
    except Exception as e:
        logger.error(f"Failed to update recurring def {def_.id}: {e}")
        return False


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.rename(path)
