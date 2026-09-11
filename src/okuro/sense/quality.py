# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Session QUALITY judging (Metric 2) — a fast-tier LLM judges the
#   session transcript via the provider-agnostic CLI bridge and persists a
#   1..5 quality score, separate from protocol-adherence (compliance_score).
# index:
#   imports
#   def _extract_turns
#   def _load_transcript_excerpt
#   def _build_judge_prompt
#   def _parse_verdict
#   def judge_session
#   def judge_pending
# AGENT_HEADER_END -->
"""Session quality judging (Metric 2).

Adherence (``compliance_score``) asks "did the agent follow the okuro tool
protocol". Quality asks "was the work actually good". They are orthogonal — a
session can nail the protocol and produce garbage, or ignore the protocol and
do excellent work. This module judges the latter, from the transcript, using a
fast-tier model routed through the bridge (``capability='fast-judge'``) so it is
provider-agnostic and cheap (DP01).

The transcript is read from ``agent_events`` — the archive that SURVIVES —
joined to the compliance ``sessions`` table via ``provider_session_id`` (the id
bridge), with the on-disk JSONL kept only as a fallback.

Why the source moved (2026-08-13, Phase 3)
------------------------------------------
This module used to read ``agent_sessions.source_path`` exclusively, which is a
provider's own transcript file. Those files are pruned: the retention window
leaves roughly a month of them, so a judgeable session became UNJUDGEABLE the
day its JSONL aged out, and quality coverage was structurally capped at the
disk window no matter how large the corpus grew. ``agent_events`` holds the
same messages for the whole corpus, so pointing at it extends an existing
qualification signal across the archive instead of a rolling window.

The semantics are identical BY CONSTRUCTION rather than by re-implementation.
The ingester stores each JSONL line's ``message`` object verbatim in
``content_json`` (``trace/claude_code.py:137,148``), so the events path parses
the same payload and calls the SAME :func:`_flatten_content` the disk path
does, with the same per-turn cap and the same drop rules. Pinned by
``tests/sense/test_quality_events_parity.py``, which compares both readers on
sessions that still exist in both places.

One asymmetry is deliberate: the ingester RECLASSIFIES a user line carrying a
``tool_result`` block to ``type='tool_result'``, while the disk reader sees it
as a user line and drops it for flattening to exactly ``[tool_result]``. The
events query therefore INCLUDES ``tool_result`` rows and applies the same drop
rule, rather than filtering them out in SQL — filtering would diverge on the
rare line that carries both a tool result and real text, which the disk reader
keeps.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Excerpt budget handed to the judge. Transcripts can be hundreds of turns;
# the judge needs the opening task + the arc + the resolution, not the middle.
_MAX_EXCERPT_CHARS = 8000
_HEAD_TURNS = 4
_TAIL_TURNS = 8
_PER_TURN_CHARS = 1200

_JUDGE_SYSTEM = (
    "You are a strict, fair evaluator of an AI coding/assistant session. "
    "You judge OUTCOME QUALITY only — not whether the agent followed any tool "
    "protocol. Consider: did it accomplish the user's actual request, is the "
    "work correct and coherent, and did the user show satisfaction vs "
    "frustration (corrections, 'no', 'that's wrong', repeated retries). "
    "Reply in EXACTLY this format and nothing else:\n"
    "SCORE: <1-5>\n"
    "REASON: <one line>\n"
    "Scale: 1 failed/harmful, 2 poor, 3 mixed, 4 good, 5 excellent."
)


def _flatten_content(content) -> str:
    """Best-effort text from a message ``content`` field, provider-tolerant.

    Handles: plain string; list of blocks with ``text``/``type=text``; and
    dicts. Tool-call / tool-result blocks are reduced to a short marker so the
    judge sees that tools ran without drowning in payloads.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if "text" in block and isinstance(block["text"], str):
                    parts.append(block["text"])
                elif btype == "tool_use":
                    parts.append(f"[tool:{block.get('name', '?')}]")
                elif btype == "tool_result":
                    parts.append("[tool_result]")
        return " ".join(p for p in parts if p)
    if isinstance(content, dict):
        return _flatten_content(content.get("content") or content.get("text"))
    return ""


def _extract_turns(source_path: str) -> list[tuple[str, str]]:
    """Read a transcript jsonl -> ordered ``(role, text)`` for user/assistant.

    Provider-tolerant: matches claude's ``{type, message:{role, content}}`` and
    generic ``{role, content}`` / ``{role, text}`` shapes. Skips system, tool
    and empty turns.
    """
    turns: list[tuple[str, str]] = []
    p = Path(source_path)
    if not p.exists():
        return turns
    try:
        with p.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                etype = raw.get("type")
                if etype in ("user", "assistant"):
                    msg = raw.get("message", {}) or {}
                    role = msg.get("role", etype)
                    text = _flatten_content(msg.get("content"))
                elif "role" in raw and raw.get("role") in ("user", "assistant"):
                    role = raw["role"]
                    text = _flatten_content(raw.get("content") or raw.get("text"))
                else:
                    continue

                text = (text or "").strip()
                # Drop pure tool-noise turns and okuro protocol boilerplate.
                if not text or text in ("[tool_result]",):
                    continue
                turns.append((role, text[:_PER_TURN_CHARS]))
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is not hypothetical: 2 of 62 live transcripts with
        # a file still on disk are not valid UTF-8 (measured 2026-08-13), and
        # this function caught only OSError — so judge_session raised instead of
        # reporting a skipped session. Fixed here rather than at the call site
        # because every caller of this reader shares the exposure.
        return turns
    return turns


# The event types a transcript is built from. `tool_result` is present because
# the ingester reclassifies user lines carrying a tool_result block into it;
# the disk reader still sees those as user lines and drops them by the
# `[tool_result]` rule below, so including them here and applying the SAME rule
# is what keeps the two readers identical. See the module docstring.
_TRANSCRIPT_EVENT_TYPES = ("user", "assistant", "tool_result")


def _extract_turns_from_events(db, native_session_id: str) -> list[tuple[str, str]]:
    """Read ``(role, text)`` turns out of ``agent_events``.

    The events mirror of :func:`_extract_turns`. Both call
    :func:`_flatten_content` on the same ``message.content`` payload — the
    ingester stores each JSONL line's message object verbatim — so this is the
    same transformation reading a durable source rather than a pruned one.
    """
    rows = db.fetchall(
        f"""
        SELECT type, role, content_json
        FROM agent_events
        WHERE session_id = ?
          AND type IN ({','.join('?' * len(_TRANSCRIPT_EVENT_TYPES))})
        ORDER BY ord
        """,
        (native_session_id, *_TRANSCRIPT_EVENT_TYPES),
    )

    turns: list[tuple[str, str]] = []
    for row in rows:
        try:
            payload = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        # `role` is what the ingester read off the message; a reclassified
        # tool_result row still carries role='user', which is what the disk
        # reader would have called it.
        role = row["role"] or ("assistant" if row["type"] == "assistant" else "user")
        if role not in ("user", "assistant"):
            continue
        text = (_flatten_content(payload.get("content")) or "").strip()
        if not text or text in ("[tool_result]",):
            continue
        turns.append((role, text[:_PER_TURN_CHARS]))
    return turns


def events_are_skeletal(db, native_session_id: str) -> bool:
    """True when the compactor has already taken this session's assistant side.

    ``interaction.lifecycle.compact_traces`` NULLs ``text`` and ``content_json``
    for the types each tier lists, and at the COLD tier that list includes
    ``assistant`` (lifecycle.py:74). Input turns are never compacted, so a cold
    session still reads back as a full set of user turns with NOTHING the agent
    said — and a quality judge handed that transcript would score "was the work
    good" against a session containing none of the work.

    That is worse than the failure this module's repoint fixed. The old
    disk-only path REFUSED when it had nothing to read; an unguarded events path
    SUCCEEDS and returns a confident 1-5 on a skeleton, which is a wrong number
    where there used to be no number.

    Measured on the live store 2026-08-13: 1091 sessions already have assistant
    rows with zero surviving assistant bodies, against 1169 sessions at the cold
    tier. So this is the common case for old sessions, not an edge.

    Detected structurally rather than by reading ``trace_lifecycle``: what
    matters is whether the bodies are THERE, and a session can lose them by any
    route. Zero assistant rows is NOT skeletal — that is an empty session, which
    the caller reports separately.

    Registering this module in the extractor registry was the other option and
    is deliberately not taken, for the reason migration 137 gives for the
    distiller: it would make the compactor wait on the whole backlog being
    judged first.
    """
    row = db.fetchone(
        """
        SELECT COUNT(*) AS rows_,
               SUM(CASE WHEN content_json IS NOT NULL OR text IS NOT NULL
                        THEN 1 ELSE 0 END) AS bodies
        FROM agent_events
        WHERE session_id = ? AND type = 'assistant'
        """,
        (native_session_id,),
    ) or {}
    total = int(row.get("rows_") or 0)
    bodies = int(row.get("bodies") or 0)
    return total > 0 and bodies == 0


def _excerpt_from_turns(turns: list[tuple[str, str]]) -> tuple[str, int]:
    """Head + tail turns within the budget. Shared by both readers."""
    total = len(turns)
    if total == 0:
        return "", 0

    if total <= _HEAD_TURNS + _TAIL_TURNS:
        chosen = turns
        elided = False
    else:
        chosen = turns[:_HEAD_TURNS] + turns[-_TAIL_TURNS:]
        elided = True

    lines: list[str] = []
    for role, text in chosen[:_HEAD_TURNS]:
        lines.append(f"{role.upper()}: {text}")
    if elided:
        lines.append(f"… [{total - _HEAD_TURNS - _TAIL_TURNS} turns elided] …")
    for role, text in chosen[_HEAD_TURNS:]:
        lines.append(f"{role.upper()}: {text}")

    excerpt = "\n".join(lines)
    if len(excerpt) > _MAX_EXCERPT_CHARS:
        excerpt = excerpt[:_MAX_EXCERPT_CHARS] + "\n… [truncated]"
    return excerpt, total


def _load_transcript_excerpt(source_path: str) -> tuple[str, int]:
    """The DISK reader. Kept as the fallback for sessions with no events."""
    return _excerpt_from_turns(_extract_turns(source_path))


def _load_transcript_excerpt_from_events(db, native_session_id: str) -> tuple[str, int]:
    """The ARCHIVE reader — the default, because these rows are not pruned."""
    return _excerpt_from_turns(_extract_turns_from_events(db, native_session_id))


def _build_judge_prompt(task_hint: str | None, excerpt: str) -> str:
    task = (task_hint or "").strip() or "(no stated task)"
    return (
        f"Stated task: {task}\n\n"
        f"Session transcript (head + tail):\n{excerpt}\n\n"
        "Judge the outcome quality now."
    )


_SCORE_RE = re.compile(r"SCORE:\s*([1-5])", re.IGNORECASE)
_REASON_RE = re.compile(r"REASON:\s*(.+)", re.IGNORECASE)


def _parse_verdict(text: str) -> tuple[int | None, str]:
    """Parse ``SCORE:`` / ``REASON:`` from the judge's text reply.

    CLI bridge providers return free text (JSON mode is local-only), so we pin
    a strict format and parse it. Returns ``(None, reason)`` on unparseable
    output so the caller can skip rather than store a fake score.
    """
    if not text:
        return None, ""
    m = _SCORE_RE.search(text)
    score = int(m.group(1)) if m else None
    r = _REASON_RE.search(text)
    reason = (r.group(1).strip() if r else text.strip())[:280]
    return score, reason


def judge_session(session_id: str, *, timeout: int = 120) -> dict:
    """Judge one session's quality and persist it. Idempotent-ish: overwrites.

    Returns ``{session_id, ok, score, reason, provider, model, error}``.
    """
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        """SELECT s.session_id, s.task_hint, s.provider_session_id,
                  a.source_path
             FROM sessions s
             JOIN agent_sessions a ON a.session_id = s.provider_session_id
            WHERE s.session_id = ?
            LIMIT 1""",
        (session_id,),
    )
    if not row:
        return {"session_id": session_id, "ok": False,
                "error": "no bridged transcript (missing provider_session_id)"}

    # Events first. The old code refused outright when source_path was NULL or
    # its file had aged out of the retention window, which made coverage a
    # function of the disk window rather than of the corpus — the whole reason
    # this moved.
    #
    # But a compacted session must NOT be judged from events: the cold tier
    # nulls assistant bodies, so what comes back is the user's half of a
    # conversation and nothing the agent did. Refusing is the honest answer, and
    # it is what the disk-only path did for the same session.
    native_id = row["provider_session_id"]
    skeletal = events_are_skeletal(db, native_id)
    excerpt, total, source = "", 0, "agent_events"
    if not skeletal:
        excerpt, total = _load_transcript_excerpt_from_events(db, native_id)

    if total == 0 and row["source_path"]:
        excerpt, total = _load_transcript_excerpt(row["source_path"])
        source = "source_path"

    if total == 0 and skeletal:
        return {"session_id": session_id, "ok": False,
                "error": "compacted transcript — the cold tier nulled the "
                         "assistant bodies and no on-disk copy remains, so "
                         "only the user's turns survive. Judging outcome "
                         "quality from those would score work that is not "
                         "there."}
    if total == 0:
        return {"session_id": session_id, "ok": False, "error": "empty transcript"}

    prompt = _build_judge_prompt(row["task_hint"], excerpt)

    from okuro.bridge.invoke import invoke
    result = invoke(
        prompt,
        capability="fast-judge",     # -> provider's fast-tier model, provider-agnostic
        system_prompt=_JUDGE_SYSTEM,
        tool=True,                    # stateless tooling bridge: no agent context, faster
        timeout=timeout,
    )
    if not result.get("success"):
        return {"session_id": session_id, "ok": False,
                "error": result.get("error") or "bridge invoke failed",
                "provider": result.get("provider"), "model": result.get("model")}

    score, reason = _parse_verdict(result.get("output", ""))
    if score is None:
        return {"session_id": session_id, "ok": False,
                "error": "unparseable verdict", "provider": result.get("provider"),
                "model": result.get("model")}

    db.execute(
        """UPDATE sessions
              SET quality_score = ?, quality_rationale = ?,
                  quality_judged_at = datetime('now')
            WHERE session_id = ?""",
        (score, reason, session_id),
    )
    db.conn.commit()
    return {"session_id": session_id, "ok": True, "score": score, "reason": reason,
            "provider": result.get("provider"), "model": result.get("model"),
            # Which reader produced the excerpt. Reported rather than inferred:
            # a run judging mostly from source_path means the events archive is
            # not carrying the corpus, which is a finding about ingestion.
            "transcript_source": source}


_ELIGIBLE_SQL = """SELECT s.session_id
     FROM sessions s
     JOIN agent_sessions a ON a.session_id = s.provider_session_id
    WHERE s.quality_score IS NULL
      AND s.provider_session_id IS NOT NULL
      AND COALESCE(a.assistant_count, 0) >= ?"""


def pending_count(min_activity: int = 4) -> int:
    """How many judgeable sessions are still unjudged.

    Coverage was invisible before this existed: the scorecard printed a
    Quality column and a Q-judged count, but nothing said what fraction of the
    corpus that count represented. A quality average over 1% of sessions reads
    exactly like one over 100%.
    """
    from okuro.db import get_db

    row = get_db().fetchall(
        f"SELECT COUNT(*) AS n FROM ({_ELIGIBLE_SQL})", (min_activity,)
    )
    return int(row[0]["n"]) if row else 0


def judge_pending(
    limit: int = 10, min_activity: int = 4, backfill_share: float = 0.5
) -> str:
    """Judge up to ``limit`` unjudged sessions that have a bridged transcript.

    Cost control: capped per run — each judge is one fast-tier LLM call
    (haiku / flash / codex-low / local qwen via the capability router). Only
    sessions with real activity (``assistant_count >= min_activity``) and a
    joined transcript are eligible.

    THE STARVATION THIS FIXES. The query was ``ORDER BY started_at DESC``
    with a cap, so it only ever saw the NEWEST unjudged sessions. Whenever
    sessions arrive faster than the cap, the older ones are never reached —
    not slowly, never. Measured 2026-07-28: 24 of 2199 claude-code sessions
    carried a quality score, and every other provider carried zero, while the
    task had been running daily. Raising the cap alone would not have fixed
    it; it would only have moved the arrival rate at which the tail starves.

    So the batch is SPLIT: ``backfill_share`` of it drains oldest-first while
    the remainder stays newest-first. Freshness and coverage are both real
    requirements and neither ordering serves both. Set ``backfill_share=0``
    to restore pure newest-first once the backlog is gone.
    """
    from okuro.db import get_db

    db = get_db()
    n_old = int(limit * backfill_share)
    n_new = limit - n_old

    picked: list[str] = []
    seen: set[str] = set()
    for order, n in (("DESC", n_new), ("ASC", n_old)):
        if n <= 0:
            continue
        for r in db.fetchall(
            f"{_ELIGIBLE_SQL} ORDER BY s.started_at {order} LIMIT ?",
            (min_activity, n),
        ):
            sid = r["session_id"]
            # The two halves meet in the middle once the backlog is small.
            if sid not in seen:
                seen.add(sid)
                picked.append(sid)

    if not picked:
        return "Quality: no pending sessions with a bridged transcript"

    judged = 0
    failed = 0
    for sid in picked:
        res = judge_session(sid)
        if res.get("ok"):
            judged += 1
        else:
            failed += 1

    remaining = pending_count(min_activity)
    return (
        f"Quality: judged {judged}, failed {failed} "
        f"(of {len(picked)} attempted; {remaining} still pending)"
    )
