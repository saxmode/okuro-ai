# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Cortex retrieval observability — query logging + zero-result/hit-rate/staleness/index-health metrics.
# index:
#   imports
#   def log_query
#   def query_metrics
#   def index_health
# AGENT_HEADER_END -->
"""Cortex retrieval observability (audit F38/F39/F40).

Two halves:
  * log_query — best-effort, fire-and-forget write of one cortex_query_log row
    per search (query text + result count + zero-result flag + latency + scope).
    Never raises into the search hot path; a logging failure must not break a
    search.
  * query_metrics / index_health — SQL aggregates over the query log and the
    cortex tables, surfaced by the stats endpoint and CLI so an operator can
    see retrieval health at a glance on ANY install.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def log_query(
    query: str,
    n_results: int,
    *,
    project: str | None = None,
    latency_ms: float | None = None,
    hybrid: bool = False,
    db=None,
) -> None:
    """Best-effort append of one query-log row (F38). Never raises.

    Fire-and-forget: any failure (table missing on a pre-058 DB, lock
    contention) is swallowed with a debug log so search latency/behaviour is
    unaffected.
    """
    try:
        if db is None:
            from okuro.db import get_db

            db = get_db()
        db.execute(
            "INSERT INTO cortex_query_log "
            "(query, project, n_results, zero_result, latency_ms, hybrid) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                (query or "")[:2000],
                project,
                int(n_results),
                1 if n_results == 0 else 0,
                float(latency_ms) if latency_ms is not None else None,
                1 if hybrid else 0,
            ),
        )
        # Autocommit connection (isolation_level=None) — no explicit commit
        # needed; the INSERT is durable immediately and outside any caller txn.
    except Exception as exc:  # noqa: BLE001
        log.debug("cortex query-log write skipped: %s", exc)


def query_metrics(db=None, window_days: int | None = 30) -> dict:
    """Aggregate search volume + zero-result/hit rate from the query log (F39).

    ``window_days`` limits to recent rows (None = all time). Returns zeros on a
    pre-058 DB (missing table) so the stats endpoint stays robust.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()

    where = ""
    params: tuple = ()
    if window_days is not None:
        where = "WHERE created_at >= datetime('now', ?)"
        params = (f"-{int(window_days)} days",)

    try:
        row = db.fetchone(
            f"SELECT COUNT(*) AS total, "
            f"SUM(zero_result) AS zeros, "
            f"AVG(latency_ms) AS avg_latency "
            f"FROM cortex_query_log {where}",
            params,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("query_metrics unavailable: %s", exc)
        return {
            "search_volume": 0,
            "zero_result_count": 0,
            "zero_result_rate": 0.0,
            "hit_rate": 0.0,
            "avg_latency_ms": None,
            "window_days": window_days,
        }

    total = (row["total"] if row else 0) or 0
    zeros = (row["zeros"] if row else 0) or 0
    avg_latency = row["avg_latency"] if row else None
    # No queries → rates are undefined; report 0.0 (no data), not 1.0.
    zero_rate = (zeros / total) if total else 0.0
    hit_rate = (1.0 - zero_rate) if total else 0.0
    return {
        "search_volume": total,
        "zero_result_count": zeros,
        "zero_result_rate": round(zero_rate, 4),
        "hit_rate": round(hit_rate, 4),
        "avg_latency_ms": round(avg_latency, 2) if avg_latency is not None else None,
        "window_days": window_days,
    }


def index_health(db=None) -> dict:
    """Index-health snapshot (F40): staleness + live/orphan/dup counts.

    Fields:
      * last_indexed_at  — max(indexed_at) over live docs (staleness signal)
      * live_documents   — non-tombstoned cortex_docs
      * live_vectors     — vec_cortex rows
      * orphan_vectors   — vec rows whose doc is missing/tombstoned (pkg1 helper)
      * worktree_docs    — live docs whose path looks like agent worktree dup
        pollution (a quick LIKE heuristic, mirrors the audit's F2 signal)
    All best-effort: a missing table yields 0/None rather than an error.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()

    def _scalar(sql: str, params: tuple = ()):
        try:
            row = db.fetchone(sql, params)
            return next(iter(row.values())) if row else None
        except Exception:  # noqa: BLE001
            return None

    last_indexed = _scalar(
        "SELECT MAX(indexed_at) AS v FROM cortex_docs WHERE deleted_at IS NULL"
    )
    live_docs = _scalar(
        "SELECT COUNT(*) AS v FROM cortex_docs WHERE deleted_at IS NULL"
    ) or 0
    live_vectors = _scalar("SELECT COUNT(*) AS v FROM vec_cortex") or 0

    try:
        from .reclaim import count_orphan_vectors

        orphans = count_orphan_vectors(db)
    except Exception:  # noqa: BLE001
        orphans = 0

    # Worktree dup heuristic (F2): paths under a worktrees dir. Cheap LIKE.
    worktree_docs = _scalar(
        "SELECT COUNT(*) AS v FROM cortex_docs "
        "WHERE deleted_at IS NULL AND ("
        "file_path LIKE '%/.worktrees/%' OR "
        "file_path LIKE '%/.claude/worktrees/%' OR "
        "file_path LIKE '%/worktrees/%')"
    ) or 0

    return {
        "last_indexed_at": last_indexed,
        "live_documents": int(live_docs),
        "live_vectors": int(live_vectors),
        "orphan_vectors": int(orphans),
        "worktree_docs": int(worktree_docs),
    }
