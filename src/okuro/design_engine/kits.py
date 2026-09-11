# SPDX-License-Identifier: Apache-2.0
"""Shipped kits: the authored brands the engine is delivered with.

A KIT is a `Brand` plus the little that surrounds it -- the font faces the
family really ships on the machine that will render it, and the flags a human
should see before trusting the sheet. Nothing here computes a colour; a kit is
input to `resolve`, never a result of it.

WHERE THE VALUES COME FROM, per slot. Three sources disagree in places and the
precedence is not uniform, so it is recorded per slot rather than as a rule:

    brand colour      his 2026-08-07 decision, #F03541. The note's #D41BD1
                      predates it.
    neutrals          the NOTE (#030303 / #FFFFFF). P0 q6: "the note wins over
                      the v1 kit's #0a0a0a".
    font family       the note, "JetBrains Mono". The v1 kit says "JetBrains
                      Mono Variable" and no variable font exists on this
                      machine -- eight static faces do.
    weight slots      the NOTE (ExtraBold / Bold / Regular / Light). Memory
                      2b31d581: "THE NOTE'S VALUES ARE EXACT, NOT EXAMPLES. The
                      v1 kit YAML (Light/Bold/Regular/ExtraLight) is WRONG for
                      okuro."
    weight assignment P0 q1, slots 1/1/2/3.
    signals           the V1 KIT, because those are the colours that ship today.
                      They differ from the note's on all three shared roles and
                      the difference is FLAGGED, never silently reconciled --
                      see `SIGNAL_DIVERGENCE`.
    warning signal    P0 q2: the fourth signal is WARNING and it is pink
                      ("I hate orange"). The v1 kit already carries the pink.
    motion            the v1 kit, including okuro's signature curve. A NEW brand
                      gets neutral motion; the signature is okuro's own.
    effects           the v1 kit's four intensities.
    geometry          radius base 8 (v1 kit). The SYSTEM base (8) and the root
                      (50 %) are NOT kit slots -- his ruling of 2026-08-17 made
                      both constants, so no kit authors them.

The v1 YAML is consumed as DATA and only as data: no module here imports
`okuro.design_systems`, and `test_kits.py` re-reads that file and asserts the
transcription below still matches it. If the frozen kit changes, the test says
so; if it disappears, the test says that too. Source stays free of I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import colour as c
from . import scale
from .schema import (
    Brand,
    BrandColours,
    BorderSettings,
    CaseAssignment,
    EffectIntensity,
    EffectSettings,
    FontSettings,
    MotionSettings,
    Neutrals,
    RadiusSettings,
    Signals,
    WeightAssignment,
    lint,
)

# ---------------------------------------------------------------------------
# Measured facts about this machine
# ---------------------------------------------------------------------------

JETBRAINS_MONO_FACES: dict[str, int] = {
    "Thin": 100,
    "ExtraLight": 200,
    "Light": 300,
    "Regular": 400,
    "Medium": 500,
    "SemiBold": 600,
    "Bold": 700,
    "ExtraBold": 800,
}
"""The eight upright faces JetBrains Mono ships here, with the OS/2
usWeightClass each file declares.

MEASURED, not assumed (`fc-list`, and each file's own table). This matters
twice. It is what P0 q5 says the numeric side must come from -- "both filtered
by the font's actually available values" -- and it contradicts the P2 brief,
which said a variable font was installed: there is none, so the sheet emits one
`@font-face` per static face.

There is no Black (900) or ExtraBlack (950) face, which is why okuro's slot 1 is
ExtraBold. The default assignment 1/1/2/3 therefore resolves to 800/800/700/400,
all of them real files.
"""

OKURO_FONT_STACK = (
    "ui-monospace, 'SF Mono', 'Cascadia Code', 'Roboto Mono', Menlo, "
    "Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace"
)
"""Verbatim from the v1 kit's `font-stack`."""


# ---------------------------------------------------------------------------
# The v1 kit's values, transcribed
# ---------------------------------------------------------------------------

V1_SIGNALS: dict[str, str] = {
    "red": "#f92f77",
    "green": "#11d425",
    "blue": "#2998fd",
    "pink": "#c52998",
}
"""What ships today, from `design_systems/data/kits/okuro-ds.yaml`."""

NOTE_SIGNALS: dict[str, str] = {
    "red": "#c8255f",
    "green": "#169723",
    "blue": "#0071d8",
}
"""What note OKURO-DS authors. Kept so the divergence can be stated exactly."""

V1_MOTION_SPEEDS: dict[str, int] = {
    "xfast": 150,
    "fast": 350,
    "medium": 650,
    "slow": 1200,
    "xslow": 1500,
}

V1_MOTION_CURVES: dict[str, str] = {
    "ease-in": "cubic-bezier(0.4, 0, 1, 1)",
    "ease-out": "cubic-bezier(0, 0, 0.2, 1)",
    "ease-in-out": "cubic-bezier(0.4, 0, 0.2, 1)",
    "gentle": "cubic-bezier(0.25, 0.1, 0.25, 1)",
    "quick": "cubic-bezier(0.55, 0, 0.1, 1)",
    "bouncy": "cubic-bezier(0.68, -0.55, 0.27, 1.55)",
}

OKURO_SIGNATURE_CURVE = "cubic-bezier(0.75, -0.1, 0.4, 1.0)"
"""okuro's own. A curve is a brand signature and cannot exist in Figma at all,
so this is purely his. A new brand gets the neutral curves and no signature."""

V1_EFFECT_INTENSITIES: dict[str, tuple[float, float, float, float]] = {
    # name: (shadow-x, shadow-y, shadow-spread, blur-amount), in PIXELS as the
    # v1 YAML authors them. The engine holds FACTORS (register rule 10), so the
    # kit divides by the base on the way in -- and `test_kits` divides the same
    # way on the way out, against the YAML.
    "light": (0.0, 4.0, 16.0, 16.0),
    "normal": (0.0, 4.0, 24.0, 32.0),
    "strong": (0.0, 4.0, 32.0, 64.0),
    # x-strong DIVERGES FROM THE v1 YAML, and it is the only slot here that
    # does. His ruling, 2026-09-06: the numbers are wrong, not the name.
    #
    # v1 authored (0.0, 0.0, 8.0, 64.0) -- no offset and a spread of 8 against
    # strong's 32. Emitted, that made `--shadow-xl` `0px 0px 8px`, SMALLER than
    # every rung below it including `xs`, while `adapter.SHADOW_INTENSITY`'s own
    # docstring promises "five monotone steps" and maps `xl` here.
    #
    # The shape (no offset, tight spread, wide blur) reads like a GLOW, which
    # would have made the NAME the error instead -- so it was put to him rather
    # than guessed. "glow" appears nowhere in this package; it was an auditor's
    # word, not authored intent.
    #
    # The values continue the ladder v1 itself authored: spread 16 / 24 / 32 ->
    # 40, blur 16 / 32 / 64 -> 128, offset staying at the 4.0 every other
    # intensity uses. `test_kits` states this divergence rather than asserting
    # parity, because a v1 value that shipped a defect is not an oracle.
    "x-strong": (0.0, 4.0, 40.0, 128.0),
}


def _effects(
    table: dict[str, tuple[float, float, float, float]], base: float
) -> EffectSettings:
    """px in, factors out."""

    def one(name: str) -> EffectIntensity:
        x, y, spread, blur = table[name]
        f = lambda v: scale.factor_of(base, v)  # noqa: E731
        return EffectIntensity(
            shadow_x=f(x), shadow_y=f(y), shadow_spread=f(spread), blur_amount=f(blur)
        )

    return EffectSettings(
        light=one("light"),
        normal=one("normal"),
        strong=one("strong"),
        x_strong=one("x-strong"),
    )


# ---------------------------------------------------------------------------
# A kit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Kit:
    """A shipped brand, with what a human needs to read alongside it.

    `notes` are DIVERGENCES a person must decide about -- places where two of
    his sources disagree and the kit had to pick one. They are not lint: the
    engine's own `lint()` reports what is questionable about the VALUES, while
    these report what is questionable about the PROVENANCE.
    """

    brand: Brand
    font_sources: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def flags(self) -> list[c.Flag]:
        """Everything the engine itself flags about this brand."""
        return lint(self.brand)


SIGNAL_DIVERGENCE = (
    "SIGNALS: the note and the v1 kit disagree on all three shared roles. "
    f"note red {NOTE_SIGNALS['red']} vs kit {V1_SIGNALS['red']}; "
    f"note green {NOTE_SIGNALS['green']} vs kit {V1_SIGNALS['green']}; "
    f"note blue {NOTE_SIGNALS['blue']} vs kit {V1_SIGNALS['blue']}. "
    "The kit's values ship today, so the kit wins here and the note does not. "
    "This is the opposite precedence from the neutrals (P0 q6) and the weight "
    "slots (memory 2b31d581), where the note wins -- so it is a decision for "
    "the owner, not a rule the engine can infer."
)

BRAND_ADAPTATION_NOTE = (
    "BRAND ADAPTATIONS: the canonical is authored; both poles are DERIVED from "
    "it by the shade rule, so changing okuro's colour changes them with it and "
    "nothing here has to be re-authored. A colour that stands on both neutrals "
    "unadjusted takes no shade; a light one takes the amount "
    "`growth.propose_adaptation` computes -- the least change that clears the "
    "threshold. That is the whole rule, and it is why the brand colour is a "
    "one-line decision rather than a migration."
)

CANONICAL_TAKES_WHITE_NOTE = (
    "INK ON THE BRAND'S OWN SURFACE IS COMPUTED, never authored: the ratified "
    f"threshold {c.POLARITY_THRESHOLD} decides the polarity and the brand's own "
    "neutral of the opposite polarity is the ink. The owner's 2026-08-19 ink "
    "ruling stands -- \"if white on red works better, so be it\" -- and it is a "
    "ruling about the RULE, so it survives a change of brand colour."
)


def okuro_ds() -> Kit:
    """okuro's own kit.

    The one brand okuro ships. Every slot's source is in this module's
    docstring; the flags it carries are real and deliberate.
    """
    brand = Brand(
        id="okuro-ds",
        # OKURO'S BRAND COLOUR. One authored value; everything downstream is
        # computed from it -- both adaptations, every ink, every ladder. The owner,
        # 2026-09-03: "I need to be able to chose whatever color i want to ship
        # with okuro as default and the system needs to calculate everything."
        #
        # A LIGHT COLOUR NEEDS A SHADE and this one is light: #A2FFE5 as a figure
        # on the brand's own white does not clear 3:1, so `shade_light` carries
        # the amount `growth.propose_adaptation` computed -- the least change
        # that clears the threshold. `on_light`/`on_dark` are NOT authored here:
        # Brand's validator materialises them from the shade, so the poles move
        # with the canonical instead of having to be kept in step by hand.
        # A DICT, NOT A BrandColours(). Brand's `model_validator(mode="before")`
        # is what turns a shade AMOUNT into the two adaptation colours, and it
        # guards on `isinstance(raw, dict)` -- so handing it an already-built
        # BrandColours skips materialisation silently and both poles come back
        # None. Measured here the hard way: the shade was set, the flag still
        # fired, and `on_light` was empty.
        brand={"canonical": "#A2FFE5", "shade_light": 0.39},
        # P0 q6: the note wins over the v1 kit's #0a0a0a.
        neutrals=Neutrals(black="#030303", white="#FFFFFF"),
        # the v1 kit's, because they are what ships. See SIGNAL_DIVERGENCE.
        signals=Signals(**V1_SIGNALS),
        font=FontSettings(
            family="JetBrains Mono",
            stack=OKURO_FONT_STACK,
            slot_1="ExtraBold",
            slot_2="Bold",
            slot_3="Regular",
            slot_4="Light",
            faces=JETBRAINS_MONO_FACES,
        ),
        weights=WeightAssignment(titles=1, headings=1, leads=2, paragraphs=3),
        # THE v1 KIT'S OWN CASE VALUES, spelled here for the same reason the
        # weight assignment is: this is the kit that carries what SHIPS, and a
        # slot left implicit is a slot whose provenance is one indirection away.
        # `data/kits/okuro-ds.yaml` authors `display-title: transform: uppercase`
        # and `title` / `lead` / `paragraph` as `none`; its four bands are the
        # engine's four in order. Register rule 12 admits that YAML as authored
        # DATA, which is the one thing it is read for.
        case=CaseAssignment(
            titles=True, headings=False, leads=False, paragraphs=False
        ),
        border=BorderSettings(),
        radius=RadiusSettings(base=8.0),
        effects=_effects(V1_EFFECT_INTENSITIES, scale.BASE),
        motion=MotionSettings(
            speeds=dict(V1_MOTION_SPEEDS),
            curves=dict(V1_MOTION_CURVES),
            signature=OKURO_SIGNATURE_CURVE,
            default_curve="signature",
            default_speed="medium",
        ),
    )
    return Kit(
        brand=brand,
        # No URLs: the family is installed, so `local()` resolves every face and
        # a shipped sheet carries no absolute machine path.
        font_sources={},
        notes=(SIGNAL_DIVERGENCE, BRAND_ADAPTATION_NOTE, CANONICAL_TAKES_WHITE_NOTE),
    )


KITS: dict[str, callable] = {"okuro-ds": okuro_ds}
"""Every shipped kit, by id. One today."""


def get(kit_id: str) -> Kit:
    try:
        return KITS[kit_id]()
    except KeyError:
        raise ValueError(
            f"unknown kit {kit_id!r}; shipped kits are {sorted(KITS)}"
        ) from None
