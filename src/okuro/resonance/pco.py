# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Prepared Content Object (PCO) — Resonance's media-agnostic IR. An
#   audience-neutral, provenance-tagged narrative that any media adapter renders.
# index:
#   class Claim
#   class NarrativeBeat
#   class PCO
#   def build_pco
# AGENT_HEADER_END -->
"""The Prepared Content Object — Resonance's intermediate representation.

A PCO holds MEANING + PROVENANCE only. No phrasing, no layout, no audience
binding — those are applied at render time (STRUCTURE=audience, PAINT=brand).
This is the invariant that makes media pluggable: build the PCO once, render
it as a prism page, a slide deck, a website, a HubSpot site — as many as you
want, each audience-fitted, provenance preserved.

``to_content_brief()`` collapses the PCO into the CONTENT text a generator
consumes — every claim annotated with its source + confidence so the medium
can surface *why* a claim is made (prism expert rung, deck speaker notes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# depth_hint maps onto prism's rung ladder; a deck reads it as emphasis.
DEPTH_LEVELS = ("glance", "brief", "working", "expert")


# Claim-selection vocab (Phase E — content IR). ``weight`` gates whether a
# claim survives projection for a concise audience; ``construal`` picks the
# altitude the generator phrases it at; ``frame`` offers gain/loss reframes.
WEIGHTS = ("must", "should", "nice")
CONSTRUALS = ("why", "how")


@dataclass
class Claim:
    """One assertion + where it came from + how sure we are.

    ``source_ref`` is a URL, doc id, kg triple id, or interview turn — the
    provenance thread that lets a user verify. ``confidence`` is 0.0-1.0.

    Phase E adds audience-SELECTION metadata (all optional — an untagged claim
    is universal and rendered as-is, so pre-E PCOs behave identically):

    * ``audiences`` — audience KINDS this claim serves (``board``/``exec``/
      ``technical``/``general``); empty = every audience.
    * ``weight`` — ``must``/``should``/``nice``; projection drops ``nice`` (and
      optionally ``should``) for concise readers so the claim SET shrinks, not
      just the wording.
    * ``construal`` — ``why`` (outcome/strategic altitude) or ``how``
      (mechanism); projection picks per audience.
    * ``frame`` — ``{"gain": ..., "loss": ...}`` alternative framings; projection
      selects the one matching the audience's regulatory focus.

    ``directive`` is a transient set BY projection — a phrasing instruction the
    content brief surfaces to the generator (never set by the builder).
    """
    text: str
    source_ref: Optional[str] = None
    confidence: float = 1.0
    audiences: list[str] = field(default_factory=list)
    weight: str = "must"
    construal: Optional[str] = None
    frame: Optional[dict[str, str]] = None
    directive: Optional[str] = None

    def annotated(self) -> str:
        meta = []
        if self.source_ref:
            meta.append(f"source: {self.source_ref}")
        meta.append(f"confidence: {self.confidence:.2f}")
        base = f"{self.text}  ({'; '.join(meta)})"
        if self.directive:
            base += f"  → {self.directive}"
        return base


@dataclass
class NarrativeBeat:
    """A unit of narrative intent — one thing the communication must land."""
    intent: str                      # what this beat achieves, e.g. "establish the problem"
    claims: list[Claim] = field(default_factory=list)
    priority: int = 3                # 1..5 — ordering / emphasis
    depth_hint: str = "brief"        # DEPTH_LEVELS — how deep this beat wants to go


@dataclass
class PCO:
    """Prepared Content Object — the media-agnostic IR. audience stays UNBOUND."""
    goal: str                        # the communication goal (the "so that")
    beats: list[NarrativeBeat] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)   # kg entity/triple refs (grounding)
    assets: list[dict[str, Any]] = field(default_factory=list)
    title: Optional[str] = None

    def min_confidence(self) -> float:
        cs = [c.confidence for b in self.beats for c in b.claims]
        return min(cs) if cs else 1.0

    def to_content_brief(self, audience_brief: str = "") -> str:
        """Collapse the PCO into the CONTENT text a generator consumes.

        Provenance is preserved inline so the medium can show *why* at depth.
        ``audience_brief`` (STRUCTURE) is prepended when present — the adapter
        supplies it from the resolved audience lens.
        """
        lines: list[str] = []
        if audience_brief:
            lines.append(audience_brief.strip())
            lines.append("")
        lines.append(f"GOAL: {self.goal}")
        if self.title:
            lines.insert(0, f"TITLE: {self.title}")
        lines.append("")
        lines.append(
            "Build the narrative from these beats (priority order). Each claim "
            "carries its source + confidence — preserve the ability to show WHY "
            "a claim is made at depth; never present a low-confidence claim as "
            "certain.")
        for i, beat in enumerate(sorted(self.beats, key=lambda b: -b.priority), 1):
            lines.append("")
            lines.append(f"[Beat {i} — intent: {beat.intent} — depth: {beat.depth_hint}]")
            for c in beat.claims:
                lines.append(f"  - {c.annotated()}")
        if self.facts:
            lines.append("")
            lines.append(f"GROUNDING refs: {', '.join(self.facts)}")
        return "\n".join(lines)


def build_pco(goal: str, beats: list[dict[str, Any]], *,
              title: Optional[str] = None, facts: Optional[list[str]] = None) -> PCO:
    """Minimal builder: dicts → PCO. A real builder (net-new #4) will assemble
    this from the enriched brain + gap-analysis; this is the spike ingress.

    Each beat dict: ``{intent, priority?, depth_hint?, claims: [{text, source_ref?,
    confidence?, audiences?, weight?, construal?, frame?}]}``. The Phase-E
    selection fields are optional — omitted → universal/must/no-reframe.
    """
    out_beats: list[NarrativeBeat] = []
    for b in beats:
        claims = [
            Claim(
                text=c["text"], source_ref=c.get("source_ref"),
                confidence=float(c.get("confidence", 1.0)),
                audiences=[a for a in (c.get("audiences") or []) if a],
                weight=(c.get("weight") or "must"),
                construal=c.get("construal") or None,
                frame=(c.get("frame") if isinstance(c.get("frame"), dict) else None),
            )
            for c in b.get("claims", [])
        ]
        out_beats.append(NarrativeBeat(
            intent=b["intent"], claims=claims,
            priority=int(b.get("priority", 3)),
            depth_hint=b.get("depth_hint", "brief"),
        ))
    return PCO(goal=goal, beats=out_beats, title=title, facts=facts or [])
