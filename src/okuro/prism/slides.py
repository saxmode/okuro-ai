# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism SLIDE MODEL — the layer that maps the L1–L4 depth ladder +
#   the two deck-level slides onto real SLIDE TYPES (hero / index / cover / section
#   / content / doc-view), each with a layout archetype, ON TOP of the grid packer.
#   Pure + extractive (no LLM, no fabrication): the cover line comes from a facet's
#   existing hook/headline, the doc-view from its existing prose ladder. Stamped at
#   the storage chokepoint so every path (generate / build / edit) renders the same
#   slide model, and mirrored by the 2D grid frontend.
# index:
#   SLIDE_TYPES | LEVEL_SLIDE_TYPE | SLIDE_ARCHETYPE
#   def slide_type_for | def cover_line | def docview_markdown
#   def stamp_slide_types
# AGENT_HEADER_END -->
"""okuro·prism slide model — the fix for a deck with no cover.

Until now prism generated four depth RUNGS of content and packed each into a
grid; it had no concept of a cover slide, a title slide, or a doc-view. The user
confirmed (2026-07-21) a SLIDE model where visual density is INVERSE to depth:

    deck  ── HERO  (the title / takeaway cover)
          └─ INDEX (the agenda matrix — deck-level only)
    topic ── L1 = COVER    — one idea, biggest type, lowest density
          ── L2 = CONTENT  — key points
          ── L3 = CONTENT  — enriched detail (≤5 key points)
          └─ L4 = DOC-VIEW — the whole topic as a navigable document (highest text)

This module is the map from a (level, facet) to its SLIDE TYPE and the archetype
that renders it, plus the two EXTRACTIVE builders the slide types need — a cover
line (the facet's assertion, unchanged) and a doc-view markdown (the facet's own
prose ladder, concatenated). Nothing here calls an LLM or invents a value, so it
can run at the deterministic storage chokepoint and never threatens faithfulness.
"""

from __future__ import annotations

from typing import Any, Optional

from okuro.prism.rungs import RUNGS  # ("L1", "L2", "L3", "L4")

# Every slide type in the model — two deck-level, four per-topic.
SLIDE_TYPES = ("hero", "index", "cover", "section", "content", "doc-view")

# The slide type each depth LEVEL renders as (the confirmed inverse-density model).
LEVEL_SLIDE_TYPE: dict[str, str] = {
    "L1": "cover",     # intro / hook — full-page statement or quote, biggest type
    "L2": "content",   # key points
    "L3": "content",   # enriched detail (≤5 key points)
    "L4": "doc-view",  # the whole topic rendered as a document inside the deck
}

# Slide type → layout archetype (the name the grid packer / frontend key off). A
# cover and a doc-view are single-column, full-bleed by nature; content levels fall
# through to the ordinary block packer. "section" is a divider for a parent facet.
SLIDE_ARCHETYPE: dict[str, str] = {
    "hero": "hero",
    "index": "index",
    "cover": "cover",
    "section": "section",
    "content": "packed",     # → the existing grid packer (layout.derive_rung_layout)
    "doc-view": "doc-view",
}


def slide_type_for(level: str, facet: Optional[dict[str, Any]] = None) -> str:
    """The SLIDE TYPE a depth level renders as.

    L1 is a COVER, except a top-level facet that carries child facets (a chapter
    with sub-topics) renders its L1 as a SECTION divider — the same big-type
    treatment, but announcing a group rather than a leaf idea. L4 is a DOC-VIEW;
    L2/L3 are content. An unknown level falls through to content (safe default)."""
    st = LEVEL_SLIDE_TYPE.get(level, "content")
    if st == "cover" and isinstance(facet, dict) and (facet.get("children") or []):
        return "section"
    return st


def _rung_body(facet: dict[str, Any], level: str) -> str:
    rungs = facet.get("rungs") if isinstance(facet, dict) else None
    rc = rungs.get(level) if isinstance(rungs, dict) else None
    return str(rc.get("body") or "").strip() if isinstance(rc, dict) else ""


def _hook_line(facet: dict[str, Any]) -> str:
    """An existing L1 hook statement's text (the per-recipient impact line placed by
    the projection engine), if any — it is the strongest cover line when present."""
    rungs = facet.get("rungs") if isinstance(facet, dict) else None
    l1 = rungs.get("L1") if isinstance(rungs, dict) else None
    for b in (l1 or {}).get("blocks") or []:
        if isinstance(b, dict) and b.get("type") == "statement" and (b.get("text") or "").strip():
            return b["text"].strip()
    return ""


def cover_line(facet: dict[str, Any]) -> str:
    """The COVER line for a facet — EXTRACTIVE, invents nothing.

    Priority: an existing L1 hook statement (the projection engine's faithfulness-
    gated impact line for a single recipient) > the facet's assertion ``headline``
    (the 'so what', present on every facet) > its ``title``. For a GROUP deck with
    no per-recipient hook the headline carries the cover — the resolved answer to
    the spec's one open question (2026-07-21): the hero/cover replaces the hook."""
    return (
        _hook_line(facet)
        or str(facet.get("headline") or "").strip()
        or str(facet.get("title") or "").strip()
    )


def cover_block(facet: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Build the L1 cover as a standalone big-type ``statement`` block, or None when
    the facet has no cover line. The frontend renders ``display:"standalone"`` as a
    full-page cover; the kicker frames it with the topic's short nav title."""
    line = cover_line(facet)
    if not line:
        return None
    block: dict[str, Any] = {"type": "statement", "text": line, "display": "standalone"}
    title = str(facet.get("title") or "").strip()
    # Only add the kicker when it adds signal (a title distinct from the cover line).
    if title and title != line:
        block["kicker"] = title
    return block


def docview_markdown(facet: dict[str, Any]) -> str:
    """The L4 'full documentation' body — the facet's ENTIRE prose ladder (L1→L4
    bodies) concatenated into one long-form markdown document, deepest-context
    last. EXTRACTIVE: every line already exists in a level body; nothing is written.
    Duplicate/blank bodies are skipped so the doc reads clean. '' when the facet has
    no prose at all."""
    seen: set[str] = set()
    parts: list[str] = []
    for level in RUNGS:
        body = _rung_body(facet, level)
        if not body or body in seen:
            continue
        seen.add(body)
        parts.append(body)
    return "\n\n".join(parts)


def stamp_slide_types(doc: Any) -> int:
    """Stamp ``slide_type`` on every present level of every facet, in place — the
    universal, idempotent tag the frontend keys off to render covers/doc-views. Runs
    at the storage chokepoint so generate / build / edit all get the same model.
    Returns the number of levels stamped. Never fabricates content — only labels."""
    if not isinstance(doc, dict):
        return 0
    facets = doc.get("facets")
    if not isinstance(facets, dict):
        return 0
    n = 0
    for facet in facets.values():
        if not isinstance(facet, dict):
            continue
        rungs = facet.get("rungs")
        if not isinstance(rungs, dict):
            continue
        for level in RUNGS:
            rc = rungs.get(level)
            if isinstance(rc, dict):
                rc["slide_type"] = slide_type_for(level, facet)
                n += 1
    return n


__all__ = [
    "SLIDE_TYPES",
    "LEVEL_SLIDE_TYPE",
    "SLIDE_ARCHETYPE",
    "slide_type_for",
    "cover_line",
    "cover_block",
    "docview_markdown",
    "stamp_slide_types",
]
