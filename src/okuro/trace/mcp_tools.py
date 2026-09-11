# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Trace module MCP tools — session_list, session_trace, trace_search, trace_stats.
# index: imports | _text | _fts_quote | get_tools | handle_tool
# AGENT_HEADER_END -->
"""Trace module MCP tools.

Exposes the raw-trace store (Meta-Harness P1) to agents:

* ``session_list`` — browse recent prior sessions
* ``session_trace`` — walk one session's event stream in order
* ``trace_search`` — FTS5 search across all trace text
* ``trace_stats`` — aggregate the store: counts, adoption rates,
  per-session tool profiles, post-signature routing
* ``trace_index`` — trigger a provider ingest on demand

``trace_search`` finds events; ``trace_stats`` counts them. Without the
latter, any behavioural rate had to be re-derived by shelling into
``~/.okuro/okuro.db`` — the exact bypass okuro exists to remove.
"""

from __future__ import annotations

import json
import logging

from mcp.types import Tool, TextContent

from okuro.clock import SQL_UTC_ISO

logger = logging.getLogger(__name__)


def _text(data) -> list[TextContent]:
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def _fts_quote(query: str) -> str:
    """Wrap a user query as a single FTS5 phrase.

    FTS5 treats hyphens, colons, dots as operators, so ``Meta-Harness``
    parses as ``Meta AND -Harness``. Wrapping in double quotes forces
    phrase match. Embedded quotes are escaped per FTS5 rules (double them).
    """
    escaped = query.replace('"', '""')
    return f'"{escaped}"'


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="session_list",
            description=(
                "List recent prior agent sessions (claude-code, codex, gemini, cursor) "
                "with summary stats. Use to find a session to inspect before calling "
                "session_trace. Backed by the raw-trace store, not compressed memory."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "description": "Filter by provider (claude-code / codex / gemini / cursor)"},
                    "project": {"type": "string", "description": "Filter by project_path prefix (e.g., '~/okuro')"},
                    "since_days": {"type": "integer", "description": "Only sessions with activity in the last N days"},
                    "limit": {"type": "integer", "default": 20, "maximum": 200},
                },
            },
        ),
        Tool(
            name="session_trace",
            description=(
                "Read a prior session's raw event stream in order (user turns, assistant "
                "turns, tool calls, tool results, system events). Use when you need to "
                "diagnose what a prior agent actually did — the full trace the Meta-Harness "
                "paper identifies as the key signal that compressed summaries lose."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Session ID from session_list or trace_search"},
                    "type": {"type": "string", "description": "Filter to one event type: user / assistant / system / progress / tool_result"},
                    "tool": {"type": "string", "description": "Filter to events whose tool_name matches"},
                    "start_ord": {"type": "integer", "description": "Skip events before this ord (for pagination)"},
                    "limit": {"type": "integer", "default": 100, "maximum": 500},
                    "include_content": {"type": "boolean", "default": False, "description": "Return full content_json instead of just text excerpts"},
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="trace_search",
            description=(
                "FTS5 search across all stored agent traces. Finds sessions/events "
                "where the text matched your query. Use to locate prior work on a "
                "topic before repeating analysis. User queries are phrase-quoted "
                "automatically (so hyphens and punctuation work as typed)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search phrase"},
                    "provider": {"type": "string"},
                    "project": {"type": "string", "description": "Filter by project_path prefix"},
                    "type": {"type": "string", "description": "Event type filter"},
                    "since_days": {"type": "integer"},
                    "limit": {"type": "integer", "default": 20, "maximum": 100},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="trace_stats",
            description=(
                "Aggregate the agent-trace store — counts and rates, not hits. "
                "Use INSTEAD of shelling into okuro.db with sqlite whenever you "
                "need a behavioural figure. Modes: 'tool_counts' (how often a "
                "tool was called, grouped by tool/provider/day/project), "
                "'tool_adoption' (share of sessions that called a tool at least "
                "once), 'session_tools' (per-session tool-call profile — finds "
                "the sessions that used one tool heavily and another never), "
                "'after_signature' (what the agent called in the next N tool "
                "calls after events matching a text signature — the post-denial "
                "routing question), 'composition' (WHERE THE BYTES ARE: events "
                "and size by event type x lifecycle tier x age bucket, plus the "
                "text-vs-content_json split — the retention instrument; pass "
                "all_history=true, the 7-day default measures intake instead). "
                "Every result is time-bounded (default 7 "
                "days; set all_history=true to scan ~950k events) and row-capped, "
                "and says so via 'capped'. Read-only. NOTE for signature work: "
                "raw text matching overcounts, because an agent READING a hook's "
                "source produces an event containing the hook's own message. "
                "after_signature classifies every match structurally via its "
                "parent tool call and reports each exclusion class."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["tool_counts", "tool_adoption", "session_tools", "after_signature", "composition"],
                        "description": "Which aggregation to run",
                    },
                    "tool": {"type": "string", "description": "Exact tool name, e.g. 'Bash' or 'mcp__okuro__cortex_search'"},
                    "tool_prefix": {"type": "string", "description": "Match any tool starting with this, e.g. 'mcp__okuro__cortex'"},
                    "provider": {"type": "string", "description": "claude-code / codex / gemini / antigravity"},
                    "project": {"type": "string", "description": "Filter by project_path prefix"},
                    "since_days": {"type": "integer", "default": 7, "description": "Time window in days (default 7)"},
                    "all_history": {"type": "boolean", "default": False, "description": "Opt in to an unbounded scan of the whole store"},
                    "group_by": {"type": "string", "enum": ["tool", "provider", "day", "project"], "default": "tool", "description": "tool_counts only"},
                    "session_id": {"type": "string", "description": "session_tools: restrict to one session"},
                    "without_tool_prefix": {"type": "string", "description": "session_tools: keep only sessions with ZERO calls to tools matching this prefix"},
                    "top_n": {"type": "integer", "default": 8, "maximum": 30, "description": "session_tools: tools listed per session"},
                    "signature": {"type": "string", "description": "after_signature: literal text to match in event text"},
                    "next_n": {"type": "integer", "default": 3, "maximum": 20, "description": "after_signature: how many following tool calls to attribute"},
                    "match_types": {"type": "array", "items": {"type": "string"}, "description": "after_signature: event types treated as live emissions (default ['tool_result'])"},
                    "exclude_source_echo": {"type": "boolean", "default": True, "description": "after_signature: drop matches whose parent tool opened the gate's own source"},
                    "extra_echo_hints": {"type": "array", "items": {"type": "string"}, "description": "after_signature: additional parent-text substrings that mark an echo"},
                    "max_anchors": {"type": "integer", "default": 200, "maximum": 1000, "description": "after_signature: cap on matched events examined"},
                    "limit": {"type": "integer", "default": 20, "description": "Rows returned (capped per mode)"},
                },
                "required": ["mode"],
            },
        ),
        Tool(
            name="trace_index",
            description=(
                "Re-run the transcript ingesters to pick up new sessions. Normally "
                "this runs automatically via the daemon; call this after a long "
                "run to force-refresh without waiting."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "force": {"type": "boolean", "default": False, "description": "Re-index even if source mtime is unchanged"},
                },
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    from okuro.db import get_db
    db = get_db()

    if name == "session_list":
        provider = arguments.get("provider")
        project = arguments.get("project")
        since_days = arguments.get("since_days")
        limit = min(int(arguments.get("limit", 20)), 200)

        clauses: list[str] = []
        params: list = []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if project:
            clauses.append("project_path LIKE ?")
            params.append(f"{project}%")
        if since_days is not None:
            # ISO column, so NOT datetime('now'): agent_sessions.last_ts is
            # 'YYYY-MM-DDTHH:MM:SSZ' while SQLite renders a SPACE separator,
            # and ' ' (0x20) sorts below 'T' (0x54). Every row on the boundary
            # day therefore matched regardless of its time — the window ran up
            # to 24h wide, always inclusive, never visible. Same fix already
            # shipped in trace/stats.py; this site kept the old form.
            clauses.append(f"last_ts >= {SQL_UTC_ISO}")
            params.append(f"-{int(since_days)} days")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        rows = db.fetchall(
            f"""
            SELECT session_id, provider, project_path, git_branch,
                   first_ts, last_ts, message_count, assistant_count,
                   tokens_in, tokens_out, model
            FROM agent_sessions
            {where}
            ORDER BY last_ts DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        return _text({"count": len(rows), "sessions": rows})

    if name == "session_trace":
        session_id = arguments["session_id"]
        type_filter = arguments.get("type")
        tool_filter = arguments.get("tool")
        start_ord = int(arguments.get("start_ord", 0))
        limit = min(int(arguments.get("limit", 100)), 500)
        include_content = bool(arguments.get("include_content", False))

        clauses: list[str] = ["session_id = ?", "ord >= ?"]
        params: list = [session_id, start_ord]
        if type_filter:
            clauses.append("type = ?")
            params.append(type_filter)
        if tool_filter:
            clauses.append("tool_name = ?")
            params.append(tool_filter)

        cols = "uuid, ord, type, role, timestamp, model, tool_name, tokens_in, tokens_out, "
        cols += "content_json AS content" if include_content else "substr(text, 1, 500) AS text"

        rows = db.fetchall(
            f"""
            SELECT {cols}
            FROM agent_events
            WHERE {' AND '.join(clauses)}
            ORDER BY ord ASC
            LIMIT ?
            """,
            (*params, limit),
        )
        if include_content:
            for r in rows:
                try:
                    r["content"] = json.loads(r["content"]) if r.get("content") else None
                except (TypeError, ValueError):
                    pass
        session = db.fetchone(
            "SELECT provider, project_path, first_ts, last_ts, message_count FROM agent_sessions WHERE session_id = ?",
            (session_id,),
        )
        return _text({"session": session, "count": len(rows), "events": rows})

    if name == "trace_search":
        raw_query = arguments.get("query", "").strip()
        if not raw_query:
            return _text({"error": "query is required"})
        fts_query = _fts_quote(raw_query)
        provider = arguments.get("provider")
        project = arguments.get("project")
        type_filter = arguments.get("type")
        since_days = arguments.get("since_days")
        limit = min(int(arguments.get("limit", 20)), 100)

        # Join FTS index with agent_events (for timestamp/tool/type) and
        # agent_sessions (for provider/project filters). rank is FTS5 relevance.
        clauses: list[str] = ["f.agent_events_fts MATCH ?"]
        params: list = [fts_query]
        if type_filter:
            clauses.append("f.type = ?")
            params.append(type_filter)
        if provider:
            clauses.append("s.provider = ?")
            params.append(provider)
        if project:
            clauses.append("s.project_path LIKE ?")
            params.append(f"{project}%")
        if since_days is not None:
            # See the note in session_list: agent_events.timestamp is ISO-8601
            # with a T and a Z on all four providers (verified over 957,825
            # rows), so datetime('now') widens the window by up to a day.
            clauses.append(f"e.timestamp >= {SQL_UTC_ISO}")
            params.append(f"-{int(since_days)} days")

        rows = db.fetchall(
            f"""
            SELECT e.uuid, e.session_id, s.provider, s.project_path,
                   e.type, e.timestamp, e.tool_name,
                   snippet(agent_events_fts, 0, '[[', ']]', '…', 16) AS excerpt,
                   f.rank AS rank
            FROM agent_events_fts f
            JOIN agent_events e ON e.rowid = f.rowid
            JOIN agent_sessions s ON s.session_id = e.session_id
            WHERE {' AND '.join(clauses)}
            ORDER BY rank
            LIMIT ?
            """,
            (*params, limit),
        )
        return _text({"query": raw_query, "count": len(rows), "hits": rows})

    if name == "trace_stats":
        from okuro.trace import stats

        mode = arguments.get("mode")
        common = {
            "provider": arguments.get("provider"),
            "project": arguments.get("project"),
            "since_days": arguments.get("since_days", 7),
            "all_history": bool(arguments.get("all_history", False)),
        }
        if mode == "tool_counts":
            return _text(stats.tool_counts(
                tool=arguments.get("tool"),
                tool_prefix=arguments.get("tool_prefix"),
                group_by=arguments.get("group_by", "tool"),
                limit=arguments.get("limit"),
                **common,
            ))
        if mode == "tool_adoption":
            return _text(stats.tool_adoption(
                tool=arguments.get("tool"),
                tool_prefix=arguments.get("tool_prefix"),
                **common,
            ))
        if mode == "session_tools":
            return _text(stats.session_tools(
                session_id=arguments.get("session_id"),
                tool=arguments.get("tool"),
                without_tool_prefix=arguments.get("without_tool_prefix"),
                limit=arguments.get("limit", 10),
                top_n=arguments.get("top_n"),
                **common,
            ))
        if mode == "after_signature":
            return _text(stats.after_signature(
                signature=arguments.get("signature", ""),
                next_n=arguments.get("next_n"),
                match_types=arguments.get("match_types"),
                exclude_source_echo=bool(arguments.get("exclude_source_echo", True)),
                extra_echo_hints=arguments.get("extra_echo_hints"),
                max_anchors=arguments.get("max_anchors"),
                limit=arguments.get("limit"),
                **common,
            ))
        if mode == "composition":
            return _text(stats.composition(
                limit=arguments.get("limit"),
                **common,
            ))
        return _text({"error": f"unknown mode '{mode}'", "modes": [
            "tool_counts", "tool_adoption", "session_tools", "after_signature",
            "composition",
        ]})

    if name == "trace_index":
        force = bool(arguments.get("force", False))
        # ingest_all() doesn't accept force=, so when the caller asked for
        # force we drive each per-provider ingester directly. Otherwise
        # delegate to ingest_all() so future providers automatically light up
        # without touching this code.
        if force:
            from okuro.trace.claude_code import ingest as ingest_cc
            from okuro.trace.codex import ingest as ingest_codex
            from okuro.trace.gemini import ingest as ingest_gemini
            return _text({
                "claude-code": ingest_cc(force=True),
                "codex": ingest_codex(force=True),
                "gemini": ingest_gemini(force=True),
            })
        from okuro.trace import ingest_all
        return _text(ingest_all())

    raise ValueError(f"trace: unknown tool '{name}'")
