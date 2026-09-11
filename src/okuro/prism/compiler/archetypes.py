# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism compiler ARCHETYPE REGISTRY — the machine-readable contract
#   for the 12 shipped board archetypes (kit board/archetypes/01..12.html). Per
#   archetype: its data-slot names, a per-slot fill contract (what shape of JSON
#   compose must emit for the A4 renderer), a density class (drives the plan
#   dense-adjacent-dense L1 ban), and the claim-shape -> archetype affinity used
#   by the plan stage to assign an archetype to each topic×level cell.
# index: ARCHETYPES | ARCHETYPE_IDS | DENSITY | SHAPE_AFFINITY | slots_of
#   | fill_schema | archetypes_for_shape | level_pool
# AGENT_HEADER_END -->
"""Archetype knowledge for the plan + compose stages.

The kit (Wave-1 A2) OWNS all styling; this module only names what the kit
templates expose. Slot names are extracted verbatim from the ``data-slot``
markers in ``src/okuro/prism/kit/board/archetypes/*.html`` — the source of
truth. ``FILL_SCHEMAS`` is the JSON contract compose fills and A4 renders; it is
the acceptance oracle for the compose schema-contract test.

The LLM never picks styling or geometry here — the plan stage picks an archetype
from the deterministic ``SHAPE_AFFINITY`` allowance (a discrete selection under
code-checked quotas), and compose maps claim fields into these named slots.
"""

from __future__ import annotations

from typing import Any, Optional

# ── the 12 shipped board archetypes (kit board/archetypes/NN-<id>.html) ─────────
ARCHETYPE_IDS: tuple[str, ...] = (
    "hero",                  # 01
    "lens-reframe",          # 02
    "disclosure-list",       # 03
    "scenario-beats",        # 04
    "decision-matrix",       # 05
    "framework-tiles",       # 06
    "binary-choice",         # 07
    "classification-table",  # 08
    "flow-sequence",         # 09
    "proportion-bars",       # 10
    "card-set",              # 11
    "ask-cta",               # 12
)

# data-slot names per archetype (grep of the kit templates).
SLOTS: dict[str, tuple[str, ...]] = {
    "hero": ("eyebrow", "title", "lede", "personas", "meta"),
    "lens-reframe": ("eyebrow", "title", "lens-tabs", "lens-pane"),
    "disclosure-list": ("eyebrow", "title", "rows"),
    "scenario-beats": ("eyebrow", "title", "beats", "callout"),
    "decision-matrix": ("eyebrow", "title", "options"),
    "framework-tiles": ("eyebrow", "title", "tiles", "table"),
    "binary-choice": ("eyebrow", "title", "paths", "callout"),
    "classification-table": ("eyebrow", "title", "callout", "table"),
    "flow-sequence": ("eyebrow", "title", "lede", "steps"),
    "proportion-bars": ("eyebrow", "title", "lede", "rows"),
    "card-set": ("eyebrow", "title", "stats", "cards"),
    "ask-cta": ("eyebrow", "title", "ask", "arch"),
}

# Density class — used by plan's dense-adjacent-dense ban at L1 and by the
# level pool (L0 wants sparse, L2 tolerates dense). Derived from the kit's own
# per-archetype content load (rows/tables/steps = dense; a hero/CTA = sparse).
DENSITY: dict[str, str] = {
    "hero": "sparse",
    "ask-cta": "sparse",
    "binary-choice": "medium",
    "scenario-beats": "medium",
    "card-set": "medium",
    "lens-reframe": "medium",
    "decision-matrix": "dense",
    "disclosure-list": "dense",
    "framework-tiles": "dense",
    "classification-table": "dense",
    "flow-sequence": "dense",
    "proportion-bars": "dense",
}

# Claim-shape -> archetypes that can FAITHFULLY carry it (kit spec §2 matrix).
# Ordered best-fit first; the plan stage prefers earlier entries but may fall
# back under quota pressure. A shape's archetypes are its expressiveness set.
SHAPE_AFFINITY: dict[str, tuple[str, ...]] = {
    "metric":       ("card-set", "framework-tiles", "hero"),
    "delta":        ("framework-tiles", "classification-table", "binary-choice"),
    "comparison":   ("decision-matrix", "binary-choice", "classification-table", "framework-tiles"),
    "sequence":     ("flow-sequence", "scenario-beats"),
    "relationship": ("ask-cta", "flow-sequence", "scenario-beats"),
    "proportion":   ("proportion-bars", "framework-tiles"),
    "quote":        ("disclosure-list", "hero"),
    "set":          ("card-set", "disclosure-list", "lens-reframe"),
    "trend":        ("proportion-bars", "flow-sequence"),
    "narrative":    ("hero", "disclosure-list", "binary-choice"),
    "verdict":      ("classification-table", "ask-cta", "disclosure-list"),
}

# DENSITY_RANK — a finer 1-4 module-load scale (than the coarse DENSITY class used
# for the horizontal dense-ban). It drives the LEVEL POOLS and the vertical
# density-MONOTONICITY quota: a deeper level must never render LESS than a
# shallower one, or the progressive-disclosure superset ladder reads backwards
# (design v2 §5 density bands, finally wired into plan). Rank by real module
# capacity. Kept beside DENSITY as the density single-source.
DENSITY_RANK: dict[str, int] = {
    "hero": 1, "ask-cta": 1,                                          # sparse cover
    "card-set": 2, "binary-choice": 2, "lens-reframe": 2,            # mid
    "decision-matrix": 3, "framework-tiles": 3,                       # mid-dense
    "flow-sequence": 3, "scenario-beats": 3,
    "disclosure-list": 4, "classification-table": 4, "proportion-bars": 4,   # dense
}

# Which archetypes are appropriate at each level, by density band (design v2 §5):
# L0 = sparse cover (rank 1); L1 = rank >=2; L2 = rank >=3 (always denser than the
# rank-1 cover). L3 = the doc-view. The pools set the floor; the monotonicity
# quota enforces non-decreasing rank down each column.
LEVEL_POOL: dict[int, tuple[str, ...]] = {
    0: tuple(a for a in ARCHETYPE_IDS if DENSITY_RANK[a] == 1),
    1: tuple(a for a in ARCHETYPE_IDS if DENSITY_RANK[a] >= 2),
    2: tuple(a for a in ARCHETYPE_IDS if DENSITY_RANK[a] >= 3),
    3: ("doc",),
}

# ── per-slot fill contract: {archetype: {slot: json-schema}} ────────────────────
# compose emits exactly this; the A4 renderer maps it into the kit template. Only
# the CONTENT-bearing slots are schema'd; eyebrow/title/lede are plain strings
# (title may embed one "<accent>…</accent>" span — the kit's single accent rule).
_TITLE = {"type": "string", "minLength": 1}
_PERSONA = {
    "type": "object", "required": ["name", "lens"], "additionalProperties": False,
    "properties": {"name": {"type": "string"}, "lens": {"type": "string"},
                   "mark": {"enum": ["a", "b", "c"]}},
}
_KV = {"type": "object", "required": ["label", "value"], "additionalProperties": False,
       "properties": {"label": {"type": "string"}, "value": {"type": "string"}}}
_ROW = {
    "type": "object", "required": ["title"], "additionalProperties": False,
    "properties": {"mark": {"type": "string"}, "title": {"type": "string"},
                   "sub": {"type": "string"}, "body": {"type": "string"},
                   "severity": {"enum": ["critical", "major", "moderate", ""]}},
}

FILL_SCHEMAS: dict[str, dict[str, dict[str, Any]]] = {
    "hero": {
        "personas": {"type": "array", "maxItems": 4, "items": _PERSONA},
        "meta": {"type": "array", "maxItems": 5, "items": _KV},
    },
    "lens-reframe": {
        "lens-tabs": {"type": "array", "minItems": 2, "maxItems": 4, "items": _PERSONA},
        "lens-pane": {
            "type": "array", "minItems": 2, "maxItems": 4,
            "items": {"type": "array", "maxItems": 7, "items": _ROW},  # rows per pane
        },
    },
    "disclosure-list": {
        "rows": {"type": "array", "minItems": 3, "maxItems": 7, "items": _ROW},
    },
    "scenario-beats": {
        "beats": {
            "type": "array", "minItems": 3, "maxItems": 10,
            "items": {"type": "object", "required": ["time", "body"],
                      "additionalProperties": False,
                      "properties": {"time": {"type": "string"}, "body": {"type": "string"},
                                     "actor": {"type": "string"}}},
        },
        "callout": {"type": "object", "required": ["text"], "additionalProperties": False,
                    "properties": {"label": {"type": "string"}, "text": {"type": "string"},
                                   "tone": {"enum": ["info", "warn"]}}},
    },
    "decision-matrix": {
        "options": {
            "type": "array", "minItems": 3, "maxItems": 5,
            "items": {"type": "object", "required": ["name", "lines"],
                      "additionalProperties": False,
                      "properties": {"label": {"type": "string"}, "name": {"type": "string"},
                                     "recommended": {"type": "boolean"},
                                     "lines": {"type": "array", "items": _KV}}},
        },
    },
    "framework-tiles": {
        "tiles": {"type": "array", "minItems": 3, "maxItems": 5,
                  "items": {"type": "object", "required": ["value", "label"],
                            "additionalProperties": False,
                            "properties": {"label": {"type": "string"},
                                           "value": {"type": "string"},
                                           "desc": {"type": "string"}}}},
        "table": {"type": "object", "required": ["headers", "rows"],
                  "additionalProperties": False,
                  "properties": {"headers": {"type": "array", "items": {"type": "string"}},
                                 "rows": {"type": "array",
                                          "items": {"type": "array", "items": {"type": "string"}}}}},
    },
    "binary-choice": {
        "paths": {
            "type": "array", "minItems": 2, "maxItems": 2,
            "items": {"type": "object", "required": ["title", "tone"],
                      "additionalProperties": False,
                      "properties": {"eyebrow": {"type": "string"}, "title": {"type": "string"},
                                     "body": {"type": "string"}, "footer": {"type": "string"},
                                     "tone": {"enum": ["warn", "recommend"]}}},
        },
        "callout": {"type": "object", "required": ["text"], "additionalProperties": False,
                    "properties": {"label": {"type": "string"}, "text": {"type": "string"},
                                   "tone": {"enum": ["info", "warn"]}}},
    },
    "classification-table": {
        "callout": {"type": "object", "required": ["total"], "additionalProperties": False,
                    "properties": {"total": {"type": "string"},
                                   "stats": {"type": "array",
                                             "items": {"type": "object",
                                                       "required": ["label", "count"],
                                                       "additionalProperties": False,
                                                       "properties": {"label": {"type": "string"},
                                                                      "count": {"type": "string"},
                                                                      "tone": {"type": "string"}}}}}},
        "table": {"type": "object", "required": ["headers", "rows"],
                  "additionalProperties": False,
                  "properties": {"headers": {"type": "array", "items": {"type": "string"}},
                                 "rows": {"type": "array",
                                          "items": {"type": "object", "required": ["cells"],
                                                    "additionalProperties": False,
                                                    "properties": {"cells": {"type": "array",
                                                                             "items": {"type": "string"}},
                                                                   "rowclass": {"enum": ["essential", "nogo",
                                                                                         "redesign", "redundant", ""]}}}}}},
    },
    "flow-sequence": {
        "steps": {
            "type": "array", "minItems": 4, "maxItems": 8,
            # `n` is the step ORDINAL — derivable, never asked of the model: it is
            # optional here and typed strictly as an integer (compose auto-numbers
            # it 1..N post-model, see ORDINAL_SLOTS). Keeps step LABELS in `what`.
            "items": {"type": "object", "required": ["what"],
                      "additionalProperties": False,
                      "properties": {"n": {"type": "integer"},
                                     "what": {"type": "string"}, "tool": {"type": "string"},
                                     "value": {"type": "string"},
                                     "actor": {"enum": ["user", "edge", "cloud", ""]}}},
        },
    },
    "proportion-bars": {
        "rows": {
            "type": "array", "minItems": 2, "maxItems": 8,
            "items": {"type": "object", "required": ["label", "pct"],
                      "additionalProperties": False,
                      "properties": {"label": {"type": "string"},
                                     "pct": {"type": ["number", "integer"], "minimum": 0, "maximum": 100},
                                     "value": {"type": "string"},
                                     "level": {"enum": ["safe", "caution", "danger"]}}},
        },
    },
    "card-set": {
        "stats": {"type": "array", "maxItems": 4,
                  "items": {"type": "object", "required": ["num", "label"],
                            "additionalProperties": False,
                            "properties": {"num": {"type": "string"}, "label": {"type": "string"},
                                           "tone": {"enum": ["ok", "info", "gold", "warn", ""]}}}},
        "cards": {"type": "array", "minItems": 3, "maxItems": 5,
                  "items": {"type": "object", "required": ["name"],
                            "additionalProperties": False,
                            "properties": {"icon": {"type": "string"}, "name": {"type": "string"},
                                           "desc": {"type": "string"}}}},
    },
    "ask-cta": {
        "ask": {"type": "object", "required": ["title", "items"], "additionalProperties": False,
                "properties": {"title": {"type": "string"},
                               "items": {"type": "array", "minItems": 1, "items": {"type": "string"}}}},
        "arch": {"type": "array", "maxItems": 4,
                 "items": {"type": "object", "required": ["label", "value"],
                           "additionalProperties": False,
                           "properties": {"label": {"type": "string"}, "value": {"type": "string"},
                                          "state": {"enum": ["ok", "broken", ""]}}}},
    },
}


# Decorative / genuinely-optional content slots per archetype — the A4 renderer
# guards these with `s.<slot>?.` (deck2/archetypes.tsx), so compose must NOT force
# them. Forcing an optional slot made a live model fill e.g. ask-cta `arch` with
# prose (type mismatch, re-ask exhaustion). Everything else in FILL_SCHEMAS is the
# archetype's essential content and stays required. Mirrors the `?` optionality in
# deck2/deck-types.ts. (Same robustness class as ir.lenient_fields / author tiers,
# but compose output is LOAD-BEARING for A4, so we only relax REQUIRED-ness, never
# the type of a slot the model does emit.)
OPTIONAL_SLOTS: dict[str, frozenset[str]] = {
    "hero": frozenset({"personas", "meta"}),
    "scenario-beats": frozenset({"callout"}),
    "framework-tiles": frozenset({"table"}),
    "binary-choice": frozenset({"callout"}),
    "classification-table": frozenset({"callout"}),
    "card-set": frozenset({"stats"}),
    "ask-cta": frozenset({"arch"}),
}


def optional_slots(archetype: str) -> frozenset[str]:
    return OPTIONAL_SLOTS.get(archetype, frozenset())


# ORDINAL slots — (array_slot, ordinal_field) whose value is a 1..N sequence
# number. Ordinals are DERIVABLE, so compose auto-numbers them in code and never
# asks the model (which fills them with labels). Schema-adjacent single source.
ORDINAL_SLOTS: dict[str, tuple[str, str]] = {
    "flow-sequence": ("steps", "n"),
}


def slots_of(archetype: str) -> tuple[str, ...]:
    return SLOTS.get(archetype, ())


def fill_schema(archetype: str) -> dict[str, dict[str, Any]]:
    """The content-slot JSON schemas compose must satisfy for this archetype."""
    return FILL_SCHEMAS.get(archetype, {})


def archetypes_for_shape(shape: str) -> tuple[str, ...]:
    return SHAPE_AFFINITY.get(shape, ("disclosure-list",))


def level_pool(level: int) -> tuple[str, ...]:
    return LEVEL_POOL.get(level, ARCHETYPE_IDS)


def is_dense(archetype: str) -> bool:
    return DENSITY.get(archetype, "medium") == "dense"


def density_rank(archetype: str) -> int:
    """Fine 1-4 module-load rank (drives the vertical density-monotonicity ladder)."""
    return DENSITY_RANK.get(archetype, 2)


# ── cardinality: archetype slot capacity vs a claim's collection size ────────────
# The top-level array slot each archetype fills 1:1 from a single claim's
# collection. Its [minItems, maxItems] (read from FILL_SCHEMAS) is the number of
# items it can FAITHFULLY hold — assigning a claim whose collection is outside
# that band produces junk (e.g. a 6-option comparison in binary-choice's 2 paths,
# or a 3-phase roadmap in flow-sequence's 4-step minimum). Archetypes NOT listed
# here have no claim-collection primary slot (hero, ask-cta, classification-table's
# open-ended table, lens-reframe's audience tabs) and impose no cardinality bound.
PRIMARY_ARRAY_SLOT: dict[str, str] = {
    "binary-choice": "paths",
    "decision-matrix": "options",
    "framework-tiles": "tiles",
    "card-set": "cards",
    "disclosure-list": "rows",
    "scenario-beats": "beats",
    "flow-sequence": "steps",
    "proportion-bars": "rows",
}

# The claim SHAPES each DATA-BEARING archetype can FAITHFULLY render (its primary
# slot encodes a shape-specific visualization — a proportion bar, a step chain, an
# option matrix). Assigning a mismatched shape renders empty/overflowing junk (a
# non-proportion claim in proportion-bars → empty bars). This is expressiveness
# (HARD), the inverse-and-authority of SHAPE_AFFINITY (which only ORDERS the
# preferred picks). Kept next to PRIMARY_ARRAY_SLOT as the single source.
ARCHETYPE_SHAPES: dict[str, frozenset[str]] = {
    "proportion-bars": frozenset({"proportion", "trend"}),
    "flow-sequence": frozenset({"sequence"}),
    "scenario-beats": frozenset({"sequence"}),
    "decision-matrix": frozenset({"comparison"}),
    "binary-choice": frozenset({"comparison", "delta"}),
    "classification-table": frozenset({"verdict", "comparison", "set"}),
    "framework-tiles": frozenset({"metric", "proportion", "delta", "comparison"}),
    "card-set": frozenset({"set", "metric"}),
}
# Shape-AGNOSTIC archetypes render generic titles/statements/rows, never a
# shape-specific viz that can look empty — safe for ANY dominant claim shape and
# the solver's feasibility fallback.
SHAPE_AGNOSTIC: frozenset[str] = frozenset({"hero", "ask-cta", "disclosure-list", "lens-reframe"})

# A claim shape's collection field — its cardinality is what must fit a slot.
# Scalar shapes (metric/delta/quote/narrative/verdict) have no collection.
COLLECTION_FIELD: dict[str, str] = {
    "comparison": "options",
    "sequence": "steps",
    "set": "items",
    "proportion": "parts",
    "trend": "series",
    "relationship": "nodes",
}


def collection_capacity(archetype: str) -> tuple[int, float]:
    """(min, max) items the archetype's primary claim-collection slot can hold,
    derived from FILL_SCHEMAS. (1, inf) when it has no such bounded slot."""
    slot = PRIMARY_ARRAY_SLOT.get(archetype)
    if not slot:
        return (1, float("inf"))
    spec = FILL_SCHEMAS.get(archetype, {}).get(slot, {})
    return (int(spec.get("minItems", 1)), float(spec.get("maxItems", float("inf"))))


def shape_item_count(shape: str, fields: dict[str, Any]) -> Optional[int]:
    """The cardinality of a claim's collection, or None for a scalar shape."""
    key = COLLECTION_FIELD.get(shape)
    if not key:
        return None
    val = (fields or {}).get(key)
    return len(val) if isinstance(val, list) else None


def fits_cardinality(archetype: str, item_count: Optional[int]) -> bool:
    """True unless the dominant claim's collection size falls outside the
    archetype's primary-slot capacity. A scalar claim (item_count None) imposes no
    bound — the slot is then filled from multiple claims / compose flexibility."""
    if item_count is None:
        return True
    lo, hi = collection_capacity(archetype)
    return lo <= item_count <= hi


def accepts_shape(archetype: str, shape: str) -> bool:
    """True if the archetype can FAITHFULLY render the dominant claim's shape.
    Shape-agnostic containers accept any shape; a data-bearing archetype accepts
    only the shapes its primary slot encodes (proportion-bars ⊄ comparison, so a
    comparison claim never lands in empty bars)."""
    if archetype in SHAPE_AGNOSTIC:
        return True
    return shape in ARCHETYPE_SHAPES.get(archetype, frozenset())
