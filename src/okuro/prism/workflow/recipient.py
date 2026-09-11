# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism workflow NODE 2 (recipient) — resolve who the deck is FOR and
#   what that implies. Reuses compiler.profile (people graph + cognitive-lens PII
#   firewall + person×brand disambiguation) verbatim, then reasons over that profile
#   AND the Understanding to produce a RecipientBrief: what this reader cares about,
#   what they decide, what to lead with, what to suppress.
# index: recipient_brief | NeedsDisambiguation | _BRIEF_SYSTEM
# AGENT_HEADER_END -->
"""Node 2 — recipient: named readers -> ``RecipientBrief``.

Recipient resolution is NOT rebuilt here. ``compiler.profile.profile`` already
walks the okuro people graph, reads the cognitive lens through the PII firewall,
blends several recipients to the safest envelope, and refuses to guess when a
person wears more than one brand hat. Reimplementing any of that would be a
second policy to keep in sync (DP10), so this node calls it and keeps the
resulting ``AudienceProfile`` verbatim inside the brief.

What this node ADDS is the part the profile cannot know: what this reader wants
*from this particular document*. That requires both sides — the profile and the
Understanding — which is why it is a node and not a lookup.

``NeedsDisambiguation`` propagates unchanged to the caller. An ambiguous
recipient is a question for the human, never a coin flip.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from okuro.prism.compiler import llm
from okuro.prism.compiler import profile as _profile
from okuro.prism.compiler.profile import NeedsDisambiguation  # re-exported for callers
from okuro.prism.workflow.types import RecipientBrief, Understanding

logger = logging.getLogger(__name__)

# No `maxItems`: how much a reader cares about is theirs, not a design constant.
_BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["cares_about", "lead_with"],
    "properties": {
        "cares_about": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "decisions_faced": {"type": "array", "items": {"type": "string"}},
        "lead_with": {"type": "string", "minLength": 1},
        "suppress": {"type": "array", "items": {"type": "string"}},
        "delivery": {"type": "string"},
    },
}

_BRIEF_SYSTEM = """You are the RECIPIENT node of the prism deck workflow. You are \
given (a) a resolved reader profile from the okuro people graph and (b) a holistic \
understanding of one document. Work out what THIS reader needs from THIS document.

Answer:
- cares_about: what this reader actually cares about in this material, in their \
terms. Ground every item in something the document contains — never invent an \
interest the profile does not support.
- decisions_faced: the decisions or judgements this reader has to make that this \
document bears on. Empty if the document is purely informational for them.
- lead_with: the ONE thing to put in front of them first, and why it earns that slot.
- suppress: material in the document that is real but not for this reader — detail \
that would cost them attention without changing anything they do.
- delivery: how this reader wants information delivered, in one line, taken from \
their profile (density, structure, tone, jargon tolerance).

Two hard rules. Respect the reader's stated cognitive preferences literally — they \
are accessibility requirements, not style hints. And never let `suppress` mean \
"hide an inconvenient fact"; it means "this reader does not need this detail".
Return ONLY the JSON object."""


def recipient_brief(
    recipients: list[str],
    understanding: Understanding,
    *,
    brand_pins: Optional[dict[str, str]] = None,
    provider: Optional[str] = None,
    invoke_fn=None,
    web_fn=None,
    mention_audience_members: bool = False,
) -> RecipientBrief:
    """Resolve ``recipients`` and reason out what they need from this document.

    Raises ``NeedsDisambiguation`` (from the profile stage) when a recipient's
    brand context is ambiguous and unpinned — surfaced to the caller, never guessed.

    ``mention_audience_members`` decides whether reader NAMES may reach the
    LLM at all. Default False: every other attribute of a recipient is
    firewalled, so their name is opt-in too.
    """
    if not recipients:
        raise ValueError("recipient: at least one recipient required")

    audience = _profile.profile(recipients, brand_pins=brand_pins, web_fn=web_fn)
    audience_d = audience.to_dict()

    # The profile is firewalled; the NAME was not. This node sent recipient
    # names straight to the LLM while every other attribute of the same
    # person went through cognitive_profile_for_llm. Names are now opt-in
    # via mention_audience_members (default False) — prism owns the setter
    # and the no-attribution lint; people ships the gate.
    reader_view = [
        {"role": r.role, "brand": r.brand,
         "lens": r.lens, "sliders": r.sliders, "source": r.source,
         **({"name": r.name} if mention_audience_members else {})}
        for r in audience.recipients
    ]
    doc_view = {
        "title": understanding.source_title,
        "kind": understanding.document_kind,
        "what_it_is": understanding.what_it_is,
        "thesis": understanding.thesis,
        "domain": understanding.domain,
        "through_lines": understanding.through_lines,
        "key_entities": understanding.key_entities,
        "section_headings": [s.heading for s in understanding.sections],
    }
    user = (
        "READER PROFILE (from the okuro people graph):\n" + llm.dumps(reader_view) +
        f"\n\nDELIVERY ENVELOPE: jargon_tolerance={audience.jargon_tolerance!r}, "
        f"depth_ceiling={audience.depth_ceiling} ({audience.depth_ceiling_rung}), "
        f"prefer_modules={audience.prefer_modules}, avoid_modules={audience.avoid_modules}, "
        f"composite={audience.composite}\n\n"
        "DOCUMENT UNDERSTANDING:\n" + llm.dumps(doc_view) +
        "\n\nWhat does this reader need from this document? Return ONLY the JSON."
    )
    data = llm.call_json(
        system_prompt=_BRIEF_SYSTEM, user_prompt=user, schema=_BRIEF_SCHEMA,
        stage="wf.recipient", provider=provider, capability="standard",
        timeout=600, thinking_tokens=4000, invoke_fn=invoke_fn,
    )

    def _strs(key: str) -> list[str]:
        return [str(x).strip() for x in (data.get(key) or []) if str(x).strip()]

    return RecipientBrief(
        recipients=[r.name for r in audience.recipients],
        audience=audience_d,
        cares_about=_strs("cares_about"),
        decisions_faced=_strs("decisions_faced"),
        lead_with=(data.get("lead_with") or "").strip(),
        suppress=_strs("suppress"),
        delivery=(data.get("delivery") or "").strip(),
        jargon_tolerance=audience.jargon_tolerance,
        depth_ceiling=audience.depth_ceiling,
        depth_ceiling_rung=audience.depth_ceiling_rung,
        mention_audience_members=mention_audience_members,
    )


__all__ = ["recipient_brief", "NeedsDisambiguation"]
