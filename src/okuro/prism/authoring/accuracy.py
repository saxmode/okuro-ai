# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W3 §3d — ACCURACY ACCOUNTING. Per slide: claims-in vs rendered.
#   A deficit (N<M) becomes an explicit "N of M — all M in L4" affordance — a
#   statement only ASSERTABLE because the ladder gate (iii) PROVED every mined claim
#   is in L4 (this module takes the LadderReport and refuses to emit the affordance
#   for a claim that is NOT L4-covered — that is a SILENT deficit and an error).
#   Also the count-truth rule (a title's number matches rendered items or states the
#   split) and synthesized/fallback flags carried into DeckDoc fields (W4 renders).
# index:
#   UnitAccuracy / SlideAccuracy / AccuracyReport / account / _claimed_count
# AGENT_HEADER_END -->
"""Accuracy accounting — no silent truncation, ever (audit root-cause fix).

The whole point is that a level rendering fewer items than a claim carries must
SAY SO. Each unit reports N (rendered) vs M (total); when N<M it must carry the
"N of M — all M in L4" affordance, and that affordance is only valid because the
ladder gate proved all M reached L4. A deficit on a claim the gate could NOT ground
in L4 is a SILENT deficit — surfaced here as an error, never as a soothing badge.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from okuro.prism.authoring.gate import LadderReport
from okuro.prism.authoring.ladder import LadderDoc

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}


def _claimed_count(text: str) -> int | None:
    """Extract a leading/embedded cardinal from a claim's text ('Thirteen
    principles' -> 13, '19 endpoints' -> 19). None if no clear count word/number."""
    if not text:
        return None
    m = re.search(r"\b(\d{1,3})\b", text)
    if m:
        return int(m.group(1))
    for w, n in _NUMBER_WORDS.items():
        if re.search(rf"\b{w}\b", text, re.IGNORECASE):
            return n
    return None


@dataclass(frozen=True)
class UnitAccuracy:
    claim_id: str
    component: str
    emphasis: str
    rendered_items: int                    # N
    total_items: int                       # M
    in_l4: bool
    synthesized: bool
    fallback: bool
    count_claimed: int | None              # a number asserted in the claim text
    affordance: str = ""                   # "N of M — all M in L4" when deficit
    silent_deficit: bool = False           # deficit that CANNOT be honestly labelled
    count_mismatch: bool = False           # title number != rendered/total (count-truth)
    content_available: int = 0             # real per-item content triples the claim carries
    content_incomplete: bool = False       # fewer real items than rendered -> synthetic fill

    @property
    def deficit(self) -> int:
        return max(0, self.total_items - self.rendered_items)

    def to_field(self) -> dict:
        """DeckDoc field the W4 viewer renders."""
        return {
            "claim": self.claim_id, "component": self.component,
            "emphasis": self.emphasis, "rendered": self.rendered_items,
            "total": self.total_items, "in_l4": self.in_l4,
            "synthesized": self.synthesized, "fallback": self.fallback,
            "affordance": self.affordance, "count_mismatch": self.count_mismatch,
            "content_available": self.content_available,
            "content_incomplete": self.content_incomplete,
        }


@dataclass
class SlideAccuracy:
    level: str
    units: list[UnitAccuracy]

    @property
    def claims_in(self) -> int:
        return sum(u.total_items for u in self.units)

    @property
    def rendered(self) -> int:
        return sum(u.rendered_items for u in self.units)

    def to_fields(self) -> list[dict]:
        return [u.to_field() for u in self.units]


@dataclass
class AccuracyReport:
    slides: dict[str, SlideAccuracy]
    silent_deficits: list[str]             # claim ids with an unlabelled deficit
    count_mismatches: list[str]            # count-truth failures
    synthesized: list[str]
    fallback: list[str]
    content_gaps: list[str] = field(default_factory=list)  # rendered cells with no real content

    @property
    def clean(self) -> bool:
        """Exit-gate form: zero silent deficits AND zero count-truth violations."""
        return not self.silent_deficits and not self.count_mismatches

    def to_deckdoc(self) -> dict:
        return {
            "levels": {lvl: s.to_fields() for lvl, s in self.slides.items()},
            "silent_deficits": self.silent_deficits,
            "count_mismatches": self.count_mismatches,
            "synthesized": self.synthesized,
            "fallback": self.fallback,
            "content_gaps": self.content_gaps,
        }

    def summary(self) -> str:
        return (f"accuracy: {'CLEAN' if self.clean else 'DIRTY'} · "
                f"silent-deficits {len(self.silent_deficits)} · "
                f"count-mismatch {len(self.count_mismatches)} · "
                f"synth {len(self.synthesized)} · fallback {len(self.fallback)} · "
                f"content-gaps {len(self.content_gaps)}")


def account(ladder: LadderDoc, report: LadderReport) -> AccuracyReport:
    """Build the accuracy accounting for an authored ladder, using the ladder
    report's L4-coverage to decide which deficits can be honestly labelled."""
    covered = set(report.l4_covered)
    slides: dict[str, SlideAccuracy] = {}
    silent: list[str] = []
    mismatches: list[str] = []
    synth: list[str] = []
    fallback: list[str] = []
    content_gaps: list[str] = []

    for level, units in ladder.units.items():
        ua_list: list[UnitAccuracy] = []
        for u in units:
            cid = u.dominant_claim
            g = ladder.grounding.get(cid)
            is_synth = bool(g and g.synthesized)
            is_fallback = bool(g and g.fallback)
            in_l4 = cid in covered
            claim = ladder.claims.get(cid)
            claimed = _claimed_count(claim.text) if claim else None

            affordance = ""
            silent_deficit = False
            if u.rendered_items < u.total_items:
                if in_l4:
                    affordance = f"{u.rendered_items} of {u.total_items} — all {u.total_items} in L4"
                else:
                    # a deficit we CANNOT honestly label -> the silent-truncation class.
                    silent_deficit = True
                    silent.append(cid)

            # count-truth: a claim whose text asserts ITS OWN cardinality ('Thirteen
            # principles' on a 13-item claim) must render that many OR carry the
            # deficit affordance. Scoped to claimed == M so an incidental number in
            # prose ('recall@5' on a 4-item claim) never false-positives (DP12).
            count_mismatch = False
            if (claimed is not None and claimed == u.total_items
                    and u.rendered_items < claimed and not affordance):
                count_mismatch = True
                mismatches.append(cid)

            if is_synth:
                synth.append(cid)
            if is_fallback:
                fallback.append(cid)

            # real-content coverage (engine 2): a multi-item unit whose claim carries
            # fewer NON-EMPTY (label present) content triples than it renders is
            # falling back to synthetic fill for the remainder — SHOWN, never invented.
            available = sum(1 for t in (claim.content_items() if claim else []) if t and t[0])
            content_incomplete = u.rendered_items > 1 and available < u.rendered_items
            if content_incomplete:
                content_gaps.append(cid)

            ua_list.append(UnitAccuracy(
                claim_id=cid, component=u.component, emphasis=u.emphasis,
                rendered_items=u.rendered_items, total_items=u.total_items,
                in_l4=in_l4, synthesized=is_synth, fallback=is_fallback,
                count_claimed=claimed, affordance=affordance,
                silent_deficit=silent_deficit, count_mismatch=count_mismatch,
                content_available=available, content_incomplete=content_incomplete,
            ))
        slides[level] = SlideAccuracy(level, ua_list)

    # de-dupe while preserving order.
    def _uniq(xs: list[str]) -> list[str]:
        seen: set[str] = set()
        return [x for x in xs if not (x in seen or seen.add(x))]

    return AccuracyReport(
        slides=slides, silent_deficits=_uniq(silent),
        count_mismatches=_uniq(mismatches), synthesized=_uniq(synth),
        fallback=_uniq(fallback), content_gaps=_uniq(content_gaps),
    )


__all__ = [
    "UnitAccuracy", "SlideAccuracy", "AccuracyReport", "account",
]
