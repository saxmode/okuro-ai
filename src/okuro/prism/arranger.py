# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism Component Arranger — for one topic, choose and arrange the
#   visual modules (blocks) per rung from the topic's CLAIM DATA (figures/options/
#   risks/…), honouring depth=density and using only real values. Stage 6 of the
#   root-doc→deck pipeline. Implements the prism-component-expert role charter.
# index: _ARRANGE_SYSTEM | arrange_topic
# AGENT_HEADER_END -->
"""Prism Component Arranger.

Turns a topic's rung prose + its CLAIMS into visual components. Unlike the generic
enrichment pass, it sees the structured claim data (a figure claim → a stat/chart
with the real number; an option claim → an options block; a risk claim → a risk
register) so modules carry real values, not fabrications. Places denser modules on
deeper rungs (depth = density) and never on a rung above the audience ceiling.

Mirrors the ``prism-component-expert`` charter (roles/catalog).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("okuro.prism.arranger")

from okuro.prism.rungs import RUNGS as _RUNGS

# L1 is the COVER: prose-first, but MAY carry the single L1 hook as one impact
# line — never a data module. The block types allowed to occupy the one L1 slot.
_COVER_IMPACT_TYPES = frozenset({"statement", "stat", "quote"})


def _cap_cover(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """L1 is the cover: keep AT MOST ONE impact block (the L1 hook slot), dropping
    every data module. Relaxes the old hard 'L1 → always empty' just enough to
    permit one impact line; the hook itself is placed by the assembler
    (faithfulness-gated), so in the default path the arranger still emits none."""
    return [b for b in blocks
            if isinstance(b, dict) and b.get("type") in _COVER_IMPACT_TYPES][:1]

# The arranger's palette is the SHARED canonical catalog (generate._RICHBLOCK_CATALOG)
# — every renderer-supported module, one source of truth. The intro + rules below
# wrap it; the catalog itself is injected at call time (lazy import avoids a cycle).
_ARRANGE_INTRO = (
    "You are okuro·prism's COMPONENT ARRANGER. For ONE topic you are given its CLAIMS "
    "(the only real data) and its rung bodies. Emit the visual MODULES that fit the "
    "content, per rung, using ONLY values that appear in the claims. Output ONLY a JSON "
    "object.\n\n"
    "MODULE CATALOG — pick the module whose SHAPE matches the content and emit the EXACT "
    "JSON shape shown. Reach for the RIGHT form, not the nearest habit: a relationship / "
    "architecture map is a graph; a per-stakeholder reframing is a lens; a budget / "
    "latency / proportion is a meter; an auditable decision is a decisionrecord; cited "
    "load-bearing facts are evidence; a linear flow is a diagram; competing choices are "
    "options; an interactive what-if on 2-4 numbers is a simulator. Do NOT collapse every "
    "topic into stat / table / steps — visual SAMENESS across topics is a defect. When a "
    "rung's substance is ONE dominant claim — a single takeaway, a definitional assertion, "
    "or what a headline number MEANS — emit a statement block carrying that one line at "
    "large type (or a quote block if the input has a REAL verbatim quote), NOT three tepid "
    "modules or a wall of prose: one bold idea per screen lands harder than a crowded slide. "
    "A rung wrapped in the shape 'body' is prose; you emit only the blocks list.\n\n"
    "MODULE SHAPES (the only permitted block types — anything else is dropped):\n"
)

_ARRANGE_RULES = (
    "\nDEPTH = DENSITY, RELATIVE TO THE DEEPEST LEVEL PRESENT (the audience's ceiling):\n"
    "  L1 → the COVER: prose-first, NO data modules. At most ONE short impact line (the "
    "hook) may sit here — never a table / chart / list. Default empty.\n"
    "  the DEEPEST present level → the DENSEST modules. It is the ceiling — the deepest the "
    "audience reads — so it CARRIES the deck's visual weight: give it the substantive/full "
    "modules (tables, charts, options, risk register, matrix, compare). Do NOT starve it "
    "just because it is 'only' L2; if L2 is the ceiling, L2 is where the data goes.\n"
    "  any level BETWEEN L1 and the deepest → scale up toward the deepest (heavier than "
    "L1, lighter than the ceiling).\n"
    "Density must INCREASE with depth across the PRESENT levels; never place a heavier module "
    "on a shallower level. Only emit a level's list if that level body is present.\n\n"
    "SHAPE → FORM (choose the module whose SHAPE matches the claim, not the nearest habit):\n"
    "  single-value (one dominant number/takeaway)→ stat or statement — NEVER a one-bar chart;\n"
    "  part-of-whole (a share of a total)→ meter or pie; ranking (ordered magnitudes)→ bar chart "
    "or table; time-series (a value over time)→ line chart or timeline; comparison (A vs B, "
    "option×criteria)→ compare / matrix / options; distribution (a spread/uncertainty)→ hops; "
    "relationship (deps/architecture)→ graph; process (a linear flow)→ steps or diagram; "
    "text (an assertion or a REAL quote)→ statement / quote; hierarchy (a tree)→ graph or cards.\n"
    "THE NUMBER IS THE POINT: when a level turns on ONE figure, emit a big stat carrying it — a "
    "single-bar or single-slice chart is a defect (a chart's axis earns its keep only across ≥2 "
    "categories).\n"
    "\nLAYOUT CRAFT — established practice, not taste (Müller-Brockmann grid · Bringhurst measure · "
    "Ruder rhythm & contrast · Bauhaus asymmetry · NN/g inverted-pyramid + chunking · bento "
    "size-as-hierarchy). Let it govern WHICH and HOW MANY modules you emit per level:\n"
    "  1. ONE IDEA PER SCREEN (inverted pyramid): every level leads with its single most important "
    "point. L1 is that idea alone. Never open with three tepid modules — a reader who stops after "
    "the first still gets the message.\n"
    "  2. FOCAL HIERARCHY (asymmetry, size=hierarchy): a dense level has ONE dominant 'hero' module "
    "(a graph, a full chart, a matrix, a rich table, a simulator) that carries the level, plus at "
    "most a few supporting ones — NOT N equal-weight modules. Even weight reads as no hierarchy.\n"
    "  3. CHUNK, DON'T LIST (Miller 3–5, bento): a set of peer items is ONE grouped module "
    "(cards / options / compare / smallmultiples), 3–5 tiles — never a 12-item wall or N separate "
    "stats. Group, then let one tile out-size the rest when one matters more.\n"
    "  4. CONTRAST ACROSS SCREENS (Ruder rhythm): vary the LEAD module level-to-level and "
    "facet-to-facet — visual sameness (stat→table→steps everywhere) kills scanning. Reach for a "
    "different hero form than the sibling levels used.\n"
    "  5. MEASURE & WHITESPACE (Bringhurst, Ma): keep prose SHORT — the module carries the "
    "structure, prose is only connective tissue. Fewer, stronger modules with room to breathe beat "
    "a crowded screen.\n"
    "TAG EACH BLOCK: on every block you emit, add \"shape\":<one of single-value|part-of-whole|"
    "ranking|time-series|comparison|distribution|relationship|process|text|spatial|hierarchy> and "
    "\"fit\":<0.0-1.0, how well this module form fits that claim shape>. A decision/risk/evidence/"
    "ADR module carries a purpose, not a shape — tag it \"text\" with your honest fit. These two "
    "keys are metadata; they are stripped before render.\n\n"
    "HARD RULES: every number/name/row must come from a CLAIM — never invent data. If the "
    "claims don't support a module, emit fewer modules. A figure claim → stat/chart; an "
    "option claim → options; a risk/anti-pattern claim → risk or callout; a comparison → "
    "compare/table; a sequence → timeline/steps.\n\n"
    'Return exactly: {"L1":[],"L2":[...],"L3":[...],"L4":[...]}. Rungs not '
    "present in the input get an empty list."
)


def arrange_topic(
    rungs: dict[str, str],
    claims: list[dict[str, Any]],
    *,
    brief: Optional[dict[str, Any]] = None,
    feedback: Optional[str] = None,
    personas: Optional[list[dict[str, Any]]] = None,
    shape_hints: Optional[list[dict[str, Any]]] = None,
    provider: Optional[str] = None,
) -> dict[str, dict[str, Any]]:
    """Arrange modules for one topic. ``rungs`` = present rung bodies; ``claims`` =
    the topic's claims. Returns {rung: {"blocks": [...], "layout": {...}|None}} for
    each present rung, filtered to the block whitelist with a derived layout.

    ``shape_hints`` (from ``composer.shape_group_hints``) surfaces clusters of
    same-shape claims that should feed ONE multi-item component (the chart-unlock
    the arranger otherwise never sees). None/empty → the prompt is unchanged."""
    from okuro.prism.generate import (
        _RICH_BLOCK_TYPES,
        _RICHBLOCK_CATALOG,
        _extract_json_span,
        _lenient_json_loads,
    )
    from okuro.prism.layout import derive_rung_layout
    from okuro.prism.shapes import refine_blocks

    system = _ARRANGE_INTRO + _RICHBLOCK_CATALOG + _ARRANGE_RULES

    present = {r: (rungs.get(r) or "").strip() for r in _RUNGS if (rungs.get(r) or "").strip()}
    claim_stmts = [c for c in (claims or []) if (c.get("statement") or "").strip()]
    if not present or not claim_stmts:
        return {r: {"blocks": [], "layout": None} for r in present}

    claim_block = "\n".join(
        f"- ({c.get('kind', 'fact')}) {c['statement'].strip()}" for c in claim_stmts
    )
    rung_block = "\n\n".join(f"[{r}]\n{present[r]}" for r in _RUNGS if r in present)
    ceiling = (brief or {}).get("depth_ceiling") or "L4"
    fb_block = (
        f"\n\nCRAFT FEEDBACK (a prior arrangement of this topic was flagged — fix it): "
        f"{feedback.strip()}\n"
        "Choose a RICHER, more fitting module form for this topic's content (still claims-only)."
        if (feedback or "").strip() else ""
    )
    persona_list = [p for p in (personas or []) if (p.get("label") or "").strip()]
    persona_block = ""
    if persona_list:
        rows = "\n".join(f"  - {p['label']}: {(p.get('lens') or '').strip() or 'no profile'}"
                         for p in persona_list)
        persona_block = (
            f"\n\nAUDIENCE PERSONAS (the actual people who will read this) — {len(persona_list)} seats:\n"
            f"{rows}\n"
            "IF this topic's implication genuinely lands DIFFERENTLY for these seats, emit EXACTLY "
            "ONE lens block on the DEEPEST present rung: {\"type\":\"lens\",\"tabs\":[{\"label\":<persona "
            "name>,\"md\":<the topic's takeaway reframed for THAT seat's concern, 1-2 sentences, "
            "claims-only>} ...]} — one tab per persona, in the order given. If the topic lands the "
            "SAME for everyone, DO NOT force a lens block. Never invent a concern the lens/claims don't support."
        )
    # Chart-unlock: clusters of >=2 claims sharing a groupable data shape should
    # feed ONE multi-item component (a bar chart, a compare, a small-multiples
    # grid) rather than N separate stats/sentences. A CANDIDATE, not a mandate —
    # the arranger still decides, and refine_blocks downgrades a 1-datum chart.
    hints = [h for h in (shape_hints or [])
             if isinstance(h, dict) and h.get("shape") and h.get("suggest")]
    hint_block = ""
    if hints:
        rows = "\n".join(
            f"  - {h['shape']} ×{h.get('count', 2)} → prefer one {h['suggest']}"
            for h in hints[:6]
        )
        hint_block = (
            "\n\nSHAPE GROUPS — these claims SHARE a data shape; when they belong "
            "together, emit ONE multi-item component instead of N separate stats or "
            "prose sentences (this is where the quantitative family — chart / "
            "smallmultiples / compare / matrix — earns its keep; claims-only, never "
            "invent a value):\n" + rows
        )
    user = (
        f"DEPTH CEILING: {ceiling}\n\n"
        f"TOPIC CLAIMS (your only data source):\n{claim_block}\n\n"
        f"RUNG BODIES:\n{rung_block}"
        f"{hint_block}"
        f"{persona_block}"
        f"{fb_block}\n\n"
        "Arrange the modules. Return ONLY the JSON object."
    )

    # Reasoning budget: choosing the RIGHT module per rung (not the habitual one)
    # is a judgment task — give it real thinking instead of the pipeline's default 0.
    _prev = os.environ.get("MAX_THINKING_TOKENS")
    os.environ["MAX_THINKING_TOKENS"] = "8000"
    try:
        from okuro.bridge.invoke import invoke
        res = invoke(prompt=user, system_prompt=system, provider=provider, capability="standard", timeout=300)
    finally:
        if _prev is None:
            os.environ.pop("MAX_THINKING_TOKENS", None)
        else:
            os.environ["MAX_THINKING_TOKENS"] = _prev

    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError((res or {}).get("error") if isinstance(res, dict) else "invoke failed")

    out: dict[str, dict[str, Any]] = {r: {"blocks": [], "layout": None, "fit": None} for r in present}
    try:
        data = _lenient_json_loads(_extract_json_span(res.get("output") or "", "{", "}"))
        if isinstance(data, dict):
            for r in present:
                blocks = [
                    b for b in (data.get(r) or [])
                    if isinstance(b, dict) and b.get("type") in _RICH_BLOCK_TYPES
                ]
                # L1 stays the cover: at most ONE impact block (the L1 hook slot)
                # survives, never a data module (see _cap_cover).
                if r == "L1":
                    blocks = _cap_cover(blocks)
                # WS-3: deterministic corrector — fix the number-is-the-point
                # anti-pattern (a one-datum chart → a stat), strip the shape/fit
                # tags, and score the rung's weakest module fit for the craft gate.
                blocks, fit = refine_blocks(blocks)
                layout = derive_rung_layout({"blocks": blocks}) if blocks else None
                out[r] = {"blocks": blocks, "layout": layout, "fit": fit}
    except (ValueError, KeyError) as exc:
        logger.warning("arrange_topic: parse failed: %s", exc)
    return out
