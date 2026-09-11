# SPDX-License-Identifier: Apache-2.0
"""The engine's HTTP surface -- thin on purpose.

Every number this module returns was computed by `resolve`, `emit`, `sizing` or
`explain`. Nothing here decides a colour, a size or a rule, and nothing here
holds a second copy of one. The routes are a projection of the engine into
JSON; if a rule changes in the engine, every route changes with it and none of
them has to be edited.

    GET  /api/design-engine/fonts              families installed here, faces measured
    GET  /api/design-engine/stages             the growth choreography
    GET  /api/design-engine/rules              the rule catalogue
    GET  /api/design-engine/kits               shipped + user-authored brands
    GET  /api/design-engine/kits/{id}          one brand, as authored
    POST /api/design-engine/kits               create in the USER store
    PUT  /api/design-engine/kits/{id}          save in the USER store
    DELETE /api/design-engine/kits/{id}
    POST /api/design-engine/essentials         a typeface + a colour -> a whole brand
    POST /api/design-engine/resolve            a brand -> the complete resolved model
    POST /api/design-engine/sheet              a brand -> its emitted CSS
    GET  /api/design-engine/sheet/{id}.css     a saved brand's emitted CSS

THE DESCENT TRAVELS AS A TRANSITION TABLE, not as a tree. Each ground carries
`transitions`, mapping every request to the ground key it resolves to, so a
client can draw a tree of ANY shape and ANY depth from one response. That is
the fixed-point closure made useful: the reachable set is finite even though
the tree is not, so the whole descent fits in a table.

WRITES NEVER TOUCH THE PACKAGE TREE. `store.save` refuses a shipped id
outright; see that module for why the refusal is not a default.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from . import emit as emitter
from . import explain, growth, kits, scale, schema, sizing, store
from . import colour as c
from .resolve import COMPONENT_STYLES, Ground, Request, Resolver
from .schema import (
    COMPONENT_RUNGS,
    MAX_ADDITIONAL,
    TEXT_BANDS,
    TEXT_RUNGS,
    Brand,
    lint,
)

logger = logging.getLogger("okuro.design_engine.api")

router = APIRouter(prefix="/api/design-engine", tags=["design-engine"])


# ---------------------------------------------------------------------------
# Fonts installed on this machine
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def installed_families() -> list[dict]:
    """Every font family this machine can actually render, with real weights.

    MEASURED, twice over. `fc-list` finds the files; the OS/2 `usWeightClass`
    inside each file gives the number. fontconfig's own weight scale is not
    that number and does not map onto CSS weights one to one, so reading it
    would put a plausible wrong number in front of a builder.

    This is the only I/O in the engine's world, and it is in the API layer
    deliberately: `resolve` and `emit` stay pure functions of a brand, which is
    what makes a sheet reproduce byte for byte.
    """
    try:
        out = subprocess.run(
            ["fc-list", "--format", "%{family[0]}|%{style[0]}|%{file}\n"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - host dependent
        logger.warning("fc-list unavailable, font list is empty: %s", exc)
        return []

    try:
        from fontTools.ttLib import TTFont  # type: ignore
    except ImportError:  # pragma: no cover - fontTools ships with okuro
        TTFont = None  # type: ignore

    families: dict[str, dict[str, int]] = {}
    seen_files: set[str] = set()
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        family, style, path = (p.strip() for p in parts)
        # Italics are a second axis, not a fatness range; a weight slot holds
        # an upright descriptor.
        if not family or "italic" in style.lower() or "oblique" in style.lower():
            continue
        if path in seen_files or not path.lower().endswith((".ttf", ".otf")):
            continue
        seen_files.add(path)
        weight: int | None = None
        if TTFont is not None:
            try:
                with TTFont(path, lazy=True, fontNumber=0) as font:
                    os2 = font.get("OS/2")
                    weight = int(getattr(os2, "usWeightClass", 0)) or None
            except Exception:  # pragma: no cover - a broken font file
                weight = None
        if weight is None:
            continue
        faces = families.setdefault(family, {})
        # A duplicate descriptor across two files is the same fatness range;
        # keep the first measurement rather than letting file order decide.
        faces.setdefault(style, weight)

    return [
        {
            "family": family,
            "faces": dict(sorted(faces.items(), key=lambda kv: kv[1])),
            "quad": list(growth.weight_quad(faces) or ()),
        }
        for family, faces in sorted(families.items())
        if faces
    ]


# ---------------------------------------------------------------------------
# Serialisation -- the engine's values, as JSON
# ---------------------------------------------------------------------------


def _flag(flag: c.Flag) -> dict:
    """A flag, with BOTH of its addresses.

    `where` reaches the rail's field, `renders` reaches the canvas's elements.
    Serialising only `where` is what made a flag advice you had to go looking
    for -- "here is where it's failing" needs the second list.
    """
    return {
        "code": flag.code,
        "message": flag.message,
        "where": flag.where,
        "renders": list(flag.renders),
        # HOW BAD, from the engine's own table. The page ranks nothing: it
        # renders the worst severity present as one sentence, which is the
        # answer to "is my configuration good". See `colour.SEVERITY`.
        "severity": flag.severity,
    }


def _step(step) -> dict:
    return {
        "ink": step.ink,
        "percent": step.percent,
        "over": step.over,
        "css": step.css,
    }


def _ladder(ladder) -> dict:
    return {"ink": ladder.ink, "steps": [_step(s) for s in ladder.steps]}


def _shadow(answer) -> dict:
    return {
        "kind": answer.kind,
        "colour": answer.colour,
        "opacity": answer.opacity,
        "width": answer.width,
        "reason": answer.reason,
    }


def _layer(layer) -> dict:
    out = {
        "name": layer.name,
        "colour": layer.colour,
        "ink": layer.ink,
        "border_width": layer.border_width,
    }
    if layer.outline is not None:
        out["outline"] = asdict(layer.outline)
    return out


def _surface(surface) -> dict:
    return {
        "style": surface.style,
        "fill": surface.fill,
        "ink": surface.ink,
        "border": surface.border,
        "border_branded": surface.border_branded,
        "states": {
            name: _layer(surface.state(name))
            for name in ("normal", "hover", "pressed", "focus")
        },
    }


def _palette(palette) -> dict:
    """The complete palette, flattened. Complete because a partial one is how
    a boundary leaks its parent's values."""
    s = palette.states
    return {
        "background": palette.background,
        "foreground": palette.foreground,
        "highlight_neutral_background": palette.highlight_neutral_background,
        "highlight_neutral_foreground": palette.highlight_neutral_foreground,
        "highlight_branded_background": palette.highlight_branded_background,
        "highlight_branded_foreground": palette.highlight_branded_foreground,
        "alternate": _ladder(palette.alternate),
        "alternate_inverse": _ladder(palette.alternate_inverse),
        "separator": _step(palette.separator),
        "border_full": palette.border_full,
        "border_half": _step(palette.border_half),
        "border_branded": palette.border_branded,
        "border_branded_inverse": palette.border_branded_inverse,
        "focus": _step(palette.focus),
        "solid": palette.solid,
        "solid_inverse": palette.solid_inverse,
        "solid_brand": palette.solid_brand,
        "solid_brand_inverse": palette.solid_brand_inverse,
        "transparent": _step(palette.transparent),
        "transparent_inverse": _step(palette.transparent_inverse),
        "blur_background": _step(palette.blur_background),
        "shadow": _shadow(palette.shadow),
        "blur_and_shadow": _shadow(palette.blur_and_shadow),
        "signal_backgrounds": palette.signal_backgrounds,
        "signal_foregrounds": palette.signal_foregrounds,
        "states": {
            "hover": _step(s.hover),
            "hover_brand": _step(s.hover_brand),
            "pressed": s.pressed,
            "pressed_brand": s.pressed_brand,
            "focus": _step(s.focus),
            "border_widths": s.border_widths,
        },
        "disabled": palette.disabled,
        "off": palette.off,
        "flags": [_flag(f) for f in palette.flags],
    }


def _request_labels(brand: Brand) -> list[tuple[str, Request]]:
    """Every builder action, in the order the UI offers them.

    These are BUILDER actions, not user settings: "a USER chooses only
    dark/light. Signal and brand are design facts placed by the designer."
    """
    out = [("emphasis", Request(emphasis=True)), ("branded", Request(branded=True))]
    for role in brand.signals.by_role():
        out.append((f"signal:{role}", Request(signal=role)))
    return out


def _ground(resolver: Resolver, ground: Ground) -> dict:
    """One ground, with everything derivable from it and where it leads."""
    palette = resolver.palette(ground)
    dag = explain.graph(resolver, ground)
    transitions = {
        label: emitter.ground_key(resolver.ground(request, ground))
        for label, request in _request_labels(resolver.brand)
    }
    return {
        "key": emitter.ground_key(ground),
        "colour": ground.colour,
        "lightness": ground.lightness,
        "polarity": ground.polarity,
        "origin": ground.origin,
        "in_warn_band": c.in_warn_band(ground.lightness),
        "palette": _palette(palette),
        "transitions": transitions,
        "components": [
            _surface(resolver.surface_of(ground, style)) for style in COMPONENT_STYLES
        ],
        "derivations": [
            {
                "slot": d.slot,
                "label": d.label,
                "value": d.value,
                "rule": d.rule,
                "reads": list(d.reads),
                "because": d.because,
                "swatch": d.swatch,
                "stage": growth.stage_of(d.slot),
            }
            for d in explain.derivations(resolver, ground)
        ],
        "graph": {
            "nodes": [dict(n) for n in dag.nodes],
            "edges": [list(e) for e in dag.edges],
        },
    }


def _roots(resolver: Resolver) -> list[dict]:
    """The grounds a builder may place at the top of a tree.

    Dark and light are the USER's choice. Brand-full and the signals are here
    because a design fact may be placed on the highest element -- they are
    builder placements that happen to be at the root, not a second kind of
    theme.
    """
    brand = resolver.brand
    out = [
        {"id": "light", "label": "light", "user_choice": True,
         "ground": resolver.root("light")},
        {"id": "dark", "label": "dark", "user_choice": True,
         "ground": resolver.root("dark")},
        {"id": "brand", "label": "brand-full", "user_choice": False,
         "ground": resolver.root("light", Request(branded=True))},
    ]
    for role in brand.signals.by_role():
        out.append(
            {
                "id": f"signal:{role}",
                "label": f"signal {role}",
                "user_choice": False,
                "ground": resolver.root("light", Request(signal=role)),
            }
        )
    return [
        {
            "id": row["id"],
            "label": row["label"],
            "user_choice": row["user_choice"],
            "key": emitter.ground_key(row["ground"]),
        }
        for row in out
    ]


def _factor(base: float, factor: float) -> dict:
    """One authored size slot, both ways.

    THE UI SHOWS PX AND SAVES FACTORS. A designer reads pixels, but the factor
    is what a brand stores -- and it is also what the emitted rem IS, against
    the 50 % root. So every size slot travels as the factor that is stored AND
    the pixel it currently comes to, and the editing surface is never handed a
    pixel on its own.
    """
    return {"factor": factor, "px": scale.size(base, factor)}


def _sizes(brand: Brand) -> dict:
    """The numeric tables -- computed, never stored.

    There is no grid here and no value list of any kind: `grid.py` was deleted
    with ruling 4's amendment, and a payload that shipped a ladder would put one
    back on the wire for a client to iterate.
    """
    factors = brand.sizes
    return {
        # CONSTANTS, carried so the page can state the rule -- not slots. The
        # rail must not offer either; the ruling took both out of the brand.
        "root_percent": scale.ROOT_PERCENT,
        "root_px": scale.ROOT_PX,
        "base": scale.BASE,
        # The authored factor table, which is what the rail edits. TEXT is his
        # own 168 cells and travels whole; COMPONENTS are still an anchor, a
        # step and seven role ratios, because he authored no table for them.
        "factors": {
            "text": {
                rung: dict(row) for rung, row in factors.text_factors.items()
            },
            "component_anchor": factors.component_anchor_factor,
            "component_rung_step": factors.component_rung_step,
            "role_ratios": dict(factors.role_ratios),
        },
        "text_bands": [
            {"letter": letter, "name": name, "count": count}
            for letter, name, count in TEXT_BANDS
        ],
        "text_rungs": list(TEXT_RUNGS),
        "component_rungs": list(COMPONENT_RUNGS),
        "text_styles": list(sizing.TEXT_STYLES),
        "component_roles": list(sizing.COMPONENT_ROLES),
        "text": {
            rung: sizing.text_table(brand, rung) for rung in TEXT_RUNGS
        },
        "components": {
            rung: sizing.component_table(brand, rung) for rung in COMPONENT_RUNGS
        },
        "downscale": {
            "text": {rung: sizing.downscale_text(rung) for rung in TEXT_RUNGS},
            "components": {
                rung: sizing.downscale_component(rung) for rung in COMPONENT_RUNGS
            },
        },
        "radius": {
            rung: brand.radius.outer(rung) for rung in ("S", "M", "L", "MAX")
        },
        "radius_base": brand.radius.base,
        # S/M/L are factors of the RADIUS base, which is its own authored number
        # and not the brand's. MAX is absent on purpose: it is a named sentinel,
        # never a multiplication, so it has no factor to show or to edit.
        "radius_factors": {
            "S": _factor(brand.radius.base, brand.radius.s_factor),
            "M": _factor(brand.radius.base, brand.radius.m_factor),
            "L": _factor(brand.radius.base, brand.radius.l_factor),
        },
    }


def _type(brand: Brand) -> dict:
    slots = []
    for slot in (1, 2, 3, 4):
        descriptor = brand.font.descriptor(slot)
        try:
            numeric: int | None = brand.font.numeric(slot)
        except ValueError:
            numeric = None
        slots.append({"slot": slot, "descriptor": descriptor, "numeric": numeric})
    return {
        "family": brand.font.family,
        "stack": brand.font.stack,
        "slots": slots,
        "offered": [
            {"descriptor": d, "numeric": n} for d, n in brand.font.offered_weights()
        ],
        "assignment": {
            band: getattr(brand.weights, band) for band in schema.TYPE_BANDS
        },
        # THE CASE TOGGLE TRAVELS BESIDE THE WEIGHT ASSIGNMENT, keyed by the same
        # four bands, because the rail reads one type panel and a second shape for
        # a second per-band fact is how the panel starts needing to know which is
        # which. `uppercase` is the RENDERED value, off `CaseAssignment.css`, so
        # the rail can label a toggle with what the sheet will actually say.
        "case": {
            band: {
                "uppercase": getattr(brand.case, band),
                "css": brand.case.css(band),
            }
            for band in schema.TYPE_BANDS
        },
    }


def resolved_model(brand: Brand) -> dict:
    """A brand's whole system, resolved. The page's single source of truth."""
    resolver = Resolver(brand)
    grounds = emitter.reachable_grounds(resolver)
    return {
        "brand_id": brand.id,
        "brand": brand.model_dump(mode="json"),
        "authored": [asdict(i) for i in explain.authored_inputs(brand)],
        "threshold": c.POLARITY_THRESHOLD,
        "warn_band": list(c.WARN_BAND),
        "roots": _roots(resolver),
        "grounds": [_ground(resolver, g) for g in grounds],
        "requests": [label for label, _ in _request_labels(brand)],
        "sizes": _sizes(brand),
        "type": _type(brand),
        "motion": {
            "speeds": brand.motion.speeds,
            "curves": brand.motion.curves,
            "signature": brand.motion.signature,
            "default_curve": brand.motion.default_curve,
            "default_speed": brand.motion.default_speed,
        },
        # Effect intensities are FACTORS. Both forms travel: `.px(base)` is what
        # the emitter renders, the factor is what a brand saves.
        "effects": {
            name: {
                field: _factor(scale.BASE, value)
                for field, value in brand.effects.by_name(name)
                .model_dump(mode="json")
                .items()
            }
            for name in ("light", "normal", "strong", "x-strong")
        },
        "border": {
            "weights": [
                _factor(scale.BASE, f) for f in brand.border.weight_factors
            ],
            "opacity": brand.border.opacity,
        },
        # The brand-colour model, as the page has to render it: which mode the
        # brand is in, what the two shade amounts are, and -- when a shade is
        # unset -- what the ENGINE would propose. The proposal travels so the
        # slider can start where the engine would, and so no client ever computes
        # a shade for itself.
        "brand_colours": _brand_colours(brand),
        "additional": brand.additional_colours(),
        "flags": [_flag(f) for f in lint(brand)],
    }


def _brand_colours(brand: Brand) -> dict:
    """The authored brand colour, its mode, and the shade behind each adaptation.

    `proposed` is `growth.propose_adaptation` -- the least amount that clears the
    threshold. It is ADVICE and the page seeds a slider with it; nothing here
    changes a value. `shade` is what the brand actually authored, or None for a
    brand whose adaptation was authored as a colour before the shade model
    existed.
    """
    b = brand.brand
    poles = {}
    for pole, shade in (("light", b.shade_light), ("dark", b.shade_dark)):
        proposal = growth.propose_adaptation(b.canonical, pole, brand.neutrals)
        poles[pole] = {
            "value": b.adaptation(pole),
            "shade": shade,
            "authored_colour": b.on_light if pole == "light" else b.on_dark,
            "proposed_shade": proposal.amount,
            "proposed_value": proposal.value,
            "reason": proposal.reason,
            "toward": proposal.toward,
        }
    return {
        "canonical": b.canonical,
        "two_colour_mode": b.two_colour_mode,
        "max_additional": MAX_ADDITIONAL,
        "poles": poles,
        "signals": _signal_inks(brand),
    }


def _signal_inks(brand: Brand) -> dict:
    """Per signal role: the computed ink, the authored one, and BOTH ratios.

    His ruling of 2026-08-18 asks for an override that is "flagged-not-rejected
    with the contrast readout shown ... surface WCAG numbers for both candidates
    next to the override control so the decision is informed". A control that
    offered the choice without the two numbers would be asking the author to
    guess, which is the opposite of the ruling.

    The page never computes either ratio: both come from the engine's own
    `wcag_contrast`, so what the rail prints and what the flag says are one
    measurement.
    """
    out: dict[str, dict] = {}
    for role, background in brand.signals.by_role().items():
        computed = brand.ink(c.polarity(background))
        authored = brand.signals.foreground_override(role)
        # The other neutral is the only other candidate the system can offer --
        # rule 13's foreground exception is a choice between the brand's OWN black
        # and white, never an arbitrary colour.
        alternative = (
            brand.neutrals.white
            if computed.lower() == brand.neutrals.black.lower()
            else brand.neutrals.black
        )
        out[role] = {
            "background": background,
            "computed": computed,
            "authored": authored,
            "resolved": authored or computed,
            "computed_contrast": round(c.wcag_contrast(computed, background), 2),
            "alternative": alternative,
            "alternative_contrast": round(
                c.wcag_contrast(alternative, background), 2
            ),
            "authored_contrast": (
                round(c.wcag_contrast(authored, background), 2) if authored else None
            ),
            "aa": c.WCAG_AA_TEXT,
        }
    return out


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


class BrandBody(BaseModel):
    brand: dict = Field(..., description="an authored Brand, as JSON")


class EssentialsBody(BaseModel):
    id: str = "new-brand"
    font_family: str
    brand_colour: str


class NestingBody(BaseModel):
    """A host brand, some guests nested inside it, and the ground they sit on."""

    brand: dict = Field(..., description="the HOST brand, as JSON")
    guests: list[dict] = Field(
        default_factory=list, description="brands nested inside the host"
    )
    ground: str = Field(
        "light",
        description='the host root: "light", "dark", "brand" or "signal:<role>"',
    )


def _brand_of(payload: dict) -> Brand:
    """Every brand that arrives over the wire enters through here.

    `store.migrate` runs on the way in for the same reason it runs on a file
    read: a page or a script holding a brand authored before the base/root
    ruling would otherwise get a 400 naming a field it never chose. One entry
    point, one migration -- a second call site is how the two drift.
    """
    try:
        return Brand.model_validate(store.migrate(payload))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/fonts")
def fonts() -> dict:
    return {"families": installed_families()}


@router.get("/stages")
def stages() -> dict:
    return {"stages": [asdict(s) for s in growth.STAGES]}


@router.get("/rules")
def rules() -> dict:
    return {"rules": [asdict(r) for r in explain.RULES.values()]}


def _kit_rows() -> list[dict]:
    """Every design system, with the two things a LIBRARY row has to show.

    `store.list_kits` answers a question about the filesystem -- which ids exist
    and which of them may be written -- and that is the right shape for it. An
    overview asks a different question: it has to render a row a builder can
    RECOGNISE, and an id alone does not do that. The colour and the typeface are
    the two values a design system is identified by on sight, so they are
    resolved here rather than in the store.

    ENRICHED AT THE ROUTE, NOT IN THE STORE, and one call rather than N+1. The
    page already knows how to fan out `GET /kits/{id}` -- the GUESTS scene does
    exactly that -- and doing it again for a list would put four round trips
    behind a screen whose whole job is to appear at once.

    A KIT THAT WILL NOT LOAD IS STILL LISTED. A user store is a directory a
    human can edit, so a malformed file is a normal state rather than an
    exceptional one, and an overview that 500s because one row is broken hides
    the other three. The row comes back marked `unreadable` and the screen says
    so; `colour` and `font` are simply absent.
    """
    rows: list[dict] = []
    for row in store.list_kits():
        entry = dict(row)
        try:
            brand = store.load(row["id"])
        except Exception:
            logger.warning("design kit %r is listed but will not load", row["id"])
            entry["unreadable"] = True
        else:
            entry["colour"] = brand.brand.canonical
            entry["font"] = brand.font.family
        rows.append(entry)
    return rows


@router.get("/kits")
def list_kits() -> dict:
    """The library, plus WHICH ONE IS PAINTING THE APP.

    `active` is served here because nothing else exposes it. `_active_kit_id`
    is private and `/api/design/active` answers about the v0 profile store, so
    an overview had no way to mark the running system without guessing -- and a
    library that cannot say which entry is live is a list, not an overview.
    """
    return {"kits": _kit_rows(), "active": _active_kit_id()}


DEFAULT_KIT = "okuro-ds"
"""The brand the page opens ON, before anybody has chosen anything.

There has to be one. The page used to open on an empty form and render nothing
until a builder filled it in and waited for three more round trips, so the first
thing it said about a system that derives itself was a blank canvas.
"""


@router.get("/boot")
def boot() -> dict:
    """Everything the page needs to draw a live system on its FIRST PAINT.

    ONE call, on purpose. The page previously opened four independent queries
    for its vocabulary and then, only after the builder submitted a form, three
    more for the brand itself -- seven serial round trips before a single ground
    appeared, each one able to leave the canvas empty with no error. Latency was
    then indistinguishable from breakage, which is exactly what happened.

    Nothing here is new engine work: it is `stages` + `rules` + `kits` + the
    default kit + `resolve` + `emit`, answered together.
    """
    rows = _kit_rows()
    ids = [row["id"] for row in rows]
    chosen = DEFAULT_KIT if DEFAULT_KIT in ids else (ids[0] if ids else None)

    brand = store.load(chosen) if chosen else Brand()
    return {
        "brand": brand.model_dump(mode="json"),
        "model": resolved_model(brand),
        "sheet": emitter.emit(brand).css,
        "stages": [asdict(s) for s in growth.STAGES],
        "rules": [asdict(r) for r in explain.RULES.values()],
        # THE SAME ROWS THE LIST ROUTE SERVES, so the overview is complete on
        # the first paint rather than after a second query. Two shapes for one
        # concept is how the SYSTEMS scene would come to render a swatch on
        # reload and none on open.
        "kits": rows,
        "active": _active_kit_id(),
        "families": installed_families(),
        "opened": chosen,
    }


@router.get("/kits/{kit_id}")
def get_kit(kit_id: str) -> dict:
    try:
        brand = store.load(kit_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    notes = list(kits.get(kit_id).notes) if kit_id in kits.KITS else []
    return {
        "id": kit_id,
        "origin": "package" if kit_id in kits.KITS else "user",
        "editable": kit_id not in kits.KITS,
        "brand": brand.model_dump(mode="json"),
        "notes": notes,
        "flags": [_flag(f) for f in lint(brand)],
    }


@router.post("/kits", status_code=201)
def create_kit(body: BrandBody) -> dict:
    brand = _brand_of(body.brand)
    try:
        path = store.save(brand, create=True)
    except store.ShippedKit as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except store.KitExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": brand.id, "origin": "user", "path": str(path)}


@router.put("/kits/{kit_id}")
def save_kit(kit_id: str, body: BrandBody) -> dict:
    brand = _brand_of(body.brand)
    if brand.id != kit_id:
        raise HTTPException(
            status_code=400,
            detail=f"body declares brand id {brand.id!r} but the path says {kit_id!r}",
        )
    try:
        path = store.save(brand)
    except store.ShippedKit as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": brand.id, "origin": "user", "path": str(path)}


@router.delete("/kits/{kit_id}")
def delete_kit(kit_id: str) -> dict:
    try:
        store.delete(kit_id)
    except store.ShippedKit as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": kit_id, "deleted": True}


@router.post("/essentials")
def essentials(body: EssentialsBody) -> dict:
    """A typeface and a colour -> a whole valid brand. Writes nothing."""
    faces = next(
        (f["faces"] for f in installed_families() if f["family"] == body.font_family),
        None,
    )
    try:
        result = growth.brand_from_essentials(
            body.id, body.font_family, body.brand_colour, faces=faces
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "brand": result.brand.model_dump(mode="json"),
        "on_light": asdict(result.on_light),
        "on_dark": asdict(result.on_dark),
        "weight_note": result.weight_note,
        "flags": [_flag(f) for f in lint(result.brand)],
    }


@router.post("/resolve")
def resolve_brand(body: BrandBody) -> dict:
    return resolved_model(_brand_of(body.brand))


@router.post("/sheet")
def sheet(body: BrandBody) -> Response:
    brand = _brand_of(body.brand)
    rendered = emitter.emit(brand)
    return Response(content=rendered.css, media_type="text/css")


def _host_ground(resolver: Resolver, ground: str) -> Ground:
    """The host root a nesting scene sits on.

    Exactly the placements the page already offers on a root -- the user's two
    appearances, plus brand and the signals as builder placements. Resolved
    through `Resolver.root` rather than constructed here, so the nesting section
    cannot land on a ground the rest of the page could not produce.
    """
    if ground in ("light", "dark"):
        return resolver.root(ground)
    if ground == "brand":
        return resolver.root("light", Request(branded=True))
    if ground.startswith("signal:"):
        role = ground.split(":", 1)[1]
        if role not in resolver.brand.signals.by_role():
            raise HTTPException(status_code=400, detail=f"unknown signal {role!r}")
        return resolver.root("light", Request(signal=role))
    raise HTTPException(status_code=400, detail=f"unknown ground {ground!r}")


@router.post("/nesting")
def nesting(body: NestingBody) -> dict:
    """GUEST BRANDS ON A HOST GROUND -- his inbox story, resolved live.

        "we have nested brands inside light / dark or even branded themes
        ([host] theme as the main theme and UBS, Valiant, Mobiliar etc, as
        nested themes)"

    One host, one ground, several guests -- and each guest lands where register
    rule 4 sends it, not where a setting puts it. Two guests on the SAME ground
    can go different ways, which is the whole point: it is one rule read against
    each guest's own colours.

    The sheet comes from `emit(guests=...)`, so what the page renders is the
    real emitted CSS with the guests' own blocks in it, and the placement rows
    beside it are read back out of the same resolvers. A page that drew the
    outcome from the placement rows instead would be an illustration of the
    rule rather than evidence of it.
    """
    host = _brand_of(body.brand)
    guests = tuple(_brand_of(g) for g in body.guests)
    resolver = Resolver(host)
    ground = _host_ground(resolver, body.ground)
    rendered = emitter.emit(host, guests=guests)
    return {
        "css": rendered.css,
        "host": {
            "id": host.id,
            "key": emitter.ground_key(ground),
            "colour": ground.colour,
            "lightness": ground.lightness,
            "polarity": ground.polarity,
            "origin": ground.origin,
        },
        "placements": [
            asdict(explain.guest_placement(guest, ground)) for guest in guests
        ],
        "rule": asdict(explain.RULES["branded-ground"]),
        "flags": [_flag(f) for f in rendered.flags],
    }


@router.get("/sheet/{kit_id}.css")
def sheet_of_kit(kit_id: str) -> Response:
    try:
        brand = store.load(kit_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    sources = kits.get(kit_id).font_sources if kit_id in kits.KITS else None
    rendered = emitter.emit(brand, font_sources=sources)
    return Response(content=rendered.css, media_type="text/css")


#: The engine sheet, at a path a `<link rel="stylesheet">` can actually fetch.
#:
#: EVERYTHING ELSE IN THIS MODULE LIVES UNDER `/api/design-engine`, which the
#: orchestrator's bearer middleware gates. A stylesheet fetch cannot attach an
#: `Authorization` header, so `GET /api/design-engine/sheet/okuro-ds.css`
#: answers 401 to a browser — measured, not assumed. That is why P5a shipped the
#: capability and the app still rendered from `/tokens.css`: the sheet existed
#: and nothing could load it.
#:
#: The fix is the one `main.py` already names as the historical scar: a public
#: sheet "must NOT live under /api/*", because gating tokens that way "broke the
#: whole SPA twice". `/ds-tokens.css` is v1's answer to the same problem, on its
#: own prefix-less router. This is v2's, deliberately built the same way rather
#: than by adding an exemption prefix — an exemption widens the unauthenticated
#: surface under `/api/*` for every future route, and a separate router widens
#: nothing.
#:
#: NO KIT IN THE PATH. `/tokens.css` takes none either, and the app has exactly
#: one brand of its own; a kit parameter here would invite a caller to point the
#: whole SPA at a guest brand, which is precisely what the store module is
#: written as a refusal to allow.
public_router = APIRouter(tags=["design-engine"])

#: okuro's own kit. Named here rather than passed in — see the note above.
OKURO_KIT = "okuro-ds"


def _active_kit_id() -> str:
    """Which design system the app renders from.

    THE CHOICE LIVES ON THE PROFILE because `/engine.css` is fetched by a bare
    `<link rel="stylesheet">` that carries no state, so the preference has to be
    resolved server-side or not at all. It is now the ONLY thing the profile
    says about appearance: the accent that used to live beside it was retired on
    2026-09-06, so `design.kit` alone decides what the app is painted with.

    NOT A QUERY PARAMETER, and that is deliberate — see the note on
    `public_router` above. A kit id in the path would let any caller point the
    whole SPA at a guest brand, which is precisely what the store module is
    written as a refusal to allow. A stored choice is the same capability
    without the hole: the user picks, the server resolves.

    NOT `design.brand` either. That key meant a v1 stack brand, read by
    `web/app.py::_active_theme` until v0 was deleted; nothing has read it since,
    and the Settings control that wrote it was removed on 2026-09-06. Do not
    revive it here — one field answering two questions is how it went dead.

    Falls back to okuro's own kit whenever the profile is unreadable, names a
    kit that no longer exists, or names nothing — an app with no stylesheet is a
    worse failure than an app on the default one.
    """
    try:
        from okuro.yu.profile import get_profile_raw

        design = (get_profile_raw() or {}).get("design") or {}
        chosen = design.get("kit") or design.get("profile")
    except Exception:
        logger.warning("could not read the active kit; falling back to %s", OKURO_KIT)
        return OKURO_KIT
    if not isinstance(chosen, str) or not chosen.strip():
        return OKURO_KIT
    chosen = chosen.strip()
    if chosen == OKURO_KIT:
        return OKURO_KIT
    try:
        store.load(chosen)
    except Exception:
        logger.info(
            "the profile names design kit %r, which does not resolve; using %s",
            chosen, OKURO_KIT,
        )
        return OKURO_KIT
    return chosen


@public_router.get("/engine.css")
def engine_css() -> Response:
    """The ACTIVE design system, emitted, for the running app.

    `Cache-Control: no-store` matches `/ds-tokens.css`: the sheet is generated
    from the saved kit on every request, so a cached copy is a stale palette
    that survives an authoring change and looks like the engine got it wrong.

    THE ACTIVE KIT IS THE WHOLE ANSWER, and that is the point. A per-request
    accent used to be spliced in here for okuro's own kit — the one exception to
    "okuro-ds is not editable", and the owner retired it on 2026-09-06 so the
    sentence needs no asterisk: a design system is duplicated and then edited,
    never adjusted in place. What this route emits is now exactly what
    `/api/design-engine/sheet/{kit_id}.css` emits for the same kit, which is
    also what makes the Settings preview true rather than approximately true.
    """
    kit_id = _active_kit_id()
    brand = store.load(kit_id)
    sources = kits.get(kit_id).font_sources if kit_id in kits.KITS else None
    rendered = emitter.emit(brand, font_sources=sources)
    return Response(
        content=rendered.css,
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["router", "public_router", "resolved_model", "installed_families"]
