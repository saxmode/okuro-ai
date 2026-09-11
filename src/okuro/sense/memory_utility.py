# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Memory utility — correlate memory surfacings with session outcomes (Meta-Harness P8).
# index: imports | DEFAULT_MIN_SURFACINGS | FLAG_NEG_DELTA | _baseline_score | compute_utility | audit | summary
# AGENT_HEADER_END -->
"""Memory utility — correlate memory surfacings with session outcomes.

Backs Meta-Harness P8 (external/objective evaluator). Instead of asking
agents to rate memories (subjective, paper warns against), we join the
``surface_log`` (every time a memory was shown to an agent, filtered to
``kind='memory'``) with ``sessions.compliance_normalized`` (the external
compliance score) and compare each memory's cohort score against the
global baseline.

Stats are computed on-demand — at current volumes the baseline query
costs a few ms. We will move to a materialized aggregate table once
``surface_log`` passes ~100k rows.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MIN_SURFACINGS = 5
FLAG_NEG_DELTA = -0.10          # flag memories whose delta is ≤ this
STAT_CONFIDENCE_MIN = 20        # surfacings needed before auto-decay should consider a memory

# A SESSION THAT DID NOTHING CANNOT TELL YOU A MEMORY WAS HARMFUL.
#
# Measured 2026-07-29: memory_audit(min_surfacings=20) returned 50 rows against
# a baseline of 0.4487, and its ranking was an artifact twice over.
#
#   * 12 rows showed avg_score EXACTLY 0.0 across 78-234 surfacings, giving
#     delta exactly -0.4487 (= -baseline) — the most "harmful" score the tool
#     can emit. Every one of their surfacings came from runs that scored zero.
#   * 27+ of the 50 shared a last_surfaced_at inside a 20-SECOND window
#     (2026-07-25 15:06:01..15:06:20), mostly one project's document-engine rows.
#     Those are not distinct agent sessions; one bulk run surfaced them all.
#
# The two artifacts have ONE cause: those runs had zero turns and zero tool
# calls. A retro independently found all 10 of its low-score sessions were
# provider=codex with zero turns. So the tool was ranking "was this row in a
# scoreless bulk sweep", not "did this row hurt" — and callers were told to use
# it to pick supersede candidates.
#
# The predicate below is the fix: a session contributes to a memory's utility
# only if it actually DID something. `tools_used` is a JSON array, so an empty
# run is NULL, '' or '[]'. Compared explicitly rather than for truthiness —
# '[]' is a truthy string in Python, and that exact mistake is what made
# _rescore_sessions leave 1,401 bridged sessions unscored.
def active_session_sql(alias: str = "s") -> str:
    """SQL predicate for "this session DID something" — the one definition.

    A session that called no tool has a compliance score describing nothing:
    it never had the chance to comply. Averaging those in does not measure
    worse behaviour, it measures a different population. This module learned
    that first (see `_baseline_score`), and then `project_status` recomputed a
    per-project average WITHOUT the predicate and reported 0.166 for a project
    whose only real session scored 0.83 — six of its seven sessions were
    zero-tool bootstrap probes.

    So it is a function taking the table alias rather than a private constant:
    the caller that needs it most was in another module and could not reach it.
    """
    return (
        f"{alias}.tools_used IS NOT NULL AND {alias}.tools_used != '' "
        f"AND {alias}.tools_used != '[]'"
    )


_ACTIVE_SESSION_SQL = active_session_sql("s")


def _baseline_score() -> float | None:
    """Global mean of ``compliance_normalized`` across sessions that DID something.

    The baseline must be drawn from the same population as the per-memory
    averages, or every delta is measured against a different distribution.
    Including scoreless zero-turn runs dragged the baseline down, which then
    made ordinary memories look like helpers and batch-surfaced ones look
    catastrophic.
    """
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        "SELECT AVG(compliance_normalized) AS avg FROM sessions s "
        "WHERE compliance_normalized IS NOT NULL AND " + _ACTIVE_SESSION_SQL
    )
    return (row or {}).get("avg")


def compute_utility(
    memory_id: str | None = None,
    min_surfacings: int = DEFAULT_MIN_SURFACINGS,
    limit: int = 50,
) -> dict[str, Any]:
    """Per-memory utility stats (surfacings, avg score, delta vs baseline).

    Returns::

        {
            "baseline": 0.64,
            "total_memories": 12,
            "stats": [
                {"memory_id": "...", "topic": "gotcha", "surfacings": 14,
                 "avg_score": 0.52, "delta": -0.12, "content": "…"},
                ...
            ],
        }

    Ranking is delta DESC so callers can show top-N helpers or reverse for
    bottom-N. A single memory's ``stats`` list has length 1 or 0.
    """
    from okuro.db import get_db

    db = get_db()
    baseline = _baseline_score()

    where = (
        "s.compliance_normalized IS NOT NULL AND l.kind = 'memory' "
        "AND " + _ACTIVE_SESSION_SQL
    )
    params: list = []
    if memory_id:
        where += " AND l.entity_id = ?"
        params.append(memory_id)

    rows = db.fetchall(
        f"""
        SELECT l.entity_id                             AS memory_id,
               COUNT(DISTINCT l.session_id)            AS surfacings,
               AVG(s.compliance_normalized)            AS avg_score,
               MIN(s.compliance_normalized)            AS min_score,
               MAX(s.compliance_normalized)            AS max_score,
               MAX(l.surfaced_at)                      AS last_surfaced_at,
               MIN(l.surfaced_at)                      AS first_surfaced_at,
               COUNT(DISTINCT substr(l.surfaced_at, 1, 10)) AS distinct_days,
               m.topic, m.confidence, m.source_agent,
               substr(m.content, 1, 140)               AS content_head
        FROM surface_log l
        JOIN sessions s ON s.session_id = l.session_id
        LEFT JOIN agent_memory m ON m.id = l.entity_id
        WHERE {where}
        GROUP BY l.entity_id
        HAVING surfacings >= ?
        ORDER BY (AVG(s.compliance_normalized) - COALESCE(?, 0)) DESC
        LIMIT ?
        """,
        (*params, int(min_surfacings), baseline, int(limit)),
    )

    for r in rows:
        r["delta"] = (r["avg_score"] or 0) - (baseline or 0)
        # BATCH TELL, exposed rather than silently filtered. Surfacings spread
        # over many days are evidence; surfacings compressed onto one or two
        # days are a bulk sweep wearing a statistic's clothes. The caller sees
        # the spread and can weigh the delta accordingly — the zero-turn filter
        # above removes the scoreless bulk runs, but a scored one-day burst is
        # still a thin sample, and hiding that would trade one silent artifact
        # for another.
        days = r.get("distinct_days") or 0
        r["surfacings_per_day"] = (
            round(r["surfacings"] / days, 1) if days else None
        )
        r["batch_suspect"] = bool(days and days <= 2 and r["surfacings"] >= 10)

    # WHAT WAS EXCLUDED, counted. A filter that improves a number without
    # saying what it dropped is how the original artifact survived unnoticed.
    dropped = db.fetchone(
        "SELECT COUNT(DISTINCT l.entity_id) AS n FROM surface_log l "
        "JOIN sessions s ON s.session_id = l.session_id "
        "WHERE s.compliance_normalized IS NOT NULL AND l.kind = 'memory' "
        "AND NOT (" + _ACTIVE_SESSION_SQL + ")"
    )

    return {
        "baseline": baseline,
        "total_memories": len(rows),
        "min_surfacings": int(min_surfacings),
        "excluded_zero_turn_sessions": True,
        "memories_touched_only_by_zero_turn_sessions": (dropped or {}).get("n"),
        "stats": rows,
    }


def audit(min_surfacings: int = STAT_CONFIDENCE_MIN, flag_threshold: float = FLAG_NEG_DELTA) -> dict[str, Any]:
    """Return only memories with sustained-negative utility.

    A memory makes the list when:
      * it's been surfaced in ≥ ``min_surfacings`` scored sessions, AND
      * its average ``compliance_normalized`` is at least
        ``flag_threshold`` below the global baseline.
    """
    result = compute_utility(min_surfacings=min_surfacings, limit=200)
    result["stats"] = [s for s in result["stats"] if s["delta"] <= flag_threshold]
    result["total_memories"] = len(result["stats"])
    result["flag_threshold"] = flag_threshold
    return result


DECAY_STEP = 0.05               # confidence subtracted per decay fire
DECAY_COOLDOWN_DAYS = 7         # don't decay the same memory more than once per week


def auto_decay(
    min_surfacings: int = STAT_CONFIDENCE_MIN,
    flag_threshold: float = FLAG_NEG_DELTA,
    step: float = DECAY_STEP,
    cooldown_days: int = DECAY_COOLDOWN_DAYS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Auto-decay confidence on memories with sustained-negative utility.

    Eligibility: ``audit()`` finds the memory (≥ ``min_surfacings`` and
    ``delta ≤ flag_threshold``) AND it hasn't been utility-decayed in the
    last ``cooldown_days``.

    Decrements ``agent_memory.confidence`` by ``step`` (floored at 0.0)
    and stamps ``last_utility_decayed_at``. The existing
    ``memory_hygiene`` task then prunes anything below its threshold on
    its own schedule; we don't prune here — decay is soft.
    """
    from okuro.db import get_db

    result = audit(min_surfacings=min_surfacings, flag_threshold=flag_threshold)
    candidates = result["stats"]
    base_return = {
        "candidates": len(candidates),
        "decayed": 0,
        "skipped_cooldown": 0,
        "step": step,
        "flag_threshold": flag_threshold,
        "min_surfacings": min_surfacings,
        "dry_run": dry_run,
        "memory_ids": [],
    }
    if not candidates:
        return base_return

    db = get_db()
    decayed_ids: list[str] = []
    skipped_cooldown: list[str] = []

    for c in candidates:
        mid = c["memory_id"]
        row = db.fetchone(
            "SELECT confidence, last_utility_decayed_at FROM agent_memory WHERE id = ?",
            (mid,),
        )
        if not row:
            continue
        last = row.get("last_utility_decayed_at")
        if last:
            cooldown_row = db.fetchone(
                "SELECT (julianday('now') - julianday(?)) AS days_since",
                (last,),
            ) or {}
            if (cooldown_row.get("days_since") or 0) < cooldown_days:
                skipped_cooldown.append(mid)
                continue
        if dry_run:
            decayed_ids.append(mid)
            continue
        old_conf = row.get("confidence") or 0.0
        new_conf = max(0.0, old_conf - step)
        db.execute(
            "UPDATE agent_memory SET confidence = ?, last_utility_decayed_at = datetime('now') WHERE id = ?",
            (new_conf, mid),
        )
        # Decay is one of the five mechanisms that write this float. Without an
        # event, a decayed row is indistinguishable from one whose author was
        # simply unsure — see migration 105.
        from okuro.sense.memory import record_confidence_event

        record_confidence_event(db, mid, old_conf, new_conf, "decay",
                                note=f"utility step {step}")
        decayed_ids.append(mid)

    return {
        "candidates": len(candidates),
        "decayed": len(decayed_ids),
        "skipped_cooldown": len(skipped_cooldown),
        "step": step,
        "flag_threshold": flag_threshold,
        "min_surfacings": min_surfacings,
        "dry_run": dry_run,
        "memory_ids": decayed_ids,
    }


def summary() -> dict[str, Any]:
    """High-level counters — how much signal do we have so far?"""
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        """
        SELECT
            (SELECT COUNT(*) FROM surface_log WHERE kind = 'memory')                                     AS surfacings,
            (SELECT COUNT(DISTINCT entity_id) FROM surface_log WHERE kind = 'memory')                    AS memories_surfaced,
            (SELECT COUNT(DISTINCT session_id) FROM surface_log WHERE kind = 'memory' AND session_id IS NOT NULL) AS sessions_with_surface,
            (SELECT COUNT(*) FROM surface_log WHERE kind = 'memory' AND session_id IS NULL)              AS surfacings_without_session,
            (SELECT COUNT(DISTINCT context) FROM surface_log WHERE kind = 'memory')                      AS contexts
        """
    )
    data = rows[0] if rows else {}
    data["baseline"] = _baseline_score()
    return data
