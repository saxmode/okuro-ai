# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism SLIDE TEMPLATE ENGINE — the fix for "generic slideware".
#   A template is a DESIGNED grid layout with named SLOTS; the binder maps a
#   rung's content to the best-fitting template and fills its slots. This is the
#   intentional-design replacement for the greedy 12-col packer (layout.py) that
#   bailed to a flat stack. Templates encode the size-as-hierarchy, real column
#   tracks, and per-content modules the packer structurally could not express.
# index:
#   TEMPLATES | _num | _parse_metric | _metric_duel | _card_grid | _hero_stat
#   def bind_rung | def stamp_templates
# AGENT_HEADER_END -->
"""okuro·prism slide-template engine (the message → designed-template layer).

The diagnosis (2026-07-22): prism generated prose + components on two decoupled
tracks then greedily packed them into a flat 12-column grid with no hierarchy —
"generic slideware". The gold standard (hand-authored reference decks) uses a
DESIGNED grid per message: real column tracks, one dominant element, micro-viz
in-line, per-cell colour.

This module is the deterministic binder: given a rung's already-arranged blocks
(the real, claim-grounded data) + its level + facet, it selects a designed
TEMPLATE and extracts its SLOTS. The frontend renders the template. When no
template fits, it returns None and the rung falls back to the legacy block
stack — so a deck never regresses while the template library grows.

Pure + extractive: it only re-shapes data already present in the blocks (parses a
compare block's "0.28 recall@5" into {value:0.28, unit:"recall@5"}); it invents
nothing. Stamped at the storage chokepoint like slides.stamp_slide_types.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Template registry — id → the shapes/intents it serves + a human note. The
# binder below owns slot extraction; this is the catalogue the frontend mirrors.
TEMPLATES: dict[str, dict[str, Any]] = {
    "cover": {"note": "one idea, biggest type — L1 statement/quote"},
    "section": {"note": "a chapter divider announcing sub-topics"},
    "metric-duel": {"note": "A-vs-B with numeric metrics — size-as-hierarchy + micro-bars"},
    "card-grid": {"note": "2-4 peer items as a repeat(N,1fr) card grid"},
    "hero-stat": {"note": "one dominant number + supporting context"},
    "doc-view": {"note": "the full prose document inside the deck (L4)"},
    # "stack" is the implicit fallback (no template → legacy BlockGrid).
}

# A metric item = optional ~, a number, an OPTIONAL short unit (KB/ms/%/x…, ≤4
# letters, on its own word boundary so "recall@5" is NOT eaten), then the label.
_METRIC = re.compile(r"^\s*(~?)\s*(-?\d[\d,]*\.?\d*)\s*([A-Za-z%]{0,4})\b(.*)$")


def _parse_metric(item: str) -> Optional[dict[str, Any]]:
    """A compare-item string → {value, display, label}. 'display' = the numeric
    token (~ + number + immediate unit, e.g. '~25 KB'); 'label' = the descriptor
    that remains (e.g. 'recall@5', 'context per query'). None with no number."""
    m = _METRIC.match(item or "")
    if not m:
        return None
    try:
        val = float(m.group(2).replace(",", ""))
    except ValueError:
        return None
    prefix, unit, rest = m.group(1), (m.group(3) or "").strip(), (m.group(4) or "").strip(" -–—·,")
    display = f"{prefix}{m.group(2)}{(' ' + unit) if unit else ''}"
    return {"value": val, "display": display, "label": rest or unit}


def _metric_duel(compare: dict[str, Any]) -> Optional[dict[str, Any]]:
    """A `compare` block (before/after, each a labelled item list) → metric-duel
    slots: for each metric position, two competitors with a bar ratio and a
    winner. Needs both sides numeric on ≥1 aligned position. None otherwise."""
    before, after = compare.get("before") or {}, compare.get("after") or {}
    bi, ai = before.get("items") or [], after.get("items") or []
    b_name = (before.get("label") or "A").strip()
    a_name = (after.get("label") or "B").strip()
    metrics: list[dict[str, Any]] = []
    for pos in range(min(len(bi), len(ai))):
        pb, pa = _parse_metric(bi[pos]), _parse_metric(ai[pos])
        if not pb or not pa:
            continue
        # 'after' is the subject prism is arguing FOR; it wins unless clearly worse.
        hi = max(pb["value"], pa["value"]) or 1.0
        # bigger-is-better vs smaller-is-better is ambiguous from data alone, so
        # scale each bar to the max on its row and let colour mark the argued side.
        row_label = pa["label"] or pb["label"] or f"metric {pos + 1}"
        metrics.append({
            "k": row_label,
            "bars": [
                {"who": a_name, "display": pa["display"], "ratio": round(pa["value"] / hi, 3), "win": True},
                {"who": b_name, "display": pb["display"], "ratio": round(pb["value"] / hi, 3), "win": False},
            ],
        })
    if not metrics:
        return None
    return {"winner": a_name, "loser": b_name, "metrics": metrics}


def _card_grid(cards: dict[str, Any]) -> Optional[dict[str, Any]]:
    items = [c for c in (cards.get("cards") or []) if isinstance(c, dict) and (c.get("title") or "").strip()]
    if len(items) < 2:
        return None
    return {"columns": min(4, len(items)), "cards": [
        {"tag": (c.get("tag") or "").strip(), "name": c["title"].strip(), "desc": (c.get("body") or "").strip()}
        for c in items[:4]
    ]}


def _hero_stat(stat: dict[str, Any]) -> Optional[dict[str, Any]]:
    items = [i for i in (stat.get("items") or []) if isinstance(i, dict) and (i.get("value") or "")]
    if not items:
        return None
    lead, rest = items[0], items[1:]
    return {
        "value": str(lead.get("value")), "label": (lead.get("label") or "").strip(),
        "sub": (lead.get("sub") or "").strip(), "state": lead.get("state"),
        "support": [{"value": str(i.get("value")), "label": (i.get("label") or "").strip()} for i in rest[:3]],
    }


def _first(blocks: list[dict[str, Any]], t: str) -> Optional[dict[str, Any]]:
    return next((b for b in blocks if isinstance(b, dict) and b.get("type") == t), None)


def bind_rung(rung: dict[str, Any], level: str, facet: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Select a designed template for a rung + fill its slots, or None → legacy
    stack. Extractive: slots only re-shape data already on the blocks.

    Priority is by the rung's SLIDE ROLE then its strongest data block: L1→cover/
    section; a compare with numeric metrics→metric-duel; a lead stat→hero-stat;
    peer cards→card-grid; L4→doc-view. A rung whose content has no designed
    template yet returns None (renders as today) — the library grows safely."""
    if not isinstance(rung, dict):
        return None
    slide_type = rung.get("slide_type")
    blocks = [b for b in (rung.get("blocks") or []) if isinstance(b, dict)]

    # Deck slide-roles first (cover/section/doc-view own their whole slide).
    if slide_type in ("cover", "section"):
        # Prefer the composed message headline (punchiest), else the gated hook
        # statement, else the topic headline.
        msg = rung.get("message") if isinstance(rung.get("message"), dict) else {}
        line = (msg.get("headline") or "").strip()
        if not line:
            st = _first(blocks, "statement") or _first(blocks, "quote")
            if st:
                line = (st.get("text") or st.get("quote") or "").strip()
        line = line or (facet.get("headline") or facet.get("title") or "").strip()
        if not line:
            return None
        return {"id": slide_type, "slots": {
            "kicker": (facet.get("title") or "").strip(),
            "line": line,
            "children": len(facet.get("children") or []),
        }}
    if slide_type == "doc-view":
        return {"id": "doc-view", "slots": {"headline": (facet.get("headline") or facet.get("title") or "").strip()}}

    # Content levels: pick the template that the strongest data block supports.
    title = _headline(rung, facet)   # the composed assertion headline (message layer)
    claim = _lede(rung, facet)       # the composed lede, else a trimmed prose lede
    cmp_b = _first(blocks, "compare")
    if cmp_b:
        duel = _metric_duel(cmp_b)
        if duel:
            grid = _card_grid(_first(blocks, "cards") or {})  # optional supporting set
            return {"id": "metric-duel", "slots": {
                "title": title, "claim": claim, **duel,
                "set": grid,  # None-safe; the template renders it only if present
            }}
    cards_b = _first(blocks, "cards")
    if cards_b:
        grid = _card_grid(cards_b)
        if grid:
            return {"id": "card-grid", "slots": {"title": title, "claim": claim, **grid}}
    stat_b = _first(blocks, "stat")
    if stat_b:
        hero = _hero_stat(stat_b)
        if hero:
            return {"id": "hero-stat", "slots": {"title": title, "claim": claim, **hero}}
    # HERO-BLOCK — a designed stage for ANY rich visual module (graph, table,
    # steps, meter, options, timeline, a non-numeric compare…): title + a short
    # claim + the module rendered big via the existing renderer, plus a lens
    # footer when the topic reframes per audience. This is the generic coverage
    # template — the frame + hierarchy + brand tokens are the win, the module
    # keeps its own proven renderer.
    lead = next((b for b in blocks if b.get("type") in _FEATURED), None)
    if lead:
        lens = _first(blocks, "lens")
        return {"id": "hero-block", "slots": {
            "title": title, "claim": claim,
            "block": lead, "lens": lens if lens is not lead else None,
        }}
    return None


def _headline(rung: dict[str, Any], facet: dict[str, Any]) -> str:
    """The composed assertion headline (message layer) for a rung, else the topic
    headline/title as a safe fallback."""
    m = rung.get("message") if isinstance(rung.get("message"), dict) else {}
    return (m.get("headline") or "").strip() or (facet.get("headline") or facet.get("title") or "").strip()


def _lede(rung: dict[str, Any], facet: dict[str, Any]) -> str:
    """The composed lede (message layer), else a trimmed prose lede."""
    m = rung.get("message") if isinstance(rung.get("message"), dict) else {}
    return (m.get("lede") or "").strip() or _lead_line(rung, facet)


# Rich modules that earn a full designed stage (the module keeps its own renderer;
# the template supplies the branded frame + title hierarchy + wide canvas).
_FEATURED = frozenset({
    "graph", "diagram", "table", "steps", "meter", "matrix", "chart",
    "smallmultiples", "options", "timeline", "simulator", "risk", "flow", "compare",
})


def _lead_line(rung: dict[str, Any], facet: dict[str, Any]) -> str:
    """A SHORT framing line for a content template's supporting slot — the rung
    body's first sentence, capped at a word boundary. Extractive. NOT the big
    title (that's the facet headline); the crisp message point is what the future
    message-IR layer will supply — until then this is a bounded prose lede."""
    body = (rung.get("body") or "").strip().lstrip("* ")
    if not body:
        return ""
    first = re.split(r"(?<=[.!?])\s+", body, 1)[0].strip("* ")
    if len(first) > 150:
        first = first[:150].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return first


def stamp_templates(doc: Any) -> int:
    """Stamp ``template`` = {id, slots} on every rung that binds to a designed
    template, in place, at the storage chokepoint. Returns the count. Idempotent;
    a rung with no fitting template is left untouched (renders as the legacy
    stack). Never fabricates — slots re-shape existing block data only."""
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
        for level, rung in rungs.items():
            if not isinstance(rung, dict):
                continue
            bound = bind_rung(rung, level, facet)
            if bound:
                rung["template"] = bound
                n += 1
            else:
                rung.pop("template", None)
    return n


__all__ = ["TEMPLATES", "bind_rung", "stamp_templates"]
