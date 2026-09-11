# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 ENTRY GATE — load + validate the frozen grid-family
#   parameter sets (grid-families.json). One GridFamily = the parameter tuple the
#   beam scorer reads (critique dim 4; v4.1 B4). Answer 7: the family is selected
#   PER DECK and drives placement via these params + the family-conformance term.
# index:
#   GridFamily dataclass
#   load_families / get_family / family_names
#   _validate_freeze (split_whitelist subset of layout._LEGAL_SPANS)
# AGENT_HEADER_END -->
"""Frozen grid families — the W2 entry-gate deliverable.

Each family reduces the layouting research to a machine-readable parameter tuple:
``split_whitelist`` (bounds candidate generation), ``dominance_ratio``,
``asymmetry_bias``, ``whitespace_floor``, ``alignment_strictness``,
``prose_measure_floor``, ``baseline_unit_px``, ``gutter_px`` and per-level
``density_ceilings``. Prose research that could not be reduced to a parameter is
logged in ``grid-families.json._excluded`` (entry-gate rule).

The freeze is validated on load: every family's ``split_whitelist`` must be a
subset of the shipped harmonic whitelist ``layout._LEGAL_SPANS`` — a family can
only ever be a RESTRICTION of the proven legal splits, never introduce an
off-ratio split. This is the anti-template-rigidity guard from the critique
(dim 7, risk 2: an under-sized/off-whitelist split_whitelist is how rigidity
returns).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from okuro.prism.layout import _LEGAL_SPANS

_FAMILIES_JSON = Path(__file__).resolve().parent / "grid-families.json"


@dataclass(frozen=True)
class GridFamily:
    """One frozen family's parameter tuple (critique dim 4)."""

    name: str
    label: str
    split_whitelist: tuple[tuple[int, ...], ...]
    dominance_ratio: float
    asymmetry_bias: float
    whitespace_floor: float
    max_void: float
    alignment_strictness: float
    prose_measure_floor: int
    gutter_px: int
    baseline_unit_px: int
    density_ceilings: dict[str, float]

    def density_ceiling(self, level: str) -> float:
        """Per-level density ceiling (L1<L2<L3). Unknown level -> the L3 max."""
        return float(self.density_ceilings.get(level, self.density_ceilings["L3"]))

    def allows(self, spans: tuple[int, ...]) -> bool:
        return tuple(spans) in self.split_whitelist


def _as_span_tuples(rows: list[list[int]]) -> tuple[tuple[int, ...], ...]:
    return tuple(tuple(int(x) for x in r) for r in rows)


def _validate_freeze(fam: GridFamily) -> None:
    """Freeze invariant: split_whitelist subset of layout._LEGAL_SPANS, and the
    density ladder is strictly monotonic L1<L2<L3 (v4 §3c)."""
    for spans in fam.split_whitelist:
        assert spans in _LEGAL_SPANS, (
            f"family {fam.name}: split {spans} not in layout._LEGAL_SPANS "
            f"(off-harmonic split — freeze violation)"
        )
    dc = fam.density_ceilings
    assert dc["L1"] < dc["L2"] < dc["L3"], (
        f"family {fam.name}: density_ceilings not monotonic L1<L2<L3: {dc}"
    )
    assert 0.0 <= fam.whitespace_floor < 1.0, f"family {fam.name}: bad whitespace_floor"
    assert fam.whitespace_floor < fam.max_void < 1.0, (
        f"family {fam.name}: need whitespace_floor < max_void < 1 "
        f"(min-void {fam.whitespace_floor} / max-void {fam.max_void})"
    )
    assert fam.dominance_ratio >= 1.0, f"family {fam.name}: dominance_ratio < 1"


@lru_cache(maxsize=1)
def _raw() -> dict[str, Any]:
    return json.loads(_FAMILIES_JSON.read_text())


@lru_cache(maxsize=1)
def load_families() -> dict[str, GridFamily]:
    """Load, build, and freeze-validate every family. Cached (deterministic)."""
    raw = _raw()
    out: dict[str, GridFamily] = {}
    for name, f in raw["families"].items():
        fam = GridFamily(
            name=name,
            label=f["label"],
            split_whitelist=_as_span_tuples(f["split_whitelist"]),
            dominance_ratio=float(f["dominance_ratio"]),
            asymmetry_bias=float(f["asymmetry_bias"]),
            whitespace_floor=float(f["whitespace_floor"]),
            max_void=float(f["max_void"]),
            alignment_strictness=float(f["alignment_strictness"]),
            prose_measure_floor=int(f["prose_measure_floor"]),
            gutter_px=int(f["gutter_px"]),
            baseline_unit_px=int(f["baseline_unit_px"]),
            density_ceilings={k: float(v) for k, v in f["density_ceilings"].items()},
        )
        _validate_freeze(fam)
        out[name] = fam
    return out


def get_family(name: str) -> GridFamily:
    fams = load_families()
    if name not in fams:
        raise KeyError(f"unknown grid family {name!r}; known: {sorted(fams)}")
    return fams[name]


def family_names() -> list[str]:
    return sorted(load_families())


def columns() -> int:
    return int(_raw()["grid"]["columns"])


def content_w_px() -> int:
    return int(_raw()["grid"]["content_w_px"])


__all__ = [
    "GridFamily",
    "load_families",
    "get_family",
    "family_names",
    "columns",
    "content_w_px",
]
