# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism compiler STAGE 6 (plan) — assign one of the 12 kit
#   archetypes to every (topic × level) grid cell, DETERMINISTICALLY (no LLM), so
#   that the code-checked quotas hold: no two adjacent topic columns share an
#   archetype at the same level, no two adjacent columns are both DENSE at L1, and
#   every enabled archetype is used at least once. L3 is always the doc-view.
# index: plan | assert_quotas | QuotaError | _candidates | _solve
# AGENT_HEADER_END -->
"""Stage 6 — plan: the topic×level archetype grid.

This is a constraint-satisfaction problem, not a generation problem — the design
(v3 §3) puts arrangement in CODE, never in an LLM. Each cell's candidate
archetypes come from the topic's dominant claim shape ∩ the level's pool
(``archetypes.py``). A backtracking solver assigns archetypes so the HARD quotas
hold (row-adjacency + the L1 dense-adjacent-dense ban), preferring an as-yet
-unused archetype at each step so the soft coverage quota (every enabled
archetype ≥1×) is met on these small 4-7×3 grids. ``assert_quotas`` re-checks the
hard quotas independently — it is the acceptance oracle for the quota golden test.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

from okuro.prism.compiler import archetypes as A
from okuro.prism.compiler.ir import Authored, Outline, PageCell, PagePlan

logger = logging.getLogger(__name__)

_GRID_LEVELS = (0, 1, 2)   # L3 is always "doc", not an archetype cell


class QuotaError(AssertionError):
    """A hard plan quota (adjacency / L1 dense-ban) was violated."""


def _dominant_claim(topic_id: str, authored: Authored, claims_by_id: dict[str, dict],
                    topic_claims: dict[str, list[str]]) -> tuple[str, Optional[int]]:
    """The (shape, item_count) that should drive a topic's archetype: its tier-0
    claim's shape + that claim's collection cardinality (None if scalar), else the
    modal shape across the topic's claims. item_count is what the archetype's
    primary slot must be able to hold (the cardinality gate)."""
    at = next((t for t in authored.topics if t.topic_id == topic_id), None)
    ids = topic_claims.get(topic_id, [])
    lead: Optional[str] = None
    if at is not None:
        zero = [c for c in ids if at.tier_of(c) == 0]
        if zero and zero[0] in claims_by_id:
            lead = zero[0]
    if lead is None:
        shapes = [claims_by_id[c]["shape"] for c in ids if c in claims_by_id]
        if not shapes:
            return "narrative", None
        modal = Counter(shapes).most_common(1)[0][0]
        lead = next((c for c in ids if claims_by_id.get(c, {}).get("shape") == modal), None)
    claim = claims_by_id.get(lead, {}) if lead else {}
    shape = claim.get("shape", "narrative")
    return shape, A.shape_item_count(shape, claim.get("fields", {}))


def _candidates(shape: str, level: int, item_count: Optional[int]) -> list[str]:
    """Ordered archetype candidates for a cell. TWO hard gates decide candidacy
    (a rejected archetype is never a fallback, so junk can't sneak in via the
    filler path): (1) SHAPE — a data-bearing archetype must be able to render the
    dominant claim's shape (proportion-bars ⊄ comparison); (2) CARDINALITY — its
    primary slot must hold the claim's collection size (binary-choice only at 2).
    The shape gate applies at L1/L2 where data-bearing archetypes render a
    shape-specific viz; L0 is a single-claim headline (pool = hero/ask-cta/card
    -set, no empty-viz risk) so only cardinality gates there. Ordered shape
    -affinity first, agnostic containers as fallback; never empty."""
    pool = [a for a in A.level_pool(level) if a != "doc"]

    def gate(a: str) -> bool:
        if not A.fits_cardinality(a, item_count):
            return False
        return level == 0 or A.accepts_shape(a, shape)

    primary = [a for a in A.archetypes_for_shape(shape) if a in pool and gate(a)]
    rest = [a for a in pool if a not in primary and gate(a)]
    cands = primary + rest
    if not cands:  # both gates rejected everything → shape-agnostic containers
        cands = [a for a in pool if a in A.SHAPE_AGNOSTIC and A.fits_cardinality(a, item_count)] \
            or pool
    return cands


def _solve(
    topic_order: list[str],
    cand: dict[tuple[str, int], list[str]],
    *,
    adjacency: bool = True,
    vertical_variety: bool = True,
) -> Optional[dict[tuple[str, int], str]]:
    """Backtracking assignment, coverage-preferring, COLUMN-MAJOR (each topic's
    L0→L1→L2 in turn) so both the horizontal neighbor (previous column) and the
    vertical neighbors (this column's shallower levels) are already placed.

    DENSITY MONOTONICITY (rank(L0) < rank(L1) ≤ rank(L2)) is CORRECTNESS and is
    ALWAYS enforced — a backwards ladder is never acceptable. The VARIETY quotas
    (row-adjacency + L1 dense-ban, and vertical variety L1≠L2) are relaxable via
    the flags: the phased driver drops them, in that order, only when the fully
    -constrained solve is infeasible for a narrow-shape deck. Ordering: coverage
    (unused first), then L1 sparsest-first (leaves room for L2 to step up), L2
    densest-first."""
    cells = [(lvl, col) for col in range(len(topic_order)) for lvl in _GRID_LEVELS]
    assign: dict[tuple[str, int], str] = {}
    used: Counter = Counter()

    def ok(topic: str, lvl: int, col: int, arch: str) -> bool:
        rank = A.density_rank(arch)
        # density ladder (correctness, always): L0<L1 strict, L1<=L2 non-decreasing.
        if lvl == 1 and rank <= A.density_rank(assign[(topic, 0)]):
            return False
        if lvl == 2 and rank < A.density_rank(assign[(topic, 1)]):
            return False
        if vertical_variety and lvl == 2 and arch == assign[(topic, 1)]:
            return False
        if adjacency and col > 0:
            left = assign[(topic_order[col - 1], lvl)]
            if arch == left:
                return False
            if lvl == 1 and A.is_dense(arch) and A.is_dense(left):
                return False
        return True

    def order(topic: str, lvl: int) -> list[str]:
        opts = cand[(topic, lvl)]
        rank_key = {0: 0, 1: 1, 2: -1}[lvl]   # L1 sparsest-first, L2 densest-first
        return sorted(opts, key=lambda a: (used[a] > 0, rank_key * A.density_rank(a),
                                           opts.index(a)))

    def rec(i: int) -> bool:
        if i == len(cells):
            return True
        lvl, col = cells[i]
        topic = topic_order[col]
        for arch in order(topic, lvl):
            if not ok(topic, lvl, col, arch):
                continue
            assign[(topic, lvl)] = arch
            used[arch] += 1
            if rec(i + 1):
                return True
            used[arch] -= 1
            del assign[(topic, lvl)]
        return False

    return assign if rec(0) else None


def _phased_solve(
    topic_order: list[str], cand: dict[tuple[str, int], list[str]],
) -> tuple[Optional[dict[tuple[str, int], str]], bool]:
    """Solve keeping the density ladder correct; relax VARIETY only when forced.
    Returns (assignment, relaxed). Phases: full → drop row-adjacency+dense-ban →
    also drop vertical variety. The density monotonicity ladder holds in every
    phase (it is never relaxed)."""
    for adjacency, variety, relaxed in ((True, True, False), (False, True, True),
                                        (False, False, True)):
        a = _solve(topic_order, cand, adjacency=adjacency, vertical_variety=variety)
        if a is not None:
            if relaxed:
                logger.warning("plan: relaxed variety quotas (adjacency=%s variety=%s) "
                               "to keep the density ladder on a narrow-shape deck",
                               adjacency, variety)
            return a, relaxed
    return None, True


def topic_cardinality(outline: Outline, mine_out: dict,
                      authored: Authored) -> dict[str, tuple[str, Optional[int]]]:
    """Per-topic (dominant shape, dominant claim item_count) — the cardinality the
    topic's archetype must satisfy at every level. Shared by plan() and the quota
    check so both gate on the same numbers."""
    topic_claims = {t.id: t.claim_ids for t in outline.topics}
    claims_by_id = {c["id"]: c for c in mine_out.get("claims", [])}
    return {t.id: _dominant_claim(t.id, authored, claims_by_id, topic_claims)
            for t in outline.topics}


def plan(
    outline: Outline,
    mine_out: dict,
    authored: Authored,
) -> PagePlan:
    """Assign archetypes across the topic×level grid under the code quotas —
    including the cardinality gate (an archetype whose primary slot can't hold the
    topic's dominant-claim collection is never assigned)."""
    topic_order = [t.id for t in outline.topics]
    if not topic_order:
        raise ValueError("plan: no topics")
    card = topic_cardinality(outline, mine_out, authored)

    cand: dict[tuple[str, int], list[str]] = {}
    for tid in topic_order:
        shape, item_count = card[tid]
        for lvl in _GRID_LEVELS:
            cand[(tid, lvl)] = _candidates(shape, lvl, item_count)

    assign, relaxed = _phased_solve(topic_order, cand)
    if assign is None:
        logger.warning("plan: no solution even after relaxing variety — greedy first-fit")
        assign = {(tid, lvl): cand[(tid, lvl)][0]
                  for tid in topic_order for lvl in _GRID_LEVELS}
        relaxed = True

    cells: list[PageCell] = []
    for tid in topic_order:
        for lvl in _GRID_LEVELS:
            cells.append(PageCell(topic_id=tid, level=lvl, archetype=assign[(tid, lvl)]))
        cells.append(PageCell(topic_id=tid, level=3, archetype="doc"))

    pp = PagePlan(cells=cells, topic_order=topic_order)
    _log_coverage(pp, cand)
    # deck-level distinct-archetype floor across L1/L2, capped at what the (shape/
    # cardinality-narrowed) candidate pools can actually supply so it never
    # false-fails a legitimately narrow deck.
    available = len({a for opts in cand.values() for a in opts})
    min_distinct = min(3, available, len(topic_order) * 2)
    # Density ladder + shape + cardinality are always hard; variety quotas are only
    # asserted when the solver did NOT have to relax them (relax is logged above).
    assert_quotas(pp, {tid: c[1] for tid, c in card.items()},
                  {tid: c[0] for tid, c in card.items()},
                  min_distinct=(None if relaxed else min_distinct),
                  strict_variety=not relaxed)
    return pp


def _enabled_archetypes(cand: dict[tuple[str, int], list[str]]) -> set[str]:
    return {a for opts in cand.values() for a in opts}


def _log_coverage(pp: PagePlan, cand: dict[tuple[str, int], list[str]]) -> None:
    enabled = _enabled_archetypes(cand)
    used = {c.archetype for c in pp.cells if c.archetype != "doc"}
    missing = enabled - used
    if missing:
        logger.info("plan: %d/%d enabled archetypes used; not placed: %s",
                    len(used), len(enabled), sorted(missing))


def assert_quotas(pp: PagePlan,
                  topic_item_count: Optional[dict[str, Optional[int]]] = None,
                  topic_shape: Optional[dict[str, str]] = None,
                  min_distinct: Optional[int] = None,
                  strict_variety: bool = True) -> None:
    """Raise QuotaError on a breach. CORRECTNESS quotas are always enforced: the
    DENSITY MONOTONICITY ladder (rank(L0) < rank(L1) ≤ rank(L2) — a deeper level
    must never render less), plus CARDINALITY and SHAPE when their maps are given.
    VARIETY quotas (row adjacency, L1 dense-ban, vertical L1≠L2, deck distinct
    floor) are enforced only when ``strict_variety`` — the plan stage turns them
    off when it had to relax them to keep the ladder feasible on a narrow deck."""
    order = pp.topic_order

    # ── density ladder (CORRECTNESS — always) ──────────────────────────────────
    for tid in order:
        c0, c1, c2 = pp.cell(tid, 0), pp.cell(tid, 1), pp.cell(tid, 2)
        r0 = A.density_rank(c0.archetype) if c0 else 0
        r1 = A.density_rank(c1.archetype) if c1 else r0
        r2 = A.density_rank(c2.archetype) if c2 else r1
        if not (r0 < r1 <= r2):
            raise QuotaError(
                f"{tid}: density ladder broken — ranks L0={r0} L1={r1} L2={r2} "
                f"(need L0 < L1 <= L2; a deeper level must not render less)")

    # ── variety quotas (relaxable) ─────────────────────────────────────────────
    if strict_variety:
        for lvl in _GRID_LEVELS:
            row = [pp.cell(tid, lvl) for tid in order]
            for i in range(1, len(row)):
                a, b = row[i - 1], row[i]
                if a is None or b is None:
                    raise QuotaError(f"L{lvl}: missing cell in row")
                if a.archetype == b.archetype:
                    raise QuotaError(
                        f"L{lvl}: adjacent columns {order[i-1]}/{order[i]} share "
                        f"archetype {a.archetype!r}")
                if lvl == 1 and A.is_dense(a.archetype) and A.is_dense(b.archetype):
                    raise QuotaError(
                        f"L1: adjacent columns {order[i-1]}/{order[i]} are both dense "
                        f"({a.archetype}/{b.archetype})")
        for tid in order:
            c1, c2 = pp.cell(tid, 1), pp.cell(tid, 2)
            if c1 and c2 and c1.archetype == c2.archetype:
                raise QuotaError(
                    f"{tid}: L1 and L2 share archetype {c1.archetype!r} "
                    f"(no vertical variety — superset step reads identical)")

    if topic_item_count is not None:
        for cell in pp.cells:
            if cell.archetype == "doc":
                continue
            n = topic_item_count.get(cell.topic_id)
            if not A.fits_cardinality(cell.archetype, n):
                lo, hi = A.collection_capacity(cell.archetype)
                raise QuotaError(
                    f"{cell.topic_id}/L{cell.level}: {cell.archetype} holds "
                    f"{lo}-{hi} items but the dominant claim has {n}")

    if topic_shape is not None:
        for cell in pp.cells:
            if cell.archetype == "doc" or cell.level == 0:  # L0 headline: no viz gate
                continue
            sh = topic_shape.get(cell.topic_id)
            if sh and not A.accepts_shape(cell.archetype, sh):
                raise QuotaError(
                    f"{cell.topic_id}/L{cell.level}: {cell.archetype} cannot render "
                    f"claim shape {sh!r} (would produce empty/overflowing output)")

    if min_distinct is not None:
        used = {c.archetype for c in pp.cells if c.level in (1, 2) and c.archetype != "doc"}
        if len(used) < min_distinct:
            raise QuotaError(
                f"deck uses only {len(used)} distinct L1/L2 archetypes; floor is "
                f"{min_distinct} (too repetitive)")


def coverage(pp: PagePlan, cand: Optional[dict] = None) -> dict:
    """Coverage report: which enabled archetypes were placed (soft quota)."""
    used = Counter(c.archetype for c in pp.cells if c.archetype != "doc")
    return {"used": dict(used), "distinct_used": len(used)}
