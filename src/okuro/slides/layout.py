# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Deterministic fit/bounds guarantee — no slide's content spills off or
#          is cropped by the canvas. A safety net over LLM-authored absolute px.
# index:
#   def scale_element
#   def fit_slide
#   def fit_deck
# AGENT_HEADER_END -->
"""Native slides layout guarantee — the fit + bounds pass.

The deck author places elements as absolute px on a fixed canvas (1280×720).
Nothing guaranteed the result FITS: content taller/wider than the canvas is
silently cropped (push mode) or spills off-slide (morph mode), and elements with
off-canvas coords are accepted verbatim (see docs/research/slides-killer/
R5-layout-engine.md). This pass closes R5's #1 gap.

It is deliberately CONSERVATIVE and non-destructive:
  - it acts ONLY when a slide's content bounding box exceeds the canvas or sits
    partly outside it; a slide that already fits is left byte-for-byte unchanged;
  - it uniformly SCALES a violating slide's geometry (incl. fontSize, recursing
    into frame children) down to a legibility floor, then re-centers it — it
    never reflows or reorders, so intentional composition (text over an image,
    overlapping cards) is preserved.

Scope boundary (honest): this works on DECLARED element boxes, not on
render-measured text height — a text box the LLM under-sized whose text wraps
past its declared height is a runtime overflow only the client can measure
(the frontend fit pass, a follow-up). Collision/separation of overlapping
siblings is intentionally NOT done here (z-order + design intent make blind
separation harmful). This pass is the guaranteed no-spill floor.
"""

from __future__ import annotations

from typing import Any

# Smallest scale we will shrink a slide to. Below this, text stops being legible
# and scaling is the wrong fix (the content genuinely needs splitting) — we clamp
# at the floor and let the follow-up paginate. 0.55 ≈ an 84px title → ~46px.
_MIN_SCALE = 0.55

# Geometry fields that scale with the slide. `x`/`y` handled by the transform;
# these are intrinsic sizes that must shrink too so proportions hold.
_SCALABLE = ("w", "h", "fontSize", "radius", "pad")
_LAYOUT_SCALABLE = ("gap", "padX", "padY")


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def scale_element(el: dict[str, Any], s: float) -> None:
    """Scale an element's intrinsic sizes by ``s`` in place (recurses frames).

    Position (x/y) is NOT touched here — the slide-level transform owns absolute
    placement; children of a frame use relative coords so their x/y DO scale."""
    if not isinstance(el, dict):
        return
    for k in _SCALABLE:
        v = _num(el.get(k))
        if v is not None:
            el[k] = type(el[k])(v * s) if isinstance(el[k], int) else v * s
    lay = el.get("layout")
    if isinstance(lay, dict):
        for k in _LAYOUT_SCALABLE:
            v = _num(lay.get(k))
            if v is not None:
                lay[k] = int(round(v * s))
    for child in el.get("children") or []:
        # child x/y are relative offsets inside the frame → they scale too.
        for k in ("x", "y"):
            v = _num(child.get(k))
            if v is not None:
                child[k] = type(child[k])(v * s) if isinstance(child[k], int) else v * s
        scale_element(child, s)


def _union(elements: list[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    xs0, ys0, xs1, ys1 = [], [], [], []
    for e in elements:
        if not isinstance(e, dict):
            continue
        x, y = _num(e.get("x")), _num(e.get("y"))
        w, h = _num(e.get("w")), _num(e.get("h"))
        if None in (x, y, w, h):
            continue
        xs0.append(x); ys0.append(y); xs1.append(x + w); ys1.append(y + h)
    if not xs0:
        return None
    return min(xs0), min(ys0), max(xs1), max(ys1)


def fit_slide(elements: list[dict[str, Any]], canvas_w: float, canvas_h: float) -> bool:
    """If the slide's content exceeds or sits outside the canvas, scale it down
    (to a floor) and re-center so nothing spills or crops. Mutates in place.
    Returns True if anything changed. No-op for a slide that already fits."""
    box = _union(elements)
    if box is None:
        return False
    minx, miny, maxx, maxy = box
    content_w, content_h = maxx - minx, maxy - miny
    if content_w <= 0 or content_h <= 0:
        return False

    overflowing = content_w > canvas_w or content_h > canvas_h
    out_of_bounds = minx < 0 or miny < 0 or maxx > canvas_w or maxy > canvas_h
    if not overflowing and not out_of_bounds:
        return False  # already fits — leave untouched

    scale = 1.0
    if overflowing:
        scale = max(_MIN_SCALE, min(canvas_w / content_w, canvas_h / content_h))

    new_w, new_h = content_w * scale, content_h * scale
    # Center when we had to shrink; otherwise just shift the block inside bounds.
    if scale < 1.0:
        tx = (canvas_w - new_w) / 2.0
        ty = (canvas_h - new_h) / 2.0
    else:
        tx = min(max(minx, 0.0), max(0.0, canvas_w - new_w))
        ty = min(max(miny, 0.0), max(0.0, canvas_h - new_h))

    for e in elements:
        if not isinstance(e, dict):
            continue
        x, y = _num(e.get("x")), _num(e.get("y"))
        if x is None or y is None:
            continue
        e["x"] = int(round((x - minx) * scale + tx))
        e["y"] = int(round((y - miny) * scale + ty))
        if scale < 1.0:
            scale_element(e, scale)
    return True


def fit_deck(deck: dict[str, Any]) -> int:
    """Run the fit/bounds pass over every slide. Returns the count of slides
    that were adjusted. Reads the deck's own canvas size (falls back to
    1280×720 when absent/malformed)."""
    size = deck.get("size") if isinstance(deck.get("size"), dict) else {}
    w = _num(size.get("w")) or 1280.0
    h = _num(size.get("h")) or 720.0
    changed = 0
    for s in deck.get("slides") or []:
        if isinstance(s, dict) and fit_slide(s.get("elements") or [], w, h):
            changed += 1
    return changed
