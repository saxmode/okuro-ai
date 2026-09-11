# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Resonance PREPARE — build a real PCO from the enriched brain. Grounds
#   on ingested evidence (gather_context) and assembles provenance-tagged beats.
# index:
#   def _extract_json_span
#   def build_pco_from_brain
# AGENT_HEADER_END -->
"""Build a Prepared Content Object from the brain.

Net-new build #4 (the real builder; `pco.build_pco` was the hand-fed spike
ingress). Given a communication GOAL, this:

  1. Grounds on the brain via ``gather_context`` — surfaces the evidence
     artifacts landed by ``resonance.ingest`` plus memory.
  2. Asks an LLM to assemble the narrative as audience-NEUTRAL beats, each
     claim tagged with a ``source_ref`` (which grounding source it came from)
     and a ``confidence``. The prompt forbids inventing claims not in context.
  3. Returns a ``PCO`` — ready for ``resonance.render`` to fan out to any media.

The PCO stays audience-neutral: no phrasing for a particular reader, no layout.
Audience fit (STRUCTURE) and brand (PAINT) are applied only at render.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from .pco import PCO, build_pco

log = logging.getLogger("okuro.resonance.pco_builder")

_PCO_SYSTEM = (
    "You are a communication compiler. Given a GOAL and grounded CONTEXT, "
    "assemble the narrative as audience-NEUTRAL beats. Return ONLY JSON:\n"
    '{"title": str, "beats": [{"intent": str, "priority": 1-5, '
    '"depth_hint": "glance|brief|working|expert", '
    '"claims": [{"text": str, "source_ref": str, "confidence": 0.0-1.0, '
    '"audiences": ["board"|"exec"|"technical"|"general"], '
    '"weight": "must|should|nice", "construal": "why|how", '
    '"frame": {"gain": str, "loss": str}}]}\n'
    "Rules: (1) every claim MUST come from the CONTEXT — never invent facts; "
    "(2) source_ref = the context section/source the claim came from (use the "
    "'## kind: title' headers as labels); (3) confidence reflects how strongly "
    "the context supports it; (4) do NOT phrase for any specific audience and "
    "do NOT add layout — meaning only; (5) 3-6 beats, ordered by priority.\n"
    "SELECTION metadata (these do NOT phrase for an audience — they let a "
    "projector pick the right claims per reader; keep meaning neutral): "
    "(6) audiences = which reader KINDS this claim serves — OMIT or leave [] "
    "for a universal claim every reader needs; add kinds ONLY for a claim that "
    "genuinely serves some readers and not others (deep mechanism → "
    "['technical']; governance ask/ROI → ['board','exec']). "
    "(7) weight = 'must' (load-bearing, every reader), 'should' (strengthens), "
    "'nice' (color a concise reader can lose); default 'must'. "
    "(8) construal = the claim's natural altitude: 'why' (outcome / strategic) "
    "or 'how' (mechanism); omit if it reads the same either way. "
    "(9) frame = OPTIONAL gain/loss reframes of the SAME fact (gain = the "
    "opportunity, loss = the risk of not acting) — omit unless a reframe is "
    "faithful. Most claims need only text+source_ref+confidence; add selection "
    "fields sparingly, where they change WHICH reader should hear the claim."
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


def build_pco_from_brain(
    goal: str,
    *,
    project: Optional[str] = None,
    title: Optional[str] = None,
    provider: Optional[str] = None,
    budget: int = 9000,
    source_artifact_ids: Optional[list[str]] = None,
) -> PCO:
    """Assemble a provenance-tagged PCO for ``goal`` from the brain.

    ``source_artifact_ids`` (from ``ingest_document``) grounds DETERMINISTICALLY
    on exactly those documents — the correct intake→prepare linkage. Without
    them, falls back to a semantic ``gather_context`` sweep (which may miss a
    just-ingested doc among many artifacts).

    Raises ``RuntimeError`` if grounding is empty or the LLM produces no beats.
    """
    from okuro.sense.grounding import gather_context, SOURCE_ARTIFACTS, SOURCE_MEMORY

    if source_artifact_ids:
        from .context import context_from_artifacts
        context, sources = context_from_artifacts(source_artifact_ids, budget)
    else:
        context, sources = gather_context(
            goal, budget=budget, project=project,
            sources=(SOURCE_ARTIFACTS, SOURCE_MEMORY))
    if not context.strip():
        raise RuntimeError(
            "no brain context for this goal — ingest documents first "
            "(resonance.ingest.ingest_document) and pass source_artifact_ids")

    from okuro.bridge.invoke import invoke
    user = f"GOAL: {goal}\n\nCONTEXT:\n{context}"
    res = invoke(prompt=user, system_prompt=_PCO_SYSTEM,
                 capability="standard", provider=provider, timeout=180)
    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError(f"PCO generation failed: {(res or {}).get('error')}")

    raw = _extract_json_span(res.get("output") or "", "{", "}")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        try:
            import json_repair
            data = json_repair.loads(raw)
        except Exception as exc:
            raise RuntimeError(f"PCO JSON unparseable: {exc}") from exc

    beats = data.get("beats") if isinstance(data, dict) else None
    if not beats:
        raise RuntimeError("PCO builder produced no beats")

    return build_pco(
        goal=goal,
        title=title or data.get("title") or goal,
        beats=beats,
        facts=sources,
    )
