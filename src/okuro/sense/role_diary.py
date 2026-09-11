# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Per-role/per-agent diary streams (mempalace agent-diary pattern).
# index: imports | def role_diary_write | def role_diary_read | def role_diary_search | def role_diary_stats
# AGENT_HEADER_END -->
"""Per-role/per-agent diary streams.

Distinct from role_knowledge (curated, deduped, type-checked).
Diary entries are append-only chronological narrative — what happened in
this session, what was observed, recurring patterns. Either ``role_id``
or ``agent`` (or both) keys the stream.

Schema: ``033_role_diary.sql`` (role_diary_entries + vec_role_diary).
"""

from __future__ import annotations

import logging
import uuid
from okuro.sense.retrieval import candidate_pool

log = logging.getLogger(__name__)

_EMBED_CHAR_CAP = 4000


def role_diary_write(entry: str, role_id: str | None = None,
                     agent: str | None = None, project: str | None = None,
                     topic: str | None = None,
                     session_id: str | None = None,
                     embed: bool = True) -> str:
    """Append an entry to a role/agent diary.

    Either ``role_id`` or ``agent`` MUST be set (otherwise the entry has no
    stream key and never resurfaces).
    """
    if not (role_id or agent):
        return "REJECTED: at least one of role_id / agent must be set"
    if not entry or not entry.strip():
        return "REJECTED: entry is empty"

    from okuro.db import get_db
    db = get_db()

    if project:
        from okuro.sense.progress import _ensure_project
        _ensure_project(db, project)

    eid = str(uuid.uuid4())
    vec_bytes = None
    if embed:
        try:
            from okuro.embed.client import embed_one, to_bytes
            vec_bytes = to_bytes(embed_one(entry[:_EMBED_CHAR_CAP]))
        except Exception:
            vec_bytes = None

    with db.write():
        db.execute(
            """INSERT INTO role_diary_entries
               (id, role_id, agent, project, topic, entry, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (eid, role_id, agent, project, topic, entry, session_id),
        )
        if vec_bytes is not None:
            try:
                db.execute(
                    "INSERT INTO vec_role_diary (id, embedding) VALUES (?, ?)",
                    (eid, vec_bytes),
                )
            except Exception:
                pass  # vector failure must not block storage

    key = f"role={role_id}" if role_id else f"agent={agent}"
    proj = f", project={project}" if project else ""
    return f"Diary entry stored ({key}{proj}, id={eid[:12]})"


def role_diary_read(role_id: str | None = None, agent: str | None = None,
                    project: str | None = None, topic: str | None = None,
                    last_n: int = 10) -> list[dict]:
    """Return the last N diary entries for the matching stream.

    At least one of role_id / agent / project must be provided to scope
    the read; otherwise the call returns the system-wide tail (use sparingly).
    """
    from okuro.db import get_db
    db = get_db()

    where = ["1=1"]
    params: list = []
    if role_id:
        where.append("role_id = ?")
        params.append(role_id)
    if agent:
        where.append("agent = ?")
        params.append(agent)
    if project:
        where.append("(project = ? OR project IS NULL)")
        params.append(project)
    if topic:
        where.append("topic = ?")
        params.append(topic)

    sql = f"""SELECT id, role_id, agent, project, topic, entry, session_id, created_at
              FROM role_diary_entries
              WHERE {' AND '.join(where)}
              ORDER BY created_at DESC
              LIMIT ?"""
    rows = db.fetchall(sql, tuple(params + [last_n]))
    return [dict(r) for r in rows]


def role_diary_search(query: str, role_id: str | None = None,
                      agent: str | None = None, project: str | None = None,
                      limit: int = 10) -> list[dict]:
    """Semantic search over diary entries with optional stream filters."""
    from okuro.db import get_db
    db = get_db()

    base_where = ["1=1"]
    base_params: list = []
    if role_id:
        base_where.append("role_id = ?")
        base_params.append(role_id)
    if agent:
        base_where.append("agent = ?")
        base_params.append(agent)
    if project:
        base_where.append("(project = ? OR project IS NULL)")
        base_params.append(project)

    where_sql = " AND ".join(base_where)

    try:
        from okuro.embed.client import embed_query, to_bytes
        vec_bytes = to_bytes(embed_query(query))
        matches = db.vec_search(
            "vec_role_diary", vec_bytes,
            limit=candidate_pool(limit, scoped=True),
        )
    except Exception:
        matches = []

    if matches:
        ids = [m["id"] for m in matches]
        distances = {m["id"]: m["distance"] for m in matches}
        placeholders = ",".join("?" * len(ids))
        rows = db.fetchall(
            f"""SELECT id, role_id, agent, project, topic, entry, session_id,
                       created_at
                FROM role_diary_entries
                WHERE id IN ({placeholders}) AND {where_sql}""",
            tuple(ids) + tuple(base_params),
        )
        ranked = sorted(
            (dict(r) for r in rows),
            key=lambda r: distances.get(r["id"], 1.0),
        )[:limit]
        for r in ranked:
            r["similarity"] = 1.0 - distances.get(r["id"], 1.0)
        return ranked

    rows = db.fetchall(
        f"""SELECT id, role_id, agent, project, topic, entry, session_id, created_at
            FROM role_diary_entries
            WHERE {where_sql}
            ORDER BY created_at DESC
            LIMIT ?""",
        tuple(base_params) + (limit,),
    )
    return [dict(r) for r in rows]


def role_diary_stats(role_id: str | None = None,
                     agent: str | None = None) -> dict:
    """Stream-level summary: entry count, distinct sessions, project mix."""
    from okuro.db import get_db
    db = get_db()
    where = ["1=1"]
    params: list = []
    if role_id:
        where.append("role_id = ?")
        params.append(role_id)
    if agent:
        where.append("agent = ?")
        params.append(agent)
    where_sql = " AND ".join(where)
    total = db.fetchone(
        f"SELECT COUNT(*) AS c FROM role_diary_entries WHERE {where_sql}",
        tuple(params),
    )["c"]
    sessions = db.fetchone(
        f"""SELECT COUNT(DISTINCT session_id) AS c FROM role_diary_entries
            WHERE {where_sql} AND session_id IS NOT NULL""",
        tuple(params),
    )["c"]
    projects = db.fetchall(
        f"""SELECT COALESCE(project, 'system') AS project, COUNT(*) AS c
            FROM role_diary_entries
            WHERE {where_sql}
            GROUP BY project ORDER BY c DESC""",
        tuple(params),
    )
    return {
        "total_entries": total,
        "distinct_sessions": sessions,
        "by_project": [dict(r) for r in projects],
    }
