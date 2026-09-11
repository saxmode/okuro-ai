# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism kit COMPONENT CONTRACT (PRISM v4 W1 §3a) — the typed,
#   self-describing manifest for EVERY board-kit component the composition solver
#   (W2) may place on a slide. One row per component: content schema · claim-shape
#   fit-scores · capacity range · intrinsic size (col-spans + height-model ref) ·
#   density_cost in RAW units · emphasis variants · optional provenance slot.
#   This is the machine-readable contract; `python -m okuro.prism.kit.manifest`
#   emits board/component-manifest.json which W2's solver + the calibration
#   harness both consume (single source). The kit OWNS styling; this names the
#   placeable-unit vocabulary and its sizing/fit metadata.
# index: CLAIM_SHAPES | EMPHASIS | Component | COMPONENTS | manifest_json
#   | component | placeable | text_bearing | build_manifest | main
# AGENT_HEADER_END -->
"""Component contract for the PRISM v4 free-composition solver.

The v3 kit shipped 12 fixed-capacity *archetypes* (compiler/archetypes.py). v4's
heart (design artifact 08082bbf §3) is free composition: the solver places
individual *components* on a 12-col grid, not one template per cell. This module
is the typed contract for those components — the thing the beam-search solver
reads to know, per component: what content it accepts, which claim shapes it
serves and how well, how many items it holds, how wide/tall it can be, how much
ink-density it costs, which emphasis roles it can take, and whether it can carry
provenance. It supersedes the per-archetype registry as the placement vocabulary
while archetypes.py remains the L1 hook-slide vocabulary (design §5).

Wave boundary (W1 pre-check D1): `density_cost` here is RAW units —
`base_px` + `per_item_px` = the intrinsic ink-height a component contributes at a
1-column reference width. The *cross-model* density scalar that unifies
archetype-L1 with free-composed L2/L3 is a W2 concern and is NOT defined here.

Height model (W1 exit gate, v4.1 B2): `size.height_model` names the calibrated
model key in board/height-model.json produced by tests/calibrate_heights.py.
`base_px`/`per_item_px` are the solver's cheap first estimate; the calibrated
model is the accurate one. Both are keyed by the same component id.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional

# ── claim-shape vocabulary (compiler/ir.py — single source of truth) ────────────
# metric|delta|comparison|sequence|relationship|proportion|quote|set|trend|
# narrative|verdict. fit-scores below are 0.0 (cannot carry) .. 1.0 (ideal home).
CLAIM_SHAPES: tuple[str, ...] = (
    "metric", "delta", "comparison", "sequence", "relationship", "proportion",
    "quote", "set", "trend", "narrative", "verdict",
)

# Emphasis variants a component can take in the solver's hierarchy (§3c step 1).
# focal   — the ONE dominant unit per L1/L2 slide (dominance >=2x runner-up)
# primary — a strong supporting unit
# support — ordinary body unit
# aside   — de-emphasized / marginal
EMPHASIS: tuple[str, ...] = ("focal", "primary", "support", "aside")


@dataclass(frozen=True)
class Capacity:
    """Item band a component holds FAITHFULLY. `typical` seeds fill-level
    'typical'; min/max seed 'min'/'max' in the specimen gallery + calibration.
    unbounded=True marks high-cardinality components (no upper design cap)."""
    min: int
    typical: int
    max: int
    unbounded: bool = False
    item_noun: str = "item"          # what one item IS (calibration factory key)


@dataclass(frozen=True)
class Size:
    """Intrinsic size envelope on the 12-col deck grid.
    min_cols/max_cols  — legal column-span range (alignment is by construction).
    height_model       — key into board/height-model.json (calibrated f()).
    reflows            — True if the component reflows with container width
                         (container-query / auto-fit): height is width-sensitive,
                         so the height model carries a col_span term. Fixed-layout
                         components are width-invariant (col_span coeff ~ 0)."""
    min_cols: int
    max_cols: int
    height_model: str
    reflows: bool = False


@dataclass(frozen=True)
class Density:
    """RAW density cost (W1 pre-check D1 — NOT the W2 cross-model scalar).
    base_px    — ink-height floor the component contributes empty, at ref width.
    per_item_px— additional ink-height per collection item, at ref width.
    These are the solver's cheap pre-calibration estimate and the seed for the
    calibrated linear height model; units are CSS px at a 1-col reference width
    unless `Size.reflows`, in which case the calibrated model rescales by span."""
    base_px: float
    per_item_px: float


@dataclass(frozen=True)
class Component:
    id: str
    css: str                                   # primary CSS class in components*.css
    tier: str                                  # shared | bespoke | port | high-cardinality
    role: str                                  # one-line what-it-is
    content_schema: dict                       # shape of the JSON the solver fills
    fit: dict[str, float]                      # claim-shape -> fit score 0..1
    capacity: Capacity
    size: Size
    density: Density
    emphasis: tuple[str, ...]                  # emphasis roles this component can take
    provenance: bool = False                   # can surface source_ref on demand
    inline: bool = False                       # a chip/mark placed INSIDE another block
    audit: str = "conforms"                    # design-language audit verdict (§3-a)
    ports_from: Optional[str] = None           # v2 block id if this is a port
    notes: str = ""

    def text_bearing(self) -> bool:
        """True if a text field's char_count drives height (calibration needs a
        char-band sweep). False for count-only components (bars, chips, marks)."""
        return bool(self.content_schema.get("_text_bearing"))

    def placeable(self) -> bool:
        """True if the solver places it as a standalone grid cell (vs inline)."""
        return not self.inline


def _fit(**scores: float) -> dict[str, float]:
    """Fit map with unspecified shapes defaulting to 0.0 (cannot carry)."""
    return {s: float(scores.get(s, 0.0)) for s in CLAIM_SHAPES}


# ════════════════════════════════════════════════════════════════════════════
#  THE COMPONENT SET
#  (a) shared v3 kit — re-audited against the design language (§3 item 3a)
#  (b) v2 ports — provenance/rigor, chart, flow-embed, statement/quote, matrix
#      re-skinned to the design language, consuming ONLY the theme contract (3b)
#  (c) NEW high-cardinality — dense-table, two-column-list, tag-wall (3c)
# ════════════════════════════════════════════════════════════════════════════

COMPONENTS: tuple[Component, ...] = (

    # ─────────────────────────── (a) SHARED KIT ────────────────────────────
    Component(
        id="statement-title", css="slide-title", tier="shared",
        role="the ONE claim of a slide, display type, <=22ch",
        content_schema={"text": "string", "accent": "optional inline span", "_text_bearing": True},
        fit=_fit(narrative=1.0, verdict=0.6, metric=0.4),
        capacity=Capacity(1, 1, 1, item_noun="title"),
        size=Size(6, 12, "statement-title"),
        density=Density(96.0, 0.0),
        emphasis=("focal",),
    ),
    Component(
        id="lede", css="slide-lede", tier="shared",
        role="one-sentence supporting frame under a title, <=70ch",
        content_schema={"text": "string", "_text_bearing": True},
        fit=_fit(narrative=1.0),
        capacity=Capacity(1, 1, 1, item_noun="sentence"),
        size=Size(6, 12, "lede"),
        density=Density(40.0, 0.0),
        emphasis=("support",),
    ),
    Component(
        id="prose", css="prose", tier="shared",
        role="full markdown typography — L3/L4 doc body",
        content_schema={"markdown": "string", "_text_bearing": True},
        fit=_fit(narrative=1.0, quote=0.7, verdict=0.4),
        capacity=Capacity(1, 1, 1, item_noun="block"),
        size=Size(6, 12, "prose", reflows=True),
        density=Density(60.0, 34.0),
        emphasis=("primary", "support"),
        provenance=True,
    ),
    Component(
        id="disclosure-list", css="bullet-list", tier="shared",
        role="expandable evidence rows (mark + title + sub + reveal body)",
        content_schema={"rows": [{"mark": "str", "title": "str", "sub": "str",
                                  "body": "str", "severity": "enum"}], "_text_bearing": True},
        fit=_fit(set=0.9, narrative=0.7, verdict=0.7, quote=0.5),
        capacity=Capacity(3, 5, 7, item_noun="row"),
        size=Size(6, 12, "disclosure-list"),
        density=Density(24.0, 84.0),
        emphasis=("primary", "support"),
        provenance=True,
    ),
    Component(
        id="card-set", css="card-grid", tier="shared",
        role="auto-fit card grid (eyebrow/title/body/footer)",
        content_schema={"cards": [{"eyebrow": "str", "title": "str", "body": "str",
                                   "footer": "str"}], "_text_bearing": True},
        fit=_fit(set=1.0, metric=0.5, comparison=0.4),
        capacity=Capacity(2, 3, 5, item_noun="card"),
        size=Size(6, 12, "card-set", reflows=True),
        density=Density(28.0, 150.0),
        emphasis=("primary", "support"),
        provenance=True,
    ),
    Component(
        id="metric-tiles", css="card-grid", tier="shared",
        role="KPI tiles (label/value/desc) — the number is the point",
        content_schema={"tiles": [{"label": "str", "value": "str", "desc": "str"}]},
        fit=_fit(metric=1.0, proportion=0.5, delta=0.6),
        capacity=Capacity(2, 3, 4, item_noun="tile"),
        size=Size(4, 12, "metric-tiles", reflows=True),
        density=Density(28.0, 110.0),
        emphasis=("focal", "primary"),
    ),
    Component(
        id="callout", css="callout", tier="shared",
        role="left-accent-bar emphasis box (note/info/warn)",
        content_schema={"label": "str", "title": "str", "text": "str",
                        "tone": "enum[accent,info,warn]", "_text_bearing": True},
        fit=_fit(narrative=0.9, verdict=0.7),
        capacity=Capacity(1, 1, 1, item_noun="box"),
        size=Size(6, 12, "callout"),
        density=Density(88.0, 0.0),
        emphasis=("primary", "aside"),
    ),
    Component(
        id="option-grid", css="option-grid", tier="shared",
        role="decision matrix of option cards (label/name/k-v lines, one recommended)",
        content_schema={"options": [{"label": "str", "name": "str", "recommended": "bool",
                                     "lines": [{"k": "str", "v": "str"}]}], "_text_bearing": True},
        fit=_fit(comparison=1.0, delta=0.6, set=0.4),
        capacity=Capacity(3, 3, 5, item_noun="option"),
        size=Size(8, 12, "option-grid", reflows=True),
        density=Density(24.0, 190.0),
        emphasis=("focal", "primary"),
    ),
    Component(
        id="beat-list", css="beat-list", tier="shared",
        role="time-ordered scenario beats (T-mark + body + actor)",
        content_schema={"beats": [{"time": "str", "body": "str", "actor": "str"}], "_text_bearing": True},
        fit=_fit(sequence=1.0, relationship=0.5),
        capacity=Capacity(3, 5, 10, item_noun="beat"),
        size=Size(6, 12, "beat-list"),
        density=Density(24.0, 62.0),
        emphasis=("primary", "support"),
    ),
    Component(
        id="stat-row", css="stat-row", tier="shared",
        role="joined KPI strip (num + label, tone modifiers)",
        content_schema={"cells": [{"num": "str", "label": "str", "tone": "enum"}]},
        fit=_fit(metric=1.0, proportion=0.4),
        capacity=Capacity(2, 4, 4, item_noun="cell"),
        size=Size(6, 12, "stat-row"),
        density=Density(64.0, 0.0),
        emphasis=("focal", "primary"),
    ),
    Component(
        id="persona-row", css="persona-row", tier="shared",
        role="audience roster pills (mark + name + lens)",
        content_schema={"personas": [{"name": "str", "lens": "str", "mark": "enum"}]},
        fit=_fit(set=0.8),
        capacity=Capacity(2, 3, 4, item_noun="pill"),
        size=Size(6, 12, "persona-row", reflows=True),
        density=Density(44.0, 0.0),
        emphasis=("support", "aside"),
    ),
    Component(
        id="lens-reframe", css="lens-tabs", tier="shared",
        role="persona tab switcher — same claim reframed per audience",
        content_schema={"tabs": [{"name": "str", "frame": "str"}],
                        "panes": [{"rows": "list"}], "_text_bearing": True},
        fit=_fit(set=0.9, narrative=0.7, comparison=0.5),
        capacity=Capacity(2, 3, 4, item_noun="lens"),
        size=Size(8, 12, "lens-reframe"),
        density=Density(120.0, 0.0),
        emphasis=("focal", "primary"),
        provenance=True,
    ),
    Component(
        id="story-grid", css="story-grid", tier="shared",
        role="overview chapter cards (num/title/desc/links) — index affordance",
        content_schema={"chapters": [{"num": "str", "title": "str", "desc": "str"}], "_text_bearing": True},
        fit=_fit(set=0.9, narrative=0.5),
        capacity=Capacity(2, 3, 4, item_noun="chapter"),
        size=Size(6, 12, "story-grid", reflows=True),
        density=Density(20.0, 120.0),
        emphasis=("primary", "support"),
    ),

    # ─────────────────────────── (a) BESPOKE KIT ───────────────────────────
    Component(
        id="proportion-bars", css="token-bar-wrap", tier="bespoke",
        role="share-of-whole / budget magnitude bars (label/pct/value/level)",
        content_schema={"rows": [{"label": "str", "pct": "number", "value": "str",
                                  "level": "enum[safe,caution,danger]"}]},
        fit=_fit(proportion=1.0, trend=0.7, metric=0.5),
        capacity=Capacity(2, 4, 8, item_noun="bar"),
        size=Size(6, 12, "proportion-bars"),
        density=Density(20.0, 34.0),
        emphasis=("focal", "primary"),
    ),
    Component(
        id="flow-steps", css="flow-step", tier="bespoke",
        role="numbered agentic-flow steps (num/what/tool)",
        content_schema={"steps": [{"what": "str", "tool": "str", "actor": "enum"}], "_text_bearing": True},
        fit=_fit(sequence=1.0, relationship=0.5),
        capacity=Capacity(4, 6, 8, item_noun="step"),
        size=Size(6, 12, "flow-steps"),
        density=Density(20.0, 56.0),
        emphasis=("primary", "support"),
    ),
    Component(
        id="pipeline-table", css="pipeline-row", tier="bespoke",
        role="multi-column stage comparison table (layer/edge/cloud/cost)",
        content_schema={"headers": ["str"], "rows": [["str"]], "_text_bearing": True},
        fit=_fit(comparison=1.0, set=0.6),
        capacity=Capacity(2, 5, 10, item_noun="row"),
        size=Size(8, 12, "pipeline-table"),
        density=Density(42.0, 48.0),
        emphasis=("primary", "support"),
    ),
    Component(
        id="verb-grid", css="verb-grid", tier="bespoke",
        role="tool/verb schema cards, 3-col (name/desc/params/ack)",
        content_schema={"cards": [{"name": "str", "desc": "str", "params": "str",
                                   "ack": "str"}], "_text_bearing": True},
        fit=_fit(set=1.0, comparison=0.4),
        capacity=Capacity(3, 3, 6, item_noun="card"),
        size=Size(8, 12, "verb-grid", reflows=True),
        density=Density(24.0, 130.0),
        emphasis=("primary", "support"),
    ),
    Component(
        id="cmp-table", css="cmp-table", tier="bespoke",
        role="before->after comparison table with status tags",
        content_schema={"headers": ["str"], "rows": [{"v1": "str", "v2": "str",
                        "tag": "enum"}], "_text_bearing": True},
        fit=_fit(comparison=1.0, delta=1.0),
        capacity=Capacity(2, 5, 12, item_noun="row"),
        size=Size(8, 12, "cmp-table"),
        density=Density(44.0, 44.0),
        emphasis=("primary", "support"),
    ),
    Component(
        id="transform-grid", css="transform-grid", tier="bespoke",
        role="before->after transform cards (before/arrow/after + label)",
        content_schema={"cards": [{"header": "str", "before": "str", "after": "str",
                        "label": "str"}], "_text_bearing": True},
        fit=_fit(delta=1.0, comparison=0.8),
        capacity=Capacity(2, 3, 6, item_noun="card"),
        size=Size(8, 12, "transform-grid", reflows=True),
        density=Density(24.0, 120.0),
        emphasis=("primary", "support"),
    ),
    Component(
        id="phase-row", css="phase-row", tier="bespoke",
        role="roadmap phase chain (done/next/future + arrows)",
        content_schema={"phases": [{"label": "str", "state": "enum[done,next,future]"}]},
        fit=_fit(sequence=0.9, trend=0.8),
        capacity=Capacity(2, 4, 7, item_noun="phase"),
        size=Size(6, 12, "phase-row", reflows=True),
        density=Density(64.0, 8.0),
        emphasis=("primary", "support"),
    ),
    Component(
        id="verdict-callout", css="verdict-callout", tier="bespoke",
        role="summary banner with inline stat dots",
        content_schema={"total": "str", "stats": [{"label": "str", "count": "str"}], "_text_bearing": True},
        fit=_fit(verdict=1.0, metric=0.6),
        capacity=Capacity(1, 3, 5, item_noun="stat"),
        size=Size(8, 12, "verdict-callout", reflows=True),
        density=Density(56.0, 0.0),
        emphasis=("focal", "primary"),
    ),
    Component(
        id="ask-box", css="ask-box", tier="bespoke",
        role="board-ask call-to-action (title + item list)",
        content_schema={"title": "str", "items": ["str"], "_text_bearing": True},
        fit=_fit(verdict=0.9, narrative=0.7),
        capacity=Capacity(1, 3, 6, item_noun="ask"),
        size=Size(6, 12, "ask-box"),
        density=Density(72.0, 26.0),
        emphasis=("focal", "primary"),
    ),
    Component(
        id="arch-diagram", css="arch-diagram", tier="bespoke",
        role="flow boxes colored by health (relationship)",
        content_schema={"items": [{"label": "str", "value": "str", "state": "enum"}]},
        fit=_fit(relationship=0.9, set=0.5),
        capacity=Capacity(2, 3, 5, item_noun="box"),
        size=Size(6, 12, "arch-diagram", reflows=True),
        density=Density(20.0, 74.0),
        emphasis=("primary", "support"),
    ),

    # ───────────────────────── (b) v2 PORTS (re-skinned) ────────────────────
    Component(
        id="provenance", css="provenance", tier="port",
        role="source-ref affordance — surfaced on demand under any block",
        content_schema={"refs": [{"source": "str", "as_of": "str", "url": "str"}], "_text_bearing": True},
        fit=_fit(),  # not a claim carrier — an overlay affordance
        capacity=Capacity(1, 2, 5, item_noun="ref"),
        size=Size(4, 12, "provenance", reflows=True),
        density=Density(16.0, 20.0),
        emphasis=("aside",),
        provenance=True, inline=True,
        ports_from="Provenance (blocks.tsx:166 — on every v2 block)",
        notes="v3 kit had NO provenance affordance (inventory gap). Every block can carry it.",
    ),
    Component(
        id="evidence-list", css="evidence-list", tier="port",
        role="rigor 'nutrition label' — claim + source grade + confidence + as_of",
        content_schema={"claims": [{"claim": "str", "grade": "enum[A,B,C,D]",
                        "confidence": "number", "as_of": "str"}], "_text_bearing": True},
        fit=_fit(verdict=0.8, set=0.7, comparison=0.5),
        capacity=Capacity(2, 4, 10, item_noun="claim"),
        size=Size(8, 12, "evidence-list"),
        density=Density(28.0, 58.0),
        emphasis=("primary", "support"),
        provenance=True,
        ports_from="evidence (rigor-blocks.tsx:34)",
        notes="closes the provenance/rigor claim-shape gap the brief names.",
    ),
    Component(
        id="bias-check", css="bias-check", tier="port",
        role="rigor — cognitive biases + risk + counter-measure",
        content_schema={"biases": [{"bias": "str", "risk": "enum", "counter": "str"}], "_text_bearing": True},
        fit=_fit(verdict=0.7, set=0.7),
        capacity=Capacity(2, 3, 8, item_noun="bias"),
        size=Size(8, 12, "bias-check"),
        density=Density(24.0, 60.0),
        emphasis=("support",),
        provenance=True,
        ports_from="biascheck (rigor-blocks.tsx:91)",
    ),
    Component(
        id="chart", css="chart", tier="port",
        role="dep-free single-hue SVG chart (bar/line/area/pie) + annotations",
        content_schema={"kind": "enum[bar,line,area,pie]", "series": [{"label": "str",
                        "value": "number"}], "annotations": ["str"]},
        fit=_fit(metric=0.8, trend=1.0, proportion=0.9, comparison=0.8, delta=0.6),
        capacity=Capacity(2, 6, 16, item_noun="datum"),
        size=Size(6, 12, "chart", reflows=True),
        density=Density(220.0, 6.0),
        emphasis=("focal", "primary"),
        provenance=True,
        ports_from="chart (chart-block.tsx)",
        notes="closes the chart claim-shape gap the brief names; dep-free SVG.",
    ),
    Component(
        id="flow-embed", css="flow-embed", tier="port",
        role="embed a saved okuro flow by id (framed panel + maximize affordance)",
        content_schema={"flow_id": "str", "title": "str", "nodes": "int", "_text_bearing": True},
        fit=_fit(relationship=1.0, sequence=0.6),
        capacity=Capacity(1, 1, 1, item_noun="flow"),
        size=Size(6, 12, "flow-embed", reflows=True),
        density=Density(300.0, 0.0),
        emphasis=("focal", "primary"),
        provenance=True,
        ports_from="flow (flow-embed.tsx)",
        notes="closes the flow-embed class the brief names; leapfrog v2 mechanic.",
    ),
    Component(
        id="statement", css="statement", tier="port",
        role="one assertion in huge type — the L1 hook / cover claim",
        content_schema={"text": "string", "accent": "optional inline span", "_text_bearing": True},
        fit=_fit(narrative=1.0, verdict=0.8, quote=0.6),
        capacity=Capacity(1, 1, 1, item_noun="assertion"),
        size=Size(8, 12, "statement"),
        density=Density(140.0, 0.0),
        emphasis=("focal",),
        ports_from="statement (typography-blocks.tsx:18)",
        notes="the maximal-impact hook archetype the inventory flagged missing (quote-cover class).",
    ),
    Component(
        id="pull-quote", css="pull-quote", tier="port",
        role="pull-quote + attribution (oversized mark)",
        content_schema={"quote": "str", "attribution": "str", "_text_bearing": True},
        fit=_fit(quote=1.0, narrative=0.6),
        capacity=Capacity(1, 1, 1, item_noun="quote"),
        size=Size(6, 12, "pull-quote"),
        density=Density(120.0, 0.0),
        emphasis=("focal", "primary"),
        provenance=True,
        ports_from="quote (typography-blocks.tsx:45)",
        notes="closes the quote claim-shape gap (kit had only .prose blockquote).",
    ),
    Component(
        id="matrix", css="matrix", tier="port",
        role="feature comparison grid (checkmark/cross/tilde), winner-col tint",
        content_schema={"cols": ["str"], "rows": [{"label": "str",
                        "cells": ["enum[yes,no,partial]"]}], "_text_bearing": True},
        fit=_fit(comparison=1.0, set=0.7, verdict=0.5),
        capacity=Capacity(2, 6, 16, item_noun="row", unbounded=True),
        size=Size(8, 12, "matrix"),
        density=Density(48.0, 40.0),
        emphasis=("primary", "focal"),
        provenance=True,
        ports_from="matrix (data-blocks.tsx:39)",
        notes="feature-comparison distinct from classification-table (verdict).",
    ),

    # ─────────────────── (c) NEW HIGH-CARDINALITY COMPONENTS ────────────────
    # Closes the 13-principles silent-truncation failure class (inventory GAP,
    # HIGH). Each holds 10-20+ items readably where every fixed archetype caps
    # at 3-8. These are v4-native (no v2/v3 antecedent).
    Component(
        id="dense-table", css="dense-table", tier="high-cardinality",
        role="compact open-ended data table, 10-30 rows, zebra + sticky header",
        content_schema={"headers": ["str"], "rows": [["str"]], "row_state": "optional enum",
                        "_text_bearing": True},
        fit=_fit(set=0.9, comparison=0.9, verdict=0.8, metric=0.5),
        capacity=Capacity(8, 16, 30, unbounded=True, item_noun="row"),
        size=Size(8, 12, "dense-table"),
        density=Density(44.0, 30.0),
        emphasis=("primary", "support"),
        provenance=True,
        notes="the 13-principles claim renders here without truncation.",
    ),
    Component(
        id="two-column-list", css="two-col-list", tier="high-cardinality",
        role="10-20 short items balanced across two columns",
        content_schema={"items": [{"title": "str", "sub": "str"}], "_text_bearing": True},
        fit=_fit(set=1.0, narrative=0.4),
        capacity=Capacity(8, 14, 24, unbounded=True, item_noun="item"),
        size=Size(8, 12, "two-column-list", reflows=True),
        density=Density(24.0, 30.0),
        emphasis=("primary", "support"),
        provenance=True,
    ),
    Component(
        id="tag-wall", css="tag-wall", tier="high-cardinality",
        role="10-40 chips as a scannable wall (tech list, capability set, glossary)",
        content_schema={"tags": [{"label": "str", "tone": "optional enum"}]},
        fit=_fit(set=1.0),
        capacity=Capacity(10, 20, 40, unbounded=True, item_noun="tag"),
        size=Size(6, 12, "tag-wall", reflows=True),
        density=Density(20.0, 12.0),
        emphasis=("support", "aside"),
        notes="a 40-item capability set that would overflow card-set fits here.",
    ),

    # ─────────────────────────── INLINE CHIPS (audit only) ──────────────────
    # Placed INSIDE a block, never a standalone grid cell — no height model.
    Component(
        id="tier-badge", css="tier-badge", tier="bespoke", role="pill classifier chip",
        content_schema={"label": "str", "tone": "enum"}, fit=_fit(verdict=0.5),
        capacity=Capacity(1, 1, 1, item_noun="chip"), size=Size(1, 2, "_inline"),
        density=Density(0.0, 0.0), emphasis=("aside",), inline=True),
    Component(
        id="verdict-badge", css="verdict-badge", tier="bespoke", role="status chip (dim tint + ink, AA)",
        content_schema={"label": "str", "tone": "enum"}, fit=_fit(verdict=0.5),
        capacity=Capacity(1, 1, 1, item_noun="chip"), size=Size(1, 2, "_inline"),
        density=Density(0.0, 0.0), emphasis=("aside",), inline=True),
    Component(
        id="method-badge", css="method-badge", tier="bespoke", role="HTTP-method mono chip",
        content_schema={"label": "str"}, fit=_fit(),
        capacity=Capacity(1, 1, 1, item_noun="chip"), size=Size(1, 2, "_inline"),
        density=Density(0.0, 0.0), emphasis=("aside",), inline=True),
)


def component(component_id: str) -> Optional[Component]:
    return next((c for c in COMPONENTS if c.id == component_id), None)


def placeable() -> tuple[Component, ...]:
    """Components the solver places as standalone grid cells (excludes chips)."""
    return tuple(c for c in COMPONENTS if c.placeable())


def text_bearing() -> tuple[Component, ...]:
    """Components whose height depends on char_count (need a char-band sweep)."""
    return tuple(c for c in placeable() if c.text_bearing())


def build_manifest() -> dict:
    """The machine-readable contract W2's solver + the calibration harness read."""
    return {
        "kit_family": "board",
        "version": "v4-w1",
        "claim_shapes": list(CLAIM_SHAPES),
        "emphasis": list(EMPHASIS),
        "density_cost_units": "raw px at 1-col reference width (NOT the W2 cross-model scalar)",
        "height_model_ref": "board/height-model.json (calibrated by tests/calibrate_heights.py)",
        "counts": {
            "total": len(COMPONENTS),
            "placeable": len(placeable()),
            "text_bearing": len(text_bearing()),
            "by_tier": {t: sum(1 for c in COMPONENTS if c.tier == t)
                        for t in ("shared", "bespoke", "port", "high-cardinality")},
        },
        "components": [asdict(c) for c in COMPONENTS],
    }


def manifest_json(indent: int = 2) -> str:
    return json.dumps(build_manifest(), indent=indent, ensure_ascii=False)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(prog="python -m okuro.prism.kit.manifest",
                                 description="Emit the prism component contract JSON.")
    ap.add_argument("-o", "--out", default=str(
        Path(__file__).resolve().parent / "board" / "component-manifest.json"),
        help="output path (default: board/component-manifest.json)")
    ap.add_argument("--stdout", action="store_true", help="print to stdout instead of writing")
    args = ap.parse_args(argv)

    if args.stdout:
        print(manifest_json())
        return 0
    out = Path(args.out)
    out.write_text(manifest_json() + "\n")
    m = build_manifest()
    print(f"wrote {out}")
    print(f"  {m['counts']['total']} components "
          f"({m['counts']['placeable']} placeable, {m['counts']['text_bearing']} text-bearing)")
    print(f"  by tier: {m['counts']['by_tier']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
