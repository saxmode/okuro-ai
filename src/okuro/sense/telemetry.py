# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Session telemetry — session lifecycle, compliance scoring, tool performance.
# index:
#   imports
#   def create_session
#   def session_report
#   def tool_performance
#   def session_history
#   def get_previous_session_score
#   def _tools_from_jsonl
#   def _calculate_compliance
#   def _detect_session_type
#   def is_ghost_session
#   def _calculate_compliance_v2
#   def _infer_project_from_tools
#   def close_orphaned_sessions
#   def aggregate_provider_compliance
#   def _calculate_tool_rates
#   def compliance_scorecard
#   def get_provider_compliance
#   def _performance_from_jsonl
# AGENT_HEADER_END -->
"""Session telemetry — session lifecycle, compliance scoring, tool performance.

Ported from tm-launcher brain/telemetry.py.
Postgres ops replaced with okuro.db SQLite backend.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from okuro.db.engine import okuro_home

USAGE_FILE = okuro_home() / "telemetry" / "usage.jsonl"
_SESSION_REPORT_DB_BUSY_TIMEOUT_MS = 1500


# Provider -> ordered list of env vars carrying the CLI's NATIVE session id
# (the one the transcript index `agent_sessions` keys on). okuro's stdio MCP
# server is one subprocess per session, spawned by the CLI, so it inherits
# these. Explicit map first so claude's transcript uuid wins over the derived
# CLAUDE_CODE_BRIDGE_SESSION_ID. Extending to a new provider = one line.
_PROVIDER_SESSION_ENV = {
    "claude-code": ["CLAUDE_CODE_SESSION_ID"],
    "codex": ["CODEX_SESSION_ID", "CODEX_THREAD_ID"],
    "gemini": ["GEMINI_SESSION_ID", "GEMINI_CLI_SESSION_ID"],
    "cursor": ["CURSOR_SESSION_ID"],
    # Known to expose no transcript session id — empty list pins the result to
    # None so a leaked sibling *_SESSION_ID can't be mis-attributed.
    "claude-desktop": [],
}


def _resolve_provider_session_id(provider: str | None) -> str | None:
    """Best-effort native session id from the inherited CLI env.

    Provider-agnostic: explicit per-provider vars first, then a generic
    ``*_SESSION_ID`` fallback (excluding known non-transcript ids like the
    BRIDGE-derived one and desktop-environment noise). Returns None when the
    provider exposes nothing (e.g. claude-desktop) — the caller degrades.
    """
    import os as _os

    known = _PROVIDER_SESSION_ENV.get(provider or "")
    if known is not None:
        # Mapped provider: use ONLY its own vars. Never fall through to the
        # generic scan — that could grab a foreign *_SESSION_ID leaked into the
        # env (e.g. claude-desktop, which has no transcript id, picking up a
        # sibling claude-code var). Absent var -> None, degrade gracefully.
        for var in known:
            val = _os.environ.get(var)
            if val:
                return val
        return None

    # Unknown/unmapped provider: best-effort generic scan, excluding known
    # non-transcript ids (BRIDGE-derived) and desktop-environment noise.
    for key, val in _os.environ.items():
        if not val:
            continue
        ku = key.upper()
        if ku.endswith("_SESSION_ID") and not any(
            bad in ku for bad in ("BRIDGE", "GNOME", "MANAGER", "DBUS", "XDG")
        ):
            return val
    return None


def create_session(
    session_id: str,
    provider: str = "unknown",
    task_hint: str = "",
    project: str | None = None,
    session_type: str | None = None,
    pid: int | None = None,
    host: str | None = None,
    task_id: str | None = None,
    subtask_id: str | None = None,
    dispatch_epoch: str | None = None,
) -> None:
    """Write initial session row at bootstrap time with ended_at=null.

    ROCK-SOLID v5 P4.1 — ``task_id`` / ``subtask_id`` / ``generation`` give the
    session a WORK identity. Without them this table records who is running and
    when, but not what they were dispatched to do, so "is a session live for
    subtask 1.2?" has no answer — and dispatch cannot refuse a duplicate, a
    successor engine cannot reap its predecessor's subagents, and a write from
    a superseded dispatch is indistinguishable from a current one.

    ``dispatch_epoch`` is the SAME ISO value artifact_write and
    write_role_handover already carry (C12) — not a parallel counter. P4.4
    compares a write's epoch against the supersede-sweep watermark to reject a
    straggler from a prior retry; a session that recorded its generation any
    other way could not be checked against it.

    All three default to None. An interactive CLI session has no work identity
    and should keep NULL rather than be given a fabricated one.

    `pid` and `host` default to the current process. They are used by
    `close_orphaned_sessions` as a provider-agnostic liveness signal:
    while that PID is running on that host, the session is alive regardless
    of whether the agent happens to be calling okuro MCP tools right now.

    Also stamps the agent heartbeat and links the session to its agent row,
    so the Live-Agents view can surface "agent X is currently working on Y".
    """
    import os as _os
    import socket as _socket
    from okuro.db import get_db

    if session_type is None:
        session_type = _detect_session_type(provider)
    if pid is None:
        pid = _os.getpid()
    if host is None:
        try:
            host = _socket.gethostname()
        except Exception:
            host = None

    # Stamp heartbeat and resolve agent_id before the session insert so the
    # row is linked from the start. Both calls are defensive — failures must
    # not block session creation.
    agent_id: str | None = None
    try:
        from okuro.sense.agents import upsert_heartbeat
        agent_id = upsert_heartbeat(provider=provider, pid=pid)
    except Exception:
        agent_id = None

    provider_session_id = _resolve_provider_session_id(provider)

    db = get_db()
    row_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO sessions (id, session_id, provider, task_hint, project,
                                 started_at, session_type, pid, host, agent_id,
                                 provider_session_id,
                                 task_id, subtask_id, dispatch_epoch)
           VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (session_id) DO UPDATE SET
               agent_id = COALESCE(sessions.agent_id, excluded.agent_id),
               provider_session_id = COALESCE(sessions.provider_session_id,
                                              excluded.provider_session_id),
               -- COALESCE on the EXISTING value, like the columns above: a
               -- re-bootstrap of the same session must not silently re-point
               -- it at different work. A genuine re-dispatch mints a new
               -- session id and therefore a new row.
               task_id = COALESCE(sessions.task_id, excluded.task_id),
               subtask_id = COALESCE(sessions.subtask_id, excluded.subtask_id),
               dispatch_epoch = COALESCE(sessions.dispatch_epoch,
                                         excluded.dispatch_epoch)""",
        (row_id, session_id, provider, task_hint, project,
         session_type, pid, host, agent_id, provider_session_id,
         task_id, subtask_id, dispatch_epoch),
    )
    db.conn.commit()


def live_sessions_for_subtask(task_id: str, subtask_id: str) -> list[dict]:
    """Open sessions dispatched to this exact (task, subtask) — the LEASE query.

    ROCK-SOLID v5 P4.2 asks dispatch to refuse when one already exists. That
    question was unanswerable before P4.1 gave sessions a work identity, which
    is why duplicate dispatch was prevented by ordering and hope rather than by
    a check.

    "Open" is ``ended_at IS NULL`` AND a live pid on this host — the same
    provider-agnostic liveness signal ``close_orphaned_sessions`` uses. A row
    whose process died without closing is NOT a lease; treating it as one would
    wedge the subtask permanently, which is a worse failure than the duplicate
    this prevents.

    Two sources, merged on pid. ``sessions`` is written when the subagent
    BOOTSTRAPS, so a subagent that has spawned but not yet called okuro is
    invisible there — a real window, since spawn to first tool call is seconds.
    ``sessions_inline`` is written at SPAWN by the process that owns the CLI,
    so the lease exists from the moment there is something to hold it.
    """
    from okuro.db import get_db

    rows = get_db().execute(
        """SELECT session_id, provider, pid, host, dispatch_epoch, started_at
             FROM sessions
            WHERE task_id = ? AND subtask_id = ? AND ended_at IS NULL""",
        (task_id, subtask_id),
    ).fetchall()
    merged = _merge_on_pid(
        [dict(r) for r in rows],
        _inline_sessions(task_id, subtask_id=subtask_id),
    )
    return [r for r in merged if _pid_is_live(r.get("pid"), r.get("host"))]


def open_sessions_for_task(task_id: str) -> list[dict]:
    """Every open session for a task — the REAP query (P4.2/P4.3).

    A successor engine enumerates its predecessor's work here so it can cancel
    it BEFORE reconcile revives anything. Unlike the lease query this does NOT
    filter on liveness: a dead-but-unclosed row still needs closing out, and
    that is the caller's job to decide.

    Merged with the spawn-time ``sessions_inline`` rows for the same reason as
    the lease: the pid on a bootstrap-written row is only the CLI's because the
    identity carried it there, and a subagent killed before its first okuro call
    has no bootstrap row at all — while being exactly the orphan a reap exists
    to clear.
    """
    from okuro.db import get_db

    rows = get_db().execute(
        """SELECT session_id, provider, pid, host, subtask_id, dispatch_epoch,
                  started_at
             FROM sessions
            WHERE task_id = ? AND ended_at IS NULL
            ORDER BY started_at""",
        (task_id,),
    ).fetchall()
    merged = _merge_on_pid([dict(r) for r in rows], _inline_sessions(task_id))
    return sorted(merged, key=lambda r: r.get("started_at") or "")


def _inline_sessions(task_id: str, subtask_id: str | None = None) -> list[dict]:
    """Running ``sessions_inline`` rows for this work, in lease/reap shape.

    Empty on any error — including a database that has not run migration 122.
    A telemetry read must never be able to block a dispatch.
    """
    from okuro.db import get_db

    sql = """SELECT id AS session_id, provider, agent_pid AS pid,
                    agent_host AS host, subtask_id, dispatch_epoch, started_at
               FROM sessions_inline
              WHERE task_id = ? AND status = 'running'"""
    params: tuple = (task_id,)
    if subtask_id is not None:
        sql += " AND subtask_id = ?"
        params = (task_id, subtask_id)
    try:
        return [dict(r) for r in get_db().execute(sql, params).fetchall()]
    except Exception:  # noqa: BLE001
        return []


def _merge_on_pid(primary: list[dict], secondary: list[dict]) -> list[dict]:
    """Union two session lists, keyed by pid.

    One subagent has a row in each table with different session ids (the
    bootstrap marker mints its own uuid) but the SAME process. Pid is therefore
    the identity that matters to both callers — the lease counts processes, the
    reap signals them. Rows without a pid cannot be deduplicated and are kept.
    """
    out = list(primary)
    seen = {int(r["pid"]) for r in primary if r.get("pid")}
    for row in secondary:
        pid = row.get("pid")
        if pid and int(pid) in seen:
            continue
        if pid:
            seen.add(int(pid))
        out.append(row)
    return out


def _pid_is_live(pid, host) -> bool:
    """True when that pid is running HERE. A pid on another host is unknowable
    from this process, so it is reported live — refusing to dispatch is the
    safe side of that ambiguity, and okuro is single-host today anyway."""
    import os as _os
    import socket as _socket

    if not pid:
        return False
    try:
        if host and host != _socket.gethostname():
            return True
    except Exception:
        pass
    try:
        _os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def session_report(
    session_id: str,
    provider: str = "unknown",
    task_hint: str = "",
    project: str | None = None,
    feedback: list[dict] | None = None,
) -> str:
    """Write session report with optional tool feedback.

    This is an end-of-session hook, so it must be best-effort: a locked
    telemetry database should degrade the report, not block the MCP client.
    """
    tools_used = _tools_from_jsonl(session_id)
    if "session_report" not in tools_used:
        tools_used.append("session_report")
        tools_used.sort()

    score = _calculate_compliance(session_id, tools_used)
    session_type = _detect_session_type(provider)
    _, normalized, _ = _calculate_compliance_v2(tools_used, session_type)
    feedback_count = len(feedback) if feedback else 0

    try:
        from okuro.db import get_db, temporary_busy_timeout

        db = get_db()
        with temporary_busy_timeout(db, _SESSION_REPORT_DB_BUSY_TIMEOUT_MS):
            row_id = str(uuid.uuid4())
            tools_json = json.dumps(tools_used)

            # Resolve agent_id so the ended row still links back to its live agent.
            # `session_report` closes the work unit, NOT the agent — the CLI keeps
            # running and can start a new work unit. Pulse/Live-Agents queries agents,
            # so the session end has no effect on presence.
            agent_id_for_row: str | None = None
            try:
                from okuro.sense.agents import get_current_agent_id
                agent_id_for_row = get_current_agent_id(provider=provider)
            except Exception:
                agent_id_for_row = None

            db.execute(
                """INSERT INTO sessions
                       (id, session_id, provider, task_hint, project, started_at, ended_at,
                        compliance_score, compliance_normalized, session_type, tools_used,
                        end_reason, agent_id)
                   VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?, ?, ?, ?, 'reported', ?)
                   ON CONFLICT (session_id) DO UPDATE SET
                       ended_at = datetime('now'),
                       compliance_score = excluded.compliance_score,
                       compliance_normalized = excluded.compliance_normalized,
                       session_type = excluded.session_type,
                       tools_used = excluded.tools_used,
                       end_reason = 'reported',
                       agent_id = COALESCE(sessions.agent_id, excluded.agent_id)""",
                (row_id, session_id, provider, task_hint, project, score, normalized,
                 session_type, tools_json, agent_id_for_row),
            )
            db.conn.commit()

            if feedback:
                for fb in feedback:
                    fb_id = str(uuid.uuid4())
                    db.execute(
                        """INSERT INTO tool_feedback
                               (id, session_id, tool_name, used, useful, comment, bypass_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            fb_id,
                            session_id,
                            fb.get("tool_name", "unknown"),
                            1 if fb.get("used", False) else 0,
                            1 if fb.get("useful") else (0 if fb.get("useful") is False else None),
                            fb.get("comment"),
                            fb.get("bypass_reason"),
                        ),
                    )
                db.conn.commit()
    except Exception as exc:
        return (
            "Session report accepted, but telemetry DB write failed fast "
            f"({type(exc).__name__}: {exc}). "
            f"Score: {score}/5, Tools used: {len(tools_used)}, "
            f"Feedback items: {feedback_count}."
        )

    return (
        f"Session recorded. Score: {score}/5, "
        f"Tools used: {len(tools_used)}, "
        f"Feedback items: {feedback_count}."
    )


def tool_performance(
    tool_name: str | None = None,
    server: str | None = None,
    days: int = 30,
) -> str:
    """Query tool performance aggregated from telemetry."""
    from okuro.db import get_db

    db = get_db()

    where_clauses = [f"tu.called_at > datetime('now', '-{days} days')"]
    params: list = []
    if tool_name:
        where_clauses.append("tu.tool = ?")
        params.append(tool_name)
    if server:
        where_clauses.append("tu.server = ?")
        params.append(server)
    where = " AND ".join(where_clauses)

    rows = db.fetchall(f"""
        SELECT
            tu.tool,
            tu.server,
            COUNT(*) AS calls,
            COUNT(DISTINCT tu.session_id) AS sessions,
            ROUND(AVG(tu.latency_ms)) AS avg_latency,
            ROUND(100.0 * SUM(CASE WHEN tu.ok THEN 1 ELSE 0 END) / MAX(COUNT(*), 1), 1) AS success_pct
        FROM tool_usage tu
        WHERE {where}
        GROUP BY tu.tool, tu.server
        ORDER BY calls DESC
    """, tuple(params))

    if not rows:
        return _performance_from_jsonl(tool_name, server, days)

    fb_where = [f"tf.created_at > datetime('now', '-{days} days')"]
    fb_params: list = []
    if tool_name:
        fb_where.append("tf.tool_name = ?")
        fb_params.append(tool_name)
    fb_where_str = " AND ".join(fb_where)

    fb_rows = db.fetchall(f"""
        SELECT
            tf.tool_name,
            SUM(CASE WHEN tf.useful = 1 THEN 1 ELSE 0 END) AS useful,
            SUM(CASE WHEN tf.useful = 0 THEN 1 ELSE 0 END) AS not_useful,
            SUM(CASE WHEN tf.used = 0 THEN 1 ELSE 0 END) AS skipped,
            GROUP_CONCAT(DISTINCT tf.comment) AS comments,
            GROUP_CONCAT(DISTINCT tf.bypass_reason) AS bypass_reasons
        FROM tool_feedback tf
        WHERE {fb_where_str}
        GROUP BY tf.tool_name
        ORDER BY useful DESC
    """, tuple(fb_params))
    feedback_map = {r["tool_name"]: r for r in fb_rows}

    lines = [f"## Tool Performance (last {days} days)\n"]
    lines.append("| Tool | Server | Calls | Sessions | Avg ms | Success | Useful | Not Useful | Skipped |")
    lines.append("|------|--------|-------|----------|--------|---------|--------|------------|---------|")

    for row in rows:
        fb = feedback_map.get(row["tool"], {})
        useful = fb.get("useful", 0) or 0
        not_useful = fb.get("not_useful", 0) or 0
        skipped = fb.get("skipped", 0) or 0
        lines.append(
            f"| {row['tool']} | {row['server']} | {row['calls']} | {row['sessions']} | "
            f"{row['avg_latency'] or '-'} | {row['success_pct']}% | {useful} | {not_useful} | {skipped} |"
        )

    for t_name, fb in feedback_map.items():
        comments = fb.get("comments")
        bypass = fb.get("bypass_reasons")
        if comments:
            lines.append(f"\n**{t_name} comments:** {comments}")
        if bypass:
            lines.append(f"**{t_name} bypass reasons:** {bypass}")

    return "\n".join(lines)


def session_history(
    project: str | None = None,
    provider: str | None = None,
    limit: int = 10,
) -> str:
    """Show recent session history with compliance scores."""
    from okuro.db import get_db

    db = get_db()

    where_clauses = ["1=1"]
    params: list = []
    if project:
        where_clauses.append("s.project = ?")
        params.append(project)
    if provider:
        where_clauses.append("s.provider = ?")
        params.append(provider)
    where = " AND ".join(where_clauses)
    params.append(limit)

    rows = db.fetchall(f"""
        SELECT
            s.session_id,
            s.provider,
            s.project,
            s.task_hint,
            s.started_at,
            s.compliance_score,
            s.compliance_normalized,
            s.session_type,
            s.tools_used,
            s.end_reason,
            CAST((julianday(COALESCE(s.ended_at, datetime('now')))
                  - julianday(s.started_at)) * 86400 AS INTEGER) AS duration_s
        FROM sessions s
        WHERE {where}
        ORDER BY s.started_at DESC
        LIMIT ?
    """, tuple(params))

    if not rows:
        return "No sessions recorded yet. Sessions are created when agents call session_report()."

    lines = ["## Recent Sessions\n"]
    for row in rows:
        dur_min = (row["duration_s"] or 0) // 60
        tools = json.loads(row["tools_used"]) if row["tools_used"] else []
        tools_str = ", ".join(tools) if tools else "none"
        score = row["compliance_score"]
        norm = row["compliance_normalized"]
        score_str = f"{score}/5" if score is not None else "?"
        norm_str = f" ({norm})" if norm is not None else ""
        stype = row["session_type"]
        type_str = f" [{stype}]" if stype and stype != "direct" else ""
        started = row["started_at"] or "?"
        if isinstance(started, str) and len(started) > 16:
            started = started[:16].replace("T", " ")
        lines.append(
            f"**{started}** | {row['provider']}{type_str} | "
            f"score: {score_str}{norm_str} | {dur_min}min | {row['end_reason'] or '?'}\n"
            f"  Task: {row['task_hint'] or '-'} | Project: {row['project'] or '-'}\n"
            f"  Tools: {tools_str}\n"
        )

    return "\n".join(lines)


def get_previous_session_score(project: str | None = None) -> dict | None:
    """Get the most recent session's compliance data for bootstrap social pressure."""
    from okuro.db import get_db

    db = get_db()

    if project:
        row = db.fetchone(
            """SELECT compliance_score, tools_used, provider, task_hint
               FROM sessions WHERE project = ?
               ORDER BY started_at DESC LIMIT 1""",
            (project,),
        )
    else:
        row = db.fetchone(
            """SELECT compliance_score, tools_used, provider, task_hint
               FROM sessions ORDER BY started_at DESC LIMIT 1""",
        )

    if not row:
        return None
    tools = json.loads(row["tools_used"]) if row["tools_used"] else []
    return {
        "score": row["compliance_score"],
        "tools_used": tools,
        "provider": row["provider"],
        "task_hint": row["task_hint"],
    }


# ---------------------------------------------------------------------------
# Compliance scoring
# ---------------------------------------------------------------------------

def _tools_from_jsonl(session_id: str) -> list[str]:
    tools = set()
    try:
        if USAGE_FILE.exists():
            with USAGE_FILE.open() as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("session") == session_id:
                            tools.add(entry["tool"])
                    except (json.JSONDecodeError, KeyError):
                        continue
    except OSError:
        pass
    return sorted(tools)


def _jsonl_session_activity() -> dict[str, dict]:
    """One pass over usage.jsonl — returns {session_id: {tools, last_ts}}.

    B2 fix: `close_orphaned_sessions` needs live per-session activity to
    avoid mis-marking active sessions as ghost. The `tool_usage` SQL table
    is only populated by the weekly `_telemetry_aggregate`, so between
    aggregations it is stale. The live source of truth is this JSONL.
    """
    activity: dict[str, dict] = {}
    try:
        if USAGE_FILE.exists():
            for line in USAGE_FILE.read_text().splitlines():
                try:
                    entry = json.loads(line)
                    sid = entry.get("session")
                    if not sid:
                        continue
                    tool = entry.get("tool")
                    ts = entry.get("ts")
                    bucket = activity.setdefault(sid, {"tools": set(), "last_ts": None})
                    if tool:
                        bucket["tools"].add(tool)
                    if ts and (bucket["last_ts"] is None or ts > bucket["last_ts"]):
                        bucket["last_ts"] = ts
                except (json.JSONDecodeError, KeyError):
                    continue
    except OSError:
        pass
    return activity


def _calculate_compliance(session_id: str, tools_used: list[str]) -> int:
    score = 0
    if "bootstrap" in tools_used:
        score += 1
    if "write_memory" in tools_used:
        score += 1
    if "log_progress" in tools_used:
        score += 1
    cortex_tools = {"cortex_search", "cortex_route", "cortex_read_header", "cortex_read_section"}
    if cortex_tools & set(tools_used):
        score += 1
    score += 1  # calling session_report itself
    return score


_SCORING_WEIGHTS = {
    "direct": {
        "bootstrap": 2, "cortex": 1, "write_memory": 1,
        "log_progress": 1, "session_report": 1,
    },
    "orchestrated": {
        "bootstrap": 1, "cortex": 1, "write_memory": 1,
        "log_progress": 1, "session_report": 1,
    },
    "subagent": {
        "bootstrap": 0, "cortex": 1, "write_memory": 0,
        "log_progress": 0, "session_report": 0,
    },
}


#: Provider label the retired predecessor orchestrator wrote into session
#: rows. Kept verbatim so those historical rows still classify as
#: "orchestrated"; nothing in okuro writes it any more.
_RETIRED_ORCHESTRATOR_PROVIDER = "nightbird"


def _detect_session_type(provider: str) -> str:
    if not provider:
        return "direct"
    p = provider.lower()
    if p.startswith("nb(") or p.startswith("nb-"):
        return "subagent"
    if p == _RETIRED_ORCHESTRATOR_PROVIDER:
        return "orchestrated"
    return "direct"


def is_ghost_session(
    tools_used: list[str] | None,
    duration_seconds: float | None = None,
) -> bool:
    if bool(tools_used):
        return False
    if duration_seconds is not None and duration_seconds >= 120:
        return False
    return True


def _calculate_compliance_v2(
    tools_used: list[str],
    session_type: str = "direct",
    duration_seconds: float | None = None,
) -> tuple[int, float | None, dict[str, int]]:
    """Compute compliance score for a single session.

    Returns ``(raw_points, normalized_score, breakdown)``.

    ``breakdown`` is a dict mapping bucket name -> 1/0 (whether the
    bucket fired for this session). Buckets: ``bootstrap``, ``cortex``,
    ``memory``, ``progress``, ``report``. The bootstrap renderer
    aggregates these counts across recent sessions to produce the
    "show the math" line (audit #13). For non-scorable cases (ghost
    or subagent) the breakdown is returned empty so callers can
    ignore it without surfacing fake zeros.

    Math is unchanged — only the return shape gained a third element.
    """
    if is_ghost_session(tools_used, duration_seconds):
        return 0, None, {}

    if session_type == "subagent":
        if not tools_used:
            if duration_seconds is not None and duration_seconds >= 60:
                return 0, 0.5, {}
            else:
                return 0, 0.0, {}
        else:
            return 1, 0.7, {}

    weights = _SCORING_WEIGHTS.get(session_type, _SCORING_WEIGHTS["direct"])
    cortex_tools = {"cortex_search", "cortex_route", "cortex_read_header",
                    "cortex_read_section", "cortex_read_file"}
    tool_set = set(tools_used)

    breakdown = {
        "bootstrap": 1 if "bootstrap" in tool_set else 0,
        "cortex": 1 if (tool_set & cortex_tools) else 0,
        "memory": 1 if "write_memory" in tool_set else 0,
        "progress": 1 if "log_progress" in tool_set else 0,
        "report": 1 if "session_report" in tool_set else 0,
    }

    raw = 0
    if breakdown["bootstrap"]:
        raw += weights["bootstrap"]
    if breakdown["cortex"]:
        raw += weights["cortex"]
    if breakdown["memory"]:
        raw += weights["write_memory"]
    if breakdown["progress"]:
        raw += weights["log_progress"]
    if breakdown["report"]:
        raw += weights["session_report"]

    max_score = sum(weights.values())
    normalized = round(raw / max_score, 2) if max_score > 0 else 1.0
    return raw, normalized, breakdown


def _infer_project_from_tools(session_id: str, task_hint: str | None) -> str | None:
    """Infer project slug from task_hint keywords and DB project metadata."""
    if not task_hint:
        return None
    task_lower = task_hint.lower()

    try:
        from okuro.db import get_db
        db = get_db()
        rows = db.fetchall("SELECT id, name, path FROM projects WHERE active = 1")
        for r in rows:
            if r["id"] in task_lower:
                return r["id"]
            if r["name"] and r["name"].lower() in task_lower:
                return r["id"]
        for r in rows:
            if r["path"]:
                parts = r["path"].strip("/").split("/")
                for part in parts[-2:]:
                    part_l = part.lower()
                    if part_l in ("active", "archive", "tm-dev", "tm-tools", "dev", "unknown"):
                        continue
                    if part_l in task_lower:
                        return r["id"]
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Orphaned session cleanup
# ---------------------------------------------------------------------------

def _pid_alive(pid: int | None) -> bool:
    """Return True if the given PID exists on this host, False otherwise.

    Uses signal 0 (`os.kill(pid, 0)`) — the POSIX existence probe. A
    PermissionError means the process exists but is owned by another user,
    which still counts as alive for our purposes.
    """
    import os as _os
    if not pid or pid <= 0:
        return False
    try:
        _os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def close_orphaned_sessions(
    timeout_minutes: int = 30,
    abandon_hours: int = 6,
) -> str:
    """Close sessions whose owning process is gone or that look truly abandoned.

    Liveness signal priority (provider-agnostic):

    1. **PID liveness** (strongest). If the session row has a `pid` recorded
       on *this* host and that PID currently maps to a running process,
       the session is alive — regardless of okuro-tool activity. This fixes
       the case where an agent spends >30 min doing native tool calls
       (Read/Edit/Bash/etc.) that don't hit `usage.jsonl` and was previously
       being reaped mid-work.

       Skipped when the same PID owns multiple unclosed sessions (shared
       server / HTTP transport): then PID is not a per-session signal.

    2. **JSONL tool activity** (fallback). If `usage.jsonl` shows a tool call
       for this session within `timeout_minutes`, keep it alive. Covers
       cross-host agents and legacy rows with no recorded PID.

    3. **Abandon age** (safety net). A row with no PID signal, no activity,
       and `started_at` older than `abandon_hours` is reaped. Prevents rows
       from drifting forever when neither signal is available.
    """
    import socket
    from okuro.db import get_db

    db = get_db()

    activity = _jsonl_session_activity()
    now_utc = datetime.now(timezone.utc)
    cutoff_activity = now_utc - timedelta(minutes=timeout_minutes)
    # `abandon_hours` was the safety-net threshold for sessions whose
    # JSONL shows NO activity. It was wrongly chosen at 6h — larger than
    # the activity cutoff — so a session that started 90 min ago with no
    # tool calls stayed open forever (the 30-min activity cutoff never
    # fired, and 90min < 6h so the abandon cutoff didn't fire either).
    # Correct contract: the no-activity cutoff must be at most the
    # activity cutoff, so a session older than `timeout_minutes` with no
    # signal is reaped. Tight the hourly safety-net to the activity
    # cutoff when callers don't pass something smaller.
    abandon_delta = min(
        timedelta(hours=abandon_hours), timedelta(minutes=timeout_minutes)
    )
    cutoff_abandon = now_utc - abandon_delta

    try:
        my_host = socket.gethostname()
    except Exception:
        my_host = None

    rows = db.fetchall(
        """SELECT session_id, provider, started_at, task_hint, project, pid, host
           FROM sessions WHERE ended_at IS NULL"""
    )

    # Count PID occurrences so shared-process transports (one PID, many
    # sessions) don't false-positive every session as alive.
    pid_counts: dict[int, int] = {}
    for r in rows:
        p = r["pid"] if "pid" in r.keys() else None
        if p:
            pid_counts[p] = pid_counts.get(p, 0) + 1

    closed = 0
    ghosts = 0
    attributed = 0
    alive_by_pid = 0

    for row in rows:
        sid = row["session_id"]
        bucket = activity.get(sid, {"tools": set(), "last_ts": None})
        tools = sorted(bucket["tools"])
        last_ts_str = bucket["last_ts"]

        # Parse last-jsonl-activity timestamp once for downstream uses.
        last_dt = None
        if last_ts_str:
            try:
                last_dt = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                last_dt = None

        # --- Signal 1: PID liveness ---
        pid = row["pid"] if "pid" in row.keys() else None
        host = row["host"] if "host" in row.keys() else None
        pid_usable = (
            pid is not None
            and host is not None
            and my_host is not None
            and host == my_host
            and pid_counts.get(pid, 0) == 1
        )
        if pid_usable:
            if _pid_alive(pid):
                alive_by_pid += 1
                continue  # process running → session alive, per contract
            # PID is dead → session is genuinely abandoned; close it below
            # using the last known activity timestamp for accurate duration.
        else:
            # --- Signal 2: JSONL activity fallback ---
            if last_dt is not None and last_dt > cutoff_activity:
                continue  # recent tool call — still working
            # --- Signal 3: abandon-age safety net ---
            if last_dt is None:
                try:
                    started_dt = datetime.fromisoformat(row["started_at"])
                    if started_dt.tzinfo is None:
                        started_dt = started_dt.replace(tzinfo=timezone.utc)
                    if started_dt > cutoff_abandon:
                        continue  # no signal yet, but too fresh to reap
                except (ValueError, TypeError):
                    continue

        # Compute duration for scoring.
        ended_at_str = last_ts_str or row["started_at"]
        try:
            dt_end = last_dt or datetime.fromisoformat(row["started_at"])
            dt_start = datetime.fromisoformat(row["started_at"])
            if dt_start.tzinfo is None:
                dt_start = dt_start.replace(tzinfo=timezone.utc)
            if dt_end.tzinfo is None:
                dt_end = dt_end.replace(tzinfo=timezone.utc)
            duration_s = (dt_end - dt_start).total_seconds()
        except (ValueError, TypeError):
            duration_s = 0

        session_type = _detect_session_type(row["provider"])
        ghost = is_ghost_session(tools, duration_s)
        raw, normalized, _ = _calculate_compliance_v2(tools, session_type, duration_seconds=duration_s)
        end_reason = "ghost" if ghost else "timeout"
        tools_json = json.dumps(tools)

        db.execute(
            """UPDATE sessions SET
                   ended_at = COALESCE(?, started_at),
                   end_reason = ?,
                   compliance_score = ?,
                   compliance_normalized = ?,
                   session_type = ?,
                   tools_used = ?
               WHERE session_id = ? AND ended_at IS NULL""",
            (ended_at_str, end_reason, raw, normalized, session_type,
             tools_json, sid),
        )

        if not row["project"] and not ghost:
            inferred = _infer_project_from_tools(sid, row["task_hint"])
            if inferred:
                db.execute(
                    "UPDATE sessions SET project = ? WHERE session_id = ? AND project IS NULL",
                    (inferred, sid),
                )
                attributed += 1

        closed += 1
        if ghost:
            ghosts += 1

    db.conn.commit()
    parts = [f"{closed} orphaned sessions closed ({ghosts} ghosts)"]
    if alive_by_pid:
        parts.append(f"{alive_by_pid} kept alive by PID")
    if attributed:
        parts.append(f"{attributed} projects inferred")
    return f"Compliance: {', '.join(parts)}"


# ---------------------------------------------------------------------------
# Provider compliance aggregation
# ---------------------------------------------------------------------------

def _rescore_sessions(db, window_days: int) -> int:
    """Persist a protocol-compliance score for EVERY non-ghost session.

    Correctness fix: ``compliance_score`` was only ever written by
    ``session_report`` and the orphan/timeout closer. Sessions closed via the
    agent-presence cascade (``end_reason='agent_ended'`` — ~89% of claude-code
    sessions) kept a NULL score and were dropped from the average, so the mean
    reflected only the self-selected subset that closed cleanly (calling
    ``session_report`` is itself a compliance point). That inflated the metric
    and hid the exact violations we want to surface.

    This recomputes the score from the persisted ``tools_used`` (maintained for
    all sessions by the telemetry sweep) for every non-ghost session in the
    window. Idempotent — reported sessions recompute to the same value because
    the report bucket only fires when ``session_report`` is genuinely in the
    tool set.

    Empty-tools sessions are disambiguated via the id bridge: joined to the
    transcript index (``agent_sessions``) by ``provider_session_id``. Real work
    that used zero okuro tools (assistant activity > 0) scores 0 — a genuine
    total non-compliance we WANT surfaced. Idle CLIs with no transcript stay
    NULL (excluded). Historical rows predate the bridge (no captured id) and
    stay excluded — the fix is forward-deterministic.
    """
    rows = db.fetchall(f"""
        SELECT session_id, provider, tools_used, started_at, ended_at,
               provider_session_id
        FROM sessions
        WHERE started_at > datetime('now', '-{window_days} days')
    """)

    def _tools_of(row) -> list:
        """Parse tools_used once, so every guard below agrees on 'empty'.

        This helper exists because the two guards disagreed and the bug was
        invisible: the activity lookup tested `not row["tools_used"]` on the
        RAW COLUMN while the scoring loop tested `not tools` on the PARSED
        list. The sweep writes `json.dumps(sorted(...))`, so a session with no
        attributed okuro calls stores the string '[]' — which is FALSY as a
        list but TRUTHY as a string. Such rows were therefore never added to
        `needed`, the activity lookup had no entry for them, and the rescue
        below skipped every one.

        Measured 2026-07-27 before the fix: 1393 of 1393 bridged-but-unscored
        sessions in a 30-day window held exactly '[]'. All of them were traced
        with assistant_count > 0, i.e. real work — precisely the population
        this function was written to surface as score 0. They had been
        excluded since the function shipped, so every published adherence rate
        was computed over the self-selected half that closed via
        session_report (itself a compliance point).
        """
        try:
            return json.loads(row["tools_used"]) if row["tools_used"] else []
        except (json.JSONDecodeError, TypeError):
            return []

    # Activity lookup: assistant turns per native session id, for empty-tools
    # disambiguation. Only pull ids we actually need (bridged, empty-tools).
    needed = {
        r["provider_session_id"] for r in rows
        if r["provider_session_id"] and not _tools_of(r)
    }
    activity: dict[str, int] = {}
    if needed:
        placeholders = ",".join("?" * len(needed))
        for a in db.fetchall(
            f"""SELECT session_id, MAX(assistant_count) AS ac
                FROM agent_sessions WHERE session_id IN ({placeholders})
                GROUP BY session_id""",
            tuple(needed),
        ):
            activity[a["session_id"]] = int(a["ac"] or 0)

    rescored = 0
    for r in rows:
        tools = _tools_of(r)

        # Empty okuro tool set: only scorable when the transcript confirms real
        # work (score 0 = total non-compliance). Without a bridged transcript
        # showing activity, idle CLI and real-non-okuro work are
        # indistinguishable from the usage log alone — leave NULL (excluded).
        if not tools:
            psid = r["provider_session_id"]
            if not (psid and activity.get(psid, 0) > 0):
                continue

        duration_s: float | None = None
        try:
            if r["started_at"] and r["ended_at"]:
                start = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(r["ended_at"].replace("Z", "+00:00"))
                duration_s = (end - start).total_seconds()
        except (ValueError, TypeError, AttributeError):
            duration_s = None

        session_type = _detect_session_type(r["provider"] or "")
        _, normalized, breakdown = _calculate_compliance_v2(tools, session_type, duration_s)
        # /5 score = count of protocol buckets that actually fired (no free points).
        score = sum(breakdown.values()) if breakdown else 0
        db.execute(
            "UPDATE sessions SET compliance_score = ?, compliance_normalized = ? "
            "WHERE session_id = ?",
            (score, normalized, r["session_id"]),
        )
        rescored += 1
    db.conn.commit()
    return rescored


def aggregate_provider_compliance(window_days: int = 30) -> str:
    """Aggregate compliance stats per provider into provider_compliance table."""
    from okuro.db import get_db

    db = get_db()

    # Score every non-ghost session before aggregating so the average reflects
    # all real sessions, not just the ones that closed via session_report.
    _rescore_sessions(db, window_days)

    # Engaged = a protocol score was assigned (non-empty okuro tool set).
    # avg, scored and the per-tool rates all share this one denominator so the
    # columns are cross-readable. `excluded` = sessions with no okuro engagement
    # (the metric's known blind spot), surfaced rather than silently averaged.
    rows = db.fetchall(f"""
        SELECT
            provider,
            COUNT(*) AS total,
            SUM(CASE WHEN compliance_score IS NOT NULL THEN 1 ELSE 0 END) AS scored,
            AVG(compliance_score) AS avg_score,
            AVG(compliance_normalized) AS avg_norm,
            SUM(CASE WHEN compliance_score IS NULL THEN 1 ELSE 0 END) AS ghost_count,
            AVG(quality_score) AS avg_quality,
            SUM(CASE WHEN quality_score IS NOT NULL THEN 1 ELSE 0 END) AS quality_scored
        FROM sessions
        WHERE started_at > datetime('now', '-{window_days} days')
        GROUP BY provider
    """)

    updated = 0
    for row in rows:
        scored = row["scored"] or 0
        if scored == 0:
            continue

        # Calculate per-tool rates from sessions
        tool_rates = _calculate_tool_rates(db, row["provider"], window_days)

        db.execute(
            """INSERT INTO provider_compliance
                   (provider, total_sessions, scored_sessions, avg_score, avg_normalized,
                    bootstrap_rate, report_rate, cortex_rate, memory_rate, progress_rate,
                    avg_quality, quality_scored, last_updated, window_days)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
               ON CONFLICT (provider) DO UPDATE SET
                   total_sessions = excluded.total_sessions,
                   scored_sessions = excluded.scored_sessions,
                   avg_score = excluded.avg_score,
                   avg_normalized = excluded.avg_normalized,
                   bootstrap_rate = excluded.bootstrap_rate,
                   report_rate = excluded.report_rate,
                   cortex_rate = excluded.cortex_rate,
                   memory_rate = excluded.memory_rate,
                   progress_rate = excluded.progress_rate,
                   avg_quality = excluded.avg_quality,
                   quality_scored = excluded.quality_scored,
                   last_updated = datetime('now'),
                   window_days = excluded.window_days""",
            (
                row["provider"], row["total"], row["scored"],
                round(row["avg_score"] or 0, 1), round(row["avg_norm"] or 0, 2),
                tool_rates.get("bootstrap", 0), tool_rates.get("report", 0),
                tool_rates.get("cortex", 0), tool_rates.get("memory", 0),
                tool_rates.get("progress", 0),
                round(row["avg_quality"], 1) if row["avg_quality"] is not None else None,
                row["quality_scored"] or 0, window_days,
            ),
        )
        updated += 1

    db.conn.commit()
    return f"Compliance: {updated} providers aggregated"


def _calculate_tool_rates(db, provider: str, window_days: int) -> dict[str, float]:
    """Per-tool usage rates over ENGAGED sessions (compliance_score IS NOT NULL).

    Same denominator as ``avg_score`` so the scorecard's Avg and per-tool rate
    columns are cross-readable — previously they used different filters.
    """
    rows = db.fetchall(
        f"""SELECT tools_used FROM sessions
            WHERE provider = ? AND started_at > datetime('now', '-{window_days} days')
              AND compliance_score IS NOT NULL""",
        (provider,),
    )
    if not rows:
        return {}

    total = len(rows)
    cortex_tools = {"cortex_search", "cortex_route", "cortex_read_header",
                    "cortex_read_section", "cortex_read_file"}
    counts = {"bootstrap": 0, "report": 0, "cortex": 0, "memory": 0, "progress": 0}

    for r in rows:
        tools = set(json.loads(r["tools_used"])) if r["tools_used"] else set()
        if "bootstrap" in tools:
            counts["bootstrap"] += 1
        if "session_report" in tools:
            counts["report"] += 1
        if tools & cortex_tools:
            counts["cortex"] += 1
        if "write_memory" in tools:
            counts["memory"] += 1
        if "log_progress" in tools:
            counts["progress"] += 1

    return {k: round(100.0 * v / total, 1) for k, v in counts.items()}


def _live_scorecard_rows(db, provider: str | None, days: int,
                         session_type: str | None) -> list[dict]:
    """Compute the rates from `sessions` over the requested window.

    WHY NOT THE AGGREGATE. `provider_compliance` is pre-aggregated at its own
    stored `window_days`, so reading it made the `days` argument INERT for
    every rate: days=7 and days=365 returned identical numbers, and only the
    exclusion footer moved. A parameter that silently governs nothing is worse
    than an absent one — a reader compares two windows, sees the same figures,
    and concludes the behaviour is stable.

    ``session_type`` splits the population. Sessions okuro's own dispatcher
    minted carry 'subagent'; interactive CLI sessions carry 'direct'. Mixing
    them averages a human's session with a machine's.
    """
    where = "WHERE started_at > datetime('now', ?)"
    params: list = [f"-{int(days)} days"]
    if provider:
        where += " AND provider = ?"
        params.append(provider)
    if session_type:
        where += " AND COALESCE(session_type, 'direct') = ?"
        params.append(session_type)

    rows = db.fetchall(
        f"SELECT provider, tools_used, compliance_score, compliance_normalized "
        f"FROM sessions {where}", tuple(params)
    )
    cortex_tools = {"cortex_search", "cortex_route", "cortex_read_header",
                    "cortex_read_section", "cortex_read_file"}
    per: dict[str, dict] = {}
    for r in rows:
        p = per.setdefault(r["provider"] or "unknown", {
            "provider": r["provider"] or "unknown", "total_sessions": 0,
            "scored_sessions": 0, "_score_sum": 0.0, "_norm_sum": 0.0,
            "bootstrap": 0, "report": 0, "cortex": 0, "memory": 0, "progress": 0,
        })
        p["total_sessions"] += 1
        if r["compliance_score"] is None:
            continue
        p["scored_sessions"] += 1
        p["_score_sum"] += float(r["compliance_score"] or 0)
        p["_norm_sum"] += float(r["compliance_normalized"] or 0)
        try:
            tools = set(json.loads(r["tools_used"])) if r["tools_used"] else set()
        except Exception:
            tools = set()
        if "bootstrap" in tools:
            p["bootstrap"] += 1
        if "session_report" in tools:
            p["report"] += 1
        if tools & cortex_tools:
            p["cortex"] += 1
        if "write_memory" in tools:
            p["memory"] += 1
        if "log_progress" in tools:
            p["progress"] += 1

    out = []
    for p in per.values():
        n = p["scored_sessions"] or 0
        pct = (lambda v: round(100.0 * v / n, 1) if n else 0.0)
        out.append({
            "provider": p["provider"],
            "total_sessions": p["total_sessions"],
            "scored_sessions": n,
            "avg_score": round(p["_score_sum"] / n, 1) if n else None,
            "avg_normalized": round(p["_norm_sum"] / n, 3) if n else None,
            "bootstrap_rate": pct(p["bootstrap"]),
            "report_rate": pct(p["report"]),
            "cortex_rate": pct(p["cortex"]),
            "memory_rate": pct(p["memory"]),
            "progress_rate": pct(p["progress"]),
        })
    out.sort(key=lambda d: -d["total_sessions"])
    return out


def compliance_scorecard(
    provider: str | None = None,
    days: int = 30,
    split_population: bool = True,
) -> str:
    """Provider compliance scorecard over the LAST ``days`` days.

    ``split_population`` renders interactive ('direct') and okuro-minted
    ('subagent') sessions as separate blocks instead of one average.
    """
    from okuro.db import get_db

    db = get_db()

    rows = _live_scorecard_rows(db, provider, days, None)
    if not rows:
        return f"No sessions in the last {days} days."

    # Two axes, kept visually separate: protocol ADHERENCE (did the agent follow
    # the tool protocol) and QUALITY (was the work good, judged by a fast-tier
    # LLM over the transcript). Quality columns are blank until the judge runs.
    lines = [f"## Provider Compliance Scorecard — last {days} days\n"]

    def _table(title: str, data: list[dict]) -> None:
        if not data:
            return
        lines.append(f"### {title}\n")
        lines.append("| Provider | Sessions | Scored | Avg | Norm | Bootstrap | Report | Cortex | Memory | Progress |")
        lines.append("|----------|----------|--------|-----|------|-----------|--------|--------|--------|----------|")
        for row in data:
            n = row["scored_sessions"]
            # Denominators inline. A bare "27%" invites a reader to weigh a
            # 3-session provider the same as a 3,000-session one.
            def _r(key: str) -> str:
                return f"{row[key]}% ({round(row[key] * n / 100)}/{n})"
            lines.append(
                f"| {row['provider']} | {row['total_sessions']} | {n} | "
                f"{row['avg_score']} | {row['avg_normalized']} | "
                f"{_r('bootstrap_rate')} | {_r('report_rate')} | {_r('cortex_rate')} | "
                f"{_r('memory_rate')} | {_r('progress_rate')} |"
            )
        lines.append("")

    if split_population:
        _table("Interactive sessions (a human is present)",
               _live_scorecard_rows(db, provider, days, "direct"))
        sub = _live_scorecard_rows(db, provider, days, "subagent")
        if sub:
            _table("okuro-minted subprocesses (session_type='subagent')", sub)
        else:
            lines.append(
                "_No sessions tagged `session_type='subagent'` in this window._\n"
            )
        # STATED, because the split is smaller than it looks. Nested agents a
        # CLIENT spawns INSIDE its own process (Claude Code's Agent tool) carry
        # no okuro environment, so they land in 'direct' alongside humans. The
        # split separates okuro's OWN spawns and nothing else — see
        # mcp_middleware._is_non_interactive_session's stated known gap.
        lines.append(
            "_Split covers okuro-minted spawns only. A client that spawns "
            "nested agents inside its own process passes no okuro environment, "
            "so those sessions are recorded as `direct` and are NOT separated "
            "here._\n"
        )
    else:
        _table("All sessions", rows)

    # Excluded rows are split by CAUSE, computed live, because the previous
    # footer hard-coded a diagnosis and it rotted into a falsehood: it told
    # every reader the blind spot needed "an okuro-session <-> provider-session
    # id bridge" long after that bridge shipped (2026-07-26) — and at the time
    # it was measured, 1401 of the 1515 excluded rows were CORRECTLY BRIDGED.
    # The real cause then was a truthiness bug in _rescore_sessions. A static
    # explanation cannot notice it has stopped being true; a computed one can.
    # Also provider-scoped now: the old query ignored the `provider` filter, so
    # a per-provider scorecard reported the all-provider exclusion count.
    stats = db.fetchone(f"""
        SELECT
            SUM(CASE WHEN s.ended_at IS NULL THEN 1 ELSE 0 END) AS orphaned,
            SUM(CASE WHEN s.compliance_score IS NULL THEN 1 ELSE 0 END) AS excluded,
            SUM(CASE WHEN s.compliance_score IS NULL
                      AND s.provider_session_id IS NULL THEN 1 ELSE 0 END) AS unbridged,
            SUM(CASE WHEN s.compliance_score IS NULL
                      AND s.provider_session_id IS NOT NULL
                      AND COALESCE(a.assistant_count, 0) = 0 THEN 1 ELSE 0 END) AS no_activity,
            SUM(CASE WHEN s.compliance_score IS NULL
                      AND COALESCE(a.assistant_count, 0) > 0 THEN 1 ELSE 0 END) AS scorable
        FROM sessions s
        LEFT JOIN agent_sessions a ON a.session_id = s.provider_session_id
        WHERE 1=1{" AND s.provider = ?" if provider else ""}
          AND s.started_at > datetime('now', '-{days} days')
    """, (provider,) if provider else ())
    if stats:
        if stats.get("orphaned"):
            lines.append(f"\n**Active/orphaned sessions:** {stats['orphaned']}")
        if stats.get("excluded"):
            parts = [
                f"\n**Excluded from the rates:** {stats['excluded']} sessions with no "
                f"okuro tool calls that could not be confirmed as real work."
            ]
            if stats.get("unbridged"):
                parts.append(
                    f"{stats['unbridged']} carry no captured native session id, so they "
                    f"cannot be joined to a transcript — idle CLI and real non-okuro work "
                    f"stay indistinguishable. Close by extending `_PROVIDER_SESSION_ENV` "
                    f"(sense/telemetry.py) for the providers that capture none; check "
                    f"`provider_session_id` coverage per provider before assuming which."
                )
            if stats.get("no_activity"):
                parts.append(
                    f"{stats['no_activity']} are bridged but their transcript shows no "
                    f"assistant turns — genuinely idle, correctly excluded."
                )
            if stats.get("scorable"):
                parts.append(
                    f"**{stats['scorable']} are bridged AND show real transcript activity "
                    f"— these should have been scored 0 and are a BUG, not a blind spot.** "
                    f"Investigate `_rescore_sessions` before trusting any rate above."
                )
            lines.append(" ".join(parts))

    return "\n".join(lines)


def get_provider_compliance(provider: str) -> dict | None:
    """Get compliance stats for a specific provider. Used by bootstrap injection.

    audit(R4 / #13): exposes ``scored_sessions`` (denominator EXCLUDING ghost
    sessions, computed by ``aggregate_provider_compliance``'s SQL) and
    ``window_days`` so the bootstrap renderer can show its math:
    ``"(N sessions over D days)"``. Per-bucket counts are derived as
    ``round(rate/100 * scored_sessions)``.
    """
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        "SELECT avg_normalized, bootstrap_rate, cortex_rate, memory_rate, "
        "report_rate, progress_rate, total_sessions, scored_sessions, window_days "
        "FROM provider_compliance WHERE provider = ?",
        (provider,),
    )
    if not row:
        return None
    return {
        "avg_normalized": float(row["avg_normalized"] or 0),
        "bootstrap_rate": float(row["bootstrap_rate"] or 0),
        "cortex_rate": float(row["cortex_rate"] or 0),
        "memory_rate": float(row["memory_rate"] or 0),
        "report_rate": float(row["report_rate"] or 0),
        "progress_rate": float(row["progress_rate"] or 0),
        "total_sessions": row["total_sessions"] or 0,
        "scored_sessions": row["scored_sessions"] or 0,
        "window_days": row["window_days"] or 30,
    }


def _performance_from_jsonl(
    tool_name: str | None, server: str | None, days: int,
) -> str:
    """Fallback: compute performance from raw jsonl."""
    from collections import Counter
    import time as _time

    cutoff = _time.time() - (days * 86400)
    tool_counts: Counter = Counter()
    tool_sessions: dict[str, set] = {}
    tool_latencies: dict[str, list] = {}

    try:
        if not USAGE_FILE.exists():
            return "No telemetry data yet."
        for line in USAGE_FILE.read_text().splitlines():
            try:
                entry = json.loads(line)
                ts_str = entry.get("ts", "")
                try:
                    dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    if dt.timestamp() < cutoff:
                        continue
                except ValueError:
                    continue

                t = entry.get("tool", "?")
                s = entry.get("server", "?")
                if tool_name and t != tool_name:
                    continue
                if server and s != server:
                    continue

                key = f"{t} ({s})"
                tool_counts[key] += 1
                tool_sessions.setdefault(key, set()).add(entry.get("session", "?"))
                lat = entry.get("latency_ms")
                if lat is not None:
                    tool_latencies.setdefault(key, []).append(lat)
            except (json.JSONDecodeError, KeyError):
                continue
    except OSError:
        return "Error reading telemetry file."

    if not tool_counts:
        return "No matching telemetry data found."

    lines = [f"## Tool Performance (last {days} days, from jsonl)\n"]
    lines.append("| Tool | Calls | Sessions | Avg ms |")
    lines.append("|------|-------|----------|--------|")
    for key, count in tool_counts.most_common(20):
        sessions = len(tool_sessions.get(key, set()))
        lats = tool_latencies.get(key, [])
        avg_lat = round(sum(lats) / len(lats)) if lats else "-"
        lines.append(f"| {key} | {count} | {sessions} | {avg_lat} |")

    return "\n".join(lines)
