# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: prism workflow — tell the AUTHOR what the component kit can actually
#   carry at each rung, computed from the library rather than restated.
# index: shape_budget | budget_prose | unrenderable
# AGENT_HEADER_END -->
"""What the kit can carry, per shape, per rung.

The author writes blocks with a shape and an item count, and has never known
what the component library can render. Measured on a real deck, that produces
blocks nothing can carry:

    comparison @ L1              0 components
    relationship @ L1            0
    set @ L1 with 6+ items       0 (persona-row/story-grid cap at 4)
    sequence @ L1 with 10 items  0 (phase-row caps at 7)

The quota layer reports those as KIT GAPS, correctly — the agent choosing the
component had no legal option. But the defect is upstream of the choice: a
block was written that the rung cannot express. Fixing the choice is
impossible; fixing the WRITING is not.

So the budget is computed here from ``components.json`` and injected into the
author prompt. Computed, never restated: a hand-written copy of the library's
constraints is wrong the day the library changes, and this file's entire reason
for existing is that a constraint nobody propagated caused the defect.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

_LIB = Path(__file__).resolve().parents[1] / "library" / "components.json"

#: Rungs the author writes slides for. L4 is a DocView with no component grid.
AUTHOR_LEVELS = ("L1", "L2", "L3")


@lru_cache(maxsize=1)
def _entries() -> tuple[dict, ...]:
    return tuple(json.loads(_LIB.read_text())["entries"])


def shape_budget(level: str, shape: str) -> Optional[tuple[int, int]]:
    """``(min_items, max_items)`` this rung can render for this shape.

    ``None`` when the kit has NO component for the pair — the author must not
    write that shape at that rung at all.
    """
    lo: Optional[int] = None
    hi: Optional[int] = None
    for c in _entries():
        if shape not in (c.get("carries") or []):
            continue
        if level not in (c.get("info_depths") or []):
            continue
        cap = c.get("capacity") or {}
        cmin, cmax = cap.get("min"), cap.get("max")
        if cmin is None or cmax is None:
            continue
        lo = cmin if lo is None else min(lo, cmin)
        hi = cmax if hi is None else max(hi, cmax)
    return None if lo is None else (lo, hi or lo)


def unrenderable(level: str, shape: str, items: int) -> bool:
    """True when nothing in the kit can carry this block as written."""
    b = shape_budget(level, shape)
    return b is None or not (b[0] <= max(1, items) <= b[1])


def budget_prose(shapes: tuple[str, ...]) -> str:
    """The budget as prompt text — per rung, what each shape may carry.

    Deliberately phrased as what the AUTHOR may write, not as component names:
    component choice is the agent's job downstream, and naming components here
    would pull that decision into the writing stage.
    """
    lines: list[str] = []
    for level in AUTHOR_LEVELS:
        allowed: list[str] = []
        banned: list[str] = []
        for shape in shapes:
            b = shape_budget(level, shape)
            if b is None:
                banned.append(shape)
            else:
                lo, hi = b
                allowed.append(f"{shape} {lo}-{hi}" if lo != hi else f"{shape} {lo}")
        line = f"- {level}: " + ", ".join(allowed)
        if banned:
            line += (f". NOT AVAILABLE at {level}: {', '.join(banned)} — "
                     f"express that material at a deeper rung, or reframe it "
                     f"into a shape this rung has.")
        lines.append(line)
    return "\n".join(lines)


__all__ = ["AUTHOR_LEVELS", "budget_prose", "shape_budget", "unrenderable"]
