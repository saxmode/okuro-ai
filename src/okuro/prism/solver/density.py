# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 item 4 — the CROSS-MODEL DENSITY SCALAR. One density scale
#   that spans both engines (archetype-L1 hooks and free-composed L2/L3), so the
#   resolution ladder (density strictly rises L1<L2<L3, v4 §3c) is monotonic
#   across engines and comparable to a per-family density ceiling. Calibrated from
#   the W1 raw density_cost (base_px + per_item_px), NOT the height model.
# index:
#   density_cost / slide_density / within_ceiling / ladder_monotonic
# AGENT_HEADER_END -->
"""Cross-model density scalar (critique dim 5).

Density under free composition can't be an archetype rank (there is no single
archetype per slide). It is redefined as an ink FILL RATIO over the grid
rectangle:

    density(slide) = Σ_cells [ density_cost(comp, items) * col_span ] / (12 * H_ref)

``density_cost`` is the W1 RAW cost (``base_px + per_item_px * items``) — the same
number for an L1-hook component as for an L3 dense-table, which is exactly what
makes the scalar span both engines. An L1 hook (one focal component + a large
whitespace floor) scores low; an L3 slide (several high-cardinality components)
scores high — so the same metric orders the whole ladder. The ladder gate then
asserts L1 < L2 < L3 (monotone across the template and free rungs alike), and
each level is checked against its per-family ceiling (grid-families.json
``density_ceilings``).
"""
from __future__ import annotations

from okuro.prism.solver.families import GridFamily
from okuro.prism.solver.manifest import Component, get_component

# Reference grid height: kit canvas is 1600x900 (calibrate_heights: "900px canvas
# / 20 row-units"). Ink fraction is normalised over the 12 x 900 rectangle so the
# scalar lands in ~[0,1] for a full canvas and density_cost stays in raw px units.
H_REF_PX = 900.0
GRID_COLS = 12


def density_cost(component: str | Component, items: int) -> float:
    """Raw W1 density cost of one component instance (base_px + per_item_px*items)."""
    c = component if isinstance(component, Component) else get_component(component)
    n = max(items, c.cap_min)
    return c.base_px + c.per_item_px * n


def slide_density(placements: list[tuple[str, int, int]]) -> float:
    """Cross-model density scalar for a slide.

    ``placements`` = [(component_id, items, col_span), ...]. Returns the ink fill
    ratio over the grid rectangle (0 = empty, ~1 = full canvas of ink)."""
    ink = 0.0
    for cid, items, col_span in placements:
        ink += density_cost(cid, items) * max(col_span, 1)
    return ink / (GRID_COLS * H_REF_PX)


def within_ceiling(density: float, family: GridFamily, level: str) -> bool:
    """True iff the slide density is at or below the family's per-level ceiling."""
    return density <= family.density_ceiling(level) + 1e-9


def ceiling_overflow(density: float, family: GridFamily, level: str) -> float:
    """How far density exceeds the ceiling (0 if within). Feeds the score penalty."""
    return max(0.0, density - family.density_ceiling(level))


def ladder_monotonic(level_densities: dict[str, float]) -> bool:
    """The resolution-ladder gate: density strictly rises L1 < L2 < L3 (v4 §3c).
    Missing levels are skipped; present levels must be strictly increasing."""
    order = [level_densities[k] for k in ("L1", "L2", "L3") if k in level_densities]
    return all(a < b for a, b in zip(order, order[1:]))


__all__ = [
    "density_cost",
    "slide_density",
    "within_ceiling",
    "ceiling_overflow",
    "ladder_monotonic",
    "H_REF_PX",
]
