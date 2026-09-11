# SPDX-License-Identifier: Apache-2.0
"""Sizes: two tables, one rung shift, one switch.

    "This exists, so that in Figma a change of the size attribute of a
    component changes its size. This is inherited. So in Figma I can add the
    size M to a frame, and as long as the children have no sizing overrides,
    they consume the parents size configuration."

    "Exist, so that one switch downsizes everything based on their current
    mode. A component that has the size L is immediately changed to size XS,
    while a text of size S is changed to XS."

Two shifts, and they differ -- text moves one rung, components move three:

    "TEXT / DOWNSCALE: ... Sizes: mode=size-1 L=M"
    "COMPONENTS / DOWNSCALE: ... Sizes: mode=size-3, L=XS"

Every value is base x factor, on the SYSTEM base (`scale.BASE`, always 8 --
"Brands do not change their base"). Nothing here stores a pixel, and there is no
ladder to land on -- register rule 10, "THE GRID IS DEAD AS DATA".
"""

from __future__ import annotations

from dataclasses import dataclass

from . import scale
from .schema import COMPONENT_RUNGS, TEXT_RUNGS, TEXT_STYLES, Brand

TEXT_DOWNSCALE_SHIFT = 1
COMPONENT_DOWNSCALE_SHIFT = 3

COMPONENT_ROLES: tuple[str, ...] = (
    "container-size",
    "container-size-double",
    "gap-half",
    "gap",
    "gap-double",
    "margin",
    "margin-double",
    "padding-double",
    "padding",
    "padding-half",
    "icon-size",
    "icon-size-double",
    "icon-size-smaller",
    "negative-adjustment",
    "icon-subtraction",
    "icon-smaller-subtraction",
)
"""The note's 16 component roles, in his order and with his names."""

__all__ = [
    "COMPONENT_ROLES",
    "TEXT_STYLES",
    "Frame",
    "component_size",
    "component_table",
    "downscale_component",
    "downscale_text",
    "shift_rung",
    "text_size",
    "text_table",
]
"""`TEXT_STYLES` is re-exported from `schema`, where the band definition lives.
It is imported from here by everything that renders the table, and moving the
definition rather than duplicating it is what keeps T/H/N one vocabulary."""


def shift_rung(rungs: tuple[str, ...], rung: str, by: int) -> str:
    """Move `by` steps down the ladder, clamped at the floor.

    Clamped because the ladder ends: his own downscale rail runs XXS -> XXXXS,
    a two-step move at the bottom of a three-step shift. A shift past the floor
    is the smallest rung, never an error.
    """
    i = rungs.index(rung.upper())
    return rungs[min(i + by, len(rungs) - 1)]


def downscale_text(rung: str) -> str:
    """"mode=size-1 L=M"."""
    return shift_rung(TEXT_RUNGS, rung, TEXT_DOWNSCALE_SHIFT)


def downscale_component(rung: str) -> str:
    """"mode=size-3, L=XS"."""
    return shift_rung(COMPONENT_RUNGS, rung, COMPONENT_DOWNSCALE_SHIFT)


@dataclass(frozen=True)
class Frame:
    """A frame's size configuration, which children consume unless they override.

    "as long as the children have no sizing overrides, they consume the parents
    size configuration ... One drag of a component into another frame with
    sizing settings, changes the size of the component, the form, whatever."

    `scaled` is SCALES -- the one switch. It is carried on the frame rather
    than applied to the values, so that inheriting a frame and flipping the
    switch commute.
    """

    text: str = "L"
    component: str = "L"
    scaled: bool = False

    def inherit(self, text: str | None = None, component: str | None = None,
                scaled: bool | None = None) -> "Frame":
        """A child frame. Anything not overridden is consumed from the parent."""
        return Frame(
            text=text or self.text,
            component=component or self.component,
            scaled=self.scaled if scaled is None else scaled,
        )

    def effective_text(self) -> str:
        return downscale_text(self.text) if self.scaled else self.text

    def effective_component(self) -> str:
        return downscale_component(self.component) if self.scaled else self.component


def text_size(brand: Brand, rung: str, style: str) -> float:
    """One cell of the 8 x 21 text table, in px -- and it is base x factor.

    Ruling 4 corrected: "shouldnt the sizes just be calculated? base * factor?
    2 * 8 = 16?" `SizeFactors.text_factors` is HIS authored table and supplies
    the factor for a (rung, style); this multiplies by the SYSTEM base, which is
    the same 8 for every brand. A rung shift (downscale, SCALES) simply reads a
    different row.
    """
    return scale.size(scale.BASE, brand.sizes.text_factor(rung, style))


def text_table(brand: Brand, rung: str) -> dict[str, float]:
    return {style: text_size(brand, rung, style) for style in TEXT_STYLES}


def component_size(brand: Brand, rung: str, role: str) -> float:
    """One cell of the 9 x 16 component table, in px -- and it is base x factor.

    Ruling 4 corrected, same as `text_size`.

    The *-double, *-half and *-subtraction roles carry no factor of their own.
    The note states their arithmetic on its own list ("(*2)", "(*0.5)", "(*-1)"),
    so only the seven independent roles are authored.
    """
    g = lambda role: scale.size(  # noqa: E731
        scale.BASE, brand.sizes.component_factor(rung, role)
    )

    container = g("container-size")
    gap = g("gap")
    margin = g("margin")
    padding = g("padding")
    icon = g("icon-size")
    icon_smaller = g("icon-size-smaller")
    negative = g("negative-adjustment")

    table = {
        "container-size": container,
        "container-size-double": container * 2,
        "gap-half": gap * 0.5,
        "gap": gap,
        "gap-double": gap * 2,
        "margin": margin,
        "margin-double": margin * 2,
        "padding-double": padding * 2,
        "padding": padding,
        "padding-half": padding * 0.5,
        "icon-size": icon,
        "icon-size-double": icon * 2,
        "icon-size-smaller": icon_smaller,
        "negative-adjustment": -negative,
        "icon-subtraction": -icon,
        "icon-smaller-subtraction": -icon_smaller,
    }
    return table[role]


def component_table(brand: Brand, rung: str) -> dict[str, float]:
    return {role: component_size(brand, rung, role) for role in COMPONENT_ROLES}
