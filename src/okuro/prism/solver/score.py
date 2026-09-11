# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 solver core (2/3) — the weighted scorer. Every layout-
#   research rule is ONE explicit weighted score term (v4.1 B1). The score is a
#   sum of named terms so a layout can state WHY it won/lost (the explainability
#   the prism-critic gate needs). Includes the two pre-check fixes: family-
#   conformance (DEFECT 1, answer 7) and per-family focal-dominance reading the
#   frozen dominance_ratio (DEFECT 2).
# index:
#   ScoreBreakdown / WEIGHTS / score_placement (+ per-term functions)
# AGENT_HEADER_END -->
"""The beam scorer — research rules as weighted terms.

Terms (each normalised to ~[0,1]; higher = better):
  reading_gravity  — reading_order follows an F/Z scan path over the geometry.
  alignment        — cell heights snap to the family baseline unit (x strictness).
  whitespace       — MARGIN void (uniform outer margin + gutters) meets the family
                     floor; in-band holes do NOT count (void-as-margin class).
  space_utilization — HOLE void penalty: a band narrower than the widest leaves a
                     dead hole beside content (void-as-hole class, no allowance).
  macro_structure  — one dominant region (band mass vs runner-up, dominance_ratio)
                     + subordinate bands cleanly organised (no trailing spacer).
  density_band     — slide density within the per-family per-level ceiling.
  family_conformance — row asymmetry matches the family asymmetry_bias (answer 7).
  focal_dominance  — focal cell area / runner-up >= the family dominance_ratio.
  balance          — notan mass balance: ink centroid + band occupancy spread +
                     band-end column-height alignment (all asymmetry-aware).

A hard guard asserts every row's spans are in the family whitelist (they are, by
construction in candidates.py) — split membership is not a soft term, it bounds
the search. Deterministic: pure arithmetic over the estimated geometry.
"""
from __future__ import annotations

from dataclasses import dataclass

from okuro.prism.solver.candidates import Cell, Placement, cell_area, cell_height
from okuro.prism.solver.density import ceiling_overflow, slide_density
from okuro.prism.solver.families import GridFamily
from okuro.prism.solver.schema import Claim, SlidePlan

# One weight per research rule. Sum = 1.0 (documented; tune here, nowhere else).
WEIGHTS: dict[str, float] = {
    "reading_gravity": 0.12,
    "alignment": 0.10,
    "whitespace": 0.10,
    "density_band": 0.12,
    "family_conformance": 0.10,
    "focal_dominance": 0.12,
    "balance": 0.10,
    "space_utilization": 0.12,
    "macro_structure": 0.12,
}


@dataclass(frozen=True)
class ScoreBreakdown:
    total: float
    terms: dict[str, float]

    def why(self) -> str:
        parts = [f"{k}={self.terms[k]:.2f}*{WEIGHTS[k]:.2f}" for k in WEIGHTS]
        return f"total={self.total:.3f} [" + " ".join(parts) + "]"


def _cell_xs(rows) -> list[list[tuple[Cell, int]]]:
    """Per row, [(cell, x_start_col)] with running column offset."""
    out = []
    for r in rows:
        x = 0
        row_cells = []
        for c in r.cells:
            row_cells.append((c, x))
            x += c.span
        out.append(row_cells)
    return out


def _reading_gravity(p: Placement, plan: SlidePlan) -> float:
    """Reward reading_order that flows top->bottom, left->right (F/Z). Score =
    fraction of consecutive reading_order pairs that do not backtrack."""
    pos: dict[int, tuple[int, int]] = {}
    for ri, r in enumerate(p.rows):
        x = 0
        for c in r.cells:
            if c.unit is not None:
                pos[c.unit] = (ri, x)
            x += c.span
    seq = [u for u in plan.reading_order if u in pos]
    if len(seq) < 2:
        return 1.0
    good = 0
    for a, b in zip(seq, seq[1:]):
        ra, xa = pos[a]
        rb, xb = pos[b]
        if rb > ra or (rb == ra and xb > xa):
            good += 1
    return good / (len(seq) - 1)


def _alignment(p: Placement, plan: SlidePlan, claims, brand: str, fam: GridFamily) -> float:
    """Reward cell heights near multiples of the baseline unit (x strictness)."""
    base = float(fam.baseline_unit_px)
    fracs = []
    for r in p.rows:
        for c in r.cells:
            if c.unit is None:
                continue
            h = cell_height(c, plan, claims, brand)
            frac = (h % base) / base
            fracs.append(min(frac, 1.0 - frac) * 2.0)     # 0 best, 1 worst
    if not fracs:
        return 1.0
    penalty = sum(fracs) / len(fracs)
    return max(0.0, 1.0 - fam.alignment_strictness * penalty)


def _bands(p: Placement, plan: SlidePlan, claims, brand: str):
    """Per row: (content_cols, height, has_spacer). The shared geometry the void-
    class terms read. content_cols = grid columns carrying a unit; has_spacer =
    the row contains a void cell."""
    out = []
    for r in p.rows:
        h = max((cell_height(c, plan, claims, brand) for c in r.cells), default=0.0)
        ccols = sum(c.span for c in r.cells if c.unit is not None)
        has_spacer = any(c.unit is None for c in r.cells)
        out.append((ccols, h, has_spacer))
    return out


def _whitespace(p: Placement, plan: SlidePlan, claims, brand: str, fam: GridFamily) -> float:
    """MARGIN whitespace vs the family floor/ceiling — the void-as-MARGIN class.
    'Good' whitespace = the uniform outer margin (columns empty in EVERY content
    band, so a framing side-margin like the references' active margins) + inter-row
    gutters. In-band HOLES (a band narrower than the widest -> empty columns
    enclosed beside content) are NOT whitespace here; space_utilization penalises
    them. Only framing void earns the notan floor; a lone half-filled band no longer
    'satisfies' the floor (the quantity-model trap that rewarded a dead hole)."""
    bands = [(cc, h) for cc, h, _ in _bands(p, plan, claims, brand) if cc > 0 and h > 0]
    if not bands:
        return 1.0
    max_content = max(cc for cc, _ in bands)
    margin_cols = 12 - max_content
    total_area = sum(12.0 * h for _, h in bands)
    margin_area = sum(margin_cols * h for _, h in bands)
    gutters = fam.gutter_px * max(0, len(bands) - 1) * 12
    denom = total_area + gutters
    if denom <= 0:
        return 1.0
    good = (margin_area + gutters) / denom
    floor = fam.whitespace_floor
    if floor <= 0:
        return 1.0
    if good < floor:
        # the outer canvas pad (64px/side) + component internal padding already
        # breathe, so an under-margin full-bleed layout is only MILDLY penalised —
        # never the old trap where an in-band hole was needed to clear the floor.
        return 0.6 + 0.4 * (good / floor)
    over = max(0.0, good - fam.max_void)
    return max(0.0, 1.0 - over / (1.0 - fam.max_void))


def _density_band(p: Placement, plan: SlidePlan, claims, fam: GridFamily) -> float:
    placements = [
        (plan.units[c.unit].component, plan.units[c.unit].resolved_items(claims), c.span)
        for r in p.rows for c in r.cells if c.unit is not None
    ]
    d = slide_density(placements)
    ceil = fam.density_ceiling(plan.level)
    over = ceiling_overflow(d, fam, plan.level)
    if over <= 0:
        return 1.0
    return max(0.0, 1.0 - over / ceil)


def _row_asymmetry(spans: tuple[int, ...]) -> float:
    """0 = perfectly even (6|6, 4|4|4); ->1 = extreme (9|3). Single-cell = 0."""
    if len(spans) < 2:
        return 0.0
    return 1.0 - min(spans) / max(spans)


def _family_conformance(p: Placement, fam: GridFamily) -> float:
    """DEFECT 1 fix (answer 7): reward rows whose asymmetry matches the family's
    asymmetry_bias. Asymmetry is measured over CONTENT cells only — a spacer-padded
    row (one 8-col unit + a 4-col void) is NOT a composed asymmetric split, it is
    an under-filled row, so it must not earn asymmetry credit (else the scorer
    trades grid fill for fake asymmetry — the persistent-margin defect). Split
    membership is a hard guard (asserted below)."""
    content_rows = [
        tuple(c.span for c in r.cells if c.unit is not None)
        for r in p.rows
    ]
    multi = [spans for spans in content_rows if len(spans) >= 2]
    if not multi:
        # no composed rows: fine for a low-asymmetry family (swiss/editorial use
        # full-width bands freely); a high-asymmetry family wants composition.
        return 1.0 if fam.asymmetry_bias < 0.6 else 1.0 - 0.4 * fam.asymmetry_bias
    mean_asym = sum(_row_asymmetry(spans) for spans in multi) / len(multi)
    # tolerance band, not an exact target: a calm family accepts everything up to
    # its asymmetry ceiling (so a filled 6|6 is NOT penalised vs an 8|4 — that
    # traded fill for fake asymmetry); a kinetic family is penalised for being too
    # flat. Only EXCESS asymmetry (or, for kinetic families, deficit) costs.
    over = max(0.0, mean_asym - (fam.asymmetry_bias + 0.25))
    under = (max(0.0, (fam.asymmetry_bias - 0.25) - mean_asym)
             if fam.asymmetry_bias >= 0.7 else 0.0)
    return max(0.0, 1.0 - over - under)


def _focal_dominance(
    p: Placement, plan: SlidePlan, claims, brand: str, fam: GridFamily
) -> float:
    """DEFECT 2 fix: focal SPAN / runner-up span >= the per-family dominance_ratio.

    Size hierarchy here is span-driven, not font-px (the architecture-noir brand is mono —
    typographic-layout-systems.md:238) and layout.py rule #10 states dominance as
    'hero span >= 2x supporting' (the 8|4 = 2:1 hero+rail). So dominance is a SPAN
    ratio: a full-width focal (12) alone in its row reads as dominant even when
    short, which an area metric would wrongly penalise. Area still governs balance
    and whitespace; only dominance is span-based. No focal (e.g. L3) -> neutral."""
    focal_spans: list[int] = []
    other_spans: list[int] = []
    for r in p.rows:
        for c in r.cells:
            if c.unit is None:
                continue
            if plan.units[c.unit].emphasis == "focal":
                focal_spans.append(c.span)
            else:
                other_spans.append(c.span)
    if not focal_spans:
        return 1.0
    fs = max(focal_spans)
    runner = max(other_spans) if other_spans else 1
    ratio = fs / runner
    return min(1.0, ratio / fam.dominance_ratio)


def _space_utilization(
    p: Placement, plan: SlidePlan, claims, brand: str, fam: GridFamily
) -> float:
    """HOLE penalty — the void-as-HOLE class, over the whole in-band bounding box.
    Within a band (bounding box = 12 cols x band_height) the void is everything the
    content ink does not fill. Two flavours, both dead holes beside content:
      - HORIZONTAL: a trailing spacer / a band narrower than the widest (empty cols).
      - VERTICAL: a short cell beside a tall one (the hanging-column gap below it).
    Only the UNIFORM outer margin (columns empty in EVERY band -> a framing side
    margin, scored by _whitespace) is exempt; everything else is a hole. Holes get
    NO family void allowance (max_void governs the margin, never a hole). Whole-slide
    hole fraction + the single worst band, so one tall half-empty band is punished
    even beside full rows. This is the eye's 'dead void beside a cluster'."""
    bands = _bands(p, plan, claims, brand)
    content = [(cc, h) for cc, h, _ in bands if cc > 0 and h > 0]
    if not content:
        return 1.0
    max_content = max(cc for cc, _ in content)
    margin_cols = 12 - max_content                          # uniform framing margin (exempt)
    total_bbox = 0.0
    hole = 0.0
    worst = 0.0
    for r in p.rows:
        heights = [cell_height(c, plan, claims, brand) for c in r.cells]
        H = max(heights, default=0.0)
        ccols = sum(c.span for c in r.cells if c.unit is not None)
        if ccols <= 0 or H <= 0:
            continue
        bbox = 12.0 * H
        ink = sum(c.span * h for c, h in zip(r.cells, heights) if c.unit is not None)
        margin_area = margin_cols * H                       # exempt uniform margin
        band_hole = max(0.0, bbox - ink - margin_area)
        total_bbox += bbox - margin_area
        hole += band_hole
        worst = max(worst, band_hole / max(bbox - margin_area, 1.0))
    if total_bbox <= 0:
        return 1.0

    # tiny tolerance (a single narrow rag / slight height jitter), then steep.
    def pen(frac: float) -> float:
        return max(0.0, 1.0 - max(0.0, frac - 0.05) / 0.45)

    return 0.6 * pen(hole / total_bbox) + 0.4 * pen(worst)


def _macro_structure(p: Placement, plan: SlidePlan, claims, brand: str, fam: GridFamily) -> float:
    """Macro composition skeleton — ONE dominant region + subordinate order (the
    'dominant band + supporting bands' read the references win on; the judge's
    'three competing clusters, no dominant block' is its absence). Region = band.
    (1) region dominance: the largest band mass out-weighs the runner-up toward the
    family dominance_ratio, applied at REGION (band) level not component level —
    two equal big bands read as competing clusters. (2) subordinate order: every
    non-dominant content band should be cleanly organised — full-width or a family
    split with NO trailing spacer (a hole makes a supporting band read unfinished)."""
    info = []                                            # (mass, has_spacer)
    for cc, h, has_spacer in _bands(p, plan, claims, brand):
        if cc > 0 and h > 0:
            info.append((cc * h, has_spacer))
    if len(info) < 2:
        return 1.0
    masses = sorted((m for m, _ in info), reverse=True)
    region_dom = min(1.0, (masses[0] / masses[1]) / fam.dominance_ratio) if masses[1] > 0 else 1.0
    dom_mass = masses[0]
    seen_dom = False
    subs: list[bool] = []
    for m, has_spacer in info:
        if not seen_dom and m == dom_mass:
            seen_dom = True                              # exempt exactly one dominant band
            continue
        subs.append(has_spacer)
    sub_order = (sum(1 for hs in subs if not hs) / len(subs)) if subs else 1.0
    return 0.5 * region_dom + 0.5 * sub_order


def _balance(p: Placement, plan: SlidePlan, claims, brand: str, fam: GridFamily) -> float:
    """Notan mass balance over the WHOLE CANVAS (not per-row): the horizontal ink
    centroid AND per-band occupancy spread. Even families want the centroid near mid
    (col 6) and consistent band occupancy; asymmetric families tolerate an off-centre
    centroid (balance by relationship, not mirroring). The occupancy spread term
    penalises 'two disconnected halves' — a slide whose bands swing between near-full
    and near-empty. (Side-by-side column-height mismatch is a VERTICAL hole, scored
    by space_utilization — kept out of here to avoid double-counting.)"""
    ink_x = 0.0
    ink = 0.0
    occ: list[float] = []
    for r in p.rows:
        rh = max((cell_height(c, plan, claims, brand) for c in r.cells), default=0.0)
        x = 0
        content_cols = 0
        for c in r.cells:
            if c.unit is not None:
                a = c.span * rh
                ink_x += a * (x + c.span / 2.0)
                ink += a
                content_cols += c.span
            x += c.span
        occ.append(content_cols / 12.0)
    if ink <= 0:
        return 1.0
    centroid = ink_x / ink                                  # 0..12
    off = abs(centroid - 6.0) / 6.0                         # 0 centred, 1 edge
    tolerance = fam.asymmetry_bias * 0.5
    centroid_score = max(0.0, 1.0 - max(0.0, off - tolerance))
    spread = (max(occ) - min(occ)) if len(occ) > 1 else 0.0
    spread_score = 1.0 - 0.5 * spread                       # mild: full/empty swing
    return 0.7 * centroid_score + 0.3 * spread_score


def _assert_whitelist(p: Placement, fam: GridFamily) -> None:
    for r in p.rows:
        assert r.spans in fam.split_whitelist, (
            f"row spans {r.spans} not in {fam.name} whitelist (search invariant)"
        )
        assert sum(r.spans) == 12, f"row spans {r.spans} do not sum to 12"


def score_placement(
    p: Placement, plan: SlidePlan, claims: dict[str, Claim], fam: GridFamily,
    brand: str = "okuro",
) -> ScoreBreakdown:
    """Weighted sum of the named research terms. Deterministic."""
    _assert_whitelist(p, fam)
    terms = {
        "reading_gravity": _reading_gravity(p, plan),
        "alignment": _alignment(p, plan, claims, brand, fam),
        "whitespace": _whitespace(p, plan, claims, brand, fam),
        "density_band": _density_band(p, plan, claims, fam),
        "family_conformance": _family_conformance(p, fam),
        "focal_dominance": _focal_dominance(p, plan, claims, brand, fam),
        "balance": _balance(p, plan, claims, brand, fam),
        "space_utilization": _space_utilization(p, plan, claims, brand, fam),
        "macro_structure": _macro_structure(p, plan, claims, brand, fam),
    }
    total = sum(WEIGHTS[k] * v for k, v in terms.items())
    return ScoreBreakdown(total=total, terms=terms)


__all__ = ["ScoreBreakdown", "WEIGHTS", "score_placement"]
