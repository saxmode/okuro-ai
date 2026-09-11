# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Durable resonance interview_session — the resumable wrapper over the
#   (stateless) interview + ingest + gap engine. Owns the goal, the growing
#   evidence-artifact set, the turn log, and the cached completeness/questions so
#   an interview can be paused, listed, and resumed. Sits on migration 076.
# index:
#   def session_create / session_get / session_list
#   def _add_evidence
#   def session_next / session_answer / session_ingest / session_close
# AGENT_HEADER_END -->
"""Durable interview session — makes Resonance's enrich⇄analyze loop resumable.

The loop itself lives in :mod:`resonance.interview` (stateless functions over an
artifact-id set). This module persists that set: a session row owns the goal +
``artifact_ids`` + the last-computed ``completeness``/``open_questions``, and a
turn log records each Q/A. ``session_next`` and ``session_answer`` wrap the
stateless functions and write the result back, so a caller only threads a
``session_id`` — not the whole artifact list — and can resume after a crash.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional


def _gen_id() -> str:
    return f"rs_{uuid.uuid4().hex[:12]}"


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["artifact_ids"] = json.loads(d.get("artifact_ids") or "[]")
    d["open_questions"] = json.loads(d.get("open_questions") or "[]")
    d["ready"] = bool(d.get("ready"))
    return d


def session_create(
    goal: str,
    *,
    project: Optional[str] = None,
    person_id: Optional[str] = None,
    brand_id: Optional[str] = None,
    source_artifact_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Open a new interview session. Seed with any already-ingested doc ids."""
    goal = (goal or "").strip()
    if not goal:
        raise ValueError("goal required")
    from okuro.db import get_db

    db = get_db()
    sid = _gen_id()
    db.execute(
        """INSERT INTO resonance_sessions (id, goal, project, person_id, brand_id, artifact_ids)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (sid, goal, project, person_id, brand_id, json.dumps(source_artifact_ids or [])),
    )
    db.conn.commit()
    return session_get(sid)  # type: ignore[return-value]


def session_get(session_id: str) -> Optional[dict[str, Any]]:
    """Full session incl. the turn log (chronological)."""
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT * FROM resonance_sessions WHERE id = ?", (session_id,))
    if not row:
        return None
    out = _row(row)
    # rowid = true insertion order — created_at has 1s granularity, so same-second
    # turns would otherwise tie and sort by the (non-chronological) uuid id.
    turns = db.fetchall(
        "SELECT kind, question, answer, artifact_id, created_at FROM "
        "resonance_session_turns WHERE session_id = ? ORDER BY created_at, rowid",
        (session_id,),
    )
    out["turns"] = [dict(t) for t in turns]
    return out


def session_list(*, status: Optional[str] = None, project: Optional[str] = None,
                 limit: int = 50) -> list[dict[str, Any]]:
    """List sessions (newest first), optionally filtered by status/project."""
    from okuro.db import get_db

    db = get_db()
    where, params = [], []
    if status:
        where.append("status = ?"); params.append(status)
    if project:
        where.append("project = ?"); params.append(project)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = db.fetchall(
        f"SELECT * FROM resonance_sessions{clause} ORDER BY updated_at DESC LIMIT ?",
        (*params, int(limit)),
    )
    return [_row(r) for r in rows]


def _add_evidence(db, session_id: str, artifact_id: str, *, kind: str,
                  question: Optional[str] = None, answer: Optional[str] = None) -> None:
    """Append an artifact id to the session set + log a turn. Caller commits."""
    row = db.fetchone("SELECT artifact_ids FROM resonance_sessions WHERE id = ?", (session_id,))
    ids = json.loads(row["artifact_ids"] or "[]") if row else []
    if artifact_id and artifact_id not in ids:
        ids.append(artifact_id)
    db.execute(
        "UPDATE resonance_sessions SET artifact_ids = ?, updated_at = datetime('now') WHERE id = ?",
        (json.dumps(ids), session_id),
    )
    db.execute(
        """INSERT INTO resonance_session_turns (id, session_id, kind, question, answer, artifact_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (_gen_id(), session_id, kind, question, answer, artifact_id),
    )


def _persist_analysis(db, session_id: str, result: dict[str, Any]) -> None:
    """Cache the last gap analysis onto the session (completeness/ready/questions)."""
    ready = bool(result.get("ready"))
    db.execute(
        """UPDATE resonance_sessions SET completeness = ?, ready = ?, open_questions = ?,
           status = CASE WHEN status = 'closed' THEN status
                         WHEN ? THEN 'ready' ELSE 'open' END,
           updated_at = datetime('now') WHERE id = ?""",
        (result.get("completeness"), 1 if ready else 0,
         json.dumps(result.get("questions") or []), 1 if ready else 0, session_id),
    )


def session_next(session_id: str, *, provider: Optional[str] = None,
                 max_q: int = 3) -> dict[str, Any]:
    """Compute + persist the next interview questions for a stored session."""
    from okuro.db import get_db
    from .interview import next_questions

    db = get_db()
    s = session_get(session_id)
    if not s:
        raise ValueError(f"session not found: {session_id}")
    result = next_questions(s["goal"], source_artifact_ids=s["artifact_ids"],
                            project=s["project"], provider=provider, max_q=max_q)
    _persist_analysis(db, session_id, result)
    db.conn.commit()
    return {"session_id": session_id, **result}


def session_answer(session_id: str, question: str, answer: str, *,
                   provider: Optional[str] = None) -> dict[str, Any]:
    """Record one Q&A: ingest as evidence, attach to the session, recompute."""
    from okuro.db import get_db
    from .interview import record_answer

    db = get_db()
    s = session_get(session_id)
    if not s:
        raise ValueError(f"session not found: {session_id}")
    man = record_answer(s["goal"], question, answer, project=s["project"], provider=provider)
    _add_evidence(db, session_id, man["artifact_id"], kind="answer",
                  question=question, answer=answer)
    db.conn.commit()
    return {"session_id": session_id, "artifact_id": man["artifact_id"],
            "claims_added": man.get("claims_added", 0),
            **session_next(session_id, provider=provider)}


def session_ingest(session_id: str, artifact_id: str, *, kind: str = "doc",
                   question: Optional[str] = None) -> dict[str, Any]:
    """Attach an already-ingested artifact (doc / research finding) to a session."""
    from okuro.db import get_db

    db = get_db()
    if not db.fetchone("SELECT id FROM resonance_sessions WHERE id = ?", (session_id,)):
        raise ValueError(f"session not found: {session_id}")
    _add_evidence(db, session_id, artifact_id, kind=kind, question=question)
    db.conn.commit()
    return session_get(session_id)  # type: ignore[return-value]


def session_close(session_id: str) -> dict[str, Any]:
    """Mark a session closed (its PCO/deck has been built)."""
    from okuro.db import get_db

    db = get_db()
    db.execute(
        "UPDATE resonance_sessions SET status = 'closed', updated_at = datetime('now') WHERE id = ?",
        (session_id,),
    )
    db.conn.commit()
    return session_get(session_id)  # type: ignore[return-value]
