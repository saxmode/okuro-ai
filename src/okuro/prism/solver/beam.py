# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 solver core (3/3) — beam search over candidate placements
#   (v4.1 B1). Beam width >=2 so the top-2 complete placements ARE the per-slide
#   A/B pair (subsumes seed-A/B). Deterministic per seed: every ordering is a
#   total order (score, canonical key), so the same seed yields a byte-identical
#   layout (exit gate a).
# index:
#   ScoredPlacement / canonical_key / beam_search / ab_pair
# AGENT_HEADER_END -->
"""Beam search over the candidate placement space.

Each step expands every beam state into its legal next rows (candidates.expansions
under both emphasis seeds), scores the partial with the weighted rule set, and
keeps the top ``beam_width``. Completed placements are collected and ranked by the
full score; the top two distinct placements are returned as the A/B pair. The
whole search is deterministic given ``seed`` — the seed only reorders the two
emphasis-seed passes (a deterministic diversity lever), never the final total
ordering, which is always (-score, canonical_key).
"""
from __future__ import annotations

from dataclasses import dataclass

from okuro.prism.solver.candidates import Placement, expansions, is_complete
from okuro.prism.solver.families import GridFamily
from okuro.prism.solver.score import ScoreBreakdown, score_placement
from okuro.prism.solver.schema import Claim, SlidePlan


@dataclass(frozen=True)
class ScoredPlacement:
    placement: Placement
    score: ScoreBreakdown

    @property
    def total(self) -> float:
        return self.score.total


def canonical_key(p: Placement) -> tuple:
    """A total-order key over placements — the deterministic tie-break."""
    return tuple(
        tuple((c.unit if c.unit is not None else -1, c.span) for c in r.cells)
        for r in p.rows
    )


def _rank(states: list[Placement], plan, claims, fam, brand) -> list[ScoredPlacement]:
    scored = [ScoredPlacement(s, score_placement(s, plan, claims, fam, brand)) for s in states]
    scored.sort(key=lambda sp: (-sp.total, canonical_key(sp.placement)))
    return scored


def beam_search(
    plan: SlidePlan, claims: dict[str, Claim], fam: GridFamily,
    brand: str = "okuro", beam_width: int = 6, seed: int = 0,
    allowed_splits: frozenset[tuple[int, ...]] | None = None,
) -> list[ScoredPlacement]:
    """Return all complete placements found, ranked best-first. Deterministic.

    ``allowed_splits`` (P4.1) narrows candidate generation to a named
    arrangement's span tuples; see ``candidates.expansions``. It only ever
    restricts, so the search stays inside the family freeze."""
    assert beam_width >= 2, "beam width must be >=2 (A/B pair falls out of the search)"
    focal_order = (True, False) if seed % 2 == 0 else (False, True)

    frontier: list[Placement] = [Placement((), 0)]
    completed: dict[tuple, Placement] = {}
    steps = len(plan.units) + 1                             # max rows <= units
    for _ in range(steps):
        nxt: dict[tuple, Placement] = {}
        for state in frontier:
            for focal_first in focal_order:
                for row, n in expansions(state, plan, fam, focal_first, allowed_splits):
                    ns = state.add(row, n)
                    key = canonical_key(ns)
                    if is_complete(ns, plan):
                        completed.setdefault(key, ns)
                    else:
                        nxt.setdefault(key, ns)
        if not nxt:
            break
        ranked = _rank(list(nxt.values()), plan, claims, fam, brand)
        frontier = [sp.placement for sp in ranked[:beam_width]]

    return _rank(list(completed.values()), plan, claims, fam, brand)


def ab_pair(
    plan: SlidePlan, claims: dict[str, Claim], fam: GridFamily,
    brand: str = "okuro", beam_width: int = 6, seed: int = 0,
    allowed_splits: frozenset[tuple[int, ...]] | None = None,
) -> tuple[ScoredPlacement, ScoredPlacement | None]:
    """The per-slide A/B: the top-2 distinct complete placements. B is None only
    when the slide admits a single legal placement."""
    ranked = beam_search(plan, claims, fam, brand, beam_width, seed, allowed_splits)
    if not ranked:
        raise ValueError("no legal placement found for slide plan")
    a = ranked[0]
    b = ranked[1] if len(ranked) > 1 else None
    return a, b


__all__ = ["ScoredPlacement", "canonical_key", "beam_search", "ab_pair"]
