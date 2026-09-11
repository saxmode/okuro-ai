# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Resonance MCP-facing composition — one-shot helpers that keep the PCO
#   object server-side and return JSON-serializable digests for agents/UI.
# index:
#   def _pco_digest
#   def render_from_brain
# AGENT_HEADER_END -->
"""Composition layer for the Resonance MCP/REST surface.

The PCO is a Python object, not something to marshal across a tool boundary.
``render_from_brain`` composes PREPARE (build_pco_from_brain) + RENDER in one
call and returns a serializable digest — so an agent or the UI can go from a
goal + ingested docs straight to a media deliverable without holding the IR.
"""

from __future__ import annotations

from typing import Any, Optional

from .pco import PCO
from .render import render, _DEFAULT_BRAND


def _pco_digest(pco: PCO) -> dict[str, Any]:
    """Serializable summary of a PCO — beats, provenance, confidence floor."""
    return {
        "goal": pco.goal,
        "title": pco.title,
        "beat_count": len(pco.beats),
        "min_confidence": pco.min_confidence(),
        "beats": [
            {
                "intent": b.intent,
                "depth_hint": b.depth_hint,
                "priority": b.priority,
                "claims": [
                    {"text": c.text, "source_ref": c.source_ref,
                     "confidence": c.confidence}
                    for c in b.claims
                ],
            }
            for b in pco.beats
        ],
        "facts": pco.facts,
    }


def render_from_brain(
    goal: str,
    *,
    source_artifact_ids: Optional[list[str]] = None,
    audience: Optional[str] = None,
    media: str = "prism",
    brand_id: str = _DEFAULT_BRAND,
    project: Optional[str] = None,
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """PREPARE + RENDER in one call: goal + ingested docs → media deliverable.

    Builds a provenance-tagged PCO from the brain, then renders it for the
    given audience into the given media. Returns ``{pco, render}`` where ``pco``
    is a serializable digest and ``render`` is the backend result.
    """
    from .pco_builder import build_pco_from_brain

    pco = build_pco_from_brain(
        goal, source_artifact_ids=source_artifact_ids, project=project,
        provider=provider)
    result = render(pco, audience, brand_id=brand_id, media=media,
                    provider=provider)
    return {"pco": _pco_digest(pco), "render": result}
