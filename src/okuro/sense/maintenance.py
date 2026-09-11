# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Self-maintenance — hygiene tasks and health checks.
# index:
#   imports
#   def run_hygiene
#   def _memory_hygiene
#   def _project_sync
#   def _thought_management
#   def _progress_cleanup
#   def _telemetry_aggregate
#   def _compliance_scoring
#   def _health_check
# AGENT_HEADER_END -->
"""Self-maintenance — hygiene tasks and health checks.

Ported from tm-launcher maintenance.py.
Postgres ops replaced with okuro.db SQLite backend.
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from okuro.db.engine import okuro_home

_OKURO_ROOT = Path.home() / "okuro"


def run_hygiene() -> str:
    """Run all hygiene tasks. Returns summary string."""
    results = []

    # NOTE: _doc_indexing was removed — it bulk-imported tm-dev/*/docs/*.md
    # into agent_memory under source_agent='okuro-maintenance', creating
    # 128 of 286 rows (45% noise) observed in the 2026-04-16 audit. The
    # daemon's `memory_hygiene` task now actively deletes rows matching
    # that signature. Doc import is a deliberate user action, not a
    # background side-effect.
    for name, func in [
        ("Memory", _memory_hygiene),
        ("Stale", _memory_staleness),
        ("Projects", _project_sync),
        ("Thoughts", _thought_management),
        ("Progress", _progress_cleanup),
        ("Telemetry", _telemetry_aggregate),
        ("Compliance", _compliance_scoring),
        ("Surface", _surface_log_health),
        ("Health", _health_check),
    ]:
        try:
            results.append(func())
        except Exception as e:
            results.append(f"{name}: FAILED ({e})")

    return "## Maintenance Report\n\n" + "\n".join(f"- {r}" for r in results)


def _memory_hygiene() -> str:
    from okuro.db import get_db

    db = get_db()
    ninety_ago = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()

    cur = db.execute(
        """UPDATE agent_memory SET confidence = MAX(confidence - 0.1, 0.0)
           WHERE last_accessed < ? AND confidence > 0.1""",
        (ninety_ago,),
    )
    decayed = cur.rowcount

    cur = db.execute("DELETE FROM agent_memory WHERE confidence <= 0.05")
    archived = cur.rowcount

    row = db.fetchone("SELECT COUNT(*) AS cnt FROM agent_memory")
    total = row["cnt"] if row else 0

    db.conn.commit()
    return f"Memory: {total} total, {decayed} decayed, {archived} archived"


def _memory_staleness() -> str:
    """REPORT memories the code now contradicts. Never mutates.

    ORCH-RETIRE-ON-FIX asks the session that fixes a bug to supersede the
    gotcha describing it. Nothing connected the two, so the principle ran on
    somebody remembering — and a 2026-07-13 memory that measured the recall
    outage correctly but blamed "compressed embeddings" (rather than a vec0
    table declared L2 and scored as cosine) sat at confidence 0.9 for two
    months, teaching every reader that the symptom was normal. This makes the
    forgetting visible on a weekly cadence instead of never.

    Report-only, unlike every other task in this pass. A memory is prose;
    only the mechanical claims inside it are decidable, and auto-demoting on
    that signal is how a cleanup becomes a regression. It names the offenders
    so a human or agent can retire each with write_memory(supersedes=...),
    keeping whatever is still true.
    """
    from okuro.sense.memory_staleness import scan

    res = scan(min_confidence=0.3)
    proof = [f for f in res["findings"] if f["strength"] == "proof"]
    if not proof:
        return "Stale: 0 memories contradicted by code"

    lit = sum(1 for f in proof if f["kind"] == "contradicted_literal")
    dead = sum(1 for f in proof if f["kind"] == "dangling_path")
    top = "; ".join(
        f"{f['symbol']}={f['memory_says']} (code {f['code_says'][0]})"
        if f["kind"] == "contradicted_literal" else f"missing {f['path']}"
        for f in proof[:3]
    )
    return (
        f"Stale: {len(proof)} memories contradicted by code "
        f"({lit} literal, {dead} dangling) — {top}"
        f"{' …' if len(proof) > 3 else ''} "
        f"[`memory_stale` to review; retire via write_memory(supersedes=…)]"
    )


def _project_sync() -> str:
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone("SELECT COUNT(*) AS cnt FROM projects WHERE active = 1")
    total = row["cnt"] if row else 0
    return f"Projects: {total} active"


def _thought_management() -> str:
    from okuro.db import get_db

    db = get_db()

    # Gate on last_surfaced, not created_at. Semantics: "we surfaced this to
    # you, 30 days passed, no action -> dismiss". Thoughts that were never
    # surfaced stay open forever — the system hasn't earned the right to
    # dismiss a thought the user was never shown. See audit 2026-04-21.
    thirty_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cur = db.execute(
        """UPDATE thoughts SET status = 'dismissed'
           WHERE status = 'open'
             AND last_surfaced IS NOT NULL
             AND last_surfaced < ?""",
        (thirty_ago,),
    )
    dismissed_unengaged = cur.rowcount

    fourteen_ago = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    row = db.fetchone(
        "SELECT COUNT(*) AS cnt FROM thoughts WHERE status = 'open' AND updated_at < ?",
        (fourteen_ago,),
    )
    stuck = row["cnt"] if row else 0

    counts_rows = db.fetchall("SELECT status, COUNT(*) AS cnt FROM thoughts GROUP BY status")
    counts = {r["status"]: r["cnt"] for r in counts_rows}

    db.conn.commit()
    return (
        f"Thoughts: {counts}, "
        f"dismissed_unengaged_30d={dismissed_unengaged}, stuck={stuck}"
    )


def _progress_cleanup() -> str:
    from okuro.db import get_db

    db = get_db()

    one_day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    cur = db.execute(
        """UPDATE progress SET status = 'blocked'
           WHERE status IN ('implementing', 'testing') AND updated_at < ?""",
        (one_day_ago,),
    )
    stale = cur.rowcount

    thirty_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cur = db.execute("DELETE FROM progress WHERE updated_at < ?", (thirty_ago,))
    archived = cur.rowcount

    db.conn.commit()
    return f"Progress: {stale} flagged stale, {archived} archived"


def _telemetry_aggregate() -> str:
    """Aggregate usage.jsonl into DB tables and rotate the log."""
    from okuro.db import get_db
    import uuid

    usage_file = okuro_home() / "telemetry" / "usage.jsonl"
    if not usage_file.exists():
        return "Telemetry: no data to aggregate"

    db = get_db()
    lines = usage_file.read_text().splitlines()
    if not lines:
        return "Telemetry: empty file"

    ingested = 0
    sessions_seen: set[str] = set()
    errors = 0

    for line in lines:
        try:
            entry = json.loads(line)
            session_id = entry.get("session", "unknown")
            sessions_seen.add(session_id)

            ts_str = entry.get("ts", "")
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                ts = datetime.now(timezone.utc)

            entry_provider = entry.get("provider", "unknown")
            entry_session_type = entry.get("session_type")

            if session_id != "unknown":
                row_id = str(uuid.uuid4())
                st = entry_session_type or "direct"
                db.execute(
                    """INSERT INTO sessions (id, session_id, provider, started_at, session_type)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT (session_id) DO UPDATE SET
                           provider = CASE
                               WHEN sessions.provider = 'unknown' AND ? != 'unknown'
                               THEN ? ELSE sessions.provider END,
                           session_type = CASE
                               WHEN ? IS NOT NULL THEN ?
                               ELSE sessions.session_type END""",
                    (row_id, session_id, entry_provider, ts.isoformat(), st,
                     entry_provider, entry_provider,
                     entry_session_type, entry_session_type),
                )

            tu_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO tool_usage
                       (id, session_id, server, tool, called_at, latency_ms, ok, arg_keys)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tu_id,
                    session_id if session_id != "unknown" else None,
                    entry.get("server", "unknown"),
                    entry.get("tool", "unknown"),
                    ts.isoformat(),
                    entry.get("latency_ms"),
                    1 if entry.get("ok", True) else 0,
                    json.dumps(entry.get("arg_keys", [])),
                ),
            )
            ingested += 1

        except (json.JSONDecodeError, KeyError, Exception):
            errors += 1
            continue

    db.conn.commit()

    # Update session tools_used from aggregated tool_usage
    for sid in sessions_seen:
        if sid == "unknown":
            continue
        tool_rows = db.fetchall(
            "SELECT DISTINCT tool FROM tool_usage WHERE session_id = ?", (sid,)
        )
        tools_json = json.dumps(sorted(r["tool"] for r in tool_rows))
        db.execute(
            "UPDATE sessions SET tools_used = ? WHERE session_id = ?",
            (tools_json, sid),
        )
    db.conn.commit()

    # Rotate: archive old file
    archive_dir = okuro_home() / "telemetry" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"usage-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.jsonl"
    usage_file.rename(archive_dir / archive_name)

    # Clean up archives older than 90 days
    ninety_ago = datetime.now(timezone.utc) - timedelta(days=90)
    cleaned = 0
    for f in archive_dir.glob("usage-*.jsonl"):
        if datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc) < ninety_ago:
            f.unlink()
            cleaned += 1

    return (
        f"Telemetry: {ingested} calls ingested from {len(sessions_seen)} sessions, "
        f"{errors} errors, {cleaned} archives cleaned"
    )


def _compliance_scoring() -> str:
    from okuro.sense.telemetry import close_orphaned_sessions, aggregate_provider_compliance
    from okuro.sense.agents import close_dead_agents

    agents_closed = close_dead_agents(heartbeat_timeout_min=30)
    r1 = close_orphaned_sessions(timeout_minutes=30)

    # Metric 2: judge a batch of unjudged sessions BEFORE aggregating so new
    # quality scores land in this cycle's rollup. Capped + best-effort — a
    # bridge/model failure must not break compliance scoring.
    #
    # Was limit=5, HALF the function's own default of 10, against a backlog of
    # ~2,175. Combined with the newest-first ordering that is now fixed in
    # judge_pending, coverage sat at 24 of 2199 claude-code sessions and zero
    # for every other provider — a Quality column with no corpus behind it.
    # 25/cycle with half the batch backfilling drains the tail in weeks rather
    # than never, and each judge is one fast-tier call (haiku / flash / local
    # qwen), so the cost is bounded by the cap, not by the backlog.
    try:
        from okuro.sense.quality import judge_pending
        rq = judge_pending(limit=25)
    except Exception as exc:
        rq = f"Quality: skipped ({type(exc).__name__})"

    r2 = aggregate_provider_compliance(window_days=30)
    return f"agents_closed={agents_closed}, {r1}, {rq}, {r2}"


def _surface_log_health() -> str:
    """Report surface-log write failures since the last hygiene cycle.

    Surface-log losses corrupt bucket math silently — forgotten-thought
    detection assumes surface rows always land. Reading and resetting
    here means a stuck DB or schema drift becomes visible in the
    Maintenance Report instead of accumulating invisibly to stderr.
    """
    from okuro.sense.surface import get_and_reset_failure_counts

    counts = get_and_reset_failure_counts()
    total = sum(counts.values())
    if total == 0:
        return "Surface: no write failures since last cycle"
    return (
        f"Surface: {total} write failures (memory={counts.get('memory', 0)}, "
        f"thought={counts.get('thought', 0)}) — bucket math may be inconsistent"
    )


def _health_check() -> str:
    checks = []

    try:
        from okuro.db import get_db
        db = get_db()
        db.fetchone("SELECT 1 AS ok")
        checks.append("db:ok")
    except Exception:
        checks.append("db:FAIL")

    try:
        from okuro.embed.client import embed_one
        embed_one("test")
        checks.append("embeddings:ok")
    except Exception:
        checks.append("embeddings:FAIL")

    return f"Health: {', '.join(checks)}"
