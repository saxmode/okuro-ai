# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Cascade generation — materializes BehavioralStrategy into DB rows.
# index: imports | def generate_cascade | def _clear_cascade
# AGENT_HEADER_END -->
"""Cascade generation — materializes BehavioralStrategy into DB rows.

Each reminder gets a set of (fire_at, channels, step) rows consumed by the eval loop.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from .strategy import BehavioralStrategy

log = logging.getLogger("okuro.reminders")


def generate_cascade(
    reminder_id: str,
    when_due: datetime,
    urgency: int,
    strategy: BehavioralStrategy,
    explicit_channels: list[str] | None = None,
) -> list[dict]:
    """Materialize cascade steps into reminder_cascade table.

    Args:
        reminder_id: UUID of the parent reminder.
        when_due: When the reminder is due (timezone-aware UTC).
        urgency: 1-5 urgency level.
        strategy: Resolved behavioral strategy.
        explicit_channels: If provided, override channel selection for all steps.

    Returns:
        List of created cascade step dicts.
    """
    from okuro.db import get_db

    db = get_db()

    # Clear existing pending cascade for this reminder (e.g. after snooze)
    _clear_cascade(db, reminder_id)

    now = datetime.now(timezone.utc)
    rows = []

    def _add_step(step_num: int, fire_at: datetime, channels: list[str]) -> None:
        step_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO reminder_cascade (id, reminder_id, step, fire_at, channels, status)
               VALUES (?, ?, ?, ?, ?, 'pending')""",
            (step_id, reminder_id, step_num, fire_at.isoformat(), json.dumps(channels)),
        )
        rows.append({
            "id": step_id,
            "reminder_id": reminder_id,
            "step": step_num,
            "fire_at": fire_at.isoformat(),
            "channels": channels,
            "status": "pending",
        })

    # Emit the lead-up nudges (typically all negative offsets = "before due").
    # Track whether any emitted step actually delivers at/after the due time.
    step_num = 0
    covers_due = False
    for pattern_step in strategy.cascade_pattern:
        step_num += 1
        fire_at = when_due + timedelta(minutes=pattern_step.offset_minutes)
        if fire_at <= now:
            continue
        channels = explicit_channels if explicit_channels else pattern_step.channels
        _add_step(step_num, fire_at, channels)
        if pattern_step.offset_minutes >= 0:
            covers_due = True

    # Guarantee a delivery AT the due time. The cascade pattern carries only
    # lead-up nudges, so without this a reminder never fires at the requested
    # moment — and one due within the final nudge window would generate zero
    # steps and be silently lost. Fire at the due time, or immediately if the
    # due time is already past (reminder set for "now" or backdated).
    if not covers_due:
        step_num += 1
        due_fire_at = when_due if when_due > now else now
        due_channels = explicit_channels or strategy.channel_map.get(urgency) or ["desktop"]
        _add_step(step_num, due_fire_at, due_channels)

    db.conn.commit()

    if not rows:
        log.warning("No cascade steps generated for reminder %s", reminder_id)
    else:
        log.info("Generated %d cascade steps for reminder %s", len(rows), reminder_id)

    return rows


def _clear_cascade(db, reminder_id: str) -> None:
    """Delete all pending cascade steps for a reminder."""
    db.execute(
        "DELETE FROM reminder_cascade WHERE reminder_id = ? AND status = 'pending'",
        (reminder_id,),
    )
