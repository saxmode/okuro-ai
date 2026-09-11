# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Named analytic over the review loop's own telemetry — rounds,
#   verdict-by-attempt, inter-round latency, finding severity. The review
#   subsystem emits convergence_telemetry per published verdict but had no
#   reader, so every measurement pass hand-wrote SQL against okuro.db.
# index:
#   imports
#   constants
#   def _percentile / _iso_to_epoch
#   def _round_rows
#   def _severity_breakdown
#   def review_loop_stats
# AGENT_HEADER_END -->
"""Review-loop analytics.

Two stores, deliberately kept apart in the output:

1. ``task_events`` where ``event_type='convergence_telemetry'`` — the
   DURABLE, append-only source. One row per published review verdict
   (``dispatcher_streaming`` emits it after every ``publish_review``).
   Everything in ``rounds`` / ``verdict_by_attempt`` / ``latency`` comes
   from here.
2. ``review_queue`` — the publish/subscribe row the subagent's
   ``await_review`` long-polls. Rows carry ``findings_json`` (the only
   place finding SEVERITY is stored) but they are TTL'd, so
   ``severity`` is reported with its own ``rows_matched`` count. A low
   count means the queue was purged, NOT that findings were clean.

GROUPING CONTRACT — the trap this module exists to encode: ``subtask_id``
is ``"1.1"``-style and is **not** unique across tasks, and ``review_queue``
carries no ``task_id`` column at all. Grouping rounds by ``subtask_id``
alone silently merges every task in the window and manufactures
subtasks with hundreds of rounds. Every aggregate here groups by
``(task_id, subtask_id)``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

# Sanity bound on an inter-round gap. A gap wider than this is not a
# review round — it is a task that was parked overnight, resumed, or had
# its engine restarted between rounds. Including those would swamp the
# mean with wall-clock the loop did not actually spend.
_MAX_PLAUSIBLE_GAP_S: float = 7200.0


def _iso_to_epoch(value: Any) -> Optional[float]:
    """Parse a telemetry timestamp to epoch seconds, or None.

    ``convergence_telemetry.ts`` is written by the dispatcher as a naive
    ``datetime.isoformat()``; ``task_events.created_at`` is SQLite's
    ``datetime('now')`` (``"YYYY-MM-DD HH:MM:SS"``, space-separated). Both
    are UTC and both must parse, so the separator is normalised rather
    than assumed.
    """
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _percentile(values: list[float], q: float) -> Optional[float]:
    """Nearest-rank percentile of an already-sorted-or-not list."""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _round_rows(
    *, window_days: int, task_id: Optional[str],
) -> list[dict]:
    """Every convergence_telemetry row in the window, newest-last.

    Returns dicts of ``task_id, subtask_id, artifact_id, attempt, verdict,
    findings_count, ts_epoch`` — ``subtask_id`` read from the BODY (the
    dispatcher writes it there) falling back to the column, and ``ts``
    falling back to ``created_at`` when the body carries no timestamp.
    """
    from okuro.db import get_db

    db = get_db()
    sql = (
        "SELECT task_id, subtask_id, body, created_at "
        "  FROM task_events "
        " WHERE event_type = 'convergence_telemetry' "
        "   AND created_at >= datetime('now', ?) "
    )
    params: list[Any] = [f"-{int(window_days)} day"]
    if task_id:
        sql += "   AND task_id = ? "
        params.append(task_id)
    sql += " ORDER BY created_at ASC"

    out: list[dict] = []
    for row in db.fetchall(sql, tuple(params)):
        r = dict(row)
        try:
            body = json.loads(r.get("body") or "{}")
        except (TypeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        ts = _iso_to_epoch(body.get("ts")) or _iso_to_epoch(r.get("created_at"))
        if ts is None:
            continue
        out.append({
            "task_id": r.get("task_id") or "",
            "subtask_id": str(body.get("subtask_id") or r.get("subtask_id") or ""),
            "artifact_id": str(body.get("artifact_id") or ""),
            "attempt": int(body.get("attempt") or 0),
            "verdict": str(body.get("verdict") or ""),
            "findings_count": int(body.get("findings_count") or 0),
            "ts_epoch": ts,
        })
    return out


def _severity_breakdown(*, window_days: int) -> dict:
    """Finding severity from ``review_queue`` — best-effort, TTL'd source.

    ``rows_matched`` is returned alongside so a purged queue reads as
    "no data" rather than "no load-bearing findings". Not filterable by
    task: the table has no ``task_id`` column.
    """
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        "SELECT verdict, findings_json FROM review_queue "
        " WHERE created_at >= datetime('now', ?)",
        (f"-{int(window_days)} day",),
    )

    severity: dict[str, int] = {}
    verdict_profile: dict[str, dict[str, int]] = {}
    fail_zero_load_bearing = 0
    fail_total = 0

    for row in rows:
        r = dict(row)
        verdict = str(r.get("verdict") or "")
        try:
            findings = json.loads(r.get("findings_json") or "[]")
        except (TypeError, ValueError):
            findings = []
        if not isinstance(findings, list):
            findings = []

        load_bearing = 0
        cosmetic = 0
        for f in findings:
            if not isinstance(f, dict):
                continue
            sev = str(f.get("severity") or "unknown").lower()
            severity[sev] = severity.get(sev, 0) + 1
            if sev == "load_bearing":
                load_bearing += 1
            elif sev == "cosmetic":
                cosmetic += 1

        if load_bearing:
            profile = "load_bearing"
        elif cosmetic:
            profile = "cosmetic_only"
        else:
            profile = "no_findings"
        verdict_profile.setdefault(verdict, {})
        verdict_profile[verdict][profile] = (
            verdict_profile[verdict].get(profile, 0) + 1
        )

        if verdict == "FAIL":
            fail_total += 1
            if not load_bearing:
                fail_zero_load_bearing += 1

    return {
        "source": "review_queue (TTL'd — low rows_matched means purged, not clean)",
        "rows_matched": len(rows),
        "severity_counts": severity,
        "verdict_by_finding_profile": verdict_profile,
        "fail_rows": fail_total,
        "fail_with_zero_load_bearing": fail_zero_load_bearing,
    }


def review_loop_stats(
    *,
    window_days: int = 30,
    task_id: Optional[str] = None,
) -> dict:
    """Aggregate the review loop over ``window_days``.

    Pass ``task_id`` to scope the round/latency sections to one task; the
    severity section stays global because ``review_queue`` carries no
    ``task_id`` column (stated in its ``scope`` field, never implied).
    """
    window_days = max(1, int(window_days))
    rows = _round_rows(window_days=window_days, task_id=task_id)

    # (task_id, subtask_id) — NOT subtask_id. See module docstring.
    by_subtask: dict[tuple[str, str], list[dict]] = {}
    verdict_by_attempt: dict[int, dict[str, int]] = {}
    for r in rows:
        by_subtask.setdefault((r["task_id"], r["subtask_id"]), []).append(r)
        if r["attempt"]:
            slot = verdict_by_attempt.setdefault(r["attempt"], {})
            slot[r["verdict"]] = slot.get(r["verdict"], 0) + 1

    rounds_hist: dict[int, int] = {}
    one_shot_pass = 0
    gaps: list[float] = []
    for rounds in by_subtask.values():
        # CHRONOLOGICAL, not by attempt. Sorting by attempt would reorder a
        # re-dispatched subtask's 1,2,1,2 sequence into 1,1,2,2 and destroy
        # the dispatch boundary the pairing rule below depends on.
        rounds.sort(key=lambda x: x["ts_epoch"])
        n = len(rounds)
        rounds_hist[n] = rounds_hist.get(n, 0) + 1
        if n == 1 and rounds[0]["verdict"] == "PASS":
            one_shot_pass += 1
        for prev, cur in zip(rounds, rounds[1:]):
            # Pair CONSECUTIVE ATTEMPTS only. Adjacency in the sorted list
            # is not enough: a subtask re-dispatched after a blocked_review
            # restarts its attempt counter, so the row sequence can read
            # 1,2,1,2 for one (task, subtask). Pairing by list position
            # would then invent a gap across the dispatch boundary, and
            # would also pair two rows sharing an attempt number. Both are
            # dispatch artefacts, not review rounds.
            if cur["attempt"] != prev["attempt"] + 1:
                continue
            gap = cur["ts_epoch"] - prev["ts_epoch"]
            if 0.0 <= gap <= _MAX_PLAUSIBLE_GAP_S:
                gaps.append(gap)

    subtasks = len(by_subtask)
    total_rounds = len(rows)

    latency: dict[str, Any] = {
        "gaps_measured": len(gaps),
        "excluded_over_s": _MAX_PLAUSIBLE_GAP_S,
        "mean_s": round(sum(gaps) / len(gaps), 1) if gaps else None,
        "median_s": (
            round(_percentile(gaps, 0.5), 1) if gaps else None  # type: ignore[arg-type]
        ),
        "p90_s": (
            round(_percentile(gaps, 0.9), 1) if gaps else None  # type: ignore[arg-type]
        ),
        "total_hours": round(sum(gaps) / 3600.0, 2) if gaps else 0.0,
    }

    return {
        "window_days": window_days,
        "task_id": task_id,
        "scope": (
            "rounds/latency scoped to task_id when given; severity is "
            "always global — review_queue has no task_id column"
        ),
        "grouping": "(task_id, subtask_id)",
        "rounds": {
            "subtasks": subtasks,
            "total_rounds": total_rounds,
            "mean_rounds": (
                round(total_rounds / subtasks, 2) if subtasks else None
            ),
            "one_shot_pass": one_shot_pass,
            "one_shot_pass_pct": (
                round(100.0 * one_shot_pass / subtasks, 1) if subtasks else None
            ),
            "histogram": dict(sorted(rounds_hist.items())),
        },
        "verdict_by_attempt": {
            k: dict(sorted(v.items(), key=lambda kv: -kv[1]))
            for k, v in sorted(verdict_by_attempt.items())
        },
        "inter_round_latency": latency,
        "findings": _severity_breakdown(window_days=window_days),
    }
