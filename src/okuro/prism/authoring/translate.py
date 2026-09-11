# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W3 engine 2 — COMPONENT TRANSLATION. Map one claim (shape +
#   cardinality + emphasis) to the component instance that serves it best, using
#   ONLY the W1 manifest fit-scores + capacity (no LLM styling, no geometry). High-
#   cardinality claims route to the unbounded dense components (dense-table / two-
#   column-list / tag-wall / matrix) — the 13-principles truncation class. Returns
#   the component AND the items it can render, so the accuracy accounting can state
#   "N of M" when a level shows fewer than the claim carries.
# index:
#   EMPHASIS_MANIFEST / choose_component / render_items_for
# AGENT_HEADER_END -->
"""Claim -> component translation via the W1 manifest.

The single rule: a component may carry a claim iff its ``fit`` for the claim shape
clears the threshold; among those, prefer one that (a) accepts the emphasis and
(b) can hold the item count; ties break by fit desc then id (deterministic, mirrors
``manifest.components_for_shape``). When the item count exceeds every bounded
component's capacity, the unbounded high-cardinality components take over — that is
the whole point of the W1 high-cardinality tier. The chosen component's realisable
item count is returned alongside so a shallower level can honestly render a subset.
"""
from __future__ import annotations

from okuro.prism.solver.heights import get_height_model
from okuro.prism.solver.manifest import Component, components_for_shape, get_component

# schema.EMPHASIS uses "supporting"; the manifest uses "support". Normalise so the
# component's own accepts_emphasis (manifest spelling) can be consulted.
_EMPHASIS_TO_MANIFEST = {"focal": "focal", "primary": "primary",
                         "supporting": "support", "aside": "aside"}


def _accepts(c: Component, emphasis: str) -> bool:
    return c.accepts_emphasis(_EMPHASIS_TO_MANIFEST.get(emphasis, emphasis))


def _compactness(c: Component, shape: str, items: int) -> float:
    """Estimated full-width rendered height for a component at this item count — the
    tiebreak among EQUAL-FIT holders (fit is never sacrificed for it). A nominal
    char load keeps it a pure component-shape compare. Lower = more compact."""
    hm = get_height_model()
    return hm.estimate_px(c.id, items, chars=items * 24, col_span=12)


_SAMENESS_FIT_DELTA = 0.2   # accept a slightly-lower-fit DISTINCT component within this

# Visual render families — components that paint the SAME way (a .card-grid of
# cards). Anti-sameness avoids repeating a FAMILY on one slide, not just a
# component id: two card-grids with different column counts read as one mismatched
# block (judge: "5-up and 3-up rows on mismatched columns"). A grid + a table/list
# reads as intentional hierarchy.
_RENDER_FAMILY: dict[str, str] = {
    "card-set": "card-grid", "verb-grid": "card-grid", "story-grid": "card-grid",
    "transform-grid": "card-grid", "metric-tiles": "card-grid", "option-grid": "card-grid",
}


def render_family(component_id: str) -> str:
    """The visual family a component paints as (card-grid / else itself)."""
    return _RENDER_FAMILY.get(component_id, component_id)


def choose_component(
    shape: str, items: int, emphasis: str, threshold: float = 0.5,
    avoid: frozenset[str] = frozenset(),
) -> str:
    """Best-fit component id for (shape, items, emphasis).

    Selection order (deterministic):
      1. fit-set for the shape (fit >= threshold), best fit first.
      2. best-fit component that accepts the emphasis AND can hold ``items``.
      3. best-fit holder ignoring emphasis; then the best-fit component clamped
         (its cap_max renders a subset — the deficit is reported by the accuracy
         accounting, never silently dropped).

    High-cardinality routing is emergent, not special-cased: a set of 5 keeps its
    best-fit card-set (cap 5), a set of 13 falls through to the best-fit HOLDER
    (two-column-list / dense-table / tag-wall — the unbounded components), because
    the small-but-better-fit components simply cannot hold the count. Capacity is a
    filter, never a preference — so semantic fit is never traded for raw capacity
    (a 5-item set never lands on a comparison matrix just because it holds more).
    """
    fitset = components_for_shape(shape, threshold)
    if not fitset:
        # no component fits the shape at all -> narrative prose is the universal
        # fallback (it fits 'narrative' 1.0 and carries any text).
        return "prose"

    def _rank(pool: list[Component]) -> str:
        # best fit first; among EQUAL fit, the most COMPACT (so a set of 5 lands on
        # verb-grid ~372px, not the alphabetically-first card-set ~558px); id last
        # for determinism. Fit is primary — compactness never overrides it.
        # ANTI-SAMENESS (echoes critic.sibling_sameness): prefer a component NOT
        # already used on this slide, but only within a small fit delta — two equal
        # card-grids read as one undifferentiated block with no hierarchy (judge
        # flag). A distinct component type gives the slide variety + implicit order.
        best_fit = max(c.fit_for(shape) for c in pool)

        def _repeats(c: Component) -> bool:
            # penalise reusing a component id OR its render family (two card-grids).
            return c.id in avoid or render_family(c.id) in avoid

        return min(
            pool,
            key=lambda c: (
                _repeats(c) and c.fit_for(shape) >= best_fit - _SAMENESS_FIT_DELTA,
                -c.fit_for(shape), _compactness(c, shape, items), c.id,
            ),
        ).id

    holders = [c for c in fitset if c.can_hold(items)]
    accept_holders = [c for c in holders if _accepts(c, emphasis)]
    if accept_holders:
        return _rank(accept_holders)
    if holders:
        return _rank(holders)
    # nothing can hold the full count at this emphasis: best-fit component, whose
    # cap_max will render a subset (accuracy accounting reports N of M).
    return fitset[0].id


def render_items_for(component_id: str, wanted: int) -> int:
    """How many items ``component_id`` can actually render of ``wanted`` (clamped to
    cap_max unless unbounded; never below cap_min when wanted>0)."""
    c = get_component(component_id)
    if wanted <= 0:
        return 0
    if c.unbounded:
        return max(wanted, c.cap_min)
    return max(min(wanted, c.cap_max), c.cap_min if wanted else 0)


__all__ = ["choose_component", "render_items_for"]
