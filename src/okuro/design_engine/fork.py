# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: one recipe for "a new design system is okuro-ds plus a colour and a typeface".
# index:
#   fork_kit
# AGENT_HEADER_END -->
"""A new design system is okuro-ds with a small override set. That is the rule.

The owner, 2026-09-03, on registering a brand from a website and on the two
customer kits alike: *"based on okuro-ds. color, type and the small adjustments
… border radius, typography, color. keep it simple"* and *"rest same as base."*

WHY THIS IS NOT `growth.brand_from_essentials`. That function also turns a
typeface and a colour into a whole valid brand, and it is the right answer for
authoring a brand from nothing. It is the WRONG answer here, and the difference
was measured rather than assumed -- eight of ten blocks come out identical to
okuro-ds, and two do not:

    motion   grown has no signature curve and defaults to `ease-in-out`;
             okuro-ds carries cubic-bezier(0.75, -0.1, 0.4, 1.0) as `signature`.
    signals  grown derives its own red/green/blue from the brand colour;
             okuro-ds ships authored ones.

So a kit grown from essentials silently drops okuro's motion signature and
repaints the status colours. "Rest same as base" means the base's, which is what
this does.

THE TWO NON-OBVIOUS STEPS, both learned the expensive way:

1. `on_light` / `on_dark` are DROPPED before re-validation. `Brand`'s
   `model_validator(mode="before")` re-derives them from the shade amounts, so
   carrying the parent's materialised adaptations forward would leave a new
   canonical wearing okuro's red poles -- the "value moves, derived values do
   not" defect, hidden one level deeper than usual.

2. Font slots and faces come from the MEASURED family, never from the parent.
   The weight quad differs per typeface (JetBrains Mono is
   ExtraBold/Bold/Regular/Light, Be Vietnam Pro is Black/Bold/Regular/Light), so
   copying the parent's slots names faces the new family may not have.
"""

from __future__ import annotations

from typing import Any

from . import growth, kits
from .schema import Brand, lint

#: The kit every design system starts from.
BASE_KIT = "okuro-ds"

#: A sane sans fallback for a scraped or picked family. The parent's stack is a
#: MONOSPACE one, so inheriting it would put `ui-monospace` behind a sans face.
SANS_STACK = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "'Helvetica Neue', Arial, sans-serif"
)


def fork_kit(
    kit_id: str,
    *,
    colour: str | None = None,
    font_family: str | None = None,
    font_faces: dict[str, int] | None = None,
    font_quad: list[str] | None = None,
    font_stack: str | None = None,
    radius_base: float | None = None,
    border_base_px: float | None = None,
    base: str = BASE_KIT,
) -> Brand:
    """`base`, with a new id and at most a colour and a typeface changed.

    Every argument except `kit_id` is optional: a fork that overrides nothing is
    a faithful copy under a new name, which is the correct answer for "start
    from okuro-ds and edit it in the UI".

    `font_faces` and `font_quad` come from `api.installed_families()` for the
    target family. When they are absent the parent's font block is kept whole,
    because a family with no measured faces is one the engine cannot describe --
    better to keep a font that renders than to name slots that do not exist.

    A LIGHT BRAND COLOUR GETS A SHADE, AUTOMATICALLY. okuro-ds is a red that
    stands on both grounds unchanged, so it authors no shade and none is added
    here. A yellow or a lime does not: measured, #ffcc00 on the brand's own
    white is 1.51:1 and #bfff2f is 1.20:1, both under the 3.0:1 floor, and the
    engine's own words for that flag are "the on-light shade is where a brand
    fixes this, not the engine". A fork that skipped it would hand back a kit
    whose branded figures are invisible in light mode -- and a scraped brand
    colour is exactly the case nobody inspects first.

    The AMOUNT is not guessed: `growth.propose_adaptation` walks toward the
    brand's own black and stops at the first step that clears the threshold,
    which is the least change that works. The shade is applied only when the
    lint actually flags the pole, so a brand that does not need one does not
    get one.

    Raises ValueError from `Brand` validation if the result is not a valid
    brand; the caller decides whether that is a 400 or a crash.
    """
    data: dict[str, Any] = kits.get(base).brand.model_dump(mode="json")
    data["id"] = kit_id

    if colour:
        authored = dict(data["brand"])
        if not authored.get("two_colour_mode"):
            # See note 1 in the module docstring — and note that the SHADE
            # AMOUNTS go with the poles, not just the poles.
            #
            # A shade is an answer about ONE colour: 39% toward black is what a
            # mint needs and what a red does not. Carrying the base's amount
            # into a fork with a different canonical applies a correction to a
            # colour that never had the problem. Measured when okuro's own
            # brand colour changed: a #f03541 fork silently came back with the
            # mint's 0.39 and an on_light of #942229. Invisible until the base
            # itself gained a shade, which is exactly the kind of coupling that
            # waits.
            for derived in ("on_light", "on_dark", "shade_light", "shade_dark"):
                authored.pop(derived, None)
            authored["canonical"] = colour
            data["brand"] = authored

    if font_family:
        font = {**data["font"], "family": font_family}
        font["stack"] = font_stack or SANS_STACK
        if font_faces:
            font["faces"] = font_faces
        if font_quad and len(font_quad) >= 4:
            # See note 2 in the module docstring.
            font["slot_1"], font["slot_2"] = font_quad[0], font_quad[1]
            font["slot_3"], font["slot_4"] = font_quad[2], font_quad[3]
        data["font"] = font

    if radius_base and radius_base > 0:
        # THE CORNER IS ONE NUMBER AND THE LADDER IS A RULE. `base` is the only
        # authored radius; s/m/l are factors off it, so setting the base moves
        # the whole ladder and the nested-corner formula with it. A scan that
        # reported every rung separately would be authoring four numbers where
        # the system holds one.
        data["radius"] = {**data["radius"], "base": float(radius_base)}

    if border_base_px and border_base_px > 0:
        # Border weights are FACTORS of the same base, so a scanned pixel width
        # is converted rather than stored. 1px against a base of 8 is 0.125 —
        # which is exactly the factor okuro-ds authors, so a site with hairline
        # borders reproduces the base rather than fighting it.
        rung = float(radius_base or data["radius"]["base"])
        if rung > 0:
            data["border"] = {
                **data["border"],
                "weight_factors": [round(border_base_px / rung, 4)]
                + list(data["border"].get("weight_factors", [])[1:]),
            }

    brand = Brand.model_validate(data)
    return shade_for_own_colour(brand) if colour else brand


def shade_for_own_colour(brand: Brand) -> Brand:
    """A brand whose shade is computed FROM ITS OWN COLOUR, or left absent.

    The owner, 2026-09-06: *"color calculation can never be based on a
    calculation of another color."*

    THE ONE RECIPE FOR THAT RULE, so the two places a canonical can change --
    a fork, and the Settings accent picker -- cannot answer it differently.
    Both used to drop the poles and keep the AMOUNTS, which is the rule broken
    in the least visible way: a picked yellow came back re-derived through
    okuro's own 39 %, a number computed to lift a mint over the threshold.

    IT ONLY SHADES WHERE THE ENGINE SAYS ONE IS NEEDED. The lint decides, not
    this function -- a brand colour that already stands on white gets nothing,
    and the amount comes from `propose_adaptation`, which walks toward the
    brand's own black and stops at the first step that clears 0.675. The least
    change that works.

    TWO-COLOUR MODE IS RETURNED UNTOUCHED: there the poles are independently
    authored colours rather than a calculation, so there is nothing to re-derive
    and quietly moving one would be the guess this rule exists to forbid.
    """
    authored = brand.brand
    if authored.two_colour_mode:
        return brand
    if not any(f.where == "brand.on_light" for f in lint(brand)):
        return brand

    proposal = growth.propose_adaptation(authored.canonical, "light", brand.neutrals)
    data = brand.model_dump(mode="json")
    shaded = {
        k: v for k, v in data["brand"].items() if k not in ("on_light", "on_dark")
    }
    shaded["shade_light"] = proposal.amount
    return Brand.model_validate({**data, "brand": shaded})


__all__ = ["fork_kit", "shade_for_own_colour", "BASE_KIT", "SANS_STACK"]
