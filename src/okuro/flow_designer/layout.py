# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-flow server-side auto-layout — estimate a node's RENDERED size
#   from its content, then place programmatically-authored nodes so no two
#   nodes ever overlap. Shared by every non-interactive entry point (MCP save,
#   streaming draw worker) so agents never have to invent coordinates.
# index: size constants | ports | estimate_size | apply_layout | StreamLayout
# AGENT_HEADER_END -->
"""Width-aware auto-layout for okuro-flow graphs.

An agent authoring a flow (``flow_designer_save``, or the live draw stream) has
no renderer, so it cannot know how wide a node will actually be. The old
guidance was prose — *"x grows ~260/column, y ~160/row"* — and a node whose
title is longer than ~28 characters blows straight through it, so the user got
overlapping cards to untangle by hand.

This module removes the guess:

* :func:`estimate_size` derives a node's rendered width/height from its content
  (chip, title, sub, port labels, node category) using constants read off the
  real frontend CSS — each one carries its source line in a comment below. The
  estimate is deliberately **generous**: over-estimating costs whitespace,
  under-estimating costs an overlap, which is the bug being fixed.
* :func:`apply_layout` runs a deterministic layered left-to-right layout —
  topological depth from the edges gives the column, estimated sizes give the
  column widths and the row heights.
* :class:`StreamLayout` is the same estimator behind a running-column allocator,
  for the draw pipeline where nodes arrive one at a time and the edge that would
  reveal a node's depth has not been emitted yet.

Stdlib only, no wall-clock, no randomness: the same input always produces the
same coordinates.

**Coordinates are TOP-LEFT.** ``node.position`` in @xyflow/react v12 is the top
-left of the node wrapper, not its centre.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Glyph metrics — the ONE part that is not readable off the CSS.
#
# A server has no font metrics, so text width is estimated as
# ``len(text) * font_size * ratio``. Ratios are chosen on the generous side:
# JetBrains Mono (var(--fd-mono) -> var(--font-mono)) has an exact 0.6em
# advance, and the UI sans averages ~0.5-0.58em at weight 400-700, so 0.62
# leaves headroom for wide glyphs without doubling a node's width.
# ---------------------------------------------------------------------------
CHAR_W_MONO = 0.62  # heuristic: JetBrains Mono advance 0.6em + slack
CHAR_W_SANS = 0.62  # heuristic: generous average for the UI sans

# Longest single line the body text is assumed to hold before wrapping. The
# card itself has no max-width in /flow (node-view.tsx NODE_VIEW_FLOW.maxW =
# null), so this only decides how many LINES a long sub costs, never the width.
SUB_WRAP_W = 320.0

# ---------------------------------------------------------------------------
# Card geometry — every constant below is read off the live frontend.
# CSS  = src/okuro/web/frontend/src/components/flow-designer/flow-designer.css
# TSX  = .../flow-designer/nodes.tsx · VIEW = .../flow-designer/node-view.tsx
# ---------------------------------------------------------------------------
BORDER = 1.0  # CSS .fd-nd:741 `border: 1px` — counted on both sides

# -- standard card (.fd-nd + .fd-ndinner), the default port mode --------------
STD_MIN_W = 230.0  # CSS .fd-nd:735 min-width · VIEW NODE_VIEW_FLOW.minW:104
STD_MIN_H = 96.0  # CSS .fd-nd:736 min-height · VIEW NODE_VIEW_FLOW.minH:105
STD_PAD_X = 24.0  # CSS .fd-ndinner:908 `padding: 12px 24px` (per side)
STD_PAD_Y = 12.0  # CSS .fd-ndinner:908 (per side)
STD_GAP = 3.0  # CSS .fd-ndinner:902 `gap: 3px`
CHIP_FS = 10.0  # CSS .fd-ndtag:913 font-size
CHIP_PAD_X = 6.0  # CSS .fd-ndtag:915 `padding: 1px 6px` (per side)
CHIP_PAD_Y = 1.0  # CSS .fd-ndtag:915 (per side)
CHIP_LH = 1.4  # heuristic: inline-flex chip, no explicit line-height
TITLE_FS = 13.0  # CSS .fd-ndttl:926 font-size (weight 700)
TITLE_LH = 1.2  # CSS .fd-ndttl:928 line-height
SUB_FS = 10.5  # CSS .fd-ndsub:948 font-size
SUB_LH = 1.3  # CSS .fd-ndsub:949 line-height

# -- sided port labels (.fd-plab), the absolutely-positioned variant ----------
PLAB_FS = 9.5  # CSS .fd-plab:1113 font-size
PLAB_MAX_W = 74.0  # CSS .fd-plab:1117 max-width
PLAB_INSET = 14.0  # TSX renderPorts:651,655 left/right offset
PLAB_BAND_Y = 11.0  # TSX renderPorts:660,665 top/bottom offset
PORT_ROW_H = 20.0  # CSS .fd-prow:880 min-height — one port's vertical slot

# -- lr card (.fd-nd.lr), the "classic" ins-left / outs-right port mode -------
LR_MIN_W = 244.0  # CSS .fd-nd.lr:820 min-width
LR_HD_PAD_X = 11.0  # CSS .fd-nd.lr .fd-hd:826 `padding: 7px 11px`
LR_HD_PAD_Y = 7.0  # CSS .fd-nd.lr .fd-hd:826
LR_HD_GAP = 8.0  # CSS .fd-nd.lr .fd-hd:830 `gap: 8px`
LR_TAG_FS = 10.0  # CSS .fd-hd .fd-tag:836 font-size
LR_TAG_PAD_X = 6.0  # CSS .fd-hd .fd-tag:839 `padding: 1px 6px`
LR_TTL_FS = 12.5  # CSS .fd-ttl-hd:844 font-size
LR_TTL_LH = 1.2  # CSS .fd-ttl-hd:845 line-height
LR_SUB_PAD_X = 11.0  # CSS .fd-nsub:857 `padding: 5px 11px 2px`
LR_SUB_PAD_T = 5.0  # CSS .fd-nsub:857
LR_SUB_PAD_B = 2.0  # CSS .fd-nsub:857
LR_SUB_FS = 10.5  # CSS .fd-nsub:859 font-size
LR_SUB_LH = 1.35  # CSS .fd-nsub:860 line-height
LR_BODY_PAD_T = 6.0  # CSS .fd-nd.lr .fd-body:866 `padding: 6px 0 10px`
LR_BODY_PAD_B = 10.0  # CSS .fd-nd.lr .fd-body:866
LR_BODY_GAP = 12.0  # CSS .fd-nd.lr .fd-body:865 `gap: 12px`
LR_PROW_FS = 10.5  # CSS .fd-prow:881 font-size
LR_PROW_INSET = 16.0  # CSS .fd-col.in/.out .fd-prow:885,888 padding

# -- title node (.fd-title): standalone heading, no chrome, no ports ----------
TTL_PAD_X = 8.0  # CSS .fd-title:1017 `padding: 4px 8px`
TTL_PAD_Y = 4.0  # CSS .fd-title:1017
TTL_MAX_W = 520.0  # CSS .fd-title:1018 max-width
TTL_TXT_FS = 30.0  # CSS .fd-title .fd-title-txt:1029 font-size (weight 800)
TTL_TXT_LH = 1.1  # CSS .fd-title .fd-title-txt:1031 line-height
TTL_SUB_FS = 14.0  # CSS .fd-title-sub:1047 font-size
TTL_SUB_MT = 2.0  # CSS .fd-title-sub:1046 margin-top
TTL_SUB_LH = 1.35  # heuristic: no explicit line-height on .fd-title-sub

# -- mdnote (.fd-mdnote): markdown prose card, no ports ----------------------
MD_MIN_W = 240.0  # CSS .fd-mdnote:1055 min-width · TSX Resizer minW:853
MD_MAX_W = 560.0  # CSS .fd-mdnote:1058 max-width
MD_MIN_H = 120.0  # TSX Resizer minH:853
MD_HD_PAD_X = 10.0  # CSS .fd-mdnote-hd:1076 `padding: 6px 10px`
MD_HD_PAD_Y = 6.0  # CSS .fd-mdnote-hd:1076
MD_HD_GAP = 8.0  # CSS .fd-mdnote-hd:1075 `gap: 8px`
MD_BODY_PAD_X = 12.0  # CSS .fd-mdnote-body:1081 `padding: 8px 12px 10px`
MD_BODY_PAD_T = 8.0  # CSS .fd-mdnote-body:1081
MD_BODY_PAD_B = 10.0  # CSS .fd-mdnote-body:1081
MD_BODY_FS = 12.0  # CSS .fd-mdnote-body:1082 font-size
MD_BODY_LH = 1.5  # heuristic: shared markdown rhythm, compact variant

# -- group node --------------------------------------------------------------
GRP_MIN_W = 230.0  # TSX GroupNode Resizer:989 minW
GRP_MIN_H = 96.0  # TSX GroupNode Resizer:989 minH
GRPBOX_MIN_W = 320.0  # TSX GroupNode expanded Resizer:946 minW
GRPBOX_MIN_H = 190.0  # TSX GroupNode expanded Resizer:946 minH

# ---------------------------------------------------------------------------
# Layout spacing — mirrors the frontend's own auto-layout so a server-laid-out
# graph and one the user re-layouts in the UI space alike.
# ---------------------------------------------------------------------------
COL_GAP = 96.0  # graph.ts:225 dagre `ranksep: 96`
ROW_GAP = 46.0  # graph.ts:225 dagre `nodesep: 46`
GRID_ROWS = 4  # disconnected nodes per trailing column
STREAM_COL_H = 900.0  # running-column height budget for the draw stream
_MAX_PROBE = 512  # allocation attempts before giving up on a free slot
_OVERLAP_EPS = 1.0  # px of tolerance before two boxes count as overlapping

# How much two AUTHOR-SUPPLIED boxes must overlap before the layout decides
# those positions were never really a layout and re-places them. The estimator
# runs generous on purpose, and a user's deliberately tight canvas arriving
# back through an agent save must not be rearranged over a few pixels of
# estimation slack — while the failure this module exists to fix (nodes stacked
# on the origin, or 260px apart under a 500px title) overlaps by hundreds.
RELAYOUT_TOLERANCE = 24.0


# ---------------------------------------------------------------------------
# ports — the canonical single list and the legacy ins/outs pair
# ---------------------------------------------------------------------------
def iter_ports(data: dict) -> list[dict]:
    """Normalise either port shape into one list of ``{label, dir, side}``.

    Canonical (what the frontend builder is unifying on)::

        data.ports = [{id, label?, t?, dir: "in"|"out", side?}]

    Legacy (still accepted, still written by the draw worker)::

        data.ins = [Port…]   # dir "in"
        data.outs = [Port…]  # dir "out"

    ``data.ports`` wins when it is a non-empty list; otherwise ins/outs are
    read. Side defaults follow the renderer (TSX renderPorts:623): an input
    lands left, an output right — or top/bottom when the node is vertical
    (``data.orient == "v"``).
    """
    vertical = str(data.get("orient") or "h") == "v"
    raw: list[tuple[dict, str]] = []
    ports = data.get("ports")
    if isinstance(ports, list) and ports:
        for p in ports:
            if not isinstance(p, dict):
                continue
            d = "out" if str(p.get("dir") or "in") == "out" else "in"
            raw.append((p, d))
    else:
        for key, d in (("ins", "in"), ("outs", "out")):
            seq = data.get(key)
            if isinstance(seq, list):
                raw.extend((p, d) for p in seq if isinstance(p, dict))

    out: list[dict] = []
    for p, d in raw:
        side = p.get("side")
        if side not in ("left", "right", "top", "bottom"):
            if vertical:
                side = "top" if d == "in" else "bottom"
            else:
                side = "left" if d == "in" else "right"
        out.append({"label": str(p.get("label") or ""), "dir": d, "side": side})
    return out


# ---------------------------------------------------------------------------
# size estimation
# ---------------------------------------------------------------------------
def _text_w(text: Any, font_px: float, ratio: float = CHAR_W_SANS) -> float:
    if not text:
        return 0.0
    return len(str(text)) * font_px * ratio


def _lines(text: Any, font_px: float, box_w: float, ratio: float = CHAR_W_SANS) -> int:
    """How many wrapped lines ``text`` needs inside ``box_w`` — explicit
    newlines respected, minimum one line."""
    if not text:
        return 0
    if box_w <= 0:
        return 1
    total = 0
    for raw in str(text).split("\n"):
        w = _text_w(raw, font_px, ratio)
        total += max(1, math.ceil(w / box_w))
    return total


def _num(v: Any) -> float | None:
    """A finite number, or None. ``bool`` is not a coordinate."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _explicit(node: dict, key: str) -> float | None:
    """An author-set dimension, in the renderer's own precedence order
    (graph.ts mw/mh:103-104): measured -> node -> style."""
    measured = node.get("measured")
    if isinstance(measured, dict):
        v = _num(measured.get(key))
        if v and v > 0:
            return v
    v = _num(node.get(key))
    if v and v > 0:
        return v
    style = node.get("style")
    if isinstance(style, dict):
        v = _num(style.get(key))
        if v and v > 0:
            return v
    return None


def _side_label_w(ports: list[dict], side: str) -> float:
    """Widest label on one side, capped at the CSS max-width."""
    w = 0.0
    for p in ports:
        if p["side"] == side and p["label"]:
            w = max(w, min(PLAB_MAX_W, _text_w(p["label"], PLAB_FS)))
    return w


def _std_size(data: dict, ports: list[dict]) -> tuple[float, float]:
    """Standard card: chip / title / sub stacked in .fd-ndinner, ports on the
    four sides as absolutely-positioned handles + labels."""
    chip_w = 0.0
    chip_h = 0.0
    if not data.get("hideChip") and data.get("tag"):
        chip_w = _text_w(data.get("tag"), CHIP_FS, CHAR_W_MONO) + 2 * CHIP_PAD_X
        chip_h = CHIP_FS * CHIP_LH + 2 * CHIP_PAD_Y
    title_w = 0.0
    title_h = 0.0
    if not data.get("hideTitle"):
        # .fd-ndttl is `white-space: nowrap` (CSS:934), so a long title widens
        # the shrink-to-fit card instead of wrapping — width grows with it.
        title_w = _text_w(data.get("title") or "—", TITLE_FS)
        title_h = TITLE_FS * TITLE_LH
    sub = "" if data.get("hideSub") else (data.get("sub") or "")
    sub_w = min(_text_w(sub, SUB_FS), SUB_WRAP_W)

    content_w = max(chip_w, title_w, sub_w)
    sub_h = 0.0
    if sub:
        sub_h = _lines(sub, SUB_FS, max(content_w, 1.0)) * SUB_FS * SUB_LH

    parts = [h for h in (chip_h, title_h, sub_h) if h > 0]
    content_h = sum(parts) + STD_GAP * max(0, len(parts) - 1)

    w = content_w + 2 * STD_PAD_X + 2 * BORDER
    h = content_h + 2 * STD_PAD_Y + 2 * BORDER

    # Side labels sit INSIDE the card (absolute, inset 14px). They do not push
    # the card wider in the browser, but a card too narrow to hold them reads
    # as overlapping text — so treat them as a width floor.
    lab_w = _side_label_w(ports, "left") + _side_label_w(ports, "right")
    if lab_w:
        w = max(w, lab_w + 2 * PLAB_INSET + STD_GAP * 2)
    band = max(_side_label_w(ports, "top"), _side_label_w(ports, "bottom"))
    if band:
        h += 2 * (PLAB_BAND_Y + PLAB_FS)

    # Vertical room for the handles: they are spread at (i+1)/(n+1) of the
    # side, so n ports need n+1 slots to stay apart.
    for side in ("left", "right"):
        n = sum(1 for p in ports if p["side"] == side)
        if n:
            h = max(h, (n + 1) * PORT_ROW_H)
    for side in ("top", "bottom"):
        n = sum(1 for p in ports if p["side"] == side)
        if n:
            w = max(w, (n + 1) * (PORT_ROW_H + PLAB_MAX_W / 2))

    return max(w, STD_MIN_W), max(h, STD_MIN_H)


def _lr_size(data: dict, ports: list[dict]) -> tuple[float, float]:
    """`lr` port mode: coloured header, sub line, then an ins column and an
    outs column side by side (TSX renderLR:679)."""
    hd_w = 2 * LR_HD_PAD_X
    hd_inner = 0.0
    if not data.get("hideChip") and data.get("tag"):
        hd_inner += _text_w(data.get("tag"), LR_TAG_FS, CHAR_W_MONO) + 2 * LR_TAG_PAD_X + LR_HD_GAP
    if not data.get("hideTitle"):
        hd_inner += _text_w(data.get("title") or "—", LR_TTL_FS)
    hd_w += hd_inner
    hd_h = 2 * LR_HD_PAD_Y + max(CHIP_FS * CHIP_LH + 2 * CHIP_PAD_Y, LR_TTL_FS * LR_TTL_LH)

    sub = "" if data.get("hideSub") else (data.get("sub") or "")
    sub_w = 0.0
    sub_h = 0.0
    if sub:
        sub_w = min(_text_w(sub, LR_SUB_FS), SUB_WRAP_W) + 2 * LR_SUB_PAD_X
        sub_h = (
            _lines(sub, LR_SUB_FS, SUB_WRAP_W) * LR_SUB_FS * LR_SUB_LH
            + LR_SUB_PAD_T
            + LR_SUB_PAD_B
        )

    ins = [p for p in ports if p["dir"] == "in"]
    outs = [p for p in ports if p["dir"] == "out"]
    in_w = max((_text_w(p["label"], LR_PROW_FS) for p in ins), default=0.0)
    out_w = max((_text_w(p["label"], LR_PROW_FS) for p in outs), default=0.0)
    body_w = 0.0
    body_h = 0.0
    rows = max(len(ins), len(outs))
    if rows:
        body_w = 2 * LR_PROW_INSET + in_w + out_w + LR_BODY_GAP
        body_h = rows * PORT_ROW_H + LR_BODY_PAD_T + LR_BODY_PAD_B

    w = max(LR_MIN_W, hd_w, sub_w, body_w) + 2 * BORDER
    h = max(STD_MIN_H, hd_h + sub_h + body_h + 2 * BORDER)
    return w, h


def _title_size(data: dict) -> tuple[float, float]:
    """`title` cat: a standalone heading — no card, no ports (TSX:838)."""
    inner_max = TTL_MAX_W - 2 * TTL_PAD_X
    w = 0.0
    h = 0.0
    if not data.get("hideTitle"):
        text = data.get("title") or "—"
        w = max(w, min(_text_w(text, TTL_TXT_FS), inner_max))
        h += _lines(text, TTL_TXT_FS, inner_max) * TTL_TXT_FS * TTL_TXT_LH
    if not data.get("hideSub") and data.get("sub"):
        w = max(w, min(_text_w(data.get("sub"), TTL_SUB_FS), inner_max))
        h += TTL_SUB_MT + _lines(data.get("sub"), TTL_SUB_FS, inner_max) * TTL_SUB_FS * TTL_SUB_LH
    return w + 2 * TTL_PAD_X, max(h + 2 * TTL_PAD_Y, TTL_TXT_FS * TTL_TXT_LH)


def _mdnote_size(data: dict) -> tuple[float, float]:
    """`mdnote` cat: markdown body under a coloured header (TSX:849)."""
    hd_w = 2 * MD_HD_PAD_X
    if not data.get("hideChip") and data.get("tag"):
        hd_w += _text_w(data.get("tag"), CHIP_FS, CHAR_W_MONO) + 2 * CHIP_PAD_X + MD_HD_GAP
    if not data.get("hideTitle"):
        hd_w += _text_w(data.get("title") or "—", LR_TTL_FS)
    hd_h = 2 * MD_HD_PAD_Y + max(CHIP_FS * CHIP_LH + 2 * CHIP_PAD_Y, LR_TTL_FS * LR_TTL_LH)

    body = "" if data.get("hideSub") else (data.get("body") if data.get("body") is not None else data.get("sub"))
    body_w = _text_w(body, MD_BODY_FS) + 2 * MD_BODY_PAD_X
    w = min(MD_MAX_W, max(MD_MIN_W, hd_w, body_w))
    inner = w - 2 * MD_BODY_PAD_X
    body_h = 0.0
    if body:
        body_h = _lines(body, MD_BODY_FS, inner) * MD_BODY_FS * MD_BODY_LH + MD_BODY_PAD_T + MD_BODY_PAD_B
    h = max(MD_MIN_H, hd_h + body_h + 2 * BORDER)
    return w, h


def estimate_size(node: dict, *, mode: str = "auto") -> tuple[float, float]:
    """Estimated rendered ``(width, height)`` of one node, in canvas pixels.

    ``mode`` picks the port layout the flow is rendered with:
    ``"sided"`` (handles on four sides), ``"lr"`` (ins-left / outs-right), or
    ``"auto"`` (default) which takes the larger of the two — a flow's port mode
    is a per-flow UI setting the author can flip at any time, so a layout that
    only fits one of them would break on the flip.

    An author-set width/height always wins (that is what a resize persists).
    """
    node = node if isinstance(node, dict) else {}
    data = node.get("data")
    data = data if isinstance(data, dict) else {}
    cat = str(data.get("cat") or "")

    if node.get("type") == "group" or cat == "group":
        if data.get("collapsed") is False:
            w, h = GRPBOX_MIN_W, GRPBOX_MIN_H
        else:
            w, h = max(GRP_MIN_W, _std_size(data, [])[0]), GRP_MIN_H
    elif cat == "title":
        w, h = _title_size(data)
    elif cat == "mdnote":
        w, h = _mdnote_size(data)
    else:
        ports = iter_ports(data)
        if mode == "lr":
            w, h = _lr_size(data, ports)
        elif mode == "sided":
            w, h = _std_size(data, ports)
        else:
            sw, sh = _std_size(data, ports)
            lw, lh = _lr_size(data, ports)
            w, h = max(sw, lw), max(sh, lh)

    ew, eh = _explicit(node, "width"), _explicit(node, "height")
    return (ew or w, eh or h)


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------
def _position(node: dict) -> tuple[float, float] | None:
    """The node's author-supplied top-left, or None when it has none/invalid."""
    pos = node.get("position")
    if not isinstance(pos, dict):
        return None
    x, y = _num(pos.get("x")), _num(pos.get("y"))
    if x is None or y is None:
        return None
    return x, y


def has_position(node: dict) -> bool:
    """True when the node carries a usable ``position``."""
    return _position(node) is not None


def _overlaps(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    tolerance: float = _OVERLAP_EPS,
) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return (
        ax < bx + bw - tolerance
        and bx < ax + aw - tolerance
        and ay < by + bh - tolerance
        and by < ay + ah - tolerance
    )


# ---------------------------------------------------------------------------
# layered layout
# ---------------------------------------------------------------------------
def _depths(ids: list[str], edges: Iterable[tuple[str, str]]) -> dict[str, int]:
    """Longest-path depth per node — Kahn, so a cycle cannot hang it.

    Order is input order throughout (FIFO queue seeded in input order), so the
    result is deterministic. Nodes trapped in a cycle are never dequeued; they
    are resolved afterwards, in input order, one level past their resolved
    predecessors.
    """
    succ: dict[str, list[str]] = {i: [] for i in ids}
    preds: dict[str, list[str]] = {i: [] for i in ids}
    for s, t in edges:
        if s == t or s not in succ or t not in preds:
            continue
        succ[s].append(t)
        preds[t].append(s)

    indeg = {i: len(preds[i]) for i in ids}
    depth = {i: 0 for i in ids}
    queue = [i for i in ids if indeg[i] == 0]
    settled: set[str] = set()
    head = 0
    while head < len(queue):
        n = queue[head]
        head += 1
        settled.add(n)
        for m in succ[n]:
            depth[m] = max(depth[m], depth[n] + 1)
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    for i in ids:  # cycle remainder, input order
        if i in settled:
            continue
        known = [depth[p] + 1 for p in preds[i] if p in settled]
        depth[i] = max(known) if known else depth[i]
        settled.add(i)
    return depth


def _columns(
    ids: list[str], edges: Iterable[tuple[str, str]]
) -> list[list[str]]:
    """Group node ids into left-to-right columns.

    Connected nodes land in the column of their topological depth; nodes with
    no edge at all are packed into trailing columns of :data:`GRID_ROWS` so
    they never share space with the wired graph.
    """
    edges = list(edges)
    known = set(ids)
    linked = {i for e in edges for i in e if i in known}
    wired = [i for i in ids if i in linked]
    loose = [i for i in ids if i not in linked]

    cols: list[list[str]] = []
    if wired:
        depth = _depths(wired, edges)
        by_depth: dict[int, list[str]] = {}
        for i in wired:
            by_depth.setdefault(depth[i], []).append(i)
        cols = [by_depth[d] for d in sorted(by_depth)]
    for k in range(0, len(loose), GRID_ROWS):
        cols.append(loose[k : k + GRID_ROWS])
    return cols


def _place_columns(
    cols: list[list[str]],
    sizes: dict[str, tuple[float, float]],
    origin: tuple[float, float],
) -> dict[str, tuple[float, float]]:
    """Assign a top-left to every id in ``cols``. Columns are centred on a
    common horizontal axis so a fan-out reads symmetrically."""
    heights = [
        sum(sizes[i][1] for i in col) + ROW_GAP * max(0, len(col) - 1) for col in cols
    ]
    tallest = max(heights, default=0.0)
    out: dict[str, tuple[float, float]] = {}
    x = origin[0]
    for col, col_h in zip(cols, heights):
        col_w = max((sizes[i][0] for i in col), default=0.0)
        y = origin[1] + (tallest - col_h) / 2.0
        for i in col:
            out[i] = (x, y)
            y += sizes[i][1] + ROW_GAP
        x += col_w + COL_GAP
    return out


def _edge_pairs(edges: Iterable[Any]) -> list[tuple[str, str]]:
    pairs = []
    for e in edges or ():
        if not isinstance(e, dict):
            continue
        s, t = e.get("source"), e.get("target")
        if isinstance(s, str) and isinstance(t, str):
            pairs.append((s, t))
    return pairs


def apply_layout(
    nodes: list[dict],
    edges: list[dict] | None = None,
    *,
    only_missing: bool = True,
    origin: tuple[float, float] = (0.0, 0.0),
    mode: str = "auto",
) -> list[dict]:
    """Return ``nodes`` with a non-overlapping ``position`` on every node.

    ``only_missing=True`` (the default) respects the author: a node that
    carries a position keeps it, and only nodes without one are placed. The
    exception is a set of positions that cannot be meant literally — every
    node on ``{0, 0}``, or estimated boxes that intersect by more than
    :data:`RELAYOUT_TOLERANCE`. Those nodes are *affected* and get laid out
    too, in free space to the right of whatever was kept, so a kept node never
    moves under a relayout.

    ``only_missing=False`` lays out every node from scratch.

    Input is never mutated: each node is shallow-copied before its position is
    written. Node order is preserved.
    """
    nodes = [n for n in (nodes or ()) if isinstance(n, dict)]
    if not nodes:
        return []
    pairs = _edge_pairs(edges)
    sizes = {
        str(n.get("id")): estimate_size(n, mode=mode)
        for n in nodes
        if n.get("id") is not None
    }
    ids = [str(n.get("id")) for n in nodes if n.get("id") is not None]
    by_id = {str(n.get("id")): n for n in nodes if n.get("id") is not None}

    if only_missing:
        kept: list[str] = []
        relayout = [i for i in ids if not has_position(by_id[i])]
        positioned = [i for i in ids if has_position(by_id[i])]
        # Overlapping boxes are the symptom this module exists to fix: an
        # author that produced one has not really placed those nodes.
        boxes = {i: (*_position(by_id[i]), *sizes[i]) for i in positioned}  # type: ignore[misc]
        clashing = set()
        for a_i, a in enumerate(positioned):
            for b in positioned[a_i + 1 :]:
                if _overlaps(boxes[a], boxes[b], RELAYOUT_TOLERANCE):
                    clashing.add(a)
                    clashing.add(b)
        kept = [i for i in positioned if i not in clashing]
        relayout += [i for i in ids if i in clashing]
        relayout = [i for i in ids if i in set(relayout)]  # back to input order
    else:
        kept = []
        relayout = list(ids)

    out = [dict(n) for n in nodes]
    if not relayout:
        return out

    block_origin = origin
    if kept:
        right = max(_position(by_id[i])[0] + sizes[i][0] for i in kept)  # type: ignore[index]
        top = min(_position(by_id[i])[1] for i in kept)  # type: ignore[index]
        block_origin = (right + COL_GAP, top)

    moving = set(relayout)
    placed = _place_columns(
        _columns(relayout, [(s, t) for s, t in pairs if s in moving and t in moving]),
        sizes,
        block_origin,
    )
    for n in out:
        i = str(n.get("id"))
        if i in placed:
            x, y = placed[i]
            n["position"] = {"x": round(x), "y": round(y)}
    return out


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------
class StreamLayout:
    """Running-column allocator for nodes that arrive one at a time.

    The draw pipeline emits a node before the edge that would place it in a
    layer, so the layered layout above cannot run there without buffering the
    whole graph — and buffering is exactly what the streaming architecture
    exists to avoid (a node must render the moment it is generated). This keeps
    the same size estimator and the same gaps, and answers the only question
    that has to be answered per node: *where is there room?*

    A node that arrives with a sane position keeps it. One that arrives without
    a position, or on top of a node already placed, is allocated the next free
    slot — filling a column top-down to :data:`STREAM_COL_H`, then starting the
    next column. Deterministic for a given arrival order.
    """

    def __init__(
        self,
        *,
        origin: tuple[float, float] = (0.0, 0.0),
        column_height: float = STREAM_COL_H,
        mode: str = "auto",
    ) -> None:
        self._origin = origin
        self._column_h = column_height
        self._mode = mode
        self._boxes: list[tuple[float, float, float, float]] = []
        self._x = origin[0]
        self._y = origin[1]
        self._col_w = 0.0

    def _next_column(self) -> None:
        self._x += self._col_w + COL_GAP
        self._y = self._origin[1]
        self._col_w = 0.0

    def _allocate(self, w: float, h: float) -> tuple[float, float]:
        if self._y > self._origin[1] and self._y + h > self._origin[1] + self._column_h:
            self._next_column()
        for _ in range(_MAX_PROBE):
            box = (self._x, self._y, w, h)
            if not any(_overlaps(box, b) for b in self._boxes):
                self._y += h + ROW_GAP
                self._col_w = max(self._col_w, w)
                return box[0], box[1]
            self._y += h + ROW_GAP
            if self._y + h > self._origin[1] + self._column_h:
                self._next_column()
        return self._x, self._y

    def place(self, node: dict) -> dict:
        """Give ``node`` a position if it needs one. Returns the same dict.

        Mutates in place — the draw worker hands each node straight on to the
        sink, so a copy would only be discarded.
        """
        if not isinstance(node, dict):
            return node
        w, h = estimate_size(node, mode=self._mode)
        pos = _position(node)
        if pos is not None:
            box = (pos[0], pos[1], w, h)
            if not any(_overlaps(box, b) for b in self._boxes):
                self._boxes.append(box)
                return node
        x, y = self._allocate(w, h)
        self._boxes.append((x, y, w, h))
        node["position"] = {"x": round(x), "y": round(y)}
        return node
