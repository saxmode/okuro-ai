# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: prism workflow — the anti-sameness quotas, PORTED to the workflow IR
#   (components) rather than called on the compiler's (archetypes).
# index: Violation | legal_alternatives | check_quotas | DENSITY_RANK
# AGENT_HEADER_END -->
"""Variety quotas for the workflow build path.

The compiler's ``assert_quotas`` is real and enforces exactly the sameness this
deck path suffers from — and it CANNOT be called here. It takes a ``PagePlan``
and reasons entirely over ARCHETYPES (``density_rank``, ``is_dense``,
``fits_cardinality``); the workflow has no PagePlan and its unit of choice is a
COMPONENT. So the rules are ported, not invoked.

One rule is deliberately stronger than the compiler's, and one is deliberately
weaker.

STRONGER: the compiler's density ladder compares archetype ranks. Components
carry their own ``density`` (sparse / medium / dense on all 37 entries), so the
same "a deeper rung must not render less" rule ports directly instead of being
deferred for want of a map.

WEAKER, and this is the load-bearing design decision: a violation is only a
DEFECT when a legal alternative exists. Measured on the first real deck, the
kit forces repeats — ``relationship`` has exactly one component that is legal
at L3 by shape, depth AND capacity, so "no adjacent topic may repeat a
component" is unsatisfiable there no matter how well the agent reasons. A gate
that cannot be met is not a gate, it is a build that never ships. When the
alternative set is empty the finding is reported as a KIT GAP against the
component library, not as a mistake by the agent — which is also the evidence
P2.6's capacity/depth work needs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

LEVELS = ("L1", "L2", "L3")

#: Component `density` -> rank. A deeper rung must never render LESS than the
#: rung above it (the compiler's correctness quota, in component terms).
DENSITY_RANK = {"sparse": 0, "medium": 1, "dense": 2}

#: Distinct components a deck should use before variety is a real claim. Local
#: and declared: the measured corpus baseline is 11 distinct content components
#: across 2 decks, so 6 is a floor a healthy small deck clears and a
#: one-component deck cannot.
DECK_DISTINCT_FLOOR = 6

#: Below this many rendered units the distinct floor is meaningless — a deck
#: cannot show 6 distinct components on 4 surfaces, and demanding it would
#: refuse a legitimately small deck. Scales with the deck instead of being
#: absolute, which an earlier draft got wrong and the fixtures caught.
_DISTINCT_MIN_UNITS = 9


@dataclass(frozen=True)
class Violation:
    """A quota breach. ``kit_gap`` marks the ones the LIBRARY made unavoidable."""

    code: str
    where: str
    detail: str
    kit_gap: bool = False

    def __str__(self) -> str:
        return f"{self.where}: {self.detail}" + (" [kit gap]" if self.kit_gap else "")


def _library() -> dict[str, dict[str, Any]]:
    from okuro.prism.library.mcp_tools import _load  # type: ignore[attr-defined]

    try:
        entries = _load("components")["entries"]
    except Exception:  # noqa: BLE001 — fall back to the packaged file
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parents[1] / "library" / "components.json"
        entries = json.loads(p.read_text())["entries"]
    return {c["id"]: c for c in entries}


def legal_alternatives(shape: str, level: str, items: Optional[int],
                       *, exclude: Iterable[str] = ()) -> set[str]:
    """Components that could legally carry this unit instead of the chosen one.

    Legal means all three at once: the shape is in ``carries``, the rung is in
    ``info_depths``, and the item count sits inside ``capacity``. This is the
    set the agent actually had to choose from — an empty one means the kit, not
    the agent, produced the repeat.
    """
    out: set[str] = set()
    for cid, c in _library().items():
        if cid in exclude:
            continue
        if shape and shape not in (c.get("carries") or []):
            continue
        if level not in (c.get("info_depths") or []):
            continue
        cap = c.get("capacity") or {}
        if items and not (cap.get("min", 0) <= items <= cap.get("max", 10 ** 9)):
            continue
        out.add(cid)
    return out


def _unit_shape(unit: Any, claims: dict[str, Any]) -> str:
    claim = claims.get(getattr(unit, "dominant_claim", "") or "")
    return getattr(claim, "shape", "") or ""


def _density(component: str, lib: dict[str, dict[str, Any]]) -> int:
    return DENSITY_RANK.get((lib.get(component) or {}).get("density", "medium"), 1)


def check_quotas(
    topics: list[tuple[str, dict[str, list[Any]], dict[str, Any]]],
) -> list[Violation]:
    """Every quota breach across the deck.

    ``topics`` is ``[(topic_id, {level: [RenderedUnit]}, claims)]`` in DECK
    ORDER — adjacency is a property of the order the reader walks, so the
    caller must not sort it.
    """
    lib = _library()
    out: list[Violation] = []

    # 1. ROW ADJACENCY — neighbouring topics must not render alike at a rung.
    for level in LEVELS:
        for (ta, ua, ca), (tb, ub, cb) in zip(topics, topics[1:]):
            a, b = ua.get(level) or [], ub.get(level) or []
            shared = {u.component for u in a} & {u.component for u in b}
            for comp in sorted(shared):
                unit = next(u for u in b if u.component == comp)
                alts = legal_alternatives(
                    _unit_shape(unit, cb), level,
                    getattr(unit, "rendered_items", None),
                    exclude={comp},
                )
                out.append(Violation(
                    "quota_adjacent_repeat", f"{ta}/{tb}.{level}",
                    f"adjacent topics both render {comp!r}"
                    + (f"; legal alternatives: {sorted(alts)[:5]}" if alts
                       else f"; NO other component carries shape "
                            f"{_unit_shape(unit, cb)!r} at {level} within capacity"),
                    kit_gap=not alts,
                ))

    # 2. VERTICAL VARIETY — the superset STEP must look like a step.
    for tid, units, claims in topics:
        l1 = {u.component for u in (units.get("L1") or [])}
        l2 = {u.component for u in (units.get("L2") or [])}
        for comp in sorted(l1 & l2):
            unit = next(u for u in units["L2"] if u.component == comp)
            alts = legal_alternatives(
                _unit_shape(unit, claims), "L2",
                getattr(unit, "rendered_items", None), exclude={comp},
            )
            out.append(Violation(
                "quota_vertical_repeat", f"{tid}.L1/L2",
                f"L1 and L2 both render {comp!r} — the superset step reads identical"
                + (f"; legal alternatives: {sorted(alts)[:5]}" if alts
                   else "; NO legal alternative at L2"),
                kit_gap=not alts,
            ))

    # 2b. FIT QUALITY — the two fields the library declares on all 37 entries
    # and nothing enforced on this path: `capacity` (a 9-item claim does not go
    # in a 3-slot component) and `info_depths` (the tag-wall-at-L1 defect).
    #
    # These live HERE, not in solver.schema.validate, on purpose. That boundary
    # returns AssembleDefects — a hard refusal — and the kit measurably cannot
    # satisfy them everywhere: `comparison` and `relationship` have NO component
    # legal at L1 at all. Refusing there would make correct decks unbuildable,
    # so the same alternative-exists test the adjacency rule uses decides
    # whether a breach is the agent's or the library's.
    for tid, units, claims in topics:
        for level in LEVELS:
            for unit in units.get(level) or []:
                comp = lib.get(unit.component) or {}
                shape = _unit_shape(unit, claims)
                n = getattr(unit, "rendered_items", None)
                alts = legal_alternatives(shape, level, n, exclude={unit.component})

                if level not in (comp.get("info_depths") or []):
                    out.append(Violation(
                        "quota_depth_fit", f"{tid}.{level}",
                        f"{unit.component!r} declares info_depths="
                        f"{comp.get('info_depths')} and is being used at {level}"
                        + (f"; legal here: {sorted(alts)[:5]}" if alts
                           else f"; NO component carries shape {shape!r} at {level} "
                                "within capacity"),
                        kit_gap=not alts,
                    ))

                cap = comp.get("capacity") or {}
                lo, hi = cap.get("min"), cap.get("max")
                if n and lo is not None and not (lo <= n <= hi):
                    out.append(Violation(
                        "quota_capacity", f"{tid}.{level}",
                        f"{unit.component!r} holds {lo}-{hi} items but the claim "
                        f"renders {n}"
                        + (f"; legal here: {sorted(alts)[:5]}" if alts
                           else f"; NO component carries shape {shape!r} at {level} "
                                f"with {n} items"),
                        kit_gap=not alts,
                    ))

    # 3. DENSITY LADDER — a deeper rung must not render LESS (correctness).
    for tid, units, _claims in topics:
        ranks = {}
        for level in LEVELS:
            us = units.get(level) or []
            if us:
                ranks[level] = max(_density(u.component, lib) for u in us)
        seq = [ranks[lv] for lv in LEVELS if lv in ranks]
        if len(seq) >= 2 and any(b < a for a, b in zip(seq, seq[1:])):
            out.append(Violation(
                "quota_density_ladder", f"{tid}",
                f"density falls going deeper: "
                f"{ {lv: r for lv, r in ranks.items()} } (need non-decreasing)",
            ))

    # 4. DECK DISTINCT FLOOR — one deck must not be two components repeated.
    all_units = [u for _t, units, _c in topics for us in units.values() for u in us]
    used = {u.component for u in all_units}
    if len(all_units) >= _DISTINCT_MIN_UNITS:
        floor = min(DECK_DISTINCT_FLOOR, len(all_units) // 3)
        if len(used) < floor:
            out.append(Violation(
                "quota_deck_distinct", "deck",
                f"only {len(used)} distinct components across {len(all_units)} "
                f"units ({sorted(used)}) — floor is {floor}",
            ))
    return out


__all__ = ["DECK_DISTINCT_FLOOR", "DENSITY_RANK", "Violation",
           "check_quotas", "legal_alternatives"]
