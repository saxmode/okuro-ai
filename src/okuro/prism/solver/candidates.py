# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 solver core (1/3) — the placement model + bounded candidate
#   generation. A Placement = rows of 12-col cells (each cell a unit or a spacer).
#   Candidate rows are drawn ONLY from the selected family's split_whitelist
#   (critique dim 4: split_whitelist bounds candidate generation — the anti-
#   template-rigidity guard), x emphasis-seeded orientations. Generalises the
#   shipped greedy packer (layout.py _pack) into an enumerable expansion for beam
#   search.
# index:
#   Cell / Row / Placement dataclasses + geometry (heights/areas)
#   expansions (one beam step) / is_complete
# AGENT_HEADER_END -->
"""Placement model + candidate generation for the beam solver.

The greedy packer in ``layout.py`` makes ONE arrangement decision per row; the
solver needs to ENUMERATE the legal alternatives so beam search can score and
compare them. ``expansions`` is that enumerator: given a partial placement, it
yields every legal next row — 1..4 consecutive units seated in a whitelist split
(optionally with one spacer cell for the family whitespace floor), respecting
each component's intrinsic col-span range and the family prose-measure floor.
Bounded by construction: <=4 chunk sizes x <=|split_whitelist| tuples x a small
set of emphasis-seeded orientations.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from okuro.prism.solver.families import GridFamily
from okuro.prism.solver.heights import get_height_model
from okuro.prism.solver.manifest import get_component
from okuro.prism.solver.schema import Claim, InformationUnit, SlidePlan

_MAX_ROW_CELLS = 4                       # widest whitelist tuple (3|3|3|3)


@dataclass(frozen=True)
class Cell:
    unit: int | None                    # index into plan.units, or None = spacer/void
    span: int

    @property
    def is_spacer(self) -> bool:
        return self.unit is None


@dataclass(frozen=True)
class Row:
    cells: tuple[Cell, ...]

    @property
    def spans(self) -> tuple[int, ...]:
        return tuple(c.span for c in self.cells)


@dataclass(frozen=True)
class Placement:
    """A complete or partial arrangement: ordered rows + count of units seated."""

    rows: tuple[Row, ...]
    seated: int                          # number of reading_order entries consumed

    def add(self, row: Row, n_units: int) -> "Placement":
        return Placement(self.rows + (row,), self.seated + n_units)


# ── geometry (estimated, no render) ──────────────────────────────────────────

def cell_height(
    cell: Cell, plan: SlidePlan, claims: dict[str, Claim], brand: str
) -> float:
    if cell.is_spacer:
        return 0.0
    u = plan.units[cell.unit]
    hm = get_height_model()
    return hm.estimate_px(
        u.component, u.resolved_items(claims), u.resolved_chars(claims), cell.span, brand
    )


def row_height(row: Row, plan: SlidePlan, claims: dict[str, Claim], brand: str) -> float:
    return max((cell_height(c, plan, claims, brand) for c in row.cells), default=0.0)


def placement_height(
    p: Placement, plan: SlidePlan, claims: dict[str, Claim], brand: str, gutter_px: int
) -> float:
    hs = [row_height(r, plan, claims, brand) for r in p.rows]
    return sum(hs) + gutter_px * max(0, len(hs) - 1)


def cell_area(
    cell: Cell, plan: SlidePlan, claims: dict[str, Claim], brand: str
) -> float:
    """Area proxy = col_span x estimated height (px). Spacer area counts as void."""
    return cell.span * cell_height(cell, plan, claims, brand)


# ── candidate generation ─────────────────────────────────────────────────────

def _unit_span_ok(u: InformationUnit, span: int, family: GridFamily) -> bool:
    c = get_component(u.component)
    if not (c.min_cols <= span <= c.max_cols):
        return False
    # prose measure floor: a text-bearing prose component must not be seated below
    # the family floor (layout.py rule #2 generalised per family).
    if c.text_bearing and c.role and span < family.prose_measure_floor:
        # short single-line text-bearers (title/lede/callout/statement) are exempt:
        # they cap their own width, only multi-line prose needs the measure floor.
        if c.reflows or c.id in ("prose",):
            return False
    return True


def _assign(
    units: list[int], spans: tuple[int, ...], plan: SlidePlan, family: GridFamily,
    focal_first: bool,
) -> Row | None:
    """Seat ``units`` into a span tuple, LEFT-ANCHORED. Content occupies the
    largest cells on the LEFT (reading start); any spacer is the smallest cell,
    TRAILING (right). This kills the left-void-band defect: a single unit in a
    (4,8) split used to land in the big RIGHT cell with the void on the left; now
    the split is normalised to its descending orientation (8,4) so content is
    big-left and the void trails right where the space-utilisation term can see
    and penalise it. The focal takes the largest (leftmost) cell; the remaining
    units follow in reading order. ``focal_first=False`` drops the focal-first
    priority (pure reading order) for beam A/B diversity."""
    k = len(units)
    extra = len(spans) - k
    if extra not in (0, 1):
        return None
    # A lone single-slot text banner (statement-title / lede / statement) full-
    # bleeds: it never sits at a partial span with a trailing void (the title-void
    # wrinkle). It may still SHARE a band with real content (extra==0, e.g. an 8|4
    # title+rail), but the content+spacer variant is rejected so the beam is forced
    # onto the [12] full-width candidate. Signature = single-capacity, text-bearing,
    # non-reflowing (a headline, not a wrapping prose/grid).
    if k == 1 and extra == 1:
        c0 = get_component(plan.units[units[0]].component)
        if c0.text_bearing and c0.cap_max == 1 and not c0.reflows:
            return None
    # normalise to the descending orientation: largest cells left, spacer right.
    spans_desc = tuple(sorted(spans, reverse=True))
    if spans_desc not in family.split_whitelist:
        return None                                       # not a legal arrangement
    content_spans = spans_desc[:k]                         # largest k, left-anchored
    spacer_spans = spans_desc[k:]                          # smallest, trailing right

    focal_local = next(
        (idx for idx, ui in enumerate(units) if plan.units[ui].emphasis == "focal"),
        None,
    )
    seq = list(range(k))
    if focal_first and focal_local is not None:
        seq = [focal_local] + [x for x in seq if x != focal_local]

    cells: list[Cell] = []
    for pos, span in enumerate(content_spans):
        ui = units[seq[pos]]
        if not _unit_span_ok(plan.units[ui], span, family):
            return None
        cells.append(Cell(ui, span))
    for span in spacer_spans:
        cells.append(Cell(None, span))
    return Row(tuple(cells))


def expansions(
    p: Placement, plan: SlidePlan, family: GridFamily, focal_first: bool,
    allowed_splits: frozenset[tuple[int, ...]] | None = None,
) -> list[tuple[Row, int]]:
    """All legal next rows from partial placement ``p``. Each result is (row,
    n_units_consumed). Bounded by split_whitelist x chunk size x spacer variant.

    ``allowed_splits`` is the P4.1 arrangement bridge's narrowing hook: when a
    content-side judgment names an arrangement (``arr-hero-rail``), the solver
    enumerates only that arrangement's span tuples INTERSECTED with the family
    whitelist. It can only ever RESTRICT — a split absent from the family
    whitelist is still refused, so an arrangement can never widen the freeze.
    ``None`` = no arrangement constraint (the pre-P4 behaviour, unchanged).
    """
    order = plan.reading_order
    remaining = order[p.seated:]
    if not remaining:
        return []
    out: list[tuple[Row, int]] = []
    seen: set[tuple] = set()
    max_chunk = min(_MAX_ROW_CELLS, len(remaining))
    for chunk in range(1, max_chunk + 1):
        units = remaining[:chunk]
        for spans in family.split_whitelist:
            if allowed_splits is not None and spans not in allowed_splits:
                continue
            if len(spans) not in (chunk, chunk + 1):
                continue
            row = _assign(list(units), spans, plan, family, focal_first)
            if row is None:
                continue
            key = (tuple((c.unit, c.span) for c in row.cells), chunk)
            if key in seen:
                continue
            seen.add(key)
            out.append((row, chunk))
    return out


def is_complete(p: Placement, plan: SlidePlan) -> bool:
    return p.seated == len(plan.units)


__all__ = [
    "Cell", "Row", "Placement",
    "cell_height", "row_height", "placement_height", "cell_area",
    "expansions", "is_complete",
]
