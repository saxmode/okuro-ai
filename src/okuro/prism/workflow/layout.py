# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: prism workflow — the two layer-above-the-solver decisions that were
#   hardcoded: which grid FAMILY a deck uses, and what order its units are READ.
# index: derive_family | reading_order | FamilyChoice
# AGENT_HEADER_END -->
"""Family selection and reading order for the workflow build path.

Both sat above a genuinely serious solver and both were constants: ``family``
was the string ``"swiss"`` for every deck ever built, and ``reading_order`` was
``list(range(n))`` — a permutation validator that only ever saw the identity.

FAMILY. ``solver.selection.select_family`` exists and is never called here; it
takes ``content_character`` and ``audience_mood``, and NO workflow node emits
either. Wiring a placeholder selector to invented inputs would be worse than
the honest default it replaces, so the mapping from what the workflow DOES have
(the recipient brief) is written out explicitly below and the deck records
whether its family was CHOSEN by the agent, DERIVED here, or defaulted.

READING ORDER. The identity order is not obviously wrong — it is the order the
author wrote the blocks in. What it misses is the deck's own answer-first rule:
the focal unit is the answer, so it is read first even when it was authored
second. The solver scores ``reading_gravity`` (does the order follow an F/Z
path over the geometry), so an emphasis-led order also pulls the focal unit
toward the top-left rather than merely relabelling it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

#: Emphasis, most important first. Mirrors solver.schema.EMPHASIS.
_EMPHASIS_RANK = {"focal": 0, "primary": 1, "supporting": 2, "aside": 3}

#: RecipientBrief.jargon_tolerance -> select_family's `audience_mood`.
#: Written out rather than guessed at the call site: the two vocabularies are
#: genuinely different, and an undocumented coercion between them is how a
#: "selector" ends up being a constant with extra steps.
_MOOD_BY_JARGON = {"low": "board", "medium": "executive", "high": "technical"}

#: Words in the brief's free-text `delivery` line that move the content
#: character. Values are `solver.selection.CONTENT_BIAS` keys, NOT invented
#: labels — an unknown character silently scores from mood alone, which is how
#: a "selector" ends up returning swiss for everyone. Measured: with character
#: `analytical` the family is swiss for 4 of the 5 moods, so CHARACTER is the
#: input that actually moves this and the mapping has to reach it.
#: Deliberately small and declared: this reads a human sentence, so it fails
#: toward the neutral default rather than over-claiming.
_CHARACTER_HINTS = (
    ("narrative", ("story", "narrative", "walk through", "journey", "arc")),
    ("minimal", ("shallow", "surface-level", "concise", "brief", "glance",
                 "high-level", "headline", "skim")),
    ("editorial", ("long-form", "essay", "prose", "read in full", "reading")),
    ("conceptual", ("concept", "model", "framework", "architecture",
                    "big picture", "mental model")),
    ("analytical", ("analys", "detail", "rigor", "evidence", "data", "metric")),
)
_DEFAULT_CHARACTER = "analytical"
_DEFAULT_FAMILY = "swiss"


@dataclass(frozen=True)
class FamilyChoice:
    """Which family, and — the part that was missing — WHY."""

    family: str
    source: str          # "agent" | "derived" | "default"
    rationale: str = ""


def derive_family(brief: Optional[Any], *, chosen: str = "") -> FamilyChoice:
    """The deck's grid family.

    An explicit agent choice always wins: component and layout choice is the
    agent's job by design, and a selector that overrides it would take back the
    one decision the workflow deliberately delegates.
    """
    if chosen:
        return FamilyChoice(chosen, "agent", "explicit choice at assemble")
    if brief is None:
        return FamilyChoice(_DEFAULT_FAMILY, "default", "no recipient brief")

    from okuro.prism.solver.selection import select_family

    jargon = (getattr(brief, "jargon_tolerance", "") or "medium").lower()
    mood = _MOOD_BY_JARGON.get(jargon, "executive")
    delivery = (getattr(brief, "delivery", "") or "").lower()
    character = next(
        (name for name, words in _CHARACTER_HINTS if any(w in delivery for w in words)),
        _DEFAULT_CHARACTER,
    )
    fam = select_family(content_character=character, audience_mood=mood)
    return FamilyChoice(
        fam, "derived",
        f"jargon_tolerance={jargon!r} -> mood={mood!r}; "
        f"delivery -> character={character!r}",
    )


def reading_order(units: list[Any]) -> list[int]:
    """Indices in the order the reader should take them: answer first.

    Stable within an emphasis class, so the author's sequence survives wherever
    it is not overruled. Always a permutation of ``range(len(units))`` — the
    solver's validator #3 depends on it, and returning anything else would trade
    one silent defect for a louder one.
    """
    return sorted(
        range(len(units)),
        key=lambda i: (_EMPHASIS_RANK.get(getattr(units[i], "emphasis", "supporting"), 2), i),
    )


__all__ = ["FamilyChoice", "derive_family", "reading_order"]
