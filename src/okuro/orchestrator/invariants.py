# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Structural invariants over task state — promotes silent-corruption
#   detection from a log line to an alertable metric.
# index: Violation | INVARIANTS | check_task | check_all_tasks |
#   invariant_metrics
# AGENT_HEADER_END -->
"""Structural invariants over orchestrator task state.

WHY THIS EXISTS
---------------
On 2026-07-24 a silent data-corruption class was found by a human noticing
that a task looked wrong. 6.6% of all planned tasks on the machine (20 of 302)
carried false state: subtasks marked `skipped` that had actually shipped a
deliverable, tasks reporting a clean `done` over phases that never ran.

The corruption had been accumulating for months. Nothing alerted. And okuro
had already DETECTED it — `reconciler_discrepancy` fired correctly, on time,
naming the exact subtask — and wrote it to a log nobody reads.

A detector that cannot act is a bug that ships.

This module promotes that class of check from "log line someone might grep"
to a measurable, alertable signal. The reconciler answers "does disk agree
with the brain?" for one subtask. This answers "is this task's state
self-consistent at all?" — the properties that must hold in EVERY reachable
state, regardless of how the task got there.

DESIGN
------
Each invariant is a pure predicate over a loaded Task. No IO beyond the
artifact lookup, no mutation, no repair. Violations are returned as data so
callers choose the response: CI fails the build, a health endpoint emits a
gauge, an operator runs a report.

Invariants describe the CLASS, not the incident. `I001` would have caught the
original bug, but it is written as "no terminal-skipped subtask has a
deliverable" — which also catches any future path that reaches that state by
some other route. That is the point: the bug we fixed is one member of the set
this file guards.

Adding an invariant is cheap and is the correct response to any future
state-corruption incident. Add it here FIRST, watch the metric, then fix.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from okuro.db.engine import okuro_home

logger = logging.getLogger("okuro.orchestrator.invariants")


# Severity drives the response, not the wording.
#   critical — silent wrongness. State asserts something false to the user.
#              Alert. These are the ones that cost trust.
#   warning  — inconsistent but self-evident, or recoverable without loss.
SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class Violation:
    code: str          # stable id, e.g. "I001" — safe to alert on
    severity: str
    task_id: str
    subtask_id: str    # "" for task-level invariants
    detail: str        # one line, names the concrete conflict

    def __str__(self) -> str:  # pragma: no cover - display only
        where = f"{self.task_id}/{self.subtask_id}" if self.subtask_id else self.task_id
        return f"[{self.code}/{self.severity}] {where} — {self.detail}"


def _shipped(task_id: str, subtask_id: str) -> bool:
    """True when a Stream B artifact exists for this subtask."""
    try:
        from okuro.sense.artifacts import artifact_list
        return bool(artifact_list(task_id=task_id, subtask_id=subtask_id, limit=1))
    except Exception:
        # Unknown evidence must never manufacture a violation — a DB blip would
        # otherwise page someone about corruption that does not exist.
        return False


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------


def _i001_skipped_with_deliverable(task, shipped) -> list[Violation]:
    """A subtask that shipped must never read as `skipped`.

    THE ORIGINAL BUG. Override wrote `skipped` onto work that had produced an
    artifact and a handover, so the deliverable existed but nothing downstream
    could see it. Stated as a property, this also catches any future writer
    that reaches the same state by a different route.
    """
    out = []
    for phase in task.phases:
        for st in phase.subtasks:
            if st.status == "skipped" and st.id in shipped:
                out.append(Violation(
                    "I001", SEVERITY_CRITICAL, task.id, st.id,
                    "status='skipped' but a Stream B artifact exists — a real "
                    "deliverable is being hidden from downstream consumers",
                ))
    return out


def _i002_done_task_with_unfinished_work(task, shipped) -> list[Violation]:
    """A task reporting `done` must have no failed or never-run subtask.

    This is what let two hollow phases ship under a green `done`. `done` is a
    promise to the user; `completed_partial` exists precisely so a degraded
    run can be honest instead.
    """
    if (task.status or "") != "done":
        return []
    out = []
    for phase in task.phases:
        for st in phase.subtasks:
            if st.status == "failed":
                out.append(Violation(
                    "I002", SEVERITY_CRITICAL, task.id, st.id,
                    "task.status='done' but this subtask failed — the run is "
                    "reporting success over work that did not happen "
                    "(expected 'completed_partial')",
                ))
            elif st.status in ("pending", "running", "approved"):
                out.append(Violation(
                    "I002", SEVERITY_CRITICAL, task.id, st.id,
                    f"task.status='done' but this subtask is still "
                    f"'{st.status}' — it never ran to completion",
                ))
    return out


def _i003_review_verdict_forged_work_status(task, shipped) -> list[Violation]:
    """A review outcome must never be recorded as a work failure.

    `failed` means one thing: no deliverable was produced. A review that could
    not settle used to overwrite it purely to lock re-dispatch, which made
    every downstream reader treat shipped work as failed.
    """
    out = []
    for phase in task.phases:
        for st in phase.subtasks:
            if st.status == "failed" and st.id in shipped:
                out.append(Violation(
                    "I003", SEVERITY_CRITICAL, task.id, st.id,
                    "status='failed' but a deliverable exists — a review "
                    "verdict has been written onto the work-outcome field",
                ))
    return out


def _i004_overridden_subtask_still_blocking(task, shipped) -> list[Violation]:
    """Once a human resolves a review, nothing may stay stranded behind it.

    The user's decision is authoritative. A dependent left unrunnable after an
    override means the override did not actually release the run.
    """
    by_id = {st.id: st for ph in task.phases for st in ph.subtasks}
    resolved = {
        sid for sid, st in by_id.items()
        if getattr(st, "review_state", "") == "overridden"
        and st.status in ("done", "skipped", "superseded")
    }
    if not resolved:
        return []
    out = []
    for st in by_id.values():
        if st.status != "pending":
            continue
        blockers = [d for d in st.dependencies if d in resolved]
        unmet = [
            d for d in st.dependencies
            if d in by_id
            and by_id[d].status not in ("done", "skipped", "superseded")
        ]
        if blockers and not unmet:
            out.append(Violation(
                "I004", SEVERITY_WARNING, task.id, st.id,
                f"pending with every dependency satisfied (via overridden "
                f"{', '.join(blockers)}) — should be dispatchable, not parked",
            ))
    return out


def _i005_terminal_task_with_dispatchable_work(task, shipped) -> list[Violation]:
    """A terminal task must not leave runnable work on the floor.

    Distinct from I002: this fires for any terminal status, and is the check
    that would have caught phases 5-6 sitting dispatchable-but-dead.
    """
    if (task.status or "") not in ("done", "failed", "halted", "completed_partial"):
        return []
    by_id = {st.id: st for ph in task.phases for st in ph.subtasks}
    out = []
    for st in by_id.values():
        if st.status not in ("pending", "approved"):
            continue
        deps_ok = all(
            by_id[d].status in ("done", "skipped", "superseded")
            for d in st.dependencies if d in by_id
        )
        if deps_ok:
            out.append(Violation(
                "I005", SEVERITY_WARNING, task.id, st.id,
                f"task is terminal ('{task.status}') but this subtask is "
                f"'{st.status}' with all dependencies met — runnable work was "
                "abandoned",
            ))
    return out


INVARIANTS: list[tuple[str, Callable]] = [
    ("I001", _i001_skipped_with_deliverable),
    ("I002", _i002_done_task_with_unfinished_work),
    ("I003", _i003_review_verdict_forged_work_status),
    ("I004", _i004_overridden_subtask_still_blocking),
    ("I005", _i005_terminal_task_with_dispatchable_work),
]


# --------------------------------------------------------------------------
# Runners
# --------------------------------------------------------------------------


def check_task(task_id: str, tasks_dir: Path, *, task=None) -> list[Violation]:
    """Every invariant violation for one task. Read-only."""
    if task is None:
        from okuro.orchestrator.state import load_task
        try:
            task = load_task(task_id, tasks_dir)
        except Exception as exc:
            logger.warning("invariants: could not load %s (%r)", task_id, exc)
            return []

    # One artifact-store pass per task, shared by every invariant.
    shipped = {
        st.id
        for ph in task.phases for st in ph.subtasks
        if _shipped(task.id, st.id)
    }

    out: list[Violation] = []
    for code, fn in INVARIANTS:
        try:
            out.extend(fn(task, shipped))
        except Exception as exc:
            # A broken invariant must not take down the whole sweep.
            logger.warning("invariants: %s raised on %s (%r)", code, task_id, exc)
    return out


def check_all_tasks(tasks_dir: Path, *, limit: Optional[int] = None) -> list[Violation]:
    """Sweep every task on disk. Read-only."""
    if not tasks_dir.is_dir():
        return []
    dirs = sorted(p for p in tasks_dir.iterdir() if (p / "plan.yaml").exists())
    if limit:
        dirs = dirs[-limit:]
    out: list[Violation] = []
    for d in dirs:
        out.extend(check_task(d.name, tasks_dir))
    return out


def invariant_metrics(tasks_dir: Path, *, limit: Optional[int] = None) -> dict:
    """Aggregate counts suitable for a gauge / health endpoint / alert rule.

    `corrupt_task_rate` is the number to alert on. It is the fraction of tasks
    asserting something false about themselves — the measurement that did not
    exist while 6.6% of tasks were silently wrong.
    """
    violations = check_all_tasks(tasks_dir, limit=limit)
    total = len(
        [p for p in tasks_dir.iterdir() if (p / "plan.yaml").exists()]
    ) if tasks_dir.is_dir() else 0

    by_code: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    corrupt_tasks: set[str] = set()
    for v in violations:
        by_code[v.code] = by_code.get(v.code, 0) + 1
        by_severity[v.severity] = by_severity.get(v.severity, 0) + 1
        if v.severity == SEVERITY_CRITICAL:
            corrupt_tasks.add(v.task_id)

    return {
        "tasks_scanned": total,
        "violations_total": len(violations),
        "violations_by_code": by_code,
        "violations_by_severity": by_severity,
        "corrupt_tasks": len(corrupt_tasks),
        "corrupt_task_rate": (len(corrupt_tasks) / total) if total else 0.0,
    }


# --------------------------------------------------------------------------
# CLI — the part that makes this actionable rather than merely observable.
#
#   python -m okuro.orchestrator.invariants            # report, exit 1 on critical
#   python -m okuro.orchestrator.invariants --metrics  # JSON for a gauge/alert
#
# Non-zero exit on a critical violation is deliberate: it makes this usable as
# a CI gate and a cron alert without any further glue. The original incident
# happened because the only output was a log line with no consumer.
# --------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - CLI
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Check orchestrator state invariants")
    ap.add_argument(
        "--tasks-dir", type=Path,
        default=okuro_home() / "orchestrator" / "tasks",
    )
    ap.add_argument("--limit", type=int, default=None,
                    help="only the N most recent tasks")
    ap.add_argument("--metrics", action="store_true",
                    help="emit JSON metrics instead of a report")
    ap.add_argument("--warnings-fail", action="store_true",
                    help="exit non-zero on warnings too")
    args = ap.parse_args(argv)

    if args.metrics:
        print(json.dumps(invariant_metrics(args.tasks_dir, limit=args.limit), indent=2))
        return 0

    violations = check_all_tasks(args.tasks_dir, limit=args.limit)
    if not violations:
        print("no invariant violations")
        return 0

    by_task: dict[str, list[Violation]] = {}
    for v in violations:
        by_task.setdefault(v.task_id, []).append(v)
    for task_id, vs in sorted(by_task.items()):
        print(f"\n{task_id}")
        for v in vs:
            print(f"  {v.code} {v.severity:8} {v.subtask_id or '-':6} {v.detail}")

    crit = [v for v in violations if v.severity == SEVERITY_CRITICAL]
    print(
        f"\n{len(violations)} violation(s) across {len(by_task)} task(s) — "
        f"{len(crit)} critical"
    )
    if crit or (args.warnings_fail and violations):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
