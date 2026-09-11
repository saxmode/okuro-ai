# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 item 3 — the typed LLM/code BOUNDARY. The LLM (content
#   side) emits only content decisions: information units, component choice (from
#   manifest fit-scores), emphasis hierarchy (exactly one focal), reading order.
#   Code owns ALL geometry. Zero styling fields anywhere. Ships the 4 validators
#   from the solver critique (dim 3) + a bounded re-ask loop. Scope: schema +
#   validation over FIXTURE-authored units (component-translation authoring = W3).
# index:
#   Claim / InformationUnit / SlidePlan dataclasses
#   validate (the 4 validators) / validate_or_raise / propose_with_reask
# AGENT_HEADER_END -->
"""The content/geometry boundary schema and its validators.

The boundary is the anti-vibes guarantee (critique dim 7): the LLM never touches
CSS, geometry, spans, colours — it emits a typed ``SlidePlan`` of content
decisions and code does the rest. This module defines that schema and the four
machine-checkable validators the critique names (dim 3); quality beyond structure
(is the focal the RIGHT claim?) is the vision judge's job, not the schema's — the
schema guarantees structure, not taste.

Validators (all countable, all mirror the shipped assert_quotas discipline):
  1. exactly one ``focal`` unit at L1/L2.
  2. each unit's component is in the fit-set of its dominant claim shape.
  3. reading_order is a permutation of the unit indices.
  4. the focal unit's dominant claim traces to a tier-0 (most-important) claim.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from okuro.prism.solver.manifest import components_for_shape, get_component

EMPHASIS = ("focal", "primary", "supporting", "aside")
_FOCAL_LEVELS = ("L1", "L2")           # exactly-one-focal enforced here (v4 §3c)


@dataclass(frozen=True)
class Claim:
    """One immutable information atom. ``tier`` 0 = most important (tier-0)."""

    id: str
    text: str
    shape: str                          # metric|comparison|proportion|... (manifest claim_shapes)
    tier: int = 1
    source_id: str | None = None
    items: int = 1                      # cardinality this claim contributes
    chars: int = 0                      # text volume (for sizing)
    # Per-item REAL content bound into component slots (engine 2, W3 item 2). Each
    # item is a (label, detail, meta) triple mined from the source; a component maps
    # these generic slots to its own (card=title/body/footer, list=title/sub,
    # table row=cells, tag=label). Empty -> renderer falls back to synthetic fill
    # (flagged by the accuracy accounting; never an invented value). Tuple = hashable
    # so the frozen Claim stays hashable.
    items_content: tuple[tuple[str, str, str], ...] = ()

    def content_items(self) -> list[tuple[str, str, str]]:
        return [tuple(t) for t in self.items_content]


@dataclass
class InformationUnit:
    """One placed content unit: a set of claims rendered by one component with an
    emphasis. NO geometry, NO styling — those are code's."""

    claim_ids: list[str]
    component: str
    emphasis: str
    # optional explicit render cardinality/volume; else derived from the claims.
    items: int | None = None
    chars: int | None = None

    def dominant_claim(self, claims: dict[str, Claim]) -> Claim | None:
        """The unit's dominant claim = lowest tier (most important), stable by id."""
        cs = [claims[c] for c in self.claim_ids if c in claims]
        if not cs:
            return None
        return sorted(cs, key=lambda c: (c.tier, c.id))[0]

    def resolved_items(self, claims: dict[str, Claim]) -> int:
        if self.items is not None:
            return self.items
        return sum(claims[c].items for c in self.claim_ids if c in claims) or 1

    def resolved_chars(self, claims: dict[str, Claim]) -> int:
        if self.chars is not None:
            return self.chars
        return sum(claims[c].chars for c in self.claim_ids if c in claims)


@dataclass
class SlidePlan:
    """The LLM's typed output for one slide. ``family`` + ``level`` are content-
    side selections (answer 7 per-deck family; the depth level); geometry is
    entirely code's."""

    level: str                          # L1|L2|L3
    family: str                         # swiss|japanese|bauhaus|editorial
    units: list[InformationUnit]
    reading_order: list[int]            # permutation of range(len(units))
    fit_threshold: float = 0.5
    # P4.1 — the content side's ARRANGEMENT judgment (``arr-hero-rail``, ...),
    # read from prism_library's layout_selection_guide. Still content-side, still
    # zero geometry: it names a row GRAMMAR, and solver/arrangements.py resolves
    # it to the span tuples the beam may enumerate. None = let the beam choose
    # freely inside the family whitelist (the pre-P4 behaviour).
    arrangement: str | None = None


class SchemaError(ValueError):
    pass


def validate(plan: SlidePlan, claims: dict[str, Claim]) -> list[str]:
    """Run the 4 boundary validators. Returns a list of error strings ([] = ok)."""
    errs: list[str] = []
    n = len(plan.units)
    if n == 0:
        return ["empty slide plan (no units)"]

    # 0. every component exists in the manifest (structural precondition).
    for u in plan.units:
        try:
            get_component(u.component)
        except KeyError:
            errs.append(f"unknown component {u.component!r}")
        if u.emphasis not in EMPHASIS:
            errs.append(f"bad emphasis {u.emphasis!r} (expected {EMPHASIS})")

    # 1. exactly one focal at L1/L2.
    focals = [i for i, u in enumerate(plan.units) if u.emphasis == "focal"]
    if plan.level in _FOCAL_LEVELS and len(focals) != 1:
        errs.append(f"{plan.level}: expected exactly one focal unit, found {len(focals)}")

    # 2. component in fit-set of the unit's dominant claim shape.
    for i, u in enumerate(plan.units):
        dc = u.dominant_claim(claims)
        if dc is None:
            errs.append(f"unit {i}: no resolvable claim in {u.claim_ids}")
            continue
        fit_set = {c.id for c in components_for_shape(dc.shape, plan.fit_threshold)}
        if u.component not in fit_set:
            errs.append(
                f"unit {i}: component {u.component!r} not in fit-set for shape "
                f"{dc.shape!r} (fit<{plan.fit_threshold}); allowed: {sorted(fit_set)[:6]}"
            )

    # 3. reading_order is a permutation of unit indices.
    if sorted(plan.reading_order) != list(range(n)):
        errs.append(f"reading_order {plan.reading_order} is not a permutation of 0..{n-1}")

    # 4. focal's dominant claim traces to a tier-0 claim.
    if len(focals) == 1:
        fu = plan.units[focals[0]]
        dc = fu.dominant_claim(claims)
        if dc is None or dc.tier != 0:
            errs.append(
                f"focal unit {focals[0]}: dominant claim "
                f"{dc.id if dc else None!r} tier={dc.tier if dc else None} != tier-0"
            )
    return errs


def validate_or_raise(plan: SlidePlan, claims: dict[str, Claim]) -> None:
    errs = validate(plan, claims)
    if errs:
        raise SchemaError("; ".join(errs))


def propose_with_reask(
    proposer: Callable[[list[str]], SlidePlan],
    claims: dict[str, Claim],
    max_retries: int = 2,
) -> tuple[SlidePlan, list[str]]:
    """Bounded re-ask loop (critique dim 3): call ``proposer(prev_errors)`` up to
    ``max_retries+1`` times; each call is given the previous validation errors so
    a content proposer can correct. Returns (plan, errors) — errors is [] on
    success, else the last attempt's errors after exhausting retries. Deterministic
    given a deterministic proposer."""
    errs: list[str] = []
    plan: SlidePlan | None = None
    for _ in range(max_retries + 1):
        plan = proposer(errs)
        errs = validate(plan, claims)
        if not errs:
            return plan, []
    assert plan is not None
    return plan, errs


__all__ = [
    "Claim",
    "InformationUnit",
    "SlidePlan",
    "SchemaError",
    "EMPHASIS",
    "validate",
    "validate_or_raise",
    "propose_with_reask",
]
