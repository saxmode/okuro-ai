### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Age-tier trace sessions and compact analyzed ones; human turns are never compacted.
# index: imports | TIERS | tier_for_age | refresh_tiers | mark_analyzed | compact_traces | tier_report
# AGENT_HEADER_END -->
"""Trace lifecycle — tier by age, compact what has already been analyzed.

The trace store is 943 818 events and ~4.3 GB of a 7.2 GB database, and it
grows every ten minutes. It cannot be left unmanaged, and it must not be
deleted: a standing constraint records that ``agent_events`` is the
load-bearing corpus, not dead mass.

The resolution is compaction rather than deletion. Bodies are nulled; the
event skeleton — timing, DAG edges, tool names, token counts — survives
forever, so every aggregate metric remains computable on a compacted session.

Tiers
-----
==========  ========  ====================================================
Tier        Age       Retained
==========  ========  ====================================================
``hot``     0-30 d    everything verbatim
``warm``    30-90 d   everything verbatim; analysis runs in this band
``cool``    90-180 d  ``tool_result`` bodies dropped
``cold``    >180 d    input-side turns + skeleton only
==========  ========  ====================================================

Two invariants, both enforced in :func:`compact_traces`:

**Input-side turns are never compacted, at any tier.** They are 2.8% of the
corpus by bytes and 100% of the signal this pipeline reads. Compacting them to
reclaim space would be trading the entire purpose of the store for 89 MB.

**A session is only compactable once it has been analyzed.** ``trace_lifecycle
.analyzed_at`` gates it. Compacting first would discard evidence before anyone
looked at it, which is the one failure mode that cannot be undone.

A third gate was added once the first two proved insufficient. Analysis is not
the only consumer of raw text: two modules rebuild a derived index by
LIKE-scanning it, and neither is visible from here. **A session is only
compactable once every registered text extractor has harvested it** — see
:mod:`.extractors`. The gate fails closed and counts its refusals by reason, so
"nothing was eligible" and "the registry blocked 2000 sessions" can never look
the same in the output.

Measured shape of the corpus, which is what makes this worth doing::

    tool_result  292 502 events  2 114 MB   66%
    assistant    522 527 events  1 259 MB   39%
    user          25 746 events     89 MB  2.8%   <- never compacted
    progress+sys 103 125 events    115 MB  3.6%
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# (tier, max_age_days). None = unbounded.
TIERS: tuple[tuple[str, int | None], ...] = (
    ("hot", 30),
    ("warm", 90),
    ("cool", 180),
    ("cold", None),
)

# Event types whose bodies each tier may drop. Note the absence of 'user'.
_COMPACTABLE: dict[str, tuple[str, ...]] = {
    "hot": (),
    "warm": (),
    "cool": ("tool_result",),
    "cold": ("tool_result", "assistant", "progress", "system"),
}


def tier_for_age(age_days: float) -> str:
    """Return the tier an event of this age belongs to."""
    for tier, max_days in TIERS:
        if max_days is None or age_days <= max_days:
            return tier
    return "cold"


def refresh_tiers() -> dict:
    """Recompute ``trace_lifecycle.tier`` for every traced session.

    Cheap and idempotent — one UPDATE per tier band driven by ``last_ts``.
    """
    from okuro.db import get_db

    db = get_db()

    with db.write():
        # Ensure every traced session has a lifecycle row.
        db.execute(
            """
            INSERT INTO trace_lifecycle (native_session_id, tier)
            SELECT a.session_id, 'hot'
            FROM agent_sessions a
            WHERE NOT EXISTS (
                SELECT 1 FROM trace_lifecycle l
                WHERE l.native_session_id = a.session_id
            )
            """
        )

        lower = 0
        for tier, max_days in TIERS:
            if max_days is None:
                db.execute(
                    """
                    UPDATE trace_lifecycle SET tier = ?, updated_at = datetime('now')
                    WHERE native_session_id IN (
                        SELECT session_id FROM agent_sessions
                        WHERE julianday('now') - julianday(
                            replace(replace(last_ts,'T',' '),'Z','')) > ?
                    )
                    """,
                    (tier, lower),
                )
            else:
                db.execute(
                    """
                    UPDATE trace_lifecycle SET tier = ?, updated_at = datetime('now')
                    WHERE native_session_id IN (
                        SELECT session_id FROM agent_sessions
                        WHERE julianday('now') - julianday(
                            replace(replace(last_ts,'T',' '),'Z','')) > ?
                          AND julianday('now') - julianday(
                            replace(replace(last_ts,'T',' '),'Z','')) <= ?
                    )
                    """,
                    (tier, lower, max_days),
                )
                lower = max_days

    rows = db.fetchall(
        "SELECT tier, COUNT(*) AS n FROM trace_lifecycle GROUP BY tier"
    )
    return {r["tier"]: r["n"] for r in rows}


def mark_analyzed(native_session_ids: list[str], batch_id: str) -> int:
    """Record that these sessions fed an analysis batch — unlocks compaction."""
    from okuro.db import get_db

    if not native_session_ids:
        return 0
    db = get_db()
    with db.write():
        for sid in native_session_ids:
            db.execute(
                """
                INSERT INTO trace_lifecycle
                    (native_session_id, tier, analyzed_at, analyzed_batch)
                VALUES (?, 'hot', datetime('now'), ?)
                ON CONFLICT(native_session_id) DO UPDATE SET
                    analyzed_at    = datetime('now'),
                    analyzed_batch = excluded.analyzed_batch,
                    updated_at     = datetime('now')
                """,
                (sid, batch_id),
            )
    return len(native_session_ids)


def compact_traces(dry_run: bool = True, limit: int | None = None,
                   require_analyzed: bool = True) -> dict:
    """Null the bodies of compactable events in cool/cold sessions.

    Args:
        dry_run: Report what would be reclaimed without writing. Default True —
            this function destroys content, so the caller opts in explicitly.
        limit: Cap sessions processed in one pass.
        require_analyzed: Only touch sessions that already fed an analysis
            batch. Turning this off discards evidence nobody has read; it
            exists for reprocessing a corpus whose findings are already
            recorded, not for routine use.

    Returns:
        Per-tier counts and three byte figures that must not be conflated:
        ``bytes_reclaimed`` is what the allowed set gives up, ``blocked_bytes``
        is what the extractor gate held back, and ``candidate_bytes`` is their
        sum — the tier-eligible mass, i.e. the number to compare against an
        earlier dry run. ``refused_by_reason`` says why the difference exists.
    """
    from okuro.db import get_db

    from . import extractors

    db = get_db()

    results: dict[str, dict] = {}
    total_reclaimed = 0
    total_nulled = 0
    total_blocked_bytes = 0
    total_blocked_events = 0
    refused_by_reason: dict[str, int] = {}

    for tier in ("cool", "cold"):
        types = _COMPACTABLE[tier]
        if not types:
            continue

        where = ["l.tier = ?"]
        params: list = [tier]
        if require_analyzed:
            # Eligibility is "the evidence has been extracted", not "this
            # session appeared in a findings batch". The analysis window is a
            # rolling 7-37 days, so a per-session analysis gate would leave
            # every session older than 37 days permanently ineligible —
            # measured, that stranded 3784 cool/cold sessions, i.e. the entire
            # population compaction exists to serve.
            #
            # A completed detector scan IS the extraction: markers and their
            # evidence are persisted independently of any batch, and findings
            # are a rolling synthesis on top rather than a per-session
            # artifact. So either signal unlocks compaction.
            where.append(
                "(l.analyzed_at IS NOT NULL"
                " OR EXISTS (SELECT 1 FROM interaction_scanned s"
                "            WHERE s.native_session_id = l.native_session_id))"
            )
        where.append("l.compacted_at IS NULL")

        sql = f"""
            SELECT l.native_session_id
            FROM trace_lifecycle l
            WHERE {' AND '.join(where)}
        """
        if limit is not None:
            sql += f" LIMIT {int(limit)}"

        sessions = [r["native_session_id"] for r in db.fetchall(sql, tuple(params))]
        if not sessions:
            results[tier] = {"sessions": 0, "events_nulled": 0, "bytes_reclaimed": 0}
            continue

        type_placeholders = ",".join("?" * len(types))
        tier_reclaimed = 0
        tier_nulled = 0

        # Measure all sessions in ONE grouped pass. Per-session measurement
        # queries looked harmless but ran 3784 scans over a 943k-row table and
        # did not finish inside two minutes; the grouped form is a single scan.
        measured_by_session: dict[str, tuple[int, int]] = {}
        batch = 400
        for i in range(0, len(sessions), batch):
            chunk = sessions[i : i + batch]
            sid_placeholders = ",".join("?" * len(chunk))
            for row in db.fetchall(
                f"""
                SELECT session_id,
                       COUNT(*) AS n,
                       COALESCE(SUM(LENGTH(COALESCE(text,'')) +
                                    LENGTH(COALESCE(content_json,''))), 0) AS bytes
                FROM agent_events
                WHERE session_id IN ({sid_placeholders})
                  AND type IN ({type_placeholders})
                  AND (text IS NOT NULL OR content_json IS NOT NULL)
                GROUP BY session_id
                """,
                (*chunk, *types),
            ):
                measured_by_session[row["session_id"]] = (
                    row["n"] or 0,
                    row["bytes"] or 0,
                )

        # THE EXTRACTOR GATE. Fail closed: a session no registered extractor
        # has harvested THROUGH ITS LAST EVENT keeps its bodies, because that
        # text may be the only copy of a link somebody rebuilds from later.
        # extractors.refusals() keys its answer by reason — never scanned,
        # scanned but overtaken by later events, or no extractor covering
        # these types at all — so a blocked run never reads as an empty one.
        blocked: dict[str, set[str]] = {}
        for reason, missing in extractors.refusals(db, sessions, types).items():
            refused_by_reason[reason] = refused_by_reason.get(reason, 0) + len(missing)
            blocked.setdefault(reason, set()).update(missing)
        blocked_sids = set().union(*blocked.values()) if blocked else set()
        if blocked_sids:
            log.warning(
                "compact_traces: %s tier — %d of %d sessions refused by the "
                "extractor registry (%s). Run extractors.harvest_all() first.",
                tier, len(blocked_sids), len(sessions),
                ", ".join(sorted(blocked)),
            )

        tier_blocked_bytes = 0
        tier_blocked_events = 0

        for sid in sessions:
            n, nbytes = measured_by_session.get(sid, (0, 0))
            if n == 0:
                continue

            if sid in blocked_sids:
                tier_blocked_events += n
                tier_blocked_bytes += nbytes
                continue

            tier_nulled += n
            tier_reclaimed += nbytes

            if dry_run:
                continue

            with db.write():
                # type='user' is absent from every _COMPACTABLE tuple, so the
                # input side is structurally unreachable here. The explicit
                # guard below makes that invariant local rather than implied.
                db.execute(
                    f"""
                    UPDATE agent_events
                    SET text = NULL, content_json = NULL
                    WHERE session_id = ?
                      AND type IN ({type_placeholders})
                      AND type != 'user'
                    """,
                    (sid, *types),
                )
                db.execute(
                    """
                    UPDATE trace_lifecycle
                    SET compacted_at     = datetime('now'),
                        bytes_reclaimed  = bytes_reclaimed + ?,
                        events_nulled    = events_nulled + ?,
                        updated_at       = datetime('now')
                    WHERE native_session_id = ?
                    """,
                    (nbytes, n, sid),
                )

        results[tier] = {
            "sessions": len(sessions),
            "refused_sessions": len(blocked_sids),
            "events_nulled": tier_nulled,
            "bytes_reclaimed": tier_reclaimed,
            "blocked_events": tier_blocked_events,
            "blocked_bytes": tier_blocked_bytes,
        }
        total_reclaimed += tier_reclaimed
        total_nulled += tier_nulled
        total_blocked_bytes += tier_blocked_bytes
        total_blocked_events += tier_blocked_events

    candidate_bytes = total_reclaimed + total_blocked_bytes
    return {
        "dry_run": dry_run,
        "by_tier": results,
        "events_nulled": total_nulled,
        "bytes_reclaimed": total_reclaimed,
        "mb_reclaimed": round(total_reclaimed / 1048576.0, 1),
        # The tier-eligible mass, gate or no gate. This is the figure to
        # compare against an earlier dry run: it does not move when the
        # registry blocks something, so a shrinking reclaim and a growing
        # refusal count stay tellable apart.
        "candidate_events": total_nulled + total_blocked_events,
        "candidate_bytes": candidate_bytes,
        "candidate_mb": round(candidate_bytes / 1048576.0, 1),
        "blocked_events": total_blocked_events,
        "blocked_bytes": total_blocked_bytes,
        "blocked_mb": round(total_blocked_bytes / 1048576.0, 1),
        "refused_by_reason": refused_by_reason,
    }


def run_lifecycle(compact: bool = True, limit: int | None = None) -> dict:
    """Daemon entry point: harvest, re-tier everything, then compact.

    ``compact`` defaults to True here, unlike :func:`compact_traces`, because
    the scheduled job exists to actually reclaim space. The eligibility gate
    still holds — only analyzed cool/cold sessions are touched, and input-side
    turns are never in scope.

    Two things this used to get wrong:

    **It starved.** ``limit`` defaulted to 500 sessions per weekly run against
    a backlog of 2403 eligible cool/cold sessions holding 688 MB, so the
    scheduled job could never catch up — it reclaimed roughly a fifth of the
    standing backlog per week while new sessions kept aging in. ``None`` now
    means run to completion; pass an explicit ``limit`` only to bound an
    experiment. The first run after this change carries the whole backlog and
    is correspondingly long, which is why the task sits in its own slot.

    **It nulled before extracting.** :func:`extractors.harvest_all` now runs
    first, so the registered raw-text consumers have persisted their hits
    before anything is destroyed. The gate inside :func:`compact_traces` is
    the belt to this braces: if the harvest fails, compaction refuses rather
    than proceeding on stale coverage.
    """
    from . import extractors

    harvest = extractors.harvest_all()
    tiers = refresh_tiers()
    result = {"harvest": harvest, "tiers": tiers}
    result["compaction"] = compact_traces(dry_run=not compact, limit=limit)
    return result


def tier_report() -> dict:
    """Current tier distribution with live byte weight and analysis coverage."""
    from okuro.db import get_db

    db = get_db()

    rows = db.fetchall(
        """
        SELECT l.tier,
               COUNT(*) AS sessions,
               SUM(CASE WHEN l.analyzed_at  IS NOT NULL THEN 1 ELSE 0 END) AS analyzed,
               SUM(CASE WHEN l.compacted_at IS NOT NULL THEN 1 ELSE 0 END) AS compacted,
               SUM(l.bytes_reclaimed) AS reclaimed
        FROM trace_lifecycle l
        GROUP BY l.tier
        """
    )

    live = db.fetchone(
        """
        SELECT COUNT(*) AS events,
               COALESCE(SUM(LENGTH(COALESCE(text,'')) +
                            LENGTH(COALESCE(content_json,''))), 0) AS bytes
        FROM agent_events
        """
    ) or {}

    return {
        "tiers": [dict(r) for r in rows],
        "live_events": live.get("events", 0),
        "live_mb": round((live.get("bytes") or 0) / 1048576.0, 1),
    }
