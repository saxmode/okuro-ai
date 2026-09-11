# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro Resonance — the communication compiler. One machine turns
#   context+goal into audience-fitted content across pluggable media. This
#   package holds the media-agnostic IR (PCO) + the render seam.
# index: none
# AGENT_HEADER_END -->
"""okuro·resonance — the communication compiler (spike).

Resonance separates the three orthogonal inputs of any communication:

    CONTENT   = the Prepared Content Object (PCO) — audience-NEUTRAL,
                provenance-tagged meaning. What is true, why, from where.
    STRUCTURE = the audience cognitive lens — how THIS recipient thinks.
    PAINT     = the brand — how it looks/sounds.

A media adapter is just ``render(pco, audience, brand) -> deliverable``.
prism and slides already share the signature ``(topic, person_id, brand_id,
provider)`` — this package generalises ``topic → PCO`` and ``person_id →
audience`` so ONE prepared context fans out to many media.
"""

from .pco import PCO, NarrativeBeat, Claim, build_pco
from .render import render, render_matrix, resolve_audience_ref, MEDIA_ADAPTERS
from .ingest import ingest_document
from .pco_builder import build_pco_from_brain
from .gap import analyze_gaps
from .interview import next_questions, record_answer
from .research import resolve_research_gaps, research_question
from .actuality import check_actuality, refresh_stale
from .api import render_from_brain
from .session import (
    session_create, session_get, session_list,
    session_next, session_answer, session_ingest, session_close,
)

__all__ = [
    "PCO", "NarrativeBeat", "Claim", "build_pco",
    "render", "render_matrix", "resolve_audience_ref", "MEDIA_ADAPTERS",
    "ingest_document", "build_pco_from_brain",
    "analyze_gaps", "next_questions", "record_answer",
    "resolve_research_gaps", "research_question",
    "check_actuality", "refresh_stale",
    "render_from_brain",
    "session_create", "session_get", "session_list",
    "session_next", "session_answer", "session_ingest", "session_close",
]
