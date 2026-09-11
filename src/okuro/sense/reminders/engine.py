# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Reminder engine — CRUD operations (MCP tools) and eval loop (timer).
# index:
#   imports
#   def _load_strategy
#   def _get_channel_registry
#   def create_reminder
#   def list_reminders
#   def snooze_reminder
#   def dismiss_reminder
#   def acknowledge_reminder
#   def evaluate_due
#   def _handle_escalation
# AGENT_HEADER_END -->
"""Reminder engine — CRUD operations (MCP tools) and eval loop (timer).

All database access goes through okuro.db (SQLite).
Strategy is resolved from the user profile via compute_strategy().
Cascade steps are materialized via cascade.generate_cascade().
Delivery goes through the channel registry.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .strategy import compute_strategy, BehavioralStrategy
from .cascade import generate_cascade
from ..channels.base import Message, DeliveryStatus

log = logging.getLogger("okuro.reminders")


def _load_strategy() -> BehavioralStrategy:
    """Load user profile from DB and compute behavioral strategy."""
    try:
        from okuro.yu.profile import get_profile_raw
        profile = get_profile_raw()
        return compute_strategy(profile)
    except Exception as e:
        log.warning("Failed to load profile for strategy: %s", e)
        return compute_strategy({})


def _get_channel_registry():
    """Lazy-import ChannelRegistry to avoid circular imports."""
    from ..channels import ChannelRegistry
    return ChannelRegistry()


# ============================================================
# CRUD — called by MCP tools
# ============================================================

def create_reminder(
    what: str,
    when_due: str | datetime,
    urgency: int = 3,
    channels: Optional[list[str]] = None,
    repeat: Optional[dict] = None,
    project: Optional[str] = None,
    context: Optional[dict] = None,
    source: str = "user",
) -> dict:
    """Create a reminder with materialized cascade steps.

    Args:
        what: Human-readable reminder text.
        when_due: ISO 8601 datetime string or datetime object (UTC).
        urgency: 1-5 priority level.
        channels: Explicit channel override.
        repeat: Repeat config, e.g. {"every": "1w"}.
        project: Optional project slug.
        context: Arbitrary metadata dict.
        source: One of "user", "agent", "system".
    """
    from okuro.db import get_db

    db = get_db()

    # Normalize when_due
    if isinstance(when_due, datetime):
        if when_due.tzinfo is None:
            when_due = when_due.replace(tzinfo=timezone.utc)
        when_due_str = when_due.isoformat()
        when_due_dt = when_due
    else:
        when_due_str = when_due
        when_due_dt = datetime.fromisoformat(when_due)
        if when_due_dt.tzinfo is None:
            when_due_dt = when_due_dt.replace(tzinfo=timezone.utc)
            when_due_str = when_due_dt.isoformat()

    ctx = context or {}
    if project:
        ctx["project"] = project

    reminder_id = str(uuid.uuid4())
    urg = max(1, min(5, urgency))

    db.execute(
        """INSERT INTO reminders (id, what, when_due, urgency, context, source, status, snooze_count, repeat)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
        (reminder_id, what, when_due_str, urg, json.dumps(ctx), source, json.dumps(repeat or {})),
    )
    db.conn.commit()

    reminder = {
        "id": reminder_id,
        "what": what,
        "when_due": when_due_str,
        "urgency": urg,
        "context": ctx,
        "source": source,
        "status": "pending",
        "snooze_count": 0,
    }

    # Generate cascade
    strategy = _load_strategy()
    steps = generate_cascade(
        reminder_id=reminder_id,
        when_due=when_due_dt,
        urgency=urg,
        strategy=strategy,
        explicit_channels=channels,
    )

    reminder["cascade_steps"] = steps
    log.info("Created reminder %s: '%s' due %s (%d cascade steps)",
             reminder_id, what, when_due_str, len(steps))
    return reminder


def list_reminders(
    status: Optional[str] = None,
    upcoming_hours: Optional[float] = None,
    include_suggestions: bool = False,
) -> dict:
    """Query reminders with optional filters."""
    from okuro.db import get_db

    db = get_db()

    sql = "SELECT * FROM reminders"
    params: list = []
    conditions = []

    if status:
        conditions.append("status = ?")
        params.append(status)
    else:
        conditions.append("status IN ('pending', 'active', 'snoozed')")

    if upcoming_hours is not None:
        cutoff = (datetime.now(timezone.utc) + timedelta(hours=upcoming_hours)).isoformat()
        conditions.append("when_due <= ?")
        params.append(cutoff)

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    # Sort: upcoming-soonest first, then past-due with the most recent first.
    # "Old past-due at the top" was the reported UX smell — pure ASC buries
    # the useful "what's next" under years-stale items.
    sql += (
        " ORDER BY"
        " CASE WHEN when_due >= datetime('now') THEN 0 ELSE 1 END ASC,"
        " CASE WHEN when_due >= datetime('now') THEN when_due END ASC,"
        " CASE WHEN when_due < datetime('now') THEN when_due END DESC"
    )

    rows = db.fetchall(sql, tuple(params))
    reminders = [dict(r) for r in rows]

    # Parse JSON fields
    for r in reminders:
        if isinstance(r.get("context"), str):
            r["context"] = json.loads(r["context"])
        if isinstance(r.get("repeat"), str):
            r["repeat"] = json.loads(r["repeat"])

    result: dict = {"reminders": reminders}

    if include_suggestions:
        sug_rows = db.fetchall(
            "SELECT * FROM reminder_suggestions WHERE accepted IS NULL ORDER BY created_at DESC"
        )
        result["suggestions"] = [dict(r) for r in sug_rows]

    return result


def snooze_reminder(
    reminder_id: str,
    duration_min: Optional[int] = None,
) -> dict:
    """Snooze a reminder — regenerates cascade."""
    from okuro.db import get_db

    db = get_db()
    strategy = _load_strategy()
    snooze_min = duration_min if duration_min is not None else strategy.snooze_default_min
    new_due = datetime.now(timezone.utc) + timedelta(minutes=snooze_min)

    row = db.fetchone(
        "SELECT snooze_count, urgency FROM reminders WHERE id = ?",
        (reminder_id,),
    )
    snooze_count = (row["snooze_count"] or 0) + 1 if row else 1
    urgency = row["urgency"] if row else 3

    db.execute(
        """UPDATE reminders SET status = 'snoozed', snooze_count = ?,
           when_due = ?, updated_at = datetime('now') WHERE id = ?""",
        (snooze_count, new_due.isoformat(), reminder_id),
    )
    db.conn.commit()

    steps = generate_cascade(
        reminder_id=reminder_id,
        when_due=new_due,
        urgency=urgency,
        strategy=strategy,
    )

    log.info("Snoozed reminder %s for %d min (%d cascade steps)",
             reminder_id, snooze_min, len(steps))
    return {"id": reminder_id, "status": "snoozed", "when_due": new_due.isoformat(),
            "snooze_count": snooze_count}


def dismiss_reminder(reminder_id: str) -> dict:
    """Dismiss a reminder — skips remaining cascade."""
    from okuro.db import get_db

    db = get_db()
    db.execute(
        "UPDATE reminders SET status = 'dismissed', updated_at = datetime('now') WHERE id = ?",
        (reminder_id,),
    )
    db.execute(
        "UPDATE reminder_cascade SET status = 'skipped' WHERE reminder_id = ? AND status = 'pending'",
        (reminder_id,),
    )
    db.conn.commit()
    log.info("Dismissed reminder %s", reminder_id)
    return {"id": reminder_id, "status": "dismissed"}


def acknowledge_reminder(reminder_id: str) -> dict:
    """Acknowledge a reminder — marks done, skips remaining cascade."""
    from okuro.db import get_db

    db = get_db()
    now = datetime.now(timezone.utc).isoformat()

    db.execute(
        "UPDATE reminders SET status = 'done', updated_at = ? WHERE id = ?",
        (now, reminder_id),
    )

    # Mark most recent sent step as acknowledged
    sent_step = db.fetchone(
        """SELECT id FROM reminder_cascade
           WHERE reminder_id = ? AND status = 'sent'
           ORDER BY step DESC LIMIT 1""",
        (reminder_id,),
    )
    if sent_step:
        db.execute(
            "UPDATE reminder_cascade SET status = 'acknowledged', acknowledged_at = ? WHERE id = ?",
            (now, sent_step["id"]),
        )

    # Skip remaining pending
    db.execute(
        "UPDATE reminder_cascade SET status = 'skipped' WHERE reminder_id = ? AND status = 'pending'",
        (reminder_id,),
    )
    db.conn.commit()
    log.info("Acknowledged reminder %s", reminder_id)
    return {"id": reminder_id, "status": "done"}


# ============================================================
# Eval loop — called by timer
# ============================================================

def evaluate_due() -> dict:
    """Process all due cascade steps.

    Called periodically:
    1. Query due cascade steps
    2. Dispatch each through channel registry
    3. Update step status
    """
    from okuro.db import get_db

    db = get_db()
    sent = 0
    failed = 0

    now = datetime.now(timezone.utc).isoformat()
    lookahead = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()

    # Get due steps
    rows = db.fetchall(
        """SELECT rc.id as cascade_id, rc.reminder_id, rc.channels, rc.step,
                  r.what, r.context, r.urgency
           FROM reminder_cascade rc
           JOIN reminders r ON r.id = rc.reminder_id
           WHERE rc.status = 'pending' AND rc.fire_at <= ?
           ORDER BY rc.fire_at ASC""",
        (lookahead,),
    )

    if not rows:
        return {"sent": 0, "failed": 0}

    log.info("Processing %d due cascade steps", len(rows))
    registry = _get_channel_registry()

    for step in rows:
        cascade_id = step["cascade_id"]
        channels = json.loads(step["channels"]) if isinstance(step["channels"], str) else step["channels"]
        context = json.loads(step["context"]) if isinstance(step.get("context"), str) else (step.get("context") or {})

        message = Message(
            title=step.get("what", "Reminder"),
            body=step.get("what", ""),
            urgency=step.get("urgency", 3),
            format="brief",
            source="reminder",
            source_id=step.get("reminder_id"),
            context=context,
        )

        results = registry.send(channels, message)
        any_sent = any(r.status == DeliveryStatus.SENT or r.status == DeliveryStatus.ACKNOWLEDGED for r in results)

        if any_sent:
            db.execute(
                "UPDATE reminder_cascade SET status = 'sent', sent_at = ? WHERE id = ?",
                (now, cascade_id),
            )
            # Activate the parent reminder
            db.execute(
                "UPDATE reminders SET status = 'active', updated_at = ? WHERE id = ? AND status = 'pending'",
                (now, step["reminder_id"]),
            )
            sent += 1
        else:
            failed += 1
            log.warning("All channels failed for step %s", cascade_id)

    db.conn.commit()

    # Escalate unacknowledged reminders
    try:
        escalated = _handle_escalation()
        if escalated:
            log.info("Escalated %d reminders", escalated)
    except Exception as e:
        log.warning("Escalation failed: %s", e)
        escalated = 0

    summary = {"sent": sent, "failed": failed, "escalated": escalated}
    log.info("Eval complete: %s", summary)
    return summary


def _handle_escalation() -> int:
    """Bump urgency on reminders with unacknowledged sent steps past escalation interval."""
    from okuro.db import get_db

    strategy = _load_strategy()
    if strategy.escalation_interval_min <= 0:
        return 0

    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=strategy.escalation_interval_min)).isoformat()

    stale_steps = db.fetchall(
        """SELECT DISTINCT reminder_id FROM reminder_cascade
           WHERE status = 'sent' AND sent_at < ? AND acknowledged_at IS NULL""",
        (cutoff,),
    )

    if not stale_steps:
        return 0

    escalated = 0
    for step in stale_steps:
        rid = step["reminder_id"]
        row = db.fetchone(
            "SELECT urgency, status FROM reminders WHERE id = ?", (rid,)
        )
        if not row or row["status"] not in ("pending", "active", "snoozed"):
            continue
        if row["urgency"] >= 5:
            continue

        new_urgency = min(5, row["urgency"] + 1)
        db.execute(
            "UPDATE reminders SET urgency = ?, updated_at = datetime('now') WHERE id = ?",
            (new_urgency, rid),
        )
        log.info("Escalated reminder %s: urgency %d → %d", rid, row["urgency"], new_urgency)
        escalated += 1

    db.conn.commit()
    return escalated
