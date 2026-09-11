# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Native 12-column composition grid — column geometry, a non-destructive
#          tolerance snap, and named composition archetypes for agency-grade layout.
# index:
#   def column_lines
#   def snap_to_grid
#   def compositions
# AGENT_HEADER_END -->
"""Native composition grid for okuro·slides.

R5 (docs/research/slides-killer/R5-layout-engine.md) found the product authors
bespoke absolute px per slide → inconsistent margins, weak alignment, "generic
slideware". A harmonic column grid is the composition primitive that fixes it.
This is a fresh, native reimplementation of that idea for the scene IR — NO
dependency on okuro.prism.

Two uses:
  1. ``column_lines`` / ``compositions`` feed the authoring prompt real grid
     coordinates + named layouts so the LLM places elements ON the grid and
     VARIES composition across slides (killing the centered-container monotony
     R2 flagged) instead of guessing px.
  2. ``snap_to_grid`` is a NON-DESTRUCTIVE cleanup: it only nudges an element
     edge that is ALREADY within ``tol`` px of a grid line onto that line. It
     never moves intentionally off-grid elements, so it tidies alignment without
     wrecking composition.

Design defaults for a 1280×720 canvas: 12 columns, 80px outer margin, 24px
gutter — scaled proportionally for other canvas widths.
"""

from __future__ import annotations

from typing import Any

_COLS = 12
_MARGIN_FRAC = 0.0625  # 80 / 1280
_GUTTER_FRAC = 0.01875  # 24 / 1280

# Named composition archetypes — the SINGLE source of truth for the names
# ``compositions()`` emits, the author prompt offers, and the binder enforces.
COMPOSITION_NAMES = ("split-7-5", "rail-8-4", "full-bleed", "centered")
# The archetypes the binder's variety-repair rotates content slides through.
# "centered" is intentionally excluded — it is the cover / section-divider tell
# (R2) and is only ever assigned to slide 0.
VARIED_COMPOSITIONS = ("split-7-5", "rail-8-4", "full-bleed")


def grid_metrics(canvas_w: float) -> dict[str, float]:
    """Margin / gutter / column width for a canvas width (proportional)."""
    margin = canvas_w * _MARGIN_FRAC
    gutter = canvas_w * _GUTTER_FRAC
    usable = canvas_w - 2 * margin
    col_w = (usable - (_COLS - 1) * gutter) / _COLS
    return {"margin": margin, "gutter": gutter, "col_w": col_w}


def column_lines(canvas_w: float) -> tuple[list[float], list[float]]:
    """Return (left_edges, right_edges) for all 12 columns, incl. the outer
    margins, in canvas px. Snap candidates for element x and x+w."""
    m = grid_metrics(canvas_w)
    margin, gutter, col_w = m["margin"], m["gutter"], m["col_w"]
    lefts: list[float] = []
    rights: list[float] = []
    for i in range(_COLS):
        left = margin + i * (col_w + gutter)
        lefts.append(round(left, 2))
        rights.append(round(left + col_w, 2))
    # The outer margins are valid alignment lines too.
    lefts.insert(0, round(margin, 2))
    rights.append(round(canvas_w - margin, 2))
    return sorted(set(lefts)), sorted(set(rights))


def span_width(canvas_w: float, cols: int) -> float:
    """Pixel width of a run of ``cols`` columns incl. inner gutters."""
    m = grid_metrics(canvas_w)
    cols = max(1, min(_COLS, cols))
    return cols * m["col_w"] + (cols - 1) * m["gutter"]


def _nearest(value: float, lines: list[float], tol: float) -> float | None:
    best, best_d = None, tol
    for ln in lines:
        d = abs(value - ln)
        if d <= best_d:
            best, best_d = ln, d
    return best


def snap_to_grid(deck: dict[str, Any], tol: float = 24.0) -> int:
    """Nudge top-level element edges that are within ``tol`` of a grid line onto
    it (left edge → column left; right edge → column right, adjusting width).
    Non-destructive: elements not already near the grid are left untouched.
    Returns the number of edges snapped. Frame children (relative coords) are
    intentionally not snapped — the frame box carries the alignment."""
    size = deck.get("size") if isinstance(deck.get("size"), dict) else {}
    w = size.get("w")
    if not isinstance(w, (int, float)) or w <= 0:
        w = 1280.0
    lefts, rights = column_lines(float(w))
    snapped = 0
    for s in deck.get("slides") or []:
        for el in (s.get("elements") if isinstance(s, dict) else None) or []:
            if not isinstance(el, dict):
                continue
            x, ew = el.get("x"), el.get("w")
            if not isinstance(x, (int, float)) or not isinstance(ew, (int, float)):
                continue
            nl = _nearest(float(x), lefts, tol)
            if nl is not None and round(nl) != x:
                right = x + ew
                el["x"] = int(round(nl))
                ew = right - el["x"]  # keep the right edge put while we move left
                el["w"] = int(round(ew))
                snapped += 1
            nr = _nearest(float(el["x"]) + float(el["w"]), rights, tol)
            if nr is not None and round(nr) != round(float(el["x"]) + float(el["w"])):
                el["w"] = max(1, int(round(nr - el["x"])))
                snapped += 1
    return snapped


def compositions(canvas_w: float, canvas_h: float) -> dict[str, dict[str, dict[str, int]]]:
    """Named composition archetypes as slot rectangles (canvas px). These give
    the author concrete, on-grid layouts to choose between so slides VARY
    instead of all being centered containers. Slots are advisory anchors, not
    hard frames."""
    m = grid_metrics(canvas_w)
    margin = m["margin"]
    top = round(canvas_h * 0.14)
    body_h = round(canvas_h * 0.62)
    full_w = round(canvas_w - 2 * margin)

    def rect(x: float, y: float, w: float, h: float) -> dict[str, int]:
        return {"x": int(round(x)), "y": int(round(y)), "w": int(round(w)), "h": int(round(h))}

    left7 = span_width(canvas_w, 7)
    right5_x = margin + span_width(canvas_w, 7) + m["gutter"]
    right5 = span_width(canvas_w, 5)
    rail8 = span_width(canvas_w, 8)
    rail4_x = margin + span_width(canvas_w, 8) + m["gutter"]
    rail4 = span_width(canvas_w, 4)

    return {
        # Editorial 7/5 split — text left, visual right.
        "split-7-5": {
            "body": rect(margin, top, left7, body_h),
            "aside": rect(right5_x, top, right5, body_h),
        },
        # Hero + rail — dominant content, supporting column.
        "rail-8-4": {
            "hero": rect(margin, top, rail8, body_h),
            "rail": rect(rail4_x, top, rail4, body_h),
        },
        # Full-bleed — one idea, edge to edge (image/quote/oversized numeral).
        "full-bleed": {
            "fill": rect(0, 0, canvas_w, canvas_h),
            "caption": rect(margin, round(canvas_h * 0.72), full_w, round(canvas_h * 0.2)),
        },
        # Centered — cover / section divider (use sparingly; it's the default tell).
        "centered": {
            "block": rect(margin, round(canvas_h * 0.34), full_w, round(canvas_h * 0.32)),
        },
    }
