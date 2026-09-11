# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism compiler INTERMEDIATE REPRESENTATION — the typed data that
#   flows between stages. Defines the 11 kit-aligned claim SHAPES and their
#   per-shape structured-field JSON schemas (the mine contract), plus dataclasses
#   for every stage IR (Claim, AudienceProfile, Projection, Outline, TieredTopic,
#   SlicedCell, PagePlan, ComposedDeck) with lossless dict (de)serialization so
#   the cache can content-hash and resume any stage.
# index: SHAPES | SHAPE_FIELD_SCHEMAS | CLAIM_SCHEMA | Claim | Evidence
#   | AudienceProfile | Projection | Outline | Topic | TieredClaim | MasterDoc
#   | PagePlan | PageCell | ComposedDeck | Slide | asdict/from_dict
# AGENT_HEADER_END -->
"""Typed IR for the prism compiler.

Shape vocabulary is the KIT-SPEC claim-shape set (artifact 37c7a92a §2 matrix),
NOT the legacy ``prism.shapes`` vocabulary — the compiler is a new namespace and
maps claims onto the shipped board kit's component/claim-shape matrix.

Every ``fields`` payload is a plain dict validated against
``SHAPE_FIELD_SCHEMAS[shape]``; the compound stage IRs are frozen-ish dataclasses
with ``to_dict`` / ``from_dict`` so a stage's whole output is one JSON blob the
cache hashes. Data on a Claim is IMMUTABLE after ``mine`` — later stages only
select, rank, tier, and place; they never rewrite a claim's numbers or words.
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass, field
from typing import Any, Optional


def tolerant(schema: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy a schema with every ``additionalProperties: False`` dropped.

    LLM-generated content routinely carries harmless extra keys (e.g. a claim's
    ``fields`` gaining a stray ``ratings``); forbidding them turns a re-ask into a
    non-converging game of whack-a-mole (proven on a live mine run).
    We validate the PRESENCE and TYPE of the keys we consume (``required`` +
    typed ``properties``) and simply ignore the rest — the downstream stages read
    only known keys. This is applied at each LLM-facing schema-build site.
    """
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()
                    if not (k == "additionalProperties" and v is False)}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(copy.deepcopy(schema))


# any structured value — the widest JSON leaf set, used for opaque field values.
_ANY_VALUE = {"type": ["object", "array", "string", "number", "boolean", "null"]}


def lenient_fields(fields_schema: dict[str, Any]) -> dict[str, Any]:
    """Derive a PERMISSIVE validator from a per-shape field schema.

    Allows the shape's top-level keys but requires NONE of them, and treats their
    VALUES as opaque structured data (any JSON type). Rationale (live-surfaced on
    the real customer + okuro-reference mine runs, DP10): nothing downstream reads
    field internals — project/outline/plan select on statement/shape/salience and
    compose hands raw ``fields`` to the LLM to map into kit slots, with synthetic
    fallback for empty fields — so deep item-type / item-required / min-max
    constraints only exhaust the bounded re-ask on real model output.

    Critically, ``required`` is now EMPTY. Keeping the shape's identity key required
    (e.g. a ``comparison`` must carry ``options``) meant ONE mislabeled/incomplete
    claim in a large mine failed the WHOLE batch — 3 re-asks couldn't fix it and the
    entire deck aborted at stage 1 (okuro-reference, 46k words: "at claims/7/fields:
    'options' is a required property"). An absent field key is harmless; the shape
    label still drives component selection. Dropping the requirement kills the
    whole-batch-abort class instead of guarding each shape by hand.
    """
    props = (fields_schema or {}).get("properties", {})
    return {
        "type": "object",
        "required": [],  # see docstring — one bad claim must not fail the whole mine
        "properties": {k: _ANY_VALUE for k in props},
    }


# ── 11 kit-aligned claim shapes (kit spec §2 component→claim-shape matrix) ──────
SHAPES: tuple[str, ...] = (
    "metric",        # one measured value (+ optional baseline)
    "delta",         # before -> after change
    "comparison",    # options × criteria
    "sequence",      # ordered steps / a linear flow
    "relationship",  # nodes + edges (deps / architecture)
    "proportion",    # parts of a whole / share
    "quote",         # a verbatim quotation
    "set",           # an unordered collection of peers
    "trend",         # a value over an ordered series
    "narrative",     # an assertion in prose
    "verdict",       # a ruling on a subject
)

# ── per-shape structured-field schemas (the mine contract, JSON-Schema draft) ───
# Kept deliberately permissive on scalar types (numbers often arrive as strings
# like "−92%" or "18 ms") — the STRUCTURE is what matters for slot mapping.
_STR = {"type": "string", "minLength": 1}
_SCALAR = {"type": ["string", "number"]}

SHAPE_FIELD_SCHEMAS: dict[str, dict[str, Any]] = {
    "metric": {
        "type": "object",
        "required": ["label", "value"],
        "additionalProperties": False,
        "properties": {
            "label": _STR,
            "value": _SCALAR,
            "unit": {"type": "string"},
            "baseline": {
                "type": "object",
                "properties": {"value": _SCALAR, "label": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    },
    "delta": {
        "type": "object",
        "required": ["label", "before", "after"],
        "additionalProperties": False,
        "properties": {
            "label": _STR, "before": _SCALAR, "after": _SCALAR,
            "unit": {"type": "string"}, "direction": {"enum": ["up", "down", "flat"]},
        },
    },
    "comparison": {
        "type": "object",
        "required": ["criteria", "options"],
        "additionalProperties": False,
        "properties": {
            "criteria": {"type": "array", "minItems": 1, "items": _STR},
            "options": {
                "type": "array", "minItems": 2, "maxItems": 5,
                "items": {
                    "type": "object",
                    "required": ["name", "values"],
                    "additionalProperties": False,
                    "properties": {
                        "name": _STR,
                        "recommended": {"type": "boolean"},
                        # criterion -> value. Ideally an object, but a live model
                        # routinely emits a checklist array (["✅"]) or a scalar
                        # string for comparison cells; nothing downstream does
                        # strict dict access on this (compose re-maps it via the
                        # LLM), so accept the shapes the model actually produces
                        # rather than exhaust the bounded re-ask (DP10, mirrors
                        # ir.tolerant's permissive philosophy). Live-surfaced on
                        # that mine run.
                        "values": {"type": ["object", "array", "string", "number"]},
                    },
                },
            },
        },
    },
    "sequence": {
        "type": "object",
        "required": ["steps"],
        "additionalProperties": False,
        "properties": {
            "steps": {
                "type": "array", "minItems": 2, "maxItems": 10,
                "items": {
                    "type": "object",
                    "required": ["what"],
                    "additionalProperties": False,
                    "properties": {
                        "what": _STR, "actor": {"type": "string"},
                        "tool": {"type": "string"}, "value": {"type": ["string", "number"]},
                    },
                },
            },
        },
    },
    "relationship": {
        "type": "object",
        "required": ["nodes", "edges"],
        "additionalProperties": False,
        "properties": {
            "nodes": {
                "type": "array", "minItems": 2,
                "items": {
                    "type": "object", "required": ["id", "label"],
                    "additionalProperties": False,
                    "properties": {"id": _STR, "label": _STR, "kind": {"type": "string"}},
                },
            },
            "edges": {
                "type": "array",
                "items": {
                    "type": "object", "required": ["from", "to"],
                    "additionalProperties": False,
                    "properties": {"from": _STR, "to": _STR, "kind": {"type": "string"}},
                },
            },
        },
    },
    "proportion": {
        "type": "object",
        "required": ["parts"],
        "additionalProperties": False,
        "properties": {
            "whole": {"type": ["number", "string"]},
            "unit": {"type": "string"},
            "parts": {
                "type": "array", "minItems": 2, "maxItems": 8,
                "items": {
                    "type": "object", "required": ["label", "value"],
                    "additionalProperties": False,
                    "properties": {
                        "label": _STR, "value": {"type": ["number", "string"]},
                        "level": {"enum": ["safe", "caution", "danger"]},
                    },
                },
            },
        },
    },
    "quote": {
        "type": "object",
        "required": ["text"],
        "additionalProperties": False,
        "properties": {"text": _STR, "attribution": {"type": "string"}},
    },
    "set": {
        "type": "object",
        "required": ["items"],
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array", "minItems": 2, "maxItems": 8,
                "items": {
                    "type": "object", "required": ["name"],
                    "additionalProperties": False,
                    "properties": {
                        "name": _STR, "desc": {"type": "string"},
                        "icon": {"type": "string"},
                    },
                },
            },
        },
    },
    "trend": {
        "type": "object",
        "required": ["label", "series"],
        "additionalProperties": False,
        "properties": {
            "label": _STR, "unit": {"type": "string"},
            "series": {
                "type": "array", "minItems": 2,
                "items": {
                    "type": "object", "required": ["x", "y"],
                    "additionalProperties": False,
                    "properties": {"x": _SCALAR, "y": {"type": ["number", "string"]}},
                },
            },
        },
    },
    "narrative": {
        "type": "object",
        "required": ["text"],
        "additionalProperties": False,
        "properties": {"text": _STR},
    },
    "verdict": {
        "type": "object",
        "required": ["subject", "ruling"],
        "additionalProperties": False,
        "properties": {
            "subject": _STR, "ruling": _STR, "rationale": {"type": "string"},
            "tone": {"enum": ["essential", "nogo", "redesign", "redundant", "ok", "warn"]},
        },
    },
}

# A single claim as emitted by `mine`. `evidence.quote` must appear in the source
# artifact body (grounding check lives in mine.py); `fields` matches the shape.
CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "statement", "shape", "fields", "evidence"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "pattern": r"^c\d+$"},
        "statement": _STR,          # one-line human summary (the L-slice prose source)
        "shape": {"enum": list(SHAPES)},
        "fields": {"type": "object"},
        "salience": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {
            "type": "object",
            "required": ["quote"],
            "additionalProperties": False,
            "properties": {"quote": _STR, "artifact_id": {"type": "string"}},
        },
    },
}

MINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["claims"],
    "additionalProperties": False,
    "properties": {
        "claims": {"type": "array", "items": CLAIM_SCHEMA},
        "coverage_note": {"type": "string"},
    },
}

LEVELS: tuple[int, ...] = (0, 1, 2, 3)
# Prose word budgets per level (narrative-shape text only; module data excluded).
PROSE_BUDGET: dict[int, int] = {0: 12, 1: 60, 2: 150, 3: 10_000}


# ── compound stage IRs ─────────────────────────────────────────────────────────
@dataclass
class Evidence:
    quote: str
    artifact_id: str = ""


@dataclass
class Claim:
    id: str
    statement: str
    shape: str
    fields: dict[str, Any]
    evidence: Evidence
    salience: float = 0.5
    grounded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "statement": self.statement, "shape": self.shape,
            "fields": self.fields, "evidence": dataclasses.asdict(self.evidence),
            "salience": self.salience, "grounded": self.grounded,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Claim":
        ev = d.get("evidence") or {}
        return cls(
            id=d["id"], statement=d["statement"], shape=d["shape"],
            fields=d.get("fields") or {},
            evidence=Evidence(quote=ev.get("quote", ""), artifact_id=ev.get("artifact_id", "")),
            salience=float(d.get("salience", 0.5)), grounded=bool(d.get("grounded", False)),
        )


@dataclass
class Recipient:
    """One named reader, resolved to a brand context."""
    name: str
    person_id: str = ""
    brand: str = ""              # resolved brand slug (disambiguated)
    role: str = ""
    lens: str = ""               # anonymized cognitive lens text (person_lens)
    sliders: dict[str, int] = field(default_factory=dict)
    source: str = "archetype"    # "people-graph" | "web" | "archetype"


@dataclass
class AudienceProfile:
    recipients: list[Recipient] = field(default_factory=list)
    jargon_tolerance: str = "medium"     # low | medium | high
    depth_ceiling: int = 3               # max level this audience should see
    # The SAME ceiling in the L-rung vocabulary the writer / arranger /
    # assembler speak. Two namespaces have carried the name `depth_ceiling`
    # with incompatible types and no conversion between them; carrying both,
    # derived from one function (prism.rungs.depth_to_rung), is what makes
    # them one namespace rather than two.
    depth_ceiling_rung: str = "L3"
    prefer_modules: list[str] = field(default_factory=list)
    avoid_modules: list[str] = field(default_factory=list)
    composite: bool = False              # True when >1 recipient blended
    needs_disambiguation: Optional[list[dict[str, Any]]] = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AudienceProfile":
        recips = [Recipient(**r) for r in d.get("recipients", [])]
        d = {**d, "recipients": recips}
        return cls(**d)


@dataclass
class ProjectedClaim:
    """A `project`-stage decision about a claim. Claim DATA is untouched."""
    claim_id: str
    keep: bool
    emphasis: float = 0.5        # 0..1 arrangement weight (drives L0 tier-0 pick)
    takeaway: str = ""           # the one-line so-what for this claim, for this audience

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class Projection:
    decisions: list[ProjectedClaim] = field(default_factory=list)
    depth_ceiling: int = 3

    def kept_ids(self) -> list[str]:
        return [d.claim_id for d in self.decisions if d.keep]

    def to_dict(self) -> dict[str, Any]:
        return {"decisions": [d.to_dict() for d in self.decisions], "depth_ceiling": self.depth_ceiling}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Projection":
        return cls(
            decisions=[ProjectedClaim(**x) for x in d.get("decisions", [])],
            depth_ceiling=int(d.get("depth_ceiling", 3)),
        )


@dataclass
class Topic:
    id: str
    title: str
    claim_ids: list[str] = field(default_factory=list)   # every kept claim in exactly one topic
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class Outline:
    topics: list[Topic] = field(default_factory=list)
    arc: str = ""                # the throughline narration

    def to_dict(self) -> dict[str, Any]:
        return {"topics": [t.to_dict() for t in self.topics], "arc": self.arc}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Outline":
        return cls(topics=[Topic(**t) for t in d.get("topics", [])], arc=d.get("arc", ""))


@dataclass
class TieredClaim:
    claim_id: str
    tier: int                    # 0..3 — appears at L>=tier

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class AuthoredTopic:
    topic_id: str
    master_doc: str              # the full L3 body (markdown)
    # per-level narrative spine (the word-budgeted prose slots). spine[3] is
    # always == master_doc; spine[0/1/2] are progressively longer summaries.
    spine: dict[int, str] = field(default_factory=dict)
    tiers: list[TieredClaim] = field(default_factory=list)

    def tier_of(self, claim_id: str) -> int:
        for t in self.tiers:
            if t.claim_id == claim_id:
                return t.tier
        return 3

    def to_dict(self) -> dict[str, Any]:
        return {"topic_id": self.topic_id, "master_doc": self.master_doc,
                "spine": {str(k): v for k, v in self.spine.items()},
                "tiers": [t.to_dict() for t in self.tiers]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AuthoredTopic":
        return cls(topic_id=d["topic_id"], master_doc=d["master_doc"],
                   spine={int(k): v for k, v in (d.get("spine") or {}).items()},
                   tiers=[TieredClaim(**t) for t in d.get("tiers", [])])


@dataclass
class Authored:
    topics: list[AuthoredTopic] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"topics": [t.to_dict() for t in self.topics]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Authored":
        return cls(topics=[AuthoredTopic.from_dict(t) for t in d.get("topics", [])])


class SlicerError(AssertionError):
    """A hard slicer invariant was violated (superset chain, tier-0, budgets)."""


@dataclass
class SlicedCell:
    """A topic sliced to one level: the claims visible + the narrative prose."""
    topic_id: str
    level: int
    claim_ids: list[str] = field(default_factory=list)   # claims with tier<=level
    prose: str = ""                                       # the level's spine text

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SlicedCell":
        return cls(**d)


@dataclass
class Slices:
    cells: list[SlicedCell] = field(default_factory=list)

    def cell(self, topic_id: str, level: int) -> Optional[SlicedCell]:
        for c in self.cells:
            if c.topic_id == topic_id and c.level == level:
                return c
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"cells": [c.to_dict() for c in self.cells]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Slices":
        return cls(cells=[SlicedCell.from_dict(c) for c in d.get("cells", [])])


@dataclass
class PageCell:
    topic_id: str
    level: int
    archetype: str               # one of the 12 kit archetypes (or "doc" at L3)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class PagePlan:
    cells: list[PageCell] = field(default_factory=list)
    topic_order: list[str] = field(default_factory=list)

    def cell(self, topic_id: str, level: int) -> Optional[PageCell]:
        for c in self.cells:
            if c.topic_id == topic_id and c.level == level:
                return c
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"cells": [c.to_dict() for c in self.cells], "topic_order": self.topic_order}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PagePlan":
        return cls(cells=[PageCell(**c) for c in d.get("cells", [])],
                   topic_order=list(d.get("topic_order", [])))


@dataclass
class Slide:
    """A composed, filled-slot slide the A4 renderer feeds into the kit template."""
    topic_id: str
    level: int
    archetype: str
    variant: str                 # "A" | "B"
    slots: dict[str, Any] = field(default_factory=dict)
    claim_ids: list[str] = field(default_factory=list)
    # True when the LLM could not produce a valid fill and compose fell back to a
    # code-synthesized minimal fill from claim data (fail-soft-per-cell). Surfaced
    # so the critic / A5 can flag it for review rather than shipping it silently.
    synthesized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Slide":
        return cls(**{k: v for k, v in d.items() if k in {
            "topic_id", "level", "archetype", "variant", "slots", "claim_ids",
            "synthesized"}})


@dataclass
class ComposedDeck:
    slides: list[Slide] = field(default_factory=list)
    brand: str = ""
    topic_order: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"slides": [s.to_dict() for s in self.slides],
                "brand": self.brand, "topic_order": self.topic_order}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ComposedDeck":
        return cls(slides=[Slide.from_dict(s) for s in d.get("slides", [])],
                   brand=d.get("brand", ""), topic_order=list(d.get("topic_order", [])))
