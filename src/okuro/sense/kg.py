# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Temporal knowledge graph — subject-predicate-object triples with validity windows.
# index: imports | def _normalize_date | def _triple_id | def _ensure_entity | def kg_add | def kg_query | def kg_invalidate | def kg_timeline | def kg_stats
# AGENT_HEADER_END -->
"""Temporal knowledge graph — mempalace knowledge_graph.py pattern.

Each row is a triple ``(subject, predicate, object)`` plus a validity window
``valid_from .. valid_to``. NULL ``valid_to`` = still true. Distinguishes
"X was true Q1, Y is true Q2" from flat replacement (which agent_memory's
``supersedes`` does).

Schema lives in migration ``031_kg.sql`` (kg_entities + kg_triples).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, date

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_date(value) -> str | None:
    """Coerce ``value`` to ISO ``YYYY-MM-DD``. Accepts datetime, date, ISO string.

    Returns None when value is None/empty. Returns the original string
    untouched when it isn't parseable — KG callers may pass partial dates
    (``2026``) and we'd rather store-as-given than reject.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        v = value.strip()
        return v or None
    return str(value)


def _triple_id(subject: str, predicate: str, obj: str, valid_from: str | None) -> str:
    """Stable ID for upsert idempotency on the same temporal slice."""
    h = hashlib.sha256()
    for part in (subject, predicate, obj, valid_from or ""):
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def _ensure_entity(db, name: str, etype: str = "concept",
                   project: str | None = None,
                   properties: dict | None = None) -> str:
    """Upsert an entity, return its ID. ID = sha256(name || ':' || type)."""
    eid = hashlib.sha256(f"{name}:{etype}".encode("utf-8")).hexdigest()
    with db.write():
        existing = db.fetchone("SELECT 1 FROM kg_entities WHERE id = ?", (eid,))
        if not existing:
            db.execute(
                """INSERT INTO kg_entities (id, name, type, project, properties)
                   VALUES (?, ?, ?, ?, ?)""",
                (eid, name, etype, project, json.dumps(properties or {})),
            )
    return eid


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def kg_add(subject: str, predicate: str, object: str,
           valid_from=None, valid_to=None,
           subject_type: str = "concept", object_type: str = "concept",
           project: str | None = None,
           source_memory_id: str | None = None,
           source_artifact_id: str | None = None,
           confidence: float = 0.7) -> str:
    """Insert a temporal triple. Idempotent on (s, p, o, valid_from)."""
    from okuro.db import get_db
    db = get_db()

    if project:
        from okuro.sense.progress import _ensure_project
        _ensure_project(db, project)

    _ensure_entity(db, subject, subject_type, project=project)
    _ensure_entity(db, object, object_type, project=project)

    vf = _normalize_date(valid_from)
    vt = _normalize_date(valid_to)
    tid = _triple_id(subject, predicate, object, vf)

    with db.write():
        existing = db.fetchone("SELECT id FROM kg_triples WHERE id = ?", (tid,))
        if existing:
            return f"Triple already exists (id={tid[:12]})"
        db.execute(
            """INSERT INTO kg_triples
               (id, subject, predicate, object, valid_from, valid_to,
                project, source_memory_id, source_artifact_id, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (tid, subject, predicate, object, vf, vt, project,
             source_memory_id, source_artifact_id, confidence),
        )
    return f"Triple stored (id={tid[:12]}, {subject} -{predicate}-> {object}, valid_from={vf})"


def kg_invalidate(subject: str, predicate: str, object: str,
                  ended=None) -> str:
    """Mark every matching triple's validity as ending on ``ended`` (default: today).

    Matches by (subject, predicate, object) regardless of valid_from — useful
    when callers don't know the exact start date. Only triples with NULL
    valid_to are affected (already-closed slices stay untouched).
    """
    from okuro.db import get_db
    db = get_db()

    end = _normalize_date(ended) or date.today().isoformat()
    with db.write():
        cursor = db.execute(
            """UPDATE kg_triples
               SET valid_to = ?, invalidated_at = datetime('now')
               WHERE subject = ? AND predicate = ? AND object = ?
                 AND (valid_to IS NULL OR valid_to = '')""",
            (end, subject, predicate, object),
        )
    # SQLite cursor.rowcount reflects last UPDATE
    return f"Invalidated triples ({subject} -{predicate}-> {object}) ending {end}"


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _validity_filter(as_of: str | None) -> tuple[str, list]:
    """Build a WHERE-clause fragment + params for ``as_of`` filtering.

    NULL valid_from → considered always-known; NULL valid_to → still true.
    """
    if not as_of:
        return "1=1", []
    return (
        "(valid_from IS NULL OR valid_from = '' OR valid_from <= ?) "
        "AND (valid_to IS NULL OR valid_to = '' OR valid_to >= ?)",
        [as_of, as_of],
    )


def _kg_query_lexical(db, entity: str, aof, direction: str,
                      project: str | None, limit: int) -> list[dict]:
    """Exact-string triple lookup — the original ``WHERE subject = ?`` path."""
    val_sql, val_params = _validity_filter(aof)

    where = []
    params: list = []
    if direction == "outgoing":
        where.append("subject = ?")
        params.append(entity)
    elif direction == "incoming":
        where.append("object = ?")
        params.append(entity)
    else:
        where.append("(subject = ? OR object = ?)")
        params.extend([entity, entity])

    if project:
        where.append("(project = ? OR project IS NULL)")
        params.append(project)

    where.append(val_sql)
    params.extend(val_params)

    sql = f"""SELECT id, subject, predicate, object, valid_from, valid_to,
                     project, source_memory_id, confidence, created_at
              FROM kg_triples
              WHERE {' AND '.join(where)}
              ORDER BY (valid_from IS NULL), valid_from DESC, created_at DESC
              LIMIT ?"""
    rows = db.fetchall(sql, tuple(params + [limit]))
    return [dict(r) for r in rows]


def kg_query(entity: str, as_of=None, direction: str = "both",
             project: str | None = None, limit: int = 50,
             fuzzy: bool = True, resolve_threshold: float = 0.82) -> list[dict]:
    """Triples touching ``entity``, optionally as-of a specific date.

    direction = 'outgoing' | 'incoming' | 'both'.

    The lexical lookup is exact-string. When it returns nothing and
    ``fuzzy`` is on, the Fuzzy-Entity-Resolver maps ``entity`` to canonical
    KG names and re-runs the lookup for the single confident match (score
    >= ``resolve_threshold`` and unambiguously ahead of the runner-up). Each
    row is then tagged ``resolved_from`` / ``resolved_to`` / ``resolve_score``
    so the caller sees the substitution. Ambiguous or low-confidence terms
    return empty — call ``resolve_entity`` for the "did you mean" candidates
    rather than silently guessing between them.
    """
    from okuro.db import get_db
    db = get_db()

    aof = _normalize_date(as_of)
    rows = _kg_query_lexical(db, entity, aof, direction, project, limit)
    if rows or not fuzzy:
        return rows

    from okuro.sense.kg_resolve import resolve_entity
    cands = resolve_entity(entity, project=project)
    if not cands:
        return rows

    # Collapse by name: the lexical retry keys on the name string, so two
    # entities sharing a name (e.g. same name as both concept and role) are a
    # single resolution target, not an ambiguity.
    by_name: dict[str, float] = {}
    for c in cands:
        by_name[c["name"]] = max(by_name.get(c["name"], 0.0), c["score"])
    ranked = sorted(by_name.items(), key=lambda kv: kv[1], reverse=True)

    top_name, top_score = ranked[0]
    unambiguous = len(ranked) == 1 or (top_score - ranked[1][1]) >= 0.08
    if top_score < resolve_threshold or not unambiguous:
        return rows

    resolved = _kg_query_lexical(db, top_name, aof, direction, project, limit)
    for r in resolved:
        r["resolved_from"] = entity
        r["resolved_to"] = top_name
        r["resolve_score"] = top_score
    return resolved


def kg_timeline(entity: str | None = None, project: str | None = None,
                limit: int = 100) -> list[dict]:
    """Chronological event stream — when triples became and stopped being true."""
    from okuro.db import get_db
    db = get_db()

    where = ["1=1"]
    params: list = []
    if entity:
        where.append("(subject = ? OR object = ?)")
        params.extend([entity, entity])
    if project:
        where.append("(project = ? OR project IS NULL)")
        params.append(project)

    sql = f"""SELECT id, subject, predicate, object, valid_from, valid_to,
                     project, confidence, created_at, invalidated_at
              FROM kg_triples
              WHERE {' AND '.join(where)}
              ORDER BY COALESCE(valid_from, created_at) DESC
              LIMIT ?"""
    rows = db.fetchall(sql, tuple(params + [limit]))
    return [dict(r) for r in rows]


def kg_stats() -> dict:
    """Entity + triple counts; predicate frequency table."""
    from okuro.db import get_db
    db = get_db()

    e_total = db.fetchone("SELECT COUNT(*) AS c FROM kg_entities")["c"]
    t_total = db.fetchone("SELECT COUNT(*) AS c FROM kg_triples")["c"]
    t_active = db.fetchone(
        "SELECT COUNT(*) AS c FROM kg_triples WHERE valid_to IS NULL OR valid_to = ''"
    )["c"]
    t_closed = t_total - t_active

    type_rows = db.fetchall(
        "SELECT type, COUNT(*) AS c FROM kg_entities GROUP BY type ORDER BY c DESC"
    )
    pred_rows = db.fetchall(
        """SELECT predicate, COUNT(*) AS c FROM kg_triples
           GROUP BY predicate ORDER BY c DESC LIMIT 20"""
    )
    return {
        "entities": e_total,
        "triples_total": t_total,
        "triples_active": t_active,
        "triples_closed": t_closed,
        "entity_types": [dict(r) for r in type_rows],
        "top_predicates": [dict(r) for r in pred_rows],
    }
