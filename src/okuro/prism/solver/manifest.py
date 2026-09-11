# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 — load the W1 component contract (component-manifest.json)
#   as the solver's placement vocabulary: per component, claim-shape fit-scores,
#   capacity, intrinsic col-span range, density cost, emphasis variants, height
#   model ref, provenance. Read-only over the W1 deliverable.
# index:
#   Component dataclass / load_manifest / get_component / components_for_shape
# AGENT_HEADER_END -->
"""The component manifest — the solver's placement vocabulary (W1 → W2 consume).

W1 shipped ``kit/board/component-manifest.json``: 37 typed components with
content-schema, claim-shape ``fit`` scores, ``capacity`` (min/typical/max/
unbounded), intrinsic ``size`` (min/max cols + height_model ref), ``density``
(raw base_px + per_item_px — NOT the W2 cross-model scalar), ``emphasis`` variants
and a provenance slot. This module loads it read-only and exposes the query the
solver needs: which components can carry a claim shape, at what size, with which
emphasis. Archetypes stay as L1 hooks; these components are the free-composition
vocabulary for L2/L3.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

# kit/board/component-manifest.json — the W1 deliverable.
_MANIFEST_JSON = (
    Path(__file__).resolve().parents[1] / "kit" / "board" / "component-manifest.json"
)


@dataclass(frozen=True)
class Component:
    id: str
    css: str
    tier: str
    role: str
    fit: dict[str, float]                 # claim_shape -> fit score [0,1]
    cap_min: int
    cap_typical: int
    cap_max: int
    unbounded: bool
    item_noun: str
    min_cols: int
    max_cols: int
    height_model: str
    reflows: bool
    base_px: float                        # raw density cost (W1 units)
    per_item_px: float
    emphasis: tuple[str, ...]
    provenance: bool
    text_bearing: bool

    def fit_for(self, shape: str) -> float:
        return float(self.fit.get(shape, 0.0))

    def accepts_emphasis(self, e: str) -> bool:
        return e in self.emphasis

    def can_hold(self, items: int) -> bool:
        if items < self.cap_min:
            return False
        return self.unbounded or items <= self.cap_max


def _mk(c: dict[str, Any]) -> Component:
    cap = c.get("capacity", {})
    size = c.get("size", {})
    dens = c.get("density", {})
    schema = c.get("content_schema", {})
    return Component(
        id=c["id"],
        css=c.get("css", ""),
        tier=c.get("tier", ""),
        role=c.get("role", ""),
        fit={k: float(v) for k, v in (c.get("fit") or {}).items()},
        cap_min=int(cap.get("min", 1)),
        cap_typical=int(cap.get("typical", 1)),
        cap_max=int(cap.get("max", 1)),
        unbounded=bool(cap.get("unbounded", False)),
        item_noun=str(cap.get("item_noun", "item")),
        min_cols=int(size.get("min_cols", 4)),
        max_cols=int(size.get("max_cols", 12)),
        height_model=str(size.get("height_model", c["id"])),
        reflows=bool(size.get("reflows", False)),
        base_px=float(dens.get("base_px", 0.0)),
        per_item_px=float(dens.get("per_item_px", 0.0)),
        emphasis=tuple(c.get("emphasis", [])),
        provenance=bool(c.get("provenance", False)),
        text_bearing=bool(schema.get("_text_bearing", False)),
    )


@lru_cache(maxsize=1)
def _raw() -> dict[str, Any]:
    return json.loads(_MANIFEST_JSON.read_text())


@lru_cache(maxsize=1)
def load_manifest() -> dict[str, Component]:
    """{component_id: Component}, cached and deterministic."""
    return {c["id"]: _mk(c) for c in _raw()["components"]}


def get_component(cid: str) -> Component:
    m = load_manifest()
    if cid not in m:
        raise KeyError(f"unknown component {cid!r}")
    return m[cid]


def claim_shapes() -> list[str]:
    return list(_raw().get("claim_shapes", []))


def emphasis_levels() -> list[str]:
    return list(_raw().get("emphasis", []))


def components_for_shape(shape: str, min_fit: float = 0.5) -> list[Component]:
    """Fit-set for a claim shape: components whose fit-score >= min_fit, best
    first (deterministic: fit desc, then id asc). This is the ``component in
    fit-set`` universe the boundary schema validates against."""
    m = load_manifest().values()
    hits = [c for c in m if c.fit_for(shape) >= min_fit]
    return sorted(hits, key=lambda c: (-c.fit_for(shape), c.id))


__all__ = [
    "Component",
    "load_manifest",
    "get_component",
    "claim_shapes",
    "emphasis_levels",
    "components_for_shape",
]
