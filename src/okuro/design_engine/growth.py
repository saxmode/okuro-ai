# SPDX-License-Identifier: Apache-2.0
"""Growth -- a whole brand from the two things a builder actually has.

    "someone building a new brand on the system must SEE THE SYSTEM GROW --
    see it being adapted LIVE"

A builder arrives with a typeface and a colour. Everything else in the register's
authored set has a defensible default, so the essentials funnel is not a
shortcut around authoring: it is the shortest input that produces a COMPLETE and
VALID brand, which the builder then refines slot by slot while watching each edit
re-derive.

Two pieces live here.

`brand_from_essentials` turns (family, colour) into a `Brand`. The only real
decision it makes is the pair of brand ADAPTATIONS, and it makes that decision
the way the rest of the system makes every other "n% of a colour" decision --
by compositing toward one of the brand's own neutrals until the colour crosses
the threshold. No new colour machinery, and the amount it needed is reported, so
"we darkened your red by 18% to make it stand on white" is a sentence the UI can
say rather than a value that silently appeared.

`STAGES` is the choreography: which part of the system materialises when. It
lives here, beside the engine, rather than in the page, because the ORDER is a
statement about the system -- the ground precedes the foreground precedes the
ladders precedes everything -- and a page that invented its own order would be
animating a system that does not exist.

NEUTRAL MOTION IS DELIBERATE. A new brand gets no signature curve:

    "a curve is a brand signature -- new brands get neutral motion"

`MotionSettings` already defaults that way (signature None, default_curve
ease-in-out), so this module adds nothing; it just never overrides it.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from . import colour as c
from .schema import Brand, BrandColours, FontSettings, Neutrals, WeightAssignment

# ---------------------------------------------------------------------------
# Adaptations -- one colour, adjusted twice
# ---------------------------------------------------------------------------

ADAPTATION_MARGIN = 0.02
"""How far past the threshold a proposed adaptation must land.

Landing exactly ON the threshold is the warn band's worst case, so a proposal aims to
clear it rather than touch it. Small on purpose: the goal is the least change
that works, not the most contrast available.
"""

_ADAPTATION_STEP = 0.01
_ADAPTATION_CAP = 0.98


@dataclass(frozen=True)
class Adaptation:
    """A proposed brand adaptation and what it cost.

    `amount` is the fraction of the neutral composited in; 0.0 means the
    canonical colour already worked on that ground and was left alone -- which
    is a real and common answer, not a failure to adapt.
    """

    pole: str
    value: str
    amount: float
    toward: str
    reason: str


def propose_adaptation(canonical: str, ground_pole: str, neutrals: Neutrals) -> Adaptation:
    """The brand colour, adjusted so it stands on a ground of `ground_pole`.

    A figure on a LIGHT ground has to be dark, and vice versa -- that is
    `separates`, the same predicate the resolver uses. The adjustment walks
    toward the brand's OWN black or white (never a generic one, because "brands
    might use different blacks (030303) and whites (f4f4f4)"), in the composite
    idiom the ladders and hover already use.

    It stops at the FIRST step that clears the threshold by `ADAPTATION_MARGIN`.
    That is the least change that works; a brand that wants more authors more.
    """
    want_dark = ground_pole == "light"
    toward = neutrals.black if want_dark else neutrals.white
    target = (
        c.POLARITY_THRESHOLD - ADAPTATION_MARGIN
        if want_dark
        else c.POLARITY_THRESHOLD + ADAPTATION_MARGIN
    )

    def clears(value: str) -> bool:
        l = c.lightness(value)
        return l <= target if want_dark else l >= target

    if clears(canonical):
        return Adaptation(
            pole=ground_pole,
            value=canonical,
            amount=0.0,
            toward=toward,
            reason=(
                f"the canonical colour already sits at L {c.lightness(canonical):.3f}, "
                f"which stands on a {ground_pole} ground unchanged."
            ),
        )

    amount = _ADAPTATION_STEP
    while amount <= _ADAPTATION_CAP:
        candidate = c.composite(toward, amount, canonical)
        if clears(candidate):
            return Adaptation(
                pole=ground_pole,
                value=candidate,
                amount=amount,
                toward=toward,
                reason=(
                    f"{'darkened' if want_dark else 'lightened'} by "
                    f"{amount:.0%} toward the brand's own "
                    f"{'black' if want_dark else 'white'} so it clears "
                    f"{c.POLARITY_THRESHOLD} and stands on a {ground_pole} ground."
                ),
            )
        amount = round(amount + _ADAPTATION_STEP, 4)

    # Cannot be reached by compositing -- report it honestly rather than
    # returning something that does not clear. The lint flags it downstream and
    # the brand still renders: weak contrast is a flag, never a rejection.
    return Adaptation(
        pole=ground_pole,
        value=canonical,
        amount=0.0,
        toward=toward,
        reason=(
            "no amount of the brand's own neutral moves this colour across "
            f"{c.POLARITY_THRESHOLD}; left unchanged and flagged. Authoring an "
            f"on-{ground_pole} shade by hand is where a brand fixes this."
        ),
    )


# ---------------------------------------------------------------------------
# The weight quad
# ---------------------------------------------------------------------------

LOWER_WEIGHT_TARGETS: tuple[int, ...] = (700, 400, 300)
"""What slots 2, 3 and 4 want to weigh.

Slot 1 has no number here, and that is the correction MEASURING the two
families the register names forced. Its target is the HEAVIEST face the family
ships, which is what the schema's own lint already says a slot 1 is:

    "slot 1 is meant to be the heaviest fatness range the family offers and
    slot 4 the lightest"

Targeting a NUMBER for slot 1 gets it wrong on a family that goes past it. On
this machine Geist Mono ships Black at 900 and ExtraBold at 800; against a
target of 800 the cost function picks ExtraBold, and the register states the
quad as Black/Bold/Medium/Light. Against "the heaviest", Black wins and the
register's quad falls out. JetBrains Mono is unaffected -- its heaviest IS
ExtraBold -- so both stated quads now come from one rule.
"""


def weight_quad(faces: dict[str, int] | None) -> tuple[str, str, str, str] | None:
    """Four descriptors from the faces a family REALLY ships, heaviest first.

    Slot 1 takes the family's heaviest face. Slots 2-4 are chosen exhaustively
    over the rest, not greedily: greedy gets tight families wrong, taking the
    same face for two adjacent targets and leaving a genuine rung unused.

    The reason a family's own faces decide this at all is that a slot is a
    fatness RANGE, not a name -- so the quad has to be read off what exists.
    Both quads the register states are reproduced:

        JetBrains Mono  ExtraBold / Bold / Regular / Light
        Geist Mono      Black / Bold / Medium / Light

    and Geist Mono's is only reachable because that family ships no Regular
    here at all; its ladder steps 300 -> 500.

    Returns None when the family ships fewer than four distinct weights. None
    is the honest answer and the caller must handle it: inventing a fourth
    weight for a three-weight family is exactly what
    `FontSettings.offered_weights` refuses to do.
    """
    if not faces:
        return None
    # A duplicate weight is one fatness range spelled two ways (Geist Mono
    # spells 250 both Thin and ExtraLight). Keep the first spelling so the
    # ladder cannot contain the same rung twice.
    by_weight: dict[int, str] = {}
    for name, weight in sorted(faces.items(), key=lambda kv: (-kv[1], kv[0])):
        by_weight.setdefault(weight, name)
    if len(by_weight) < 4:
        return None

    heaviest = max(by_weight)
    slot_1 = by_weight[heaviest]
    rest = sorted(
        ((name, weight) for weight, name in by_weight.items() if weight != heaviest),
        key=lambda kv: -kv[1],
    )

    best: tuple[float, tuple[str, ...]] | None = None
    for combo in combinations(rest, 3):
        weights = [w for _, w in combo]
        cost = sum((w - t) ** 2 for w, t in zip(weights, LOWER_WEIGHT_TARGETS))
        if best is None or cost < best[0]:
            best = (cost, tuple(name for name, _ in combo))
    if best is None:  # pragma: no cover - guarded by the len check above
        return None
    lower = best[1]
    return (slot_1, lower[0], lower[1], lower[2])


# ---------------------------------------------------------------------------
# The essentials funnel
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Essentials:
    """What a builder typed, and what the engine made of it."""

    brand: Brand
    on_light: Adaptation
    on_dark: Adaptation
    weight_note: str


def brand_from_essentials(
    brand_id: str,
    font_family: str,
    brand_colour: str,
    *,
    faces: dict[str, int] | None = None,
    stack: str | None = None,
) -> Essentials:
    """A complete, valid brand from a typeface and one colour.

    Every slot this does not take from the builder comes from the schema's own
    declared default -- the signals, the neutrals, the border and radius
    ladders, the effects and the size picks. None of them is copied from okuro's
    kit: a copy would rot the day a default is corrected, and it would hand a
    new brand okuro's signature motion, which is not the new brand's to have.

    The system base and the root are not among them and never were slots to
    default: they are `scale.BASE` and `scale.ROOT_PERCENT`, the same for every
    brand this funnel produces.
    """
    neutrals = Neutrals()
    on_light = propose_adaptation(brand_colour, "light", neutrals)
    on_dark = propose_adaptation(brand_colour, "dark", neutrals)

    quad = weight_quad(faces)
    if quad is None:
        font = FontSettings(family=font_family, faces=faces)
        note = (
            f"{font_family} offers fewer than four measured faces here, so the "
            "four slots keep the engine's default descriptors. A weight slot "
            "the family does not ship renders in whatever the browser "
            "substitutes -- authoring the four by hand is the fix."
        )
        if stack:
            font = FontSettings(family=font_family, stack=stack, faces=faces)
    else:
        kwargs = dict(
            family=font_family,
            slot_1=quad[0],
            slot_2=quad[1],
            slot_3=quad[2],
            slot_4=quad[3],
            faces=faces,
        )
        if stack:
            kwargs["stack"] = stack
        font = FontSettings(**kwargs)
        note = (
            f"{font_family} ships {len(faces or {})} measured faces; the four "
            f"slots took {' / '.join(quad)} -- the family's heaviest, then the "
            f"descending three closest to "
            f"{'/'.join(str(t) for t in LOWER_WEIGHT_TARGETS)}."
        )

    brand = Brand(
        id=brand_id,
        brand=BrandColours(
            canonical=brand_colour,
            on_light=on_light.value,
            on_dark=on_dark.value,
        ),
        neutrals=neutrals,
        font=font,
        weights=WeightAssignment(),
    )
    return Essentials(
        brand=brand, on_light=on_light, on_dark=on_dark, weight_note=note
    )


# ---------------------------------------------------------------------------
# The choreography
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stage:
    """One beat of the growth: what appears, and the sentence that names it."""

    key: str
    title: str
    caption: str
    reveals: tuple[str, ...]


STAGES: tuple[Stage, ...] = (
    Stage(
        key="colour",
        title="the colour lands",
        caption=(
            "One measurement is taken and it is the only one the system ever "
            f"takes: OKLab lightness against {c.POLARITY_THRESHOLD}. Which side "
            "it falls on decides everything below."
        ),
        reveals=("threshold", "brand.canonical"),
    ),
    Stage(
        key="adaptations",
        title="one colour, adjusted twice",
        caption=(
            "The same brand colour, moved just far enough to stand on a light "
            "ground and on a dark one. The adjustment IS the contrast "
            "mechanism -- there is no second gate behind it."
        ),
        reveals=("brand.on_light", "brand.on_dark"),
    ),
    Stage(
        key="neutrals",
        title="the brand's own black and white",
        caption=(
            "Not the engine's. A brand's neutrals are authored, because brands "
            "use different blacks and whites, and every foreground in the "
            "system is one of these two."
        ),
        reveals=("neutrals.black", "neutrals.white"),
    ),
    Stage(
        key="grounds",
        title="the grounds appear",
        caption=(
            "Light, dark, brand-full and the four signals -- every ground the "
            "system can reach from any root. The set is a fixed point, which "
            "is why a static sheet can serve a tree of unbounded depth."
        ),
        reveals=("ground", "background"),
    ),
    Stage(
        key="foregrounds",
        title="each ground reads its foreground",
        caption=(
            "One rule, run on every ground including the signals. A second "
            "predicate anywhere here would break the uniformity that makes a "
            "signal ground behave like any other."
        ),
        reveals=("foreground", "signal_backgrounds", "signal_foregrounds"),
    ),
    Stage(
        key="ladders",
        title="the ladders fan out",
        caption=(
            "Seventeen steps of the foreground, and seventeen of its opposite. "
            "Separators, borders, transparencies and the focus ring are all "
            "rungs of these two -- named positions, not new colours."
        ),
        reveals=(
            "alternate",
            "alternate_inverse",
            "separator",
            "border_full",
            "border_half",
            "transparent",
            "transparent_inverse",
            "blur_background",
            "focus",
        ),
    ),
    Stage(
        key="descent",
        title="children resolve against their parent",
        caption=(
            "Emphasis flips the polarity, branded asks whether the brand can "
            "stand here, signal takes the signal. Then it happens again, one "
            "level down, forever -- with no depth counter anywhere."
        ),
        reveals=(
            "highlight_neutral_background",
            "highlight_neutral_foreground",
            "highlight_branded_background",
            "highlight_branded_foreground",
            "border_branded",
            "border_branded_inverse",
        ),
    ),
    Stage(
        key="components",
        title="components colorize",
        caption=(
            "Seven background styles, four states each. A state re-reads its "
            "own ink off the fill it just became, and the border ladder means "
            "a state moves geometry as well as colour."
        ),
        reveals=(
            "solid",
            "solid_inverse",
            "solid_brand",
            "solid_brand_inverse",
            "states",
            "shadow",
            "blur_and_shadow",
            "disabled",
            "off",
        ),
    ),
    Stage(
        key="type",
        title="the type ladders arrive",
        caption=(
            "Four fatness ranges from the faces the family really ships, and "
            "three size ladders -- display, headings, content -- every rung "
            "computed off the grid rather than stored."
        ),
        reveals=("font.family", "font.slot_1", "font.slot_2", "font.slot_3", "font.slot_4"),
    ),
    Stage(
        key="flags",
        title="and the system says what it thinks",
        caption=(
            "Advice with a reason, sitting where the value lives. Nothing here "
            "blocks and nothing here corrects -- it is a brand decision, and "
            "the brand makes it."
        ),
        reveals=("flags",),
    ),
)
"""The order the system materialises in.

It is the dependency order, not a slideshow: the ground exists before the
foreground can be read off it, the foreground before the ladders it generates,
the ladders before the named rungs that ARE ladder positions. A builder
watching this is watching the chain the register calls
"background -> foreground -> everything".
"""


def stage_of(slot: str) -> str | None:
    """Which stage first reveals `slot`, or None if no stage claims it."""
    for stage in STAGES:
        if slot in stage.reveals:
            return stage.key
    return None
