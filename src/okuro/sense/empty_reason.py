# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Make an empty tool result say WHY it is empty.
# index: imports | def is_empty_payload | def _explain_* | const _EXPLAINERS |
#        def explain_empty | def annotate_empty
# AGENT_HEADER_END -->
"""Why-is-this-empty, attached to any tool result that came back with nothing.

**The class.** An okuro read surface answers ``[]`` to four different
questions and gives the caller no way to tell them apart:

  * there is genuinely no such row
  * the id you passed belongs to a different namespace than the one indexed
  * this surface is not instrumented on the transport you are calling from
  * rows existed, and a filter applied AFTER retrieval discarded all of them

An agent cannot distinguish absence-of-record from absence-of-capability, so
it does what a reasonable reader does with an empty ledger: treats it as
evidence. Four recorded instances, each of which cost a session a wrong
conclusion:

  * ``list_tool_invocations`` -> [] for a stdio session (i.e. every Claude
    Code session and every Task subagent). Read as "this subagent skipped
    bootstrap"; ``session_trace`` refuted it. 2026-08-09.
  * ``artifact_search`` -> [] for an artifact that was indexed and reachable
    by a differently-worded query. Read as "embedding lag"; it was the
    post-scan project filter. 2026-08-09.
  * ``read_role_handover`` -> "no handover found", read as "the subtask never
    ran". It had run and emitted its output on another channel.
  * ``list_role_handovers`` + ``artifact_search`` both -> [] while the gap
    report sat in ``task_events``. Cost an M3 FAIL cycle.

**The seam.** This annotates at MCP dispatch, keyed on the SHAPE of the
payload rather than on a list of tool names, for the same reason
``_prepend_warning`` is: a new list-returning tool must not silently re-open
the hole. Tools with a specific answer register an explainer; everything else
still gets the generic one, so no surface can return a bare unexplained ``[]``.

The annotation is advisory metadata. It never changes a non-empty result, and
it never turns an empty result into a fabricated one.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

# Keys that carry an echo of the request rather than payload. A dict holding
# only these plus an empty collection is still an empty answer.
_ECHO_KEYS = {
    "session_id", "query", "project", "task_id", "subtask_id", "count",
    "limit", "offset", "kind", "topic", "audience", "backend", "provider",
    "role_id", "agent", "scope", "_compliance_warning", "_empty_reason",
}


def is_empty_payload(data: Any) -> bool:
    """True when ``data`` carries no rows, whatever container it uses.

    Shape-keyed on purpose. A bare ``[]`` is empty. A dict is empty when it
    holds at least one list/dict-valued key and EVERY one of them is empty —
    that covers ``{"invocations": []}``, ``{"count": 0, "events": []}`` and
    ``{"matches": [], "backend": "rg"}`` without naming any of them. A dict
    with no collection at all is a scalar answer, not an empty one.
    """
    if isinstance(data, list):
        return len(data) == 0
    if not isinstance(data, dict):
        return False
    collections = [
        v for k, v in data.items()
        if k not in _ECHO_KEYS and isinstance(v, (list, dict))
    ]
    if not collections:
        return False
    return all(len(c) == 0 for c in collections)


# ---------------------------------------------------------------------------
# per-surface explainers
# ---------------------------------------------------------------------------

def _explain_tool_invocations(args: dict) -> dict:
    """``tool_invocations`` is written on ONE transport, not on every session.

    ``mcp/_registry.py`` gates both write paths on ``current_inline_session_id``,
    and ``mcp/inline_http.py`` is the only place that sets it. A stdio session
    writes no rows at all, so an empty ledger says nothing about what the
    session did. Checking for the ``sessions_inline`` row separates "not
    ledgered" from "ledgered, made no calls" — a distinction the bare array
    cannot carry.
    """
    sid = (args or {}).get("session_id") or ""
    known = False
    try:
        from okuro.db import get_db
        row = get_db().fetchone(
            "SELECT 1 FROM sessions_inline WHERE id = ?", (sid,)
        )
        known = row is not None
    except Exception:  # noqa: BLE001 — a diagnostic must not raise
        return {
            "code": "unknown",
            "means": "Could not determine whether this session is ledgered.",
            "next": "Use session_trace(session_id=...) for what the session actually did.",
        }
    if known:
        return {
            "code": "no_rows",
            "means": "This IS an inline session and it recorded no tool calls.",
            "next": "The empty ledger is the answer here.",
        }
    return {
        "code": "not_an_inline_session",
        "means": (
            "No sessions_inline row for this id, so NOTHING was ever ledgered "
            "for it. tool_invocations is written only on the HTTP MCP "
            "transport; stdio sessions — every Claude Code session, every Task "
            "subagent, every Codex/Gemini CLI session — write zero rows. This "
            "empty result is not evidence the session made no tool calls."
        ),
        "next": "session_trace(session_id=...) reads the raw trace store and does cover stdio.",
    }


def _explain_semantic_search(args: dict) -> dict:
    """Empty from a filtered ANN search — the pool, not the index.

    See sense/retrieval.py for the mechanism. The two useful moves are
    widening the pool (raise ``limit``) and removing the post-scan filter
    (drop ``project``), because both change which rows the scan can reach.
    """
    a = args or {}
    filters = [k for k in ("project", "kind", "audience", "topic", "role_id",
                           "parent_id", "task_id") if a.get(k)]
    limit = a.get("limit")
    if filters:
        return {
            "code": "filtered_after_scan",
            "means": (
                "The vector scan is global; "
                f"{', '.join(filters)} narrowed its results afterwards. Nothing "
                "in the neighbourhood of this query belonged to that scope — "
                "which is NOT the same as the row not existing or not being "
                "indexed."
            ),
            "next": (
                f"Re-run without {filters[0]}=, or raise limit"
                + (f" (was {limit})" if limit else "")
                + ", or query with wording closer to the title/summary. "
                "artifact_list / note_list enumerate without a vector scan."
            ),
        }
    return {
        "code": "no_semantic_match",
        "means": "No indexed row was close enough to this query.",
        "next": (
            "Try wording closer to the target's title or summary. An embedder "
            "outage also surfaces here as an empty result."
        ),
    }


def _explain_role_handover(args: dict) -> dict:
    """Absence of a Stream-A write, not absence of work."""
    return {
        "code": "no_handover_written",
        "means": (
            "No write_role_handover call was made for this subtask. That is "
            "silent about whether the subtask ran or produced output — a "
            "subagent can emit its result as an artifact or a task_event and "
            "never write Stream A."
        ),
        "next": (
            "list_task_events(task_id=..., event_type='gap') and "
            "artifact_list(task_id=..., subtask_id=...) cover the other channels."
        ),
    }


def _explain_session_trace(args: dict) -> dict:
    """The tool= filter matches the STORED name, which carries the prefix."""
    a = args or {}
    tool = a.get("tool")
    if tool and not str(tool).startswith("mcp__"):
        return {
            "code": "filter_no_match",
            "means": (
                f"tool={tool!r} matched no event. Trace rows store the FULL "
                f"client-side name, so okuro tools are recorded as "
                f"'mcp__okuro__{tool}'. The unprefixed name matches nothing "
                "and looks identical to a session that never called it."
            ),
            "next": f"Retry with tool='mcp__okuro__{tool}'.",
        }
    return {
        "code": "no_rows",
        "means": "No event in this session matched the filters.",
        "next": "Drop the type/tool filter to see the raw stream.",
    }


def _explain_generic(args: dict) -> dict:
    a = args or {}
    passed = [f"{k}={v!r}" for k, v in a.items()
              if v not in (None, "", [], {}) and k != "limit"]
    return {
        "code": "no_rows",
        "means": (
            "No row matched"
            + (f" ({', '.join(passed[:5])})" if passed else "")
            + "."
        ),
        "next": "Relax or drop a filter to tell 'no match' from 'wrong filter'.",
    }


_EXPLAINERS: dict[str, Callable[[dict], dict]] = {
    "list_tool_invocations": _explain_tool_invocations,
    "artifact_search": _explain_semantic_search,
    "read_memory": _explain_semantic_search,
    "note_search": _explain_semantic_search,
    "search_thoughts": _explain_semantic_search,
    "role_diary_search": _explain_semantic_search,
    "roles_knowledge": _explain_semantic_search,
    "transcript_search": _explain_semantic_search,
    "person_match": _explain_semantic_search,
    "target_group_match": _explain_semantic_search,
    "read_role_handover": _explain_role_handover,
    "list_role_handovers": _explain_role_handover,
    "session_trace": _explain_session_trace,
}


def explain_empty(tool_name: str, arguments: Optional[dict]) -> dict:
    """Why ``tool_name`` returned nothing, given the arguments it got.

    Always answers. A surface with no registered explainer still gets the
    generic one, because "no reason offered" is the defect this module exists
    to remove — a specific reason is better, silence is not an option.
    """
    fn = _EXPLAINERS.get(tool_name, _explain_generic)
    try:
        return fn(arguments or {})
    except Exception:  # noqa: BLE001 — never let a diagnostic break dispatch
        return _explain_generic(arguments or {})


def annotate_empty(tool_name: str, arguments: Optional[dict], data: Any) -> Any:
    """Attach the reason to an empty payload, preserving its shape contract.

    dict -> an ``_empty_reason`` key, so the payload stays machine-readable.
    Anything else -> rendered as text with the reason above it, the same
    contract ``_prepend_warning`` already established for JSON arrays.
    Non-empty payloads are returned untouched.
    """
    if not is_empty_payload(data):
        return data
    reason = explain_empty(tool_name, arguments)
    if isinstance(data, dict):
        data["_empty_reason"] = reason
        return data
    header = (
        f"[empty: {reason['code']}] {reason['means']}\n"
        f"NEXT: {reason['next']}"
    )
    return f"{header}\n\n---\n\n{json.dumps(data, indent=2, default=str)}"
