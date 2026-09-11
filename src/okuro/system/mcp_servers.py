# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Enumerate live okuro MCP server subprocesses and flag orphans / stale ones.
# index: imports | def _commits_after | def find_mcp_servers | def format_server_report
# AGENT_HEADER_END -->
"""Orphan / stale MCP-server detection.

Each agent session spawns its own ``python -m okuro.mcp.server`` subprocess as
a child of its client (claude / cursor / codex / an Electron desktop app).
Two failure modes leave one of these running long after it should be gone, both
serving code frozen at its boot (see system/code_version.py):

  - ORPHAN — the client exited without reaping the server, so the process is
    reparented to init (ppid == 1). Measured 2026-07-19: a windowless
    claude-desktop held a 25h-old server nobody knew was running.
  - STALE — the client is alive but the server has drifted many commits behind
    HEAD, or is simply very old. It answers tool calls with code no longer on
    disk, invisibly (the strict-freshness gate now blocks these per session,
    but only once the agent makes a call — a truly idle orphan is never gated).

This module only DETECTS and reports. It never kills: an MCP server may belong
to a live session on another terminal, and reaping the wrong one drops that
agent mid-task. Reaping is a deliberate, opt-in action for a human or a future
gated task, not a side effect of a scan.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Optional

# Only the importable package can change what a running server executes — same
# subtree code_version watches.
from okuro.system.code_version import _repo_root

_SERVER_CMD_MARKER = "okuro.mcp.server"
# A server older than this with commits behind is stale enough to flag even if
# its client is still alive — nobody restarts a window they forgot is open.
_STALE_AGE_HOURS = 24.0


def _commits_after(root, started: float) -> int:
    """Commits that landed after ``started`` — the count this process cannot
    have loaded. 0 on any git failure (never invent drift)."""
    if root is None:
        return 0
    since = datetime.fromtimestamp(started, timezone.utc).isoformat()
    try:
        proc = subprocess.run(
            ["git", "log", f"--since={since}", "--pretty=%h"],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if proc.returncode != 0:
        return 0
    return len([ln for ln in proc.stdout.splitlines() if ln.strip()])


def find_mcp_servers(stale_age_hours: float = _STALE_AGE_HOURS,
                     now_ts: Optional[float] = None) -> list[dict]:
    """Every live okuro MCP server subprocess, classified.

    Returns one dict per process:
      pid, ppid, age_hours, parent_name, parent_alive, commits_behind,
      classification ∈ {'orphan','stale','healthy'}.

    Classification:
      orphan  — reparented to init (ppid == 1): the client is gone.
      stale   — client alive but commits_behind > 0 and older than
                `stale_age_hours` (an old server serving superseded code).
      healthy — everything else.

    Never raises: a monitoring scan that can crash is worse than no scan.
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return []

    root = _repo_root()
    now = now_ts if now_ts is not None else _now()
    out: list[dict] = []

    for proc in psutil.process_iter(["pid", "ppid", "cmdline", "create_time"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if not any(_SERVER_CMD_MARKER in part for part in cmdline):
                continue
            pid = proc.info["pid"]
            ppid = proc.info.get("ppid") or 0
            created = proc.info.get("create_time") or now
            age_hours = max(0.0, (now - created) / 3600.0)

            parent_alive = ppid > 1 and psutil.pid_exists(ppid)
            parent_name = None
            if parent_alive:
                try:
                    parent_name = psutil.Process(ppid).name()
                except Exception:  # noqa: BLE001
                    parent_name = None

            behind = _commits_after(root, created)

            if ppid <= 1 or not parent_alive:
                cls = "orphan"
            elif behind > 0 and age_hours >= stale_age_hours:
                cls = "stale"
            else:
                cls = "healthy"

            out.append({
                "pid": pid,
                "ppid": ppid,
                "age_hours": round(age_hours, 1),
                "parent_name": parent_name,
                "parent_alive": parent_alive,
                "commits_behind": behind,
                "classification": cls,
            })
        except Exception:  # noqa: BLE001 — one bad process must not kill the scan
            continue

    return out


def _now() -> float:
    import time
    return time.time()


def format_server_report(servers: list[dict]) -> str:
    """Human-readable one-line-per-server report, worst first."""
    if not servers:
        return "No okuro MCP servers running."
    order = {"orphan": 0, "stale": 1, "healthy": 2}
    rows = sorted(servers, key=lambda s: order.get(s["classification"], 3))
    lines = []
    for s in rows:
        parent = s["parent_name"] or ("dead" if not s["parent_alive"] else "?")
        behind = f", {s['commits_behind']} commits behind" if s["commits_behind"] else ""
        lines.append(
            f"[{s['classification']}] pid={s['pid']} age={s['age_hours']}h "
            f"parent={parent}{behind}"
        )
    return "\n".join(lines)
