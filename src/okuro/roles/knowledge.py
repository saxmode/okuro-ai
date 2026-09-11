# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Knowledge store: read/write role knowledge with DB + vector search.
# index:
#   imports
#   def _row_to_entry
#   def _touch_accessed
#   def read_knowledge
#   def write_knowledge
#   def get_knowledge_stats
#   def increment_sessions
#   def _check_auto_mature
#   def is_stale
# AGENT_HEADER_END -->
"""Knowledge store: read/write role knowledge with DB + vector search."""

import json
import re
import uuid
from datetime import datetime, timezone

from okuro.db import get_db
from okuro.embed import embed_one
from okuro.embed.client import embed_query, to_bytes
from okuro.sense.retrieval import candidate_pool, scoped_vec_search, vec_write


_COLS = "id, content, type, created_at, source_url, session_id, confidence"


def _row_to_entry(row, similarity=None) -> dict:
    return {
        "id": row["id"],
        "content": row["content"],
        "type": row["type"],
        "similarity": similarity,
        "created_at": row["created_at"],
        "source_url": row["source_url"],
        "session_id": row["session_id"],
        "confidence": row["confidence"],
    }


def _touch_accessed(db, ids: list[str]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    db.execute(
        f"UPDATE role_knowledge SET last_accessed = datetime('now') "
        f"WHERE id IN ({placeholders})",
        tuple(ids),
    )


def read_knowledge(
    role_id: str,
    task_hint: str | None = None,
    limit: int = 5,
    min_confidence: float = 0.1,
) -> list[dict]:
    """Read relevant knowledge entries for a role.

    If task_hint is provided, uses semantic search via vec_knowledge.
    Otherwise, returns most recent entries. Entries with confidence <=
    min_confidence are filtered out (allows soft-deleted/superseded rows
    to stay in the table without polluting retrieval).
    """
    db = get_db()

    if task_hint:
        embedding = embed_query(task_hint)
        emb_bytes = to_bytes(embedding)

        # Scoped by role_id in the per-row WHERE below, so the pool has to
        # outrun every OTHER role in the index. `limit * 2` could not.
        vec_results = scoped_vec_search(
            db, "vec_knowledge", emb_bytes,
            limit=candidate_pool(limit, scoped=True),
            partition=("role_id", role_id),
        )

        if not vec_results:
            return []

        results = []
        for vr in vec_results:
            row = db.fetchone(
                f"SELECT {_COLS} FROM role_knowledge "
                "WHERE id = ? AND role_id = ? AND confidence > ?",
                (vr["id"], role_id, min_confidence),
            )
            if row:
                results.append(_row_to_entry(row, round(1.0 - vr["distance"], 4)))
            if len(results) >= limit:
                break

        _touch_accessed(db, [r["id"] for r in results])
        return results
    else:
        rows = db.fetchall(
            f"SELECT {_COLS} FROM role_knowledge "
            "WHERE role_id = ? AND confidence > ? "
            "ORDER BY created_at DESC LIMIT ?",
            (role_id, min_confidence, limit),
        )
        entries = [_row_to_entry(r) for r in rows]
        _touch_accessed(db, [e["id"] for e in entries])
        return entries


MAINTENANCE_TYPES = {"research", "source"}


def write_knowledge(
    role_id: str,
    learning: str,
    type: str = "research",
    source_url: str | None = None,
    session_id: str | None = None,
    supersedes: str | None = None,
    confidence: float = 0.7,
) -> dict:
    """Write learning to DB with embedding for semantic search.

    Types: insight, pitfall, source, decision, research

    Only `research` and `source` writes bump the role's last_maintained
    timestamp — opportunistic insights/pitfalls/decisions during normal
    work should not reset the weekly-research clock (decision: rule B).

    `supersedes` soft-deletes the superseded row by setting its confidence
    to 0.0 — it stays in the table (for audit / FK integrity) but drops
    below the default read_knowledge confidence floor.
    """
    valid_types = {"insight", "pitfall", "source", "decision", "research"}
    if type not in valid_types:
        type = "research"

    if session_id is None:
        try:
            from okuro.sense.session_state import current_audit_session_id
            session_id = current_audit_session_id()
        except Exception:
            pass

    record_id = str(uuid.uuid4())

    db = get_db()
    db.execute(
        "INSERT INTO role_knowledge "
        "(id, role_id, content, type, source_url, session_id, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (record_id, role_id, learning, type, source_url, session_id, confidence),
    )

    if supersedes:
        db.execute(
            "UPDATE role_knowledge SET confidence = 0.0 WHERE id = ?",
            (supersedes,),
        )

    embedding = embed_one(learning)
    emb_bytes = to_bytes(embedding)
    vec_write(
        db, "vec_knowledge", record_id, emb_bytes,
        partition=("role_id", role_id),
    )

    db.execute(
        "UPDATE roles SET learnings = learnings + 1 WHERE role_id = ?",
        (role_id,),
    )

    _check_auto_mature(role_id)

    # Clock-bump rule B: only research-cycle outputs reset staleness.
    if type in MAINTENANCE_TYPES:
        from .maintainer import update_maintained
        update_maintained(role_id)

    return {
        "id": record_id,
        "role_id": role_id,
        "type": type,
        "status": "written",
        "supersedes": supersedes,
        "bumped_last_maintained": type in MAINTENANCE_TYPES,
    }


def get_knowledge_stats(role_id: str) -> dict:
    """Get knowledge statistics for a role."""
    db = get_db()

    row = db.fetchone(
        "SELECT COUNT(*) as total, MAX(created_at) as last_updated "
        "FROM role_knowledge WHERE role_id = ?",
        (role_id,),
    )
    total = row["total"] if row else 0
    last_updated = row["last_updated"] if row else None

    type_rows = db.fetchall(
        "SELECT type, COUNT(*) as cnt FROM role_knowledge "
        "WHERE role_id = ? GROUP BY type",
        (role_id,),
    )
    by_type = {r["type"]: r["cnt"] for r in type_rows}

    meta = db.fetchone(
        "SELECT maturity, sessions, learnings FROM roles WHERE role_id = ?",
        (role_id,),
    )

    return {
        "role_id": role_id,
        "total_entries": total,
        "by_type": by_type,
        "last_updated": last_updated,
        "maturity": meta["maturity"] if meta else None,
        "sessions": meta["sessions"] if meta else 0,
        "learnings": meta["learnings"] if meta else 0,
    }


def increment_sessions(role_id: str):
    """Increment session count for a role (called on roles_get)."""
    db = get_db()
    db.execute(
        "UPDATE roles SET sessions = sessions + 1, "
        "updated_at = datetime('now') WHERE role_id = ?",
        (role_id,),
    )
    _check_auto_mature(role_id)


def _check_auto_mature(role_id: str):
    """Auto-promote active → mature when sessions >= 10 AND learnings >= 20."""
    db = get_db()
    row = db.fetchone(
        "SELECT maturity, sessions, learnings FROM roles WHERE role_id = ?",
        (role_id,),
    )
    if not row or row["maturity"] != "active":
        return
    if row["sessions"] >= 10 and row["learnings"] >= 20:
        db.execute(
            "UPDATE roles SET maturity = 'mature' WHERE role_id = ?",
            (role_id,),
        )


def is_stale(role_id: str) -> bool:
    """Check if a role's knowledge is stale based on maintenance schedule."""
    db = get_db()
    row = db.fetchone(
        "SELECT maintenance_schedule, last_maintained FROM roles WHERE role_id = ?",
        (role_id,),
    )
    if not row:
        return True

    schedule = row["maintenance_schedule"] or "monthly"
    last_maintained = row["last_maintained"]

    if not last_maintained:
        return True

    now = datetime.now(timezone.utc)
    try:
        last_dt = datetime.fromisoformat(last_maintained)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True

    days = (now - last_dt).days

    schedule_days = {"weekly": 7, "biweekly": 14, "monthly": 30}
    threshold = schedule_days.get(schedule, 30)

    return days > threshold
