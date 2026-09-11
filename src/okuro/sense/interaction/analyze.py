### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Turn detector markers into named, evidenced findings; persist and memorialize them.
# index: imports | _PROMPT_TEMPLATE | _gather | _render | _parse | _persist | run_analysis
# AGENT_HEADER_END -->
"""Interpretation stage — markers in, actionable findings out.

:mod:`.detect` establishes *what happened* deterministically. This module asks
*what it means and what to change*, which needs a model. The split matters: the
markers are reproducible from ``agent_events`` alone, so a bad analysis run can
be redone without re-deriving the evidence, and a model that hallucinates a
pattern can be caught against markers it cannot invent.

How this differs from :mod:`okuro.sense.retros`
-----------------------------------------------
Retros compare low- and high-compliance sessions on *agent protocol*: bootstrap
called, session_report called. That measures whether the agent obeyed. It runs
on score metadata and — because the two session-id namespaces never joined — it
has never read a transcript.

This module measures whether the *interaction* worked: whether the human had to
correct, repeat, re-explain, or give up, and whether subagents were briefed and
closed out properly. It reads the transcript directly, so it needs no bridge and
covers all traced sessions.

Analysis window
---------------
Sessions aged 7-37 days by default. Newer than 7 days is still in flight — an
"abandoned" session may just be one the user has not returned to yet. Older than
37 days and the finding arrives too late to change anything.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

log = logging.getLogger(__name__)

WINDOW_MIN_AGE_DAYS = 7
WINDOW_MAX_AGE_DAYS = 37
MAX_EVIDENCE_PER_MARKER = 12
MIN_MARKERS_TO_RUN = 5

_MEMORY_ID_RE = re.compile(r"id=([0-9a-f-]{36})")

_PROMPT_TEMPLATE = """You are auditing how a human and their AI agents actually worked together, to find problems worth fixing.

## What you are reading
Deterministic detectors scanned agent session transcripts and flagged observable events. Each marker below carries real quoted evidence from the transcript. You did not observe the sessions; the markers are your only evidence.

## Marker vocabulary
{vocabulary}

## Observed markers in this window ({window_start} to {window_end})
{markers}

## Session context
- human-facing sessions scanned: {human_sessions}
- subagent sessions scanned: {subagent_sessions}
- total input-side turns: {turns}

## Task
Identify problems worth acting on. A problem qualifies only if:
1. It is supported by evidence from at least 2 DIFFERENT sessions, and
2. You can state a concrete change that would reduce it.

Prefer few, well-grounded findings over many speculative ones. If the markers do not support a real problem, return an empty list — that is a valid and useful answer.

For each finding:
- `name`: short snake_case identifier
- `description`: one sentence stating the problem
- `severity`: "low" | "medium" | "high"
- `markers`: which marker ids support it
- `evidence`: 2-5 verbatim quotes you were shown, unmodified
- `session_count`: how many distinct sessions support it
- `recommendation`: one concrete change to okuro's protocol, prompts, or tooling

Do NOT invent evidence. Every quote must appear verbatim in the markers above.

## Output
STRICT JSON only, no prose, no markdown fence:
{{"findings": [{{"name": "...", "description": "...", "severity": "medium", "markers": ["..."], "evidence": ["..."], "session_count": 2, "recommendation": "..."}}]}}
"""


def _gather(min_age_days: int, max_age_days: int) -> dict:
    """Collect markers and context for the analysis window."""
    from okuro.db import get_db
    from okuro.sense.interaction.detect import DETECTORS

    db = get_db()

    rows = db.fetchall(
        """
        SELECT m.marker, m.evidence, m.native_session_id, m.ts, m.weight,
               m.turn_index
        FROM interaction_markers m
        WHERE m.ts <= date('now', ?)
          AND m.ts >= date('now', ?)
        ORDER BY m.weight DESC, m.ts DESC
        """,
        (f"-{int(min_age_days)} days", f"-{int(max_age_days)} days"),
    )

    by_marker: dict[str, list[dict]] = {}
    sessions: set[str] = set()
    for r in rows:
        by_marker.setdefault(r["marker"], []).append(dict(r))
        sessions.add(r["native_session_id"])

    scanned = db.fetchone(
        """
        SELECT COALESCE(SUM(turns_scanned), 0) AS turns,
               SUM(CASE WHEN native_session_id LIKE 'agent-%' THEN 1 ELSE 0 END) AS subagent,
               SUM(CASE WHEN native_session_id NOT LIKE 'agent-%' THEN 1 ELSE 0 END) AS human
        FROM interaction_scanned
        """
    ) or {}

    vocabulary = "\n".join(
        f"- `{d.id}` ({d.applies_to}): {d.describe}" for d in DETECTORS
    )

    return {
        "by_marker": by_marker,
        "sessions": sessions,
        "total_markers": len(rows),
        "vocabulary": vocabulary,
        "turns": scanned.get("turns", 0),
        "human_sessions": scanned.get("human", 0),
        "subagent_sessions": scanned.get("subagent", 0),
    }


def _render(by_marker: dict[str, list[dict]]) -> str:
    """Render markers grouped by type, with capped evidence samples.

    Evidence is capped per marker so one noisy detector cannot crowd the
    others out of the context window. The session spread is stated explicitly
    so the model can apply the ">= 2 sessions" rule without counting quotes.
    """
    blocks: list[str] = []
    for marker, hits in sorted(
        by_marker.items(), key=lambda kv: sum(h["weight"] for h in kv[1]), reverse=True
    ):
        distinct = len({h["native_session_id"] for h in hits})
        blocks.append(
            f"### `{marker}` — {len(hits)} hits across {distinct} sessions"
        )
        for h in hits[:MAX_EVIDENCE_PER_MARKER]:
            sid = h["native_session_id"]
            short = sid[:18] + "…" if len(sid) > 18 else sid
            blocks.append(f'- [{short} turn {h["turn_index"]}] "{h["evidence"]}"')
        if len(hits) > MAX_EVIDENCE_PER_MARKER:
            blocks.append(f"- …and {len(hits) - MAX_EVIDENCE_PER_MARKER} more")
        blocks.append("")
    return "\n".join(blocks)


def _parse(output: str) -> dict:
    """Parse model output, tolerating fences and surrounding prose."""
    text = (output or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start : end + 1])


def _persist(batch_id: str, gathered: dict, parsed: dict, model: str,
             window: tuple[str, str], error: str | None = None) -> list[dict]:
    """Write the batch, its findings, and a gotcha memory per finding."""
    from okuro.db import get_db
    from okuro.sense.memory import write_memory

    db = get_db()
    findings = parsed.get("findings") or []

    with db.write():
        db.execute(
            """
            INSERT INTO interaction_batches
                (batch_id, window_start, window_end, sessions_in, turns_in,
                 findings_out, model, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id, window[0], window[1], len(gathered["sessions"]),
                gathered["turns"], len(findings), model, error,
            ),
        )

    written: list[dict] = []
    for f in findings:
        name = str(f.get("name") or "").strip()
        description = str(f.get("description") or "").strip()
        if not name or not description:
            continue

        severity = str(f.get("severity") or "medium").lower()
        if severity not in ("low", "medium", "high"):
            severity = "medium"

        recommendation = str(f.get("recommendation") or "").strip()

        # Findings outlive the traces that produced them — that is what makes
        # compaction safe — so each one also lands as a gotcha memory where
        # future bootstraps will surface it.
        memory_id = None
        if recommendation:
            try:
                ret = write_memory(
                    topic="gotcha",
                    content=(
                        f"[interaction-audit] {description} "
                        f"Recommendation: {recommendation} "
                        f"(markers: {', '.join(f.get('markers') or [])}; "
                        f"{f.get('session_count', 0)} sessions; batch {batch_id})"
                    ),
                    project="okuro",
                    source_agent="okuro-interaction-audit",
                    confidence=0.7,
                )
                m = _MEMORY_ID_RE.search(str(ret) or "")
                memory_id = m.group(1) if m else None
            except Exception as exc:
                log.warning("interaction analysis: memory write failed (%s)", exc)

        finding_id = str(uuid.uuid4())
        with db.write():
            db.execute(
                """
                INSERT INTO interaction_findings
                    (id, batch_id, name, description, severity, markers,
                     evidence, session_count, recommendation, memory_id,
                     window_start, window_end, model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finding_id, batch_id, name, description, severity,
                    json.dumps(f.get("markers") or [], ensure_ascii=False),
                    json.dumps(f.get("evidence") or [], ensure_ascii=False),
                    int(f.get("session_count") or 0),
                    recommendation, memory_id, window[0], window[1], model,
                ),
            )
        written.append(
            {
                "id": finding_id,
                "name": name,
                "severity": severity,
                "description": description,
                "recommendation": recommendation,
                "memory_id": memory_id,
            }
        )

    return written


def run_analysis(
    min_age_days: int = WINDOW_MIN_AGE_DAYS,
    max_age_days: int = WINDOW_MAX_AGE_DAYS,
    provider: str | None = "claude",
    capability: str | None = None,
) -> dict[str, Any]:
    """Run one interaction-analysis batch over the current window.

    Returns ``{batch_id, findings, markers_in, sessions_in}`` or a
    ``skipped_reason`` when the window holds too little to reason over.
    """
    from okuro.bridge.invoke import invoke
    from okuro.sense.interaction.lifecycle import mark_analyzed

    gathered = _gather(min_age_days, max_age_days)
    if gathered["total_markers"] < MIN_MARKERS_TO_RUN:
        return {
            "skipped_reason": (
                f"only {gathered['total_markers']} markers in window; "
                f"need >= {MIN_MARKERS_TO_RUN}"
            ),
            "markers_in": gathered["total_markers"],
        }

    window = (f"-{max_age_days}d", f"-{min_age_days}d")
    prompt = _PROMPT_TEMPLATE.format(
        vocabulary=gathered["vocabulary"],
        markers=_render(gathered["by_marker"]),
        window_start=window[0],
        window_end=window[1],
        human_sessions=gathered["human_sessions"],
        subagent_sessions=gathered["subagent_sessions"],
        turns=gathered["turns"],
    )

    batch_id = str(uuid.uuid4())
    result = invoke(
        prompt=prompt,
        provider=provider,
        capability=capability,
        system_prompt=(
            "You are a careful analyst auditing human-agent collaboration. "
            "Ground every claim in the evidence you were shown. Output only "
            "valid JSON matching the requested schema."
        ),
    )

    model = result.get("model") or ""
    if not result.get("success"):
        _persist(batch_id, gathered, {"findings": []}, model, window,
                 error=result.get("error") or "invoke failed")
        return {
            "batch_id": batch_id,
            "error": result.get("error") or "invoke failed",
            "findings": [],
        }

    try:
        parsed = _parse(result.get("output") or "")
    except Exception as exc:
        _persist(batch_id, gathered, {"findings": []}, model, window,
                 error=f"parse failed: {exc}")
        return {"batch_id": batch_id, "error": f"parse failed: {exc}", "findings": []}

    findings = _persist(batch_id, gathered, parsed, model, window)

    # Only now are these sessions eligible for compaction.
    mark_analyzed(sorted(gathered["sessions"]), batch_id)

    return {
        "batch_id": batch_id,
        "findings": findings,
        "markers_in": gathered["total_markers"],
        "sessions_in": len(gathered["sessions"]),
        "model": model,
    }
