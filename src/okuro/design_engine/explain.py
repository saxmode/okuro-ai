# SPDX-License-Identifier: Apache-2.0
"""Why a value is what it is -- the provenance layer the growth UI reveals.

    "Each derived value can reveal its rule -- hover/click shows what computed
    it and from which inputs."

THIS MODULE COMPUTES NOTHING. Every value it reports is READ out of a
:class:`~okuro.design_engine.resolve.Palette` the resolver already produced.
That is the whole discipline here: an explainer that recomputed a value would
be a second opinion about a decision the resolver has already made, and the two
would drift the first time a rule changed. What this module adds is the
*sentence* -- the rule that produced the value, the slots that rule read, and a
one-line reading of the measurement that decided it.

The rule texts come from the RULING REGISTER (artifact 7108d22d) and the note
OKURO-DS. They are documentation, not logic: nothing branches on a rule id.

Two shapes come out:

    derivations()  a flat list, one row per derived slot -- what the UI shows
                   beside a value when you ask it where it came from.
    graph()        the same rows as a DAG over authored inputs and derived
                   slots -- what the UI draws as the descent.

`COVERED` names every `Palette` field a derivation exists for.
``test_explain.py`` asserts that set equals the dataclass's own fields, so a
field added to the palette without an explanation fails the suite instead of
appearing in the UI with no rule behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from . import colour as c
from . import scale
from .resolve import Ground, Palette, Request, Resolver
from .schema import Brand

# ---------------------------------------------------------------------------
# The rule catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One rule of the system, as a human reads it.

    `formula` is the mechanism in the smallest form that is still exact.
    `source` says which authority states it, so a reader can go check.
    """

    id: str
    title: str
    formula: str
    source: str


RULES: dict[str, Rule] = {
    "polarity": Rule(
        id="polarity",
        title="Polarity -- the one constant",
        formula=(
            f"polarity(colour) = L(colour) < {c.POLARITY_THRESHOLD} ? dark : light\n"
            "L = OKLab lightness, 0..1\n\n"
            "It decides three things and nothing else decides them:\n"
            "  which neutral is the foreground\n"
            "  which direction a ladder travels\n"
            "  which way hover moves"
        ),
        source=(
            "register rule 2 -- study-fitted, re-ratified on the ink evidence, "
            "2026-08-19"
        ),
    ),
    "foreground": Rule(
        id="foreground",
        title="Foreground -- the brand's own neutral, picked by polarity",
        formula=(
            "foreground(ground) = polarity(ground) == dark\n"
            "                       ? brand.neutrals.white\n"
            "                       : brand.neutrals.black"
        ),
        source=(
            "note THEME.COLORS.BASE.FOREGROUND -- the one authored value in an "
            "otherwise computed palette; short contrast is FLAGGED, never corrected"
        ),
    ),
    "descent": Rule(
        id="descent",
        title="Descent -- one value passes down, the resolved ground",
        formula=(
            "no request  -> inherit the parent's ground\n"
            "emphasis    -> the neutral of the OPPOSITE polarity\n"
            "branded     -> rule `branded-ground`\n"
            "signal      -> that signal's colour, at any depth including the root\n\n"
            "Recursion alternates forever. There is no depth counter."
        ),
        source="register rule 3",
    ),
    "branded-ground": Rule(
        id="branded-ground",
        title="Branded ground -- brand-full, or the fallback",
        formula=(
            "on a NEUTRAL ground (one of the brand's own two):\n"
            "    brand-full  iff polarity(adaptation) != polarity(ground)\n"
            "    else        the fallback\n\n"
            "on a CHROMATIC ground (anything else): ALWAYS the fallback\n\n"
            "THE FALLBACK = the neutral of the polarity OPPOSITE to the\n"
            "host, carrying CTA = brand.on_{polarity of that fallback}"
        ),
        source="register rule 4 -- brand-semi does not exist",
    ),
    "branded-figure": Rule(
        id="branded-figure",
        title="Branded figure -- the adaptation, or a neutral",
        formula=(
            "on a NEUTRAL ground: brand.on_{polarity(ground)}, unconditionally\n"
            "on any other ground: the neutral polarity(ground) picks\n\n"
            "The ADJUSTMENT is the contrast mechanism. Weak contrast is a lint\n"
            "flag; it never degrades or rejects the colour."
        ),
        source="register rules 1 and 5",
    ),
    "ladder": Rule(
        id="ladder",
        title="The ladders -- 17 steps of one ink over the ground",
        formula=(
            "alternate         = the FOREGROUND at 17 steps\n"
            "alternate-inverse = the OPPOSITE of the foreground, same steps\n\n"
            "100 97 95 90 80 70 60 50 40 30 20 10 5 3 1 0.1 0\n\n"
            "Each rung is composited over the ground and emitted as the\n"
            "resolved literal."
        ),
        source="register rule 6 -- the 17 values stand; Figma's calculation was wrong",
    ),
    "ladder-rung": Rule(
        id="ladder-rung",
        title="A named value that is one rung of a ladder",
        formula="value = composite(ladder.ink, percent/100, ground)",
        source="note BASE.COLORS.ALPHA + the note's own derived list",
    ),
    "signals": Rule(
        id="signals",
        title="Signals -- authored grounds, computed foregrounds",
        formula=(
            "four authored: red(error) green(success) blue(info) pink(warning)\n"
            "foreground(signal) = the SAME rule every other ground runs\n\n"
            "A signal ground is not privileged anywhere in the system."
        ),
        source="register rule 7 -- never orange",
    ),
    "shadow": Rule(
        id="shadow",
        title="Shadows -- always black, only the opacity moves",
        formula=(
            "solve alpha so a black veil reproduces the anchor's OKLab\n"
            "darkening on THIS ground:\n"
            "    anchors 20% solid / 10% blurred, measured on white\n\n"
            "If full opacity still cannot reach it, the answer is a BORDER."
        ),
        source="register rule 8",
    ),
    "states": Rule(
        id="states",
        title="States -- colour and geometry both move",
        formula=(
            "hover        +/-20% measured on the COMPONENT'S OWN ground\n"
            "             (dark lightens, light darkens)\n"
            "pressed      the surface's background and foreground EXCHANGE\n"
            "             roles; the side landing on the brand is re-expressed\n"
            "             as on-light/on-dark (rule 5). A pair carrying no\n"
            "             brand keeps highlight-branded-background, gated on\n"
            "             contrast against the MOTHER element's ground\n"
            "pressed-brand the BRANDED surface's own foreground -- which is the\n"
            "             exchange read on that surface rather than on the page\n"
            "focus        alternate-normal/100 (= the foreground) drawn as an\n"
            "             OUTLINE OUTSIDE the component, on :focus-visible\n"
            "             ONLY -- a mouse click never shows it\n"
            "border WIDTH changes per state: {none, normal, hover, focus}"
        ),
        source=(
            "register rule 9 -- pressed is the SWAP and the ring is keyboard-only "
            "(both 2026-08-19); focus supersedes the note's alternate-inverse/030"
        ),
    ),
    "sizing": Rule(
        id="sizing",
        title="Sizing -- every value is base x factor",
        formula=(
            "size(base, factor) = base x factor        2 x 8 = 16\n\n"
            "a SIZE RUNG has a factor (text anchored at 2.0, components\n"
            "at 5.0; neighbours step by 0.25 and 0.75)\n"
            "a CELL is base x rung_factor x ratio\n\n"
            "sub-base sizes are fractional factors: 1/16 1/8 1/4 3/8 1/2 3/4\n"
            "MAX (999) is a named sentinel and is never multiplied\n\n"
            "THE GRID IS DEAD AS DATA. Nothing snaps, rounds, or lands on\n"
            "a ladder, because there is no ladder to land on.\n"
            "A UI shows px and saves the FACTOR -- factor_of(base, px)."
        ),
        source="register rule 10, as amended: \"base * factor? 2 * 8 = 16?\"",
    ),
    "type": Rule(
        id="type",
        title="Type -- a weight slot is a fatness range",
        formula=(
            "slot value = the family's VERBATIM style-name descriptor\n"
            "numeric CSS weight = read from the actual installed face (OS/2)\n\n"
            "assignment default = slots 1/1/2/3, per-brand configurable"
        ),
        source="register rule 11",
    ),
    "authored": Rule(
        id="authored",
        title="Authored -- a brand states this; nothing computes it",
        formula=(
            "the complete authored set: 4 signals, the brand's own neutrals,\n"
            "brand canonical/on-light/on-dark, font family + 4 weight slots +\n"
            "assignment + the per-band UPPERCASE toggle, border weights +\n"
            "opacity, radius ladder + base, effect intensities, motion speeds +\n"
            "curves, shadow anchors, the size picks and the grid base.\n\n"
            "Everything else in the system is computed from these."
        ),
        source="register rule 12",
    ),
}


# ---------------------------------------------------------------------------
# The authored inputs, as graph nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Input:
    """One authored slot: what the brand stated.

    `lightness` is carried for colour slots so no consumer has to measure one.
    A client that computed OKLab itself would be a second implementation of the
    only measurement this system takes -- and it would be measuring against the
    threshold, which is exactly where a divergence would be invisible and
    consequential.
    """

    slot: str
    label: str
    value: str
    group: str
    swatch: str | None = None
    lightness: float | None = None


def authored_inputs(brand: Brand) -> list[Input]:
    """Every authored colour, plus the constants the descent reads.

    Colours only get a swatch. The list is what the growth UI's rail edits and
    what the derivation graph's leftmost column shows.
    """
    b = brand.brand

    def colour_input(slot: str, label: str, value: str, group: str) -> Input:
        return Input(slot, label, value, group, value, c.lightness(value))

    out: list[Input] = [
        colour_input("brand.canonical", "brand canonical", b.canonical, "brand"),
        colour_input("brand.on_light", "brand on-light", b.adaptation("light"), "brand"),
        colour_input("brand.on_dark", "brand on-dark", b.adaptation("dark"), "brand"),
        colour_input("neutrals.black", "neutral black", brand.neutrals.black, "neutrals"),
        colour_input("neutrals.white", "neutral white", brand.neutrals.white, "neutrals"),
    ]
    # The additional colours are authored input like any other colour, so they
    # enter the model here and reach the rail and the lightness strip for free.
    # Their FOREGROUND is not in this list: it is computed, and an authored-input
    # list that carried a derived value would be lying about what a brand states.
    for row in brand.additional_colours():
        out.append(
            colour_input(
                f"brand.{row['name']}",
                row["label"] or row["name"].replace("-", " "),
                row["value"],
                "additional",
            )
        )
    for role, value in brand.signals.by_role().items():
        out.append(colour_input(f"signals.{role}", f"signal {role}", value, "signals"))
    out.append(
        Input(
            "threshold",
            "polarity threshold",
            f"{c.POLARITY_THRESHOLD}",
            "constant",
            None,
        )
    )
    out.append(Input("font.family", "font family", brand.font.family, "type", None))
    for slot in (1, 2, 3, 4):
        out.append(
            Input(
                f"font.slot_{slot}",
                f"weight slot {slot}",
                brand.font.descriptor(slot),
                "type",
                None,
            )
        )
    # The base is a CONSTANT, not an authored slot -- the same category the
    # polarity threshold is in, and for the same reason: "Brands do not change
    # their base. Base is always the same." It is listed so a reader can see
    # the number every size multiplies, never so a rail can offer it.
    out.append(Input("base", "base", f"{scale.BASE:g}", "constant", None))
    out.append(
        Input(
            "root",
            "root font-size",
            f"{scale.ROOT_PERCENT:g}% = {scale.ROOT_PX:g}px, so a rem is a factor",
            "constant",
            None,
        )
    )
    # Border weights are FACTORS. Shown as the px they come to on the system
    # base, because that is what a designer reads -- but the value beside it is
    # the factor, so the readout cannot be mistaken for what is stored.
    out.append(
        Input(
            "border.weight_factors",
            "border weights",
            " / ".join(
                f"{w:g}px (x{f:g})"
                for w, f in zip(
                    brand.border.weights(), brand.border.weight_factors
                )
            ),
            "geometry",
            None,
        )
    )
    return out


# ---------------------------------------------------------------------------
# Derivations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Derivation:
    """One derived slot: its value, the rule that made it, and what it read.

    `value` is READ from the palette. `because` is a reading of the measurement
    that decided it -- a measurement, never a second decision.
    """

    slot: str
    label: str
    value: str
    rule: str
    reads: tuple[str, ...]
    because: str
    swatch: str | None = None

    #: Roles whose value a curator AUTHORED over the rule's answer, if any.
    #:
    #: Ported from v1, which carried `overridden` as a fourth origin kind
    #: alongside authored/derived/literal. v2 had the MECHANISM
    #: (`Signals.foreground_override`, read at resolve.py:539) but no way to say
    #: so: an overridden ink was reported as `derived`, indistinguishable from
    #: one the rule produced. That is not a missing badge, it is a false
    #: statement -- the signal_foregrounds row asserts every signal "runs the
    #: SAME foreground rule as any other ground", and an override is precisely
    #: the case where it does not.
    overridden: tuple[str, ...] = ()


COVERED: frozenset[str] = frozenset(
    {
        "ground",
        "background",
        "foreground",
        "highlight_neutral_background",
        "highlight_neutral_foreground",
        "highlight_branded_background",
        "highlight_branded_foreground",
        "alternate",
        "alternate_inverse",
        "separator",
        "border_full",
        "border_half",
        "border_branded",
        "border_branded_inverse",
        "focus",
        "solid",
        "solid_inverse",
        "solid_brand",
        "solid_brand_inverse",
        "transparent",
        "transparent_inverse",
        "blur_background",
        "shadow",
        "blur_and_shadow",
        "signal_backgrounds",
        "signal_foregrounds",
        "states",
        "disabled",
        "off",
        "flags",
    }
)
"""Every `Palette` field a derivation is produced for.

`test_explain.py` asserts this equals the palette's own field names. A new
palette field therefore cannot reach the UI without a rule behind it.
"""


def _pct(step) -> str:
    return f"{step.percent:g}%"


def derivations(resolver: Resolver, ground: Ground) -> list[Derivation]:
    """Every derived value of one ground, with the rule that produced it.

    Reads the palette once and reports. The only arithmetic below is
    MEASUREMENT of values the resolver already chose -- an OKLab lightness, a
    WCAG ratio -- which is what the `because` sentences quote.
    """
    brand = resolver.brand
    p = resolver.palette(ground)
    pole = ground.polarity
    ell = ground.lightness
    out: list[Derivation] = []

    def add(
        slot: str,
        label: str,
        value: str,
        rule: str,
        reads: tuple[str, ...],
        because: str,
        swatch: str | None = None,
        overridden: tuple[str, ...] = (),
    ) -> None:
        out.append(
            Derivation(slot, label, value, rule, reads, because, swatch, overridden)
        )

    side = "below" if pole == "dark" else "at or above"
    add(
        "ground",
        "resolved ground",
        p.background or f"L {ell:.3f}",
        "descent",
        ("brand.canonical", "neutrals.black", "neutrals.white", "threshold"),
        f"L {ell:.4f} is {side} {c.POLARITY_THRESHOLD}, so this ground is {pole}."
        + (" It arrived by: " + ground.origin + "." if ground.origin else ""),
        p.background,
    )
    add(
        "background",
        "background",
        p.background or "(image ground -- lightness only)",
        "descent",
        ("ground",),
        "The ground IS the background; one value descends and this is it.",
        p.background,
    )

    fg_ratio = (
        c.wcag_contrast(p.foreground, p.background) if p.background else float("nan")
    )
    add(
        "foreground",
        "foreground",
        p.foreground,
        "foreground",
        ("neutrals.black", "neutrals.white", "ground", "threshold"),
        f"a {pole} ground takes the brand's own "
        f"{'white' if pole == 'dark' else 'black'} -- {fg_ratio:.2f}:1 here.",
        p.foreground,
    )
    add(
        "highlight_neutral_background",
        "highlight-neutral background",
        p.highlight_neutral_background,
        "descent",
        ("foreground",),
        "the inverse of the background, which IS the emphasis flip reached "
        "without a request.",
        p.highlight_neutral_background,
    )
    add(
        "highlight_neutral_foreground",
        "highlight-neutral foreground",
        p.highlight_neutral_foreground,
        "descent",
        ("foreground",),
        "the inverse of the foreground -- the neutral that stands on the flip.",
        p.highlight_neutral_foreground,
    )

    neutral_ground = brand.is_neutral(p.background)
    add(
        "highlight_branded_background",
        "highlight-branded background",
        p.highlight_branded_background,
        "branded-figure",
        ("brand.on_light", "brand.on_dark", "ground"),
        (
            f"this ground IS one of the brand's own neutrals, so a branded figure "
            f"renders brand.on_{pole} unconditionally."
            if neutral_ground
            else "this ground is chromatic, so the main CTA is a NEUTRAL chosen "
            "by the ground's polarity."
        ),
        p.highlight_branded_background,
    )
    add(
        "highlight_branded_foreground",
        "highlight-branded foreground",
        p.highlight_branded_foreground,
        "foreground",
        ("highlight_branded_background", "neutrals.black", "neutrals.white"),
        "the same foreground rule, asked about the branded surface instead of "
        "the ground.",
        p.highlight_branded_foreground,
    )

    add(
        "alternate",
        "alternate ladder (17)",
        f"{p.alternate.ink} at 17 steps",
        "ladder",
        ("foreground", "ground"),
        "the FOREGROUND over this ground. Its direction is a consequence of "
        "which neutral the foreground is -- never a rule of its own.",
        p.alternate.ink,
    )
    add(
        "alternate_inverse",
        "alternate-inverse ladder (17)",
        f"{p.alternate_inverse.ink} at 17 steps",
        "ladder",
        ("foreground", "ground"),
        "the OPPOSITE of the foreground over the same ground.",
        p.alternate_inverse.ink,
    )

    for slot, label, step, rule_reads in (
        ("separator", "separator", p.separator, "alternate"),
        ("border_half", "border-half", p.border_half, "alternate"),
        ("focus", "focus", p.focus, "alternate"),
        ("transparent", "transparent", p.transparent, "alternate"),
        (
            "transparent_inverse",
            "transparent-inverse",
            p.transparent_inverse,
            "alternate_inverse",
        ),
        ("blur_background", "blur-background", p.blur_background, "alternate_inverse"),
    ):
        add(
            slot,
            label,
            step.over or step.css,
            "ladder-rung",
            (rule_reads,),
            f"rung {_pct(step)} of {rule_reads.replace('_', '-')}, composited "
            "over this ground.",
            step.over,
        )

    add(
        "border_full",
        "border-full",
        p.border_full,
        "ladder-rung",
        ("foreground",),
        "the foreground itself -- rung 100% of alternate.",
        p.border_full,
    )
    add(
        "border_branded",
        "border-branded",
        p.border_branded,
        "branded-figure",
        ("highlight_branded_background",),
        "inherits the branded background, so a bordered element and a filled "
        "one are the same brand decision.",
        p.border_branded,
    )
    add(
        "border_branded_inverse",
        "border-branded-inverse",
        p.border_branded_inverse,
        "branded-figure",
        ("highlight_branded_foreground",),
        "inherits the branded foreground.",
        p.border_branded_inverse,
    )
    add(
        "solid",
        "solid",
        p.solid,
        "descent",
        ("foreground",),
        "a solid component is painted the foreground -- which makes it the "
        "inverse surface, and its own ink flips accordingly.",
        p.solid,
    )
    add(
        "solid_inverse",
        "solid-inverse",
        p.solid_inverse or "(image ground)",
        "descent",
        ("ground",),
        "painted the background, so it disappears into the ground on purpose.",
        p.solid_inverse,
    )
    add(
        "solid_brand",
        "solid-brand",
        p.solid_brand,
        "branded-figure",
        ("highlight_branded_background",),
        "the branded surface, whatever the branded rule decided it is here.",
        p.solid_brand,
    )
    add(
        "solid_brand_inverse",
        "solid-brand-inverse",
        p.solid_brand_inverse,
        "branded-figure",
        ("highlight_branded_foreground",),
        "the branded surface's own ink, used as a fill.",
        p.solid_brand_inverse,
    )

    for slot, label, answer in (
        ("shadow", "shadow", p.shadow),
        ("blur_and_shadow", "blur-and-shadow", p.blur_and_shadow),
    ):
        if answer.kind == "shadow":
            because = (
                f"black at {answer.opacity:.1%} reproduces the anchor's OKLab "
                "darkening on this ground."
            )
        else:
            because = (
                "a black veil cannot reach the anchor's darkening on a ground "
                "this dark even at full opacity, so separation comes from a "
                f"{answer.width:g}px border instead."
            )
        add(
            slot,
            label,
            f"{answer.kind}: {answer.colour} @ {answer.opacity:.3f}",
            "shadow",
            ("ground", "neutrals.black"),
            because,
            answer.colour,
        )

    add(
        "signal_backgrounds",
        "signal backgrounds",
        ", ".join(f"{k} {v}" for k, v in p.signal_backgrounds.items()),
        "signals",
        tuple(f"signals.{role}" for role in p.signal_backgrounds),
        "authored, four of them, and placeable on any element including the root.",
        None,
    )
    overridden_roles = tuple(
        role
        for role in p.signal_foregrounds
        if resolver.brand.signals.foreground_override(role)
    )
    if overridden_roles:
        named = ", ".join(overridden_roles)
        because_fg = (
            f"the rule ran for every signal, and {named} then took an AUTHORED "
            "ink instead of the answer. The rule is unchanged and still equal "
            "for all four -- an override replaces a result, it does not add a "
            "second predicate."
        )
    else:
        because_fg = (
            "each one runs the SAME foreground rule as any other ground. A "
            "second predicate anywhere here would break that equality."
        )
    add(
        "signal_foregrounds",
        "signal foregrounds",
        ", ".join(f"{k} {v}" for k, v in p.signal_foregrounds.items()),
        "signals",
        ("signal_backgrounds", "neutrals.black", "neutrals.white", "threshold"),
        because_fg,
        None,
        overridden=overridden_roles,
    )

    s = p.states
    add(
        "states",
        "states",
        f"hover {s.hover.over or s.hover.css} / pressed {s.pressed} / "
        f"focus {s.focus.over or s.focus.css}",
        "states",
        ("ground", "foreground", "highlight_branded_background", "border.weight_factors"),
        f"hover mixes {_pct(s.hover)} of {s.hover.ink} into the component's own "
        f"ground; a press exchanges a surface's background and foreground, and "
        f"reads the mother only when neither of the two carries the brand; focus "
        f"is a {s.border_widths['focus']:g}px ring outside the box, on keyboard "
        f"focus only.",
        s.hover.over,
    )
    add(
        "disabled",
        "disabled opacity",
        f"{p.disabled:.0%}",
        "authored",
        (),
        "the note's own constant -- an opacity of the element, not a colour.",
        None,
    )
    add(
        "off",
        "off opacity",
        f"{p.off:.0%}",
        "authored",
        (),
        "the note's own constant.",
        None,
    )
    add(
        "flags",
        "flags",
        f"{len(p.flags)} on this ground",
        "authored",
        ("ground", "foreground"),
        "advice with a reason. A flag never changes a value and never blocks a "
        "brand -- \"Even if the contrast isn't high enough. It's brand decision.\"",
        None,
    )
    return out


def palette_field_names() -> frozenset[str]:
    """The `Palette` dataclass's own field names -- the completeness oracle."""
    return frozenset(f.name for f in fields(Palette))


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Graph:
    """The derivation DAG of one ground: authored inputs on the left, derived
    slots to the right, every edge a real read."""

    nodes: tuple[dict, ...]
    edges: tuple[tuple[str, str], ...]


def graph(resolver: Resolver, ground: Ground) -> Graph:
    """The derivation graph, built from the SAME rows the UI lists.

    Nodes and edges are not a second description of the system: the edges are
    exactly the `reads` tuples of `derivations()`, so a value whose rule changes
    moves in the list and in the graph together or not at all.
    """
    rows = derivations(resolver, ground)
    nodes: list[dict] = []
    for inp in authored_inputs(resolver.brand):
        nodes.append(
            {
                "id": inp.slot,
                "kind": "authored",
                "label": inp.label,
                "value": inp.value,
                "swatch": inp.swatch,
                "group": inp.group,
                "rule": "authored",
            }
        )
    for row in rows:
        nodes.append(
            {
                "id": row.slot,
                # THREE KINDS, not two. `overridden` is v1's fourth origin kind,
                # ported because v2 could not express it: a slot whose value a
                # curator authored over the rule's answer read as plain
                # `derived`, which says the rule produced it. It did not.
                "kind": "overridden" if row.overridden else "derived",
                "overridden": list(row.overridden),
                "label": row.label,
                "value": row.value,
                "swatch": row.swatch,
                "group": "derived",
                "rule": row.rule,
                "because": row.because,
            }
        )
    known = {n["id"] for n in nodes}
    edges: list[tuple[str, str]] = []
    for row in rows:
        for read in row.reads:
            if read in known:
                edges.append((read, row.slot))
    return Graph(nodes=tuple(nodes), edges=tuple(edges))


# ---------------------------------------------------------------------------
# Guest brands -- the nesting case, and the reveal that names which clause fired
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Placement:
    """One guest brand, resolved on one host ground.

    This is the sharpest statement the system makes, and it is his own story:

        "main theme is [host] (yellow brand color) ... A UBS component
        (background black, main cta: red) works with contrast towards the
        [host] yellow, while maybe Valiant (purple) might not have enough
        contrast."

    Two guests on the SAME host ground can land differently, and the difference
    is not a setting anywhere -- it falls out of register rule 4 read against
    each guest's own colours. `outcome` says which way it went, `clause` says
    WHICH HALF of the rule decided it, and `because` is the measurement.

    NOTHING HERE IS COMPUTED. The ground and the CTA are read back out of the
    guest's own resolver, exactly as the emitted sheet gets them; this module
    only names the branch that produced them.
    """

    guest: str
    outcome: str          # "brand-full" | "brand-fallback"
    clause: str           # which half of rule 4 fired
    ground: str | None    # the colour the guest's surface lands on
    polarity: str
    cta: str              # the guest's main call to action on that ground
    cta_slot: str         # which authored slot the CTA came from
    because: str
    host_ground: str | None
    host_polarity: str
    host_is_neutral_to_guest: bool


def guest_placement(guest: Brand, host_ground: Ground) -> Placement:
    """Resolve one guest brand on a host ground, and say which clause fired.

    THE ONE RULE, and both branches of it:

      * The host ground is NEUTRAL to the guest, and the guest's adaptation for
        that ground sits on the OPPOSITE side of the threshold -- so it
        separates, and the guest goes BRAND-FULL on its own colour.
      * Anything else -- a chromatic host, or an adaptation that does not
        separate -- takes THE FALLBACK: the neutral of the polarity opposite
        the HOST, carrying `brand.on_{polarity of that fallback}` as its CTA.

    "Neutral" is asked of the GUEST, not of the world: a guest whose own white
    is #f4f4f4 finds #ffffff chromatic, and falls back on it. That is the same
    predicate `Resolver.ground` uses, so this cannot disagree with the sheet.
    """
    resolver = Resolver(guest)
    landed = resolver.ground(Request(branded=True), host_ground)
    cta = resolver.branded_figure(landed)
    adaptation = guest.brand.adaptation(host_ground.polarity)
    neutral_to_guest = guest.is_neutral(host_ground.colour)

    if landed.origin == "brand-full":
        clause = "neutral host ground, and the adaptation separates from it"
        because = (
            f"the host ground {host_ground.colour} is one of {guest.id}'s own "
            f"neutrals, and its on-{host_ground.polarity} shade {adaptation} "
            f"(L {c.lightness(adaptation):.3f}) sits on the opposite side of "
            f"{c.POLARITY_THRESHOLD} from it, so it stands on it. The guest wears "
            f"its own colour as the ground; its CTA is then a NEUTRAL, by C1."
        )
        cta_slot = "neutrals"
    elif not neutral_to_guest:
        clause = "chromatic host ground -- ALWAYS the fallback"
        because = (
            f"the host ground {host_ground.colour} is not one of {guest.id}'s own "
            f"neutrals, so rule 4's second clause fires with no measurement at "
            f"all. The fallback is the neutral OPPOSITE the host "
            f"({host_ground.polarity} host -> {landed.polarity} ground), because "
            f"that is the one that contrasts with the host -- and the CTA is then "
            f"the shade authored to work on it."
        )
        cta_slot = f"brand.on_{landed.polarity}"
    else:
        clause = "neutral host ground, but the adaptation does NOT separate"
        because = (
            f"the host ground {host_ground.colour} is one of {guest.id}'s own "
            f"neutrals, but its on-{host_ground.polarity} shade {adaptation} "
            f"(L {c.lightness(adaptation):.3f}) is on the SAME side of "
            f"{c.POLARITY_THRESHOLD}, so it would not stand on it. The fallback "
            f"flips the host instead, and the CTA is the shade for that ground."
        )
        cta_slot = f"brand.on_{landed.polarity}"

    return Placement(
        guest=guest.id,
        outcome=landed.origin,
        clause=clause,
        ground=landed.colour,
        polarity=landed.polarity,
        cta=cta,
        cta_slot=cta_slot,
        because=because,
        host_ground=host_ground.colour,
        host_polarity=host_ground.polarity,
        host_is_neutral_to_guest=neutral_to_guest,
    )
