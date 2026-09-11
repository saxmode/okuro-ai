# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Deterministic template binder — GUARANTEE composition, not just guide
#          it. Places each slide's named archetype + element slots onto grid
#          rects, and repairs monotone / all-centered / unassigned author output
#          into varied, on-grid layouts.
# index:
#   def bind_slide
#   def bind_deck
# AGENT_HEADER_END -->
"""Native template binder for okuro·slides.

The grid slice (``grid.py`` + the author prompt) *guides* the LLM to pick a
composition and place on the grid; nothing *guaranteed* it (R2's centered-
container monotony, R5's bespoke-px inconsistency). This pass closes that: it is
the deterministic step that makes composition a code invariant rather than a
prompt suggestion.

For every slide it:
  1. resolves a composition archetype — honouring the author's ``composition``
     when the deck already varies, else assigning one by rotation so content
     slides are guaranteed to differ (cover stays ``centered``);
  2. bins the slide's top-level elements into that archetype's slots — honouring
     the author's ``slot`` tags when the composition is kept, else distributing
     by a deterministic heuristic (text/frames → the dominant slot, visuals →
     the supporting slot);
  3. places each slot's elements onto the slot's grid rect (from
     ``grid.compositions``): a lone element fills the slot; multiple stack
     vertically, scaled to stay inside the slot.

A slot with ONE element keeps that element top-level (its id is preserved, so
smart-animate morphs survive). A slot with SEVERAL elements wraps them into a
single auto-layout ``col`` frame placed on the slot rect: the frame's flow +
hug (resolved by the frontend ``flatten`` at render, which measures wrapped text
and recurses nested frames) owns the vertical stacking, so slot-mates can NEVER
overlap — regardless of how tall their content wraps. Wrapping keeps each child's
id, so a morph authored across slides still works (the child ids are stable; only
the transparent wrapper is per-slide). Frame children (relative coords) are never
rewritten.

This replaces the old declared-height cursor stack, whose y-positions assumed the
authored ``h`` was accurate — but text/frames hug TALLER at render, so stacked
slot-mates overlapped (nothing downstream re-stacks: the fit pass only scales).
Delegating the stack to the hug-aware ``col`` frame is the systematic fix.
"""

from __future__ import annotations

from typing import Any

from okuro.slides.grid import COMPOSITION_NAMES, VARIED_COMPOSITIONS, compositions

# Elements that read as "the visual" and belong in the supporting slot when the
# author gave no slot tags. Everything else (text, frame) is treated as content.
_VISUAL_KINDS = ("image", "box", "video", "flow")
_STACK_GAP = 24  # px between vertically stacked slot-mates


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _slot_order(slots: dict[str, dict[str, int]]) -> list[str]:
    """Slot names in reading order (top-to-bottom, then left-to-right)."""
    return sorted(slots, key=lambda k: (slots[k]["y"], slots[k]["x"]))


def _primary_slot(slots: dict[str, dict[str, int]]) -> str:
    """The dominant slot = the largest-area rect (the content anchor)."""
    return max(slots, key=lambda k: slots[k]["w"] * slots[k]["h"])


def _rotate(index: int) -> str:
    """Deterministic archetype for slide ``index`` in repair mode: slide 0 is the
    cover (``centered``); content slides rotate through ``VARIED_COMPOSITIONS``
    so no two adjacent slides share a composition."""
    if index <= 0:
        return "centered"
    return VARIED_COMPOSITIONS[(index - 1) % len(VARIED_COMPOSITIONS)]


def _needs_variety(declared: list[str | None]) -> bool:
    """True when the author's content slides are monotone (≤1 distinct
    composition) — the exact all-centered / same-layout tell we must repair."""
    content = declared[1:] if len(declared) > 1 else declared
    distinct = {c for c in content if c}
    return len(distinct) <= 1


def _distribute(
    els: list[dict[str, Any]],
    comp: str,
    slots: dict[str, dict[str, int]],
    order: list[str],
    primary: str,
) -> dict[str, list[dict[str, Any]]]:
    """Bin unassigned elements into slots by a deterministic heuristic."""
    groups: dict[str, list[dict[str, Any]]] = {k: [] for k in slots}
    if len(order) == 1:
        groups[primary] = list(els)
        return groups
    if comp == "full-bleed":
        fill, cap = order[0], order[1]
        visuals = [e for e in els if e.get("kind") in _VISUAL_KINDS]
        if visuals:
            groups[fill] = [visuals[0]]
            groups[cap] = [e for e in els if e is not visuals[0]]
        else:
            groups[fill] = els[:1]
            groups[cap] = list(els[1:])
        return groups
    # Two-slot split / rail: content on the dominant slot, visuals on the aside.
    prim, sec = order[0], order[1]
    for e in els:
        (groups[sec] if e.get("kind") in _VISUAL_KINDS else groups[prim]).append(e)
    if not groups[prim]:  # all-visual slide → make the visuals the dominant block
        groups[prim], groups[sec] = groups[sec], []
    return groups


def _place_group(els: list[dict[str, Any]], rect: dict[str, int]) -> None:
    """Place a slot's elements onto its rect in place. One element fills the
    slot; multiple stack vertically, scaled to never exceed the slot height."""
    if not els:
        return
    rx, ry, rw, rh = rect["x"], rect["y"], rect["w"], rect["h"]
    if len(els) == 1:
        e = els[0]
        e["x"], e["y"], e["w"] = rx, ry, rw
        eh = _num(e.get("h"))
        e["h"] = int(round(min(eh, rh))) if eh is not None else int(round(rh))
        return
    n = len(els)
    heights = [max(1.0, _num(e.get("h")) or (rh / n)) for e in els]
    total = sum(heights) + _STACK_GAP * (n - 1)
    scale = min(1.0, rh / total) if total > 0 else 1.0
    y = float(ry)
    for e, hh in zip(els, heights):
        eh = hh * scale
        e["x"], e["w"] = rx, rw
        e["y"] = int(round(y))
        e["h"] = max(1, int(round(eh)))
        y += eh + _STACK_GAP * scale


def _stack_frame(
    members: list[dict[str, Any]], rect: dict[str, int], frame_id: str
) -> dict[str, Any]:
    """Wrap several slot-mates into ONE auto-layout ``col`` frame on the slot rect.

    Children become relative (x/y = 0; col flow assigns y at render) and take the
    slot width so their text wraps against it. Their ids are kept (morph-safe).
    The frame hugs + reflows at render, so the members can never overlap however
    tall they wrap — the fix for the declared-vs-measured-height stack bug."""
    z = max((int(_num(m.get("z")) or 0) for m in members), default=0)
    children: list[dict[str, Any]] = []
    for m in members:
        c = dict(m)
        c["x"], c["y"], c["w"] = 0, 0, rect["w"]
        c.pop("slot", None)  # slot is a top-level concept; the wrapper owns it
        children.append(c)
    return {
        "id": frame_id,
        "kind": "frame",
        "x": rect["x"], "y": rect["y"], "w": rect["w"], "h": rect["h"],
        "z": z, "bg": "transparent", "radius": 0,
        "layout": {"flow": "col", "gap": _STACK_GAP, "padX": 0, "padY": 0, "align": "start"},
        "children": children,
    }


def bind_slide(
    slide: dict[str, Any], comp: str, canvas_w: float, canvas_h: float, *, honor: bool
) -> bool:
    """Bind one slide onto composition ``comp``. Sets ``slide["composition"]``,
    tags each placed element (or wrapper frame) with its ``slot``, and places
    slot contents onto the grid rects. A lone slot element stays top-level (id
    preserved); multiple slot-mates are wrapped into a hugging ``col`` frame so
    they never overlap. Rebuilds ``slide["elements"]`` in slot reading order.
    Returns True if it bound anything."""
    els = [e for e in (slide.get("elements") or []) if isinstance(e, dict)]
    slots = compositions(canvas_w, canvas_h).get(comp)
    if not slots or not els:
        return False
    order = _slot_order(slots)
    primary = _primary_slot(slots)

    if honor:
        groups: dict[str, list[dict[str, Any]]] = {k: [] for k in slots}
        for e in els:
            name = e.get("slot") if e.get("slot") in slots else primary
            groups[name].append(e)
    else:
        groups = _distribute(els, comp, slots, order, primary)

    sid = str(slide.get("id") or "s")
    placed: list[dict[str, Any]] = []
    for name in slots:
        rect = slots[name]
        members = groups.get(name) or []
        if not members:
            continue
        if len(members) == 1:
            _place_group(members, rect)
            members[0]["slot"] = name
            placed.append(members[0])
        else:
            frame = _stack_frame(members, rect, f"{sid}__{name}__stack")
            frame["slot"] = name
            placed.append(frame)
    slide["elements"] = placed
    slide["composition"] = comp
    return True


def bind_deck(deck: dict[str, Any]) -> int:
    """Enforce composition across the whole deck. Honours a deck that already
    varies (keeping author composition + slots); repairs a monotone / all-
    centered deck by rotating varied archetypes and re-binning elements. Reads
    the deck's own canvas size. Returns the count of slides bound."""
    size = deck.get("size") if isinstance(deck.get("size"), dict) else {}
    w = _num(size.get("w")) or 1280.0
    h = _num(size.get("h")) or 720.0

    slides = [s for s in (deck.get("slides") or []) if isinstance(s, dict)]
    declared: list[str | None] = [
        s.get("composition") if s.get("composition") in COMPOSITION_NAMES else None
        for s in slides
    ]
    # Repair monotony ONLY when the author left it implicit — with ONE exception.
    # A deck whose content slides ALL carry an explicit, NON-centered composition
    # is a deliberate choice (e.g. a data deck that is split-7-5 throughout) →
    # honour it. But an all-`centered` content run is the exact tell the repair
    # exists to fix (centered is cover/section-only), so it is always repaired.
    content = declared[1:] if len(declared) > 1 else declared
    distinct = {c for c in content if c}
    all_explicit = bool(content) and all(c is not None for c in content)
    deliberate = all_explicit and bool(distinct) and distinct != {"centered"}
    repair_all = _needs_variety(declared) and not deliberate

    changed = 0
    for i, s in enumerate(slides):
        comp = declared[i]
        if repair_all or comp is None:
            comp, honor = _rotate(i), False
        else:
            honor = True
        if bind_slide(s, comp, w, h, honor=honor):
            changed += 1
    return changed
