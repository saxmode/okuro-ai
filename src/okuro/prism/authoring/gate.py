# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W3 — the CODE LADDER GATE. A BLOCKING pipeline stage over
#   claim-COVERAGE MAPS (units, not prose — explicitly NOT the dead prose
#   critic.critique_ladder, pre-check REC-3). Three checks: (i) every L_n claim
#   traces to an L_{n+1} claim carrying >= its detail; (ii) L1 has exactly one
#   focal; (iii) L4-COVERAGE — every mined claim is verbatim-grounded in the L4
#   master doc (pre-check REQ-1: the keystone that makes 'all M in L4' a proven
#   fact). Plus the v4 §3c density-ladder (L1<L2<L3) and the schema precondition.
# index:
#   LadderViolation / LadderReport / check_ladder / assert_ladder
# AGENT_HEADER_END -->
"""The resolution-ladder gate — falsifiable, blocking, over coverage maps.

This is the gate the whole accuracy affordance rests on. Pairwise traceability
alone proves L1 subset L2 subset L3 (nothing gained upward is lost downward) but
NOT that every mined claim reached the reading level — a claim mined and dropped
from all levels would make "N of M — all M in L4" a lie. Check (iii) closes that
exact truncation-without-indication hole at the root: a mined claim whose verbatim
source slice is absent from the L4 master doc is a gate FAILURE, not a warning.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from okuro.prism.authoring.ladder import (
    LadderDoc, body_signature, shared_same_family,
)
from okuro.prism.authoring.topic import TopicSource
from okuro.prism.solver.schema import validate

_LEVELS = ("L1", "L2", "L3")


@dataclass(frozen=True)
class LadderViolation:
    check: str                            # "traceability"|"one-focal"|"l4-coverage"|"density"|"schema"
    level: str
    detail: str


@dataclass
class LadderReport:
    passed: bool
    violations: list[LadderViolation]
    # claim_id -> {level: rendered_items} — the coverage map (auditable evidence).
    coverage: dict[str, dict[str, int]]
    l4_covered: list[str]
    l4_missing: list[str]

    def summary(self) -> str:
        if self.passed:
            return f"ladder OK · {len(self.coverage)} claims · L4-coverage {len(self.l4_covered)}/{len(self.l4_covered)+len(self.l4_missing)}"
        return f"ladder FAIL · {len(self.violations)} violations"


def _coverage_map(ladder: LadderDoc) -> dict[str, dict[str, int]]:
    """claim_id -> {level: max rendered_items at that level}."""
    cov: dict[str, dict[str, int]] = {}
    for level in _LEVELS:
        for u in ladder.units.get(level, []):
            for cid in u.claim_ids:
                cov.setdefault(cid, {})
                cov[cid][level] = max(cov[cid].get(level, 0), u.rendered_items)
    return cov


def check_ladder(ladder: LadderDoc, topic: TopicSource) -> LadderReport:
    """Run every ladder check. Returns a falsifiable report (passed + violations +
    the coverage map that proves it)."""
    violations: list[LadderViolation] = []
    cov = _coverage_map(ladder)

    # W5 Phase D — a topic may emit a SHORTER ladder (honest level-collapse): only
    # the levels present in ``units`` are gated, and adjacency is over those. A short
    # ladder is CLEAN by construction (its kept levels rise + re-compose); DIRTY only
    # when a PRESENT level fails. L1 (the hook) is always present.
    present = [lvl for lvl in _LEVELS if lvl in ladder.units]
    adjacent = list(zip(present, present[1:]))

    # 0. schema precondition — each present level is a structurally valid SlidePlan.
    for level in present:
        for err in validate(ladder.plans[level], ladder.claims):
            violations.append(LadderViolation("schema", level, err))

    # (ii) L1 has exactly one focal.
    focals = [u for u in ladder.units["L1"] if u.emphasis == "focal"]
    if len(focals) != 1:
        violations.append(LadderViolation(
            "one-focal", "L1", f"expected exactly one focal, found {len(focals)}"))

    # (i) traceability: every L_n claim traces to the NEXT PRESENT level with >= detail.
    for shallow, deep in adjacent:
        for cid, per_level in cov.items():
            if shallow not in per_level:
                continue
            if deep not in per_level:
                violations.append(LadderViolation(
                    "traceability", shallow,
                    f"claim {cid!r} in {shallow} but absent from {deep} (superset broken)"))
            elif per_level[deep] < per_level[shallow]:
                violations.append(LadderViolation(
                    "traceability", shallow,
                    f"claim {cid!r}: {deep} renders {per_level[deep]} < {shallow} "
                    f"{per_level[shallow]} (detail decreases going deeper)"))

    # (iii) L4-COVERAGE — every mined claim verbatim-grounded in the master doc.
    l4_covered: list[str] = []
    l4_missing: list[str] = []
    doc = topic.master_doc or ""
    for cid in topic.mined_claim_ids():
        g = topic.ground(cid)
        quote = (g.source_quote or "").strip()
        if quote and quote in doc:
            l4_covered.append(cid)
        else:
            l4_missing.append(cid)
            violations.append(LadderViolation(
                "l4-coverage", "L4",
                f"mined claim {cid!r} not verbatim-grounded in L4 master doc "
                f"({'empty source_quote' if not quote else 'quote not a substring'})"))

    # R32 — adjacent PRESENT levels must be DISTINCT COMPOSITIONS, not one slide
    # accreting rows: (a) the body macro-skeleton signature (render-family sequence)
    # differs, and (b) a claim shared by adjacent levels RE-COMPOSES (different render
    # family) rather than repeating the identical band. Uses the SAME signature
    # helpers the ladder collapse used to select these levels, so a kept ladder that
    # collapse deemed distinct cannot fail here (single source, no drift).
    for shallow, deep in adjacent:
        su, du = ladder.units[shallow], ladder.units[deep]
        if body_signature(su) == body_signature(du):
            violations.append(LadderViolation(
                "recomposition", shallow,
                f"{shallow} and {deep} share an identical body macro-skeleton "
                f"{body_signature(su)} (accreting rows, not a distinct composition)"))
        if shared_same_family(su, du):
            violations.append(LadderViolation(
                "recomposition", shallow,
                f"every claim shared by {shallow}/{deep} keeps the same render family "
                f"(repeated band, not re-composed)"))

    # v4 §1/§3c resolution ladder — INFORMATION strictly rises L1 < L2 < L3 (the
    # "pixeled image -> full resolution" metaphor). Measured as information load
    # (items + units), NOT ink: ink varies by component airiness (a verb-grid of 6
    # out-inks a tag-wall of 13), so ink monotonicity would fight the information
    # ladder. Ink-ceiling adherence per level is the solver's density_band term.
    load = ladder.loads
    order = [load[k] for k in _LEVELS if k in load]
    if not all(a < b for a, b in zip(order, order[1:])):
        violations.append(LadderViolation(
            "resolution", "*",
            f"information load not strictly rising L1<L2<L3: {load}"))

    return LadderReport(
        passed=not violations, violations=violations, coverage=cov,
        l4_covered=l4_covered, l4_missing=l4_missing,
    )


def assert_ladder(ladder: LadderDoc, topic: TopicSource) -> LadderReport:
    """Blocking form: raise on any violation (pipeline stage guard)."""
    rep = check_ladder(ladder, topic)
    if not rep.passed:
        lines = "\n".join(f"  [{v.check}/{v.level}] {v.detail}" for v in rep.violations)
        raise ValueError(f"ladder gate FAILED ({len(rep.violations)} violations):\n{lines}")
    return rep


__all__ = ["LadderViolation", "LadderReport", "check_ladder", "assert_ladder"]
