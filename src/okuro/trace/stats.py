# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Aggregation over the raw-trace store — counts, rates, post-signature routing.
# index: helpers | tool_counts | tool_adoption | session_tools | after_signature | composition | instruction_cohort
# AGENT_HEADER_END -->
"""Aggregation over ``agent_events`` / ``agent_sessions``.

``trace_search`` finds events; this module *counts* them. It exists so
behavioural questions ("how often was Grep called", "what share of
sessions ever call cortex", "what did the agent do right after a hook
denial") are answerable through okuro tools instead of shelling into
``~/.okuro/okuro.db`` with sqlite.

Three invariants hold for every function here:

* **Read-only.** Only ``SELECT`` runs. Nothing writes to the store.
* **Bounded in time.** Every query carries a window; full history needs
  an explicit ``all_history=True`` opt-in (~950k events as of 07-2026).
* **Bounded in tokens.** Result lists are capped and the payload says so
  via ``capped``/``anchors_capped``, so a caller never mistakes a
  truncated list for the whole answer.

Counting semantics — what "a tool call" is
------------------------------------------
A tool call is an ``agent_events`` row with ``tool_name IS NOT NULL``.
The ingesters denormalize the *first* ``tool_use`` block of an assistant
event into ``tool_name`` (``claude_code._flatten_content``), so an
assistant turn emitting several tool_use blocks at once is counted once.
Measured on the live store 2026-07-27: 50 such events out of 296,836
(0.017%), 0 in the trailing 7 days. Counts are therefore exact to within
that margin; ``counting_note`` in every payload says so.

The false-heartbeat trap
------------------------
Counting rows whose ``text`` contains a hook's message OVERCOUNTS: an
agent *reading* the hook's source, or searching the corpus, produces an
event carrying the same text. A naive count once reported 7 grep-gate
"denials" that were all agents opening ``check-grep.py``.
:func:`after_signature` therefore classifies every raw match
structurally — via its parent tool_use — and reports each exclusion
class rather than returning a bare number.
"""

from __future__ import annotations

# Tools whose OUTPUT is stored text from the corpus itself. A signature
# appearing in one of their results is an echo of the record, never a
# live emission. Names are compared after stripping any ``mcp__<srv>__``.
_CORPUS_ECHO_TOOLS = frozenset({
    "trace_search", "session_trace", "trace_stats", "session_list",
    "transcript_search", "session_history", "list_tool_invocations",
    "artifact_get", "artifact_search", "artifact_list",
    "read_memory", "role_diary_read", "role_diary_search",
    "note_get", "note_search", "bootstrap", "brain_advise",
    "cortex_read_file", "cortex_read_section", "cortex_read_header",
    "cortex_search", "cortex_search_code", "cortex_route",
    "Grep", "Glob", "Agent", "Task", "WebFetch",
})

# Substrings that, seen in the PARENT tool_use input, mean the agent was
# opening the gate's own source — the classic false heartbeat.
_SOURCE_ECHO_HINTS = (
    "check-read.py", "check-grep.py", "session-compliance.py",
    "profile-compliance.py", "user-prompt-profile.py", "/hooks/",
)

# Event types where a hook message is a genuine emission (it reaches the
# agent as a tool result). assistant/user rows carrying the same text are
# the agent quoting it.
_DEFAULT_MATCH_TYPES = ("tool_result",)

_COUNTING_NOTE = (
    "A tool call = agent_events row with tool_name set (first tool_use block "
    "per assistant event). 50 of 296,836 events in full history carry >1 block, "
    "so counts are exact to ~0.02%."
)

_GROUP_COLUMNS = {
    "tool": "e.tool_name",
    "provider": "s.provider",
    "day": "substr(e.timestamp, 1, 10)",
    "project": "s.project_path",
}


def _cap(value, default: int, maximum: int) -> int:
    """Clamp a caller-supplied limit into [1, maximum]."""
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, maximum))


def _like_contains(needle: str) -> str:
    """Build a LIKE pattern matching ``needle`` literally anywhere."""
    esc = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _like_prefix(prefix: str) -> str:
    """Build a LIKE pattern matching anything starting with ``prefix``."""
    esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{esc}%"


def _bare_tool(name: str | None) -> str:
    """``mcp__okuro__cortex_search`` -> ``cortex_search``; else unchanged."""
    if not name:
        return ""
    return name.split("__")[-1] if name.startswith("mcp__") else name


# Stored timestamps are ISO-8601 UTC with a 'T' separator and 'Z' suffix on
# every provider (verified across claude-code / codex / gemini / antigravity,
# 957k rows). SQLite's datetime() renders a SPACE separator, and ' ' < 'T', so
# comparing against datetime('now', ...) silently widens the window by up to a
# day. strftime with the stored layout compares correctly and stays a constant
# on the right-hand side, so idx_agent_events_timestamp is still usable.
_TS_THRESHOLD = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)"


def _window(since_days, all_history: bool) -> tuple[list[str], list, dict]:
    """Time-bound clause plus the window descriptor echoed in every payload."""
    if all_history:
        return [], [], {"all_history": True, "since_days": None}
    days = _cap(since_days, 7, 3650)
    return (
        [f"e.timestamp >= {_TS_THRESHOLD}"],
        [f"-{days} days"],
        {"all_history": False, "since_days": days},
    )


def _scope(provider, project) -> tuple[list[str], list, dict]:
    """Provider / project-path filters (require the agent_sessions join)."""
    clauses: list[str] = []
    params: list = []
    if provider:
        clauses.append("s.provider = ?")
        params.append(provider)
    if project:
        clauses.append("s.project_path LIKE ?")
        params.append(f"{project}%")
    return clauses, params, {"provider": provider, "project": project}


def _like_suffix(suffix: str) -> str:
    """Build a LIKE pattern matching anything ending with ``suffix``."""
    esc = suffix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}"


def _tool_filter(tool, tool_prefix, *, match_bare: bool = False) -> tuple[list[str], list]:
    """Filter clauses for a tool name.

    ``match_bare`` makes the match CROSS-PROVIDER. The same okuro tool is
    stored under a different name by every front-end: measured on the live
    store 2026-09-17, ``bootstrap`` appears as ``mcp__okuro__bootstrap``
    (claude-code, 5,263 events), bare ``bootstrap`` (codex, 140),
    ``mcp__okuro-sense__bootstrap`` (16) and two one-off spellings. An exact
    filter therefore answers for ONE provider and returns a confident zero
    for the others — which is indistinguishable from a provider that never
    called the tool, and is exactly the reading a cross-provider comparison
    must not make.

    The bare match accepts the name itself or any ``mcp__<server>__<name>``
    form. It does NOT accept ``<name>_<something>``: ``bootstrap`` must not
    quietly count ``bootstrap_project``, and the underscore is escaped so
    LIKE's single-character wildcard cannot make it.
    """
    clauses: list[str] = []
    params: list = []
    if tool:
        if match_bare:
            bare = _bare_tool(tool)
            clauses.append(
                "(e.tool_name = ? OR e.tool_name LIKE ? ESCAPE '\\')"
            )
            params.extend([bare, _like_suffix(f"__{bare}")])
        else:
            clauses.append("e.tool_name = ?")
            params.append(tool)
    if tool_prefix:
        clauses.append("e.tool_name LIKE ? ESCAPE '\\'")
        params.append(_like_prefix(tool_prefix))
    return clauses, params


def tool_counts(
    *,
    tool: str | None = None,
    tool_prefix: str | None = None,
    provider: str | None = None,
    project: str | None = None,
    since_days: int | None = 7,
    all_history: bool = False,
    group_by: str = "tool",
    limit: int | None = 20,
) -> dict:
    """How many times was each tool called, in a window, optionally grouped.

    Answers "how often was Grep called last week, per provider".
    """
    from okuro.db import get_db

    db = get_db()
    if group_by not in _GROUP_COLUMNS:
        return {"error": f"group_by must be one of {sorted(_GROUP_COLUMNS)}"}
    limit_n = _cap(limit, 20, 200)

    wc, wp, window = _window(since_days, all_history)
    sc, sp, scope = _scope(provider, project)
    tc, tp = _tool_filter(tool, tool_prefix)

    clauses = ["e.tool_name IS NOT NULL", *wc, *sc, *tc]
    params = [*wp, *sp, *tp]
    where = " AND ".join(clauses)
    col = _GROUP_COLUMNS[group_by]

    total = db.fetchone(
        f"""
        SELECT COUNT(*) AS calls, COUNT(DISTINCT e.session_id) AS sessions
        FROM agent_events e JOIN agent_sessions s ON s.session_id = e.session_id
        WHERE {where}
        """,
        tuple(params),
    ) or {"calls": 0, "sessions": 0}

    rows = db.fetchall(
        f"""
        SELECT {col} AS key, COUNT(*) AS calls,
               COUNT(DISTINCT e.session_id) AS sessions
        FROM agent_events e JOIN agent_sessions s ON s.session_id = e.session_id
        WHERE {where}
        GROUP BY {col}
        ORDER BY calls DESC
        LIMIT ?
        """,
        (*params, limit_n + 1),
    )
    capped = len(rows) > limit_n
    rows = rows[:limit_n]

    return {
        "mode": "tool_counts",
        "window": window,
        "filters": {**scope, "tool": tool, "tool_prefix": tool_prefix},
        "group_by": group_by,
        "total_calls": total["calls"],
        "total_sessions": total["sessions"],
        "groups": rows,
        "returned": len(rows),
        "capped": capped,
        "counting_note": _COUNTING_NOTE,
    }


def tool_adoption(
    *,
    tool: str | None = None,
    tool_prefix: str | None = None,
    provider: str | None = None,
    project: str | None = None,
    since_days: int | None = 7,
    all_history: bool = False,
    match_bare: bool = False,
) -> dict:
    """What share of sessions called this tool at least once.

    Denominator is sessions that made >=1 tool call in the window — a
    session that never called any tool cannot be said to have skipped one.

    Pass ``match_bare=True`` for any comparison ACROSS providers: the same
    tool is stored under a different name per front-end and an exact filter
    reports a confident zero for every provider but the one it was spelled
    for. See :func:`_tool_filter`.
    """
    from okuro.db import get_db

    db = get_db()
    if not tool and not tool_prefix:
        return {"error": "tool or tool_prefix is required"}

    wc, wp, window = _window(since_days, all_history)
    sc, sp, scope = _scope(provider, project)
    tc, tp = _tool_filter(tool, tool_prefix, match_bare=match_bare)

    base = ["e.tool_name IS NOT NULL", *wc, *sc]
    base_params = [*wp, *sp]

    denom_rows = db.fetchall(
        f"""
        SELECT s.provider AS provider, COUNT(DISTINCT e.session_id) AS sessions
        FROM agent_events e JOIN agent_sessions s ON s.session_id = e.session_id
        WHERE {' AND '.join(base)}
        GROUP BY s.provider
        """,
        tuple(base_params),
    )
    num_rows = db.fetchall(
        f"""
        SELECT s.provider AS provider, COUNT(DISTINCT e.session_id) AS sessions
        FROM agent_events e JOIN agent_sessions s ON s.session_id = e.session_id
        WHERE {' AND '.join([*base, *tc])}
        GROUP BY s.provider
        """,
        (*base_params, *tp),
    )
    num = {r["provider"]: r["sessions"] for r in num_rows}

    by_provider = []
    total_d = total_n = 0
    for r in sorted(denom_rows, key=lambda x: -x["sessions"]):
        d = r["sessions"]
        n = num.get(r["provider"], 0)
        total_d += d
        total_n += n
        by_provider.append({
            "provider": r["provider"],
            "sessions_total": d,
            "sessions_with_tool": n,
            "adoption_rate": round(n / d, 4) if d else None,
        })

    return {
        "mode": "tool_adoption",
        "window": window,
        "filters": {
            **scope,
            "tool": tool,
            "tool_prefix": tool_prefix,
            "match_bare": match_bare,
        },
        "denominator": "sessions with >=1 tool call in window",
        "sessions_total": total_d,
        "sessions_with_tool": total_n,
        "adoption_rate": round(total_n / total_d, 4) if total_d else None,
        "by_provider": by_provider,
        "counting_note": _COUNTING_NOTE,
    }


def session_tools(
    *,
    session_id: str | None = None,
    tool: str | None = None,
    without_tool_prefix: str | None = None,
    provider: str | None = None,
    project: str | None = None,
    since_days: int | None = 7,
    all_history: bool = False,
    limit: int | None = 10,
    top_n: int | None = 8,
) -> dict:
    """Per-session tool-call counts — reproduces "187 Bash / 0 cortex".

    ``tool`` ranks sessions by that tool's count instead of the total.
    ``without_tool_prefix`` keeps only sessions with ZERO calls to any
    tool matching the prefix, which is the adoption-gap question.
    """
    from okuro.db import get_db

    db = get_db()
    limit_n = _cap(limit, 10, 50)
    top = _cap(top_n, 8, 30)

    wc, wp, window = _window(since_days, all_history)
    sc, sp, scope = _scope(provider, project)

    clauses = ["e.tool_name IS NOT NULL", *wc, *sc]
    params: list = [*wp, *sp]
    if session_id:
        clauses.append("e.session_id = ?")
        params.append(session_id)

    having: list[str] = []
    having_params: list = []
    focus_expr = "0"
    if tool:
        focus_expr = "SUM(CASE WHEN e.tool_name = ? THEN 1 ELSE 0 END)"
    if without_tool_prefix:
        having.append(
            "SUM(CASE WHEN e.tool_name LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END) = 0"
        )
        having_params.append(_like_prefix(without_tool_prefix))

    # focus_expr binds in the SELECT list, so its param must lead.
    select_params = [tool] if tool else []
    where_params = list(params)
    order = "focus_calls DESC, total_calls DESC" if tool else "total_calls DESC"

    rows = db.fetchall(
        f"""
        SELECT e.session_id AS session_id,
               COUNT(*) AS total_calls,
               {focus_expr} AS focus_calls
        FROM agent_events e JOIN agent_sessions s ON s.session_id = e.session_id
        WHERE {' AND '.join(clauses)}
        GROUP BY e.session_id
        {('HAVING ' + ' AND '.join(having)) if having else ''}
        ORDER BY {order}
        LIMIT ?
        """,
        (*select_params, *where_params, *having_params, limit_n + 1),
    )
    capped = len(rows) > limit_n
    rows = rows[:limit_n]
    ids = [r["session_id"] for r in rows]

    breakdown: dict[str, list[dict]] = {sid: [] for sid in ids}
    meta: dict[str, dict] = {}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        for r in db.fetchall(
            f"""
            SELECT session_id, tool_name, COUNT(*) AS calls
            FROM agent_events
            WHERE session_id IN ({placeholders}) AND tool_name IS NOT NULL
                  {'' if all_history else f'AND timestamp >= {_TS_THRESHOLD}'}
            GROUP BY session_id, tool_name
            ORDER BY calls DESC
            """,
            (*ids, *([] if all_history else wp)),
        ):
            bucket = breakdown.setdefault(r["session_id"], [])
            if len(bucket) < top:
                bucket.append({"tool": r["tool_name"], "calls": r["calls"]})
        for r in db.fetchall(
            f"""
            SELECT session_id, provider, project_path, git_branch, first_ts, last_ts
            FROM agent_sessions WHERE session_id IN ({placeholders})
            """,
            tuple(ids),
        ):
            meta[r["session_id"]] = r

    sessions = []
    for r in rows:
        sid = r["session_id"]
        m = meta.get(sid, {})
        entry = {
            "session_id": sid,
            "provider": m.get("provider"),
            "project_path": m.get("project_path"),
            "first_ts": m.get("first_ts"),
            "last_ts": m.get("last_ts"),
            "total_calls": r["total_calls"],
            "top_tools": breakdown.get(sid, []),
            "distinct_tools_shown": len(breakdown.get(sid, [])),
        }
        if tool:
            entry["focus_tool"] = tool
            entry["focus_calls"] = r["focus_calls"]
        sessions.append(entry)

    return {
        "mode": "session_tools",
        "window": window,
        "filters": {
            **scope,
            "session_id": session_id,
            "tool": tool,
            "without_tool_prefix": without_tool_prefix,
        },
        "sessions": sessions,
        "returned": len(sessions),
        "capped": capped,
        "top_tools_per_session": top,
        "counting_note": _COUNTING_NOTE,
    }


def after_signature(
    *,
    signature: str,
    next_n: int | None = 3,
    match_types: tuple[str, ...] | list[str] | None = None,
    exclude_source_echo: bool = True,
    extra_echo_hints: list[str] | None = None,
    provider: str | None = None,
    project: str | None = None,
    since_days: int | None = 7,
    all_history: bool = False,
    max_anchors: int | None = 200,
    limit: int | None = 20,
) -> dict:
    """What did the agent call in the next N tool calls after a signature?

    This is the post-denial routing question. Raw text matching is not
    enough: agents that READ a gate's source, or search the corpus for
    its message, produce events carrying the same text. Every raw match
    is classified via its parent tool_use and the exclusion classes are
    reported, so the caller sees what was thrown away and why.
    """
    from okuro.db import get_db

    db = get_db()
    sig = (signature or "").strip()
    if not sig:
        return {"error": "signature is required"}

    types = tuple(match_types) if match_types else _DEFAULT_MATCH_TYPES
    hints = tuple(h.lower() for h in (_SOURCE_ECHO_HINTS if exclude_source_echo else ()))
    hints += tuple(h.lower() for h in (extra_echo_hints or ()))
    anchor_cap = _cap(max_anchors, 200, 1000)
    limit_n = _cap(limit, 20, 100)
    n_follow = _cap(next_n, 3, 20)

    wc, wp, window = _window(since_days, all_history)
    sc, sp, scope = _scope(provider, project)
    clauses = ["e.text LIKE ? ESCAPE '\\'", *wc, *sc]
    params = [_like_contains(sig), *wp, *sp]

    raw = db.fetchall(
        f"""
        SELECT e.uuid, e.session_id, e.ord, e.type, e.timestamp,
               p.tool_name AS parent_tool, substr(p.text, 1, 400) AS parent_text
        FROM agent_events e
        JOIN agent_sessions s ON s.session_id = e.session_id
        LEFT JOIN agent_events p ON p.uuid = e.parent_uuid
        WHERE {' AND '.join(clauses)}
        ORDER BY e.timestamp DESC
        LIMIT ?
        """,
        (*params, anchor_cap + 1),
    )
    anchors_capped = len(raw) > anchor_cap
    raw = raw[:anchor_cap]

    excluded = {"agent_authored": 0, "corpus_echo": 0, "source_echo": 0, "no_parent": 0}
    kept: list[dict] = []
    for r in raw:
        if r["type"] not in types:
            excluded["agent_authored"] += 1
            continue
        if not r["parent_tool"]:
            excluded["no_parent"] += 1
            continue
        if _bare_tool(r["parent_tool"]) in _CORPUS_ECHO_TOOLS:
            excluded["corpus_echo"] += 1
            continue
        ptext = (r["parent_text"] or "").lower()
        if any(h in ptext for h in hints):
            excluded["source_echo"] += 1
            continue
        kept.append(r)

    immediate: dict[str, int] = {}
    within: dict[str, int] = {}
    no_follow = 0
    for a in kept:
        follows = db.fetchall(
            """
            SELECT tool_name FROM agent_events
            WHERE session_id = ? AND ord > ? AND tool_name IS NOT NULL
            ORDER BY ord ASC LIMIT ?
            """,
            (a["session_id"], a["ord"], n_follow),
        )
        if not follows:
            no_follow += 1
            continue
        immediate[follows[0]["tool_name"]] = immediate.get(follows[0]["tool_name"], 0) + 1
        for f in follows:
            within[f["tool_name"]] = within.get(f["tool_name"], 0) + 1

    def _rank(counts: dict[str, int], denom: int) -> list[dict]:
        ordered = sorted(counts.items(), key=lambda kv: -kv[1])[:limit_n]
        return [
            {"tool": t, "calls": c, "share": round(c / denom, 4) if denom else None}
            for t, c in ordered
        ]

    followed = len(kept) - no_follow
    return {
        "mode": "after_signature",
        "window": window,
        "signature": sig,
        "filters": {**scope, "match_types": list(types), "next_n": n_follow},
        "match_summary": {
            "raw_matches": len(raw),
            "kept_anchors": len(kept),
            "excluded": excluded,
            "anchors_with_no_following_call": no_follow,
        },
        "anchors_capped": anchors_capped,
        "immediate_next": _rank(immediate, followed),
        "within_next_n": _rank(within, sum(within.values())),
        "distinct_tools_immediate": len(immediate),
        "distinct_tools_within": len(within),
        "capped": len(within) > limit_n or len(immediate) > limit_n,
        "echo_guard": (
            "Raw text matches are classified by parent tool_use. "
            "agent_authored = the agent wrote/quoted the text; corpus_echo = a "
            "search/read tool returned it from the store; source_echo = the "
            "parent opened the gate's own source file. Only unexplained "
            "tool_result rows are treated as live emissions."
        ),
        "counting_note": _COUNTING_NOTE,
    }


# ---------------------------------------------------------------------------
# composition — where the bytes actually are
# ---------------------------------------------------------------------------
# Age buckets deliberately mirror lifecycle.TIERS, so a row here answers "how
# much would the compactor take at this tier" without a second mental model.
# The tier column comes from trace_lifecycle (assigned state); the age bucket
# is recomputed from the event timestamp (observed age). They disagree when
# re-tiering has not run, and seeing both is the point.
_AGE_BUCKETS: tuple[tuple[str, int], ...] = (
    ("0-30d", 30),
    ("30-90d", 90),
    ("90-180d", 180),
)
_OLDEST_BUCKET = "180d+"

_MEASUREMENT_NOTE = (
    "Sizes are SQLite LENGTH() over text and content_json — characters, not "
    "octets, so non-ASCII content weighs more on disk than reported. This is "
    "the same measure lifecycle.compact_traces uses for bytes_reclaimed, "
    "chosen for comparability with the compactor rather than for absolute "
    "accuracy. Row overhead, indexes and the FTS shadow tables are excluded: "
    "this measures content, not file size."
)


def composition(
    *,
    provider: str | None = None,
    project: str | None = None,
    since_days: int | None = 7,
    all_history: bool = False,
    limit: int | None = 100,
) -> dict:
    """Where the trace store's bytes are: type x lifecycle tier x age bucket.

    The retention instrument. Row counts alone cannot answer "what would
    compaction reclaim" — 72.7% of rows are non-dialogue but the byte mass is
    concentrated in tool_result, and the split between ``text`` and
    ``content_json`` decides whether nulling one column is worth anything.

    Pass ``all_history=True`` for the corpus-wide picture; the 7-day default
    inherited from the other modes describes the intake rate instead, which is
    a different and much smaller question.
    """
    from okuro.db import get_db

    db = get_db()
    # Group cardinality is bounded (types x tiers x buckets, ~120), so the
    # default is set above it: the rollups below sum the RETURNED groups, and
    # a cap would make them quietly disagree with `totals`.
    limit_n = _cap(limit, 200, 500)

    wc, wp, window = _window(since_days, all_history)
    sc, sp, scope = _scope(provider, project)

    # Only pay for the agent_sessions join when a scope filter needs it — this
    # scans ~950k rows and the join is pure overhead otherwise.
    join = ""
    if sc:
        join = "JOIN agent_sessions s ON s.session_id = e.session_id"

    # strftime with the stored ISO layout, never datetime(): ' ' < 'T' makes
    # the naive form silently one day wide (see _TS_THRESHOLD).
    case_parts = []
    bucket_params: list = []
    for label, days in _AGE_BUCKETS:
        case_parts.append(f"WHEN e.timestamp >= {_TS_THRESHOLD} THEN '{label}'")
        bucket_params.append(f"-{days} days")
    age_case = "CASE " + " ".join(case_parts) + f" ELSE '{_OLDEST_BUCKET}' END"

    clauses = [*wc, *sc]
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = db.fetchall(
        f"""
        SELECT e.type                              AS type,
               COALESCE(l.tier, 'untiered')        AS tier,
               {age_case}                          AS age_bucket,
               COUNT(*)                            AS events,
               COALESCE(SUM(LENGTH(COALESCE(e.text, ''))), 0)          AS text_bytes,
               COALESCE(SUM(LENGTH(COALESCE(e.content_json, ''))), 0)  AS json_bytes,
               SUM(CASE WHEN e.text IS NULL AND e.content_json IS NULL
                        THEN 1 ELSE 0 END)         AS empty_events
        FROM agent_events e
        {join}
        LEFT JOIN trace_lifecycle l ON l.native_session_id = e.session_id
        {where}
        GROUP BY e.type, COALESCE(l.tier, 'untiered'), {age_case}
        ORDER BY (text_bytes + json_bytes) DESC
        LIMIT ?
        """,
        (*bucket_params, *wp, *sp, *bucket_params, limit_n + 1),
    )
    capped = len(rows) > limit_n
    rows = rows[:limit_n]

    for r in rows:
        r["bytes"] = (r["text_bytes"] or 0) + (r["json_bytes"] or 0)

    totals = db.fetchone(
        f"""
        SELECT COUNT(*)                            AS events,
               COALESCE(SUM(LENGTH(COALESCE(e.text, ''))), 0)          AS text_bytes,
               COALESCE(SUM(LENGTH(COALESCE(e.content_json, ''))), 0)  AS json_bytes,
               SUM(CASE WHEN e.text IS NULL AND e.content_json IS NULL
                        THEN 1 ELSE 0 END)         AS empty_events
        FROM agent_events e
        {join}
        {where}
        """,
        (*wp, *sp),
    ) or {}

    text_b = totals.get("text_bytes") or 0
    json_b = totals.get("json_bytes") or 0
    total_b = text_b + json_b

    def _roll(key: str) -> list[dict]:
        acc: dict[str, dict] = {}
        for r in rows:
            slot = acc.setdefault(
                r[key], {key: r[key], "events": 0, "text_bytes": 0,
                         "json_bytes": 0, "bytes": 0}
            )
            slot["events"] += r["events"]
            slot["text_bytes"] += r["text_bytes"] or 0
            slot["json_bytes"] += r["json_bytes"] or 0
            slot["bytes"] += r["bytes"]
        return sorted(acc.values(), key=lambda d: -d["bytes"])

    return {
        "mode": "composition",
        "window": window,
        "filters": scope,
        "totals": {
            "events": totals.get("events", 0),
            "empty_events": totals.get("empty_events", 0) or 0,
            "text_bytes": text_b,
            "json_bytes": json_b,
            "bytes": total_b,
            "mb": round(total_b / 1048576.0, 1),
            "text_share": round(text_b / total_b, 4) if total_b else None,
            "json_share": round(json_b / total_b, 4) if total_b else None,
        },
        "by_type": _roll("type"),
        "by_tier": _roll("tier"),
        "by_age_bucket": _roll("age_bucket"),
        "groups": rows,
        "returned": len(rows),
        "capped": capped,
        "rollup_scope": (
            "returned groups only — raise limit for complete rollups"
            if capped else "all groups"
        ),
        "measurement_note": _MEASUREMENT_NOTE,
    }


# ---------------------------------------------------------------------------
# instruction_cohort — the denominator fix
# ---------------------------------------------------------------------------

_COHORT_NOTE = (
    "A session joins a cohort only when the instruction bundle live at its "
    "FIRST event and at its LAST are the same — boundaries are totally "
    "ordered in time, so that equality is proof no change was crossed, not a "
    "sample. A session whose ends disagree is reported under 'spanning' and "
    "belongs to no cohort: it received both versions. Sessions starting "
    "before the ledger's earliest row for their provider are reported under "
    "bundle=null. Neither bucket may be pooled with an attributed cohort — a "
    "rate over a mixed cohort is the defect this mode exists to end."
)


def instruction_cohort(
    *,
    tool: str | None = None,
    provider: str | None = None,
    project: str | None = None,
    since_days: int | None = 30,
    all_history: bool = False,
    session_kind: str | None = None,
    limit: int = 20,
) -> dict:
    """Sessions grouped by the instruction version in force for their whole run.

    THE QUESTION THIS ANSWERS, AND WHY THE OTHER MODES CANNOT. Every rate in
    this module pools sessions over a time window. okuro rewrites each
    provider's instruction files on the daemon's ``*/5`` refresh, so any
    window wider than five minutes can straddle a change, and the resulting
    number is an average of two populations presented as one measurement.
    This mode makes the instruction bundle the grouping key, so a rate is
    always computed inside one version.

    A SESSION IS NOT A MOMENT. ``first_ts`` is when the transcript FILE began,
    not when a run began: Claude Code resumes sessions and one JSONL
    accumulates across days. So the bundle is resolved at BOTH ends and a
    session joins a cohort only if they agree — which is proof, not a sample,
    because version boundaries are totally ordered in time. A session whose
    ends disagree received both versions and goes to ``spanning``.

    FOUR THINGS IT REFUSES TO HIDE, because each one has already been read
    as a result:

    * ``bundle: null`` is its own cohort, never folded into the newest one.
      It means the ledger has nothing at or before that session's start — the
      honest state for every session that ran before the ledger existed.

    * ``session_kind`` is reported per cohort. 110 of the newest 200
      claude-code transcripts are subagent sidechains and 278 of 650 codex
      rollouts are ``codex exec`` runs; pooling them with interactive
      sessions measures fan-out, not adoption.

    * ``numerator_valid`` goes False for any provider with zero tool_name
      coverage in the window. antigravity's ingester writes NULL into
      ``tool_name`` for every event it has ever produced, so every
      tool-shaped query in this module returns a structural zero for it no
      matter how healthy ingestion is. A zero that means "cannot see" must
      not render the same as a zero that means "never called".

    * ``spanning`` is counted and never folded in. A large value is itself
      the finding: it means instruction content is changing faster than
      sessions finish, and session-level attribution is the wrong unit until
      that changes.
    """
    from okuro.db import get_db
    from okuro.sense.providers._instruction_ledger import (
        PROVIDER_TO_ADAPTER, bundle_at, versions_at,
    )

    db = get_db()
    n = _cap(limit, 20, 200)

    # Window applies to the SESSION's start, not to event timestamps: the
    # cohort is defined by when a session began, and a long session whose
    # events fall inside the window did not start inside it.
    #
    # last_ts comes along because a session is NOT a moment. `first_ts` is
    # when the transcript FILE began, not when a run began — Claude Code
    # resumes sessions and one JSONL accumulates across days (measured in
    # sense/interaction/bridge.py: start-time delta up to 210,543 s, mean
    # 7.2 h; 1,930 okuro sessions collapse to 118 native ids). Measured here
    # 2026-09-17: 718 of 1,014 claude-code sessions in the last 30 days span
    # more than the 5-minute refresh interval and 35 span more than a day.
    # Stamping such a session with the version live at first_ts would emit a
    # confident digest for instructions half its turns never saw.
    clauses = ["s.first_ts IS NOT NULL"]
    params: list = []
    if all_history:
        window = {"all_history": True, "since_days": None}
    else:
        days = _cap(since_days, 30, 3650)
        clauses.append(f"s.first_ts >= {_TS_THRESHOLD}")
        params.append(f"-{days} days")
        window = {"all_history": False, "since_days": days}
    if provider:
        clauses.append("s.provider = ?")
        params.append(provider)
    if project:
        clauses.append("s.project_path LIKE ?")
        params.append(f"{project}%")
    if session_kind:
        clauses.append("s.session_kind IS ?" if session_kind == "null"
                       else "s.session_kind = ?")
        params.append(None if session_kind == "null" else session_kind)

    sessions = db.fetchall(
        f"""
        SELECT s.session_id AS session_id, s.provider AS provider,
               s.first_ts AS first_ts, s.last_ts AS last_ts,
               s.session_kind AS session_kind
        FROM agent_sessions s
        WHERE {' AND '.join(clauses)}
        """,
        tuple(params),
    )
    if not sessions:
        return {
            "mode": "instruction_cohort",
            "window": window,
            "filters": {"provider": provider, "project": project,
                        "tool": tool, "session_kind": session_kind},
            "cohorts": [],
            "spanning": {"sessions": 0, "by_provider": {}},
            "sessions_total": 0,
            "sessions_attributed": 0,
            "counting_note": _COHORT_NOTE,
        }

    # Which sessions called the tool. Bare-name matched, so one call answers
    # for every provider (see _tool_filter).
    called: set[str] = set()
    if tool:
        tc, tp = _tool_filter(tool, None, match_bare=True)
        ids = [r["session_id"] for r in sessions]
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            # `e.` alias: _tool_filter emits clauses against it, shared with
            # every other mode in this module.
            for r in db.fetchall(
                f"""
                SELECT DISTINCT e.session_id AS session_id FROM agent_events e
                WHERE e.session_id IN ({marks}) AND {' AND '.join(tc)}
                """,
                (*chunk, *tp),
            ):
                called.add(r["session_id"])

    # Providers whose events carry NO tool_name at all in this set — their
    # numerator is structurally zero and is reported as unusable, not as 0.
    tool_capable: set[str] = set()
    ids = [r["session_id"] for r in sessions]
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        for r in db.fetchall(
            f"""
            SELECT DISTINCT s.provider AS provider
            FROM agent_events e JOIN agent_sessions s ON s.session_id = e.session_id
            WHERE e.session_id IN ({marks}) AND e.tool_name IS NOT NULL
            """,
            tuple(chunk),
        ):
            tool_capable.add(r["provider"])

    # Resolve each session's bundle at BOTH ENDS. The ledger is small (one row
    # per content change) and lookups repeat heavily, so memoize on
    # (adapter, timestamp) rather than querying per session.
    memo: dict[tuple[str, str], str | None] = {}

    def _bundle(adapter: str | None, when: str | None) -> str | None:
        if not adapter or not when:
            return None
        key = (adapter, when)
        if key not in memo:
            memo[key] = bundle_at(adapter, when)
        return memo[key]

    groups: dict[tuple[str, str | None], dict] = {}
    spanning: dict[str, int] = {}
    spanning_total = 0
    for row in sessions:
        adapter = PROVIDER_TO_ADAPTER.get(row["provider"] or "")
        start = _bundle(adapter, row["first_ts"])
        end = _bundle(adapter, row["last_ts"] or row["first_ts"])
        if start != end:
            # The session ran across a version change and received BOTH. It
            # belongs to neither cohort, and folding it into either would put
            # a confident digest on instructions half its turns never saw.
            # Boundaries are totally ordered in time, so start == end is proof
            # that none was crossed; start != end is proof that one was.
            spanning_total += 1
            spanning[row["provider"]] = spanning.get(row["provider"], 0) + 1
            continue
        bundle = start
        gk = (row["provider"], bundle)
        g = groups.setdefault(gk, {
            "provider": row["provider"],
            "bundle": bundle,
            "sessions": 0,
            "sessions_with_tool": 0,
            "by_session_kind": {},
            "first_ts_min": row["first_ts"],
            "first_ts_max": row["first_ts"],
        })
        g["sessions"] += 1
        kind = row["session_kind"] or "unknown"
        g["by_session_kind"][kind] = g["by_session_kind"].get(kind, 0) + 1
        if row["session_id"] in called:
            g["sessions_with_tool"] += 1
        if row["first_ts"] < g["first_ts_min"]:
            g["first_ts_min"] = row["first_ts"]
        if row["first_ts"] > g["first_ts_max"]:
            g["first_ts_max"] = row["first_ts"]

    cohorts = []
    for g in sorted(groups.values(), key=lambda x: (x["provider"], x["first_ts_min"])):
        valid = g["provider"] in tool_capable
        adapter = PROVIDER_TO_ADAPTER.get(g["provider"] or "")
        cohorts.append({
            **g,
            # The per-path set the bundle digest collapses. The digest is for
            # grouping; this is for reading — "which file changed" is not
            # answerable from a hash, and a bundle moves whenever ANY of a
            # provider's managed paths does.
            "paths": (
                versions_at(adapter, g["first_ts_min"]) if adapter and g["bundle"]
                else {}
            ),
            "adoption_rate": (
                round(g["sessions_with_tool"] / g["sessions"], 4)
                if tool and valid and g["sessions"] else None
            ),
            "numerator_valid": valid if tool else None,
            "numerator_note": None if valid else (
                f"{g['provider']} has zero events with tool_name set in this "
                "window — its ingester does not denormalize tool names, so "
                "the numerator cannot be measured and is not reported as 0"
            ),
            "attributed": g["bundle"] is not None,
        })

    return {
        "mode": "instruction_cohort",
        "window": window,
        "filters": {"provider": provider, "project": project,
                    "tool": tool, "session_kind": session_kind},
        "sessions_total": len(sessions),
        "sessions_attributed": sum(
            c["sessions"] for c in cohorts if c["attributed"]
        ),
        # Its own bucket, never a cohort. Large here means the instruction
        # content is changing faster than sessions finish, which makes
        # session-level attribution the wrong unit — a finding, not noise.
        "spanning": {"sessions": spanning_total, "by_provider": spanning},
        "cohorts": cohorts[:n],
        "returned": min(len(cohorts), n),
        "capped": len(cohorts) > n,
        "counting_note": _COHORT_NOTE,
    }
