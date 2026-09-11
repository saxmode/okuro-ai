# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Flow feedback (F3) — store the user's post-flow rating, join it to
#   the flow's autopilot_decision events, and feed forward-comments to planning.
# index:
#   OUTCOME_CLASSES
#   def record_feedback
#   def get_feedback
#   def _read_autopilot_decisions
#   def decisions_for_task
#   def unresolved_forward_comments
#   def mark_comments_resolved
# AGENT_HEADER_END -->
"""Flow feedback — the F3 learning loop.

Backed by the ``flow_feedback`` table (migration 108): one row per task,
keyed by ``task_id``. F2 made every orchestrator gate answer itself in-process
and emit a durable ``autopilot_decision`` event into that task's
``log.jsonl``. Those events carried no outcome label, so nothing could learn
which auto-answers were good. F3 supplies the label:

  * :func:`record_feedback` / :func:`get_feedback` — the rating write/read path.
  * :func:`decisions_for_task` — the learning JOIN: every autopilot_decision the
    flow emitted, tagged with the flow's usability + outcome_class. This is the
    clean accessor a future scorer (F-future) consumes; F3 itself does no ML.
  * :func:`unresolved_forward_comments` / :func:`mark_comments_resolved` — the
    forward-note feed that ``decomposer.decompose_task`` injects into the NEXT
    flow's plan prompt (bounded + non-repeating).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("okuro.orchestrator.feedback")

# Closed outcome vocabulary — mirrors the CHECK constraint in migration 108.
# The API validates against this before writing so the caller gets a clean 422.
OUTCOME_CLASSES: tuple[str, ...] = (
    "success",
    "partial",
    "failed",
    "off_track",
    "needs_rework",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_feedback(
    task_id: str,
    usability: int,
    outcome_class: str,
    comment: Optional[str] = None,
) -> dict:
    """Insert (or replace) the feedback row for a finished flow.

    One row per task — a re-rating overwrites the prior row via
    ``ON CONFLICT(task_id)``. ``resolved_at`` is reset to NULL on every write so
    an edited forward-comment becomes eligible for injection again.

    Raises ``ValueError`` on an out-of-range score or an unknown outcome_class
    (the API also validates, but the storage layer must not trust its caller).
    """
    if not task_id:
        raise ValueError("task_id must be non-empty")
    if not 1 <= int(usability) <= 5:
        raise ValueError("usability must be 1..5")
    if outcome_class not in OUTCOME_CLASSES:
        raise ValueError(f"outcome_class must be one of {list(OUTCOME_CLASSES)}")

    comment = (comment or "").strip() or None
    now = _utc_now_iso()

    from okuro.db import get_db

    get_db().execute(
        """
        INSERT INTO flow_feedback
            (task_id, usability, outcome_class, comment, resolved_at,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            usability    = excluded.usability,
            outcome_class = excluded.outcome_class,
            comment      = excluded.comment,
            resolved_at  = NULL,
            updated_at   = excluded.updated_at
        """,
        (task_id, int(usability), outcome_class, comment, now, now),
    )
    row = get_feedback(task_id)
    assert row is not None  # just wrote it
    return row


def get_feedback(task_id: str) -> Optional[dict]:
    """The feedback row for a task, or None if it was never rated."""
    from okuro.db import get_db

    row = get_db().fetchone(
        "SELECT * FROM flow_feedback WHERE task_id = ?", (task_id,)
    )
    return dict(row) if row else None


def _read_autopilot_decisions(task_id: str, tasks_dir: Path) -> list[dict]:
    """Every ``autopilot_decision`` event in a task's log.jsonl, in order.

    The events are the durable signal F2 emits at each auto-answered gate.
    They live in the per-task JSONL log (not the DB), so the join reads the
    file and filters by ``type``.
    """
    log_path = Path(tasks_dir) / task_id / "log.jsonl"
    if not log_path.exists():
        return []
    out: list[dict] = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "autopilot_decision":
                    out.append(event)
    except OSError as exc:
        logger.warning("feedback: failed to read %s: %r", log_path, exc)
    return out


def decisions_for_task(task_id: str, tasks_dir: Path) -> dict[str, Any]:
    """Learning join: this flow's autopilot decisions labelled by its outcome.

    Returns::

        {
          "task_id": <id>,
          "rated": <bool>,                # was flow_feedback recorded?
          "usability": <int|None>,
          "outcome_class": <str|None>,
          "decisions": [ <autopilot_decision event>, ... ],
        }

    Each decision event is returned verbatim (kind, chosen_option, rationale,
    confidence, options, ts, seq, …). A future scorer reads this to correlate
    an auto-answer's confidence against the flow's realised outcome — no ML
    here, just the clean labelled accessor.
    """
    feedback = get_feedback(task_id)
    decisions = _read_autopilot_decisions(task_id, tasks_dir)
    return {
        "task_id": task_id,
        "rated": feedback is not None,
        "usability": feedback["usability"] if feedback else None,
        "outcome_class": feedback["outcome_class"] if feedback else None,
        "decisions": decisions,
    }


def unresolved_forward_comments(limit: int = 3) -> list[dict]:
    """Most-recent unresolved forward-comments, newest first.

    A row is a candidate when it has a non-empty ``comment`` and ``resolved_at``
    is NULL. ``decompose_task`` injects these into the next flow's plan prompt
    then calls :func:`mark_comments_resolved`, so each note shapes one future
    flow and does not echo forever. ``limit`` bounds the injected context.
    """
    if limit <= 0:
        return []
    from okuro.db import get_db

    rows = get_db().fetchall(
        """
        SELECT task_id, usability, outcome_class, comment, created_at
        FROM flow_feedback
        WHERE resolved_at IS NULL AND comment IS NOT NULL AND comment != ''
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (int(limit),),
    )
    return [dict(r) for r in rows]


def mark_comments_resolved(task_ids: list[str]) -> int:
    """Stamp ``resolved_at`` on the given feedback rows. Returns rows touched."""
    task_ids = [t for t in (task_ids or []) if t]
    if not task_ids:
        return 0
    from okuro.db import get_db

    now = _utc_now_iso()
    placeholders = ",".join("?" for _ in task_ids)
    cur = get_db().execute(
        f"UPDATE flow_feedback SET resolved_at = ?, updated_at = ? "
        f"WHERE task_id IN ({placeholders}) AND resolved_at IS NULL",
        (now, now, *task_ids),
    )
    try:
        return int(cur.rowcount)
    except (AttributeError, TypeError):
        return 0
