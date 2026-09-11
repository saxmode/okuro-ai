# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Checkpoint/Restore for Okuro Orchestrator Tasks
# index:
#   imports
#   class CheckpointType
#   class Checkpoint
#   def _checkpoints_dir
#   def _prune_old_checkpoints
#   def create_checkpoint
#   def list_checkpoints
#   def get_latest_checkpoint
#   def restore_checkpoint
# AGENT_HEADER_END -->
"""
Checkpoint/Restore for Okuro Orchestrator Tasks

File-based snapshots of plan.yaml + task.yaml before risky operations.
"""

import json
import logging
import shutil
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from okuro.orchestrator.yamlfast import yload

logger = logging.getLogger("okuro.orchestrator.checkpoint")

MAX_CHECKPOINTS = 10

# Columns we snapshot for role_handovers. Pinned explicitly so a future
# schema add (vector embeddings, summary cache, etc.) doesn't silently
# pollute checkpoint files with binary blobs or unstable columns.
_HANDOVER_COLUMNS = (
    "id", "task_id", "subtask_id", "from_role", "to_subtask_id",
    "to_role", "brief", "cortex_refs", "kg_edges", "artifact_refs",
    "memory_refs", "supersedes", "status", "confidence", "created_by",
    "created_at", "consumed_at",
)


def _snapshot_role_handovers(task_id: str) -> list[dict] | None:
    """Snapshot role_handovers rows for a task. Returns None on DB
    unavailability (test environments, fresh installs). The checkpoint
    pipeline must keep working even when the DB is gone."""
    try:
        from okuro.db import get_db
        db = get_db()
        col_list = ", ".join(_HANDOVER_COLUMNS)
        rows = db.fetchall(
            f"SELECT {col_list} FROM role_handovers WHERE task_id = ? "
            "ORDER BY created_at ASC",
            (task_id,),
        )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"role_handovers snapshot skipped (db unavailable): {e}")
        return None


def _restore_role_handovers(task_id: str, rows: list[dict]) -> int:
    """Hard rollback: delete current task_id rows, re-insert the
    snapshot. Returns the number of rows re-inserted. Best-effort —
    failures log but don't abort the file-side restore.

    All DB writes are wrapped in the writer transaction so the delete
    and re-insert commit atomically. ``db.execute()`` alone does NOT
    commit under okuro's connection settings.
    """
    try:
        from okuro.db import get_db
        db = get_db()
        with db.write():
            # Drop vec parity rows for any current ids
            # (vec_role_handovers is best-effort per migration 035 —
            # table may not exist if sqlite-vec is unavailable).
            try:
                db.conn.execute(
                    "DELETE FROM vec_role_handovers WHERE id IN "
                    "(SELECT id FROM role_handovers WHERE task_id = ?)",
                    (task_id,),
                )
            except Exception:
                pass  # vec table optional
            db.conn.execute(
                "DELETE FROM role_handovers WHERE task_id = ?", (task_id,)
            )
            if rows:
                col_list = ", ".join(_HANDOVER_COLUMNS)
                placeholders = ", ".join(["?"] * len(_HANDOVER_COLUMNS))
                params = [
                    tuple(row.get(c) for c in _HANDOVER_COLUMNS) for row in rows
                ]
                db.conn.executemany(
                    f"INSERT INTO role_handovers ({col_list}) "
                    f"VALUES ({placeholders})",
                    params,
                )
        return len(rows)
    except Exception as e:
        logger.warning(f"role_handovers restore skipped: {e}")
        return 0


class CheckpointType(str, Enum):
    PHASE = "phase"
    DISPATCH = "dispatch"
    MANUAL = "manual"
    RECOVERY = "recovery"


@dataclass
class Checkpoint:
    id: str
    task_id: str
    timestamp: str
    checkpoint_type: str
    reason: str
    subtask_id: Optional[str] = None
    phase_id: Optional[int] = None
    files: list[str] = None

    def __post_init__(self):
        if self.files is None:
            self.files = []

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Checkpoint":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _checkpoints_dir(task_id: str, tasks_dir: Path) -> Path:
    return tasks_dir / task_id / ".checkpoints"


def _prune_old_checkpoints(task_id: str, tasks_dir: Path):
    cp_dir = _checkpoints_dir(task_id, tasks_dir)
    if not cp_dir.exists():
        return
    checkpoint_dirs = sorted([d for d in cp_dir.iterdir() if d.is_dir()], key=lambda d: d.name)
    while len(checkpoint_dirs) > MAX_CHECKPOINTS:
        oldest = checkpoint_dirs.pop(0)
        try:
            shutil.rmtree(oldest)
        except Exception as e:
            logger.warning(f"Failed to prune checkpoint {oldest.name}: {e}")


def create_checkpoint(
    task_id: str,
    tasks_dir: Path,
    checkpoint_type: CheckpointType = CheckpointType.MANUAL,
    reason: str = "",
    subtask_id: Optional[str] = None,
    phase_id: Optional[int] = None,
) -> Optional[Checkpoint]:
    task_dir = tasks_dir / task_id
    if not task_dir.exists():
        logger.warning(f"Cannot checkpoint: task dir {task_dir} does not exist")
        return None

    # Microsecond resolution — without it, a RECOVERY checkpoint taken
    # at the start of restore_checkpoint() can land in the SAME directory
    # as the target it's about to restore (both happen within one
    # wall-clock second). The recovery checkpoint then overwrites the
    # target's snapshot files. Surfaced by the umbrella-audit-fix-#6
    # test suite — pre-fix it was a silent latent corruption.
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    cp_path = _checkpoints_dir(task_id, tasks_dir) / timestamp
    cp_path.mkdir(parents=True, exist_ok=True)

    files_copied = []
    for filename in ("plan.yaml", "task.yaml", "state.md", "log.jsonl"):
        src = task_dir / filename
        if src.exists():
            try:
                shutil.copy2(src, cp_path / filename)
                files_copied.append(filename)
            except Exception as e:
                logger.warning(f"Failed to copy {filename} for checkpoint: {e}")

    # Umbrella audit fix #6 — snapshot role_handovers rows alongside the
    # files. Pre-fix, restore reverted plan.yaml/state.md but left the
    # post-failure handover rows in the DB; downstream subagents re-ran
    # with stale brief context (the very ghost handovers the file
    # restore was supposed to undo). Best-effort: if the DB is gone,
    # snapshot=None and we don't fail the checkpoint.
    snapshot = _snapshot_role_handovers(task_id)
    if snapshot is not None:
        try:
            (cp_path / "handovers.json").write_text(json.dumps(snapshot, indent=2))
            files_copied.append("handovers.json")
        except Exception as e:
            logger.warning(f"Failed to write handovers.json snapshot: {e}")

    if not files_copied:
        try:
            shutil.rmtree(cp_path)
        except Exception:
            pass
        return None

    checkpoint = Checkpoint(
        id=timestamp,
        task_id=task_id,
        timestamp=datetime.utcnow().isoformat(),
        checkpoint_type=checkpoint_type.value,
        reason=reason,
        subtask_id=subtask_id,
        phase_id=phase_id,
        files=files_copied,
    )
    try:
        (cp_path / "checkpoint.json").write_text(json.dumps(checkpoint.to_dict(), indent=2))
    except Exception as e:
        logger.warning(f"Failed to write checkpoint metadata: {e}")

    _prune_old_checkpoints(task_id, tasks_dir)
    logger.info(f"Checkpoint {timestamp} created for {task_id}: {reason}")
    return checkpoint


def list_checkpoints(task_id: str, tasks_dir: Path) -> list[Checkpoint]:
    cp_dir = _checkpoints_dir(task_id, tasks_dir)
    if not cp_dir.exists():
        return []

    checkpoints = []
    for d in sorted(cp_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta_path = d / "checkpoint.json"
        if meta_path.exists():
            try:
                data = json.loads(meta_path.read_text())
                checkpoints.append(Checkpoint.from_dict(data))
            except Exception as e:
                logger.warning(f"Failed to load checkpoint {d.name}: {e}")
        else:
            checkpoints.append(Checkpoint(
                id=d.name, task_id=task_id, timestamp=d.name,
                checkpoint_type="unknown", reason="(no metadata)",
                files=[f.name for f in d.iterdir() if f.is_file() and f.name != "checkpoint.json"],
            ))
    return checkpoints


def get_latest_checkpoint(task_id: str, tasks_dir: Path) -> Optional[Checkpoint]:
    checkpoints = list_checkpoints(task_id, tasks_dir)
    return checkpoints[0] if checkpoints else None


def restore_checkpoint(task_id: str, checkpoint_id: str, tasks_dir: Path) -> tuple[bool, str]:
    task_dir = tasks_dir / task_id
    cp_path = _checkpoints_dir(task_id, tasks_dir) / checkpoint_id

    if not cp_path.exists():
        return False, f"Checkpoint {checkpoint_id} not found"

    create_checkpoint(task_id, tasks_dir, checkpoint_type=CheckpointType.RECOVERY,
                      reason=f"Before restore to {checkpoint_id}")

    restored = []
    for filename in ("plan.yaml", "task.yaml", "state.md", "log.jsonl"):
        src = cp_path / filename
        dst = task_dir / filename
        if src.exists():
            try:
                shutil.copy2(src, dst)
                restored.append(filename)
            except Exception as e:
                return False, f"Failed to restore {filename}: {e}"

    # Umbrella audit fix #6 — replay role_handovers snapshot. Without
    # this, post-failure rows persist and pollute the dispatcher's
    # upstream-context injection on re-dispatch.
    handovers_path = cp_path / "handovers.json"
    if handovers_path.exists():
        try:
            snapshot_rows = json.loads(handovers_path.read_text())
            if not isinstance(snapshot_rows, list):
                snapshot_rows = []
            restored_count = _restore_role_handovers(task_id, snapshot_rows)
            logger.info(
                f"Restored {restored_count} role_handover row(s) for {task_id}"
            )
            restored.append("handovers.json")
        except Exception as e:
            logger.warning(f"Failed to restore handovers.json: {e}")

    if not restored:
        return False, "No files found in checkpoint"

    if "plan.yaml" in restored:
        try:
            plan_path = task_dir / "plan.yaml"
            plan_data = yload(plan_path.read_text())
            reset_count = 0
            if plan_data and "phases" in plan_data:
                for phase in plan_data["phases"]:
                    for st in phase.get("subtasks", []):
                        if st.get("status") in ("running", "waiting_approval"):
                            st["status"] = "pending"
                            st["error"] = ""
                            reset_count += 1
            if reset_count:
                plan_path.write_text(yaml.dump(plan_data, default_flow_style=False, sort_keys=False))
                logger.info(f"Reset {reset_count} orphaned subtask(s) after restore")
        except Exception as e:
            logger.warning(f"Failed to reset subtask statuses after restore: {e}")

    msg = f"Restored {', '.join(restored)} from checkpoint {checkpoint_id}"
    logger.info(msg)
    return True, msg
