# SPDX-License-Identifier: Apache-2.0
"""Colour primitives for the design engine.

Everything the engine decides about colour bottoms out here. Two rules govern
this module and they come from the owner directly:

    "everything behaves exactly the same. it doesn't fucking matter what the
    background color is."

    "You obviously didn't understand that everything is relying on the
    background color."

Together they say: a ground is never asked *what kind* it is, only *what value*
it has. Every function below therefore takes a colour or a lightness and never
a category.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# The threshold
# ---------------------------------------------------------------------------

POLARITY_THRESHOLD = 0.675
"""OKLab L below which a ground is dark.

PROVENANCE, in the order it happened.

1. STUDY-FITTED. 0.675 is the OKLab lightness at which the owner's preference for
   near-white over near-black text flipped across 242 two-alternative trials,
   n = 1 observer, on flat chromatic rectangles (artifact c557b868, the
   instrument dissection; bootstrap 95% CI 0.645-0.680, and 12/12 on the
   independent authored-cell check).
2. The sandbox ruling of 2026-08-17 moved it to 0.5 on a live slider pass.
3. RE-RATIFIED AT 0.675 by the owner, 2026-08-19, after the signal-ink
   walkthrough: "Every foreground color (ink) needs to be calculated. so if
   white on red works better, so be it." Every ink judgement he has made --
   white on red, on pink, on blue; black on the success green -- comes out
   right at 0.675 and came out wrong at 0.5 on all but one. The 0.5 value is
   now the historical record.

The evidence that decided it: at 0.5 okuro's own #F03541 (L 0.626) is a light
ground and takes black ink, which is precisely the judgement his blind study
recorded as WHITE; the computed ink then failed WCAG AA on three of the four
signals. At 0.675 those four colours are dark grounds and take white ink.

It remains the system's SINGLE threshold, used for every "which neutral" and
"do these two separate" question -- polarity, foreground, ladder direction,
hover direction, brand-full eligibility. A second predicate is what the
uniformity principle forbids. The per-role signal-ink OVERRIDE (ruling
2026-08-18, `Signals.foregrounds`) is authored INPUT and stays available for
brand exceptions; it does not compete with this constant. WCAG 2.2 AA stays a
separate legal gate (see `wcag_contrast`).
"""

WARN_BAND = (0.582, 0.752)
"""The transition band in which the system flags rather than asserts.

MEASURED. The fitted logistic's 90%-to-10% transition from the 242-trial study,
centred on that fit's midpoint 0.667 (artifact c557b868). Restored with the
threshold on 2026-08-19; the provisional 0.415-0.585 band that accompanied the
0.5 ruling was the same measured width 0.170 recentred by hand, and it goes
with that ruling. Advice-only either way: nothing branches on it.

Note the band is symmetric about the logistic midpoint 0.667, not about the
shipped threshold 0.675 -- they are two estimators of the same study, and the
threshold sits inside its own band, 0.008 above the midpoint.
"""

WCAG_AA_TEXT = 4.5
"""The legal gate. Kept separate from the polarity threshold on purpose; a
flag, never a veto:
"Even if the contrast isn't high enough. It's brand decision." """

WCAG_NON_TEXT_FLAG = 3.0
"""A LINT REFERENCE, never a gate. WCAG 2.2 SC 1.4.11 Non-text Contrast.

P1 used 3:1 as a second predicate that DEGRADED a branded figure to a neutral
when the brand could not be told apart from its ground. The owner ruled that
mechanism wrong (2026-08-17, ruling 1):

    "Only one is the brand color. brand color can have two shades adjusted for
    on-dark and on-light."

The adaptation IS the contrast mechanism. A branded figure on one of the
brand's own neutrals renders `brand.on_{polarity}` unconditionally; a weak
result is reported and left alone, exactly as every other short contrast in
this system is:

    "Even if the contrast isn't high enough. It's brand decision."

So this number survives only as the threshold `figure_contrast_short` reports
against. Nothing in the engine branches on it.
"""


# ---------------------------------------------------------------------------
# sRGB <-> OKLab
# ---------------------------------------------------------------------------

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def parse_hex(value: str) -> tuple[float, float, float]:
    """'#F03541' -> (r, g, b) each in 0..1."""
    m = _HEX_RE.match(value.strip())
    if not m:
        raise ValueError(f"not a hex colour: {value!r}")
    digits = m.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return tuple(int(digits[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb)


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def _cbrt(x: float) -> float:
    return x ** (1 / 3) if x >= 0 else -((-x) ** (1 / 3))


def oklab(colour: str) -> tuple[float, float, float]:
    """sRGB hex -> OKLab (L, a, b). Ottosson's matrices."""
    r, g, b = (_srgb_to_linear(c) for c in parse_hex(colour))
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = _cbrt(l), _cbrt(m), _cbrt(s)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def lightness(colour: str) -> float:
    """OKLab L of a hex colour, 0..1."""
    return oklab(colour)[0]


# ---------------------------------------------------------------------------
# Polarity -- the one measurement the whole system runs on
# ---------------------------------------------------------------------------


def polarity(ground: str | float) -> str:
    """'dark' or 'light' for a ground.

    Accepts a hex colour OR a bare OKLab lightness. The float form is not a
    convenience: it is how a ground that is not a colour enters the system.
    The owner's answer on image grounds (P0 q7) is that the region an element
    covers is sampled, averaged to an OKLab L, and fed to *this same test* --
    engine-side where the image is known at config time, adapter-side where it
    is only known at runtime. One rule, three delivery paths; the signature
    accepts a lightness precisely so that no second code path exists.
    """
    l = ground if isinstance(ground, (int, float)) else lightness(ground)
    return "dark" if l < POLARITY_THRESHOLD else "light"


def opposite(pole: str) -> str:
    return "light" if pole == "dark" else "dark"


def separates(figure: str | float, ground: str | float) -> bool:
    """True when figure and ground land on opposite sides of the threshold.

    This single predicate answers three of his questions, which is why it is
    one function: may a branded element take the brand as its ground
    ("Let's go with 0.675 polarity test" -- his words, and after the 0.5
    sandbox detour the value is his again too), may a branded figure be
    rendered on this ground at all, and does `pressed` have "enough contrast to
    background on the mother element".
    """
    return polarity(figure) != polarity(ground)


def figure_contrast_short(figure: str, ground: str) -> bool:
    """True when a figure is hard to tell apart from the ground it sits on.

    Reporting only. This replaced `distinguishes`, which was a GATE: it used to
    degrade a branded figure to a neutral. Ruling 1 killed that mechanism --
    "brand color can have two shades adjusted for on-dark and on-light", and the
    adjustment is the contrast mechanism. Nothing may call this to decide a
    colour; the only legal caller is a lint.
    """
    return wcag_contrast(figure, ground) < WCAG_NON_TEXT_FLAG


def in_warn_band(ground: str | float) -> bool:
    l = ground if isinstance(ground, (int, float)) else lightness(ground)
    return WARN_BAND[0] <= l <= WARN_BAND[1]


# ---------------------------------------------------------------------------
# WCAG -- the separate legal gate
# ---------------------------------------------------------------------------


def relative_luminance(colour: str) -> float:
    r, g, b = (_srgb_to_linear(c) for c in parse_hex(colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def wcag_contrast(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------------------
# Compositing
# ---------------------------------------------------------------------------


def _composite_rgb(
    figure: str, alpha: float, ground: str
) -> tuple[float, float, float]:
    f = parse_hex(figure)
    g = parse_hex(ground)
    return tuple(f[i] * alpha + g[i] * (1 - alpha) for i in range(3))  # type: ignore[return-value]


def composite(figure: str, alpha: float, ground: str) -> str:
    """`figure` at `alpha` (0..1) painted over `ground`, in sRGB.

    sRGB because that is where a browser composites a translucent fill; the
    engine measures the *rendered* result, never the nominal one.
    """
    return to_hex(_composite_rgb(figure, alpha, ground))


def shift(ground: str, amount: float) -> str:
    """The owner's hover rule: "Dark colors = lighten up, light colors = darken
    down. By 20%".

    Implemented as `amount` of the opposing neutral composited over the ground,
    because that is the idiom the rest of the system already uses for every
    other "n% of a colour" (the 17-step ladder, separator, border-half). The
    direction is read from `polarity`, so this is the same one code path a
    ground of any kind travels.

    PROVISIONAL: he stated the magnitude and the direction, never the colour
    space or the mixing operator. See OPEN-H1 in the P1 report.
    """
    ink = "#ffffff" if polarity(ground) == "dark" else "#000000"
    return composite(ink, amount, ground)


# ---------------------------------------------------------------------------
# Shadows
# ---------------------------------------------------------------------------

SHADOW_ANCHOR_SOLID = 0.20
"""His words: "on white a 20% black color for a shadow is enough"."""

SHADOW_ANCHOR_BLURRED = 0.10
"""The note's BLUR-AND-SHADOW rung (ALTERNATE-NORMAL.010). He then ruled
blur-and-shadow "it's only the shadow color" -- so it is the same variable at
a second strength, not a second effect."""


def _lightness_rgb(rgb: tuple[float, float, float]) -> float:
    r, g, b = (_srgb_to_linear(c) for c in rgb)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    return 0.2104542553 * _cbrt(l) + 0.7936177850 * _cbrt(m) - 0.0040720468 * _cbrt(s)


def _darkening(ground: str, alpha: float) -> float:
    """How far a black veil at `alpha` drags the ground's OKLab L down.

    Measured at float precision rather than through an 8-bit hex round-trip:
    quantising here would smear the anchor across a 1/255 plateau and the
    solved opacity would no longer reproduce the number he authored.
    """
    return lightness(ground) - _lightness_rgb(_composite_rgb("#000000", alpha, ground))


def shadow_target(anchor: float) -> float:
    """The perceptual darkening an anchor buys on white -- the constant the
    table holds fixed on every other ground.

    The solid anchor yields 0.155, which is the figure his ratified table was
    built on ("let's keep it like that").
    """
    return _darkening("#ffffff", anchor)


def shadow_alpha(ground: str, anchor: float = SHADOW_ANCHOR_SOLID) -> float | None:
    """Black-veil opacity that reproduces the anchor's darkening on `ground`.

    Returns None when the ground is so dark that even a fully opaque black
    veil cannot reach the target -- "on dark it needs to be 100% to be
    visible", and below roughly L 0.15 not even that is enough. The owner's P0
    answer q4 for that case is one word: "border ok". The resolver turns the
    None into the border answer; this function only reports the physics.
    """
    target = shadow_target(anchor)
    if _darkening(ground, 1.0) < target:
        return None
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _darkening(ground, mid) < target:
            lo = mid
        else:
            hi = mid
    return hi


# ---------------------------------------------------------------------------
# Flags -- the engine reports, it never rejects
# ---------------------------------------------------------------------------


Severity = Literal["must-fix", "decide", "note"]

SEVERITY: dict[str, Severity] = {
    # MUST-FIX -- a legal failure, or a brand that cannot render at all.
    "foreground.contrast-short": "must-fix",
    "font.weight-unreadable": "must-fix",
    # DECIDE -- it will not stand, and in three of these the engine already
    # computed the exact fix. Never a veto: "Even if the contrast isn't high
    # enough. It's brand decision."
    "brand.adaptation-does-not-separate": "decide",
    "brand.figure-contrast-short": "decide",
    "branded.figure-contrast-short": "decide",
    "font.weights-not-monotone": "decide",
    # An AUTHORED signal foreground. It is `decide` whichever way the contrast
    # reads: the brand overrode a computed answer on purpose, so the page's job is
    # to show both ratios and let the author confirm -- never to rank it a defect
    # (rule 5, "weak contrast is a lint flag, NEVER degradation or rejection").
    "signal.foreground-authored": "decide",
    # NOTE -- legal, usually unfinished, or the system working as designed.
    "brand.possibly-unadjusted": "note",
    "brand.polarity-ambiguous": "note",
    "ground.polarity-ambiguous": "note",
    "shadow.became-border": "note",
}
"""How bad each finding is, ranked -- and the ENGINE owns the ranking.

WHY IT IS NOT IN THE UI. A page that assigned severity would be a second
opinion about the engine's own findings: it would have to know that WCAG AA on
text is a legal gate while 3:1 on a figure is a lint reference (`WCAG_AA_TEXT`
vs `WCAG_NON_TEXT_FLAG` above), that the warn band is advice nothing branches
on, and that a shadow becoming a border is the system working. All four facts
live in this module, so the ranking lives here too.

IT DECIDES NOTHING AND CHANGES NO RULE. Every flag was already produced,
already carried its message, and already reached both addresses. This orders
them so a page can say "is my configuration good" in one sentence instead of
printing a count. Nothing in the engine branches on a severity.

A code with no entry is a `note`: unranked advice is the weakest claim
available, and a new lint that forgot to rank itself must not be able to raise
a page's verdict to must-fix by omission.
"""


def severity_of(code: str) -> Severity:
    """The rank of one finding. Unknown codes are `note` -- see `SEVERITY`."""
    return SEVERITY.get(code, "note")


@dataclass(frozen=True)
class Flag:
    """A lint finding. Never fatal.

    "Even if the contrast isn't high enough. It's brand decision."

    TWO ADDRESSES, because he ruled a flag belongs in both places:

        "this belongs to the ui. both configuration (something is not good with
        this value) and visibility (here is where it's failing)"

    `where` is the CONFIGURATION address -- the authored slot in the rail whose
    value is questionable ("something is not good with this value").

    `renders` is the VISIBILITY address -- the render slots on the canvas where
    that value is actually painted, so the preview can mark the exact component
    that is failing rather than leaving a builder to hunt for it ("here is where
    it's failing"). The engine names them because only the engine knows which
    slot a value reaches; a UI that guessed would mark the wrong element the
    first time a rule changed.

    An empty `renders` is a real answer, not a gap: some flags -- an unreadable
    weight descriptor, a non-monotone weight quad -- are about the AUTHORED set
    and have no single element that shows them.
    """

    code: str
    message: str
    where: str = ""
    renders: tuple[str, ...] = ()

    @property
    def severity(self) -> Severity:
        """How bad this is: `must-fix`, `decide` or `note`.

        ADDITIVE, and deliberately a property rather than a constructor
        argument. Every call site keeps the signature it had, and no caller can
        pass a rank that disagrees with `SEVERITY` -- which is the only way a
        second opinion about the engine's findings could get in.
        """
        return severity_of(self.code)

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"[{self.code}] {self.where}: {self.message}" if self.where else f"[{self.code}] {self.message}"
