# SPDX-License-Identifier: Apache-2.0
"""The adapter: the consumer's vocabulary, refilled wherever `--ground` is published.

    "that's something that needs to be solved systematically"  (d8)

WHAT THIS IS. The engine sheet speaks the engine's vocabulary -- `.ds-surface`,
`.ds-alt-70`, `[data-emphasis]`. The app speaks Tailwind's and shadcn's --
`bg-background`, `text-muted-foreground`, `var(--color-fg-muted)`. Measured on
the live tree: 1,682 shadcn-semantic utility uses and 3,566 okuro-namespace uses,
5,248 call sites in 448 files. The adapter is what makes those call sites
ground-aware without editing one of them: for every rule the emitter writes that
publishes a ground, the adapter refills the CONSUMER names in THAT SAME RULE, as
resolved literals.

WHERE IT PLUGS IN, and why it is three places and not four. `--ground` is a
custom property, so it INHERITS: an element with no request keeps its parent's
ground and therefore must keep its parent's palette. The consumer names inherit
by the same mechanism, so they need writing in exactly the places `--ground` is
written -- `_root_rules`, `_boundary_rules`, `_portal_rules`. There is no blanket
`*` rule, and adding one would be a second propagation mechanism next to the one
CSS already provides.

WHY THE REFILL, and not a sheet of names per ground block. Because a style query
never matches the element that PUBLISHES the property -- it is evaluated against
the nearest ANCESTOR container. A boundary declares its new ground on itself but
still matches its PARENT's block, so a blanket fill inside that block hands the
boundary its parent's palette. Measured in Chromium 151 on the naive shape: 3 of
4 boundary elements carried the inverse of what they were painted, and every name
was PRESENT on all four. That is why the gates in
`tests/design_engine/test_adapter*.py` assert AGREEMENT between the name and the
pixel, never presence.

WHAT IT DOES NOT DO. It computes no colour. Every value below is either something
the resolver already decided, one of the derivations documented on its own entry,
or one of the ADAPTER EXTENSIONS -- app vocabulary with no engine concept
(charts, field widths, row heights, measures, type line/tracking), which are
authored HERE rather than pushed into `Brand`, because a design system that owns
`--max-width: 1280px` is a design system that has stopped being one.
"""

from __future__ import annotations

from . import colour as c
from . import scale, schema, sizing
from .resolve import Palette
from .schema import COMPONENT_ANCHOR_RUNG, Brand

# ---------------------------------------------------------------------------
# The extensions: app vocabulary with no engine concept
# ---------------------------------------------------------------------------

FIELD_WIDTHS: dict[str, str] = {
    "xs": "160px",
    "sm": "240px",
    "md": "360px",
    "lg": "100%",
}
"""Form-field width tiers. LAYOUT, not a design system: they answer "how wide is
a field that does not need the column", which no rule in the register speaks to.

Kept in px on purpose. px is root-immune, so these four do not move when the root
does -- which is the whole reason the 7,319 px-denominated spacing occurrences in
the app need no compensation either."""

ROW_HEIGHTS: dict[str, str] = {
    "dense": "32px",
    "default": "44px",
    "spacious": "56px",
}
"""List/table row heights. Arguably `container-size` rungs -- but the engine's
nine rungs do not align with the app's three, and bending one onto the other
would invent a mapping nobody authored. px, for the reason above."""

MAX_WIDTH = "1280px"
"""The page shell. A layout decision of one app."""

PROSE_MAX = "72ch"
"""The measure. `ch` is font-relative and therefore already correct under any
root: it is a count of characters, not a length."""

SCRIM_ANCHOR = 0.60
"""The scrim's darkening anchor, in the SHAPE of his two shadow anchors.

Register rule 8 governs the family a scrim belongs to -- "SHADOWS. Always black;
only opacity varies with the ground, through the two authored anchors (20% solid
/ 10% blurred on white)". A scrim is that same black veil at page scale, so it
gets the same treatment and a third anchor rather than a hand-picked alpha per
theme: `c.shadow_alpha` reproduces this darkening on whatever ground it lands on.

0.60 is the app's own current scrim alpha on its near-black surface, carried over
so the adapter does not change how a modal reads while it changes where the value
comes from. AUTHORED, not measured -- it is an extension, and it is listed as one."""



def line_for(role: str) -> str:
    """The line height of an app type role, out of the engine's own table.

    `TYPE_ROLES` already says which engine cell each role reads; this follows the
    same mapping for the line box, so a role cannot end up with one table's size
    and another table's leading.
    """
    style, _weight_role = TYPE_ROLES[role]
    return f"{schema.line_height(style):g}"

TYPE_ROLES: dict[str, tuple[str, str]] = {
    # app role -> (engine text style, engine weight role)
    "display": ("H6", "titles"),
    "title": ("N1", "titles"),
    "subtitle": ("N3", "headings"),
    "lead": ("N4", "leads"),
    "body": ("N5", "paragraphs"),
    "small": ("N6", "paragraphs"),
    "label": ("N7", "leads"),
}
"""Which engine cell each app type role reads, FITTED against the parsed oracle.

Every size is EXACT at rung L against what the app renders today: display 28 =
H6, title 24 = N1, subtitle 20 = N3, lead 18 = N4, body 16 = N5, small 14 = N6,
label 12 = N7. Nothing here was authored from the names -- `bridge.py`'s `--type-*`
table was authored from the names and came out wrong by up to 2x (artifact
375d6ba3); it was repaired by fitting against a parsed, browser-confirmed oracle,
and that is the method used here.

NOTE THE NAME COLLISION, because it is a real one and hiding it would make the
next reader "fix" the table: the app's `title` is the engine's `N1` -- band N,
"normal" -- not the engine's `T` band. The app's t-shirt scale simply lives in
the engine's normal band; the words do not line up and the pixels do.

The WEIGHT half is not a fit and cannot be: the app's roles carry 400-600 while
the brand's authored slots carry 400-800 (register rule 11, slots 1/1/2/3 =
ExtraBold/ExtraBold/Bold/Regular on JetBrains Mono). The brand's authored
assignment wins -- that is what authoring it means -- so app type gets heavier.
Intended, listed, and reversible by the brand's own weight assignment."""

FONT_SIZE_STEPS: dict[str, str] = {
    # app t-shirt name -> engine text style, fitted at rung L
    "3xs": "N9",
    "2xs": "N8",
    "xs": "N7",
    "base": "N5",
    "sm": "N6",
    "lg": "N4",
    "xl": "N3",
    "2xl": "N1",
    "display": "H6",
}
"""The nine LIVE `--font-size-*` names, fitted the same way.

Exact at rung L for eight of nine: 2xs 10 = N8, xs 12 = N7, sm 14 = N6,
base 16 = N5, lg 18 = N4, xl 20 = N3, 2xl 24 = N1, display 28 = H6. ONE
RESIDUAL, reported rather than smoothed: `--font-size-3xs` is 9px today and 9 is
not a cell in his 168 -- N9 is 8 and N8 is 10, so the name moves 9 -> 8px, by one
pixel, on the smaller side.

`xs` WAS ABSENT AND IS THE ONE THAT MATTERED, which is why it is called out here
rather than quietly added. The old note said it was DEAD -- emitted by
`/tokens.css` and read by nothing. That was true of the ENGINE's name and false of
the consequence: `globals.css` could not bind `--text-xs` to a name that did not
exist, so it kept a bare `1.5rem` literal, and `text-xs` is the most-used size in
the kit (24 sites, 15 files, `badge` among them). A build-time literal cannot
follow the rung, so those 24 sites were frozen at 12px at every rung while their
neighbours moved -- one of the four faces of the owner's "components do not react to
the chosen rung".

The binding is his ruling, not a choice made here: bind the utility to whichever
authored cell already matches. `text-xs` renders 12px at rung L and
`AUTHORED_TEXT_PX["L"]["N7"]` is 12, so the fit is exact and the default rung
renders byte-identically. No size category was added to the mother table.

`--font-size-3xl` stays absent: still emitted by `/tokens.css`, still read by
nothing. The adapter owes a name nothing until something reads it."""

SPACING_FACTORS: dict[str, float] = {
    "xs": 0.5,
    "sm": 1.0,
    "md": 2.0,
    "lg": 3.0,
    "lg-plus": 4.0,
    "xl": 5.0,
    "xl-plus": 6.0,
    "xxl": 8.0,
}
"""The app's 8-step spacing scale as FACTORS of the system base.

The engine holds a 2-dimensional 9-rung x 16-role table and the app names a
1-dimensional 8-step scale, so something has to flatten -- and the honest
flattening is register rule 10 itself, `value = base x factor`. On base 8 these
factors reproduce the app's live scale exactly: 4, 8, 16, 24, 32, 40, 48, 64.

This is what keeps `--spacing` safe. globals.css binds Tailwind's whole dynamic
scale to `var(--spacing-xs)`, so `p-4` is `calc(var(--spacing-xs) * 4)`. Emitted
as 0.5rem against an 8px root that is 4px -- the same 4px it is today -- so all
7,319 px-denominated spacing occurrences keep their size AND start following the
reader's own font size, which is what the 50 % root is for.

`--spacing-md-plus` is absent: dead, like the two font sizes."""

UTILITY_WEIGHTS: dict[str, int] = {
    "light": 300,
    "normal": 400,
    "medium": 500,
    "semibold": 600,
    "bold": 700,
}
"""The framework's weight scale, and the canonical number each rung means.

NOT THE BRAND'S VOCABULARY -- the brand authors four BANDS (titles, headings,
leads, paragraphs) and picks a real face for each. This is the other side of the
seam: five names a Tailwind consumer writes (`font-medium`, `font-bold`, ...),
which until his 2026-09-07 ruling reached no brand at all. Measured across the
frontend: 473 sites, every one of them rendering a framework literal.

THE WINDOW EACH RUNG OWNS is the open interval between its neighbours, read off
this ladder rather than invented as a tolerance -- 500 IS medium, so a 500 face
does not also answer `semibold`. The two ends are open-ended: anything below 400
is light, anything above 600 is bold.
"""

BORDER_WEIGHTS: tuple[str, str, str, str] = ("hair", "thin", "medium", "thick")
"""Names for `BorderSettings.weight_factors`, in its own order.

The schema authors four factors and no names -- on base 8 they are his 1 / 2 /
4 / 8 px. Numbering them `1..4` would have read as pixels, which is the one
thing a factor is not; these say thickness and stay true whatever the base."""

MEDIUM_WEIGHT = 500
BOLD_WEIGHT = 700
"""The two weights `--font-weight-semibold` sits between, and the reason the name
exists at all. Not a tolerance -- the open interval between them IS the gap the
slot was created to fill. See `_semibold`."""

SHADOW_INTENSITY: dict[str, str] = {
    "xs": "light",
    "sm": "light",
    "md": "normal",
    "lg": "strong",
    "xl": "x-strong",
}
"""Five app elevation levels onto the four authored engine intensities.

The app wants five monotone steps; `EffectSettings` authors four (register rule
12). Dropping one is the recommendation the engine research made and this is
where it lands: `xs` and `sm` share `light`. Authoring a fifth intensity would be
adding to his authored input to satisfy a consumer's naming, which is backwards.

C6 IS NOT SOLVED HERE AND CANNOT BE. On a ground where a black veil cannot
separate, the engine's answer is a BORDER, not a shadow -- and a `--shadow-*`
string has no way to say "border". Those grounds get `none` on all five names and
the separation comes from `.ds-elevated`, which carries the border answer. A
consumer that reaches for `shadow-md` on such a ground gets nothing, correctly,
rather than a veil that does not show."""

MOTION_STEPS: tuple[str, ...] = ("xfast", "fast", "medium", "slow", "xslow")
"""`--motion-1..5` in the order the brand declares its speeds.

Fitted against the oracle where they agree and NOT smoothed where they do not:
`--motion-fast` 350 and `--motion-medium` 650 match the brand's authored speeds
exactly; `--motion-slow` moves 850 -> 1200 and `--motion-5` moves 1200 -> 1500,
because register rule 12 authors 150/350/650/1200/1500 and the app's 850 was
never one of his numbers."""

CHART_LADDER_STEPS: tuple[float, ...] = (100.0, 70.0, 50.0, 30.0, 20.0, 10.0)
"""The engine-neutral chart ramp: six rungs of the ALTERNATE ladder.

THE SEAM. The owner authored a new section on 2026-08-17 -- BRAND ADDITIONAL
COLOURS, up to 10, add/remove, each getting the same foreground calculation as
everything else (memory fcee064f) -- and said it is meant for exactly this, among
other things: "presentation slides, charts". When `Brand.brand` grows that field
the chart slots read it and this ladder stops being used; `_chart_colours` is the
one function that changes, and it already branches on presence rather than on a
version.

Until then a chart must not invent hues. The engine computes nothing chromatic:
it has four authored signals with fixed meanings and one brand colour, and a
categorical ramp is a different problem (n mutually-distinguishable hues that
must also separate from an arbitrary ground). So the fallback is MONOCHROME --
six rungs of the ground's own foreground ladder, distinguishable by lightness,
correct on every ground by construction, and visibly a placeholder. A grey chart
says "nobody has authored these yet". Six invented hues would not."""


# ---------------------------------------------------------------------------
# Small renderers
# ---------------------------------------------------------------------------


def _rgba(colour: str, alpha: float) -> str:
    r, g, b = (round(x * 255) for x in c.parse_hex(colour))
    return f"rgba({r}, {g}, {b}, {alpha:g})"


def _radius(brand: Brand, rung: str) -> str:
    outer = brand.radius.outer(rung)
    return "9999px" if outer >= scale.MAX_SENTINEL else scale.rem(outer)


def _utility_weights(brand: Brand) -> dict[str, int]:
    """The framework's weight rungs a brand can actually SPELL, and no others.

    His ruling, 2026-09-07: bind only real faces. "A utility binds when the
    family ships a face for it, otherwise keeps Tailwind's literal."

    WHY NOT BIND ALL FIVE. Measured on his own kit, whose family is Unifont and
    ships exactly one face: binding the scale would publish 400 for every rung
    and flatten the app in a single commit. That is the cost that made his
    2026-08-11 deferral correct, and it is not paid here -- on a single-weight
    family exactly one rung binds and the other four keep the framework literal
    they already had.

    WHY NOT LEAVE IT UNBOUND. `.font-semibold` published 400 while `.font-medium`
    fell to the framework's 500, so semibold rendered LIGHTER than medium across
    63 sites. A rung that binds to the wrong face is worse than one that does not
    bind at all -- the utility's own fallback was already right.

    EACH RUNG PICKS THE NEAREST FACE INSIDE ITS OWN WINDOW, not the nearest face
    overall. The difference is not cosmetic: a family shipping 100 and 400 has a
    genuine light face, and "nearest to 300" would answer 400 and then reject it
    for being out of range, publishing nothing where a real Light exists.

    NO MEASURED FACES AT ALL -> the brand's authored band slots, which are real by
    construction. `offered_weights()` returns nothing rather than inventing, so
    an authored number is the only honest one left.
    """
    offered = [value for _name, value in brand.font.offered_weights()]
    if not offered:
        offered = sorted(
            {
                brand.font.numeric(getattr(brand.weights, band))
                for band in schema.TYPE_BANDS
            }
        )

    rungs = sorted(UTILITY_WEIGHTS.values())
    out: dict[str, int] = {}
    for name, canonical in UTILITY_WEIGHTS.items():
        i = rungs.index(canonical)
        low = rungs[i - 1] if i else float("-inf")
        high = rungs[i + 1] if i + 1 < len(rungs) else float("inf")
        inside = [w for w in offered if low < w < high]
        if inside:
            out[name] = min(inside, key=lambda w: abs(w - canonical))
    return out


def _chart_colours(brand: Brand, palette: Palette) -> list[str]:
    """Six categorical colours: the brand's additional colours when it has them.

    The seam described on `CHART_LADDER_STEPS`. `additional` is read with
    `getattr` because the field does not exist yet -- his ruling is a day old and
    the schema lands with the page work. When it arrives this needs no edit.
    """
    additional = list(getattr(brand.brand, "additional", None) or ())
    if additional:
        return [additional[i % len(additional)] for i in range(6)]
    return [_step(palette, percent) for percent in CHART_LADDER_STEPS]


def _step(palette: Palette, percent: float) -> str:
    """One rung of the alternate ladder as the composited literal it renders as."""
    over = palette.alternate.at(percent).over
    assert over is not None, "an image ground has no literal palette (P0 q7)"
    return over


def _inverse_step(palette: Palette, percent: float) -> str:
    over = palette.alternate_inverse.at(percent).over
    assert over is not None, "an image ground has no literal palette (P0 q7)"
    return over


def _veil(ground_colour: str, anchor: float) -> str:
    """A black veil at the opacity that reproduces `anchor`'s darkening here.

    `c.shadow_alpha` returns None when the ground is so dark that even a fully
    opaque veil cannot reach the target -- the case whose answer for a SHADOW is
    "border ok". A scrim has no border option (it is the page behind a modal, not
    an edge), so the honest answer there is the opaque maximum: it is the darkest
    a black veil can be, which is exactly what the None means.
    """
    alpha = c.shadow_alpha(ground_colour, anchor)
    return _rgba("#000000", 1.0 if alpha is None else round(alpha, 4))


# ---------------------------------------------------------------------------
# The ground-dependent half
# ---------------------------------------------------------------------------


def ground_declarations(brand: Brand, palette: Palette) -> dict[str, str]:
    """Every consumer name whose value depends on the ground, as literals.

    This is what gets merged into the three rules that publish `--ground`. It is
    COMPLETE per boundary for the same reason `_palette_rules` is: a rule that
    fills only the names a page happens to use leaks the parent's values across
    the boundary the moment a component reaches for one that is missing.
    """
    ground = palette.background
    assert ground is not None, "an image ground has no literal palette (P0 q7)"

    fg = palette.foreground
    accent = palette.highlight_branded_background
    accent_fg = palette.highlight_branded_foreground

    # Two ladder rungs whose derivation is worth naming. ELEVATED moves AWAY from
    # the foreground (alternate-inverse) and SUBTLE moves TOWARD it (alternate):
    # one rule, and the outcomes differ because the grounds do. On white that
    # makes elevated white -- correct, and the reason elevation on a light ground
    # is carried by a shadow rather than a tint (register rule 8). On black it
    # lifts. No branch on the ground anywhere.
    elevated = _inverse_step(palette, 5.0)
    subtle = _step(palette, 3.0)

    # The three quiet text levels are rungs of the alternate ladder, one choice
    # for every ground. `disabled` sits on 40 because 40 % IS his disabled
    # opacity (`resolve.DISABLED_OPACITY`), so the named colour and the opacity
    # utility agree instead of drifting.
    secondary = _step(palette, 70.0)
    tertiary = _step(palette, 50.0)
    disabled = _step(palette, 40.0)

    signals = palette.signal_backgrounds
    signal_fgs = palette.signal_foregrounds
    charts = _chart_colours(brand, palette)

    out: dict[str, str] = {
        # -- tier U: what /tokens.css supplies and the app reads --------------
        "--color-background-base": ground,
        "--color-background-elevated": elevated,
        "--color-background-subtle": subtle,
        # The ground's own colour, nearly opaque: an overlay is the surface
        # showing a hint of what it covers, so it is the same colour and not a
        # different one.
        "--color-background-overlay": _rgba(ground, 0.95),
        "--color-background-scrim": _veil(ground, SCRIM_ANCHOR),
        "--color-foreground-primary": fg,
        "--color-foreground-secondary": secondary,
        "--color-foreground-tertiary": tertiary,
        "--color-foreground-disabled": disabled,
        "--color-foreground-inverse": palette.highlight_neutral_foreground,
        # THE EMPHASIS FLIP, UNDER ITS OWN NAME. The pair is the note's
        # HIGHLIGHT-NEUTRAL-BACKGROUND / -FOREGROUND -- "the inverse of
        # BACKGROUND" / "the inverse of FOREGROUND" -- i.e. the next level in,
        # reached without a request: on a light ground a black surface carrying
        # white. The resolver has computed it since the palette existed and the
        # `/design-engine` tables already read it; only the CSS name was missing.
        #
        # WHY IT IS EMITTED NOW. `ds-engine-codex.css:7` asked for it to paint
        # every active pill and got nothing, so the hand-typed `#1a1a1a` beside
        # it became the permanent value on every kit -- measured 1.19:1 against
        # the text in light appearance. A name a consumer reads and the engine
        # never emits is a second design system one token at a time; the fix is
        # to emit the concept the consumer is correctly asking for.
        #
        # BOTH HALVES, always. `--color-foreground-inverse` above already
        # carried the ink under a different vocabulary, and shipping only the
        # background would have left a consumer pairing this with the PAGE's
        # foreground -- which is what collapsed the pill: one side of the
        # expression followed the engine and the other did not.
        "--color-highlight-neutral-background": palette.highlight_neutral_background,
        "--color-highlight-neutral-foreground": palette.highlight_neutral_foreground,
        "--color-accent": accent,
        "--color-accent-hover": _hover_brand(palette),
        # The branded figure at the SEPARATOR rung -- his own 10, not a
        # hand-picked 12 %.
        "--color-accent-subtle": c.composite(accent, 0.10, ground),
        "--color-borders-default": _step(palette, 20.0),
        "--color-borders-subtle": _step(palette, 10.0),
        # No border-hover slot exists in the engine, so: the next rung up. A
        # border that brightens by one step on hover is the smallest statement
        # the ladder can make, and it is a ladder step rather than a new number.
        "--color-borders-hover": _step(palette, 30.0),
        # -- shadows: elevation, or nothing where the ground took the border ---
        **_shadow_declarations(brand, palette),
        # -- tier S: the shadcn bridge ----------------------------------------
        "--background": ground,
        "--foreground": fg,
        "--card": elevated,
        "--card-foreground": fg,
        "--popover": elevated,
        "--popover-foreground": fg,
        # THE CTA, and the engine already decides it: `highlight_branded_background`
        # IS register rules 4 and 5 -- the brand's adaptation on a neutral ground,
        # the contrast-picked neutral on a chromatic one ("on brand-full theme,
        # the main ctas need to be neutral ... either black or white").
        "--primary": accent,
        "--primary-foreground": accent_fg,
        # shadcn's `secondary` is a quiet component fill, which the engine
        # already has as a component STYLE: TRANSPARENT = alternate-normal/005.
        "--secondary": _step(palette, 5.0),
        "--secondary-foreground": fg,
        "--muted": _step(palette, 10.0),
        "--muted-foreground": _step(palette, 60.0),
        # DELIBERATE DEVIATION from the spec's seed, which reads
        # `accent<-highlight-branded-background`. In okuro's vendored copy
        # `--accent` means accent-SUBTLE -- a 12 % tint (memory 59229d45) -- and
        # 679 call sites are written against that meaning, including
        # `hover:bg-accent/10` in button.tsx, which on a full brand colour would
        # be 10 % of an already-tinted value. The seed's INTENT is served at the
        # name that carries it, `--color-accent`; giving `--accent` the full
        # brand colour would repaint every menu focus row in the app, which is a
        # design decision for the owner and not an adapter's to take. Listed OPEN.
        "--accent": c.composite(accent, 0.10, ground),
        "--accent-foreground": accent,
        "--destructive": signals["error"],
        "--destructive-foreground": signal_fgs["error"],
        "--border": _step(palette, 20.0),
        "--input": _step(palette, 20.0),
        # RULE 9: focus is alternate-normal/100, which is the foreground.
        # shadcn's own default aliases `--ring` to the accent and draws it as a
        # box-shadow ring; the colour is corrected here, and the OUTLINE half is
        # `.ds-*:focus-visible`, which the engine already emits.
        #
        # THIS ADAPTER SETS THE RING'S COLOUR AND NEVER ITS TRIGGER. The trigger
        # is in whatever selector the consumer wrote -- Tailwind's own primitives
        # say `focus-visible:ring-*`, which is already his 2026-08-19 ruling; the
        # two that said bare `focus:ring-*` (the dialog and sheet close buttons)
        # were corrected in the same commit as this comment. A ring colour handed
        # to a bare `:focus` rule would still fire on a click, and no value
        # emitted here could stop it.
        "--ring": palette.focus.over or fg,
        # -- the alias names measured as read directly via var() --------------
        # 22 names live only because plain `@theme` declared them at :root, and
        # `@theme inline` stops declaring them at all. Measured across 448 files:
        # these are read by ~50 of them. Emitting them here is what makes the
        # `@theme inline` switch survivable, and it upgrades them from frozen
        # root values to ground-aware ones on the way.
        "--color-surface": ground,
        "--color-surface-elevated": elevated,
        "--color-surface-subtle": subtle,
        "--color-scrim": _veil(ground, SCRIM_ANCHOR),
        "--color-fg": fg,
        "--color-fg-primary": fg,
        "--color-fg-secondary": secondary,
        "--color-fg-muted": secondary,
        "--color-fg-tertiary": tertiary,
        "--color-fg-subtle": tertiary,
        "--color-fg-inverse": palette.highlight_neutral_foreground,
        "--color-tertiary": tertiary,
        "--color-border": _step(palette, 20.0),
        "--color-border-subtle": _step(palette, 10.0),
        "--color-border-hover": _step(palette, 30.0),
        # THE INK ON THE BRANDED FIGURE, not the figure itself. This slot held
        # `accent` -- the branded background repeated as its own foreground, so
        # `bg-accent text-accent-foreground` painted the brand on the brand. The
        # engine already answers it: `highlight_branded_foreground` IS register
        # rule 5's "on the brand's own ground the main CTAs are NEUTRAL
        # black/white by contrast". Same value the tier-S `--accent-foreground`
        # carries; the two names differ only in which vocabulary reads them.
        "--color-accent-foreground": accent_fg,
        "--color-destructive": signals["error"],
    }

    for role, value in signals.items():
        out[f"--color-status-{role}"] = value
        # OPAQUE, and that is load-bearing: 35 of the 37 call sites apply their
        # own /30../60 modifier, so a translucent token would attenuate twice --
        # /60 would mean 60 % of 20 %. The modifier is the only transparency;
        # the token is a colour to take a share OF.
        out[f"--color-status-{role}-subtle"] = c.composite(value, 0.20, ground)
        # THE COMPUTED INK, one name per role, emitted for ALL FOUR and not only
        # for the one shadcn happens to have a slot for.
        #
        # WHY IT HAS TO EXIST AT TIER U. `@theme inline` resolves a utility at
        # its USE site, so `text-destructive-foreground` compiles to whatever
        # `--color-destructive-foreground` points at -- and that row pointed at
        # `--color-foreground-primary`, the NEUTRAL body ink. So the tier-S
        # `--destructive-foreground` the adapter has always emitted (white on
        # the error red, register rule 2 at 0.675) was read by nothing, and a
        # destructive button rendered BLACK on red. A signal's ink is a value
        # the engine computes per role; the consumer needs a name per role to
        # read it through, so uniformity gives all four the name rather than
        # patching the one that was reported.
        out[f"--color-status-{role}-foreground"] = signal_fgs[role]
    # The four short aliases the app reads directly, same values.
    out["--color-error"] = signals["error"]
    out["--color-success"] = signals["success"]
    out["--color-info"] = signals["info"]
    out["--color-warning"] = signals["warning"]

    for i, value in enumerate(charts, start=1):
        out[f"--chart-{i}"] = value

    return out


def _hover_brand(palette: Palette) -> str:
    """`--color-accent-hover`: the engine's own brand-hover overlay.

    Register rule 9 -- hover is +-20 % measured on the component's own ground --
    and `States.hover_brand` is that answer already composited.
    """
    over = palette.states.hover_brand.over
    return over or palette.highlight_branded_background


def _shadow_declarations(brand: Brand, palette: Palette) -> dict[str, str]:
    """`--shadow-xs..xl`, or `none` where this ground took the border answer."""
    answer = palette.shadow
    if answer.kind == "border":
        return {f"--shadow-{name}": "none" for name in SHADOW_INTENSITY}
    veil = _rgba(answer.colour, round(answer.opacity, 4))
    out: dict[str, str] = {}
    for name, intensity in SHADOW_INTENSITY.items():
        effect = brand.effects.by_name(intensity).px()
        out[f"--shadow-{name}"] = (
            f"{effect.shadow_x:g}px {effect.shadow_y:g}px "
            f"{effect.shadow_spread:g}px {veil}"
        )
    return out


# ---------------------------------------------------------------------------
# The ground-independent half
# ---------------------------------------------------------------------------


def system_declarations(brand: Brand) -> dict[str, str]:
    """Every consumer name whose value is the same on every ground.

    Emitted ONCE, at `:root`, and inherited from there. A size does not change
    when the ground does, so refilling it at every boundary would be noise that
    a reader has to prove is noise. The invariant test states this as a
    partition: a name is in exactly one of the two halves, and the ground half is
    complete in every rule that publishes a ground.

    EVERY REM HERE IS THE FACTOR. Register rule 10: `rendered px = user-font-size
    x 0.5 x factor`, and because 16 x 50 % = 8 = the system base, `rem = factor =
    grid units`. So 16px is 2rem, not 1rem -- which is also the whole content of
    the x2 compensation the app-side scales need (see globals.css).
    """
    font = brand.font
    motion = brand.motion
    # THE BRAND'S OWN DEFAULT RUNG, not a hardcoded one. His ruling, 2026-09-06:
    # "I want the okuro-ui consume the full rung as designed."
    #
    # `body` (emit.py:973) and every `.ds-*` class (emit.py:1164) already read
    # `defaults.rung.desktop`; the two size loops below read a literal "L", and
    # those are exactly the names okuro's own components consume. On a brand at
    # any other rung the sheet contradicted itself -- `standard` renders its body
    # at XL while `--type-body-size` published L.
    #
    # DESKTOP ONLY, deliberately: emit.py:1283 already re-declares `:root` at
    # `defaults.rung.mobile` inside the mobile media query, so the second default
    # is already live and must not be duplicated here.
    default_rung = brand.defaults.rung.desktop

    out: dict[str, str] = {
        "--font-family-base": f'"{font.family}", {font.stack}',
        "--font-mono": f'"{font.family}", {font.stack}',
        "--radius-sm": _radius(brand, "S"),
        "--radius-md": _radius(brand, "M"),
        "--radius-lg": _radius(brand, "L"),
        # shadcn's base radius follows the medium rung, as it does today.
        "--radius": _radius(brand, "M"),
        # THE FIVE TAILWIND RADIUS NAMES THAT WERE OUTSIDE THE LADDER.
        #
        # `globals.css` declared these as literals -- 0.25rem / 1.5rem / 2rem /
        # 3rem / 4rem, Tailwind's own numbers, doubled for the 50 % root and
        # nothing to do with any brand. Three of okuro's most visible primitives
        # read them: `card.tsx` is `rounded-xl`, `dialog.tsx` and `sheet.tsx` are
        # `rounded-xs`. So a Card wore Tailwind's corner while every Button next
        # to it wore the brand's, and no amount of authoring the radius ladder
        # moved it.
        #
        # CLAMPED ONTO THE LADDER, never extended past it. Register rule 12 lists
        # the authored radius input exhaustively -- "radius ladder S/M/L/MAX +
        # radius-base 8" -- so inventing an xs factor or a 4xl factor would be
        # adding a slot the register does not have. The ladder has three real
        # rungs and a sentinel; eight app names funnel into them. Below S there
        # is no radius, so `xs` takes S; above L there is only MAX, which is a
        # SENTINEL for the pill shape and not "very large", so everything above L
        # takes L. `rounded-full` reaches MAX on its own and needs no name here.
        "--radius-xs": _radius(brand, "S"),
        "--radius-xl": _radius(brand, "L"),
        "--radius-2xl": _radius(brand, "L"),
        "--radius-3xl": _radius(brand, "L"),
        "--radius-4xl": _radius(brand, "L"),
        # THE BRAND'S FOUR AUTHORED BORDER WEIGHTS, published as names.
        #
        # A brand AUTHORS these -- "every brand needs to be able to define their
        # border weights" (BorderSettings) -- and the engine computes them off
        # the system base. Until now they reached exactly one place: the
        # `border-width` declaration inside a ground rule that took the BORDER
        # answer instead of the shadow one. No custom property carried them, so
        # no framework could consume them, and Tailwind's `border` stayed 1px at
        # every brand: measured 2026-09-06, 743 of 743 rendered borders pinned,
        # the only property in the app at 100 %.
        #
        # PUBLISHING IS NOT BINDING. These names give a framework something to
        # read; whether `border` maps to `hair` is that framework's adaptor's
        # question, and Tailwind v4 has no border-width theme namespace to do it
        # in `@theme`. This half is framework-neutral by construction.
        # THE WHOLE FAMILY, not the two the app happens to use. This test's own
        # principle, quoted in `test_g4`: "A block that emits only the values a
        # page happens to use leaks the parent's values across the boundary."
        # `--border-opacity` is deliberately NOT here -- the engine spends it
        # inside `border-color`, no consumer reads it, and a name without a
        # reader is the dead-output defect this work exists to remove.
        **{
            f"--border-width-{name}": f"{width:g}px"
            for name, width in zip(BORDER_WEIGHTS, brand.border.weights())
        },
        "--max-width": MAX_WIDTH,
        "--prose-max": PROSE_MAX,
        "--motion-easing": motion.curve(motion.default_curve),
    }

    for name, value in FIELD_WIDTHS.items():
        out[f"--field-{name}"] = value
    for name, value in ROW_HEIGHTS.items():
        out[f"--row-{name}"] = value

    # THE BRAND'S CHOSEN ROW OF AN ABSOLUTE TABLE, not the bare base.
    #
    # This used to be `scale.size(scale.BASE, factor)` with no rung in it at all,
    # which is why choosing a default rung moved no geometry: `--spacing-xs`
    # drives every Tailwind length in the app through `globals.css`'s
    # `--spacing: var(--spacing-xs)`, and it was reading a constant.
    #
    # It reduces to exactly the old expression at the ladder's anchor rung, so
    # every kit whose default rung IS the anchor is byte-identical to what it
    # shipped -- which is every kit but one at the time this landed.
    out.update(rung_component_declarations(brand, default_rung))

    # THE FAMILY WHOSE ABSENCE IS THE ANSWER. His ruling, 2026-09-07: a utility
    # weight binds only where the family really ships a face for it. A rung with
    # no face publishes NOTHING, and the app's own
    # `var(--font-weight-medium, 500)` answers it correctly -- which is strictly
    # better than an engine value that is true about the font and wrong about the
    # rung. This is the only name family in the sheet that varies in SIZE per
    # brand, and that is deliberate.
    for name, weight in _utility_weights(brand).items():
        out[f"--font-weight-{name}"] = str(weight)

    for name, style in FONT_SIZE_STEPS.items():
        out[f"--font-size-{name}"] = scale.rem(
            sizing.text_size(brand, default_rung, style)
        )

    for role, (style, weight_role) in TYPE_ROLES.items():
        out[f"--type-{role}-size"] = scale.rem(
            sizing.text_size(brand, default_rung, style)
        )
        out[f"--type-{role}-weight"] = str(
            font.numeric(getattr(brand.weights, weight_role))
        )
        out[f"--type-{role}-line"] = line_for(role)
        # TRACKING, AUTHORED PER BAND since his 2026-09-07 ruling. It used to be
        # `TYPE_TRACKING[role]` -- seven values in this file, which the docstring
        # there conceded were "still ZERO engine concept". It reads the brand
        # through the same band key as weight and case, so the three cannot
        # disagree about which band a role belongs to.
        out[f"--type-{role}-tracking"] = brand.tracking.css(weight_role)
        # THE CASE, published as a property and not only as a band rule.
        #
        # The engine already decides this: `brand.case` is a per-BAND boolean
        # (his 2026-08-19 toggle) and the emitter writes it into `.ds-titles`
        # and friends. But `globals.css` `.type-label` reads
        # `--type-label-transform` per ROLE, and nothing published it — so a
        # brand that set its labels UPPERCASE reached the band classes and not
        # the app's own type utility. P7's drift gate is what surfaced it.
        #
        # NO NEW MAPPING WAS INVENTED. `TYPE_ROLES` already carries each role's
        # band as its second value, and that table was FITTED against a parsed
        # oracle rather than authored from the names. Case follows size and
        # weight through the same door.
        out[f"--type-{role}-transform"] = brand.case.css(weight_role)

    for i, name in enumerate(MOTION_STEPS, start=1):
        out[f"--motion-{i}"] = f"{motion.speeds[name]}ms"
    for name in ("fast", "medium", "slow"):
        out[f"--motion-{name}"] = f"{motion.speeds[name]}ms"

    return out


# ---------------------------------------------------------------------------
# The vocabulary, as sets -- what the gates count
# ---------------------------------------------------------------------------


def ground_names(brand: Brand, palette: Palette) -> tuple[str, ...]:
    return tuple(ground_declarations(brand, palette))


def system_names(brand: Brand) -> tuple[str, ...]:
    return tuple(system_declarations(brand))


def consumer_names(brand: Brand, palette: Palette) -> tuple[str, ...]:
    """The whole vocabulary the adapter owns, in emission order."""
    return ground_names(brand, palette) + system_names(brand)


def rung_size_declarations(brand: Brand, rung: str) -> dict[str, str]:
    """The consumer names whose value depends on the RUNG, at one rung.

    WHY THIS EXISTS. `system_declarations` publishes the whole vocabulary on
    `:root`, at the brand's default rung — which is right for a document that
    never changes rung. It is wrong the moment a FRAME names one: register rule 10
    says "size inherits by frame ... as long as the children have no sizing
    overrides, they consume the parents size configuration", and okuro's own
    components are children.

    Measured before this existed, on the v2 canvas with the scene rung switch:
    moving the rung to XXL took the `.ds-*` specimen's body from 16px to 20px and
    its CTA from 40px to 52px, while okuro's real `Button` stayed at 14px/36px at
    every rung. The `.ds-*` classes are emitted per rung; these custom properties
    were not, so one half of the example followed the frame and the other half did
    not.

    THE TEXT TABLE IS HERE; THE COMPONENT TABLE IS `rung_component_declarations`.
    `--font-size-*` and `--type-*-size` read a cell of the text table, so they move
    with the TEXT rung and belong on a `TEXT_RUNGS` loop. The radii, the field
    widths and the row heights are fixed lengths and move with neither.

    An earlier version of this docstring also excluded `--spacing-*` "because they
    are fixed factors", and that sentence was the root cause of the whole rung
    deafness: the component table was then reachable only through the `.ds-*`
    utility classes, which no vendored primitive carries. See
    `rung_component_declarations`.
    """
    out: dict[str, str] = {}
    for name, style in FONT_SIZE_STEPS.items():
        out[f"--font-size-{name}"] = scale.rem(sizing.text_size(brand, rung, style))
    for role, (style, _weight_role) in TYPE_ROLES.items():
        out[f"--type-{role}-size"] = scale.rem(sizing.text_size(brand, rung, style))
    return out


def rung_component_declarations(brand: Brand, rung: str) -> dict[str, str]:
    """The app's spacing scale, re-based onto one COMPONENT rung.

    WHY THIS EXISTS. Register rule 10's component table was published only as
    `.ds-{role}` utility classes, and measured on the deployed COMPONENTS scene
    on 2026-08-19: 0 of 33 vendored primitives carry a single `.ds-*` class, so
    across 9 rungs not one primitive moved a height, a padding, a gap or an icon
    size. A class cannot be consumed by a Tailwind utility — the component table
    had no consumer NAME at all, which is the only reason the axis looked dead.

    WHY ONE SCALAR REPRODUCES A 9 x 16 TABLE. `SizeFactors.component_factor` is
    `component_rung_factors()[rung] * role_ratios[role]` — the rung contributes a
    single factor and the role a fixed ratio, so every one of the 16 roles moves
    by the SAME ratio between two rungs. Re-basing the app's 8-step spacing scale
    by that one ratio therefore reproduces the whole table for every geometry
    utility at once. Measured in the served bundle: `h-9`, `px-4`, `gap-2`,
    `size-4`, `p-6`, `min-h-9`, `mt-2` all already compile to
    `calc(var(--spacing-xs) * N)`, so ~275 fixed-size sites across 31 components
    start following the frame with ZERO component edits.

    NORMALISED TO THE LADDER'S ANCHOR RUNG, never to the brand's own default.
    His ruling, 2026-09-06: "The rung is an absolute default! it's coming from the
    main design system and cannot be overwritten by a copy! what can be
    overwritten, what brand takes what rung as default for mobile and for
    desktop."

    THIS LINE USED TO DIVIDE BY `defaults.rung.desktop`, and that made a rung
    RELATIVE -- the one thing his ruling says it is not. Measured before the fix:
    `[data-rung="L"]` published `--spacing-xs: 0.5rem` inside okuro-ds and
    `0.4348rem` inside a brand whose default is XL, so the same named row of the
    same absolute table painted two different geometries. The brand's default
    chooses WHICH row it starts on; it may not move the rows.

    The old normalisation was written to make the change "safe to land" -- ratio
    exactly 1 at the brand's default, so nothing moves. What it actually bought
    was the deafness it was trying to fix: with the ratio pinned to 1, a brand
    that switched its default rung changed no geometry at all, which is exactly
    the symptom he reported when he chose XL and saw nothing move.
    """
    rungs = brand.sizes.component_rung_factors()
    ratio = rungs[rung.upper()] / rungs[COMPONENT_ANCHOR_RUNG]
    return {
        f"--spacing-{name}": scale.rem(scale.size(scale.BASE, factor * ratio))
        for name, factor in SPACING_FACTORS.items()
    }
