# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM P4.1 — the ARRANGEMENT BRIDGE. Turns the 11 arr-* entries in
#   library/layouts.json (the human/LLM-readable decision layer) into a typed,
#   freeze-validated arr-id -> span-tuple map the solver can actually consume.
#   Before this, the arrangements had ZERO consumers: the agent could read the
#   layout_selection_guide, form a judgment, and had no way to express it.
# index:
#   Arrangement dataclass
#   load_arrangements / get_arrangement / arrangement_ids
#   arrangements_for_family / splits_for_arrangements / arrangement_ids_for_spans
#   label_rows (reverse label: solved Placement -> arrangement id per row)
#   _validate_bridge (the freeze: sum-12, _LEGAL_SPANS, per-family whitelist,
#     bidirectional agreement with family-*.legal_arrangements)
# AGENT_HEADER_END -->
"""The arrangement <-> span-tuple bridge.

``library/layouts.json`` already carried each arrangement's ``splits`` as span
tuples; what did not exist was a CONSUMER and a validated contract between that
library and the frozen ``solver/grid-families.json`` whitelists. Two independent
encodings of the same fact drifted silently — the exact trap this campaign has
hit four times.

This module is that contract. Every arrangement is validated on load against
FOUR invariants, and a violation refuses the load rather than degrading:

1. every split sums to the 12-column grid;
2. every split is in ``layout._LEGAL_SPANS`` (the same harmonic freeze the
   families obey — an arrangement can only ever RESTRICT the proven legal
   splits, never introduce an off-ratio one);
3. for every family an arrangement declares itself legal in, all of its splits
   are in THAT family's ``split_whitelist``;
4. the relation is symmetric: ``arr in family-X.legal_arrangements`` if and only
   if ``"X" in arr.families``.

Invariant 4 is what makes the two encodings one fact. It is asserted, not
assumed: the bridge refuses to load a layouts.json where the two lists disagree.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from okuro.prism.layout import _LEGAL_SPANS
from okuro.prism.solver.candidates import Placement
from okuro.prism.solver.families import columns, get_family, load_families

_LAYOUTS_JSON = Path(__file__).resolve().parents[1] / "library" / "layouts.json"

#: kind discriminators inside layouts.json entries[]
_KIND_ARRANGEMENT = "arrangement"
_KIND_FAMILY = "grid-family"


class ArrangementError(ValueError):
    """A layouts.json <-> grid-families.json contract violation (freeze failure)."""


@dataclass(frozen=True)
class Arrangement:
    """One legal row grammar, keyed by its library id (``arr-hero-rail``).

    ``splits`` is the span-tuple map this bridge exists to provide: the English
    ``one_liner`` says "dominant artifact leads a supporting rail", the splits
    say ``((8, 4), (4, 8))``. Multi-row arrangements (``arr-z-pattern``) carry
    one tuple per row; single-shape arrangements carry their orientations.
    """

    id: str
    name: str
    splits: tuple[tuple[int, ...], ...]
    families: frozenset[str]
    info_depths: frozenset[str]
    density: str

    def legal_in(self, family: str) -> bool:
        return family in self.families

    def allows(self, spans: Iterable[int]) -> bool:
        return tuple(spans) in self.splits


def _as_span_tuples(rows: list[list[int]]) -> tuple[tuple[int, ...], ...]:
    return tuple(tuple(int(x) for x in r) for r in rows)


@lru_cache(maxsize=1)
def _raw() -> dict[str, Any]:
    return json.loads(_LAYOUTS_JSON.read_text())


def _validate_bridge(arrs: dict[str, Arrangement], entries: dict[str, dict]) -> None:
    """The freeze. Raises ArrangementError on the first violation, naming it."""
    ncols = columns()
    fams = load_families()

    for a in arrs.values():
        if not a.splits:
            raise ArrangementError(f"{a.id}: no splits declared")
        if not a.families:
            raise ArrangementError(f"{a.id}: declares no legal family")
        for spans in a.splits:
            # 1. sums to the grid
            if sum(spans) != ncols:
                raise ArrangementError(
                    f"{a.id}: split {spans} sums to {sum(spans)}, not {ncols}"
                )
            # 2. harmonic freeze — same rule the families obey
            if spans not in _LEGAL_SPANS:
                raise ArrangementError(
                    f"{a.id}: split {spans} not in layout._LEGAL_SPANS "
                    f"(off-harmonic — an arrangement may only restrict, never widen)"
                )
        # 3. every declared family really whitelists every split
        for fname in sorted(a.families):
            if fname not in fams:
                raise ArrangementError(f"{a.id}: unknown family {fname!r}")
            fam = fams[fname]
            for spans in a.splits:
                if spans not in fam.split_whitelist:
                    raise ArrangementError(
                        f"{a.id}: split {spans} not in family {fname!r} "
                        f"split_whitelist {list(fam.split_whitelist)}"
                    )

    # 4. the relation is symmetric — the two encodings are ONE fact.
    for fname in sorted(fams):
        key = f"family-{fname}"
        entry = entries.get(key)
        if entry is None:
            raise ArrangementError(f"layouts.json has no {key} entry for frozen family")
        declared = set(entry.get("legal_arrangements", ()))
        claimed = {aid for aid, a in arrs.items() if fname in a.families}
        if declared != claimed:
            raise ArrangementError(
                f"family {fname!r}: legal_arrangements and arrangement.families "
                f"disagree — only in family list: {sorted(declared - claimed)}; "
                f"only in arrangement list: {sorted(claimed - declared)}"
            )


@lru_cache(maxsize=1)
def load_arrangements() -> dict[str, Arrangement]:
    """Load, build and freeze-validate every arrangement. Cached (deterministic)."""
    entries = {e["id"]: e for e in _raw()["entries"]}
    arrs: dict[str, Arrangement] = {}
    for eid, e in entries.items():
        if e.get("kind") != _KIND_ARRANGEMENT:
            continue
        arrs[eid] = Arrangement(
            id=eid,
            name=e["name"],
            splits=_as_span_tuples(e["splits"]),
            families=frozenset(e["families"]),
            info_depths=frozenset(e.get("info_depths", ())),
            density=str(e.get("density", "")),
        )
    if not arrs:
        raise ArrangementError("layouts.json declares no arrangements")
    _validate_bridge(arrs, entries)
    return arrs


def get_arrangement(arr_id: str) -> Arrangement:
    arrs = load_arrangements()
    if arr_id not in arrs:
        raise KeyError(f"unknown arrangement {arr_id!r}; known: {sorted(arrs)}")
    return arrs[arr_id]


def arrangement_ids() -> list[str]:
    return sorted(load_arrangements())


def arrangements_for_family(family: str) -> tuple[Arrangement, ...]:
    """Every arrangement legal in ``family``, id-sorted (deterministic)."""
    get_family(family)                                  # raises on unknown family
    return tuple(
        a for _, a in sorted(load_arrangements().items()) if a.legal_in(family)
    )


def splits_for_arrangements(
    arr_ids: Iterable[str], family: str
) -> frozenset[tuple[int, ...]]:
    """The narrowed candidate whitelist for ``arr_ids`` within ``family``.

    This is the solver-facing half of the bridge: an agent that reasons over
    ``prism_library`` and decides "this slide is a hero+rail" gets its judgment
    turned into the exact span tuples ``candidates.expansions`` may enumerate.
    Arrangements not legal in the family are dropped (never silently widened) —
    an empty result means the caller asked for nothing this family can express,
    and callers must treat that as a refusal, not as "no constraint".
    """
    fam = get_family(family)
    out: set[tuple[int, ...]] = set()
    for aid in arr_ids:
        a = get_arrangement(aid)
        if not a.legal_in(family):
            continue
        out.update(s for s in a.splits if s in fam.split_whitelist)
    return frozenset(out)


def arrangement_ids_for_spans(spans: Iterable[int], family: str) -> tuple[str, ...]:
    """Reverse lookup: which arrangements does this row shape realise in ``family``?

    A shape can realise more than one arrangement (``(8, 4)`` is both hero-rail
    and, in japanese, notan-void) — the tuple is id-sorted and complete rather
    than picking a winner, because which one it "is" depends on content the
    geometry cannot see.
    """
    t = tuple(spans)
    return tuple(a.id for a in arrangements_for_family(family) if t in a.splits)


def label_rows(p: Placement, family: str) -> tuple[tuple[str, ...], ...]:
    """Per-row arrangement labels for a solved placement (telemetry + the DOM
    ``data-arrangement`` hook the geometry linter and eyes-on-glass proof read).
    A row whose shape realises no library arrangement labels as ``()`` — that is
    a real signal (the solver found a legal split no arrangement names), not an
    error."""
    return tuple(arrangement_ids_for_spans(r.spans, family) for r in p.rows)


__all__ = [
    "Arrangement",
    "ArrangementError",
    "load_arrangements",
    "get_arrangement",
    "arrangement_ids",
    "arrangements_for_family",
    "splits_for_arrangements",
    "arrangement_ids_for_spans",
    "label_rows",
]
