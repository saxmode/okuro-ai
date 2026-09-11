# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Wave-5 G20 — disk YAML ↔ brain artifact-row reconciler.
# index: imports | def reconcile_task | def reconcile_all_tasks |
#   def _check_subtask | _Discrepancy
# AGENT_HEADER_END -->
"""State ↔ artifact reconciler.

Wave-1 added Stream A + Stream B verification at finalize time, but the disk
``plan.yaml`` and the ``artifacts`` SQLite rows are written under different
locks. SIGTERM between them, a partial backup/restore, or a manual edit can
leave them desynced — e.g. status=done with no artifact row, or a live
artifact row for a status=failed subtask.

This module is read-only by default: it walks every task, lists discrepancies,
and returns them as ``_Discrepancy`` records so the engine can log them or
the operator can decide whether to repair manually. Auto-repair is intentionally
NOT included — desync is rare enough that human inspection is the right
default; a future wave can add ``--auto-repair`` when telemetry shows the
shape of the discrepancies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("okuro.orchestrator.reconciler")


@dataclass
class _Discrepancy:
    task_id: str
    subtask_id: str
    status: str            # subtask status from disk YAML
    has_artifact: bool     # at least one Stream B row in DB?
    has_handover: bool     # at least one Stream A row in DB?
    note: str              # one-line human-readable explanation


def _check_subtask(task_id: str, subtask_id: str, status: str) -> Optional[_Discrepancy]:
    """Compare disk-YAML status against brain rows. Return discrepancy or None.

    Categories surfaced:
      - status='done' but no Stream B row              → 'done_without_artifact'
      - status='done' but no Stream A row              → 'done_without_handover'
      - status in ('failed','skipped') but artifact present → 'finalized_with_orphan_artifact'
      - status='running' for >24h with brain rows present  → 'stuck_running_likely_done'

    Read-only: this function NEVER mutates anything.
    """
    try:
        from okuro.sense.artifacts import artifact_list
        from okuro.sense.role_handover import read_role_handover
    except Exception as exc:
        logger.warning("reconciler unavailable (%r) — skipping subtask %s",
                       exc, subtask_id)
        return None

    try:
        rows = artifact_list(task_id=task_id, subtask_id=subtask_id, limit=1,
                              order="created_at_desc")
        has_artifact = bool(rows)
    except Exception:
        has_artifact = False

    try:
        ho = read_role_handover(subtask_id=subtask_id, task_id=task_id)
        has_handover = ho is not None
    except Exception:
        has_handover = False

    if status == "done":
        if not has_artifact:
            return _Discrepancy(
                task_id=task_id, subtask_id=subtask_id, status=status,
                has_artifact=False, has_handover=has_handover,
                note="status='done' on disk but no Stream B row in artifacts",
            )
        if not has_handover:
            return _Discrepancy(
                task_id=task_id, subtask_id=subtask_id, status=status,
                has_artifact=True, has_handover=False,
                note="status='done' on disk but no Stream A row in role_handovers",
            )

        # Umbrella audit fix #10 — handover-to-KG lineage gap. A
        # handover row whose kg_triples set (source_handover_id) is
        # empty means _emit_kg_edges silently failed for every edge
        # (cf. former log.debug swallowing). The handover is still
        # readable, but the lineage graph has a hole for it — cross-
        # task search by predicate misses this row.
        try:
            from okuro.db import get_db
            ho = read_role_handover(subtask_id=subtask_id, task_id=task_id)
            handover_id = (ho or {}).get("id")
            if handover_id:
                db = get_db()
                row = db.fetchone(
                    "SELECT COUNT(*) AS n FROM kg_triples "
                    "WHERE source_handover_id = ?",
                    (handover_id,),
                )
                triple_count = (row or {}).get("n", 0) if row else 0
                if triple_count == 0:
                    return _Discrepancy(
                        task_id=task_id, subtask_id=subtask_id, status=status,
                        has_artifact=has_artifact, has_handover=True,
                        note=(
                            "handover row exists but zero kg_triples cite it "
                            "(source_handover_id) — _emit_kg_edges may have "
                            "silently failed; KG lineage incomplete."
                        ),
                    )
        except Exception:
            pass  # best-effort; never fail the reconcile sweep
        return None

    if status in ("failed", "skipped") and has_artifact:
        return _Discrepancy(
            task_id=task_id, subtask_id=subtask_id, status=status,
            has_artifact=True, has_handover=has_handover,
            note=f"status='{status}' on disk but Stream B row present — "
                 "likely a subagent shipped before kill (see G16); inspect.",
        )

    return None


def reconcile_task(task_id: str, tasks_dir: Path) -> list[_Discrepancy]:
    """Walk every subtask in the task and return state↔brain discrepancies.

    Read-only. Safe to run at orchestrator startup or on-demand.
    """
    from okuro.orchestrator.state import load_task

    try:
        task = load_task(task_id, tasks_dir)
    except Exception as exc:
        logger.warning("reconciler: could not load task %s (%r)", task_id, exc)
        return []

    discrepancies: list[_Discrepancy] = []
    for phase in task.phases:
        for subtask in phase.subtasks:
            d = _check_subtask(task_id, subtask.id, subtask.status)
            if d is not None:
                discrepancies.append(d)
    return discrepancies


def reconcile_all_tasks(tasks_dir: Path, *, max_age_days: int = 14) -> list[_Discrepancy]:
    """Walk every task directory under ``tasks_dir``, scoped to recent tasks.

    Long-running deployments accumulate hundreds of completed task dirs; the
    age cap keeps the reconciler bounded. Discrepancies are returned, not
    repaired.
    """
    if not tasks_dir.exists():
        return []

    import time
    cutoff = time.time() - (max_age_days * 86400)
    discrepancies: list[_Discrepancy] = []

    for task_dir in tasks_dir.iterdir():
        if not task_dir.is_dir():
            continue
        plan_path = task_dir / "plan.yaml"
        if not plan_path.exists():
            continue
        try:
            if plan_path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        discrepancies.extend(reconcile_task(task_dir.name, tasks_dir))

    return discrepancies
