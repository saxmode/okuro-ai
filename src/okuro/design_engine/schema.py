# SPDX-License-Identifier: Apache-2.0
"""The authored brand input -- everything a brand states, and nothing it derives.

The dividing line, in his words:

    "Everytime a brand is added, everything that follows in the theme, needs to
    be implemented based on the chosen brand colors."

So: colours, a font, four weight descriptors, the geometry constants and the
motion vocabulary are AUTHORED. Grounds, foregrounds, ladders, borders,
highlights, states, shadows and every size are COMPUTED from them by
`resolve`. There is exactly one documented exception, and it is his:

    "FOREGROUND: Main Foreground color. Could be calculated, but as brands,
    might use different blacks (030303) and whites (f4f4f4), for light and dark
    themes they are used. Need to be flagged, if they have not enough contrast."

Validation is strict (extra keys are rejected: a typo'd slot is a silently
wrong brand). Linting is the opposite -- it FLAGS and never rejects:

    "Even if the contrast isn't high enough. It's brand decision."
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import colour as c
from . import fonts
from . import scale
from .scale import BASE, MAX_SENTINEL


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _hex_field(value: str) -> str:
    c.parse_hex(value)  # raises on anything that is not a hex colour
    return value.lower() if value.startswith("#") else "#" + value.lower()


def _norm(value: str | None) -> str:
    """One hex spelling, so a value test cannot miss on case or a bare digest.

    `is_neutral` and `is_brand` both compare an engine-produced colour against an
    authored slot, and the two arrive spelled differently often enough to matter:
    the fixtures author `#F03541` while the resolver hands back what the composite
    wrote. Two copies of this normalisation is how one of them would drift.
    """
    if value is None:
        return ""
    text = value.strip().lower()
    return text if text.startswith("#") else "#" + text


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------


class Signals(_Strict):
    """The authored signal colours.

    Three are in the note: red #C8255F, green #169723, blue #0071D8. The note
    also names FOUR signal themes -- "signal-* {error, success, info, info-2}"
    -- and info-2 has no authored colour anywhere in it. P0 q2 closed that:
    the fourth signal is named WARNING and its colour is pink ("I hate
    orange"). `info_2` does not exist in this schema; `pink` replaces it.
    """

    red: str = "#c8255f"      # error
    green: str = "#169723"    # success
    blue: str = "#0071d8"     # info
    pink: str = "#c52998"     # warning -- P0 q2

    foregrounds: dict[str, str] | None = None
    """AUTHORED FOREGROUND OVERRIDES, per role -- his ruling of 2026-08-18.

    The default is unchanged and stays the computation of rule 2: the brand's own
    black or white, chosen by the signal colour's polarity at the one threshold.
    This slot exists because a brand may disagree with the answer on a specific
    signal -- "white on red" is his own instinct, and the contrast study agreed
    with him on red -- and a design system that cannot express that forces the
    brand to move the signal COLOUR to get the ink it wanted.

    IT IS AUTHORED INPUT, NOT A KIND BRANCH, which is what keeps rule 1 intact.
    Nothing downstream asks whether a foreground was computed or authored: the
    resolver reads one value per role and every rule below it behaves the same.
    That is the same shape as `brand.on_light` -- a slot whose absence means
    "compute it" and whose presence means "this one".

    FLAGGED, NEVER REJECTED. An override that reads short of WCAG AA is a brand
    decision the engine reports and honours ("weak contrast is a lint flag, NEVER
    degradation or rejection", rule 5). The flag carries the ratio for BOTH
    candidates so the rail can show what was given up.

    Keys are ROLES (error / success / info / warning), never the colour names,
    because the role is what a consumer asks for.
    """

    _norm = field_validator("red", "green", "blue", "pink")(_hex_field)

    ROLES: ClassVar[tuple[str, ...]] = ("error", "success", "info", "warning")

    @field_validator("foregrounds")
    @classmethod
    def _known_roles(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        out: dict[str, str] = {}
        for role, colour in value.items():
            if role not in cls.ROLES:
                raise ValueError(
                    f"unknown signal role {role!r}; expected one of {cls.ROLES}"
                )
            out[role] = _hex_field(colour)
        # An empty mapping and no mapping mean the same thing, and storing the
        # empty one would make "has the brand authored anything here" two checks.
        return out or None

    def by_role(self) -> dict[str, str]:
        return {
            "error": self.red,
            "success": self.green,
            "info": self.blue,
            "warning": self.pink,
        }

    def foreground_override(self, role: str) -> str | None:
        """The authored ink for one role, or None to compute it."""
        return (self.foregrounds or {}).get(role)


class Neutrals(_Strict):
    """The brand's OWN black and white -- not the engine's.

    "as brands, might use different blacks (030303) and whites (f4f4f4)".
    okuro's black is #030303 (P0 q6: the note wins over the v1 kit's #0a0a0a).
    """

    black: str = "#030303"
    white: str = "#ffffff"

    _norm = field_validator("black", "white")(_hex_field)


MAX_ADDITIONAL = 10
"""How many additional colours a brand may author. His number."""


class AdditionalColour(_Strict):
    """One of the brand's additional colours -- a free palette entry.

    His ruling of 2026-08-17:

        "BRAND ADDITIONAL COLOURS -- up to 10, add/remove. Each gets the SAME
         foreground calculation as everything else (the one threshold). Freely
         usable in the interface; intended uses incl. presentation slides,
         charts."

    So this is authored input and nothing else: the VALUE is the brand's, and
    the foreground that goes on it is computed by the same one rule the whole
    system runs on. Nothing about a chart, a series or a slide is encoded here
    -- a consumer picks entries out of the emitted vocabulary and decides what
    they mean. `label` is for the builder's own eye; the EMITTED name is
    positional (`additional-1` .. `additional-10`), because a rename must not
    silently repoint every var() that already uses it.
    """

    value: str
    label: str = ""

    _norm = field_validator("value")(_hex_field)


class BrandColours(_Strict):
    """One colour, adjusted twice, plus the one that IS the brand.

    "It isn't: "dark color on light". It's a brand color that works better on
    light or on dark. You can't measure a brand color against: "is it dark?".
    The question needs to be: "does it work on dark?""

    `canonical` exists because at the root there is no surround to adapt to:
    "there is no reference color around the brand color." He chose option A --
    the brand declares its canonical colour, full-bleed uses it, figure use
    picks the ground-appropriate adaptation.

    ONE BRAND IS ONE COLOUR BY DEFAULT (his ruling, 2026-08-17):

        "canonical is chosen, and on-light/on-dark are SHADES OF THAT COLOUR
         (adjustment flow), not free colours. A brand genuinely using two
         different colours must TOGGLE into that mode explicitly."

    So the default record is `canonical` plus two ADJUSTMENT AMOUNTS. A shade is
    the amount of the brand's own black (for a light ground) or its own white
    (for a dark ground) composited into the canonical -- the same mechanism
    `growth.propose_adaptation` walks and the same one the ladders and hover use,
    so a shade is never a second colour model. `Brand` materialises the amounts
    into `on_light` / `on_dark` on validation, which is why every reader in the
    engine still sees a resolved hex and needed no change.

    `two_colour_mode` is the explicit toggle. In that mode `on_light`/`on_dark`
    are free, independently authored colours and a shade amount is a
    contradiction rather than a preference, so it is rejected as the typo it is.

    Omitting BOTH an adaptation and its shade collapses that pole onto canonical,
    which is the untouched one-colour brand. Two identical adaptations are legal
    and mean something different from omitting them -- a brand ASSERTING it
    checked both grounds -- so the lint only ever flags that case, never rejects
    it.
    """

    canonical: str
    on_light: str | None = None
    on_dark: str | None = None

    two_colour_mode: bool = False
    shade_light: float | None = None
    shade_dark: float | None = None

    additional: tuple[AdditionalColour, ...] = ()

    _norm = field_validator("canonical", "on_light", "on_dark")(
        lambda v: _hex_field(v) if v is not None else None
    )

    @field_validator("shade_light", "shade_dark")
    @classmethod
    def _shade_range(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if not 0.0 <= v <= 1.0:
            raise ValueError("a shade is a fraction of the brand's own neutral, 0..1")
        return round(v, 4)

    @field_validator("additional")
    @classmethod
    def _additional_count(
        cls, v: tuple[AdditionalColour, ...]
    ) -> tuple[AdditionalColour, ...]:
        if len(v) > MAX_ADDITIONAL:
            raise ValueError(f"a brand authors at most {MAX_ADDITIONAL} additional colours")
        return v

    @model_validator(mode="after")
    def _modes_do_not_mix(self) -> "BrandColours":
        if self.two_colour_mode and (self.shade_light is not None or self.shade_dark is not None):
            raise ValueError(
                "two_colour_mode means on_light/on_dark are independently authored "
                "colours, so a shade amount contradicts it. Drop the shades or "
                "drop the toggle."
            )
        return self

    def adaptation(self, ground_polarity: str) -> str:
        """The brand colour for figure use on a ground of this polarity.

        Reads the RESOLVED value. In the default mode `Brand` has already
        materialised it from the shade amount, so there is exactly one place a
        shade becomes a colour and no caller has to know which mode it is in.
        """
        chosen = self.on_light if ground_polarity == "light" else self.on_dark
        return chosen or self.canonical

    def additional_pairs(self) -> list[tuple[str, AdditionalColour]]:
        """The additional colours with their EMITTED names, positional."""
        return [(f"additional-{i}", entry) for i, entry in enumerate(self.additional, start=1)]


# ---------------------------------------------------------------------------
# Type
# ---------------------------------------------------------------------------


class FontSettings(_Strict):
    """Family, stack, and four weight slots holding VERBATIM style names.

    The slots are fatness ranges, not fixed names -- one family fills them
    ExtraBold/Bold/Regular/Light, another Black/Bold/Medium/Light, because
    "Black and ExtraBold are in a similar range". `faces` is the family's own
    measured descriptor -> usWeightClass table; when present it is the
    authority for the numeric side (P0 q5).
    """

    family: str = "JetBrains Mono"
    stack: str = (
        "ui-monospace, 'SF Mono', 'Cascadia Code', 'Roboto Mono', Menlo, "
        "Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace"
    )
    slot_1: str = "ExtraBold"
    slot_2: str = "Bold"
    slot_3: str = "Regular"
    slot_4: str = "Light"
    faces: dict[str, int] | None = None

    def descriptor(self, slot: int) -> str:
        return (self.slot_1, self.slot_2, self.slot_3, self.slot_4)[slot - 1]

    def numeric(self, slot: int) -> int:
        return fonts.numeric_weight(self.descriptor(slot), self.faces)

    def offered_weights(self) -> list[tuple[str, int]]:
        """What a weight dropdown or slider may show -- filtered by the font's
        actually available values (q5)."""
        return fonts.available_weights(self.faces)


TYPE_BANDS: tuple[str, ...] = ("titles", "headings", "leads", "paragraphs")
"""The four type bands a brand authors against, in his order.

They were already the four fields of `WeightAssignment` and the four `.ds-{band}`
classes `emit` publishes; naming the tuple is what stops the third and fourth
consumer of the list -- `CaseAssignment`, the emitter, the API -- from each
carrying its own copy of it.
"""


class WeightAssignment(_Strict):
    """Which slot each type role uses. Ordinals 1..4, never face names.

    "Spec is correct. but it needs to be configurable anyway. for each brand."

    The default is P0 q1, stated in JetBrains Mono terms: titles=ExtraBold,
    headings=ExtraBold, leads=Bold, paragraphs=Regular -> slots 1/1/2/3. That
    supersedes both the note's 1/3/1/3 and the transcript's
    "Black, Black, Bold, Regular" reading.
    """

    titles: int = 1
    headings: int = 1
    leads: int = 2
    paragraphs: int = 3

    @field_validator("titles", "headings", "leads", "paragraphs")
    @classmethod
    def _in_range(cls, v: int) -> int:
        if not 1 <= v <= 4:
            raise ValueError("weight assignment is a slot ordinal 1..4")
        return v


class CaseAssignment(_Strict):
    """WHICH TYPE BANDS ARE SET IN UPPERCASE. The owner's ruling of 2026-08-19.

        "Title/Header/Lead/Paragraph bands need an UPPERCASE TOGGLE (authored
        per-band boolean, default per band as currently shipped)."

    IT IS A BOOLEAN, NOT A `text-transform` KEYWORD, and that is his word:
    a toggle. `lowercase` and `capitalize` are CSS values nobody ruled, and a
    four-way enum in the rail would be three affordances for a decision that has
    two answers. The emitter turns the boolean into the property.

    THE FOUR BANDS ARE THE ENGINE'S OWN FOUR, not the v1 kit's. `WeightAssignment`
    above already establishes titles/headings/leads/paragraphs as the per-band
    vocabulary a brand authors against, and `emit` already publishes them as the
    four `.ds-{band}` classes every specimen on the page carries beside its size
    class (`ds-h1 ds-headings`, `ds-n4 ds-leads`). So the case lands exactly where
    the weight lands, and a brand cannot end up with its weight keyed one way and
    its case another.

    THE DEFAULTS ARE THE SHIPPED v1 VALUES, read off the authored kit rather than
    chosen here: `data/kits/okuro-ds.yaml` sets `display-title: transform:
    uppercase` and `title` / `lead` / `paragraph` to `none`. Register rule 12
    admits the v1 kit YAML as authored DATA, which is the one thing it is being
    used for. v1's four bands map onto the engine's four in order --
    display-title -> titles, title -> headings, lead -> leads,
    paragraph -> paragraphs -- so the shipped state survives the port unchanged:
    the top band is uppercase and the other three are not.
    """

    titles: bool = True
    headings: bool = False
    leads: bool = False
    paragraphs: bool = False

    def css(self, band: str) -> str:
        """The `text-transform` value for one band. One definition, so the
        emitter and the API cannot disagree about what `True` renders as."""
        try:
            on = getattr(self, band)
        except AttributeError:
            raise ValueError(
                f"unknown type band {band!r}; the four are "
                "titles, headings, leads, paragraphs"
            ) from None
        return "uppercase" if on else "none"


class TrackingAssignment(_Strict):
    """LETTER-SPACING PER TYPE BAND, in em. His ruling, 2026-09-07.

    UNTIL THIS EXISTED NO BRAND COULD AUTHOR TRACKING AT ALL. The seven values
    lived in `adapter.TYPE_TRACKING`, keyed by the app's roles, with the adapter's
    own docstring conceding "still ZERO engine concept" — so a brand with a
    distinctive typographic voice could set its colours, its faces, its radius
    and its case, and not the one property that most separates a display face
    from a label.

    THE FOUR BANDS ARE THE ENGINE'S OWN, exactly where weight and case already
    land (`WeightAssignment`, `CaseAssignment`). A brand cannot end up with its
    weight keyed one way and its tracking another, and the emitter's four
    `.ds-{band}` classes carry all three properties off the same key.

    EM, NOT REM OR PX, and that is not a style choice: letter-spacing in em is
    font-relative, so it scales with whatever size the rung resolves to and never
    needs the root compensation every rem value in this system carries.

    THE DEFAULTS ARE WHAT THE APP RENDERS TODAY, carried over rather than
    re-derived — `titles` takes the -0.01em the display and title roles already
    had, and the other three take 0. ONE ROLE MOVES: `label` was 0.06em and its
    band (`leads`) also holds `lead` at 0. Four bands cannot express seven roles;
    that collapse is stated here rather than hidden, and `leads` keeps the 0 that
    the larger of its two roles was already rendering.
    """

    titles: float = -0.01
    headings: float = 0.0
    leads: float = 0.0
    paragraphs: float = 0.0

    def css(self, band: str) -> str:
        """The `letter-spacing` value for one band, as the emitter writes it.

        Zero is emitted as a bare `0` rather than `0em` — a unitless zero is the
        same length in CSS and it is what the app's own curated table already
        published, so the sheet does not change shape for the common case.
        """
        try:
            em = getattr(self, band)
        except AttributeError:
            raise ValueError(
                f"unknown type band {band!r}; the four are "
                "titles, headings, leads, paragraphs"
            ) from None
        return "0" if em == 0 else f"{em:g}em"


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


class BorderSettings(_Strict):
    """Per-brand authored, on his instruction:

    "the stepps are by accident looking like a formula. every brand needs to be
    able to define their border weights. THis is the default."

    "this is 40% opacity. this needs to be configurable. Placeholder uses the
    same percent for displaying text."
    """

    weight_factors: tuple[float, float, float, float] = (
        scale.EIGHTH, scale.QUARTER, scale.HALF, 1.0,
    )
    """FACTORS, not pixels -- register rule 10, "borders all compute". On base 8
    these are his authored 1 / 2 / 4 / 8 px, and 1/8 and 1/4 are two of the
    fractions the note's own sub-base values were made of."""

    opacity: float = 0.40

    @field_validator("opacity")
    @classmethod
    def _fraction(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("border opacity is a fraction 0..1")
        return v

    def weights(self, base: float = BASE) -> tuple[float, float, float, float]:
        """The four widths in px, computed off the SYSTEM base."""
        return tuple(scale.size(base, f) for f in self.weight_factors)  # type: ignore[return-value]


class RadiusSettings(_Strict):
    """"RADIUS {S, M, L, MAX} - 4 step ladder for radii".

    `inner = outer - distance` is P0 q3, confirmed: "Inside radius is meant to
    be auto calculated based on the inner container distance towards the outer
    container". His worked example writes 24 - 8 = 18; he flagged his own
    uncertainty twice and q3 settled it -- the arithmetic is right, 24 - 8 = 16.
    """

    base: float = BASE
    """"radius-base 8" -- authored, and the thing the three real rungs multiply.

    THE ONE BASE A BRAND STILL AUTHORS. It is not the system base (which is a
    constant and not a slot): register rule 12 lists "radius ladder S/M/L/MAX +
    radius-base 8" among the authored input, and moving it must move radii and
    nothing else."""

    s_factor: float = 1.0
    m_factor: float = 2.0
    l_factor: float = 3.0
    """S / M / L as factors of the radius base: 8 / 16 / 24 px on base 8.
    Register rule 10 -- a radius is a size, so it computes."""

    def outer(self, rung: str) -> float:
        """The rung in px. MAX is the SENTINEL, never a multiplication: register
        rule 10, "MAX/999 = named sentinel only"."""
        rung = rung.upper()
        if rung == "MAX":
            return MAX_SENTINEL
        factor = {"S": self.s_factor, "M": self.m_factor, "L": self.l_factor}[rung]
        return scale.size(self.base, factor)

    def inner(self, outer: float, distance: float) -> float:
        """P0 q3. Clamped at 0: a distance larger than the radius squares the
        corner, it does not invert it."""
        return max(0.0, outer - distance)


# ---------------------------------------------------------------------------
# Effects and motion -- authored values, from his kit
# ---------------------------------------------------------------------------


class EffectIntensity(_Strict):
    """One intensity, as FACTORS of the system base.

    Register rule 10 names effect strengths explicitly among the things that
    compute: "font sizes, gaps, effect strengths, borders all compute". On base
    8 the defaults below are his authored kit values in px.
    """

    shadow_x: float
    shadow_y: float
    shadow_spread: float
    blur_amount: float

    def px(self, base: float = BASE) -> "EffectIntensity":
        """The same intensity in pixels. The emitter renders this, never the
        factors."""
        return EffectIntensity(
            shadow_x=scale.size(base, self.shadow_x),
            shadow_y=scale.size(base, self.shadow_y),
            shadow_spread=scale.size(base, self.shadow_spread),
            blur_amount=scale.size(base, self.blur_amount),
        )


class EffectSettings(_Strict):
    """Four intensities. His authored geometry as factors; the shadow's COLOUR
    and OPACITY are not here because they are computed from the ground."""

    light: EffectIntensity = EffectIntensity(
        shadow_x=0.0, shadow_y=0.5, shadow_spread=2.0, blur_amount=2.0
    )
    normal: EffectIntensity = EffectIntensity(
        shadow_x=0.0, shadow_y=0.5, shadow_spread=3.0, blur_amount=4.0
    )
    strong: EffectIntensity = EffectIntensity(
        shadow_x=0.0, shadow_y=0.5, shadow_spread=4.0, blur_amount=8.0
    )
    x_strong: EffectIntensity = EffectIntensity(
        shadow_x=0.0, shadow_y=0.5, shadow_spread=5.0, blur_amount=16.0
    )
    """THE FOURTH STEP, and it has to be the LARGEST -- his ruling, 2026-09-06.

    IT USED TO BE `shadow_y=0.0, shadow_spread=1.0, blur_amount=8.0`, which was
    smaller than `strong` on offset AND spread. Emitted, that made `--shadow-xl`
    (`0px 0px 8px`) smaller than every rung below it, `xs` included -- measured on
    the live sheet.

    THE CONSUMER STATED THE CONTRACT THE DATA BROKE. `adapter.py`'s
    `SHADOW_INTENSITY` maps five app elevation names onto these four intensities
    and says so in its own docstring: "The app wants five monotone steps." It
    maps `xl` to this one. So a reader had a promise of monotonicity from the
    bridge and a non-monotone table underneath it, with nothing reporting the
    disagreement.

    ASKED RATHER THAN GUESSED, because the numbers could not say which side was
    wrong: the shape (no offset, tight spread, wide blur) reads like a GLOW, and
    a glow mapped onto a name meaning "biggest" would have made the NAME the
    error instead. But "glow" appears nowhere in this package -- it was an
    auditor's word, not authored intent -- so the question went to the owner and he
    ruled the numbers.

    THE VALUES CONTINUE THE LADDER HE AUTHORED rather than inventing a shape:
    spread runs 2 / 3 / 4 and blur 2 / 4 / 8 across light / normal / strong, so
    the fourth step is spread 5 and blur 16, with `shadow_y` staying at the 0.5
    every other intensity uses. Nothing else in the ladder moves.

    NO KIT HAS EVER AUTHORED ITS OWN EFFECTS -- measured across all four on
    disk -- so this default IS what every brand renders, and a future claim about
    effects must construct its own `EffectSettings` rather than compare kits."""

    def by_name(self, name: str) -> EffectIntensity:
        return {
            "light": self.light,
            "normal": self.normal,
            "strong": self.strong,
            "x-strong": self.x_strong,
        }[name]


class ShadowAnchors(_Strict):
    """The two constants the whole shadow table hangs off.

    "on white a 20% black color for a shadow is enough, while on dark it needs
    to be 100% to be visible."

    He accepted the derived table with "let's keep it like that", so these are
    treated as authored-until-revised rather than measured.
    """

    solid: float = c.SHADOW_ANCHOR_SOLID
    blurred: float = c.SHADOW_ANCHOR_BLURRED


class MotionSettings(_Strict):
    """Speeds and curves. Cannot exist in Figma at all, so these are purely his.

    A curve is a brand signature. A NEW brand gets neutral motion: `signature`
    is None until a brand authors its own, and the default pairing points at a
    neutral curve. okuro's kit sets signature explicitly and pairs it with
    `medium` -- that pairing is okuro's, not the system's default.
    """

    speeds: dict[str, int] = Field(
        default_factory=lambda: {
            "xfast": 150,
            "fast": 350,
            "medium": 650,
            "slow": 1200,
            "xslow": 1500,
        }
    )
    curves: dict[str, str] = Field(
        default_factory=lambda: {
            "ease-in": "cubic-bezier(0.4, 0, 1, 1)",
            "ease-out": "cubic-bezier(0, 0, 0.2, 1)",
            "ease-in-out": "cubic-bezier(0.4, 0, 0.2, 1)",
            "gentle": "cubic-bezier(0.25, 0.1, 0.25, 1)",
            "quick": "cubic-bezier(0.55, 0, 0.1, 1)",
            "bouncy": "cubic-bezier(0.68, -0.55, 0.27, 1.55)",
        }
    )
    signature: str | None = None
    default_curve: str = "ease-in-out"
    default_speed: str = "medium"

    @model_validator(mode="after")
    def _defaults_exist(self) -> "MotionSettings":
        if self.default_speed not in self.speeds:
            raise ValueError(f"default_speed {self.default_speed!r} is not a declared speed")
        known = set(self.curves) | ({"signature"} if self.signature else set())
        if self.default_curve not in known:
            raise ValueError(f"default_curve {self.default_curve!r} is not a declared curve")
        return self

    def curve(self, name: str) -> str:
        if name == "signature":
            if not self.signature:
                raise ValueError("this brand has authored no signature curve")
            return self.signature
        return self.curves[name]


# ---------------------------------------------------------------------------
# Sizing anchors
# ---------------------------------------------------------------------------

TEXT_RUNGS: tuple[str, ...] = ("XXL", "XL", "L", "M", "S", "XS", "XXS", "XXXS")
"""""SIZES {XXL, XL, L, M, S, XS, XXS, XXXS} - 8-STEP SIZING TABLE FOR TEXT SIZES"."""

COMPONENT_RUNGS: tuple[str, ...] = (
    "XXL", "XL", "L", "M", "S", "XS", "XXS", "XXXS", "XXXXS",
)
""""9 step ladder for component sizes. Not all of them are necessarily used, but
are in existance, that every size has a "downscale" option". The note spells
XXXS twice at the tail -- a transcription slip in a 9-name list; the ninth rung
is XXXXS, which is also what his own downscale rail shows (XXS -> XXXXS)."""

COMPONENT_ANCHOR_RUNG = "L"
"""THE RUNG THE COMPONENT LADDER IS ANCHORED ON, and therefore the rung at which
every size the app publishes is stated 1:1.

His ruling, 2026-09-06: *"The rung is an absolute default! it's coming from the
main design system and cannot be overwritten by a copy! what can be overwritten,
what brand takes what rung as default for mobile and for desktop."*

So `[data-rung="L"]` has to mean ONE geometry in every brand. That is only true
if the ladder is re-based against a FIXED rung; re-basing against the brand's own
default makes the same name mean two things -- measured before this constant
existed, `[data-rung="L"]` published 0.5rem inside okuro-ds and 0.4348rem inside
a brand defaulting to XL.

L IS NOT A CHOICE MADE HERE. `SizeFactors.component_rung_factors` already pivots
on it (`COMPONENT_RUNGS.index("L")`) and `component_anchor_factor` is documented
as "the `L` COMPONENT rung: 5.0 x base = 40 px". This name is that same fact,
said once, so the adapter and the ladder cannot drift apart."""


TEXT_BANDS: tuple[tuple[str, str, int], ...] = (
    ("T", "title", 6),
    ("H", "heading", 6),
    ("N", "normal", 9),
)
"""The three text bands, in his own letters -- T = title, H = heading,
N = normal. He renamed them when he authored the tables on 2026-08-17; the
engine's earlier "display / headings / content" survives nowhere, because the
band letter is what a class name and an API label are built from and two
vocabularies for one band is how a UI starts lying about the system."""

TEXT_STYLES: tuple[str, ...] = tuple(
    f"{letter}{i}" for letter, _name, count in TEXT_BANDS for i in range(1, count + 1)
)
""""{T,H}{1-6}, N{1-9}" -- T title, H heading, N normal. Built from the bands so
one band definition governs the ids, the CSS class names and the API labels."""


TEXT_LINE_HEIGHTS: dict[str, float] = {
    # T -- title. Display sizes set tight; a 136px headline at 1.6 would open a
    # 218px line box.
    "T1": 1.05, "T2": 1.05, "T3": 1.1, "T4": 1.1, "T5": 1.15, "T6": 1.15,
    # H -- heading. H6 is the app's `display` role and carries ITS number.
    "H1": 1.15, "H2": 1.15, "H3": 1.15, "H4": 1.15, "H5": 1.15, "H6": 1.15,
    # N -- normal. The five cells the adapter's roles name carry THEIR numbers
    # verbatim (N1 title 1.2, N3 subtitle 1.3, N4 lead 1.55, N5 body 1.6,
    # N6 small 1.5, N7 label 1.4); N2, N8 and N9 are interpolated between their
    # neighbours, which is the only place in this table a value was chosen here.
    "N1": 1.2, "N2": 1.25, "N3": 1.3, "N4": 1.55, "N5": 1.6,
    "N6": 1.5, "N7": 1.4, "N8": 1.4, "N9": 1.4,
}
"""LINE HEIGHT per text style -- unitless, and a SYSTEM CONSTANT rather than a
brand slot.

WHY IT IS NOT AUTHORED. Register rule 12 lists the authored input exhaustively
and line height is not on it, so a brand cannot move it -- the same reasoning
that made `BASE` and `ROOT_PERCENT` constants in r3. Unitless line heights are
font-relative, so nothing here moves with the root and no rem arithmetic touches
it.

WHY IT LIVES HERE AND NOT IN `adapter.py`. It was in the adapter, keyed by the
app's seven type ROLES (`TYPE_METRICS`), and the emitted sheet published it as
`--type-{role}-line` on `:root`. Nothing read it: `emit._size_rules` wrote
`font-size` and nothing else on the 21 `.ds-*` classes, so every specimen
element on the canvas rendered at `line-height: normal` while the authored value
sat in a custom property -- measured 2026-08-18, zero occurrences of
`line-height` in a 249,900-byte sheet. Moving the definition to the ENGINE, keyed
by the engine's own text style, is what lets one table serve both vocabularies:
`adapter.TYPE_METRICS` now reads its line half from here, so the seven
`--type-*-line` values are unchanged and the 21 classes finally carry theirs.

The seven cells the adapter's roles map to are its own former values, verbatim.
Three N cells (N2, N8, N9) had no role and were interpolated; every other cell
follows its band."""


def line_height(style: str) -> float:
    """The line height of one text style. Raises on an unknown style, because a
    silent fallback here is how a band starts rendering at `normal` again."""
    try:
        return TEXT_LINE_HEIGHTS[style.upper()]
    except KeyError:  # pragma: no cover - guard
        raise ValueError(
            f"unknown text style {style!r}; expected one of {TEXT_STYLES}"
        ) from None


class RungDefaults(_Strict):
    """WHICH RUNG A BRAND OPENS ON, per viewport class.

    The owner's ruling of 2026-08-18, the other half of "the mother table is
    locked": a brand does not get to move the size TABLE, and it does get to say
    which ROW of it is its default -- once for desktop and once for mobile.

    This is the only sizing choice a brand still owns, and it is a POINTER into
    the engine's table rather than a value in it. That is what keeps rule 10
    intact: every size is still `base x factor` off one engine-defined table, and
    two brands that pick the same rung get identical numbers.

    Both names must be text rungs (`TEXT_RUNGS`). One attribute drives both
    families -- the emitted `[data-rung="RUNG"]` rules prefix the text classes
    AND the component classes -- so a rung named here sizes type and components
    together, which is the frame inheritance of rule 10.
    """

    desktop: str = "L"
    mobile: str = "M"

    @field_validator("desktop", "mobile")
    @classmethod
    def _known_rung(cls, value: str) -> str:
        rung = value.upper()
        if rung not in TEXT_RUNGS:
            raise ValueError(
                f"unknown rung {value!r}; expected one of {TEXT_RUNGS}"
            )
        return rung


class Defaults(_Strict):
    """The brand's opening state. One member today; a namespace on purpose, so
    the next default does not become a second top-level Brand field."""

    rung: RungDefaults = RungDefaults()


AUTHORED_TEXT_PX: dict[str, dict[str, float]] = {
    "XXL": {
        "T1": 136, "T2": 120, "T3": 104, "T4": 100, "T5": 96, "T6": 88,
        "H1": 80, "H2": 72, "H3": 64, "H4": 56, "H5": 48, "H6": 40,
        "N1": 32, "N2": 28, "N3": 24, "N4": 22, "N5": 20,
        "N6": 18, "N7": 16, "N8": 14, "N9": 12,
    },
    "XL": {
        "T1": 128, "T2": 112, "T3": 100, "T4": 96, "T5": 88, "T6": 80,
        "H1": 72, "H2": 64, "H3": 56, "H4": 48, "H5": 40, "H6": 32,
        "N1": 28, "N2": 24, "N3": 22, "N4": 20, "N5": 18,
        "N6": 16, "N7": 14, "N8": 12, "N9": 10,
    },
    "L": {
        "T1": 120, "T2": 104, "T3": 96, "T4": 88, "T5": 80, "T6": 72,
        "H1": 64, "H2": 56, "H3": 48, "H4": 40, "H5": 32, "H6": 28,
        "N1": 24, "N2": 22, "N3": 20, "N4": 18, "N5": 16,
        "N6": 14, "N7": 12, "N8": 10, "N9": 8,
    },
    "M": {
        "T1": 112, "T2": 100, "T3": 88, "T4": 80, "T5": 72, "T6": 64,
        "H1": 56, "H2": 48, "H3": 40, "H4": 32, "H5": 28, "H6": 24,
        "N1": 22, "N2": 20, "N3": 18, "N4": 16, "N5": 14,
        "N6": 12, "N7": 10, "N8": 8, "N9": 6,
    },
    "S": {
        "T1": 104, "T2": 96, "T3": 80, "T4": 72, "T5": 64, "T6": 56,
        "H1": 48, "H2": 40, "H3": 32, "H4": 28, "H5": 24, "H6": 22,
        "N1": 20, "N2": 18, "N3": 16, "N4": 14, "N5": 12,
        "N6": 10, "N7": 8, "N8": 6, "N9": 4,
    },
    "XS": {
        "T1": 100, "T2": 88, "T3": 72, "T4": 64, "T5": 56, "T6": 48,
        "H1": 40, "H2": 32, "H3": 28, "H4": 24, "H5": 22, "H6": 20,
        "N1": 18, "N2": 16, "N3": 14, "N4": 12, "N5": 10,
        "N6": 8, "N7": 6, "N8": 4, "N9": 3,
    },
    "XXS": {
        "T1": 96, "T2": 80, "T3": 64, "T4": 56, "T5": 48, "T6": 40,
        "H1": 32, "H2": 28, "H3": 24, "H4": 22, "H5": 20, "H6": 18,
        "N1": 16, "N2": 14, "N3": 12, "N4": 10, "N5": 8,
        "N6": 6, "N7": 4, "N8": 3, "N9": 2,
    },
    "XXXS": {
        "T1": 88, "T2": 72, "T3": 56, "T4": 48, "T5": 40, "T6": 32,
        "H1": 28, "H2": 24, "H3": 22, "H4": 20, "H5": 18, "H6": 16,
        "N1": 14, "N2": 12, "N3": 10, "N4": 8, "N5": 6,
        "N6": 4, "N7": 3, "N8": 2, "N9": 1,
    },
}
"""THE OWNER'S OWN 168 CELLS, verbatim, in the unit he wrote them in.

He authored this table on 2026-08-17 -- 8 rungs x (T6 + H6 + N9) -- and it
REPLACES the engine's ratio-derived defaults, which were a guess at a shape he
had not yet stated. Kept in his pixels rather than pre-divided so the numbers
stay checkable by eye against his message; `AUTHORED_TEXT_FACTORS` below is the
single division that turns them into what a brand actually stores.

THIS IS NOT THE GRID COMING BACK, and the difference is not cosmetic. The grid
was a GENERATED ladder that computed values snapped TO -- so a cell's size was
decided by whichever rung the base happened to land near. This is the authored
table itself: 168 independent factors, nothing generated, nothing snapped, and
every cell still `base x factor`. Double the base and all 168 double.

He wrote "120104" for XXL T2/T3; it is 120 and 104, and that reading is not a
guess -- it is the only split that satisfies both invariants the table obeys
(see `tests/design_engine/test_fixtures.py`, the invariant test).
"""

AUTHORED_BASE: float = 8.0
"""The base his table was authored against. It divides `AUTHORED_TEXT_PX` once
and is never used again -- a brand on any other base gets the same factors."""

AUTHORED_TEXT_FACTORS: dict[str, dict[str, float]] = {
    rung: {style: px / AUTHORED_BASE for style, px in row.items()}
    for rung, row in AUTHORED_TEXT_PX.items()
}
"""His table as FACTORS -- 16 -> 2.0, 22 -> 2.75, 3 -> 0.375.

Every one of the 30 distinct values is a dyadic fraction of 8, so the division
is exact in binary floating point and `factor x 8` returns his integer.
"""


ROLE_PICK_ORDER: tuple[str, ...] = (
    "container-size",
    "icon-size",
    "padding",
    "margin",
    "icon-size-smaller",
    "gap",
    "negative-adjustment",
)
"""The seven INDEPENDENT component roles, declared largest-first.

The note's other nine roles are not picks at all -- it states their arithmetic
inline on its own list: GAP-HALF "(*0.5)", GAP-DOUBLE "(*2)", MARGIN-DOUBLE
"(*2)", PADDING-DOUBLE "(*2)", PADDING-HALF "(*0.5)", CONTAINER-SIZE-DOUBLE,
ICON-SIZE-DOUBLE "(*2)", ICON-SUBTRACTION "(*-1)", ICON-SMALLER-SUBTRACTION
"(*-1)". Only these seven need a position on the ladder.
"""


class SizeFactors(_Strict):
    """THE FACTOR TABLE. Every size is `base x factor`; nothing is a pixel.

    Register rule 10, and his own question that produced it:

        "why is the grid even existing? shouldnt the sizes just be calculated?
        base * factor? 2 * 8 = 16?"

    P1 stored per-rung anchors as literal dictionaries. P3's first attempt made
    them positions on a generated grid ladder. Both are gone: the ladder was a
    Figma artefact and the note says so about itself, so there is no ladder to
    hold a position on and no value to snap back to.

    THE RULE, and TEXT and COMPONENTS answer it differently because he authored
    one of the two tables himself.

      1. TEXT is AUTHORED PER CELL. `text_factors` holds his own 168 factors --
         8 rungs x {T 6, H 6, N 9} -- and a cell is `base x text_factors[rung]
         [style]`. There is no anchor and no per-style ratio for text, because
         his table is not the product of one: T1 falls 136 -> 128 -> 120 while
         H1 falls 80 -> 72 -> 64 over the same rungs, which no single rung
         factor can produce. Inventing a ratio ladder that reproduces it only
         approximately is exactly the frozen-grid failure in a new costume.
      2. COMPONENTS are still DERIVED, because he authored no table for them.
         A component rung has a factor (`L` anchored at 5.0 = 40 px on base 8,
         neighbours a stated step apart) and a cell is `base x rung_factor x
         role_ratio` over the seven independent roles in `ROLE_PICK_ORDER`.

    Both stay one multiplication away from the base, which is the ruling, and
    both are per-brand authored slots the growth UI can move.

    THE COMPONENT numbers below are still the ENGINE's default, not his -- the
    note fixes their shape (9 rungs x 16 roles, every derived role a stated
    multiplier) and the numbers lived in Figma. The TEXT numbers are his.
    """

    text_factors: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            rung: dict(row) for rung, row in AUTHORED_TEXT_FACTORS.items()
        }
    )
    """HIS 168 cells, as factors of the brand's base. Editable per brand."""

    component_anchor_factor: float = 5.0
    """The `L` COMPONENT rung: 5.0 x base = 40 px on base 8."""

    component_rung_step: float = 0.75
    """Factor difference between neighbouring component rungs: 6.5 / 5.75 / 5.0
    / 4.25 / 3.5 / 2.75 / 2.0 / 1.25 / 0.5 -- 52 px down to 4 px."""

    role_ratios: dict[str, float] = Field(
        default_factory=lambda: {
            "container-size": 1.0,
            "icon-size": 0.5,
            "padding": 0.4,
            "margin": 0.4,
            "icon-size-smaller": 0.35,
            "gap": 0.25,
            "negative-adjustment": 0.2,
        }
    )
    """The seven independent roles as fractions of the container. At rung `L` on
    base 8: 40 / 20 / 16 / 16 / 14 / 10 / 8 px."""

    component_rung_factor: dict[str, float] | None = None
    """Per-brand override of the component rung factors. `None` means derive
    from the anchor and the step."""

    @model_validator(mode="after")
    def _factors_are_ladders(self) -> "SizeFactors":
        """Every factor table must descend and stay positive.

        A `gap` factored above `container-size`, an H3 larger than an H2, or an
        `M` rung above its `L` is not an override -- it is a broken ladder. Both
        directions of the text table are checked, because it is two-dimensional
        now: each band descends WITHIN a rung, and each style descends ACROSS
        the rungs. A table that failed either would still render; it would just
        no longer be a size system.
        """
        missing = set(ROLE_PICK_ORDER) - set(self.role_ratios)
        if missing:
            raise ValueError(f"role_ratios is missing {sorted(missing)}")
        roles = [self.role_ratios[role] for role in ROLE_PICK_ORDER]
        if roles != sorted(roles, reverse=True):
            raise ValueError(f"role_ratios does not descend: {roles}")
        if any(v <= 0 for v in roles):
            raise ValueError(f"role_ratios holds a non-positive ratio: {roles}")
        if self.component_rung_step <= 0:
            raise ValueError("a rung step is a positive factor difference")

        if set(self.text_factors) != set(TEXT_RUNGS):
            raise ValueError(
                f"text_factors must hold exactly the {len(TEXT_RUNGS)} text rungs; "
                f"got {sorted(self.text_factors)}"
            )
        for rung in TEXT_RUNGS:
            row = self.text_factors[rung]
            if set(row) != set(TEXT_STYLES):
                raise ValueError(
                    f"text_factors[{rung!r}] must hold exactly the "
                    f"{len(TEXT_STYLES)} styles; got {sorted(row)}"
                )
            for letter, _name, count in TEXT_BANDS:
                band = [row[f"{letter}{i}"] for i in range(1, count + 1)]
                if band != sorted(band, reverse=True):
                    raise ValueError(
                        f"text_factors[{rung!r}] band {letter} does not descend: {band}"
                    )
                if any(v <= 0 for v in band):
                    raise ValueError(
                        f"text_factors[{rung!r}] band {letter} holds a "
                        f"non-positive factor: {band}"
                    )
        for style in TEXT_STYLES:
            column = [self.text_factors[rung][style] for rung in TEXT_RUNGS]
            if column != sorted(column, reverse=True):
                raise ValueError(
                    f"text_factors column {style} does not descend across the "
                    f"rungs: {column}"
                )

        if self.component_rung_factor is not None and any(
            v <= 0 for v in self.component_rung_factor.values()
        ):
            raise ValueError("component_rung_factor holds a non-positive factor")
        values = [self.component_rung_factors()[name] for name in COMPONENT_RUNGS]
        if values != sorted(values, reverse=True) or values[-1] <= 0:
            raise ValueError(f"component rung factors do not descend: {values}")
        return self

    # -- text: his authored cell ---------------------------------------------

    def text_factor(self, rung: str, style: str) -> float:
        """The factor for one cell of his text table. A lookup, not a product."""
        rung = rung.upper()
        style = style.upper()
        row = self.text_factors.get(rung)
        if row is None:
            raise ValueError(f"unknown text rung {rung!r}; expected one of {TEXT_RUNGS}")
        if style not in row:
            raise ValueError(
                f"unknown text style {style!r}; expected T1-6, H1-6 or N1-9"
            )
        return row[style]

    # -- components: a rung factor times a role ratio ------------------------

    def component_rung_factors(self) -> dict[str, float]:
        if self.component_rung_factor is not None:
            return self.component_rung_factor
        pivot = COMPONENT_RUNGS.index(COMPONENT_ANCHOR_RUNG)
        return {
            name: self.component_anchor_factor + (pivot - i) * self.component_rung_step
            for i, name in enumerate(COMPONENT_RUNGS)
        }

    def component_factor(self, rung: str, role: str) -> float:
        """The factor for one INDEPENDENT component role. The other nine roles
        are the note's own multipliers of these."""
        return self.component_rung_factors()[rung.upper()] * self.role_ratios[role]


# ---------------------------------------------------------------------------
# The brand
# ---------------------------------------------------------------------------


def shade_of(canonical: str, amount: float, pole: str, neutrals: Neutrals) -> str:
    """The canonical colour, shaded by `amount` for use on a `pole` ground.

    A figure on a LIGHT ground has to get darker, so it walks toward the brand's
    OWN black; on a dark ground, toward its own white. Never a generic black or
    white -- "brands might use different blacks (030303) and whites (f4f4f4)".

    This is `growth.propose_adaptation`'s step with the amount CHOSEN instead of
    searched for. One mechanism, two entry points: the engine proposes the least
    amount that clears the threshold, and the builder then moves that amount.
    """
    toward = neutrals.black if pole == "light" else neutrals.white
    return c.composite(toward, amount, canonical)


class Brand(_Strict):
    """One authored brand. Everything else in the engine is a function of this."""

    id: str = "unnamed"
    signals: Signals = Signals()
    neutrals: Neutrals = Neutrals()
    brand: BrandColours = BrandColours(canonical="#d41bd1")
    font: FontSettings = FontSettings()
    weights: WeightAssignment = WeightAssignment()
    case: CaseAssignment = CaseAssignment()
    tracking: TrackingAssignment = TrackingAssignment()
    border: BorderSettings = BorderSettings()
    radius: RadiusSettings = RadiusSettings()
    effects: EffectSettings = EffectSettings()
    motion: MotionSettings = MotionSettings()
    shadow_anchors: ShadowAnchors = ShadowAnchors()
    sizes: SizeFactors = SizeFactors()
    defaults: Defaults = Defaults()

    # NO `base` AND NO `root_percent`. Both were authored slots until his ruling
    # of 2026-08-17 -- "Brands do not change their base. Base is always the
    # same. Brands change their colors and their other settings" -- and they are
    # now `scale.BASE` and `scale.ROOT_PERCENT`, system constants. A brand that
    # could move either would produce sizes incomparable with every other
    # brand's, and the emitted rem would stop being the factor. Validation is
    # strict, so an old payload carrying them is REJECTED rather than silently
    # ignored; `store.load` drops the two keys on the way in.

    @model_validator(mode="before")
    @classmethod
    def _materialise_shades(cls, data):
        """Turn shade AMOUNTS into the two adaptation colours, once, here.

        The authored record in the default mode is one colour and two amounts.
        Every reader in the engine wants a resolved hex, and there are six of
        them, so resolving in each would be six chances to diverge exactly where
        it matters -- at the threshold. This runs before `BrandColours` is built,
        so `on_light`/`on_dark` are already correct for the whole rest of the
        program and `adaptation()` stayed a one-line lookup.

        A shade that is absent changes NOTHING: an authored adaptation is kept
        verbatim, and a pole with neither a shade nor a colour collapses onto
        canonical the way it always did. So this is additive to every brand that
        existed before the ruling.
        """
        if not isinstance(data, dict):
            return data
        raw = data.get("brand")
        if not isinstance(raw, dict) or raw.get("two_colour_mode"):
            return data
        canonical = raw.get("canonical")
        if not isinstance(canonical, str):
            return data

        neutrals_raw = data.get("neutrals")
        neutrals = (
            Neutrals.model_validate(neutrals_raw)
            if isinstance(neutrals_raw, dict)
            else (neutrals_raw if isinstance(neutrals_raw, Neutrals) else Neutrals())
        )

        patched = dict(raw)
        for key, pole in (("shade_light", "light"), ("shade_dark", "dark")):
            amount = patched.get(key)
            if isinstance(amount, (int, float)):
                patched[f"on_{pole}"] = shade_of(canonical, float(amount), pole, neutrals)
        if patched != raw:
            data = {**data, "brand": patched}
        return data

    def surface(self, pole: str) -> str:
        """The brand's own neutral used as a GROUND of this polarity."""
        return self.neutrals.white if pole == "light" else self.neutrals.black

    def additional_colours(self) -> list[dict[str, str]]:
        """The brand's additional colours, each with its computed foreground.

        The foreground is the SAME calculation as everywhere else -- polarity by
        the one threshold, then the brand's own neutral of the opposite polarity.
        There is no second rule for a chart colour, which is the whole point of
        the ruling: "Each gets the SAME foreground calculation as everything
        else."
        """
        out: list[dict[str, str]] = []
        for name, entry in self.brand.additional_pairs():
            pole = c.polarity(entry.value)
            out.append(
                {
                    "name": name,
                    "label": entry.label,
                    "value": entry.value,
                    "polarity": pole,
                    "foreground": self.ink(pole),
                }
            )
        return out

    def ink(self, ground_pole: str) -> str:
        """The brand's own neutral used as a FIGURE on a ground of this polarity.

        A dark ground carries the brand's white, a light ground its black. This
        is the FOREGROUND exception -- an authored value selected by a computed
        polarity, never a computed value.
        """
        return self.surface(c.opposite(ground_pole))

    def is_neutral(self, colour: str | None) -> bool:
        """Is this colour one of the two the brand authored as its neutrals?

        The discriminator ruling 3 introduced:

            "As soon as there is a non-neutral color on the background, the
            nested brands fall back to neutral visuals with branded ctas"

        NEUTRAL means the brand's OWN black or white -- "as brands, might use
        different blacks (030303) and whites (f4f4f4)" -- so this is a value
        test against two authored slots, not a category. It reads the ground's
        colour and nothing else: not how the ground was reached, not
        `Ground.origin`, not a theme name. A signal ground, a brand-full ground
        and an arbitrary photo tint all answer False for the same reason and by
        the same comparison.

        A ground with no colour (an image, P0 q7) cannot be shown to be one of
        the two, so it answers False.
        """
        if colour is None:
            return False
        return _norm(colour) in {
            _norm(self.neutrals.black),
            _norm(self.neutrals.white),
        }

    def is_brand(self, colour: str | None) -> bool:
        """Is this colour one of the three the brand authored as ITS OWN colour?

        The mirror of `is_neutral`, and the discriminator the amended pressed
        rule needs (register rule 9 as amended 2026-08-19): a press EXCHANGES a
        component's background and foreground, and which way the exchange runs
        depends on which of the two currently holds the brand.

        A VALUE TEST, NOT A KIND BRANCH -- the same shape `is_neutral` and
        `Resolver.foreground` already establish as the legitimate form of this
        question. Nothing here asks what STYLE the component is or how the colour
        was reached; `.ds-solid-brand` is not named anywhere, and a hand-authored
        fill that happens to be the brand's own colour answers True for the same
        reason and by the same comparison. That is what keeps register rule 1
        (uniformity) intact through the amendment.

        ALL THREE SLOTS COUNT, canonical included. Canonical is what a brand at
        the root renders (`Resolver.root`), so a slot-by-slot test that admitted
        only the two adaptations would leave the root's own branded button out of
        the rule that exists for it.

        A brand whose adaptation IS one of its neutrals answers True to BOTH this
        and `is_neutral`. That is not a conflict to resolve -- it is his
        black/white-brand example, and the amended rule reads this test FIRST
        precisely so that case swaps instead of pressing to the colour it is
        already painted.
        """
        if colour is None:
            return False
        return _norm(colour) in {
            _norm(self.brand.canonical),
            _norm(self.brand.adaptation("light")),
            _norm(self.brand.adaptation("dark")),
        }


# ---------------------------------------------------------------------------
# Lint -- flags, never rejects
# ---------------------------------------------------------------------------


BRANDED_FIGURE_RENDERS: tuple[str, ...] = (
    "highlight_branded_background",
    "solid_brand",
    "border_branded",
)
"""Where a brand's adaptation is actually PAINTED on a canvas.

Every flag about `on_light` / `on_dark` names these, so the preview can mark the
exact components rendering the weak shade instead of only reporting the slot:

    "both configuration (something is not good with this value) and visibility
    (here is where it's failing)"

The engine owns this mapping because the engine owns the rule that puts the
adaptation there. A UI that hard-coded the same list would mark the wrong
element the first moment `branded_figure` changed.

WHEN A BRAND-LEVEL FLAG MAY CARRY `renders` AT ALL. Only when it is true
EVERYWHERE those slots are painted. A finding about the brand's adaptation
qualifies -- every branded figure in the system wears that shade. A finding
about ONE ground does not: the lint's `foreground.contrast-short` names a
specific ground in `where`, and broadcasting it would outline the foreground of
every node on the canvas, saying "here is where it's failing" about places where
it is not. Those get their visibility from the per-ground palette flags in
`resolve.py`, which fire on the ground they are about and nowhere else.
"""


def lint(brand: Brand) -> list[c.Flag]:
    """Everything questionable about an authored brand, as flags.

    Nothing here can fail a brand. Rejecting was the mistake he corrected:
    "Even if the contrast isn't high enough. It's brand decision."

    Every flag whose value has a visible rendering carries `renders` as well as
    `where`, so the advice reaches both surfaces from one source.
    """
    flags: list[c.Flag] = []
    b = brand.brand

    # AN AUTHORED SIGNAL FOREGROUND -- his ruling of 2026-08-18, "an override is
    # flagged-not-rejected with the contrast readout shown".
    #
    # It is a fact about the AUTHORED brand, which is why it is here and not in
    # `resolve.palette`: a signal's ink does not depend on what the signal sits
    # on, so a per-ground home would report one finding once per reachable ground.
    # `where` names the one role, so the rail shows it on that swatch and
    # `markFlags` places it on that ground and nowhere else.
    for role, background in brand.signals.by_role().items():
        authored = brand.signals.foreground_override(role)
        if authored is None:
            continue
        computed = brand.ink(c.polarity(background))
        if authored.lower() == computed.lower():
            continue
        chosen = c.wcag_contrast(authored, background)
        default = c.wcag_contrast(computed, background)
        flags.append(
            c.Flag(
                "signal.foreground-authored",
                f"{role} carries an authored ink: {authored} on {background} reads "
                f"{chosen:.2f}:1 where the computed {computed} reads "
                f"{default:.2f}:1. "
                + (
                    f"Under WCAG AA {c.WCAG_AA_TEXT}:1 — keep it only as a "
                    "deliberate brand decision."
                    if chosen < c.WCAG_AA_TEXT
                    else "Both clear WCAG AA, so this is a brand decision rather "
                    "than a defect."
                ),
                f"signals.{role}",
                (f"signal_{role}",),
            )
        )

    if b.on_light is not None and b.on_dark is not None and b.on_light == b.on_dark:
        flags.append(
            c.Flag(
                "brand.possibly-unadjusted",
                "on-light and on-dark hold the same colour. Legal -- a brand may "
                "verify one colour works on both grounds -- but identical values "
                "more often mean the adaptation has not been made yet.",
                "brand.on_light/on_dark",
                BRANDED_FIGURE_RENDERS,
            )
        )

    # "The question needs to be: does it work on dark?" -- an adaptation that
    # lands on the same side of the threshold as the ground it was made for has
    # not been adapted for it.
    for slot, pole in (("on_light", "light"), ("on_dark", "dark")):
        value = getattr(b, slot)
        if value is None:
            continue
        if c.polarity(value) == pole:
            flags.append(
                c.Flag(
                    "brand.adaptation-does-not-separate",
                    f"{slot} ({value}) is on the same side of {c.POLARITY_THRESHOLD} "
                    f"as a {pole} ground, so it will not stand on one. A dark grey "
                    "brand colour has to be lightened to work on dark.",
                    f"brand.{slot}",
                    BRANDED_FIGURE_RENDERS,
                )
            )

    # Ruling 1: a branded figure on one of the brand's own neutrals ALWAYS
    # renders the adaptation. Weak separation is reported here and nowhere else
    # -- "brand color can have two shades adjusted for on-dark and on-light",
    # and the adjustment is the mechanism. This flag is the whole remaining
    # consequence of a short figure contrast.
    for pole in ("light", "dark"):
        ground = brand.surface(pole)
        figure = b.adaptation(pole)
        if c.figure_contrast_short(figure, ground):
            flags.append(
                c.Flag(
                    "brand.figure-contrast-short",
                    f"the brand figure {figure} on the brand's own {pole} ground "
                    f"{ground} is {c.wcag_contrast(figure, ground):.2f}:1, under "
                    f"{c.WCAG_NON_TEXT_FLAG}:1. It still renders: the on-{pole} "
                    "shade is where a brand fixes this, not the engine.",
                    f"brand.on_{pole}",
                    BRANDED_FIGURE_RENDERS,
                )
            )

    if c.in_warn_band(b.canonical):
        flags.append(
            c.Flag(
                "brand.polarity-ambiguous",
                f"canonical ({b.canonical}) sits inside the study's warn band "
                f"{c.WARN_BAND} where the neutral choice is close to a coin flip. "
                "The engine will still decide; the decision is weakly supported.",
                "brand.canonical",
                # No renders: the canonical is painted as a GROUND (brand-full),
                # not as a figure, and it is one ground rather than all of them.
                # `ground.polarity-ambiguous` marks it where it actually appears.
            )
        )

    # THEME.FOREGROUND: his one non-computed value, and the one he asked to be
    # flagged. Checked against every ground the brand itself authors.
    # IT CHECKS THE RESOLVED INK, NOT THE COMPUTED ONE. A signal whose foreground
    # the brand authored (his ruling of 2026-08-18) is painted with the authored
    # value, so reading `brand.ink()` here would report a failure about a colour
    # the system no longer renders -- measured: overriding the error ink to white
    # took that ground from 3.82:1 to 5.40:1 and this flag still said must-fix at
    # 3.82. A brand that fixes a contrast by choosing the other neutral has fixed
    # it, and the verdict band has to agree.
    grounds = {
        "neutrals.white": (brand.neutrals.white, None),
        "neutrals.black": (brand.neutrals.black, None),
        "brand.canonical": (b.canonical, None),
        **{
            f"signals.{role}": (value, brand.signals.foreground_override(role))
            for role, value in brand.signals.by_role().items()
        },
    }
    for where, (ground, authored) in grounds.items():
        fg = authored or brand.ink(c.polarity(ground))
        ratio = c.wcag_contrast(fg, ground)
        if ratio < c.WCAG_AA_TEXT:
            flags.append(
                c.Flag(
                    "foreground.contrast-short",
                    f"foreground {fg} on {ground} is {ratio:.2f}:1, under WCAG AA "
                    f"{c.WCAG_AA_TEXT}:1. Flagged, not corrected.",
                    where,
                    # No renders: `where` names ONE ground. The per-ground
                    # `foreground.contrast-short` in `resolve.py` marks the
                    # foreground on that ground; broadcasting from here outlined
                    # every node's text over a failure on a signal ground.
                )
            )

    for slot in (1, 2, 3, 4):
        try:
            brand.font.numeric(slot)
        except ValueError as exc:
            flags.append(c.Flag("font.weight-unreadable", str(exc), f"font.slot_{slot}"))

    numbers = []
    for slot in (1, 2, 3, 4):
        try:
            numbers.append(brand.font.numeric(slot))
        except ValueError:
            numbers = []
            break
    if numbers and numbers != sorted(numbers, reverse=True):
        flags.append(
            c.Flag(
                "font.weights-not-monotone",
                f"slots 1..4 weigh {numbers}; slot 1 is meant to be the heaviest "
                "fatness range the family offers and slot 4 the lightest.",
                "font",
            )
        )

    return flags
