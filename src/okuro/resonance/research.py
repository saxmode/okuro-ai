# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Resonance RESEARCH — auto-fill the gap engine's research-route gaps.
#   The machine-side counterpart to the interview: web-research each factual/
#   external gap and ingest the findings, so the next analyze sees them covered.
# index:
#   def research_question
#   def resolve_research_gaps
# AGENT_HEADER_END -->
"""The research engine — the autonomous half of the enrich loop.

The gap engine routes each gap ``research`` (factual/external) or ``interview``
(tacit/internal). The interview asks the user; this asks the WEB. For each
research-route gap it runs a short web-research pass (bridge ``deep-research``
capability) and ingests the cited findings as evidence — so the very next
``analyze_gaps`` sees those requirements covered, exactly like a recorded
interview answer. Symmetric loop, opposite source.

Best-effort per gap: a failed/empty research pass is skipped, not fatal. This
is what turns the gap gate from advisory into ACTIONABLE — "okuro researches
the board of Mobiliar" is this function over the audience-research gaps.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("okuro.resonance.research")

_RESEARCH_PROMPT = (
    "Do a SHORT web search and return 4-8 bullet points of current, verifiable "
    "facts — each with its source URL — that answer this question. Facts only, "
    "cite every claim, no preamble:\n{q}"
)


def research_question(
    question: str,
    *,
    project: Optional[str] = None,
    provider: Optional[str] = None,
    web_timeout: int = 240,
) -> Optional[dict[str, Any]]:
    """Web-research ONE question and ingest the cited findings as evidence.

    The atom shared by the gap-filling loop and the actuality re-research. Returns
    the ingest manifest (``{artifact_id, claims_added, …}``) or ``None`` when the
    research pass is empty/failed (best-effort — never raises)."""
    from .ingest import ingest_document
    from okuro.bridge.invoke import invoke

    res = invoke(prompt=_RESEARCH_PROMPT.format(q=question),
                 capability="deep-research", provider=provider, timeout=web_timeout)
    output = (res or {}).get("output", "") if isinstance(res, dict) else ""
    if not (isinstance(res, dict) and res.get("success") and output.strip()):
        log.info("research pass empty for: %s", question)
        return None
    return ingest_document(
        output, title=f"Research: {question[:48]}",
        source_ref=f"web-research:{question[:80]}", project=project,
        extract_claims=True, provider=provider)


def resolve_research_gaps(
    goal: str,
    *,
    source_artifact_ids: Optional[list[str]] = None,
    project: Optional[str] = None,
    provider: Optional[str] = None,
    max_gaps: int = 3,
    web_timeout: int = 240,
) -> dict[str, Any]:
    """Web-research the goal's research-route gaps and ingest the findings.

    Returns ``{researched: [{question, artifact_id, claims_added}],
    new_artifact_ids, remaining_research}`` — feed ``new_artifact_ids`` back into
    ``analyze_gaps``/``build_pco_from_brain`` (added to source_artifact_ids) and
    completeness rises.
    """
    from .gap import analyze_gaps

    g = analyze_gaps(goal, source_artifact_ids=source_artifact_ids,
                     project=project, provider=provider)
    research_qs = [q for q in g["open_questions"] if q["route"] == "research"]
    todo = research_qs[:max_gaps]

    researched: list[dict[str, Any]] = []
    new_ids: list[str] = []
    for q in todo:
        question = q["question"]
        man = research_question(question, project=project, provider=provider,
                                web_timeout=web_timeout)
        if not man:
            continue
        researched.append({"question": question,
                           "artifact_id": man["artifact_id"],
                           "claims_added": man["claims_added"]})
        new_ids.append(man["artifact_id"])

    return {"researched": researched, "new_artifact_ids": new_ids,
            "remaining_research": len(research_qs) - len(researched)}
