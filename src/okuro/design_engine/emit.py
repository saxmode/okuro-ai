# SPDX-License-Identifier: Apache-2.0
"""The CSS emitter: a static sheet that behaves like the resolver.

    "whatever fits the system perfectly and let it behave the correct way"
    (his delegation of the nesting mechanism, d7)

THE MECHANISM. Every boundary publishes its RESOLVED GROUND as one custom
property, and every rule that depends on a ground is wrapped in a style
container query on it:

    :root                              { --ground: g-ffffff; ... }
    @container style(--ground: g-ffffff) {
        .ds-surface   { background-color: #ffffff; color: #030303 }
        [data-emphasis] { --ground: g-030303; background-color: #030303; ... }
    }

A style query is evaluated against the nearest ancestor container, and every
element is a style container by default, so the query reads the PARENT's
published ground. That single fact is what makes the sheet recurse without a
depth counter, exactly as the resolver does:

    "If a child has another child that needs high-contrast, the switch flips
    back. So componentA(light).componentA1(dark).componentA1A(light)."

Measured in Chromium 145 to depth 14 with correct alternation; the browser gate
re-measures it on every run.

THE GROUND KEY IS THE GROUND'S COLOUR -- `g-` plus its six hex digits, nothing
else. This is not a naming convention, it is the uniformity principle made
mechanical:

    "everything behaves exactly the same. it doesn't fucking matter what the
    background color is."

Two grounds that arrive at the same colour by different routes -- the root's
white, the emphasis-flip's white, a brand's fallback white -- produce the same
key and therefore the same block. There is nowhere for a case split to hide,
because the emitter cannot see how a ground was reached. `Ground.origin` is
never read here, exactly as it is never read in the resolver.

NO var() FOR COLOUR. `--ground` is the one custom property this sheet declares.
Every colour is a resolved literal that came out of the resolver; this module
renders, it never computes a colour. The one arithmetic it does perform is
px -> rem against the 50 % root, which is a size, not a colour -- and against
that root a rem IS the factor.

WHAT IS OPEN. An image ground carries a lightness and no colour (P0 q7), so it
has no key and no literal palette; those elements travel the blur-background
route and the sheet does not yet emit them. See the P2 report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import adapter
from . import colour as c
from . import scale, sizing
from .resolve import (
    COMPONENT_STYLES,
    Ground,
    Ladder,
    Palette,
    Request,
    Resolver,
)
from . import schema
from .schema import COMPONENT_RUNGS, TEXT_RUNGS, Brand

BODY_STYLE = "N5"
"""The cell the DOCUMENT's own text size comes from -- the normal band's body
rung, which is the same cell `adapter.TYPE_ROLES` maps the app's `body` role to.
Named once so the sheet's floor and the app's body role cannot drift apart."""


# ---------------------------------------------------------------------------
# Keys and small renderers
# ---------------------------------------------------------------------------


def ground_key(ground: Ground) -> str:
    """`g-` + the ground's six hex digits. A pure function of the COLOUR."""
    if ground.colour is None:
        raise ValueError(
            "an image ground has a lightness but no colour (P0 q7), so it has no "
            "sheet key; those elements take the blur-background route"
        )
    return "g-" + ground.colour.lstrip("#").lower()


def _rem(px: float) -> str:
    """px -> rem against the 50 % root -- and the result IS the factor.

    The division moved to `scale.rem`, next to the two constants whose identity
    it depends on, because the adapter renders sizes too and two spellings of one
    division is how two definitions start to drift. This name stays as the
    emitter's local spelling.

    The px round trip is kept rather than short-circuited to the factor because
    the derived roles (*2, *0.5, the negative subtractions) are arithmetic on
    px, and a second factor path for them is how two definitions start to drift.
    """
    return scale.rem(px)


def _step_class(percent: float) -> str:
    return f"{percent:g}".replace(".", "_")


def _scoped(selector: str, scope: str | None, attach: bool = False) -> str:
    """Confine a selector to one brand's subtree.

    A nested GUEST brand is his inbox case -- "main theme is [host] (yellow
    brand color) ... A UBS component (background black, main cta: red) works
    with contrast towards the [host] yellow, while maybe Valiant (purple) might
    not have enough contrast." The guest's whole system lives under one element
    that declares which brand it is.

    Two forms, and the difference is which ground the rule is about:

      * `attach=False` -- a rule about the ground the element SITS on, so it
        applies to the guest's descendants: `S .ds-surface`.
      * `attach=True` -- a rule about the parent's ground, which is what a
        BOUNDARY is. The guest's entry element carries both attributes at once
        (`S[data-branded]`) and an inner boundary is a descendant
        (`S [data-emphasis]`); both are the same statement, so both are
        emitted and neither is a special case.

    An unscoped sheet (the host's own) is the `scope is None` path and emits
    exactly what it did before guests existed.
    """
    if scope is None:
        return selector
    if attach:
        return f"{scope}{selector}, {scope} {selector}"
    return f"{scope} {selector}"


# ---------------------------------------------------------------------------
# Requests -- the axes an element may ask for
# ---------------------------------------------------------------------------


def requests_of(brand: Brand) -> list[tuple[str, Request]]:
    """Every request an element can make, as (attribute selector, Request).

    Two requests and a design fact, which is his whole vocabulary:

        "unread is = needs emphasis, read = doesn't need emphasis"
        "signal and brand are design facts"

    "no request" is deliberately absent: inheritance is the ABSENCE of a rule.
    An element with no attribute matches no boundary rule, keeps its parent's
    `--ground` because custom properties inherit, and paints nothing of its own
    -- which is "If the element is read, it consumes the mother theme, so it
    doesn't stick out" with no code at all.
    """
    out: list[tuple[str, Request]] = [
        ("[data-emphasis]", Request(emphasis=True)),
        ("[data-branded]", Request(branded=True)),
    ]
    for role in brand.signals.by_role():
        out.append((f'[data-signal="{role}"]', Request(signal=role)))
    return out


def reachable_grounds(
    resolver: Resolver, extra_seeds: tuple[Ground, ...] = ()
) -> list[Ground]:
    """The closure of every ground the system can ever reach, from any root.

    Computed as a fixed point rather than enumerated: seed with the roots he
    allows ("they can be placed on the highest element" -- light, dark, brand,
    every signal), then apply every request to every ground found until nothing
    new appears.

    It TERMINATES, and that is the proof that a static sheet can serve
    unbounded depth: the requests are finite, the colours a brand authors are
    finite, and the key is the colour -- so the reachable set is finite even
    though the tree is not.

    `extra_seeds` are grounds this brand did not produce but may find itself
    standing on -- a GUEST brand nested inside a host, which is the case he
    requires the system to express even though okuro will not ship it: "the
    system still needs to be able to do it. Understood?"
    """
    brand = resolver.brand
    seeds = [
        resolver.root("light"),
        resolver.root("dark"),
        resolver.root("light", Request(branded=True)),
    ]
    for role in brand.signals.by_role():
        seeds.append(resolver.root("light", Request(signal=role)))
    seeds.extend(extra_seeds)

    seen: dict[str, Ground] = {}
    queue: list[Ground] = []
    for ground in seeds:
        key = ground_key(ground)
        if key not in seen:
            seen[key] = ground
            queue.append(ground)

    requests = [request for _, request in requests_of(brand)]
    while queue:
        parent = queue.pop(0)
        for request in requests:
            child = resolver.ground(request, parent)
            key = ground_key(child)
            if key not in seen:
                seen[key] = child
                queue.append(child)
    return list(seen.values())


# ---------------------------------------------------------------------------
# The sheet
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sheet:
    """A rendered sheet, plus what it took to render it."""

    css: str
    ground_keys: tuple[str, ...]
    container_blocks: int
    rules: int
    flags: tuple = field(default_factory=tuple)

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.css


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.rules = 0
        self.blocks = 0

    def comment(self, text: str) -> None:
        self.lines.append(f"/* {text} */")

    def blank(self) -> None:
        self.lines.append("")

    def rule(self, selector: str, decls: dict[str, str], indent: str = "") -> None:
        if not decls:
            return
        body = " ".join(f"{k}: {v};" for k, v in decls.items())
        self.lines.append(f"{indent}{selector} {{ {body} }}")
        self.rules += 1

    def open_block(self, header: str, *, counts: bool = True) -> None:
        """Open an at-rule block.

        `counts` is what `Sheet.container_blocks` reports, and it means GROUND
        blocks -- the invariant "one @container block per reachable ground" is
        asserted against it. A `@media` block is not a ground, so it opens with
        `counts=False`; without that the brand's mobile default rung made the
        sheet claim one ground more than the resolver can reach.
        """
        self.lines.append(f"{header} {{")
        if counts:
            self.blocks += 1

    def close_block(self) -> None:
        self.lines.append("}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


# -- the pieces -------------------------------------------------------------


def _font_face_rules(brand: Brand, sources: dict[str, str] | None) -> list[str]:
    """One `@font-face` per face the family actually ships.

        "the font files installed on a Mac or on a Linux machine render things
        like "Light, Regular, etc." in the Figma UI. That's why font-weight
        isn't using number for weight. Programmatically this would be css
        weights."

    So the sheet is where the descriptor becomes the number: the slot holds
    "ExtraBold", the face table says 800, and `@font-face` binds the two. The
    numbers come from the faces (P0 q5), never from a guess -- `sources` is
    authored by the kit, because the emitter does no I/O and does not go
    looking for fonts.
    """
    faces = brand.font.faces or {}
    out: list[str] = []
    for descriptor, weight in sorted(faces.items(), key=lambda kv: kv[1]):
        url = (sources or {}).get(descriptor)
        srcs = [f'local("{brand.font.family} {descriptor}")']
        srcs.append(f'local("{brand.font.family.replace(" ", "")}-{descriptor}")')
        if url:
            srcs.append(f'url("{url}") format("truetype")')
        out.append(
            "@font-face { "
            f'font-family: "{brand.font.family}"; '
            "font-style: normal; "
            f"font-weight: {weight}; "
            f"src: {', '.join(srcs)}; "
            "font-display: swap; }"
        )
    return out


def _ladder_rules(
    w: _Writer, name: str, ladder: Ladder, indent: str, scope: str | None
) -> None:
    """All 17 rungs of one ladder, as literals.

        "DARKEN (17 STEP OPACITY CALCULATION ...) 100.0, 097.0, 095.0, 090.0,
        080.0, 070.0, 060.0, 050.0, 040.0, 030.0, 020.0, 010.0, 005.0, 003.0,
        001.0, 000.1, 000.0"

    Emitted as the COMPOSITED literal, not as rgba(): "palettes stay resolved
    literals". The rgba form exists on the step and is what an image ground
    would need -- which is why image grounds are the open case.
    """
    for step in ladder.steps:
        assert step.over is not None
        w.rule(
            _scoped(f".ds-{name}-{_step_class(step.percent)}", scope),
            {"background-color": step.over},
            indent,
        )


def _palette_rules(
    w: _Writer, brand: Brand, palette: Palette, indent: str, scope: str | None
) -> None:
    """The COMPLETE palette of one ground.

    Complete on purpose: "complete palette per boundary". A block that emits
    only the values a page happens to use leaks the parent's values across the
    boundary the moment a component reaches for one that is missing.
    """
    assert palette.background is not None

    w.comment("the surface itself: background -> foreground -> everything")
    w.rule(
        _scoped(".ds-surface", scope),
        {"background-color": palette.background, "color": palette.foreground},
        indent,
    )
    # "HIGHLIGHT-NEUTRAL-BACKGROUND - ... is the inverse of BACKGROUND"
    w.rule(
        _scoped(".ds-highlight-neutral", scope),
        {
            "background-color": palette.highlight_neutral_background,
            "color": palette.highlight_neutral_foreground,
        },
        indent,
    )
    # "HIGHLIGHT-BRANDED-BACKGROUND - Main brand color ... based on best
    # contrast towards main background color."
    w.rule(
        _scoped(".ds-highlight-branded", scope),
        {
            "background-color": palette.highlight_branded_background,
            "color": palette.highlight_branded_foreground,
        },
        indent,
    )

    w.comment("borders and separators")
    # "SEPARATOR: ... 10% of the foreground color"
    assert palette.separator.over is not None
    w.rule(_scoped(".ds-separator", scope), {"background-color": palette.separator.over}, indent)
    # "BORDER-FULL - inherits from FOREGROUND"
    w.rule(_scoped(".ds-border-full", scope), {"border-color": palette.border_full}, indent)
    # "BORDER-HALF: ... 20% foreground color"
    assert palette.border_half.over is not None
    w.rule(_scoped(".ds-border-half", scope), {"border-color": palette.border_half.over}, indent)
    w.rule(_scoped(".ds-border-branded", scope), {"border-color": palette.border_branded}, indent)
    w.rule(
        _scoped(".ds-border-branded-inverse", scope),
        {"border-color": palette.border_branded_inverse},
        indent,
    )

    w.comment("the two ladders: alternate = the foreground, inverse = its opposite")
    _ladder_rules(w, "alt", palette.alternate, indent, scope)
    _ladder_rules(w, "alt-inverse", palette.alternate_inverse, indent, scope)

    w.comment('"blur background needs to be background, which will be alternate inverse always"')
    assert palette.blur_background.over is not None
    w.rule(
        _scoped(".ds-blur-background", scope),
        {"background-color": palette.blur_background.over},
        indent,
    )

    w.comment("signals: foregrounds by contrast against the signal colour")
    for role, value in palette.signal_backgrounds.items():
        w.rule(
            _scoped(f".ds-signal-{role}", scope),
            {"background-color": value, "color": palette.signal_foregrounds[role]},
            indent,
        )

    w.comment("elevation: a shadow, or a border where a shadow cannot separate")
    _elevation_rules(w, brand, palette, indent, scope)

    w.comment('"DISABLED ... 40%" / "OFF ... 64%"')
    w.rule(".ds-disabled", {"opacity": f"{palette.disabled:g}"}, indent)
    w.rule(".ds-off", {"opacity": f"{palette.off:g}"}, indent)


def _rgba(colour: str, alpha: float) -> str:
    r, g, b = (round(x * 255) for x in c.parse_hex(colour))
    return f"rgba({r}, {g}, {b}, {alpha:g})"


_ELEVATION_RENDER = {
    # A table, not a case split. The resolver decided WHICH answer this ground
    # gets; the emitter only knows that the two answers are carried by two
    # different CSS properties and cannot share one declaration.
    "shadow": lambda answer, effect: {
        "box-shadow": (
            f"{effect.shadow_x:g}px {effect.shadow_y:g}px "
            f"{effect.shadow_spread:g}px {_rgba(answer.colour, round(answer.opacity, 4))}"
        ),
        "border-style": "solid",
        "border-width": "0",
    },
    "border": lambda answer, effect: {
        "box-shadow": "none",
        "border-style": "solid",
        "border-width": f"{answer.width:g}px",
        # The border IS the separation here, so it carries the brand's border
        # opacity -- "this is 40% opacity. this needs to be configurable."
        "border-color": _rgba(answer.colour, answer.opacity),
    },
}


def _elevation_rules(
    w: _Writer, brand: Brand, palette: Palette, indent: str, scope: str | None
) -> None:
    """`.ds-elevated` and `.ds-elevated-blurred`, whichever answer the ground got.

        "on white a 20% black color for a shadow is enough, while on dark it
        needs to be 100% to be visible."

    and, where 100 % still is not enough, P0 q4 in two words: "border ok".

    The geometry is his authored effect table; `shadow_spread` is the CSS blur
    radius and `blur_amount` the backdrop blur, which is the reading his own
    sentence forces -- "blur and shadow is a shadow with a blurred backdrop".
    """
    for cls, answer, effect_name in (
        (".ds-elevated", palette.shadow, "normal"),
        (".ds-elevated-blurred", palette.blur_and_shadow, "light"),
    ):
        # Effect intensities are FACTORS (register rule 10); px happens here.
        effect = brand.effects.by_name(effect_name).px()
        decls = dict(_ELEVATION_RENDER[answer.kind](answer, effect))
        if cls.endswith("blurred"):
            decls["backdrop-filter"] = f"blur({effect.blur_amount:g}px)"
        w.rule(_scoped(cls, scope), decls, indent)


#: WHICH VENDORED PRIMITIVE *IS* WHICH OF HIS SEVEN COMPONENT STYLES.
#:
#: THE DEFECT THIS TABLE EXISTS TO CLOSE. Measured on the deployed COMPONENTS
#: scene, 2026-08-19: of the 392 `:hover` / `:focus-visible` / `:active` rules
#: this module emits, 350 matched NOTHING -- because every one of them is
#: addressed to a `.ds-solid` / `.ds-transparent` / ... class and not one
#: component in the scene carries one. The engine's SIZE vocabulary is wired to
#: the components (`.ds-t*`, `.ds-n*`, `.ds-padding` -- 792 live rules); its
#: COLOUR AND STATE vocabulary was addressed to a class name the app stopped
#: using, so the register-correct outline and the pressed role-exchange were both
#: emitted correctly and reached 1 element out of 479.
#:
#: WHY AN ATTRIBUTE AND NOT A CLASS. `data-slot` and `data-variant` are what a
#: shadcn primitive already writes BY CONSTRUCTION -- `button.tsx:55-57` sets
#: all three on every render. A class has to be remembered; an attribute is what
#: the component IS. That is the difference between a binding that holds for the
#: next primitive somebody vendors and one that holds until somebody forgets.
#:
#: THE CLASSES STAY. These selectors are ADDED to the `.ds-*` ones, never
#: substituted: an element may still ask for a style by name, and the sheet
#: invariant that every style is addressable by its own class is unchanged.
#:
#: The mapping is not a guess -- each row is the tier-S bridge read backwards:
#:
#:   default   -> solid-brand  `--primary` IS `highlight_branded_background`
#:                             (adapter.py:410), which is solid-brand's fill.
#:   secondary -> transparent  adapter.py:412-414 already says so in words:
#:                             "shadcn's `secondary` is a quiet component fill,
#:                             which the engine already has as a component
#:                             STYLE: TRANSPARENT = alternate-normal/005".
#:   ghost     -> no-background  resolve.py:814, verbatim: "`no-background` is
#:                             also what his 'ghost' button is".
#:   outline   -> no-background  it renders `bg-background`, i.e. the ground
#:                             shows through, which is exactly no-background's
#:                             fill. Its BORDER is not touched here -- see below.
#: A FIELD IS NOT ON THIS TABLE, AND THAT IS THE CORRECTION OF 2026-09-11.
#: `input`, `textarea`, `select-trigger` and `switch` used to sit in the
#: `no-background` row -- "a field paints the page's ground and states itself
#: with a border and the ring" -- and that sentence was true about the FILL and
#: wrong about everything else, because this table feeds the WHOLE state block.
#: Measured on the deployed page: pressing a text input painted it `#649d8d`
#: with white text, and a CHECKED switch turned `#cdcdcd` under the cursor, i.e.
#: it read as OFF while you pointed at it. See `_FIELD_SELECTORS`.
#:
#: FOUR STYLES GET NO ATTRIBUTE, ON PURPOSE, and each absence is a decision:
#:
#:   solid   -- its fill is the ground's FOREGROUND (near-black on white). No
#:              vendored variant asks for an inverted-neutral fill. Handing it to
#:              `secondary` would be choosing his vocabulary for him.
#:   *-inverse -- the inverse family exists for a component that must read
#:              against its own ground's inverse; nothing in the kit names it.
#:
#: TWO VARIANTS ARE DELIBERATELY UNBOUND, and they are the two open questions:
#:
#:   destructive -- `--destructive` is `signals["error"]`, which is NOT one of
#:              the seven; it is a SIGNAL. The engine emits `.ds-signal-<role>`
#:              with no state rules at all, so there is nothing correct to bind
#:              it to yet. It still gets the focus ring, from the rule below.
#:   link     -- it is `no-background` by fill, but a text link's hover is an
#:              UNDERLINE, not a 20 % wash behind the words. Binding it would
#:              repaint a text affordance as a button, which is a design change
#:              and not a binding. Focus ring only.
#: A BADGE IS BOUND ONLY AS AN ANCHOR, and that is the kit's own semantics rather
#: than a compromise. `badge.tsx` gates every hover behind `[a&]:hover:` — a badge
#: is interactive when it IS a link and inert otherwise, because a status chip is
#: a label. Binding `[data-slot="badge"]` unconditionally would repaint a static
#: chip under the cursor, which is a state where there is no interaction. So the
#: selector carries the `a` and a `<span>` badge keeps having no state vocabulary,
#: correctly.
_STYLE_SELECTORS: dict[str, tuple[str, ...]] = {
    "solid-brand": (
        '[data-slot="button"][data-variant="default"]',
        'a[data-slot="badge"][data-variant="default"]',
    ),
    "transparent": (
        '[data-slot="button"][data-variant="secondary"]',
        'a[data-slot="badge"][data-variant="secondary"]',
    ),
    "no-background": (
        '[data-slot="button"][data-variant="ghost"]',
        '[data-slot="button"][data-variant="outline"]',
        'a[data-slot="badge"][data-variant="ghost"]',
        'a[data-slot="badge"][data-variant="outline"]',
    ),
}

#: THE FIELDS, AND THEY ARE A DIFFERENT QUESTION FROM THE SEVEN STYLES.
#:
#: A style answers "what is this component PAINTED". A field's answer to that is
#: still `no-background` -- the page's ground shows through -- and nothing here
#: changes it. What these four slots need is a different answer to a SECOND
#: question the style table was accidentally answering for them: WHICH STATES
#: does this surface wear.
#:
#: THE DEFECT. `_STYLE_SELECTORS` feeds `_component_rules`, which emits hover,
#: focus and active for every selector in the row. Putting the fields in the
#: `no-background` row therefore gave them a `:active` and a fill-repainting
#: `:hover` along with the fill they actually wanted. Measured 2026-09-11 on the
#: deployed page, `:active` asserted in the same evaluate pass:
#:
#:   input, pressed          #649d8d bg / #ffffff ink -- a text field wearing a
#:                           button's press
#:   switch CHECKED, hover   #cdcdcd -- the 20 % wash repainting the ON fill, so
#:                           a switch reads as OFF while the cursor is on it
#:
#: WHY A SECOND TABLE AND NOT AN EIGHTH STYLE. `COMPONENT_STYLES` is the
#: register's own list and `resolve.py` raises on anything outside it ("his seven
#: are ..."); a field's BACKGROUND is genuinely one of those seven. And why not a
#: kind test inside `_component_rules`: register rule 1 forbids branching on what
#: a component IS, and an exception buried in a loop is invisible from the
#: selector list that produced it. `_SIGNAL_SELECTORS` established the shape this
#: follows -- a surface that is not one of the seven STYLE rows gets its own
#: named table and its own emission function, where the difference is readable.
#:
#: WHAT A FIELD WEARS, AND WHAT IT DOES NOT:
#:
#:   rest   -- nothing from here. The rest rule is class-only for the reason
#:             `_component_rules` states: it writes `border-width: 0`, which on a
#:             field would delete the border it means to have.
#:   focus  -- `_focus_ring_rule`, unchanged. `input`, `textarea`, `select` and
#:             `button` (a switch and a select-trigger are buttons) are all in
#:             `_FOCUSABLE`, so the register's ring already reaches every one of
#:             them without a slot list.
#:   hover  -- the BORDER only, never the fill. Rule 9's hover is "+/-20% measured
#:             on the COMPONENT'S OWN ground", and a field's own ground IS the
#:             page's, so a 20 % wash there repaints the page under the field --
#:             which is the switch defect above. The border is what a field states
#:             itself with, so the border is what moves, one rung up the alternate
#:             ladder. Not a new number: `adapter.py` already publishes exactly
#:             this value as `--color-borders-hover` with the same reasoning
#:             ("a border that brightens by one step on hover is the smallest
#:             statement the ladder can make").
#:   pressed -- NOTHING. A press is what a button does. A field's click puts a
#:             caret in it, a switch's click toggles it, and neither is a state
#:             the surface paints.
_FIELD_SELECTORS: tuple[str, ...] = (
    '[data-slot="input"]',
    '[data-slot="textarea"]',
    '[data-slot="select-trigger"]',
    '[data-slot="switch"]',
)

#: The rung the field's border moves to on hover. One step up from the 20 % the
#: rest border is drawn at (`adapter.py` `--color-borders-default`), which is the
#: same pair `--color-borders-hover` already names.
FIELD_HOVER_BORDER_STEP = 30.0

#: Every natively focusable element, as one list.
#:
#: THE RING IS THE ONE RULE THAT DOES NOT NEED THE TABLE ABOVE, and that is a
#: property of the register rather than a shortcut: `focus_outline` reads the
#: MOTHER's ground and nothing else (ruling 5 -- "the ring is drawn on the
#: mother's surface"), so all seven styles resolve the SAME outline inside one
#: ground block. One question, one answer, one selector.
#:
#: WHY IT IS NOT A LIST OF `data-slot`s. Measured on the scene: 15 of 27 plain
#: `<button>` elements carried no focus class at all and fell back to Chromium's
#: `outline: auto 1px` -- six distinct focus treatments on one page, of which the
#: register's was used by none. A slot list would have missed every one of them,
#: because the thing they have in common is not being a design-system primitive;
#: it is being focusable. So the selector says that instead.
_FOCUSABLE: tuple[str, ...] = (
    "a[href]",
    "area[href]",
    "button",
    "input",
    "select",
    "textarea",
    "summary",
    "[contenteditable]",
    '[tabindex]:not([tabindex="-1"])',
)


#: Which signal role a vendored variant means. One row, because there is one.
#:
#: A SIGNAL IS NOT ONE OF THE SEVEN. `--destructive` is `signals["error"]`
#: (adapter.py:429), and `COMPONENT_STYLES` is the BACKGROUNDS list -- "signal and
#: brand are design facts", a different axis. So `destructive` cannot be bound
#: through `_STYLE_SELECTORS`, and leaving it unbound left the one button variant
#: in the kit with no pressed state at all.
#:
#: `_signal_state_rules` closes that by asking the resolver the question it
#: already answers: a destructive button is a component whose FILL is the signal
#: colour, which is `no-background` sitting on the SIGNAL ground with the page as
#: its mother. No new formula, no new palette, and the states come out of the same
#: `surface_of` every other component uses -- including the pressed swap.
#: The forced twin of every interactive pseudo-class, for specimens.
#:
#: A LIBRARY VIEW HAS TO SHOW STATES IT IS NOT IN. One rule carrying both the
#: pseudo-class and its attribute twin is the only form of that which cannot
#: drift: a showcase stylesheet that repeats the hover declarations is a second
#: answer to a question the engine already answers, and it goes stale silently --
#: the specimen keeps rendering the old value and looks authoritative doing it.
#:
#: `data-demo` NOT `data-state`: Radix already owns `data-state` on a dozen
#: primitives (`open`, `checked`, `active`) and colliding with it would make a
#: forced specimen indistinguishable from a real Radix state, which is a bug in
#: both directions.
#:
#: `:focus-visible` has a twin like the others even though nothing can force it
#: honestly -- the browser owns that heuristic -- and that is exactly why the
#: attribute exists: a specimen cannot ask for `:focus-visible`, so without a
#: twin the ring is the one state a library view can never show.
_DEMO_TWIN: dict[str, str] = {
    ":hover": '[data-demo="hover"]',
    ":active": '[data-demo="pressed"]',
    ":focus-visible": '[data-demo="focus"]',
}


_SIGNAL_SELECTORS: dict[str, tuple[str, ...]] = {
    "error": (
        '[data-slot="button"][data-variant="destructive"]',
        # As an anchor only, for the reason above `_STYLE_SELECTORS`.
        'a[data-slot="badge"][data-variant="destructive"]',
    ),
}


def _signal_state_rules(
    w: _Writer, resolver: Resolver, ground: Ground, indent: str, scope: str | None
) -> None:
    """Hover and pressed for a surface painted in a signal colour.

    `_palette_rules` already emits the REST state as `.ds-signal-<role>`; this is
    the other three quarters of it. Emitted for every role rather than only for
    the one `_SIGNAL_SELECTORS` binds, because a status chip and a toast are the
    same surface as a destructive button and the sheet is a contract: a selector
    set that varies by role is exactly the leak "complete palette per boundary"
    exists to stop.

    Focus is absent on purpose -- `_focus_ring_rule` already answers it for every
    focusable element on this ground, and a signal does not change the ring.
    """
    for role in resolver.brand.signals.by_role():
        signal_ground = resolver.ground(Request(signal=role), ground)
        s = resolver.surface_of(signal_ground, "no-background", mother=ground)
        heads = (f".ds-signal-{role}", *_SIGNAL_SELECTORS.get(role, ()))

        # PAIRED WITH ITS DEMO TWIN, exactly as `_component_rules` is. A signal
        # surface reaches the DOM by a DIFFERENT function from the seven styles,
        # so pairing only that one left `destructive` as the single variant whose
        # forced specimen disagreed with a real cursor — caught by the matrix on
        # its first run, which is what an enumerating view is for. Two emission
        # paths, one contract: every state the sheet paints can be forced.
        def sel(pseudo: str) -> str:
            parts = [_scoped(f"{h}{pseudo}", scope) for h in heads]
            demo = _DEMO_TWIN.get(pseudo)
            if demo:
                parts += [_scoped(f"{h}{demo}", scope) for h in heads]
            return ", ".join(parts)

        w.rule(
            sel(":hover"),
            {"background-color": s.hover.colour, "color": s.hover.ink},
            indent,
        )
        w.rule(
            sel(":active"),
            {"background-color": s.pressed.colour, "color": s.pressed.ink},
            indent,
        )


def _field_state_rules(
    w: _Writer, resolver: Resolver, ground: Ground, indent: str, scope: str | None
) -> None:
    """The one state a field paints for itself: its border, on hover.

    See `_FIELD_SELECTORS` for the measured defect and for why the four field
    slots left the `no-background` style row. This function is the whole of what
    replaced them, and its shape is the argument: there is no `:active` rule to
    write here, so there is no exception to remember not to emit.

    The colour is a rung of the ground's own alternate ladder, so it follows the
    ground like every other border in the system and needs no per-field value.
    Paired with its `[data-demo="hover"]` twin for the same reason every other
    state is: a library view must be able to force what a real cursor gets, out
    of the same declaration block.
    """
    palette = resolver.palette(ground)
    step = palette.alternate.at(FIELD_HOVER_BORDER_STEP).over
    assert step is not None, "an image ground has no literal palette (P0 q7)"
    demo = _DEMO_TWIN[":hover"]
    selector = ", ".join(
        [_scoped(f"{s}:hover", scope) for s in _FIELD_SELECTORS]
        + [_scoped(f"{s}{demo}", scope) for s in _FIELD_SELECTORS]
    )
    w.rule(selector, {"border-color": step}, indent)


def _focus_ring_rule(
    w: _Writer, resolver: Resolver, ground: Ground, indent: str, scope: str | None
) -> None:
    """The register's focus ring, for everything on this ground that can take it.

    See `_FOCUSABLE` for why the selector is a focusability test and not a slot
    list, and `_STYLE_SELECTORS` for the defect both exist to close.

    It declares ONLY the outline -- no background, no colour -- so a focused AND
    hovered component keeps its hover fill and wears the ring, which is ruling 5
    unchanged. `outline` is written as the shorthand on purpose: the vendored
    kit's base strings carry `outline-none`, and a longhand `outline-width` would
    leave `outline-style: none` standing and paint nothing.
    """
    outline = resolver.surface_of(ground, "solid").focus.outline
    assert outline is not None
    # The demo twin rides in the SAME rule, for the reason given at `_DEMO_TWIN`:
    # `:focus-visible` is the one state a specimen cannot honestly ask for, since
    # the browser owns the heuristic. Without the twin the ring is the only part
    # of the register a library view could never render.
    demo = _DEMO_TWIN[":focus-visible"]
    selector = ", ".join(
        [_scoped(f"{part}:focus-visible", scope) for part in _FOCUSABLE]
        + [_scoped(f"{part}{demo}", scope) for part in _FOCUSABLE]
    )
    w.rule(
        selector,
        {
            "outline": f"{outline.width:g}px solid {outline.colour}",
            "outline-offset": f"{outline.offset:g}px",
        },
        indent,
    )


def _component_rules(
    w: _Writer, resolver: Resolver, ground: Ground, indent: str, scope: str | None
) -> None:
    """The seven component styles, each in all four states, colour AND geometry.

    ST1 lives in `Resolver.surface_of`; this only renders what it returns. The
    border ladder is engaged by `.ds-bordered`, which is his "outlined" button
    -- the `none` rung is the default because most components have no border,
    and the hover/focus rungs are why the ladder is a ladder of SIZES.
    """
    palette_of = resolver.palette(ground)
    for style in COMPONENT_STYLES:
        s = resolver.surface_of(ground, style)
        base = f".ds-{style}"
        sc = lambda sel: _scoped(sel, scope)  # noqa: E731

        # THE STATE RULES ADDRESS THE COMPONENTS AS WELL AS THE CLASS. See
        # `_STYLE_SELECTORS` for the measured defect and the whole mapping.
        # `state("hover")` is `.ds-solid:hover, [data-slot=…]:hover, …`.
        #
        # EACH STATE IS PAIRED WITH ITS DEMO TWIN, and the pairing is the whole
        # mechanism behind a library view that cannot drift. A page that wants to
        # SHOW what hover looks like has two options: duplicate the hover
        # declarations into a showcase stylesheet, or force the state. The first
        # is a second source of truth that goes stale the day a hover value
        # changes and nobody notices, because the specimen keeps rendering the
        # old answer confidently. So `:hover` and `[data-demo="hover"]` are
        # written into ONE rule here: there is exactly one declaration block, and
        # a specimen is painted by the same bytes that paint a real cursor.
        # Measured need: across all 33 specimens the scene had 0 forced states,
        # which is why every state defect he found had to be found by hovering
        # one element at a time.
        def state(pseudo: str) -> str:
            heads = (base, *_STYLE_SELECTORS.get(style, ()))
            parts = [sc(f"{head}{pseudo}") for head in heads]
            demo = _DEMO_TWIN.get(pseudo)
            if demo:
                parts += [sc(f"{head}{demo}") for head in heads]
            return ", ".join(parts)

        # THE REST RULE IS THE ONE THAT STAYS CLASS-ONLY, and that is a
        # containment decision, not an oversight. It writes `border-width: 0`,
        # which on `[data-slot="input"]` or the `outline` button would DELETE a
        # border the component means to have -- and the rest colours already
        # arrive through the tier-S bridge (`bg-primary`, `bg-secondary`,
        # `bg-background`), which the adapter fills from this same resolver. The
        # dead half was never the rest state; it was the three states below.
        w.rule(
            sc(base),
            {
                "background-color": s.fill,
                "color": s.ink,
                "border-color": s.border,
                "border-style": "solid",
                "border-width": "0",
            },
            indent,
        )
        # Emission order is hover -> focus -> active. The state selectors share
        # a specificity, so source order decides which one a browser uses when
        # several match at once; a keyboard press holds `:focus-visible` and
        # `:active` at the same time, and "PRESSED" is the state the finger or
        # the key is in, so it is written last.
        w.rule(
            state(":hover"),
            {"background-color": s.hover.colour, "color": s.hover.ink},
            indent,
        )
        # RULING 5: focus is a RING OUTSIDE the box, not a repaint. It declares
        # no background and no colour, so a button that is focused AND hovered
        # keeps the hover fill and wears the ring -- which is what the two
        # states mean together, and what the old ALTERNATE-INVERSE.030 fill
        # could not express.
        #
        # `:focus-visible`, NEVER BARE `:focus` -- his ruling of 2026-08-19:
        # "the focus ring must appear ONLY on keyboard focus, never on mouse
        # click/:active". Measured on the deployed page before this line changed:
        # a mouse-down on the hero's branded CTA painted a 4px solid #030303
        # outline while `:focus-visible` was false on the same element -- the ring
        # answering a finger, which is what he saw. The browser owns the heuristic
        # and states it in exactly this pseudo-class; re-deriving it from
        # pointer events would be a second predicate for a question CSS answers.
        #
        # THIS ONE KEEPS THE CLASS SELECTOR ALONE while hover and active gained
        # attribute selectors, because the ring does not vary by style at all --
        # `_focus_ring_rule` emits the identical declaration for every focusable
        # element on this ground, once. Repeating it per style would be seven
        # copies of one answer, which is how a system grows a second answer.
        outline = s.focus.outline
        assert outline is not None
        w.rule(
            sc(f"{base}:focus-visible"),
            {
                "outline": f"{outline.width:g}px solid {outline.colour}",
                "outline-offset": f"{outline.offset:g}px",
            },
            indent,
        )
        w.rule(
            state(":active"),
            {"background-color": s.pressed.colour, "color": s.pressed.ink},
            indent,
        )
        # geometry -- "BORDER {none, normal, hover, focus}"
        #
        # THE LADDER IS PAINTED AS AN INSET RING, NOT AS A REAL BORDER, and that
        # is his ruling of 2026-08-18: "visibility changes must never move the
        # component".
        #
        # WHAT IT WAS. These rules wrote `border-width` per state, so the `normal`
        # -> `hover` rung was a real 1px -> 2px border change. Measured on the
        # deployed page: the hero's branded CTA went from 99px wide to 101px on
        # hover and pushed its neighbour 2px right. Height held only because
        # `.ds-container-size` pins it -- width had nothing pinning it.
        #
        # WHY AN INSET SHADOW IS THE ANSWER AND NOT A RESERVE. Every layout
        # property that could hold a reserve is already owned by a size role:
        # `margin` by `.ds-margin`, `padding` by `.ds-padding`, and a state rule
        # here outranks all of them on specificity, so reserving with one would
        # silently clobber the brand's own spacing. `outline` is taken by the
        # focus ring (ruling 5). An INSET box-shadow is the one paint property
        # left, and it is layout-neutral BY CONSTRUCTION -- it is drawn inside the
        # padding box and contributes nothing to any box model. With
        # `border-width: 0` from the base rule above, the padding-box edge IS the
        # element's outer edge, so the ring lands exactly where a border would and
        # grows INWARD as the ladder climbs.
        #
        # So the box never changes and the widest rung is always accommodated:
        # the reserve is the element itself. The ladder still changes WIDTH
        # (register rule 9 -- "geometry, not only colour"); what it can no longer
        # change is anyone's position.
        def ring(width: float, colour: str) -> str:
            return f"inset 0 0 0 {width:g}px {colour}"

        w.rule(
            sc(f"{base}.ds-bordered"),
            {"box-shadow": ring(s.normal.border_width, s.border)},
            indent,
        )
        w.rule(
            sc(f"{base}.ds-bordered:hover") + ", " + sc(f'{base}.ds-bordered{_DEMO_TWIN[":hover"]}'),
            {"box-shadow": ring(s.hover.border_width, s.border)},
            indent,
        )
        # Focus does not move the component's own border since ruling 5 -- the
        # `focus` rung of the ladder sizes the ring instead. Emitted anyway, and
        # equal to `normal` on purpose: the rule states that it does not move.
        # `:focus-visible` for the same reason as the ring above: the two rules
        # are one state and a bare `:focus` here would keep the state half
        # pointer-triggered even after the ring stopped being.
        w.rule(
            sc(f"{base}.ds-bordered:focus-visible") + ", " + sc(f'{base}.ds-bordered{_DEMO_TWIN[":focus-visible"]}'),
            {"box-shadow": ring(s.focus.border_width, s.border)},
            indent,
        )
        w.rule(
            sc(f"{base}.ds-bordered:active") + ", " + sc(f'{base}.ds-bordered{_DEMO_TWIN[":active"]}'),
            {"box-shadow": ring(s.pressed.border_width, s.border)},
            indent,
        )
        w.rule(
            sc(f"{base}.ds-bordered-branded"),
            {"box-shadow": ring(s.normal.border_width, s.border_branded)},
            indent,
        )
        # ELEVATION AND A BORDER ON ONE ELEMENT. Both are box-shadow now, and a
        # shadow list is not additive across rules -- the later, more specific
        # rule would win and silently delete the other. Rule 8 already treats the
        # two as substitutes ("dark grounds where a shadow cannot separate ->
        # BORDER"), but a sheet is a contract and an element may carry both. So
        # the combination is emitted ONCE, with both layers in the one
        # declaration, rather than left to source order.
        # EMITTED IN EVERY BLOCK, on every ground, whichever answer elevation
        # got. A ground where the shadow became a BORDER (rule 8) has no drop
        # layer to add, and skipping the selector there would make two blocks
        # carry different selector SETS -- which is exactly the leak
        # "complete palette per boundary" exists to stop, and the sheet-invariant
        # test caught it. So the selector is always present; only the number of
        # layers in it changes.
        for elevated, answer, effect_name in (
            (".ds-elevated", palette_of.shadow, "normal"),
            (".ds-elevated-blurred", palette_of.blur_and_shadow, "light"),
        ):
            layers: list[str] = []
            if answer.kind == "shadow":
                effect = resolver.brand.effects.by_name(effect_name).px()
                layers.append(
                    f"{effect.shadow_x:g}px {effect.shadow_y:g}px "
                    f"{effect.shadow_spread:g}px "
                    f"{_rgba(answer.colour, round(answer.opacity, 4))}"
                )
            for state, width in (
                ("", s.normal.border_width),
                (":hover", s.hover.border_width),
            ):
                w.rule(
                    sc(f"{base}.ds-bordered{elevated}{state}"),
                    {"box-shadow": ", ".join([ring(width, s.border), *layers])},
                    indent,
                )


def _boundary_rules(
    w: _Writer, resolver: Resolver, parent: Ground, indent: str, scope: str | None
) -> None:
    """Every request, resolved against this parent ground.

    This is `Resolver.ground(request, parent)` frozen into CSS. The element
    publishes its new ground for its subtree AND paints itself with that new
    ground's background and foreground, because the request is a statement
    about the element's own surface:

        "if the main-theme is light, and there is a component embedded that
        needs a hight contrast, it should flip to the alternating pair of
        light, which is dark."

    A signal request resolves to the same ground from every parent, and it is
    still emitted inside every parent's block. That repetition IS the
    uniformity: the emitter never asks what kind of ground it is looking at, so
    it cannot hoist the signal case out.
    """
    for selector, request in requests_of(resolver.brand):
        child = resolver.ground(request, parent)
        palette = resolver.palette(child, mother=parent)
        assert palette.background is not None
        w.rule(
            _scoped(selector, scope, attach=True),
            {
                "--ground": ground_key(child),
                "background-color": palette.background,
                "color": palette.foreground,
                # THE ADAPTER'S REFILL, and it has to be in THIS declaration.
                # A style query never matches the element that publishes the
                # property, so a boundary still matches its PARENT's block: a
                # blanket fill there would hand it the parent's palette with
                # every name present. Measured, 3 of 4 wrong. See adapter.py.
                **adapter.ground_declarations(resolver.brand, palette),
            },
            indent,
        )


def _root_rules(w: _Writer, resolver: Resolver) -> None:
    """The ground with no surround, and the only user-facing switch.

        "On the UI a user can only chose dark / light. signal and brand are
        design facts. But they can be placed on the highest element"

    So the root takes an appearance, and it also takes the design facts --
    branded and every signal -- because a brand or a signal at the root is a
    normal case and not an edge. A brand at the root takes CANONICAL: "there is
    no reference color around the brand color."
    """
    brand = resolver.brand
    w.rule("html", {"font-size": f"{scale.ROOT_PERCENT:g}%"})
    # THE DOCUMENT'S OWN TEXT SIZE, and it is the fix for "CTA texts way too
    # small". The root is 50 %, so `html` computes to 8px BY DESIGN -- 8 is the
    # base, and that identity is what makes a rem the factor (register rule 10).
    # But it also means any element that never named a text style inherited 8px
    # of rendered type. Measured on the deployed page 2026-08-18: all four
    # canvas CTAs rendered at 8px, because a button carries `font: inherit` and
    # the sheet gave `body` a family and a weight and no size.
    #
    # So `body` states the NORMAL band's body cell at the brand's default rung.
    # It is not a new number: it is the same `N5` cell the adapter's `body` role
    # already reads, off the same table, so unclassed text lands on the system's
    # own paragraph rather than on the base constant. A `.ds-*` class still wins
    # over it -- this is the floor, not an override.
    body_rung = brand.defaults.rung.desktop
    w.rule(
        "body",
        {
            "margin": "0",
            "font-family": f'"{brand.font.family}", {brand.font.stack}',
            "font-weight": str(brand.font.numeric(brand.weights.paragraphs)),
            "font-size": _rem(sizing.text_size(brand, body_rung, BODY_STYLE)),
            "line-height": f"{schema.line_height(BODY_STYLE):g}",
        },
    )

    roots: list[tuple[str, Ground]] = [
        (":root", resolver.root("light")),
        (':root[data-appearance="dark"]', resolver.root("dark")),
        (":root[data-branded]", resolver.root("light", Request(branded=True))),
    ]
    for role in brand.signals.by_role():
        roots.append(
            (
                f':root[data-signal="{role}"]',
                resolver.root("light", Request(signal=role)),
            )
        )
    # The consumer's ground-independent half, once. `:root` has no ancestor
    # container and therefore matches NO style query -- measured -- so it is the
    # one selector that must carry an explicit fill rather than inherit one.
    # Sizes, radii, fonts and motion do not move with the ground, so they are
    # written here and inherited; the ground half is written per root selector
    # below, because each of those roots IS a different ground.
    system = adapter.system_declarations(brand)

    for i, (selector, ground) in enumerate(roots):
        palette = resolver.palette(ground)
        assert palette.background is not None
        w.rule(
            selector,
            {
                "--ground": ground_key(ground),
                "background-color": palette.background,
                "color": palette.foreground,
                **adapter.ground_declarations(brand, palette),
                **(system if i == 0 else {}),
            },
        )


def _additional_rules(w: _Writer, brand: Brand, scope: str | None) -> None:
    """The brand's additional colours, and the foreground computed for each.

    GROUND-INDEPENDENT, on purpose. These are a palette a consumer reaches into
    -- "Freely usable in the interface; intended uses incl. presentation slides,
    charts" -- so their value does not depend on what they are placed on. What a
    ground WOULD change is the foreground, and it does not need to: the pair
    travels together, so anything painting `--additional-3` also has the ink that
    belongs on it, by the one threshold rule.

    Names are POSITIONAL. A label is the builder's, and a renamed label must not
    repoint a var() somebody already wrote.
    """
    rows = brand.additional_colours()
    if not rows:
        return
    decls: dict[str, str] = {}
    for row in rows:
        decls[f"--{row['name']}"] = row["value"]
        decls[f"--{row['name']}-foreground"] = row["foreground"]
    w.comment("--- the brand's additional colours, each with its computed ink ----")
    # A GUEST declares them on its own entry element, never on `:root` -- there is
    # only one root and it belongs to the host. Custom properties inherit, so the
    # guest's subtree reads the guest's palette and nothing above it changes.
    w.rule(scope or ":root", decls)


def _portal_rules(
    w: _Writer, resolver: Resolver, grounds: list[Ground], scope: str | None
) -> None:
    """The path out of the tree.

        "that's something that needs to be solved systematically" (d8)

    Systematically means: the ground is a property of the element, not of where
    it happens to be rendered. An element that leaves the tree carries
    `data-ground="g-xxxxxx"`, which re-publishes that ground and repaints the
    element, so a portalled overlay is indistinguishable from an in-tree one.

    The other path needs no rule at all and is emitted by being absent: an
    unattributed portal lands under `body`, inherits `--ground` from `:root`
    because custom properties inherit through the whole document, and therefore
    resolves against the ROOT ground. Both paths are gated in the browser test;
    this comment is the second one's implementation.
    """
    for ground in grounds:
        palette = resolver.palette(ground)
        assert palette.background is not None
        w.rule(
            _scoped(f'[data-ground="{ground_key(ground)}"]', scope, attach=True),
            {
                "--ground": ground_key(ground),
                "background-color": palette.background,
                "color": palette.foreground,
                # A portal is out of the tree, so it inherits the ROOT's names.
                # Refilling them here is what makes a portalled overlay
                # indistinguishable from an in-tree one for the consumer
                # vocabulary too, not only for `--ground`.
                **adapter.ground_declarations(resolver.brand, palette),
            },
        )


def _size_rules(w: _Writer, brand: Brand) -> None:
    """Sizes, and SCALES as an axis of the sheet.

        "Exist, so that one switch downsizes everything based on their current
        mode. A component that has the size L is immediately changed to size
        XS, while a text of size S is changed to XS."

    One switch, `[data-scale="down"]`, and the two mappings are different --
    text moves one rung, components three -- so a single attribute produces
    both of his numbers without anything else in the page changing.

    Rungs are carried by `[data-rung]` because size inherits by FRAME: "as long
    as the children have no sizing overrides, they consume the parents size
    configuration". A descendant selector is exactly that inheritance.

    Emission order is load-bearing: the base rung and the scaled base rung
    share a specificity, so the later one wins, and the explicit-rung rules
    outrank both.
    """
    def text_decls(rung: str, style: str) -> dict[str, str]:
        """A text style's SIZE AND ITS LINE BOX, together.

        THE LINE HEIGHT USED TO BE MISSING HERE, and that was the whole of the
        defect. The sheet published `--type-{role}-line` on `:root` for the
        adapter's seven roles and this function wrote `font-size` alone, so every
        one of the 21 `.ds-*` classes rendered at `line-height: normal` -- the
        UA's ~1.2 on a paragraph the system says is 1.6. Measured on the deployed
        page 2026-08-18: zero occurrences of `line-height` in the emitted sheet.

        Unitless, so it is inherited as a RATIO and re-multiplies against
        whatever size the rung gives -- which is why one declaration serves all
        8 rungs and both scale states without a second table.
        """
        return {
            "font-size": _rem(sizing.text_size(brand, rung, style)),
            "line-height": f"{schema.line_height(style):g}",
        }

    _COMPONENT_PROPERTY = {
        "container-size": ("height",),
        "container-size-double": ("width",),
        "gap-half": ("gap",),
        "gap": ("gap",),
        "gap-double": ("gap",),
        "margin": ("margin",),
        "margin-double": ("margin",),
        "padding-double": ("padding",),
        "padding": ("padding",),
        "padding-half": ("padding",),
        "icon-size": ("width", "height"),
        "icon-size-double": ("width", "height"),
        "icon-size-smaller": ("width", "height"),
        "negative-adjustment": ("margin",),
        "icon-subtraction": ("margin",),
        "icon-smaller-subtraction": ("margin",),
    }

    # A PADDING CLASS ALSO PUBLISHES THE DISTANCE IT CREATES. `RadiusSettings.
    # inner()` is `outer - distance`, and `distance` is the padding between a
    # container and the element nested in it. CSS cannot read an ancestor's
    # `padding` back, only a custom property, so the class that SETS the padding
    # is the only place that can say what it is -- exactly how a ground class
    # publishes `--ground` for descendants that need to resolve against it.
    _GAP_PUBLISHERS = frozenset({"padding", "padding-half", "padding-double"})

    def component_decls(rung: str, role: str) -> dict[str, str]:
        value = sizing.component_size(brand, rung, role)
        decls = {prop: _rem(value) for prop in _COMPONENT_PROPERTY[role]}
        if role in _GAP_PUBLISHERS:
            decls["--ds-corner-gap"] = _rem(value)
        return decls

    # THE BRAND'S OWN DEFAULT RUNG, per viewport class (his ruling of
    # 2026-08-18). It replaces two hard-coded "L"s. The table is still the
    # engine's and still not editable -- this is a POINTER into it, which is the
    # only sizing choice a brand kept when the mother table was locked.
    #
    # ONE RUNG DRIVES BOTH FAMILIES. `[data-rung="RUNG"]` already prefixes the
    # text classes and the component classes, so naming a rung sizes type and
    # components together -- the frame inheritance of rule 10 rather than two
    # independent defaults.
    default_text = brand.defaults.rung.desktop
    default_component = brand.defaults.rung.desktop

    def scaled_frame(rung: str, cls: str) -> str:
        """The scaled form of one frame rung.

        Two selectors, because the switch and the rung can sit on the SAME
        element or on different ones -- "One drag of a component into another
        frame with sizing settings, changes the size of the component". A page
        that scales a frame it already sized is the ordinary case, and it must
        not fall through to the unscaled rule.
        """
        return (
            f'[data-scale="down"] [data-rung="{rung}"] {cls}, '
            f'[data-scale="down"][data-rung="{rung}"] {cls}'
        )

    w.comment("text: 8 rungs x 21 styles, every value off the grid")
    for style in sizing.TEXT_STYLES:
        w.rule(f".ds-{style.lower()}", text_decls(default_text, style))
    for style in sizing.TEXT_STYLES:
        w.rule(
            f'[data-scale="down"] .ds-{style.lower()}',
            text_decls(sizing.downscale_text(default_text), style),
        )
    for rung in TEXT_RUNGS:
        for style in sizing.TEXT_STYLES:
            w.rule(f'[data-rung="{rung}"] .ds-{style.lower()}', text_decls(rung, style))
    for rung in TEXT_RUNGS:
        for style in sizing.TEXT_STYLES:
            w.rule(
                scaled_frame(rung, f".ds-{style.lower()}"),
                text_decls(sizing.downscale_text(rung), style),
            )

    # THE CONSUMER VOCABULARY FOLLOWS THE FRAME TOO. `--font-size-*` and
    # `--type-*-size` are cells of the same text table, so a frame that names a
    # rung has to re-declare them for its subtree or okuro's own components stop
    # following the frame the `.ds-*` classes already follow. Custom properties
    # inherit, so one declaration on the frame reaches every descendant --
    # measured: without this, the scene rung took the specimen's body 16 -> 20px
    # at XXL while a real `Button` stayed at 14px.
    w.comment("the consumer size names, per rung -- so a framed rung reaches okuro's own components")
    for rung in TEXT_RUNGS:
        w.rule(f'[data-rung="{rung}"]', adapter.rung_size_declarations(brand, rung))
    for rung in TEXT_RUNGS:
        w.rule(
            f'[data-scale="down"] [data-rung="{rung}"], '
            f'[data-scale="down"][data-rung="{rung}"]',
            adapter.rung_size_declarations(brand, sizing.downscale_text(rung)),
        )

    # AND THE COMPONENT TABLE NEEDS A CONSUMER NAME OF ITS OWN. The `.ds-{role}`
    # rules below publish it as utility classes, and measured on 2026-08-19: 0 of
    # 33 vendored primitives carry one, so the entire component axis reached
    # nothing. `--spacing-*` IS that name — every Tailwind geometry utility in the
    # bundle already compiles to `calc(var(--spacing-xs) * N)`.
    #
    # A SEPARATE LOOP FROM THE TEXT ONE ABOVE, and deliberately so: this is the
    # 9-rung component ladder, not the 8-rung text ladder, and SCALES shifts it by
    # three rungs rather than one. Sharing the text loop would silently drop XXXXS
    # and scale the geometry by the text shift.
    w.comment("the app's spacing scale, re-based per component rung -- the consumer name for the component table")
    for rung in COMPONENT_RUNGS:
        w.rule(
            f'[data-rung="{rung}"]', adapter.rung_component_declarations(brand, rung)
        )
    for rung in COMPONENT_RUNGS:
        w.rule(
            f'[data-scale="down"] [data-rung="{rung}"], '
            f'[data-scale="down"][data-rung="{rung}"]',
            adapter.rung_component_declarations(
                brand, sizing.downscale_component(rung)
            ),
        )

    w.comment("components: 9 rungs x 16 roles, the same switch, a different mapping")
    for role in sizing.COMPONENT_ROLES:
        w.rule(f".ds-{role}", component_decls(default_component, role))
    for role in sizing.COMPONENT_ROLES:
        w.rule(
            f'[data-scale="down"] .ds-{role}',
            component_decls(sizing.downscale_component(default_component), role),
        )
    for rung in COMPONENT_RUNGS:
        for role in sizing.COMPONENT_ROLES:
            w.rule(f'[data-rung="{rung}"] .ds-{role}', component_decls(rung, role))
    for rung in COMPONENT_RUNGS:
        for role in sizing.COMPONENT_ROLES:
            w.rule(
                scaled_frame(rung, f".ds-{role}"),
                component_decls(sizing.downscale_component(rung), role),
            )

    # THE BRAND'S MOBILE DEFAULT RUNG. His 2026-08-18 ruling gives a brand two
    # defaults, and the second one only means anything if the sheet acts on it.
    #
    # It re-declares the UNPREFIXED rules at the mobile rung, so it moves exactly
    # what the desktop default set and nothing else: a frame that named its own
    # `[data-rung]` has specificity (0,1,1) against this block's (0,1,0) and
    # keeps its rung at every width. That is the frame rule of register 10 --
    # an explicit rung is an override, and a default is what you get without one.
    if brand.defaults.rung.mobile != brand.defaults.rung.desktop:
        w.blank()
        w.comment(
            f"the brand's MOBILE default rung ({brand.defaults.rung.mobile}); an "
            "explicit [data-rung] frame still wins on specificity"
        )
        w.open_block(f"@media (max-width: {scale.MOBILE_MAX_PX}px)", counts=False)
        mobile = brand.defaults.rung.mobile
        # The document floor moves with the default too, or unclassed text would
        # keep the desktop rung while everything named moved.
        w.rule(
            "body",
            {"font-size": _rem(sizing.text_size(brand, mobile, BODY_STYLE))},
            "  ",
        )
        # and the consumer size names, for the same reason they are emitted per
        # rung above: okuro's own components read these, not the `.ds-*` classes.
        w.rule(":root", adapter.rung_size_declarations(brand, mobile), "  ")
        w.rule(":root", adapter.rung_component_declarations(brand, mobile), "  ")
        for style in sizing.TEXT_STYLES:
            w.rule(f".ds-{style.lower()}", text_decls(mobile, style), "  ")
        for style in sizing.TEXT_STYLES:
            w.rule(
                f'[data-scale="down"] .ds-{style.lower()}',
                text_decls(sizing.downscale_text(mobile), style),
                "  ",
            )
        for role in sizing.COMPONENT_ROLES:
            w.rule(f".ds-{role}", component_decls(mobile, role), "  ")
        for role in sizing.COMPONENT_ROLES:
            w.rule(
                f'[data-scale="down"] .ds-{role}',
                component_decls(sizing.downscale_component(mobile), role),
                "  ",
            )
        w.close_block()

    # A RADIUS CLASS PUBLISHES ITS OWN CORNER, so a nested element can compute
    # `inner = outer - distance` (P0 q3, `RadiusSettings.inner`) without naming
    # its container. `border-radius` is not readable back out of a computed
    # style into a custom property, so the rule that sets the corner is the only
    # place the number can be published from -- the `--ground` pattern, with two
    # published numbers instead of one because the formula needs a distance too.
    # `.ds-radius-inner` (globals.css) is the consumer; MAX publishes 9999px like
    # any other corner, and a pill minus a distance is still a pill.
    w.comment('"RADIUS {S, M, L, MAX}", and inner = outer - distance (P0 q3)')
    for rung in ("S", "M", "L", "MAX"):
        outer = brand.radius.outer(rung)
        value = "9999px" if outer >= scale.MAX_SENTINEL else _rem(outer)
        w.rule(
            f".ds-radius-{rung.lower()}",
            {"border-radius": value, "--ds-corner": value},
        )

    # THE BAND CLASS CARRIES BOTH OF ITS AUTHORED FACTS. Weight was already here;
    # CASE joins it (his ruling of 2026-08-19, `schema.CaseAssignment`) rather than
    # going onto the 21 `.ds-{style}` size rules, and the reason is where the page
    # puts the classes: every specimen in the canvas is written `ds-h1 ds-headings`
    # / `ds-n4 ds-leads`, size class beside band class. Emitting case per SIZE rule
    # would also mean 8 rungs x 21 styles x 2 scales copies of a value that does
    # not move with the rung -- and a brand whose weight is keyed by band while its
    # case is keyed by style is a system with two type vocabularies again.
    w.comment("type bands: the authored weight slot and the authored case, per band")
    for band in schema.TYPE_BANDS:
        slot = getattr(brand.weights, band)
        w.rule(
            f".ds-{band}",
            {
                "font-weight": str(brand.font.numeric(slot)),
                "text-transform": brand.case.css(band),
            },
        )

    w.comment("motion: speeds and curves are authored; a curve is a brand signature")
    for name, ms in brand.motion.speeds.items():
        w.rule(f".ds-speed-{name}", {"transition-duration": f"{ms}ms"})
    curves = dict(brand.motion.curves)
    if brand.motion.signature:
        curves["signature"] = brand.motion.signature
    for name, curve in curves.items():
        w.rule(f".ds-curve-{name}", {"transition-timing-function": curve})


# ---------------------------------------------------------------------------
# The whole sheet
# ---------------------------------------------------------------------------


def _brand_blocks(
    w: _Writer,
    resolver: Resolver,
    grounds: list[Ground],
    scope: str | None,
) -> None:
    """One `@container` block per reachable ground, for one brand.

    The block is keyed on "the nearest ancestor publishes G", and BOTH kinds of
    rule inside it answer to that same key -- a palette rule because the element
    SITS on G, a boundary rule because its parent's ground IS G. They are one
    block precisely because they are one question, which is why the sheet needs
    G blocks and not G x R.
    """
    for ground in grounds:
        w.blank()
        w.open_block(f"@container style(--ground: {ground_key(ground)})")
        _palette_rules(w, resolver.brand, resolver.palette(ground), "  ", scope)
        w.comment("  the seven component styles, four states each (ST1)")
        _component_rules(w, resolver, ground, "  ", scope)
        w.comment("  the other three quarters of a signal surface: hover, pressed")
        _signal_state_rules(w, resolver, ground, "  ", scope)
        w.comment("  a field states itself with its border, and never with a press")
        _field_state_rules(w, resolver, ground, "  ", scope)
        w.comment("  ruling 5: ONE focus ring, for everything focusable on this ground")
        _focus_ring_rule(w, resolver, ground, "  ", scope)
        w.comment("  boundaries: every request, resolved against this ground")
        _boundary_rules(w, resolver, ground, "  ", scope)
        w.close_block()


def emit(
    brand: Brand,
    *,
    font_sources: dict[str, str] | None = None,
    guests: tuple[Brand, ...] = (),
) -> Sheet:
    """Render a brand's whole system as one static sheet.

    `font_sources` maps a face descriptor to a URL. It is authored by the kit
    and not discovered here: the emitter does no I/O, so a sheet is a pure
    function of a brand and reproduces byte for byte.

    `guests` are brands nested INSIDE this one -- the case he insists the system
    must express: "we have nested brands inside light / dark or even branded
    themes ([host] theme as the main theme and UBS, Valiant, Mobiliar etc, as
    nested themes)". Each guest gets its own blocks, scoped to
    `[data-brand="<id>"]` and seeded with the host's grounds, so a guest
    resolves its brand colour against whatever host ground it lands on -- which
    is how one falls to brand-full and another to the neutral fallback with a
    branded CTA, from the same rule.
    """
    resolver = Resolver(brand)
    grounds = reachable_grounds(resolver)
    w = _Writer()

    w.comment("okuro design engine -- generated sheet. Do not edit.")
    w.comment(f"brand: {brand.id}")
    w.comment("Every colour below came out of the resolver. This file renders; it")
    w.comment("does not compute. The one custom property is --ground.")
    w.blank()

    faces = _font_face_rules(brand, font_sources)
    if faces:
        w.comment("the family's own faces -- the descriptor becomes the number here")
        w.lines.extend(faces)
        w.rules += len(faces)
        w.blank()

    w.comment('the root: "BASE IS 10px ... Calculation is done on 62,5%"')
    _root_rules(w, resolver)
    w.blank()

    _additional_rules(w, brand, scope=None)
    w.blank()

    w.comment("--- one block per reachable ground ------------------------------")
    w.comment("the key is the ground's COLOUR, so no case split can exist here")
    _brand_blocks(w, resolver, grounds, scope=None)

    ground_keys = [ground_key(g) for g in grounds]
    flags: list = [f for g in grounds for f in resolver.palette(g).flags]

    for guest in guests:
        guest_resolver = Resolver(guest)
        guest_grounds = reachable_grounds(guest_resolver, extra_seeds=tuple(grounds))
        scope = f'[data-brand="{guest.id}"]'
        w.blank()
        w.comment(f"--- guest brand {guest.id}: its own system, inside this one ---")
        _additional_rules(w, guest, scope=scope)
        _brand_blocks(w, guest_resolver, guest_grounds, scope=scope)
        w.blank()
        _portal_rules(w, guest_resolver, guest_grounds, scope=scope)
        ground_keys.extend(ground_key(g) for g in guest_grounds)
        flags.extend(f for g in guest_grounds for f in guest_resolver.palette(g).flags)

    w.blank()
    w.comment("--- portals: the ground travels on the element -------------------")
    _portal_rules(w, resolver, grounds, scope=None)

    w.blank()
    w.comment("--- sizes, radius, weights, motion -------------------------------")
    _size_rules(w, brand)

    return Sheet(
        css=w.text(),
        ground_keys=tuple(dict.fromkeys(ground_keys)),
        container_blocks=w.blocks,
        rules=w.rules,
        flags=tuple(flags),
    )
