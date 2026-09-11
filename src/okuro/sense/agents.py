# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Agent presence layer — tracks live CLI/orchestrator processes via (provider, host, pid) + heartbeat, independent of work-unit sessions.
# index:
#   def upsert_heartbeat        - per-tool-call stamp, creates agent row on first call
#   def close_dead_agents       - reaper: pid-dead or heartbeat-timeout
#   def get_live_agents         - query live agents for /api/live-agents
# AGENT_HEADER_END -->
"""Agent presence layer.

An **agent** is a long-lived process that speaks to okuro via MCP (a claude-code
CLI, a codex terminal, an orchestrator subprocess, etc.). It outlives any single
work-unit session and can run many of them in sequence.

`sessions` keeps its meaning: one bootstrap → one task → one session_report.
`agents` answers "which processes are alive right now?" which the Live-Agents
panel and pulse view need, and which `sessions.ended_at IS NULL` cannot answer
(the agent stays alive after session_report is called).
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta, timezone


_LIVE_SUFFIX = ":live"


def _now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _my_host() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


def _identity_key(
    provider: str,
    host: str,
    pid: int | None,
    transport: str,
    http_session_id: str | None,
) -> str:
    """Stable per-agent key.

    stdio: (provider, host, pid) uniquely identifies a CLI subprocess.
    http:  shared daemon serves many clients — key off Mcp-Session-Id.
    """
    if transport == "http" and http_session_id:
        return f"{provider}:http:{http_session_id}{_LIVE_SUFFIX}"
    return f"{provider}:{host}:{pid}{_LIVE_SUFFIX}"


def upsert_heartbeat(
    provider: str,
    pid: int | None = None,
    transport: str = "stdio",
    http_session_id: str | None = None,
) -> str | None:
    """Stamp a heartbeat for the current agent. Creates on first call.

    Must never raise — called from the MCP hot path on every tool call.
    Returns agent_id on success, None on error or skipped (provider unknown).
    """
    if not provider or provider == "unknown":
        return None
    if pid is None:
        pid = os.getpid()
    host = _my_host()
    key = _identity_key(provider, host, pid, transport, http_session_id)
    now = _now_utc_str()

    try:
        from okuro.db import get_db
        db = get_db()
        row = db.fetchone(
            "SELECT id, ended_at FROM agents WHERE identity_key = ?",
            (key,),
        )
        if row is None:
            agent_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO agents
                       (id, provider, host, pid, transport, identity_key,
                        started_at, last_heartbeat_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (agent_id, provider, host, pid, transport, key, now, now),
            )
            db.conn.commit()
            return agent_id

        agent_id = row["id"]
        if row["ended_at"] is not None:
            # Pid reuse or recovered agent — reopen.
            db.execute(
                """UPDATE agents
                      SET last_heartbeat_at=?, ended_at=NULL, end_reason=NULL
                    WHERE id=?""",
                (now, agent_id),
            )
        else:
            db.execute(
                "UPDATE agents SET last_heartbeat_at=? WHERE id=?",
                (now, agent_id),
            )
        db.conn.commit()
        return agent_id
    except Exception:
        return None


def get_current_agent_id(
    provider: str,
    pid: int | None = None,
    transport: str = "stdio",
    http_session_id: str | None = None,
) -> str | None:
    """Look up agent_id for the current process without stamping a heartbeat.

    Used by session creation/report to link the session row to its agent.
    Returns None if no row exists yet — the caller should have already
    stamped a heartbeat (which creates the row) before calling this.
    """
    if not provider or provider == "unknown":
        return None
    if pid is None:
        pid = os.getpid()
    host = _my_host()
    key = _identity_key(provider, host, pid, transport, http_session_id)
    try:
        from okuro.db import get_db
        row = get_db().fetchone(
            "SELECT id FROM agents WHERE identity_key = ?",
            (key,),
        )
        return row["id"] if row else None
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # different user but exists
    except OSError:
        return False


def close_dead_agents(heartbeat_timeout_min: int = 30) -> int:
    """Reap agents whose pid is dead or whose heartbeat is stale.

    Called by the maintenance cycle alongside close_orphaned_sessions.
    Returns the number of agents closed.
    """
    from okuro.db import get_db

    db = get_db()
    my_host = _my_host()
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(minutes=heartbeat_timeout_min)

    rows = db.fetchall(
        "SELECT id, host, pid, last_heartbeat_at FROM agents WHERE ended_at IS NULL"
    )
    closed = 0
    for r in rows:
        pid_dead = False
        if r["host"] == my_host and r["pid"]:
            pid_dead = not _pid_alive(r["pid"])

        hb_old = True  # default: if parse fails, treat as stale
        try:
            hb_dt = datetime.fromisoformat(r["last_heartbeat_at"].replace("Z", "+00:00"))
            if hb_dt.tzinfo is None:
                hb_dt = hb_dt.replace(tzinfo=timezone.utc)
            hb_old = hb_dt < cutoff
        except (ValueError, TypeError, AttributeError):
            pass

        if pid_dead or hb_old:
            db.execute(
                """UPDATE agents
                      SET ended_at=?, end_reason=?
                    WHERE id=? AND ended_at IS NULL""",
                (_now_utc_str(),
                 "pid_dead" if pid_dead else "heartbeat_timeout",
                 r["id"]),
            )
            closed += 1

    # Cascade: close any still-open sessions whose agent just died.
    db.execute(
        """UPDATE sessions
              SET ended_at = COALESCE(ended_at, datetime('now')),
                  end_reason = COALESCE(end_reason, 'agent_ended')
            WHERE ended_at IS NULL
              AND agent_id IN (SELECT id FROM agents WHERE ended_at IS NOT NULL)"""
    )
    db.conn.commit()
    return closed


def get_live_agents(limit: int = 50) -> list[dict]:
    """List live agents with their current open session, if any.

    Used by /api/live-agents to drive the pulse view and the "Now" page
    Live-Agents zone.
    """
    from okuro.db import get_db

    db = get_db()
    return db.fetchall(
        """
        SELECT
            a.id,
            a.provider,
            a.host,
            a.pid,
            a.transport,
            a.started_at,
            a.last_heartbeat_at,
            (SELECT s.task_hint FROM sessions s
               WHERE s.agent_id = a.id AND s.ended_at IS NULL
            ORDER BY s.started_at DESC LIMIT 1)          AS current_task_hint,
            (SELECT s.project FROM sessions s
               WHERE s.agent_id = a.id AND s.ended_at IS NULL
            ORDER BY s.started_at DESC LIMIT 1)          AS current_project,
            (SELECT s.session_id FROM sessions s
               WHERE s.agent_id = a.id AND s.ended_at IS NULL
            ORDER BY s.started_at DESC LIMIT 1)          AS current_session_id,
            (SELECT COUNT(*) FROM sessions s WHERE s.agent_id = a.id) AS total_sessions
        FROM agents a
        WHERE a.ended_at IS NULL
        ORDER BY a.last_heartbeat_at DESC
        LIMIT ?
        """,
        (limit,),
    )
