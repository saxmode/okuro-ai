# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: project a design_engine kit into the dict shape v0's consumers read, so the design slot can point at the engine.
# index:
#   design_system_block
#   visual_block
#   profile_view
# AGENT_HEADER_END -->
"""A kit, in the vocabulary its consumers already speak.

WHY THIS EXISTS, and what it is not. Five subsystems ask okuro for a design
profile -- the stack validator, the delivery theme chain, the slides generator,
prism, and bootstrap's design block. Every one of them reads v0's dict shape:
``design_system.accent_dark``, ``visual.palette.background.base``,
``visual.typography.primary``. Their QUESTIONS are legitimate; only the SOURCE
was wrong, because v0 was the only thing that could answer them.

So this is a projection, not a shim. Nothing here decides a colour, a size or a
rule -- every value is read back out of `resolve`, which is the same path the
emitted sheet takes. If a rule changes in the engine it changes here, because
there is no second copy of it.

    kit -> Resolver -> palette/ladder -> this projection -> the consumer's dict

THE SHAPE WAS MEASURED, NOT ASSUMED. Each field below was traced to the line
that reads it before it was written:

    design_system.foreground     stack/validator.py::_DESIGN_V2_FIELDS
    design_system.background     same
    design_system.accent_dark    same
    design_system.accent_light   same
    design_system.font           same
    design_system.white_ramp     same, and it wants >= 15 steps
    design_system.black_ramp     same
    design_system.chart          same
    visual.palette.accent        peer/delivery/theme.py:183
    visual.palette.background    peer/delivery/theme.py:184  (.base)
    visual.palette.foreground    peer/delivery/theme.py:185  (.primary, .tertiary)
    visual.palette.borders       peer/delivery/theme.py:210  (.default)
    visual.typography.primary    peer/delivery/theme.py:194
    visual.typography.fallback   peer/delivery/theme.py:195
    visual.layout.spacing        peer/delivery/theme.py:200

WHAT IT DELIBERATELY DOES NOT CARRY: ``language``. Tone, casing, locale and
terminology were in v0's design profile and are now the brand's ``voice`` slot
-- The owner, 2026-09-03: "language belongs to the brand not the design." A
design projection that still answered a language question would re-fuse exactly
what that split separated.

LIFETIME. This is the bridge that makes v0 deletable, not the end state. Each
consumer would be better off asking the engine in the engine's own terms --
delivery already builds its own token dict and could read `palette` directly,
and the validator's "is this profile v2-complete" question is meaningless for a
kit, which is complete by construction because the schema requires every block.
Those are cleanups. This is the fix.
"""

from __future__ import annotations

from typing import Any

from . import colour, store
from .adapter import ground_declarations
from .resolve import Resolver

#: The validator wants at least this many ramp steps. The engine's ladder is 17,
#: so the requirement is met with room -- asserted in tests rather than trusted.
_RAMP_MIN = 15

#: The appearance okuro RENDERS AT, pinned in `web/frontend/index.html` as
#: `<html data-appearance="dark">`. A projection describing "what okuro looks
#: like" has to describe the ground a person sees; the resolver's own default is
#: light, which is the ground behind the switch. Named rather than passed so
#: there is one place to change if the pin ever becomes a preference.
APP_APPEARANCE = "dark"


def _ramp(resolver: Resolver, ink: str, ground) -> list[str]:
    """One ink at every step of the engine's ladder, as an OPAQUE SURFACE SCALE.

    v0 stored `white_ramp` / `black_ramp` as flat lists of 15 authored values.
    The engine does not store a ramp at all -- it computes one, 17 steps of the
    same ink over the ground -- so the list is READ from the ladder rather than
    transcribed, and a brand that re-authors its ink moves the ramp with it.

    TWO THINGS HAD TO BE TRANSLATED, NOT JUST THE NAME, and skipping them made
    this projection answer the right question with the wrong quantity.

    1. OPAQUE, NOT TRANSLUCENT. A ladder step is an ink at an ALPHA over a
       ground -- `rgba(255, 255, 255, 0.97)`. v0's ramp is a list of solid
       colours. `colour.composite` flattens each step onto its own ground, which
       is what the step already renders as; nothing is invented.

    2. QUIET FIRST. The ladder runs loudest-to-quietest (100% down), v0's ramp
       runs base-upward: `#0a0a0a, #101010, #1a1a1a, …, #ffffff`. That order is
       not cosmetic -- prism's `_surfaces_from_ramp` takes indices 1, 2 and 3 as
       the ELEVATION steps, so a reversed ramp hands it near-white surfaces.

    MEASURED, on the deck theme, the moment okuro's brand pointed at a kit:
    `--bg-elev` came back `rgba(255, 255, 255, 0.97)` over a `#030303` base and
    every `--fg-*` collapsed onto `#ffffff`. Both are this one function.
    """
    scale = [
        colour.composite(step.ink, step.alpha, step.over)
        for step in reversed(resolver.ladder(ink, ground).steps)
    ]

    # A SCALE DOES NOT REPEAT ITSELF. The ladder's quietest steps composite back
    # onto the ground -- 2 % of white over #030303 is #030303 -- so the first
    # entries come out identical, and a consumer slicing indices 1..3 as its
    # elevation ramp gets two surfaces that are the base. Measured: prism's deck
    # theme reported three "broke elevation monotonicity" warnings and
    # re-derived all three. Collapsing the duplicates is the ramp saying what it
    # means; v0's authored ramp had no repeats either.
    out: list[str] = []
    for value in scale:
        if not out or value != out[-1]:
            out.append(value)

    # UNLESS COLLAPSING LEAVES NO SCALE AT ALL. Black ink over a near-black
    # ground is the ground at every step, so `black_ramp` on a dark appearance
    # deduplicates to ONE entry -- and `prism.theme._is_v2` requires five, so a
    # correct answer about a degenerate ink would have dropped the whole deck
    # onto the legacy formula. Measured: it did, and said so in a warning.
    #
    # The flat list is the honest answer there: it is what the ladder renders,
    # and the consumer that reads this ramp (`_surfaces_from_ramp`) only reaches
    # for `black_ramp` on a LIGHT ground, where the same ink is not degenerate.
    return out if len(out) >= _RAMP_MIN else scale


def design_system_block(resolver: Resolver, ground) -> dict[str, Any]:
    """The flat block the validator lints and prism's deck resolver reads."""
    brand = resolver.brand
    palette = resolver.palette(ground)
    return {
        "foreground": palette.foreground,
        "background": palette.background,
        # ONE COLOUR, TWO ADAPTATIONS -- the engine's own model. On a one-colour
        # brand both poles collapse onto canonical, which is correct rather than
        # a degenerate case: it is a brand that works on both grounds unchanged.
        "accent_dark": brand.brand.adaptation("dark"),
        "accent_light": brand.brand.adaptation("light"),
        "font": brand.font.family,
        "white_ramp": _ramp(resolver, brand.neutrals.white, ground),
        "black_ramp": _ramp(resolver, brand.neutrals.black, ground),
        "chart": list(palette.signal_backgrounds.values()),
    }


def visual_block(resolver: Resolver, ground) -> dict[str, Any]:
    """The nested block the delivery chain and bootstrap read.

    EVERY COLOUR COMES FROM `adapter.ground_declarations` — the same call that
    produces the emitted sheet's consumer names, and the one the 102-name
    contract test gates. There is no second derivation here.

    THIS BLOCK USED TO RE-DERIVE, and the re-derivation was both incomplete and
    the wrong TYPE. Measured 2026-09-05 against the v0 profile it replaces:

        background.elevated / .overlay / .scrim    ABSENT   (v0 had all three)
        foreground.disabled / .inverse             ABSENT
        borders.hover                              ABSENT
        accent_hover / accent_subtle               ABSENT
        status.*                                   ABSENT   (all four)
        background.subtle    rgba(255,255,255,.05) v0: #141414
        foreground.secondary rgba(255,255,255,.7)  v0: #b0b0b0
        borders.default      rgba(255,255,255,.1)  v0: #2a2a2a

    The adapter answers every one of those with a real hex off the same ground:
    `#0b0b0b`, `#b3b3b3`, `#353535`. WHAT THE GAPS COST, measured on the deck
    theme the moment okuro's brand pointed here: all five `--fg-*` tokens
    collapsed onto `#ffffff` (the whole text hierarchy gone), the surface stack
    became white-alpha on a near-black base, and eight `*-text` tokens failed
    their AA check and fell back to white. Zero warnings became eight.

    A projection that computes is a second authority. This one reads.
    """
    brand = resolver.brand
    palette = resolver.palette(ground)
    declared = ground_declarations(brand, palette)

    def _c(name: str) -> str | None:
        return declared.get(f"--color-{name}")

    def _group(prefix: str, *fields: str) -> dict[str, str]:
        """A v0 sub-block, filled only where the engine publishes the name --
        an absent key is honest; a key holding None is a colour that is not."""
        return {f: _c(f"{prefix}-{f}") for f in fields if _c(f"{prefix}-{f}")}

    return {
        "palette": {
            "accent": _c("accent"),
            "accent_hover": _c("accent-hover"),
            "accent_subtle": _c("accent-subtle"),
            "background": _group(
                "background", "base", "elevated", "subtle", "overlay", "scrim"
            ),
            "foreground": _group(
                "foreground", "primary", "secondary", "tertiary", "disabled", "inverse"
            ),
            "borders": _group("borders", "default", "subtle", "hover"),
            "status": _group("status", "success", "warning", "error", "info"),
        },
        "typography": {
            "primary": brand.font.family,
            "fallback": brand.font.stack,
        },
        "layout": {
            # The spacing rail in the engine's own factors. v0's rungs were
            # authored px; these are the factors the sheet emits, so a rung
            # here and a rung in the app cannot disagree.
            "spacing": {
                "sm": f"{brand.radius.base / 2:g}px",
                "md": f"{brand.radius.base:g}px",
                "lg": f"{brand.radius.base * 2:g}px",
                "xl": f"{brand.radius.base * 4:g}px",
            },
        },
    }


def profile_view(kit_id: str) -> dict[str, Any] | None:
    """A saved kit, shaped like the design profile its consumers expect.

    Returns None when the kit does not exist, so a brand slot pointing at a
    missing kit reports ``{missing: true}`` exactly as it did for a missing v0
    profile -- the UI's broken-slot state is unchanged.
    """
    try:
        brand = store.load(kit_id)
    except (FileNotFoundError, ValueError):
        return None

    resolver = Resolver(brand)
    # THE GROUND THE APP ACTUALLY RENDERS, not the resolver's default.
    #
    # `resolver.root()` defaults to LIGHT, and this projection took it. But
    # `index.html` pins `data-appearance="dark"` on the root element — every
    # user, every page — so the light ground is one nobody sees. Measured
    # 2026-09-05: this reported background #ffffff / foreground #030303 while
    # the app painted #030303 / #ffffff.
    #
    # It went unnoticed because the only consumers were still resolving through
    # v0, whose architecture-noir profile is authored dark. Repointing a design
    # slot at a kit would have flipped every deck, email and delivery theme from
    # dark to light — a visible change nobody asked for, arriving as a side
    # effect of a slot edit.
    #
    # A KIT IS NOT DARK OR LIGHT; it emits both and the appearance chooses. So
    # this is not an opinion about the brand, it is the projection answering the
    # question its consumers are really asking: what does okuro LOOK like.
    ground = resolver.root(APP_APPEARANCE)
    return {
        "id": brand.id,
        "name": brand.id,
        # A KIT HAS NO VERSION AND SHOULD NOT PRETEND TO. v0 profiles carried a
        # hand-bumped `version` string and bootstrap renders it, so the field is
        # answered rather than left to render as "v?" — but it is answered with
        # what is true: the kit's origin, which is the fact a reader of that
        # line actually wants.
        "version": store.list_kits() and next(
            (r["origin"] for r in store.list_kits() if r["id"] == brand.id),
            "user",
        ) or "user",
        "source": "design_engine",
        "design_system": design_system_block(resolver, ground),
        "visual": visual_block(resolver, ground),
    }


__all__ = ["design_system_block", "visual_block", "profile_view"]
