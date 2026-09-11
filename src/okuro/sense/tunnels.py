# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Cross-project memory tunnels (mempalace palace-graph pattern).
# index: imports | def _normalize_concept | def _tunnel_id | def tunnel_link | def tunnel_unlink | def tunnel_find | def tunnel_bridge | def tunnel_concepts
# AGENT_HEADER_END -->
"""Cross-project memory tunnels.

Memories in okuro are project-scoped — a learning under project A is invisible
when working in project B. Tunnels add a sideband of free-form concept tags
that link memories across projects under a shared label (``mcp-middleware``,
``sqlite-concurrency``, ``embedding-bug``).

A query by concept surfaces every linked memory regardless of project,
exactly mempalace's wing-bridging "tunnels" idea adapted to okuro's
project model.
"""

from __future__ import annotations

import hashlib
import re

# Concept slug shape: lowercase, alphanumeric + hyphen, max 64 chars.
_CONCEPT_SLUG_RE = re.compile(r"[^a-z0-9\-]+")
_CONCEPT_MAX_LEN = 64


def _normalize_concept(concept: str) -> str:
    """Slug-normalize a concept tag. Empty after normalization → ValueError."""
    if not concept:
        raise ValueError("concept is required")
    s = concept.strip().lower().replace(" ", "-").replace("_", "-")
    s = _CONCEPT_SLUG_RE.sub("-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:_CONCEPT_MAX_LEN]
    if not s:
        raise ValueError(f"concept {concept!r} normalizes to empty")
    return s


def _tunnel_id(concept: str, memory_id: str) -> str:
    h = hashlib.sha256()
    h.update(concept.encode("utf-8"))
    h.update(b":")
    h.update(memory_id.encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def tunnel_link(memory_id: str, concept: str,
                note: str | None = None) -> str:
    """Attach a memory to a shared concept. Idempotent on (concept, memory_id).

    Resolves the memory's project automatically so cross-project queries can
    filter without an extra join.
    """
    from okuro.db import get_db
    db = get_db()

    slug = _normalize_concept(concept)
    row = db.fetchone(
        "SELECT id, project FROM agent_memory WHERE id = ?",
        (memory_id,),
    )
    if not row:
        return f"Memory {memory_id!r} not found"

    tid = _tunnel_id(slug, memory_id)
    with db.write():
        existing = db.fetchone("SELECT 1 FROM memory_tunnels WHERE id = ?", (tid,))
        if existing:
            return f"Tunnel already linked (concept={slug}, memory={memory_id[:12]})"
        db.execute(
            """INSERT INTO memory_tunnels (id, concept, memory_id, project, note)
               VALUES (?, ?, ?, ?, ?)""",
            (tid, slug, memory_id, row["project"], note),
        )
    return f"Tunnel linked (concept={slug}, memory={memory_id[:12]}, project={row['project'] or 'system'})"


def tunnel_unlink(memory_id: str, concept: str) -> str:
    from okuro.db import get_db
    db = get_db()
    slug = _normalize_concept(concept)
    tid = _tunnel_id(slug, memory_id)
    with db.write():
        db.execute("DELETE FROM memory_tunnels WHERE id = ?", (tid,))
    return f"Tunnel unlinked (concept={slug}, memory={memory_id[:12]})"


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def tunnel_find(concept: str, project: str | None = None,
                exclude_project: str | None = None,
                limit: int = 20) -> list[dict]:
    """Memories tunnel-linked to ``concept``.

    Args:
        concept: concept tag (will be slug-normalized).
        project: when set, restrict to memories under this project plus
                 system-wide ones.
        exclude_project: when set, omit memories from this project — useful
                         to surface ONLY cross-project hits ("what does
                         everyone else know about this?").
        limit: max rows.
    """
    from okuro.db import get_db
    db = get_db()
    slug = _normalize_concept(concept)

    where = ["t.concept = ?"]
    params: list = [slug]
    if project:
        where.append("(t.project = ? OR t.project IS NULL)")
        params.append(project)
    if exclude_project:
        where.append("(t.project IS NULL OR t.project != ?)")
        params.append(exclude_project)

    sql = f"""SELECT t.concept, t.memory_id, t.project, t.note, t.created_at,
                     m.topic, m.content, m.confidence, m.source_agent
              FROM memory_tunnels t
              JOIN agent_memory m ON m.id = t.memory_id
              WHERE {' AND '.join(where)}
              ORDER BY m.confidence DESC, t.created_at DESC
              LIMIT ?"""
    rows = db.fetchall(sql, tuple(params + [limit]))
    return [dict(r) for r in rows]


def tunnel_bridge(project_a: str, project_b: str,
                  limit: int = 50) -> list[dict]:
    """Concepts present in BOTH projects' tunnels.

    Returns one row per shared concept with counts on each side. Use to
    discover where two projects' learnings intersect.
    """
    from okuro.db import get_db
    db = get_db()
    sql = """
        SELECT a.concept,
               COUNT(DISTINCT a.memory_id) AS count_a,
               (SELECT COUNT(DISTINCT b.memory_id)
                FROM memory_tunnels b
                WHERE b.concept = a.concept AND b.project = ?) AS count_b
        FROM memory_tunnels a
        WHERE a.project = ?
          AND EXISTS (
              SELECT 1 FROM memory_tunnels c
              WHERE c.concept = a.concept AND c.project = ?
          )
        GROUP BY a.concept
        ORDER BY (count_a + count_b) DESC
        LIMIT ?
    """
    rows = db.fetchall(sql, (project_b, project_a, project_b, limit))
    return [dict(r) for r in rows]


def tunnel_concepts_touching(project: str, min_project_count: int = 2,
                             limit: int = 50) -> list[dict]:
    """Concepts where ``project`` participates AND total project_count >= N.

    Cross-project bootstrap surface: concepts that are linked from THIS
    project AND from at least one other project. Counts are computed
    across the full memory_tunnels table, not restricted to the project.
    """
    from okuro.db import get_db
    db = get_db()
    sql = """
        SELECT concept,
               COUNT(DISTINCT memory_id) AS memory_count,
               COUNT(DISTINCT project)   AS project_count
        FROM memory_tunnels
        WHERE concept IN (
            SELECT DISTINCT concept FROM memory_tunnels WHERE project = ?
        )
        GROUP BY concept
        HAVING COUNT(DISTINCT project) >= ?
        ORDER BY project_count DESC, memory_count DESC
        LIMIT ?
    """
    rows = db.fetchall(sql, (project, min_project_count, limit))
    return [dict(r) for r in rows]


def tunnel_concepts(min_count: int = 2, project: str | None = None,
                    limit: int = 50) -> list[dict]:
    """List concepts ranked by linked-memory count.

    ``min_count`` of 2 surfaces concepts that bridge AT LEAST 2 memories —
    a useful filter when scanning for cross-project knowledge.
    """
    from okuro.db import get_db
    db = get_db()

    where = ["1=1"]
    params: list = []
    if project:
        where.append("(project = ? OR project IS NULL)")
        params.append(project)

    sql = f"""SELECT concept,
                     COUNT(DISTINCT memory_id) AS memory_count,
                     COUNT(DISTINCT project)   AS project_count
              FROM memory_tunnels
              WHERE {' AND '.join(where)}
              GROUP BY concept
              HAVING memory_count >= ?
              ORDER BY project_count DESC, memory_count DESC
              LIMIT ?"""
    rows = db.fetchall(sql, tuple(params + [min_count, limit]))
    return [dict(r) for r in rows]
