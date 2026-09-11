# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 proof fixtures — hand-authored claim content + typed slide
#   plans for the solver, spanning varied cardinality (incl. the 13-principles
#   truncation class), ALL claim shapes, the L1/L2/L3 resolution ladder, and all
#   4 grid families. These are the FIXTURE-authored units the W2 boundary schema
#   consumes (authoring intelligence is W3) and the corpus the exit gates run on.
# index:
#   FIXTURES registry / build_* topic builders / all_claim_shapes_covered
# AGENT_HEADER_END -->
"""Fixture content for the composition solver proof.

Each fixture is a topic rendered at three depths (L1 hook -> L2 -> L3), authored
so density strictly rises across the ladder (the resolution metaphor). The set is
chosen to exercise: every manifest claim shape at least once, the high-cardinality
13-item class (dense-table / two-column-list / tag-wall — the truncation class the
whole redesign closes), and each of the four grid families.
"""
from __future__ import annotations

from dataclasses import dataclass

from okuro.prism.solver.schema import Claim, InformationUnit, SlidePlan


@dataclass(frozen=True)
class Fixture:
    name: str
    family: str
    content_character: str
    audience_mood: str
    claims: dict[str, Claim]
    levels: dict[str, SlidePlan]         # "L1" | "L2" | "L3"


def _c(cid, text, shape, tier, items=1, chars=None) -> Claim:
    return Claim(cid, text, shape, tier=tier, items=items,
                 chars=chars if chars is not None else len(text))


def _u(claim_ids, component, emphasis, items=None, chars=None) -> InformationUnit:
    return InformationUnit(list(claim_ids), component, emphasis, items=items, chars=chars)


# ── Fixture 1 — retrieval (analytical/technical -> swiss) ─────────────────────
def _f_retrieval() -> Fixture:
    claims = {
        "hook": _c("hook", "Cortex finds code grep can't", "verdict", 0, chars=30),
        "cmp": _c("cmp", "recall@5 0.92 vs grep 0.28", "comparison", 0, items=4, chars=40),
        "cost": _c("cost", "8x less context: 2KB vs 25KB", "metric", 1, items=3, chars=32),
        "surf": _c("surf", "four retrieval surfaces", "set", 1, items=4, chars=90),
        "trend": _c("trend", "recall rises with corpus size", "trend", 1, items=8, chars=20),
        "note": _c("note", "Cortex beats grep on recall and cost across the whole corpus, "
                           "and the gap widens as the index grows.", "narrative", 2, chars=140),
    }
    return Fixture("retrieval", "swiss", "analytical", "technical", claims, {
        "L1": SlidePlan("L1", "swiss", [
            _u(["hook"], "statement-title", "focal"),
            _u(["cost"], "metric-tiles", "supporting"),
        ], [0, 1]),
        "L2": SlidePlan("L2", "swiss", [
            _u(["cmp"], "matrix", "focal"),
            _u(["cost"], "metric-tiles", "primary"),
            _u(["surf"], "card-set", "supporting"),
        ], [0, 1, 2]),
        "L3": SlidePlan("L3", "swiss", [
            _u(["cmp"], "matrix", "focal"),
            _u(["surf"], "card-set", "primary"),
            _u(["trend"], "chart", "supporting"),
            _u(["note"], "prose", "aside"),
        ], [0, 1, 2, 3]),
    })


# ── Fixture 2 — the 13-principles class (narrative/reading -> editorial) ───────
def _f_principles() -> Fixture:
    claims = {
        "hook": _c("hook", "Thirteen principles, one system", "verdict", 0, chars=32),
        "lede": _c("lede", "The design language in one screen.", "narrative", 1, chars=40),
        "set13": _c("set13", "thirteen design principles", "set", 0, items=13, chars=260),
        "set13b": _c("set13b", "thirteen principles, expanded", "set", 0, items=13, chars=390),
        "tags": _c("tags", "principle tags", "set", 1, items=13, chars=110),
    }
    return Fixture("principles", "editorial", "narrative", "reading", claims, {
        "L1": SlidePlan("L1", "editorial", [
            _u(["hook"], "statement-title", "focal"),
            _u(["lede"], "lede", "supporting"),
        ], [0, 1]),
        "L2": SlidePlan("L2", "editorial", [
            _u(["set13"], "two-column-list", "focal", items=13),
            _u(["lede"], "callout", "supporting"),
        ], [0, 1]),
        "L3": SlidePlan("L3", "editorial", [
            _u(["set13b"], "dense-table", "focal", items=13),
            _u(["tags"], "tag-wall", "supporting", items=13),
        ], [0, 1]),
    })


# ── Fixture 3 — vision (conceptual/creative -> bauhaus) ───────────────────────
def _f_vision() -> Fixture:
    claims = {
        "hook": _c("hook", "Agents that act, not chat", "verdict", 0, chars=26),
        "quote": _c("quote", "The legacy works. It also leaks.", "quote", 0, chars=34),
        "verdict": _c("verdict", "one bet, not one slice", "relationship", 0, items=3, chars=40),
        "beats": _c("beats", "three moves to the future", "sequence", 1, items=5, chars=80),
        "prose": _c("prose", "We are building an agent-native OS: one system that acts on "
                            "your behalf, remembers, and improves.", "narrative", 2, chars=130),
    }
    return Fixture("vision", "bauhaus", "conceptual", "creative", claims, {
        "L1": SlidePlan("L1", "bauhaus", [
            _u(["hook"], "statement-title", "focal"),
            _u(["quote"], "pull-quote", "supporting"),
        ], [0, 1]),
        "L2": SlidePlan("L2", "bauhaus", [
            _u(["verdict"], "arch-diagram", "focal"),
            _u(["quote"], "pull-quote", "primary"),
            _u(["beats"], "flow-steps", "supporting"),
        ], [0, 1, 2]),
        "L3": SlidePlan("L3", "bauhaus", [
            _u(["verdict"], "arch-diagram", "focal"),
            _u(["beats"], "flow-steps", "primary"),
            _u(["quote"], "statement", "supporting"),
            _u(["prose"], "prose", "aside"),
        ], [0, 1, 2, 3]),
    })


# ── Fixture 4 — premium/board (minimal/executive -> japanese) ─────────────────
def _f_premium() -> Fixture:
    claims = {
        "hook": _c("hook", "On track", "verdict", 0, chars=10),
        "kpi": _c("kpi", "three headline numbers", "metric", 0, items=3, chars=24),
        "prop": _c("prop", "budget split by area", "proportion", 1, items=4, chars=60),
        "delta": _c("delta", "quarter-over-quarter change", "delta", 1, items=3, chars=40),
        "note": _c("note", "The programme is on track against the board targets.",
                   "narrative", 2, chars=90),
    }
    return Fixture("premium", "japanese", "minimal", "executive", claims, {
        "L1": SlidePlan("L1", "japanese", [
            _u(["hook"], "statement-title", "focal"),
            _u(["kpi"], "stat-row", "supporting"),
        ], [0, 1]),
        "L2": SlidePlan("L2", "japanese", [
            _u(["kpi"], "metric-tiles", "focal"),
            _u(["prop"], "proportion-bars", "supporting"),
        ], [0, 1]),
        "L3": SlidePlan("L3", "japanese", [
            _u(["kpi"], "metric-tiles", "focal"),
            _u(["prop"], "proportion-bars", "primary"),
            _u(["delta"], "metric-tiles", "supporting"),
            _u(["note"], "prose", "aside"),
        ], [0, 1, 2, 3]),
    })


# ── Fixture 5 — architecture (relationship/sequence -> swiss) ─────────────────
def _f_architecture() -> Fixture:
    claims = {
        "hook": _c("hook", "One graph, three planes", "relationship", 0, items=5, chars=30),
        "seq": _c("seq", "request to render", "sequence", 1, items=6, chars=70),
        "surf": _c("surf", "component surfaces", "set", 1, items=5, chars=80),
        "prose": _c("prose", "The system is one graph: a control plane, a data plane, and "
                            "an agent plane, joined at the bus.", "narrative", 2, chars=120),
    }
    return Fixture("architecture", "swiss", "analytical", "technical", claims, {
        "L1": SlidePlan("L1", "swiss", [
            _u(["hook"], "arch-diagram", "focal"),
            _u(["prose"], "callout", "supporting"),
        ], [0, 1]),
        "L2": SlidePlan("L2", "swiss", [
            _u(["hook"], "arch-diagram", "focal"),
            _u(["seq"], "flow-steps", "primary"),
            _u(["surf"], "card-set", "supporting"),
        ], [0, 1, 2]),
        "L3": SlidePlan("L3", "swiss", [
            _u(["hook"], "arch-diagram", "focal"),
            _u(["seq"], "flow-steps", "primary"),
            _u(["surf"], "card-set", "supporting"),
            _u(["prose"], "prose", "aside"),
        ], [0, 1, 2, 3]),
    })


# ── Fixture 6 — roadmap (delta/trend/proportion -> editorial) ─────────────────
def _f_roadmap() -> Fixture:
    claims = {
        "hook": _c("hook", "Three phases to GA", "verdict", 0, items=4, chars=24),
        "phases": _c("phases", "phase plan", "sequence", 0, items=4, chars=60),
        "trend": _c("trend", "adoption trend", "trend", 1, items=8, chars=20),
        "delta": _c("delta", "velocity change", "delta", 1, items=3, chars=36),
        "risks": _c("risks", "eight tracked risks", "set", 1, items=8, chars=120),
        "prose": _c("prose", "The roadmap has three phases; each closes a risk class and "
                            "lifts adoption.", "narrative", 2, chars=100),
    }
    return Fixture("roadmap", "editorial", "narrative", "reading", claims, {
        "L1": SlidePlan("L1", "editorial", [
            _u(["hook"], "statement-title", "focal"),
            _u(["phases"], "phase-row", "supporting"),
        ], [0, 1]),
        "L2": SlidePlan("L2", "editorial", [
            _u(["phases"], "flow-steps", "focal"),
            _u(["trend"], "chart", "primary"),
            _u(["delta"], "metric-tiles", "supporting"),
        ], [0, 1, 2]),
        "L3": SlidePlan("L3", "editorial", [
            _u(["phases"], "flow-steps", "focal"),
            _u(["trend"], "chart", "primary"),
            _u(["risks"], "bias-check", "supporting", items=8),
            _u(["prose"], "prose", "aside"),
        ], [0, 1, 2, 3]),
    })


_BUILDERS = [
    _f_retrieval, _f_principles, _f_vision, _f_premium, _f_architecture, _f_roadmap,
]

FIXTURES: dict[str, Fixture] = {b().name: b() for b in _BUILDERS}


# A composed board slide reads as "finished" only with a title header (the
# archetype chrome). The vision judge flagged its absence at L2/L3 (solver slides
# read as floating component grids vs the references' commanding headline focal),
# so every L2/L3 slide gets a statement-title focal drawn from a tier-0 headline
# claim, and the previous focal is demoted to primary. This is realistic-slide
# authoring (a real board slide has a title), scoped to fixtures (authoring = W3).
_HEADLINES = {
    "retrieval": "Cortex finds code grep can't",
    "principles": "Thirteen principles, one system",
    "vision": "Agents that act, not chat",
    "premium": "On track for the board",
    "architecture": "One graph, three planes",
    "roadmap": "Three phases to GA",
}


def _apply_titles() -> None:
    for name, f in FIXTURES.items():
        head = _HEADLINES[name]
        f.claims["htitle"] = Claim("htitle", head, "narrative", tier=0,
                                   items=1, chars=len(head))
        for lvl in ("L2", "L3"):
            plan = f.levels[lvl]
            units = [InformationUnit(["htitle"], "statement-title", "focal")]
            for u in plan.units:
                emph = "primary" if u.emphasis == "focal" else u.emphasis
                units.append(InformationUnit(u.claim_ids, u.component, emph,
                                             u.items, u.chars))
            plan.units = units
            plan.reading_order = list(range(len(units)))


_apply_titles()


def all_fixtures() -> list[Fixture]:
    return [FIXTURES[k] for k in sorted(FIXTURES)]


def claim_shapes_covered() -> set[str]:
    shapes: set[str] = set()
    for f in FIXTURES.values():
        for c in f.claims.values():
            shapes.add(c.shape)
    return shapes


def families_covered() -> set[str]:
    return {f.family for f in FIXTURES.values()}


__all__ = ["Fixture", "FIXTURES", "all_fixtures", "claim_shapes_covered", "families_covered"]
