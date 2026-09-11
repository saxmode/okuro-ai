# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Structured server-side edit ops for a slides deck IR (shared by the
#   slides MCP tools + the chat-edit agent). Pure dict manipulation.
# index: def apply_ops | def _apply_one
# AGENT_HEADER_END -->
"""Apply structured edit ops to a deck IR dict.

The canonical edit surface: agents (chat, roles, orchestrator) emit a list of
small ops instead of rewriting the whole deck — safer, less drift, cheaper.
Ops operate on the deck dict in place-ish (returns a new dict). Unknown / invalid
ops are skipped (best-effort) and collected in the returned ``errors``.

Op shapes (``op`` field selects):
  set_meta        {title?, background?, font?, arrangement?, transitionMode?, brandId?}
  add_slide       {after?: int, slide?: {id?, elements?}}            -> appends blank if no slide
  duplicate_slide {index: int}
  remove_slide    {index: int}
  move_slide      {from: int, to: int}
  set_notes       {slide: int, notes: str}
  add_element     {slide: int, element: {...}}
  update_element  {slide: int, id: str, patch: {...}}
  remove_element  {slide: int, id: str}

An Element may be a "frame" auto-layout container: {kind:"frame", x,y,w,h,
children:[Element...], layout:{flow:"none"|"row"|"col", gap, padX, padY,
align:"start"|"center"|"end"}}. add_element/add_slide assign ids recursively
to frame children; update_element's patch can set layout/children to retrofit
a frame.
"""

from __future__ import annotations

import uuid
from typing import Any


def _eid(prefix: str = "el") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _ensure_ids(el: dict) -> None:
    """Give an element (and, recursively, any frame children) a stable id."""
    el.setdefault("id", _eid(el.get("kind", "el")))
    for c in el.get("children") or []:
        if isinstance(c, dict):
            _ensure_ids(c)


def _slug(text: str, taken: set[str], base: str = "slide") -> str:
    s = "".join(c if c.isalnum() else "-" for c in (text or base).lower()).strip("-") or base
    cand, n = s, 1
    while cand in taken:
        n += 1
        cand = f"{s}-{n}"
    return cand


def apply_ops(deck: dict[str, Any], ops: list[dict]) -> tuple[dict[str, Any], list[str]]:
    """Apply ops to a copy of the deck. Returns (new_deck, errors)."""
    import copy

    d = copy.deepcopy(deck)
    d.setdefault("slides", [])
    errors: list[str] = []
    for op in ops or []:
        if not isinstance(op, dict):
            continue
        try:
            _apply_one(d, op)
        except Exception as exc:  # noqa: BLE001 — never let one bad op abort the batch
            errors.append(f"{op.get('op')}: {exc}")
    return d, errors


def _slide_at(d: dict, idx: Any) -> dict | None:
    slides = d["slides"]
    if isinstance(idx, int) and 0 <= idx < len(slides):
        return slides[idx]
    # allow slide id
    for s in slides:
        if s.get("id") == idx:
            return s
    return None


def _apply_one(d: dict, op: dict) -> None:
    kind = op.get("op")
    slides = d["slides"]

    if kind == "set_meta":
        for k in ("title", "background", "font", "brandId"):
            if op.get(k) is not None:
                d[k] = op[k]
        if op.get("arrangement") in ("horizontal", "vertical"):
            d["arrangement"] = op["arrangement"]
        # Transition engine: "morph" (smart-animate) | "push" (whole-slide slide-out).
        if op.get("transitionMode") in ("morph", "push"):
            d.setdefault("transition", {})["mode"] = op["transitionMode"]

    elif kind == "add_slide":
        taken = {s.get("id") for s in slides}
        slide = op.get("slide") if isinstance(op.get("slide"), dict) else {"elements": []}
        slide.setdefault("id", _slug(slide.get("id") or "slide", taken))
        slide.setdefault("elements", [])
        for e in slide["elements"]:
            if isinstance(e, dict):
                _ensure_ids(e)
        after = op.get("after")
        if isinstance(after, int) and 0 <= after < len(slides):
            slides.insert(after + 1, slide)
        else:
            slides.append(slide)

    elif kind == "duplicate_slide":
        i = op.get("index")
        if isinstance(i, int) and 0 <= i < len(slides):
            import copy
            taken = {s.get("id") for s in slides}
            dup = copy.deepcopy(slides[i])
            dup["id"] = _slug(f"{slides[i].get('id', 'slide')}-copy", taken)
            slides.insert(i + 1, dup)  # element ids kept → they morph (smart-animate)

    elif kind == "remove_slide":
        i = op.get("index")
        if isinstance(i, int) and 0 <= i < len(slides) and len(slides) > 1:
            slides.pop(i)

    elif kind == "move_slide":
        a, b = op.get("from"), op.get("to")
        if isinstance(a, int) and isinstance(b, int) and 0 <= a < len(slides) and 0 <= b < len(slides):
            slides.insert(b, slides.pop(a))

    elif kind == "set_notes":
        s = _slide_at(d, op.get("slide"))
        if s is not None:
            s["notes"] = str(op.get("notes") or "")

    elif kind == "add_element":
        s = _slide_at(d, op.get("slide"))
        el = op.get("element")
        if s is not None and isinstance(el, dict):
            _ensure_ids(el)
            s.setdefault("elements", []).append(el)

    elif kind == "update_element":
        s = _slide_at(d, op.get("slide"))
        patch = op.get("patch") if isinstance(op.get("patch"), dict) else {}
        if s is not None:
            for e in s.get("elements", []):
                if e.get("id") == op.get("id"):
                    e.update(patch)
                    # A patch may retrofit a frame (set children/layout); give any
                    # id-less children a stable id, exactly as add_element does —
                    # else they collide on React keys + break morph/entailment refs.
                    if "children" in patch:
                        _ensure_ids(e)
                    break

    elif kind == "remove_element":
        s = _slide_at(d, op.get("slide"))
        if s is not None:
            s["elements"] = [e for e in s.get("elements", []) if e.get("id") != op.get("id")]

    else:
        raise ValueError(f"unknown op '{kind}'")
