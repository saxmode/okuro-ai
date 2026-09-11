# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Suggestion engine — proposes reminders from stale progress and aging thoughts.
# index:
#   imports
#   def _load_strategy
#   def _suggestion_exists
#   def _extract_actionable_line
#   def generate_suggestions
#   def accept_suggestion
#   def reject_suggestion
# AGENT_HEADER_END -->
"""Suggestion engine — proposes reminders from stale progress and aging thoughts.

Runs periodically to surface forgotten work. Users accept or reject
suggestions via MCP tools.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .strategy import BehavioralStrategy, compute_strategy

log = logging.getLogger("okuro.reminders")

# Thought categories that are plausibly actionable — the subset where
# "become a reminder" is a reasonable next state. `note` and `observation`
# are raw capture and have a 0% historical acceptance rate.
# Add a new category here to surface it as a reminder candidate.
_ACTIONABLE_CATEGORIES: tuple[str, ...] = ("idea", "question", "decision", "todo")


def _load_strategy() -> BehavioralStrategy:
    """Load user profile and compute strategy."""
    try:
        from okuro.yu.profile import get_profile_raw
        profile = get_profile_raw()
        return compute_strategy(profile)
    except Exception:
        return compute_strategy({})


def _suggestion_exists(db, source_type: str, source_id: str) -> bool:
    """Check if a suggestion for this source is pending OR already rejected.

    Rejection is sticky: once the user rejects a suggestion we do not
    re-propose it on later scan cycles. A previously accepted suggestion
    (accepted = 1) does NOT block — the reminder has been created, so
    regeneration is fine if the source resurfaces as stale again.
    """
    row = db.fetchone(
        "SELECT 1 FROM reminder_suggestions "
        "WHERE source_type = ? AND source_id = ? "
        "AND (accepted IS NULL OR accepted = 0)",
        (source_type, source_id),
    )
    return row is not None


def _extract_actionable_line(raw: str) -> str:
    """Extract the first meaningful, actionable line from raw thought content."""
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("- [x]"):
            continue
        if line.startswith("##"):
            continue
        if line == "---":
            continue
        if line.startswith("- [ ] "):
            line = line[6:]
        elif line.startswith("- "):
            line = line[2:]
        if len(line) > 10:
            return line[:80]
    return raw[:80]


def generate_suggestions() -> list[dict]:
    """Scan stale progress and aging thoughts, create suggestion records."""
    from okuro.db import get_db

    db = get_db()
    strategy = _load_strategy()
    created = []

    # --- Stale progress ---
    try:
        stale_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=strategy.stale_project_days)
        ).strftime("%Y-%m-%d %H:%M:%S")

        rows = db.fetchall(
            """SELECT project, status, summary, updated_at FROM progress
               WHERE updated_at < ? AND status != 'blocked'
               ORDER BY updated_at ASC LIMIT 20""",
            (stale_cutoff,),
        )

        for r in rows:
            project_slug = r["project"]
            if _suggestion_exists(db, "progress", project_slug):
                continue

            updated_at = r.get("updated_at", "")
            days_ago = 0
            try:
                then = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                days_ago = (datetime.now(timezone.utc) - then).days
            except Exception:
                pass

            status = r.get("status", "unknown")
            summary = (r.get("summary") or "").strip()
            if summary:
                proposed = f"{project_slug}: pick up from '{summary[:60]}'. Was {status}."
            else:
                proposed = f"{project_slug}: stale for {days_ago}d (status: {status}). Review."

            sug_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO reminder_suggestions (id, source_type, source_id,
                   proposed_what, proposed_urgency, reason)
                   VALUES (?, 'progress', ?, ?, ?, ?)""",
                (sug_id, project_slug, proposed,
                 2 if days_ago < strategy.stale_project_days * 2 else 3,
                 f"No activity for {days_ago}d. Last: {status} — {summary[:120]}"),
            )
            created.append({"id": sug_id, "source_type": "progress", "proposed_what": proposed})

    except Exception as e:
        log.error("Stale progress scan failed: %s", e)

    # --- Aging thoughts ---
    try:
        thought_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=strategy.stale_thought_days)
        ).strftime("%Y-%m-%d %H:%M:%S")

        category_placeholders = ",".join("?" * len(_ACTIONABLE_CATEGORIES))
        rows = db.fetchall(
            f"""SELECT id, content, project, created_at FROM thoughts
               WHERE created_at < ? AND status = 'open'
                 AND json_extract(metadata, '$.category') IN ({category_placeholders})
               ORDER BY created_at ASC LIMIT 20""",
            (thought_cutoff, *_ACTIONABLE_CATEGORIES),
        )

        if not rows:
            log.debug(
                "Aging-thoughts scan: no candidates "
                "(cutoff=%s, categories=%s)",
                thought_cutoff,
                _ACTIONABLE_CATEGORIES,
            )

        for r in rows:
            thought_id = str(r["id"])
            if _suggestion_exists(db, "thought", thought_id):
                continue

            raw = (r.get("content") or "").strip()
            first_line = _extract_actionable_line(raw)
            days_ago = 0
            try:
                then = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
                days_ago = (datetime.now(timezone.utc) - then).days
            except Exception:
                pass

            project = r.get("project")
            prefix = f"{project}: " if project else ""
            proposed = f"{prefix}{first_line}"

            sug_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO reminder_suggestions (id, source_type, source_id,
                   proposed_what, proposed_urgency, reason)
                   VALUES (?, 'thought', ?, ?, 2, ?)""",
                (sug_id, thought_id, proposed, raw[:150]),
            )
            created.append({"id": sug_id, "source_type": "thought", "proposed_what": proposed})

    except Exception as e:
        log.error("Aging thoughts scan failed: %s", e)

    if created:
        db.conn.commit()
        log.info("Generated %d new suggestions", len(created))
    return created


def accept_suggestion(
    suggestion_id: str,
    override_when: Optional[str | datetime] = None,
    override_urgency: Optional[int] = None,
) -> dict:
    """Accept a suggestion — creates a reminder and links it back."""
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        "SELECT * FROM reminder_suggestions WHERE id = ?",
        (suggestion_id,),
    )
    if not row:
        raise ValueError(f"Suggestion {suggestion_id} not found")

    # Determine when_due
    if override_when:
        if isinstance(override_when, datetime):
            if override_when.tzinfo is None:
                override_when = override_when.replace(tzinfo=timezone.utc)
            when_due = override_when.isoformat()
        else:
            when_due = override_when
    elif row.get("proposed_when"):
        when_due = row["proposed_when"]
    else:
        tomorrow = datetime.now(timezone.utc).replace(
            hour=9, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        when_due = tomorrow.isoformat()

    urgency = override_urgency or row.get("proposed_urgency", 3)

    from .engine import create_reminder

    reminder = create_reminder(
        what=row["proposed_what"],
        when_due=when_due,
        urgency=urgency,
        source="agent",
        context={"suggestion_id": suggestion_id, "source_type": row.get("source_type")},
    )

    db.execute(
        "UPDATE reminder_suggestions SET accepted = 1, reminder_id = ? WHERE id = ?",
        (reminder["id"], suggestion_id),
    )
    db.conn.commit()
    log.info("Accepted suggestion %s -> reminder %s", suggestion_id, reminder["id"])
    return reminder


def reject_suggestion(suggestion_id: str) -> dict:
    """Reject a suggestion — marks accepted=0."""
    from okuro.db import get_db

    db = get_db()
    db.execute(
        "UPDATE reminder_suggestions SET accepted = 0 WHERE id = ?",
        (suggestion_id,),
    )
    db.conn.commit()
    log.info("Rejected suggestion %s", suggestion_id)
    return {"id": suggestion_id, "accepted": False}
