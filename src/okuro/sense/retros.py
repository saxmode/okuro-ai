# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Session retros — batch comparative causal analysis over low/high-score sessions.
# index: imports | LOW_SCORE_MAX | HIGH_SCORE_MIN | _tools_list | _build_summary | _find_cohorts | _build_prompt | _parse | _persist | run_retros
# AGENT_HEADER_END -->
"""Session retros — causal analysis over low-compliance sessions.

Backs Meta-Harness P5 (causal reasoning over prior failures). Every run:

1. Picks up to ``max_per_cohort`` low-score sessions (compliance ≤ 3, end
   reason ``reported`` or ``timeout``) from the window that haven't been
   retro'd yet, plus a matched set of high-score sessions for contrast.
2. Builds compact summaries (tools used vs expected, first/last user
   turns from :mod:`okuro.trace`, duration, end reason).
3. Prompts an LLM via :func:`okuro.bridge.invoke` for differential
   patterns (present in ≥2 lows, ≤1 high).
4. Persists findings in ``session_retros`` / ``session_retro_batches``
   and writes each supported pattern as a ``gotcha`` memory tagged
   ``source_agent='okuro-retro'`` so future bootstraps surface it.

Safe to run repeatedly; each session retro is keyed by ``session_id``.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

_MEMORY_ID_RE = re.compile(r"id=([0-9a-f-]{36})")


def _extract_memory_id(ret: str | None) -> str | None:
    if not ret:
        return None
    m = _MEMORY_ID_RE.search(ret)
    return m.group(1) if m else ret[:80]

logger = logging.getLogger(__name__)

LOW_SCORE_MAX = 3
HIGH_SCORE_MIN = 4
MAX_PER_COHORT_DEFAULT = 10
WINDOW_DAYS_DEFAULT = 14
RETRO_SOURCE_AGENT = "okuro-retro"


def _tools_list(raw: str | None) -> list[str]:
    """Parse the JSON-array-in-text ``tools_used``/``tools_expected`` columns."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(val, list):
        return [str(t) for t in val]
    if isinstance(val, dict):
        return list(val.keys())
    return []


def _build_summary(session: dict) -> dict:
    """Compact per-session summary for the diff prompt.

    Uses the trace store (GAP 1) to pull the first and last user turns.
    """
    from okuro.db import get_db

    db = get_db()
    sid = session["session_id"]

    # `sid` lives in okuro's telemetry namespace; agent_events is keyed by the
    # provider's native id. Querying agent_events with `sid` directly returns
    # nothing — which is why every retro before this ran with empty transcript
    # fields. Resolve through the bridge first; an unbridged session simply
    # keeps the metadata-only summary rather than silently looking empty.
    try:
        from okuro.sense.interaction.bridge import resolve_native

        trace_sid = resolve_native(sid) or sid
    except Exception:
        trace_sid = sid

    user_first = db.fetchone(
        "SELECT substr(text, 1, 280) AS t FROM agent_events "
        "WHERE session_id = ? AND type = 'user' AND text != '' "
        "ORDER BY ord ASC LIMIT 1",
        (trace_sid,),
    ) or {}
    user_last = db.fetchone(
        "SELECT substr(text, 1, 280) AS t FROM agent_events "
        "WHERE session_id = ? AND type = 'user' AND text != '' "
        "ORDER BY ord DESC LIMIT 1",
        (trace_sid,),
    ) or {}

    counts = db.fetchone(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN type='assistant' THEN 1 ELSE 0 END) AS assistant_turns, "
        "SUM(CASE WHEN tool_name IS NOT NULL THEN 1 ELSE 0 END) AS tool_calls "
        "FROM agent_events WHERE session_id = ?",
        (trace_sid,),
    ) or {}

    tools_used = _tools_list(session.get("tools_used"))
    tools_expected = _tools_list(session.get("tools_expected"))
    tools_missing = sorted(set(tools_expected) - set(tools_used))

    return {
        "session_id": sid,
        "provider": session.get("provider"),
        "project": session.get("project"),
        "score": session.get("compliance_score"),
        "end_reason": session.get("end_reason"),
        "tools_used": tools_used,
        "tools_missing": tools_missing,
        "assistant_turns": counts.get("assistant_turns") or 0,
        "tool_calls": counts.get("tool_calls") or 0,
        "first_user_turn": (user_first.get("t") or "").strip(),
        "last_user_turn": (user_last.get("t") or "").strip(),
    }


def _find_cohorts(window_days: int, max_per_cohort: int) -> tuple[list[dict], list[dict]]:
    """Return (low_cohort, high_cohort). Skips already-retro'd sessions.

    We still do NOT require a matching row in ``agent_sessions``: the okuro
    ``sessions.session_id`` and the provider-native id are different UUIDs, so
    an inner join would drop most candidates. The id mapping this docstring
    used to anticipate now exists in ``session_bridge``, and
    :func:`_build_summary` resolves through it — so bridged sessions gain
    real first/last user turns while unbridged ones stay metadata-only rather
    than being excluded.
    """
    from okuro.db import get_db

    db = get_db()
    base_cols = (
        "s.session_id, s.provider, s.project, s.compliance_score, "
        "s.end_reason, s.tools_used, s.tools_expected, s.started_at"
    )
    low = db.fetchall(
        f"""
        SELECT {base_cols}
        FROM sessions s
        LEFT JOIN session_retros r ON r.session_id = s.session_id
        WHERE r.session_id IS NULL
          AND s.compliance_score IS NOT NULL
          AND s.compliance_score <= ?
          AND s.end_reason IN ('reported', 'timeout')
          AND s.started_at >= datetime('now', ?)
        ORDER BY s.started_at DESC
        LIMIT ?
        """,
        (LOW_SCORE_MAX, f"-{int(window_days)} days", int(max_per_cohort)),
    )
    high = db.fetchall(
        f"""
        SELECT {base_cols}
        FROM sessions s
        LEFT JOIN session_retros r ON r.session_id = s.session_id
        WHERE r.session_id IS NULL
          AND s.compliance_score IS NOT NULL
          AND s.compliance_score >= ?
          AND s.end_reason IN ('reported', 'timeout')
          AND s.started_at >= datetime('now', ?)
        ORDER BY s.started_at DESC
        LIMIT ?
        """,
        (HIGH_SCORE_MIN, f"-{int(window_days)} days", int(max_per_cohort)),
    )
    return low, high


_PROMPT_TEMPLATE = """You are analyzing completed agent sessions for behavioral patterns that correlate with low compliance scores.

## Context
Compliance score (0-6) rewards: bootstrap called, session_report called, cortex tools used, write_memory called, log_progress called. Higher = better.

## LOW-SCORE sessions (score ≤ 3)
{lows}

## HIGH-SCORE sessions (score ≥ 4)
{highs}

## Task
Identify behavioral patterns that are PRESENT in at least 2 low-score sessions AND absent or rare (at most 1 occurrence) in the high-score sessions.

For each pattern, output:
- `name`: short snake_case identifier
- `description`: one sentence
- `evidence`: list of session_ids where the pattern appears
- `recommendation`: concrete instruction a future agent should follow (1-2 sentences)

Only report patterns with meaningful discrimination. If none, return an empty list.

## Output
Respond with STRICT JSON only (no prose, no markdown fence). Schema:
{{"patterns": [{{"name": "...", "description": "...", "evidence": ["...","..."], "recommendation": "..."}}]}}
"""


def _render_cohort(summaries: list[dict]) -> str:
    lines: list[str] = []
    for s in summaries:
        lines.append(f"### {s['session_id']} (score={s['score']}, end={s['end_reason']}, provider={s['provider']})")
        lines.append(f"- tools_used: {s['tools_used']}")
        lines.append(f"- tools_missing: {s['tools_missing']}")
        lines.append(f"- turns: {s['assistant_turns']} assistant, {s['tool_calls']} tool calls")
        if s["first_user_turn"]:
            lines.append(f"- first_user_turn: {s['first_user_turn']}")
        if s["last_user_turn"] and s["last_user_turn"] != s["first_user_turn"]:
            lines.append(f"- last_user_turn: {s['last_user_turn']}")
        lines.append("")
    return "\n".join(lines)


def _build_prompt(lows: list[dict], highs: list[dict]) -> str:
    return _PROMPT_TEMPLATE.format(
        lows=_render_cohort(lows),
        highs=_render_cohort(highs),
    )


def _parse(output: str) -> dict:
    """Parse LLM output. Tolerates leading/trailing noise and markdown fences."""
    text = (output or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    # locate first `{` ... matching `}` for robustness
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in LLM output")
    return json.loads(text[start : end + 1])


def _persist(
    batch_id: str,
    window_days: int,
    model: str,
    lows: list[dict],
    highs: list[dict],
    parsed: dict,
    error: str | None = None,
) -> list[str]:
    """Write batch + per-session rows + gotcha memories. Returns memory ids."""
    from okuro.db import get_db
    from okuro.sense.memory import write_memory

    db = get_db()
    low_ids = {s["session_id"] for s in lows}
    memory_ids: list[str] = []
    patterns = (parsed or {}).get("patterns", [])

    # Map evidence → memory id so session rows can point back at the memory.
    per_session_findings: dict[str, list[dict]] = {s["session_id"]: [] for s in (*lows, *highs)}
    for pat in patterns:
        evidence = [sid for sid in (pat.get("evidence") or []) if sid in per_session_findings]
        # Enforce the ≥2 lows, ≤1 high rule on our side too — don't trust the LLM blindly.
        low_hits = [sid for sid in evidence if sid in low_ids]
        high_hits = [sid for sid in evidence if sid not in low_ids]
        if len(low_hits) < 2 or len(high_hits) > 1:
            pat["_rejected"] = "insufficient discrimination"
            continue
        mem_text = (
            f"[retro/{pat.get('name','unnamed')}] {pat.get('description','').strip()} "
            f"Recommendation: {pat.get('recommendation','').strip()} "
            f"Evidence: {len(low_hits)} low-score sessions ({', '.join(low_hits[:3])}"
            f"{', …' if len(low_hits) > 3 else ''})."
        )
        mem_ret = write_memory(
            topic="gotcha",
            content=mem_text,
            confidence=0.7,
            source_agent=RETRO_SOURCE_AGENT,
        )
        mem_id = _extract_memory_id(mem_ret)
        if mem_id:
            memory_ids.append(mem_id)
        pat["_memory_id"] = mem_id
        for sid in evidence:
            per_session_findings[sid].append(pat)

    with db.write() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO session_retro_batches (
                batch_id, ran_at, window_days, low_count, high_count,
                patterns_json, memory_ids, model, error
            ) VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                window_days,
                len(lows),
                len(highs),
                json.dumps(patterns, ensure_ascii=False),
                json.dumps(memory_ids, ensure_ascii=False),
                model,
                error,
            ),
        )
        session_rows = []
        for s in lows:
            session_rows.append((s["session_id"], batch_id, s["score"], "low",
                                 json.dumps(per_session_findings[s["session_id"]], ensure_ascii=False),
                                 None, model))
        for s in highs:
            session_rows.append((s["session_id"], batch_id, s["score"], "high",
                                 json.dumps(per_session_findings[s["session_id"]], ensure_ascii=False),
                                 None, model))
        conn.executemany(
            """
            INSERT OR REPLACE INTO session_retros (
                session_id, batch_id, retro_at, score, cohort, findings, memory_id, model
            ) VALUES (?, ?, datetime('now'), ?, ?, ?, ?, ?)
            """,
            session_rows,
        )
    return memory_ids


def run_retros(
    window_days: int = WINDOW_DAYS_DEFAULT,
    max_per_cohort: int = MAX_PER_COHORT_DEFAULT,
    capability: str | None = None,
    provider: str | None = "claude",
) -> dict[str, Any]:
    """Run one retro batch. Safe to call repeatedly — session_id PK de-dupes.

    Returns ``{batch_id, low, high, patterns, memories, skipped_reason?}``.
    """
    from okuro.bridge.invoke import invoke

    lows_raw, highs_raw = _find_cohorts(window_days, max_per_cohort)
    if len(lows_raw) < 2:
        return {
            "skipped_reason": f"need ≥2 low-score candidates in window; found {len(lows_raw)}",
            "low": len(lows_raw),
            "high": len(highs_raw),
        }
    if len(highs_raw) < 2:
        return {
            "skipped_reason": f"need ≥2 high-score candidates for contrast; found {len(highs_raw)}",
            "low": len(lows_raw),
            "high": len(highs_raw),
        }

    lows = [_build_summary(s) for s in lows_raw]
    highs = [_build_summary(s) for s in highs_raw]
    prompt = _build_prompt(lows, highs)
    batch_id = str(uuid.uuid4())

    result = invoke(
        prompt=prompt,
        capability=capability,
        provider=provider,
        system_prompt=(
            "You are a careful analyst. Output only valid JSON matching the "
            "requested schema. No commentary, no markdown."
        ),
    )
    if not result.get("success"):
        _persist(batch_id, window_days, result.get("model") or "", lows, highs,
                 parsed={"patterns": []}, error=result.get("error") or "invoke failed")
        return {
            "batch_id": batch_id,
            "low": len(lows), "high": len(highs),
            "patterns": 0, "memories": [],
            "error": result.get("error") or "invoke failed",
        }

    output = result.get("output", "")
    try:
        parsed = _parse(output)
    except (ValueError, json.JSONDecodeError) as exc:
        _persist(batch_id, window_days, result.get("model") or "", lows, highs,
                 parsed={"patterns": []}, error=f"parse failed: {exc}")
        return {
            "batch_id": batch_id,
            "low": len(lows), "high": len(highs),
            "patterns": 0, "memories": [],
            "error": f"parse failed: {exc}",
        }

    memory_ids = _persist(batch_id, window_days, result.get("model") or "",
                          lows, highs, parsed)
    return {
        "batch_id": batch_id,
        "low": len(lows),
        "high": len(highs),
        "patterns": len(parsed.get("patterns", [])),
        "memories": memory_ids,
        "model": result.get("model"),
    }
