# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism DEPTH-LEVEL vocabulary — the single source of truth for the
#   four per-topic depth levels (L1 cover → L4 full-doc), replacing the legacy
#   glance/brief/working/expert rung names. Every pipeline stage, the storage
#   chokepoint, the ops handler and the frontend import their level vocabulary
#   from here so the deck speaks ONE language. A legacy-alias shim normalises decks
#   and agent calls written before the rename so a deck never regresses.
# index: RUNGS | LEGACY_RUNGS | RUNG_ROLE | normalize_rung | normalize_rungs | normalize_doc_rungs
# AGENT_HEADER_END -->
"""Prism depth-level vocabulary (the L1–L4 slide-model ladder).

A topic is written as four progressive-depth levels. The names encode the
slide-model roles the user confirmed (2026-07-21): visual density is INVERSE to
depth — the shallow level is the loudest cover, the deep level is the quietest
full document.

    L1 = intro / hook — a full-page COVER (biggest typography, one idea)
    L2 = key points   — the headline substance
    L3 = enriched detail (≤5 key points) — the working depth
    L4 = full documentation — the whole topic rendered INSIDE the deck (doc-view)

These replace the legacy ``glance / brief / working / expert`` rung names 1:1.
``LEGACY_RUNGS`` maps the old vocabulary onto the new so anything persisted or
authored before the rename (stored decks, in-flight MCP ops) keeps working — the
normalisers are applied at every read/write boundary.
"""

from __future__ import annotations

from typing import Any

# The canonical four depth levels, shallow → deep. This tuple is THE order every
# stage iterates; import it, never re-literal it (DP10 — one source of truth).
RUNGS: tuple[str, str, str, str] = ("L1", "L2", "L3", "L4")

# Legacy rung vocabulary (pre-slide-model) → canonical level. Applied at storage
# read/write, the ops handler, and frontend load so a deck saved (or an op
# authored) before the rename never regresses.
LEGACY_RUNGS: dict[str, str] = {
    "glance": "L1",
    "brief": "L2",
    "working": "L3",
    "expert": "L4",
}

# The slide-model role each level plays — the human label + the depth semantics
# the writer/arranger prompts describe. Kept beside the vocabulary so a caller
# never hard-codes "what L2 means" in two places.
RUNG_ROLE: dict[str, str] = {
    "L1": "cover",       # intro / hook — full-page statement or quote, biggest type
    "L2": "key points",  # the headline substance
    "L3": "detail",      # enriched detail, ≤5 key points
    "L4": "full doc",    # the whole topic, rendered as a doc-view inside the deck
}

# Short display label per level (frontend tabs / index columns mirror this).
RUNG_LABEL: dict[str, str] = {
    "L1": "Cover",
    "L2": "Key Points",
    "L3": "Detail",
    "L4": "Full Doc",
}


def depth_to_rung(depth: Any) -> str:
    """Map a 1-based depth ceiling to its L-level. Clamped to L1..L4.

    Two namespaces have carried the name ``depth_ceiling``: an int produced
    by the compiler's archetype map and consumed by the workflow nodes, and
    an "L1".."L4" string produced by the strategist and consumed by the
    writer / arranger / assembler. Same name, incompatible types, no
    conversion anywhere — so a value could never cross from one half of the
    pipeline to the other. These two functions are that conversion, and they
    live beside the vocabulary they convert to (DP10).
    """
    try:
        i = int(depth)
    except (TypeError, ValueError):
        return RUNGS[-1]
    return RUNGS[min(max(i, 1), len(RUNGS)) - 1]


def rung_to_depth(rung: Any) -> int:
    """Inverse of :func:`depth_to_rung`. Unknown values map to the deepest level."""
    canon = normalize_rung(rung)
    try:
        return RUNGS.index(canon) + 1
    except ValueError:
        return len(RUNGS)


def normalize_rung(name: Any) -> Any:
    """Map a single (possibly legacy) rung name to its canonical L-level. A name
    already canonical, or an unknown value, is returned unchanged — so the shim is
    idempotent and never invents a level."""
    return LEGACY_RUNGS.get(name, name)


def normalize_rungs(rungs: Any) -> Any:
    """Rewrite a facet's ``rungs`` dict's legacy keys to canonical L-levels.
    Returns a NEW dict (input untouched). A non-dict is returned as-is. When both a
    legacy key and its canonical twin are present (a half-migrated doc), the
    canonical one wins — never silently overwritten by the legacy alias."""
    if not isinstance(rungs, dict):
        return rungs
    out: dict[Any, Any] = {}
    for key, value in rungs.items():
        canon = normalize_rung(key)
        if canon in out and canon != key:
            continue  # canonical twin already placed — don't let the legacy alias clobber it
        out[canon] = value
    return out


def normalize_doc_rungs(doc: Any) -> Any:
    """Normalise every facet's rung keys in a full doc payload, in place. Safe on a
    doc with no facets. Returns the doc for chaining."""
    if not isinstance(doc, dict):
        return doc
    facets = doc.get("facets")
    if isinstance(facets, dict):
        for facet in facets.values():
            if isinstance(facet, dict) and isinstance(facet.get("rungs"), dict):
                facet["rungs"] = normalize_rungs(facet["rungs"])
    # entry_rung may also carry a legacy value.
    if "entry_rung" in doc:
        doc["entry_rung"] = normalize_rung(doc.get("entry_rung"))
    return doc


__all__ = [
    "RUNGS",
    "LEGACY_RUNGS",
    "RUNG_ROLE",
    "RUNG_LABEL",
    "normalize_rung",
    "normalize_rungs",
    "normalize_doc_rungs",
]
