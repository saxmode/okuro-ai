# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Resonance ENRICH — adaptive interview that fills tacit gaps. Questions
#   target the gap engine's interview-route gaps; answers ingest as evidence, so
#   each turn enriches the brain and re-closes the loop.
# index:
#   def next_questions
#   def record_answer
# AGENT_HEADER_END -->
"""The interview engine — Resonance's enrichment stage.

Closes the enrich⇄analyze loop. The gap engine says WHAT is missing; the
interview asks the user for the *tacit/internal* gaps (intent, priorities,
constraints) that no web search can supply. Crucially, the interview's PRODUCT
is enrichment: each answer is ingested as an evidence artifact via
``resonance.ingest``, so the very next ``analyze_gaps`` sees higher completeness.

Spike scope: the engine is a set of functions over the ingested-artifact set —
session state is the growing list of artifact ids the caller threads through.
A durable ``interview_session`` entity (resume, UI) is P2b; the loop itself is
proven here without a migration.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("okuro.resonance.interview")


def next_questions(
    goal: str,
    *,
    source_artifact_ids: Optional[list[str]] = None,
    project: Optional[str] = None,
    provider: Optional[str] = None,
    max_q: int = 3,
) -> dict[str, Any]:
    """Return the next interview questions — the tacit gaps, most-missing first.

    Wraps the gap engine. Prefers ``route == "interview"`` gaps (research-route
    gaps are handled by the research flow, not the user). Returns
    ``{questions, completeness, ready, research_gaps}``.
    """
    from .gap import analyze_gaps

    g = analyze_gaps(goal, source_artifact_ids=source_artifact_ids,
                     project=project, provider=provider)
    interview_q = [q for q in g["open_questions"] if q["route"] == "interview"]
    research_q = [q for q in g["open_questions"] if q["route"] == "research"]
    # Missing-status requirements first (they carry the most information gain).
    missing_reqs = {r["requirement"] for r in g["requirements"]
                    if r["status"] == "missing"}
    interview_q.sort(key=lambda q: 0 if q["requirement"] in missing_reqs else 1)

    return {
        "questions": [q["question"] for q in interview_q[:max_q]],
        "completeness": g["completeness"],
        "ready": g["ready"],
        "research_gaps": [q["question"] for q in research_q],
    }


def record_answer(
    goal: str,
    question: str,
    answer: str,
    *,
    project: Optional[str] = None,
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Ingest one Q&A turn as evidence — enriches the brain for the next round.

    Returns the ingest manifest (``artifact_id`` to thread into the next
    ``next_questions`` / ``analyze_gaps`` / ``build_pco_from_brain`` call).
    """
    from .ingest import ingest_document

    body = (f"Interview turn (goal: {goal})\n\n"
            f"Q: {question.strip()}\nA: {answer.strip()}")
    return ingest_document(
        body, title=f"Interview: {question.strip()[:48]}",
        source_ref="interview", project=project,
        extract_claims=True, provider=provider)
