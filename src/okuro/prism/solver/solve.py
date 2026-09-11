# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 — the solve orchestrator. Ties the pieces together per
#   v4.1 B2 two-phase sizing: validate the typed plan -> beam-search on ESTIMATED
#   heights (no render) -> render the winner ONCE -> bounded measure-guard
#   re-solve on overflow/orphan (walk the ranked beam, never a solver restart).
#   Consumes a per-deck family selection (answer 7). Zero LLM styling anywhere.
# index:
#   MeasureReport / SolveResult / solve / _accept
# AGENT_HEADER_END -->
"""The composition solve — plan, render once, guard, return an A/B pair.

Phase 1 (no render): ``beam_search`` ranks every legal placement on estimated
heights. Phase 2 (render): the winner is rendered ONCE and measured; if it
overflows the per-level canvas budget or leaves an orphan cell, the guard walks
down the ranked beam to the next candidate (a bounded re-solve — at most
``max_resolves`` renders, never a search restart) until one fits. The A/B pair is
the first two accepted placements (or the top-2 if no measurer is supplied). The
measurer is injected (render.py in the proof harness) so this module stays
render-free and unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from okuro.prism.config import warn_or_fail
from okuro.prism.solver.arrangements import (
    get_arrangement, label_rows, splits_for_arrangements,
)
from okuro.prism.solver.beam import ScoredPlacement, beam_search
from okuro.prism.solver.candidates import Placement
from okuro.prism.solver.families import get_family
from okuro.prism.solver.geometry_lint import LintReport
from okuro.prism.solver.schema import Claim, SlidePlan, validate_or_raise

# Overflow cap (px). Composed slides render auto-height in the ZoomStage (nothing
# is clipped -> truncation is structurally 0), so the real, falsifiable failure is
# READABILITY: the ZoomStage fit-scales a slide into the 16:9 stage, and past ~2x
# the 1600x900 canvas height (1800px) the fit scale drops text below legibility on
# a large display. That is the overflow ceiling — a single, principled cap for all
# levels; the L1<L2<L3 density LADDER is enforced separately by the per-family
# density_ceilings (score.py) + the fixture monotonicity test, not by a px budget.
READABILITY_CAP_PX = 1800.0
# Per-level SOFT targets — the measure-guard prefers a candidate under these when
# one exists, but exceeding a soft target is not overflow (only > cap is).
SOFT_TARGET_PX: dict[str, float] = {"L1": 900.0, "L2": 1300.0, "L3": 1750.0}
# The soft target is a TIEBREAK, not a filter: a shorter candidate only displaces the
# design-best when their total scores are within this epsilon (a near-tie). Keeps a
# ~2% height saving from discarding a meaningfully-better composition.
SOFT_TIEBREAK_EPS = 0.03
_ORPHAN_MIN_PX = 24.0                    # a content cell shorter than this is an orphan


@dataclass(frozen=True)
class MeasureReport:
    slide_height_px: float
    overflow: bool
    orphan: bool
    detail: str = ""
    # P4.3: the geometry lint of the SAME render this report measured. Carried
    # here rather than fetched by a second call because a re-render is a second
    # sample — fonts settle, and the two would then describe different images.
    # None = the linter did not run, which is never the same as "clean".
    lint: "LintReport | None" = None


@dataclass
class SolveResult:
    family: str
    level: str
    brand: str
    seed: int
    a: ScoredPlacement
    b: ScoredPlacement | None
    resolves_used: int                  # how far down the ranked beam we walked for A
    converged: bool                     # resolves_used <= max_resolves
    ranked_n: int
    measured: MeasureReport | None
    reask_errors: list[str] = field(default_factory=list)
    # P4.1 telemetry: the arrangement the plan ASKED for (None = unconstrained),
    # and the arrangement ids each solved row of A actually realises. The second
    # is derived from geometry, so it is a check on the first, not an echo of it.
    arrangement: str | None = None
    arrangement_rows: tuple[tuple[str, ...], ...] = ()


MeasureFn = Callable[[Placement, SlidePlan, dict[str, Claim], str], MeasureReport]


def _accept(report: MeasureReport) -> bool:
    return not report.overflow and not report.orphan


def _resolve_arrangement(
    arr_id: str | None, family: str
) -> frozenset[tuple[int, ...]] | None:
    """P4.1: turn a content-side arrangement judgment into the span tuples the
    beam may enumerate. ``None`` in -> ``None`` out (unconstrained).

    Two failure modes, both routed through ``prism.strict`` rather than silently
    ignored — silently dropping the constraint is how a declared-but-unread
    parameter is born:

    * the arrangement is not legal in this family;
    * it is legal but its splits intersect the family whitelist emptily (a
      layouts.json/grid-families.json drift the bridge freeze should have caught).

    Under strict both refuse the solve. With strict off they log loudly and fall
    back to the unconstrained whitelist, which is the pre-P4 behaviour.
    """
    if arr_id is None:
        return None
    arr = get_arrangement(arr_id)                 # unknown id raises — never guessed
    if not arr.legal_in(family):
        warn_or_fail(
            "arrangement_illegal_in_family",
            f"arrangement {arr_id!r} is not legal in family {family!r} "
            f"(legal in: {sorted(arr.families)})",
            arrangement=arr_id, family=family,
        )
        return None
    splits = splits_for_arrangements([arr_id], family)
    if not splits:
        warn_or_fail(
            "arrangement_no_legal_split",
            f"arrangement {arr_id!r} yields no split legal in family {family!r}",
            arrangement=arr_id, family=family,
        )
        return None
    return splits


def solve(
    plan: SlidePlan,
    claims: dict[str, Claim],
    family: str | None = None,
    brand: str = "okuro",
    seed: int = 0,
    beam_width: int = 6,
    measure_fn: MeasureFn | None = None,
    max_resolves: int = 2,
) -> SolveResult:
    """Solve one slide. ``family`` is the per-deck selection (answer 7); if None,
    the plan's own family is used. Deterministic given (plan, claims, family,
    brand, seed) and a deterministic measure_fn."""
    validate_or_raise(plan, claims)
    fam_name = family or plan.family
    fam = get_family(fam_name)
    allowed = _resolve_arrangement(plan.arrangement, fam_name)

    ranked = beam_search(plan, claims, fam, brand, beam_width, seed, allowed)
    if not ranked:
        raise ValueError(
            f"no legal placement for slide plan (family={fam_name}"
            + (f", arrangement={plan.arrangement}" if plan.arrangement else "")
            + ")"
        )

    if measure_fn is None:
        # plan-only (unit tests / no render available): top-2 estimated.
        a = ranked[0]
        b = ranked[1] if len(ranked) > 1 else None
        return SolveResult(
            fam_name, plan.level, brand, seed, a, b, 0, True, len(ranked), None,
            arrangement=plan.arrangement,
            arrangement_rows=label_rows(a.placement, fam_name),
        )

    cap = READABILITY_CAP_PX
    soft = SOFT_TARGET_PX.get(plan.level, 1300.0)
    readable: list[tuple[int, ScoredPlacement, MeasureReport]] = []
    rendered: list[tuple[int, ScoredPlacement, MeasureReport]] = []
    # Render the design-best winner once; the measure-guard only WALKS the beam on a
    # real failure — an orphan or exceeding the readability CAP. The soft target is
    # NOT a filter (a candidate 2% over soft but well under cap is perfectly
    # readable); it is a TIEBREAK applied below, so a small height saving can never
    # discard the composition-best layout. Bounded: design-best readable candidate +
    # one lookahead for a shorter near-tie, capped by the re-solve budget.
    for idx, sp in enumerate(ranked):
        m = measure_fn(sp.placement, plan, claims, brand)
        over_cap = m.overflow or m.slide_height_px > cap
        rep = MeasureReport(m.slide_height_px, over_cap, m.orphan, m.detail, m.lint)
        rendered.append((idx, sp, rep))
        # P4.3 HARD FILTER: a geometry-lint failure disqualifies a candidate the
        # same way an orphan or an over-cap render does — the guard walks on down
        # the ranked beam. `lint is None` means the linter did not run, and that
        # never counts as passing it.
        geometry_ok = rep.lint is None or rep.lint.clean
        if not rep.orphan and not over_cap and geometry_ok:
            readable.append((idx, sp, rep))
        if readable and (idx >= readable[0][0] + 1 or idx >= max_resolves):
            break
        if idx >= max_resolves:               # re-solve budget spent
            break

    if readable:
        # design-best readable candidate. Soft target = TIEBREAK ONLY: swap to a
        # shorter candidate only when its design score is within EPS (a near-tie) AND
        # it clears the soft target while the current best does not.
        best = readable[0]
        for cand in readable[1:]:
            near_tie = (best[1].total - cand[1].total) <= SOFT_TIEBREAK_EPS
            shorter_fit = cand[2].slide_height_px <= soft < best[2].slide_height_px
            if near_tie and shorter_fit:
                best = cand
        # P4.3 RERANK: among candidates the design scorer calls a near-tie, prefer
        # the better GEOMETRY. Same epsilon and same direction as the height
        # tiebreak above — a measured geometry advantage may settle a tie, it may
        # never overturn a clear composition winner.
        for cand in readable:
            if cand is best or best[2].lint is None or cand[2].lint is None:
                continue
            near_tie = abs(best[1].total - cand[1].total) <= SOFT_TIEBREAK_EPS
            if near_tie and (cand[2].lint.composition_score()
                             > best[2].lint.composition_score()):
                best = cand
        a_idx, a, a_report = best
        b = ranked[a_idx + 1] if a_idx + 1 < len(ranked) else None
        return SolveResult(
            fam_name, plan.level, brand, seed, a, b, a_idx,
            a_idx <= max_resolves, len(ranked), a_report,
            arrangement=plan.arrangement,
            arrangement_rows=label_rows(a.placement, fam_name),
        )

    # nothing fit the readability cap within the re-solve budget: pick the
    # shortest rendered (fail-soft; auto-height container => still no truncation)
    # and report overflow + non-convergence.
    idx, a, a_report = min(rendered, key=lambda t: t[2].slide_height_px)
    return SolveResult(
        fam_name, plan.level, brand, seed, a,
        ranked[idx + 1] if idx + 1 < len(ranked) else None,
        idx, False, len(ranked), a_report,
        arrangement=plan.arrangement,
        arrangement_rows=label_rows(a.placement, fam_name),
    )


__all__ = [
    "MeasureReport", "SolveResult", "MeasureFn", "solve",
    "READABILITY_CAP_PX", "SOFT_TARGET_PX",
]
