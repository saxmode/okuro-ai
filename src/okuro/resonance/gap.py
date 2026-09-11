# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Resonance ANALYZE — does the assembled context suffice for the goal?
#   Decomposes goal into requirements, scores coverage, routes each gap to
#   research (factual/external) or re-interview (tacit/internal).
# index:
#   def _extract_json_span
#   def analyze_gaps
# AGENT_HEADER_END -->
"""The gap engine — Resonance's epistemic gate.

The differentiator no competitor has: before producing any media, check whether
the assembled context actually SUFFICES to achieve the stated goal. Decompose
the goal into requirements, score each against the grounded context, and route
every gap:

- **research** — factual/external gap (market size, a competitor fact) → the
  research flow (web → kg / person_source).
- **interview** — tacit/internal gap (the user's intent, priorities,
  constraints) → the interview engine.

Single LLM pass (decompose + score + route) keeps it cheap. Completeness is the
mean coverage across requirements — the gate the pipeline reads before RENDER.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger("okuro.resonance.gap")

_STATUS_WEIGHT = {"covered": 1.0, "partial": 0.5, "missing": 0.0}

_GAP_SYSTEM = (
    "You are a communication readiness auditor. Given a GOAL and the grounded "
    "CONTEXT gathered so far, decide whether the context suffices to achieve the "
    "goal. Return ONLY JSON:\n"
    '{"requirements": [{"requirement": str, "status": "covered|partial|missing", '
    '"confidence": 0.0-1.0, "route": "research|interview", "question": str}]}\n'
    "Rules: (1) decompose the goal into 4-8 things that must be KNOWN or TRUE to "
    "achieve it; (2) status = how well CONTEXT covers each; (3) route a gap to "
    "'research' if it is a factual/external fact (market size, a competitor, a "
    "public number) or to 'interview' if it is tacit/internal (the user's intent, "
    "priorities, constraints, private data); (4) question = the specific thing to "
    "research or ask; for 'covered' requirements set question to \"\". Judge only "
    "from CONTEXT — do not assume facts not present."
)


def _extract_json_span(text: str, open_ch: str, close_ch: str) -> str:
    start = text.find(open_ch)
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def analyze_gaps(
    goal: str,
    *,
    source_artifact_ids: Optional[list[str]] = None,
    project: Optional[str] = None,
    provider: Optional[str] = None,
    budget: int = 9000,
) -> dict[str, Any]:
    """Score whether the ingested context suffices for ``goal``.

    Returns::

        {
          "completeness": 0.0-1.0,          # mean coverage — the RENDER gate
          "requirements": [{requirement, status, confidence, route, question}],
          "open_questions": [{question, route, requirement}],  # gaps only
          "ready": bool,                    # completeness >= 0.75 and no missing
          "sources": [labels],
        }
    """
    from .context import context_from_artifacts
    from okuro.sense.grounding import gather_context, SOURCE_ARTIFACTS, SOURCE_MEMORY

    if source_artifact_ids:
        context, sources = context_from_artifacts(source_artifact_ids, budget)
    else:
        context, sources = gather_context(
            goal, budget=budget, project=project,
            sources=(SOURCE_ARTIFACTS, SOURCE_MEMORY))
    if not context.strip():
        # No material at all — everything is an open interview question.
        return {"completeness": 0.0, "requirements": [], "open_questions": [],
                "ready": False, "sources": [],
                "note": "no context ingested yet — start the interview"}

    from okuro.bridge.invoke import invoke
    res = invoke(prompt=f"GOAL: {goal}\n\nCONTEXT:\n{context}",
                 system_prompt=_GAP_SYSTEM, capability="standard",
                 provider=provider, timeout=150)
    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError(f"gap analysis failed: {(res or {}).get('error')}")

    raw = _extract_json_span(res.get("output") or "", "{", "}")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        try:
            import json_repair
            data = json_repair.loads(raw)
        except Exception as exc:
            raise RuntimeError(f"gap JSON unparseable: {exc}") from exc

    reqs = data.get("requirements") if isinstance(data, dict) else None
    if not reqs:
        raise RuntimeError("gap engine produced no requirements")

    norm: list[dict[str, Any]] = []
    for r in reqs:
        if not isinstance(r, dict) or not r.get("requirement"):
            continue
        status = str(r.get("status", "missing")).lower()
        if status not in _STATUS_WEIGHT:
            status = "missing"
        route = str(r.get("route", "interview")).lower()
        if route not in ("research", "interview"):
            route = "interview"
        norm.append({
            "requirement": str(r["requirement"]),
            "status": status,
            "confidence": float(r.get("confidence", 0.5)),
            "route": route,
            "question": str(r.get("question") or ""),
        })

    completeness = (sum(_STATUS_WEIGHT[r["status"]] for r in norm) / len(norm)
                    if norm else 0.0)
    open_questions = [
        {"question": r["question"], "route": r["route"], "requirement": r["requirement"]}
        for r in norm if r["status"] != "covered" and r["question"]
    ]
    ready = completeness >= 0.75 and not any(r["status"] == "missing" for r in norm)

    return {"completeness": round(completeness, 3), "requirements": norm,
            "open_questions": open_questions, "ready": ready, "sources": sources}
