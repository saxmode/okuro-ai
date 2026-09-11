# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism — ONE depth dialect on the read side. L1-L4 is what
#   humans and agents see; the deck2 store's L0-L2+l3 keys stay where they are.
# index: DISPLAY_LEVELS | to_display | from_display | display_map
# AGENT_HEADER_END -->
"""The depth naming, in one place.

Three dialects coexist and the round trip is CORRECT, so this is not a bug — it
is a confusion hazard, which is worse in a different way. Authored ``L1`` is
stored as ``L0`` and displayed as ``L1``, so every intermediate log line, MCP
payload and cache file speaks a language the reader does not.

    author / display   L1   L2   L3   L4
    deck2 storage      L0   L1   L2   l3 (a DocView, not a grid cell)

RATIFIED: L1-L4 is the naming (the L0-L3 dialect is historical). This module is
the read-side translation — everything a HUMAN or an AGENT sees says L1-L4.

NOT ``prism.rungs``, which also calls itself the depth source of truth and is
right to: it maps the LEGACY RUNG NAMES (glance/brief/working/expert) onto
L1-L4 across engine A. This module maps the DECK2 STORAGE KEYS (L0/L1/L2/l3)
onto L1-L4 on the read path. Two halves of one confusion, not duplicates —
reach for rungs when you have a rung name, for this when you have a stored
deck2 level key.

DELIBERATELY NOT a storage rename. Renaming the persisted keys breaks the
``#d=`` deep-link contract, the viewer's VALID_ROWS, per-cell accuracy keys, the
export build and every stored deck, and it needs a hash-alias table so existing
links resolve. That is its own phase with its own migration, not a line item.

ON THE LAYOUTS REGISTRY: layouts.json declares L1/L2/L3 and zero L4 while
components.json has L4 on 6 entries. That is BY DESIGN, not a mismatch to
resolve — L4 is a DocView (continuous prose with a TOC), not a grid slide, so
it has no arrangement to select. Recorded here so the next reader stops
re-opening it.
"""
from __future__ import annotations

from typing import Any, Mapping, TypeVar

#: What a reader sees, shallow -> deep.
DISPLAY_LEVELS = ("L1", "L2", "L3", "L4")

#: deck2 storage key -> display name.
_STORE_TO_DISPLAY = {"L0": "L1", "L1": "L2", "L2": "L3", "L3": "L4", "l3": "L4"}
#: display name -> deck2 storage key.
_DISPLAY_TO_STORE = {"L1": "L0", "L2": "L1", "L3": "L2", "L4": "l3"}

T = TypeVar("T")


def to_display(store_key: str) -> str:
    """A stored level key as the reader's name. Unknown keys pass through —
    a translation layer must never swallow a key it does not recognise."""
    return _STORE_TO_DISPLAY.get(store_key, store_key)


def from_display(display: str) -> str:
    """The reader's name as a stored level key. Unknown values pass through."""
    return _DISPLAY_TO_STORE.get(display, display)


def display_map(by_store_key: Mapping[str, T]) -> dict[str, T]:
    """Re-key a per-level mapping into the display dialect.

    For any payload that leaves the pipeline — an MCP result, a warning, a
    cache summary. The storage keys are correct in the store and wrong
    everywhere else.
    """
    return {to_display(k): v for k, v in by_store_key.items()}


def display_levels(obj: Any) -> Any:
    """Recursively re-key nested ``{topic: {level: ...}}`` payloads."""
    if isinstance(obj, dict):
        return {to_display(k) if k in _STORE_TO_DISPLAY else k: display_levels(v)
                for k, v in obj.items()}
    return obj


__all__ = ["DISPLAY_LEVELS", "display_levels", "display_map",
           "from_display", "to_display"]
