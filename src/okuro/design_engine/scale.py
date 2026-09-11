# SPDX-License-Identifier: Apache-2.0
"""Sizes are CALCULATED. value = base x factor. There is no grid.

    "why is the grid even existing? shouldnt the sizes just be calculated?
    base * factor? 2 * 8 = 16?"  (the owner, 2026-08-17 -- ruling 4 corrected)

This module replaces `grid.py`, which generated a 36-rung ladder and handed out
positions on it. THE GRID IS DEAD AS DATA. Register rule 10:

    "EVERY size is CALCULATED: value = base x factor (2 x 8 = 16); sub-base
    values are fractional factors. THE GRID IS DEAD AS DATA -- no stored or
    generated value list anywhere; font sizes, gaps, effect strengths, borders
    all compute."

The note said so about itself, and it was the one part of it that was never
architecture:

    "The grid is only in existance becasue Figma cannot calculate."
    "The whole negative part, is only there because Figma cannot calculate"

So the engine holds FACTORS, never values. A factor above 1 is a multiple of the
base; a factor below 1 is one of the fractions the note's own small end was made
of -- 1/16, 1/8, 1/4, 3/8, 1/2, 3/4. Every size in the system -- font size, gap,
padding, icon, border width, effect spread, radius, focus offset -- is one
multiplication away from the SYSTEM base, which is the same 8 for every brand.

And because the root is 50 % of the user's font size, that same factor is what
gets emitted: a rem IS a factor is a grid unit. See `BASE` and `ROOT_PERCENT`.

What this deliberately does NOT do: snap, round, or land a value back on
anything. A ladder to land on is the thing that died. `0.375 x 8 = 3` and
`0.35 x 6 = 2.1` are both simply the answer.
"""

from __future__ import annotations

BASE = 8.0
"""THE SYSTEM BASE. A CONSTANT, never a brand slot -- his ruling, 2026-08-17:

    "Brands do not change their base. Base is always the same. Brands change
    their colors and their other settings."

So `Brand` has no `base` field to author and no migration path back to one: a
brand that could move it would be a brand whose sizes are not comparable with
any other brand's. The RADIUS base is a different number and stays authored
(register rule 12, "radius-base 8") -- it divides radius rungs only."""

ROOT_PERCENT = 50.0
"""THE ROOT. `html { font-size: 50% }`, also a constant and also his ruling:

    rendered px = user-font-size x 50% x factor

A percentage rather than a pixel because a pixel silently overrides the user's
own font-size preference; a percentage scales with it. At the browser default
of 16 the root is 8px, so one grid unit is 8px; at 18 it is 9px and EVERY size
in the system moves with it, in lockstep and in one table."""

ROOT_PX = 16.0 * ROOT_PERCENT / 100.0
"""8.0 -- and it is BASE, which is the whole point of the 50 %.

    rem = px / ROOT_PX = (BASE x factor) / BASE = factor

So an emitted rem IS the factor: 16px is 2rem, 22px is 2.75rem, a half-pixel
sub-unit is 0.0625rem. The old "13.6rem means 136px" divide-by-ten convention
is retired with the 62.5 % root that produced it."""

assert ROOT_PX == BASE, "the 50 % root exists so that rem == factor"

MOBILE_MAX_PX = 767
"""The width below which a brand's MOBILE default rung applies.

PROVISIONAL. His ruling of 2026-08-18 gives a brand two default rungs, "for
desktop and for mobile", and does not say where one ends. 767 is the last pixel
below the 768 that every framework in the stack already treats as the tablet
boundary, so the sheet agrees with the app's own breakpoints rather than
inventing a third vocabulary.

It is a WIDTH and therefore px, not a factor: a media query is chrome, not a size
the system derives, and a rem here would be denominated in the 50 % root and fire
at half the intended width."""

MAX_SENTINEL = 999.0
"""RADIUS.MAX. A NAMED SENTINEL, not a size -- register rule 10, "MAX/999 =
named sentinel only". It is how a fixed Figma list spelled "as large as it
goes"; the emitter renders it as a pill radius and never multiplies it."""

# The fractions the note's own sub-base values were made of, kept as names so a
# factor table reads as intent rather than as arithmetic. Nothing iterates this;
# it is not a ladder, and no value list is generated from it.
SIXTEENTH = 1 / 16
EIGHTH = 1 / 8
QUARTER = 1 / 4
THREE_EIGHTHS = 3 / 8
HALF = 1 / 2
THREE_QUARTERS = 3 / 4


def size(base: float, factor: float) -> float:
    """The whole sizing engine. "base * factor? 2 * 8 = 16?"

    Kept as a named function rather than inlined so that every size in the
    system is greppable to one call, and so a reader can see there is no
    rounding step hiding behind it.

    `base` stays a parameter because the RADIUS base is authored and is not
    this one; system sizes pass `BASE` and radius rungs pass their own.
    """
    return base * factor


def rem(px: float) -> str:
    """px -> rem against the 50 % root -- and the result IS the factor.

        rendered px = user-font-size x 50% x factor   (his ruling, 2026-08-17)

    `ROOT_PX` is 8 and so is `BASE`, so this division cancels the multiplication
    `size` just did: 16px comes back as 2rem, 22px as 2.75rem, a half-pixel
    sub-unit as 0.0625rem. There is no divide-by-ten anywhere -- that convention
    retired with the 62.5 % root.

    It lives HERE, next to the two constants whose identity it depends on, rather
    than in whichever module needed it first: the emitter and the adapter both
    render sizes, and two spellings of one division is how two definitions start
    to drift.
    """
    value = px / ROOT_PX
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{text or '0'}rem"


def factor_of(base: float, value: float) -> float:
    """The inverse, for a UI that lets someone type a pixel and stores intent.

    The growth UI (P4) shows sizes in px because that is what a designer reads,
    but what a brand SAVES has to be the factor. With the system base fixed the
    two no longer diverge for system sizes -- they still do for the authored
    RADIUS base, and the factor is what the emitted rem is either way.
    """
    if base == 0:
        raise ValueError("a base cannot be zero")
    return value / base
