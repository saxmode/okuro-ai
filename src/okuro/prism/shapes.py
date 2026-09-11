# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism CLAIM-SHAPE knowledge — the deterministic half of the
#   arrangement-intelligence loop (WS-3). Maps each of 11 canonical claim shapes
#   to the block types that can FAITHFULLY express it (expressiveness, hard) and
#   an effectiveness rank (Mackinlay channels, soft), and refines an arranger's
#   proposed blocks: it corrects the "number is the point" anti-pattern (a
#   one-datum chart → a big stat) and scores each block's fit so a weak fit can
#   drive the craft gate. LLM proposes shape+fit; this module corrects + scores.
# index: SHAPES | EXPRESSIVENESS | EFFECTIVENESS | refine_blocks | block_fit
# AGENT_HEADER_END -->
"""Prism claim-shape → component knowledge (WS-3 deterministic corrector).

The arranger (an LLM) proposes visual modules AND, per block, tags the claim
SHAPE it is expressing plus a self-assessed ``fit`` 0-1. This module is the
deterministic half that the plan calls for ("LLM proposes; deterministic packer
+ critic correct" — no Draco/ASP solver):

* **Expressiveness (hard).** For each shape, the set of block types that can
  express it truthfully. A type outside the set is a mismatch.
* **Effectiveness (soft, Mackinlay).** For each shape, block types ranked best
  channel first. The top entry is the Show-Me default for that shape.
* **"The number is the point."** A single dominant value rendered as a one-bar /
  one-slice chart is the canonical anti-pattern — a chart's axis earns its keep
  only across ≥2 categories. ``refine_blocks`` rewrites such a chart to a ``stat``
  (a faithful, data-preserving transform — same datum, stronger form).

Design guardrail (mirrors ``layout.py``): the corrector can only IMPROVE a rung,
never destroy real data. The only *mutation* it applies is the number-is-the-point
downgrade (data-preserving). Every other mismatch is *scored* (a low fit) and left
intact for the LLM critic to judge — never silently dropped. Shape-agnostic
rigor/decision modules (a risk register, an ADR, an evidence label) are governed
by content purpose, not visual shape, so the fit scorer abstains on them.
"""

from __future__ import annotations

from typing import Any, Optional

from okuro.prism.rungs import RUNGS

# The 11 canonical claim shapes (D-arranger.md · Mackinlay / Show-Me / Voyager).
SHAPES = (
    "single-value",     # one dominant number or takeaway
    "part-of-whole",    # composition / share of a total
    "ranking",          # ordered magnitudes across categories
    "time-series",      # a value over an ordered sequence / time
    "comparison",       # A vs B, before→after, option × criteria
    "distribution",     # a spread / range / uncertainty
    "relationship",     # deps, network, architecture (non-linear)
    "process",          # a linear flow / pipeline / sequence
    "text",             # an assertion or a verbatim quote
    "spatial",          # geographic / physical layout
    "hierarchy",        # a tree / nesting
)

# Expressiveness (HARD): the block types that can FAITHFULLY express each shape.
# A type outside its shape's set is a genuine mismatch (scored, not mutated).
EXPRESSIVENESS: dict[str, frozenset[str]] = {
    "single-value":  frozenset({"stat", "statement", "meter", "frequency"}),
    "part-of-whole": frozenset({"meter", "chart", "frequency", "stat", "table"}),
    "ranking":       frozenset({"chart", "table", "meter", "matrix"}),
    "time-series":   frozenset({"chart", "timeline", "smallmultiples", "hops"}),
    "comparison":    frozenset({"compare", "matrix", "table", "options", "chart", "cards"}),
    "distribution":  frozenset({"hops", "chart", "smallmultiples", "frequency"}),
    "relationship":  frozenset({"graph", "diagram", "flow"}),
    "process":       frozenset({"steps", "diagram", "flow", "timeline"}),
    "text":          frozenset({"statement", "quote", "callout", "evidence"}),
    "spatial":       frozenset({"image", "gallery", "diagram", "visualizer"}),
    "hierarchy":     frozenset({"graph", "diagram", "cards", "spec", "steps"}),
}

# Effectiveness (SOFT): best channel first. ``[0]`` is the Show-Me default.
EFFECTIVENESS: dict[str, tuple[str, ...]] = {
    "single-value":  ("stat", "statement", "meter", "frequency"),
    "part-of-whole": ("meter", "chart", "frequency", "table"),
    "ranking":       ("chart", "table", "meter"),
    "time-series":   ("chart", "smallmultiples", "timeline", "hops"),
    "comparison":    ("compare", "matrix", "options", "table"),
    "distribution":  ("hops", "chart", "frequency"),
    "relationship":  ("graph", "diagram", "flow"),
    "process":       ("steps", "diagram", "flow", "timeline"),
    "text":          ("statement", "quote", "callout"),
    "spatial":       ("image", "gallery", "diagram"),
    "hierarchy":     ("graph", "diagram", "cards"),
}

# Shape-agnostic modules: governed by content PURPOSE (a decision, a risk, an
# audit trail, an interaction), not by one of the 11 visual shapes. The fit
# scorer abstains on these so a legitimate ADR/risk-register is never penalised
# for "not matching" a shape it was never meant to.
_SHAPE_AGNOSTIC = frozenset({
    "decisionrecord", "risk", "assumptionledger", "tripwires", "biascheck",
    "evidence", "messageproof", "redteam", "cta", "callout", "checklist",
    "code", "spec", "lens", "simulator", "heading", "audiobrief", "visualizer",
})


def default_component(shape: str) -> Optional[str]:
    """The Show-Me default block type for a shape (best effectiveness channel)."""
    ranked = EFFECTIVENESS.get(shape)
    return ranked[0] if ranked else None


def _is_single_datum_chart(block: dict[str, Any]) -> bool:
    """True for a one-bar / one-slice / one-point chart — the anti-pattern where
    a chart's axis buys nothing over a single big number."""
    if block.get("type") != "chart":
        return False
    series = block.get("series")
    return isinstance(series, list) and len(series) < 2


def _chart_to_stat(block: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a one-datum chart to a ``stat`` tile — same value, stronger form.
    Data-preserving: the single series point becomes the tile's value+label, the
    chart unit is folded into the value, and any source_ref is carried across."""
    series = block.get("series") or []
    pt = series[0] if series else {}
    raw = pt.get("value")
    unit = (block.get("unit") or "").strip()
    if isinstance(raw, bool):
        value = str(raw)
    elif isinstance(raw, (int, float)):
        # Drop a trailing .0 so 42.0 → "42"; keep real decimals.
        num = int(raw) if float(raw).is_integer() else raw
        value = f"{num}{unit}" if unit else str(num)
    else:
        value = f"{raw}{(' ' + unit) if unit else ''}".strip() if raw is not None else (unit or "—")
    item: dict[str, Any] = {"value": value, "label": pt.get("label") or block.get("label") or ""}
    stat: dict[str, Any] = {"type": "stat", "items": [item]}
    if block.get("source_ref"):
        stat["source_ref"] = block["source_ref"]
    return stat


def block_fit(block_type: str, shape: Optional[str], llm_fit: Optional[float]) -> Optional[float]:
    """Score how well ``block_type`` fits ``shape``, blended with the LLM's own
    ``llm_fit`` self-assessment. Returns 0-1, or None when the shape system does
    not govern this block (unknown shape, or a shape-agnostic module) — a None
    fit is 'no opinion', never a penalty.

    Deterministic component:
      * top-2 effectiveness channel for the shape → 0.9 (a best/near-best form)
      * expressible (in the shape's set) but not top-2   → 0.7
      * a general data/visual module OUTSIDE the set     → 0.35 (a real mismatch)
    """
    if not shape or shape not in EXPRESSIVENESS:
        det = None
    elif block_type in EFFECTIVENESS.get(shape, ())[:2]:
        det = 0.9
    elif block_type in EXPRESSIVENESS[shape]:
        det = 0.7
    elif block_type in _SHAPE_AGNOSTIC:
        det = None                      # purpose-governed — abstain
    else:
        det = 0.35                      # a general module on the wrong shape
    llm = llm_fit if isinstance(llm_fit, (int, float)) and 0.0 <= llm_fit <= 1.0 else None
    if det is None and llm is None:
        return None
    if det is None:
        return round(float(llm), 3)
    if llm is None:
        return round(det, 3)
    return round((det + float(llm)) / 2, 3)          # blend both signals


def refine_blocks(blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Optional[float]]:
    """Deterministically refine an arranger's proposed blocks for one rung.

    Reads the optional ``shape`` and ``fit`` tags the LLM put on each block,
    applies the number-is-the-point correction (a one-datum chart → a stat),
    scores each block's fit, and STRIPS the helper tags so they never reach the
    saved doc. Returns ``(refined_blocks, min_fit)`` where ``min_fit`` is the
    weakest governed fit in the rung (None when no block is shape-governed) — the
    signal the craft gate uses to trigger a compose-first re-arrange.
    """
    refined: list[dict[str, Any]] = []
    fits: list[float] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        b = dict(b)
        shape = b.pop("shape", None)
        llm_fit = b.pop("fit", None)
        shape = shape.strip().lower() if isinstance(shape, str) else None

        # The number is the point: a one-datum chart is never the right form.
        if _is_single_datum_chart(b):
            b = _chart_to_stat(b)
            shape = shape or "single-value"

        f = block_fit(b.get("type", ""), shape, llm_fit)
        if f is not None:
            fits.append(f)
        refined.append(b)
    return refined, (min(fits) if fits else None)


# Per-block DATAPOINT count — the unit of "how much DATA a rung carries", used to
# enforce that a deeper rung is DENSER, not just wordier (the deep-detail fix).
# A datapoint is one discrete fact the block renders: a stat tile, a table row, a
# chart point, a graph node/edge, an option, a matrix row, a step, an event, etc.
_DP_LEN_FIELD = {
    "stat": "items", "table": "rows", "chart": "series", "options": "options",
    "matrix": "rows", "meter": "items", "steps": "steps", "timeline": "events",
    "frequency": "items", "risk": "items", "cards": "cards", "evidence": "claims",
    "assumptionledger": "assumptions", "checklist": "items", "spec": "cards",
    "smallmultiples": "panels", "biascheck": "biases", "tripwires": "wires",
    "messageproof": "messages", "gallery": "images", "lens": "tabs",
}
# Single-assertion blocks carry exactly one datapoint; structural/prose blocks zero.
_DP_ONE = frozenset({"statement", "quote", "callout", "code", "cta", "decisionrecord",
                     "image", "diagram", "hops"})


def block_datapoints(block: dict[str, Any]) -> int:
    """How many discrete facts a block renders. Used to measure rung density so a
    deeper rung can be required to carry MORE data than a shallower one."""
    if not isinstance(block, dict):
        return 0
    t = block.get("type")
    if t in _DP_LEN_FIELD:
        v = block.get(_DP_LEN_FIELD[t])
        return len(v) if isinstance(v, list) else 0
    if t == "graph":
        return len(block.get("nodes") or []) + len(block.get("edges") or [])
    if t == "compare":
        return len((block.get("before") or {}).get("items") or []) + \
               len((block.get("after") or {}).get("items") or [])
    if t in _DP_ONE:
        return 1
    return 0


def rung_datapoints(blocks: list[dict[str, Any]] | None) -> int:
    """Total datapoints across a rung's blocks."""
    return sum(block_datapoints(b) for b in (blocks or []))


def ladder_density_violations(rung_blocks: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Given {rung: blocks} for the PRESENT rungs, return the rungs whose datapoint
    density INVERTS (a deeper rung carrying strictly fewer datapoints than a
    shallower present rung) — the exact 'L4 is thinner than L3' defect.
    Empty list = monotonic non-decreasing density (the invariant holds)."""
    present = [(r, rung_datapoints(rung_blocks.get(r))) for r in RUNGS if r in rung_blocks]
    violations: list[str] = []
    peak = -1
    for r, dp in present:
        if dp < peak:
            violations.append(r)
        peak = max(peak, dp)
    return violations


# Shapes whose value comes from MULTIPLE items together — a chart axis, a graph, a
# timeline earn their keep only across >= _GROUP_MIN same-shape claims. single-value
# and text are inherently per-claim (a stat / a statement each) and never grouped.
_GROUPABLE = frozenset({
    "ranking", "time-series", "part-of-whole", "comparison",
    "distribution", "relationship", "process", "hierarchy", "spatial",
})
_GROUP_MIN = 2


def group_shapes(claims: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Cluster claims that SHARE a groupable data shape into candidate visual
    groups — the signal that N same-shape claims should feed ONE multi-item
    component (three ranking claims → one bar chart) instead of N single stats.
    This is the chart-unlock: a per-claim view hides the opportunity; grouping
    surfaces it.

    Returns groups sorted biggest-first::

        [{"shape": str, "claim_ids": [...], "count": int, "suggest": <block>}, ...]

    where ``suggest`` is the Show-Me default component for that shape. A group is a
    CANDIDATE, not a mandate — two claims sharing a shape may belong to two
    different charts; the arranger (LLM) makes the final call, this only surfaces
    what the opportunity is. ``single-value`` and ``text`` are never grouped.
    """
    by_shape: dict[str, list[str]] = {}
    for c in claims or []:
        if not isinstance(c, dict):
            continue
        shape = c.get("shape")
        cid = (c.get("id") or "").strip()
        if shape in _GROUPABLE and cid:
            by_shape.setdefault(shape, []).append(cid)
    groups = [
        {"shape": s, "claim_ids": ids, "count": len(ids), "suggest": default_component(s)}
        for s, ids in by_shape.items()
        if len(ids) >= _GROUP_MIN
    ]
    # Biggest opportunity first; stable secondary sort by shape name for determinism.
    groups.sort(key=lambda g: (-g["count"], g["shape"]))
    return groups


__all__ = [
    "SHAPES",
    "EXPRESSIVENESS",
    "EFFECTIVENESS",
    "default_component",
    "block_fit",
    "refine_blocks",
    "block_datapoints",
    "rung_datapoints",
    "ladder_density_violations",
    "group_shapes",
]
