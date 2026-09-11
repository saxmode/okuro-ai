# SPDX-License-Identifier: Apache-2.0
"""The resolver. One code path, every ground.

    "d5: ... the whole system that is calculated from top to bottom behaves the
    exact same way. On the UI a user can only chose dark / light. signal and
    brand are design facts. But they can be placed on the highest element and
    everything placed inside the theme, will behave the exact same way:
    parent(dark), highlighted-child(light), highlighted-subchild-of-child(dark)."

    "d4: ... everything behaves exactly the same. it doesn't fucking matter
    what the background color is."

Read as an architectural constraint, that says: NOTHING in this module may
branch on what a ground *is*. There is no `if ground_is_signal`, no
`if ground_is_brand`, no theme enum. A `Ground` carries a lightness (and, when
it has one, a colour) and an `origin` string that exists purely so a human can
read a trace -- no function below ever reads it. Every decision is a
measurement of the ground's VALUE against one threshold.

The chain, in the agent restatement he let stand:

    "background -> foreground -> everything, with shadows branching off the
    background directly."
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from . import colour as c
from .schema import BRANDED_FIGURE_RENDERS, Brand

# ---------------------------------------------------------------------------
# The ladder steps
# ---------------------------------------------------------------------------

ALTERNATE_STEPS: tuple[float, ...] = (
    100.0, 97.0, 95.0, 90.0, 80.0, 70.0, 60.0, 50.0, 40.0,
    30.0, 20.0, 10.0, 5.0, 3.0, 1.0, 0.1, 0.0,
)
"""His 17 steps, verbatim from the note (BASE.COLORS.ALPHA, both DARKEN and
LIGHTEN carry the same list).

    "The 17 steps are not wrong. how they are calculated is."

What was wrong was the calculation, which he corrected:

    "alternate / alternate inverse are not both opacity steps of the
    foreground. alternate-inverse is the opposite of the foreground."

So `alternate` is the FOREGROUND at these steps and `alternate_inverse` is the
OPPOSITE of the foreground at the same steps. The direction a ladder happens to
travel (darkening or lightening) is a consequence of which neutral the
foreground is -- it is not itself a rule, and nothing downstream re-reads the
background to find out.
"""

DISABLED_OPACITY = 0.40
OFF_OPACITY = 0.64


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ground:
    """A resolved background. The one value that descends the tree.

    `colour` is optional: a ground can be an image ("input boxes are placed on
    images together with background blur effects"). P0 q7 keeps that inside the
    single code path -- the region an element covers is sampled to an average
    OKLab L and fed to the same polarity test. `lightness` is therefore the
    authority and `colour` the convenience.

    `origin` is provenance for humans and traces. Reading it to make a decision
    would be the case-split the uniformity principle forbids; the metamorphic
    property test exists to catch that.
    """

    lightness: float
    colour: str | None = None
    origin: str = "root"

    @staticmethod
    def of(colour: str, origin: str = "root") -> "Ground":
        return Ground(lightness=c.lightness(colour), colour=colour, origin=origin)

    @staticmethod
    def from_lightness(l: float, origin: str = "image") -> "Ground":
        """An image ground, or any surface whose lightness was measured rather
        than authored (P0 q7)."""
        return Ground(lightness=l, colour=None, origin=origin)

    @property
    def polarity(self) -> str:
        return c.polarity(self.lightness)


@dataclass(frozen=True)
class Request:
    """What an element asks for. Only two things, plus a design fact.

    "2. unread is = needs emphasis, read = doesn't need emphasis. That's
    exactly the point."

    No request at all means inherit -- a read letter "consumes the mother
    theme, so it doesn't stick out". `signal` is not a user setting either:
    "signal and brand are design facts".
    """

    emphasis: bool = False
    branded: bool = False
    signal: str | None = None


@dataclass(frozen=True)
class AlphaStep:
    """A rung of a ladder: an ink, an opacity, and -- when the ground has a
    colour -- what it composites to.

    Both halves are carried because the emitter needs a literal while the
    engine needs the rendered value to measure. An image ground yields
    `over=None`, which is the honest answer: the literal does not exist,
    the rgba does.
    """

    ink: str
    percent: float
    over: str | None = None

    @property
    def alpha(self) -> float:
        return self.percent / 100.0

    @property
    def css(self) -> str:
        r, g, b = (round(x * 255) for x in c.parse_hex(self.ink))
        return f"rgba({r}, {g}, {b}, {self.alpha:g})"


@dataclass(frozen=True)
class Ladder:
    """The 17 steps of one ink over one ground."""

    ink: str
    steps: tuple[AlphaStep, ...]

    def at(self, percent: float) -> AlphaStep:
        for step in self.steps:
            if abs(step.percent - percent) < 1e-9:
                return step
        raise KeyError(f"{percent} is not one of the 17 steps")

    def __len__(self) -> int:
        return len(self.steps)


@dataclass(frozen=True)
class ShadowAnswer:
    """What separates an element from its ground.

    Usually a shadow. On a ground dark enough that a black veil cannot reach
    the anchor's perceptual darkening even at full opacity, it is a border --
    P0 q4, his answer in two words: "border ok".
    """

    kind: str  # "shadow" | "border"
    colour: str
    opacity: float
    width: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class States:
    """The overlays. "In Code this needs to be handled differently" -- so they
    are values here, not modes."""

    hover: AlphaStep
    hover_brand: AlphaStep
    pressed: str
    pressed_brand: str
    focus: AlphaStep
    border_widths: dict[str, float]


@dataclass(frozen=True)
class Palette:
    """Everything derivable from one ground. Complete, because a boundary that
    emits a partial palette leaks the parent's values across it."""

    ground: Ground
    background: str | None
    foreground: str
    highlight_neutral_background: str
    highlight_neutral_foreground: str
    highlight_branded_background: str
    highlight_branded_foreground: str
    alternate: Ladder
    alternate_inverse: Ladder
    separator: AlphaStep
    border_full: str
    border_half: AlphaStep
    border_branded: str
    border_branded_inverse: str
    focus: AlphaStep
    solid: str
    solid_inverse: str | None
    solid_brand: str
    solid_brand_inverse: str
    transparent: AlphaStep
    transparent_inverse: AlphaStep
    blur_background: AlphaStep
    shadow: ShadowAnswer
    blur_and_shadow: ShadowAnswer
    signal_backgrounds: dict[str, str]
    signal_foregrounds: dict[str, str]
    states: States
    disabled: float = DISABLED_OPACITY
    off: float = OFF_OPACITY
    flags: tuple[c.Flag, ...] = field(default_factory=tuple)


COMPONENT_STYLES: tuple[str, ...] = (
    "no-background",
    "solid",
    "solid-inverse",
    "solid-brand",
    "solid-brand-inverse",
    "transparent",
    "transparent-inverse",
)
""""Are the main background coloring engine for components." His seven, in his
order and with his names (note, THEME.COLORS.COMPONENTS.BACKGROUNDS).

"ghost" and "outlined" are not on this axis: "buttons: ghost and outlined
button, can also have a background" -- they are a button CARRYING one of these
seven. ghost is `no-background`; outlined is any of the seven with the border
ladder engaged.
"""


@dataclass(frozen=True)
class Outline:
    """A ring drawn OUTSIDE a component, in the mother's own foreground.

    RULING 5 (the owner, 2026-08-17): focus is ALTERNATE-NORMAL.100 -- which is
    the foreground -- "rendered as an OUTLINE OUTSIDE the focussed component".
    That supersedes the note's "FOCUS: Inherits from ALTERNATE-INVERSE.030",
    which was a translucent FILL and could vanish: alternate-inverse on white is
    white, so a light component on a light ground showed nothing (P2's OPEN-F2).

    It cannot vanish now, by construction. The ring is painted on the surface
    around the component -- the mother's -- and the mother's foreground is the
    one colour guaranteed to contrast with the mother's ground.

    An outline, not a border, because a border is INSIDE the box: it would move
    the component's own geometry and be occluded by its fill. `offset` is what
    puts the ring clear of the edge.
    """

    colour: str
    width: float
    offset: float


@dataclass(frozen=True)
class StateLayer:
    """One state of one component surface: what it is painted, what it reads
    with, how wide its border is, and -- for focus only -- its outline.

    The first three, always. The border ladder exists because "BORDER {none,
    normal, hover, focus}" is a ladder of SIZES -- a state that changes only
    colour has half the definition. And a state that changes its fill without
    re-reading its ink has the other half wrong: a ghost button whose pressed
    fill is the brand's red is a dark surface now, so its label is the brand's
    white, by the same "based on best contrast" rule every other foreground
    follows.

    `outline` is None on every state but focus. Under ruling 5 focus no longer
    repaints the component at all: its `colour` and `ink` are the normal ones
    and the whole state IS the ring.
    """

    name: str
    colour: str
    ink: str
    border_width: float
    outline: Outline | None = None


@dataclass(frozen=True)
class Surface:
    """A component of one style on one ground, resolved in every state.

    See `Resolver.surface_of` for the ST1 ruling this carries.
    """

    style: str
    ground: Ground
    fill: str
    ink: str
    border: str
    border_branded: str
    normal: StateLayer
    hover: StateLayer
    pressed: StateLayer
    focus: StateLayer

    def state(self, name: str) -> StateLayer:
        return {
            "normal": self.normal,
            "hover": self.hover,
            "pressed": self.pressed,
            "focus": self.focus,
        }[name]


def _rendered_fill(palette: "Palette", style: str) -> str:
    """What the eye actually sees where the component is.

    A table, not a case split: every entry reads one value out of the palette
    the resolver already produced, and `no-background` reads the ground itself
    because that is what shows through when nothing is painted.
    """
    if palette.background is None:
        raise ValueError(
            "a component surface needs a ground with a colour; an image ground "
            "carries only a lightness (P0 q7) and its component fills come from "
            "the blur-background route, which the emitter does not yet cover"
        )
    fills: dict[str, str] = {
        "no-background": palette.background,
        "solid": palette.solid,
        "solid-inverse": palette.solid_inverse or palette.background,
        "solid-brand": palette.solid_brand,
        "solid-brand-inverse": palette.solid_brand_inverse,
        "transparent": palette.transparent.over or palette.background,
        "transparent-inverse": palette.transparent_inverse.over or palette.background,
    }
    try:
        return fills[style]
    except KeyError:
        raise ValueError(
            f"unknown component style {style!r}; his seven are {COMPONENT_STYLES}"
        ) from None


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


class Resolver:
    """Pure. No I/O, no clock, no globals. One brand in, values out."""

    def __init__(self, brand: Brand) -> None:
        self.brand = brand

    # -- grounds ------------------------------------------------------------

    def root(self, appearance: str = "light", request: Request | None = None) -> Ground:
        """The ground with no surround.

        The ONLY place `appearance` is consulted, and the only user-facing
        choice in the whole system: "On the UI a user can only chose dark /
        light."

        A brand or a signal placed here is normal, not an edge case -- "they
        can be placed on the highest element". A brand at the root takes
        CANONICAL, his option A, because there is nothing to adapt to:
        "there is no reference color around the brand color."
        """
        request = request or Request()
        if request.signal:
            return Ground.of(self._signal(request.signal), origin=f"signal:{request.signal}")
        if request.branded:
            return Ground.of(self.brand.brand.canonical, origin="brand-canonical")
        return Ground.of(self.brand.surface(appearance), origin=f"root:{appearance}")

    def ground(self, request: Request, parent: Ground) -> Ground:
        """Resolve a child's ground from its request and its parent's ground.

        The whole descent, and it has no depth counter: "If a child has another
        child that needs high-contrast, the switch flips back."
        """
        if request.signal:
            return Ground.of(self._signal(request.signal), origin=f"signal:{request.signal}")
        if request.branded:
            return self._branded_ground(parent)
        if request.emphasis:
            return Ground.of(
                self.brand.surface(c.opposite(parent.polarity)), origin="emphasis"
            )
        return replace(parent, origin="inherit")

    def _branded_ground(self, parent: Ground) -> Ground:
        """Brand-full, conditionally -- with the fallback that used to be a theme.

        RULING 3 (the owner, 2026-08-17), and it has two clauses in this order:

            "As soon as there is a non-neutral color on the background, the
            nested brands fall back to neutral visuals with branded ctas"

        So a branded element on a CHROMATIC ground -- a signal ground, another
        brand's ground, any colour that is not one of this brand's own two
        neutrals -- takes the fallback ALWAYS. No measurement is taken, because
        there is nothing to measure: the answer does not depend on how the two
        colours compare. His Valiant story is exactly this and he closed it in
        the same breath -- inside the host's yellow, Valiant is always on white,
        never on yellow, and "It theoretically could be, but this would be a
        disasterous design decision."

        On a NEUTRAL ground the ratified test decides, and only there:

            "Let's go with 0.675 polarity test."

        This is also where P1 went wrong in the other direction. The threshold
        measures
        TEXT on colour; the brand colour is never text, so comparing brand
        against ground was a misuse of it everywhere EXCEPT this one question --
        may the brand stand as a ground against the neutral behind it.

        The adaptation is chosen by the SURROUND's polarity: "if a child is
        light and has a brand-full sub-child, the subchild needs to be
        brand-on-light. If the child is dark, the sub-child needs to be
        brand-on-dark." (Canonical belongs to the root, where there is no
        surround -- see `root`.)

        THE FALLBACK IS THE EMPHASIS FLIP -- ruling 3 as amended, and this is
        the whole of it:

            "valiant-in-yellow = always dark! because dark has higher contrast
            towards yellow and valiant needs a shade that works on dark."

        So the fallback ground is `surface(opposite(parent.polarity))`, which is
        character for character the `emphasis` branch of `ground()`. One
        mechanism, no special case, and the reason is a measurement of the HOST:
        the neutral that contrasts with the host is the one on the other side of
        it. The CTA then falls out with no extra rule -- `branded_figure` reads
        the fallback ground's own polarity and returns `brand.on_{that}`, so a
        dark fallback carries brand-on-dark.

        The brand is not consulted about which neutral it lands on. That was the
        earlier reading -- "the neutral the brand FIGURE stands on" -- and it
        put Valiant on white inside the host's yellow. It is superseded; the
        note's "background white, but main cta still purple inside white"
        sentence goes with it.

        On a NEUTRAL parent the two readings agree, which is why this is a
        simplification rather than a second behaviour: when brand-full fails on
        a neutral ground, the adaptation is necessarily on the same side as that
        ground, so flipping the host and flipping the adaptation land together.

        That fallback IS what brand-semi was, and it is no longer a theme:
        "Brand semi won't survive if we get the ladders right."
        """
        adaptation = self.brand.brand.adaptation(parent.polarity)
        if self.brand.is_neutral(parent.colour) and c.separates(
            adaptation, parent.lightness
        ):
            return Ground.of(adaptation, origin="brand-full")
        return Ground.of(
            self.brand.surface(c.opposite(parent.polarity)), origin="brand-fallback"
        )

    # -- figures ------------------------------------------------------------

    def branded_figure(self, ground: Ground) -> str:
        """The brand colour as a FIGURE on a ground -- a CTA, a badge, a highlight.

        ONE PREDICATE: is this ground one of the brand's own neutrals.

        RULING 1 governs the neutral case, and it is unconditional:

            "Only one is the brand color. brand color can have two shades
            adjusted for on-dark and on-light."

        A branded figure on a neutral ground renders `brand.on_{polarity}`,
        full stop. The ADJUSTMENT is the contrast mechanism; a brand that reads
        weakly on its own white fixes that by authoring a better on-light, not
        by having the engine swap its colour out. P1's WCAG-3:1 degradation gate
        did the swapping and is gone -- weak separation is a lint flag
        (`brand.figure-contrast-short`) and changes no value.

        The note's C1 governs every other ground, and it is the same sentence
        ruling 3 generalises:

            "on brand-full theme, the main ctas need to be neutral based on the
            contrast calculation and will be either black or white."

        On a chromatic ground -- the brand's own, another brand's, a signal's --
        the figure is the neutral the ground's polarity picks. That covers
        brand-on-brand (invisible) and brand-on-signal (a colour clash) with one
        line and no case per kind of ground.

        An image ground has no colour to compare against two authored slots, so
        it cannot be shown to be neutral; it keeps the adaptation and the blur
        route carries the separation, as the note's BACKGROUNDS section says.
        """
        adaptation = self.brand.brand.adaptation(ground.polarity)
        if ground.colour is None or self.brand.is_neutral(ground.colour):
            return adaptation
        return self.brand.ink(ground.polarity)

    def foreground(self, ground: Ground) -> str:
        """The brand's own black or white, chosen by the ground's polarity.

        His one documented non-computed value: "as brands, might use different
        blacks (030303) and whites (f4f4f4), for light and dark themes they are
        used. Need to be flagged, if they have not enough contrast."

        The same function serves a signal ground -- "ERROR-FOREGROUND:
        Calculated from white, black based on better contrast against
        BASE.COLORS.SIGNAL.RED" -- because "better contrast" and "opposite side
        of the threshold" are the same question asked once.

        AND IT IS ALSO WHERE AN AUTHORED SIGNAL INK LANDS (his ruling of
        2026-08-18), because "asked once" has to keep meaning that. A signal
        colour reaches a reader by two routes -- as a component's fill
        (`.ds-signal-error`) and as a GROUND a builder placed at the root -- and if
        the override only reached the first, one authored decision would produce
        two different inks for one colour. Measured before this line existed: the
        verdict band read "this brand works" while the canvas still marked a
        must-fix on the same warning ground.

        IT IS A VALUE TEST, NOT A KIND BRANCH, which is why rule 1 survives it.
        Nothing here asks how the ground was reached or what KIND it is; it asks
        whether the ground's COLOUR is one the brand authored an ink for -- the
        same shape as `Brand.is_neutral`, which rule 3's discriminator already
        establishes as the legitimate form of this question.
        """
        if ground.colour is not None:
            value = ground.colour.lower()
            for role, signal in self.brand.signals.by_role().items():
                if signal.lower() != value:
                    continue
                authored = self.brand.signals.foreground_override(role)
                if authored:
                    return authored
                break
        return self.brand.ink(ground.polarity)

    def _signal(self, role: str) -> str:
        try:
            return self.brand.signals.by_role()[role]
        except KeyError:
            raise ValueError(
                f"unknown signal {role!r}; the authored set is "
                f"{sorted(self.brand.signals.by_role())}"
            ) from None

    # -- ladders ------------------------------------------------------------

    def ladder(self, ink: str, ground: Ground) -> Ladder:
        return Ladder(
            ink=ink,
            steps=tuple(
                AlphaStep(
                    ink=ink,
                    percent=pct,
                    over=(
                        c.composite(ink, pct / 100.0, ground.colour)
                        if ground.colour
                        else None
                    ),
                )
                for pct in ALTERNATE_STEPS
            ),
        )

    # -- shadows ------------------------------------------------------------

    def shadow(self, ground: Ground, blurred: bool = False) -> ShadowAnswer:
        """Always black; only the opacity moves.

            "Shadows should always be darken. that's why it wasn't referring to
            alternate and alternate inverse. it was darken always. And based on
            the background color the amount of darkness for a shadow needs to
            differ."

        `blurred` selects the second anchor. shadow-color and blur-and-shadow
        are one variable at two strengths, not two effects: "it's only the
        shadow color."
        """
        anchor = (
            self.brand.shadow_anchors.blurred if blurred else self.brand.shadow_anchors.solid
        )
        surrogate = ground.colour or self.brand.surface(ground.polarity)
        alpha = c.shadow_alpha(surrogate, anchor)
        if alpha is not None:
            return ShadowAnswer(
                kind="shadow",
                colour="#000000",
                opacity=alpha,
                reason=f"reproduces the {anchor:.0%}-on-white darkening on this ground",
            )
        return ShadowAnswer(
            kind="border",
            colour=self.brand.ink(ground.polarity),
            opacity=self.brand.border.opacity,
            width=self.brand.border.weights()[0],
            reason=(
                "a black shadow cannot reach the anchor's darkening on a ground "
                "this dark even at full opacity, so separation comes from a "
                "border instead (P0 q4)"
            ),
        )

    # -- states -------------------------------------------------------------

    def states(self, ground: Ground, mother: Ground | None = None) -> States:
        """The overlays for a component sitting on `ground` inside `mother`.

        Two different grounds, deliberately, and this is the only asymmetry in
        the system:

          * hover reads the component's OWN ground (P0 q8).
          * pressed reads the MOTHER's -- the note is the only place he names
            which ground a state measures against: "Inherits from
            HIGHTLIGHT-BRANDED-BACKGROUND as long as enough contrast to
            background on the mother element."
        """
        mother = mother or ground
        fg = self.foreground(ground)
        hover_ink = self.brand.surface(c.opposite(ground.polarity))
        branded_bg = self.branded_figure(ground)
        brand_ground = Ground.of(branded_bg, origin="brand-figure")
        brand_hover_ink = self.brand.surface(c.opposite(brand_ground.polarity))
        return States(
            # "HOVER: Needs to calculate a 20 percent lighten up or darken based
            # on the backgorund color. Dark colors = lighten up, light colors =
            # darken down. By 20%"
            hover=AlphaStep(
                ink=hover_ink,
                percent=20.0,
                over=c.composite(hover_ink, 0.20, ground.colour) if ground.colour else None,
            ),
            # "HOVER-BRAND: [identical rule]"
            hover_brand=AlphaStep(
                ink=brand_hover_ink,
                percent=20.0,
                over=c.composite(brand_hover_ink, 0.20, branded_bg),
            ),
            # The palette-level pair, and the amendment of 2026-08-19 reaches the
            # second one. `pressed` is what a NEUTRAL surface presses to and its
            # pair carries no brand, so rule 9's unamended clause stands.
            pressed=self.branded_figure(mother),
            # "PRESSED-BRAND: Inherits from THEME.COLORS.BASE.FOREGROUND" -- and
            # the correction of 2026-09-11 fixes WHICH ground's foreground that
            # is: the MOTHER's. This line read `foreground(brand_ground)` -- the
            # foreground of the brand's own surface -- which is the same reading
            # `pressed_swap` carried and the same one he reported as a defect,
            # one layer up. On okuro's white root it answered #ffffff while the
            # correction paints #030303.
            #
            # IT MOVES WITH THE SWAP BECAUSE IT IS THE SAME QUESTION. This is the
            # PALETTE's answer to "what does a branded surface press to" and
            # `pressed_swap` is the SURFACE's; a sheet that publishes two answers
            # to one ruling is the defect `test_pressed_brand_is_the_branded_
            # surfaces_own_foreground` was written for, and leaving this behind
            # would have re-opened it under the other value.
            pressed_brand=self.foreground(mother),
            # RULING 5: focus is ALTERNATE-NORMAL.100, which is the foreground.
            # Supersedes the note's ALTERNATE-INVERSE.030.
            focus=self.ladder(fg, ground).at(100.0),
            border_widths=self.border_widths(),
        )

    def pressed_swap(self, colour: str, ink: str, mother: Ground) -> tuple[str, str]:
        """A PRESS EXCHANGES THE TWO ROLES. Register rule 9 as amended, his
        ruling of 2026-08-19:

            "on-click/pressed, a primary (branded) button's background and
            foreground SWAP ROLES relative to what they currently are: if the
            button's rest background is the BRANDED colour (foreground on it),
            pressed state -> background becomes the FOREGROUND colour, text
            becomes the brand colour. If the button's rest background is a
            NEUTRAL (foreground colour) with brand-coloured text/accent, pressed
            state -> background becomes the BRAND colour (on-dark/on-light per
            the button's ground polarity), text becomes the neutral foreground."

        A SWAP, NOT A LOOKUP. The two colours handed in are the component's own
        rest pair -- whatever they are, from whatever style produced them -- and
        the direction is decided by a VALUE TEST on them (`Brand.is_brand`),
        never by the component's style name. `.ds-solid-brand` appears nowhere in
        this function, which is what keeps register rule 1 intact: a hand-authored
        fill that happens to be the brand's own colour presses the same way the
        seventh style does.

        WHICH NEUTRAL THE BRANDED BUTTON PRESSES TO IS A QUESTION ABOUT THE
        PAGE, NOT ABOUT THE BUTTON -- and that is the correction of 2026-09-11.

        This branch used to return the button's own REST INK as the pressed
        background (`return ink, ...`), reading "background becomes the
        FOREGROUND colour" as the button's current foreground. Measured on the
        deployed page, `:active` asserted in the same evaluate pass:

            page ground #ffffff   rest #649d8d/#ffffff   pressed **#ffffff**
            page ground #030303   rest #a2ffe5/#030303   pressed **#030303**

        i.e. on a white page the button pressed to WHITE and on a black page to
        BLACK -- it vanished into the page in both directions, which is the one
        thing a press must not do. The owner, reporting it: "white background
        clicked primary button should change to black in background. and in
        light the opposite."

        The defect fires exactly when `polarity(rest ink) == polarity(mother
        ground)`, which is the common case: a brand adapted onto its own neutral
        carries the ink that neutral's polarity picks, and the button is standing
        on that same neutral. So the old reading was right about his GREEN
        example and wrong about every kit whose brand is adapted, including
        okuro's own.

        The neutral a press is reaching for is the MOTHER's foreground -- the
        one colour guaranteed to separate from the surface the button is
        standing on, which is what rule 9's unamended clause was asking for
        ("as long as enough contrast to background on the mother element"). His
        green example still lands where he said it would: on a light page,
        `foreground(mother)` is the brand's own black.

        THE SIDE THAT LANDS ON THE BRAND IS RE-EXPRESSED BY RULE 5, and that is
        the one thing a literal exchange of two strings would get wrong. Rule 5:
        "on-light / on-dark = ONE colour adjusted twice; the adjustment IS the
        contrast mechanism for figures on neutral grounds". So the brand never
        crosses a role as a raw string -- it crosses as
        `adaptation(polarity of the neutral it now has to separate from)`:

          * brand in the BACKGROUND at rest -> it becomes the FIGURE, and the
            neutral it must separate from is the pressed background, which is
            the mother's foreground.
          * brand in the INK at rest -> it becomes the GROUND, and the neutral
            it must separate from is the surround, which is the mother.

        Both lines are the same sentence of rule 5 asked about a different
        neighbour, and since the correction both read the MOTHER, which is why
        this is one rule and not two. The second branch already did -- it is the
        first that was answering out of the component.

        THE THIRD CASE IS NOT PART OF THE AMENDMENT. A pair holding no brand in
        either role has nothing to exchange with the brand, and he ruled on the
        branded button. So rule 9's unamended clause stands there verbatim --
        "PRESSED: Inherits from HIGHTLIGHT-BRANDED-BACKGROUND as long as enough
        contrast to background on the mother element", which is
        `branded_figure(mother)` with the ink re-read off it. A ghost button
        still presses to the brand; only a button that ALREADY carries the brand
        now swaps instead of pressing to the colour it is already painted.

        `is_brand` IS READ FIRST, DELIBERATELY. A brand whose adaptation is one of
        its own neutrals answers True to both tests, and that is exactly the
        degenerate case the amendment exists for: under the old rule such a button
        pressed to `branded_figure`, which is the colour it was already wearing, so
        a press changed nothing at all. Reading the brand test first makes it swap.
        """
        brand = self.brand
        if brand.is_brand(colour):
            pressed = self.foreground(mother)
            return pressed, brand.brand.adaptation(c.polarity(pressed))
        if brand.is_brand(ink):
            return brand.brand.adaptation(mother.polarity), colour
        fill = self.branded_figure(mother)
        return fill, brand.ink(c.polarity(fill))

    def focus_outline(self, mother: Ground) -> Outline:
        """The focus ring for a component sitting inside `mother` (ruling 5).

        Colour is ALTERNATE-NORMAL.100 of the MOTHER's ground, i.e. the mother's
        foreground -- the ring is drawn on the mother's surface, so that is the
        surface it has to be visible against.

        Width is the `focus` rung of the note's border ladder, which is where
        that rung now does its work: the component's own border no longer moves
        on focus, so the geometry the ladder describes belongs to the ring.

        Offset is computed like every other size -- base x factor, register rule
        10 -- at a quarter of the base, which is 2 px on base 8. PROVISIONAL: he
        ruled "outside", never how far outside.
        """
        from . import scale

        return Outline(
            colour=self.foreground(mother),
            width=self.border_widths()["focus"],
            offset=scale.size(scale.BASE, scale.QUARTER),
        )

    def border_widths(self) -> dict[str, float]:
        """"BORDER {none, normal, hover, focus} - 4 step ladder for border sizes".

        States change GEOMETRY, not only colour -- which is why the width ladder
        exists at all and why it is returned alongside the colour overlays.

        PROVISIONAL (OPEN-B1): the brand authors four weights (default 1/2/4/8,
        "every brand needs to be able to define their border weights"), and the
        state ladder has four rungs of which the first is definitionally zero.
        He never stated the mapping between the two. The engine uses
        none=0 and normal/hover/focus = the first three authored weights,
        leaving the heaviest available for a deliberately heavy border.

        Since ruling 5 the `focus` rung sizes the focus OUTLINE rather than the
        component's own border -- the ladder still has four rungs and they are
        still widths; only the box the widest one is drawn on has moved.
        """
        w = self.brand.border.weights()
        return {"none": 0.0, "normal": w[0], "hover": w[1], "focus": w[2]}

    # -- component surfaces (ST1) -------------------------------------------

    def surface_of(
        self, ground: Ground, style: str, mother: Ground | None = None
    ) -> "Surface":
        """A component of `style` sitting on `ground`, in every state.

        THIS IS THE ST1 RULING, which P1 left open ("which surface a state
        overlay composites against, per component style"). It is one formula,
        and component style enters it only as a different FILL:

          * The overlay's INK is chosen by the surface HE names. hover reads
            the component's own ground -- P0 q8, "the component's own
            background". pressed reads the mother -- the note is the only place
            he names a state's ground: "as long as enough contrast to
            background on the mother element". focus is ALTERNATE-NORMAL.100 of
            the MOTHER's ground (ruling 5), because the ring is drawn on the
            mother's surface -- it does not read the component at all.

            PRESSED IS THE ONE THAT ALSO READS THE COMPONENT'S OWN REST PAIR,
            since his amendment of 2026-08-19: a press exchanges the surface's
            background and foreground, so it needs both, and it falls back to
            the mother-gated clause above only when neither of the two holds the
            brand. `pressed_swap` is the whole rule.

          * The overlay is always COMPOSITED against the component's own
            RENDERED fill, because that is the pixel it paints on.

          * The rendered fill is what his BACKGROUNDS list says it is:
            solid -> the foreground, solid-inverse -> the background,
            solid-brand -> highlight-branded-background, solid-brand-inverse ->
            highlight-branded-foreground, transparent -> ALTERNATE-NORMAL.005,
            transparent-inverse -> ALTERNATE-INVERSE.00, no-background -> the
            ground shows through.

        No branch on the style NAME exists below: `_FILLS` reads a value out of
        the palette and every style then travels the same three lines. A ghost
        button is not a case -- it is the formula fed the ground itself, which
        is what its fill composites to.

        `no-background` is also what his "ghost" button is, and an "outlined"
        button is this style with the border ladder engaged -- neither is a
        style of its own, which is why the note lists seven and not nine.
        """
        mother = mother or ground
        palette = self.palette(ground, mother)
        rendered = _rendered_fill(palette, style)

        # hover: the ink comes from the component's own rendered fill (q8).
        hover_ink = self.brand.surface(c.opposite(c.polarity(rendered)))

        widths = self.border_widths()
        ink = self.brand.ink(c.polarity(rendered))

        pressed_colour, pressed_ink = self.pressed_swap(rendered, ink, mother)

        def layer(name: str, colour: str, width: float) -> StateLayer:
            """Every state re-reads its own ink off what it is now painted."""
            return StateLayer(
                name=name,
                colour=colour,
                ink=self.brand.ink(c.polarity(colour)),
                border_width=width,
            )

        return Surface(
            style=style,
            ground=ground,
            fill=rendered,
            ink=ink,
            # "BORDER-FULL - inherits from FOREGROUND": the foreground of the
            # surface the border is drawn on, which for a component is its own
            # rendered fill. On a no-background component that IS the ground's
            # foreground, so the note's rule is the default case of this one.
            border=ink,
            border_branded=palette.highlight_branded_background,
            normal=layer("normal", rendered, widths["normal"]),
            hover=layer(
                "hover", c.composite(hover_ink, 0.20, rendered), widths["hover"]
            ),
            # RULE 9 AS AMENDED (2026-08-19): a press EXCHANGES the surface's
            # background and foreground -- see `pressed_swap`, which holds the
            # whole rule including rule 9's unamended clause for a pair carrying
            # no brand. It is the one state whose ink is NOT re-read off its own
            # fill by `layer`: the swap decides both halves together, because
            # "the text becomes the brand colour" is a statement about where the
            # brand went, not about what contrasts with the new fill.
            # The border ladder has no `pressed` rung, so geometry does not move.
            pressed=StateLayer(
                name="pressed",
                colour=pressed_colour,
                ink=pressed_ink,
                border_width=widths["normal"],
            ),
            # RULING 5: focus does not repaint the component. It keeps the
            # normal fill, ink and border width, and adds a ring outside itself.
            focus=StateLayer(
                name="focus",
                colour=rendered,
                ink=ink,
                border_width=widths["normal"],
                outline=self.focus_outline(mother),
            ),
        )

    # -- the whole palette --------------------------------------------------

    def palette(self, ground: Ground, mother: Ground | None = None) -> Palette:
        """Every value derivable from one ground.

        The order is his chain: background -> foreground -> everything.
        """
        brand = self.brand
        fg = self.foreground(ground)
        inverse_ink = brand.surface(ground.polarity)  # "the opposite of the foreground"

        alternate = self.ladder(fg, ground)
        alternate_inverse = self.ladder(inverse_ink, ground)

        branded_bg = self.branded_figure(ground)
        branded_fg = brand.ink(c.polarity(branded_bg))

        signal_bgs = brand.signals.by_role()
        # SIGNAL FOREGROUNDS: computed by rule 2 unless the brand authored one.
        #
        # His ruling of 2026-08-18. The computation is unchanged and is still the
        # default for every role; an authored value simply wins, exactly the way
        # `brand.on_light` wins over a shade. Nothing below this line asks which
        # of the two it got -- that is what keeps rule 1's uniformity: the
        # override is authored INPUT, not a branch on a kind.
        # THE FLAG FOR AN OVERRIDE IS NOT RAISED HERE. A signal's ink is the same
        # on every ground -- the value is authored, and nothing about it depends on
        # what the signal is sitting on -- so raising it in this function would
        # report one finding once per REACHABLE GROUND, which is the duplication
        # the canvas already had to deduplicate by code. It belongs to the
        # authored brand, so it lives in `schema.lint`, where the rail reads it
        # and from where `markFlags` places it on the one ground it names.
        # ONE DEFINITION FOR BOTH ROUTES. `foreground` answers "what ink goes on
        # this colour" for a ground, and a signal FILL asks the identical question
        # about the identical colour -- so it is asked there rather than answered
        # a second time here. That is what makes an authored override reach a
        # `.ds-signal-error` component and a placed `signal:error` ground with one
        # value; two expressions of one rule is how they would drift.
        signal_fgs: dict[str, str] = {
            role: self.foreground(Ground.of(value, origin=f"signal:{role}"))
            for role, value in signal_bgs.items()
        }

        flags: list[c.Flag] = []
        if ground.colour:
            ratio = c.wcag_contrast(fg, ground.colour)
            if ratio < c.WCAG_AA_TEXT:
                flags.append(
                    c.Flag(
                        "foreground.contrast-short",
                        f"{fg} on {ground.colour} is {ratio:.2f}:1, under WCAG AA "
                        f"{c.WCAG_AA_TEXT}:1.",
                        f"ground:{ground.origin}",
                        ("foreground",),
                    )
                )
        if c.in_warn_band(ground.lightness):
            flags.append(
                c.Flag(
                    "ground.polarity-ambiguous",
                    f"ground lightness {ground.lightness:.3f} is inside the warn band "
                    f"{c.WARN_BAND}; the neutral choice here is weakly supported.",
                    f"ground:{ground.origin}",
                    ("ground", "foreground"),
                )
            )

        shadow = self.shadow(ground)
        if shadow.kind == "border":
            flags.append(
                c.Flag(
                    "shadow.became-border",
                    shadow.reason,
                    f"ground:{ground.origin}",
                    ("shadow", "blur_and_shadow"),
                )
            )

        # A branded figure that will not separate from the ground it is painted
        # on. The BRAND-level lint says the same thing about the authored slot;
        # this says it about THIS ground, which is what lets the canvas mark the
        # component rather than only the field -- "here is where it's failing".
        branded_ratio = c.wcag_contrast(branded_fg, branded_bg)
        if c.figure_contrast_short(branded_bg, ground.colour or branded_fg):
            flags.append(
                c.Flag(
                    "branded.figure-contrast-short",
                    f"the branded figure {branded_bg} on this {ground.origin} ground "
                    f"is under {c.WCAG_NON_TEXT_FLAG}:1. It still renders -- the "
                    f"on-{ground.polarity} shade is where a brand fixes this. Its own "
                    f"CTA reads {branded_ratio:.2f}:1.",
                    f"ground:{ground.origin}",
                    BRANDED_FIGURE_RENDERS,
                )
            )

        return Palette(
            ground=ground,
            background=ground.colour,
            foreground=fg,
            # "HIGHLIGHT-NEUTRAL-BACKGROUND - Assigned in Figma but is the
            # inverse of BACKGROUND" / "-FOREGROUND ... inverse of FOREGROUND".
            # This pair IS the next level in -- the emphasis flip, reached
            # without a request: a light ground's highlight-neutral is a black
            # surface carrying white.
            highlight_neutral_background=fg,
            highlight_neutral_foreground=inverse_ink,
            highlight_branded_background=branded_bg,
            highlight_branded_foreground=branded_fg,
            alternate=alternate,
            alternate_inverse=alternate_inverse,
            # "SEPARATOR: ... needs to be calculated: 10% of the foreground color"
            separator=alternate.at(10.0),
            # "BORDER-FULL - inherits from FOREGROUND"
            border_full=fg,
            # "BORDER-HALF: ... can be calculated: 20% foreground color"
            border_half=alternate.at(20.0),
            # "BORDER-BRANDED - Inherits from HIGHLIGHT-BRANDED-BACKGROUND"
            border_branded=branded_bg,
            # "BORDER-BRANDED-INVERSE - Inherits from HIGHLIGHT-BRANDED-FOREGROUND"
            border_branded_inverse=branded_fg,
            # RULING 5: "FOCUS = ALTERNATE-NORMAL/100 (which equals the
            # foreground), rendered as an OUTLINE OUTSIDE the focussed
            # component." Supersedes the note's ALTERNATE-INVERSE.030.
            focus=alternate.at(100.0),
            # "SOLID - inherits from THEME.COLORS.BASE.FOREGROUND"
            solid=fg,
            # "SOLID-INVERSE - inherits from THEME.COLORS.BASE.BACKGROUND"
            solid_inverse=ground.colour,
            solid_brand=branded_bg,
            solid_brand_inverse=branded_fg,
            # "TRANSPARENT - inherits from THEME.COLORS.ALTERNATE-NORMAL.005"
            transparent=alternate.at(5.0),
            # "TRANSPARENT-INVERSE - inherits from ALTERNATE-INVERSE.00"
            transparent_inverse=alternate_inverse.at(0.0),
            # "blur background needs to be background, which will be alternate
            # inverse always. so this works." -- at the note's 080 rung.
            blur_background=alternate_inverse.at(80.0),
            shadow=shadow,
            blur_and_shadow=self.shadow(ground, blurred=True),
            signal_backgrounds=signal_bgs,
            signal_foregrounds=signal_fgs,
            states=self.states(ground, mother),
            flags=tuple(flags),
        )

    # -- convenience --------------------------------------------------------

    def descend(self, requests: list[Request], root: Ground) -> list[Ground]:
        """Walk a chain of requests, returning the ground at every level.

        "So componentA(light).componentA1(dark).componentA1A(light)."
        """
        grounds = [root]
        for request in requests:
            grounds.append(self.ground(request, grounds[-1]))
        return grounds
