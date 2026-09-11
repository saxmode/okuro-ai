# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism layout engine — derive a grid ARRANGEMENT for each rung
#   from its block shape, so a rung renders as composed columns/rows instead of
#   a flat vertical stack (Phase A of the composition layer).
# index:
#   ROLE constants
#   def _rung_types / def _role
#   def derive_rung_layout
#   def derive_layouts
# AGENT_HEADER_END -->
"""okuro·prism layout engine — the composition layer.

Until now a rung rendered as a flat vertical stack: every block full-width,
one after another (``prism-viewer.tsx`` mapped ``rungToBlocks`` straight to
``<BlockView>``). That reads as a wall of text and can't seat a chart beside
its explanation. This module adds an ARRANGEMENT overlay: a per-rung ``layout``
that groups the rung's flattened blocks into ROWS of column-spanned CELLS,
which the frontend renders over a 12-column grid.

Design (backward-compatible, deterministic):
  * The layout references blocks by INDEX into the SAME flattened sequence the
    renderer builds — ``rungToBlocks`` order: body(text) → media(diagram) →
    callouts(callout) → blocks[]. ``_rung_types`` replicates that order so the
    indices line up on both sides.
  * ``derive_rung_layout`` emits a layout ONLY for a small set of unambiguous,
    high-value shapes (text beside a visual, etc.); every other rung returns
    None → the renderer keeps the vertical stack. The engine can only IMPROVE a
    rung, never make one worse.
  * Called from ``storage.save_doc`` — the single write chokepoint — so every
    path (generate / edit / retailor / apply_ops) gets consistent, index-fresh
    layouts derived from the FINAL block sequence. No caller wiring needed.

v1 archetypes are deliberately conservative. Audience-aware archetype selection
(hero+KPI for a board, small-multiples for a technician) is Phase B, layered on
the leapfrog-#1 cognitive profile — this file is the substrate it will extend.
"""

from __future__ import annotations

from typing import Any, Optional

from okuro.prism.rungs import RUNGS as _RUNGS

# Layout role of a block type — what arrangement slot it wants.
#   visual — pairs beside prose (chart/graph/flow/image/mermaid diagram)
#   band   — already an internal tile grid; wants a full-width band (stat/meter)
#   text   — prose / short callout; the narration a visual explains. Also the
#            short-form claim types (evidence/quote/statement) so a claim can sit
#            AS the text side beside the visual that supports it (audit D3) —
#            these still respect the measure floor (they read as prose, so they
#            never ride the 4-col rail; `callout` alone is exempt via `_is_prose`).
#   wide   — complex internal structure; safest full-width (tables, cards, …).
#            `lens` stays wide on purpose: the persona-reframe is the deck's
#            signature full-width move, never squeezed into a column.
_VISUAL = {"chart", "graph", "flow", "image", "diagram", "gallery", "smallmultiples"}
_BAND = {"stat", "meter", "frequency", "hops"}
_TEXT = {"text", "callout", "evidence", "quote", "statement"}

# Typographic measure floor (rule #2, Bringhurst ~45ch): a substantial-prose
# block must never be seated narrower than this many of 12 columns, or the line
# runs too short to read. Visuals / cards / bands are exempt — only prose is
# measured; a short `callout` annotation is exempt too (see `_is_prose`).
_MIN_PROSE_SPAN = 5

# Type-level groups for the richer archetypes. These are matched by BLOCK TYPE
# (not by the coarse 4-way `_role`) inside `_pack`, so an archetype can pick out
# a specific kind of "wide"/"visual" block without disturbing role semantics
# (`band_lead` still keys off role alone).
#   _HERO     — a dominant analytical/interactive block; when it LEADS and a
#               short text/callout annotates it → 8|4 hero + detail rail.
#   _CARDLIKE — peer cards; a run of ≥2 tiles into a grid (2→6|6, 3→4|4|4, …).
#   _DUO      — comparable wide blocks; two adjacent → 6|6 side-by-side.
#   _RAIL     — a lead rail (checklist) beside a substantial main → 4|8. Lens is
#               deliberately NOT a rail: the persona-reframing device is the deck's
#               signature move (reference board.css:242-250) and reads full-width, not
#               squeezed into a 4-col sidebar. Its role stays "wide" (full-width row).
_HERO = {"graph", "simulator", "chart"}
_CARDLIKE = {"cards", "spec"}
_DUO = {"table", "matrix", "options", "compare"}
_RAIL = {"checklist"}


def _role(block_type: str) -> str:
    if block_type in _VISUAL:
        return "visual"
    if block_type in _BAND:
        return "band"
    if block_type in _TEXT:
        return "text"
    return "wide"


def _rung_types(rung: dict[str, Any]) -> list[str]:
    """The ordered block-TYPE sequence a rung flattens to — must match the
    frontend ``rungToBlocks``: body → media → callouts → blocks[]."""
    types: list[str] = []
    if str((rung or {}).get("body") or "").strip():
        types.append("text")
    types += ["diagram"] * len((rung or {}).get("media") or [])
    types += ["callout"] * len((rung or {}).get("callouts") or [])
    for b in (rung or {}).get("blocks") or []:
        if isinstance(b, dict) and b.get("type"):
            types.append(str(b["type"]))
    return types


def _row(*cells: tuple[int, int]) -> dict[str, Any]:
    return {"cells": [{"i": i, "span": span} for i, span in cells]}


# Column spans for a tiled grid, keyed by how many tiles share the row.
_GRID = {2: (6, 6), 3: (4, 4, 4), 4: (3, 3, 3, 3)}

# Harmonic split whitelist (typography rule #9 — Brockmann proportionality +
# Gerstner's "mobile grid": 12 divides evenly by 2/3/4/6, and the only legal
# asymmetric splits are the harmonic ratios). A row's column-span tuple may come
# ONLY from this set; an off-ratio split (11|1, 10|2, 5|7|… non-members) can
# never be emitted (`_validate_spans` asserts it). Both orientations of each
# asymmetric pair are legal so a mirrored split (5|7 vs 7|5) stays in-family.
_LEGAL_SPANS: frozenset[tuple[int, ...]] = frozenset({
    (12,),                 # full-width row
    (8, 4), (4, 8),        # 2:1  — hero+rail / sidebar
    (9, 3), (3, 9),        # 3:1  — dramatic asymmetry
    (7, 5), (5, 7),        # editorial split (mirrored for rhythm)
    (6, 6),                # 1:1  — genuine peers
    (4, 4, 4),             # thirds
    (3, 3, 3, 3),          # quarters
})


def _tile(idxs: list[int]) -> tuple[list[dict[str, Any]], bool]:
    """Tile a run of peer blocks into grid rows of ≤4 (2→6|6, 3→4|4|4,
    4→3|3|3|3; a trailing chunk of 1 stays full-width). ``paired`` is True iff a
    multi-cell row was produced. Shared by visual runs and peer-card runs."""
    rows: list[dict[str, Any]] = []
    paired = False
    m, end = 0, len(idxs)
    while m < end:
        chunk = min(4, end - m)
        if chunk == 1:
            rows.append(_row((idxs[m], 12)))
        else:
            spans = _GRID[chunk]
            rows.append(_row(*[(idxs[m + t], spans[t]) for t in range(chunk)]))
            paired = True
        m += chunk
    return rows, paired


def _pack(order: list[int], types: list[str], roles: list[str]) -> tuple[list[dict[str, Any]], bool]:
    """Greedy left-to-right packer over an index ``order``. Produces varied rows
    so a deck stops looking the same on every screen:
      * a HERO block (graph/simulator/chart) that LEADS a short text/callout →
        8|4 (hero + detail rail — weight to the dominant artifact);
      * a RUN of ≥2 ``visual`` blocks → a tiled grid row (2→6|6, 3→4|4|4,
        4→3|3|3|3, longer runs chunked into rows of ≤4) — image/chart galleries;
      * a RUN of ≥2 peer CARDS (cards/spec) → the same tiled grid — peer sets;
      * two adjacent comparable-wide blocks (table/matrix/options/compare) →
        6|6 side-by-side comparison;
      * a lead RAIL (checklist/lens) beside a substantial main → 4|8 sidebar;
      * ``text`` then ``visual`` → a split row, MIRRORED every other split
        (7|5, then visual-left 5|7) so consecutive splits don't read identical;
      * ``visual`` then ``text`` → a visual-left split (5|7);
      * anything else → its own full-width row.
    ``paired`` is True iff at least one multi-cell row was produced."""
    rows: list[dict[str, Any]] = []
    paired = False
    split_i = 0
    n = len(order)
    k = 0
    while k < n:
        i = order[k]
        j = order[k + 1] if k + 1 < n else None
        ti, r = types[i], roles[i]
        tj = types[j] if j is not None else None
        nxt = roles[j] if j is not None else None

        # Hero + detail rail: a dominant analytical/interactive block leads, a
        # short CALLOUT annotates it → 8|4 (before the visual-run/split rules so
        # a chart/graph pulls hero weight rather than an even split). The rail is
        # restricted to a callout on purpose: a prose text block at 4 cols
        # (~30ch) falls below the ~45ch measure floor (typography rule #2), so a
        # hero leading PROSE instead flows to the measure-safe visual|text 5|7
        # split below (or, if it can't pair, stacks full-width) — a prose block
        # is never squeezed into the 4-col detail rail.
        if ti in _HERO and tj == "callout":
            rows.append(_row((i, 8), (j, 4)))
            paired = True
            k += 2
            continue

        # A run of adjacent visuals → tile into grid rows of up to 4.
        if r == "visual":
            run = 1
            while k + run < n and roles[order[k + run]] == "visual":
                run += 1
            if run >= 2:
                trows, tp = _tile([order[k + t] for t in range(run)])
                rows += trows
                paired = paired or tp
                k += run
                continue

        # A run of adjacent peer cards (cards/spec) → the same tiled grid.
        if ti in _CARDLIKE:
            run = 1
            while k + run < n and types[order[k + run]] in _CARDLIKE:
                run += 1
            if run >= 2:
                trows, tp = _tile([order[k + t] for t in range(run)])
                rows += trows
                paired = paired or tp
                k += run
                continue

        # Two comparable wide blocks (table/matrix/options/compare) → 6|6.
        if ti in _DUO and tj in _DUO:
            rows.append(_row((i, 6), (j, 6)))
            paired = True
            k += 2
            continue

        # Sidebar + main: a lead rail (checklist/lens) beside a substantial main
        # (anything that isn't prose or a KPI band, and not another rail).
        if ti in _RAIL and j is not None and nxt not in ("text", "band") and tj not in _RAIL:
            rows.append(_row((i, 4), (j, 8)))
            paired = True
            k += 2
            continue

        if r == "text" and nxt == "visual":
            if split_i % 2 == 0:
                rows.append(_row((i, 7), (j, 5)))   # text | visual
            else:
                rows.append(_row((j, 5), (i, 7)))   # visual | text (mirrored)
            split_i += 1
            paired = True
            k += 2
            continue

        if r == "visual" and nxt == "text":
            rows.append(_row((i, 5), (j, 7)))       # visual | text
            paired = True
            k += 2
            continue

        rows.append(_row((i, 12)))
        k += 1
    return rows, paired


def _is_prose(block_type: str) -> bool:
    """A substantial-prose block that earns measure protection: role ``text``
    but NOT a short ``callout`` (a callout is a terse annotation, fine narrow)."""
    return _role(block_type) == "text" and block_type != "callout"


def _assert_measure(rows: list[dict[str, Any]], types: list[str]) -> None:
    """Guard (typography rule #2): no substantial-prose block is seated below the
    ~45ch measure floor (``_MIN_PROSE_SPAN`` cols). ``_pack`` never emits one by
    construction; this asserts the invariant so a future rule can't regress it."""
    for row in rows:
        for c in row["cells"]:
            if _is_prose(types[c["i"]]):
                assert c["span"] >= _MIN_PROSE_SPAN, (
                    f"prose block {c['i']} ({types[c['i']]}) seated at "
                    f"{c['span']} cols, below measure floor {_MIN_PROSE_SPAN}"
                )


def _validate_spans(rows: list[dict[str, Any]]) -> None:
    """Guard (typography rule #9): every row's column-span tuple is a harmonic
    member of ``_LEGAL_SPANS`` — no off-ratio split (11|1, 10|2) can be emitted."""
    for row in rows:
        spans = tuple(c["span"] for c in row["cells"])
        assert spans in _LEGAL_SPANS, f"non-harmonic split {spans}"


def _finalize(
    archetype: str, rows: list[dict[str, Any]], types: list[str]
) -> dict[str, Any]:
    """Validate a derived layout against the packer's guardrails, then wrap it.
    Guards (each only ever REJECTS an off-contract layout, never worsens one):
      * measure (typography rule #2) — no prose block below the measure floor.
      * harmonic (typography rule #9) — only whitelisted proportional splits.
    """
    _assert_measure(rows, types)
    _validate_spans(rows)
    return {"archetype": archetype, "rows": rows}


def derive_rung_layout(
    rung: dict[str, Any], profile: Optional[dict[str, Any]] = None
) -> Optional[dict[str, Any]]:
    """Arrange a rung's flattened blocks into grid rows, or None to keep the
    default vertical stack.

    Base: a content-only greedy packer (``_pack``) — seats a visual beside its
    narration, pairs two visuals; bands (stat/meter) and wide blocks stay
    full-width.

    Audience overlay (Phase B, via ``profile`` from ``audience.layout_profile``):
    when ``band_lead`` is set (concept-level / non-expert readers — board, exec),
    stat/meter KPI bands are PROMOTED to the top of the rung ahead of the prose
    (headline-numbers-first), then the rest is packed. A layout is emitted for a
    board reader whenever that promotion actually reorders something, even with
    no 2-column pair — the reorder is itself the board treatment.

    Slide-model overlay (via ``rung['slide_type']`` from ``slides.stamp_slide_types``):
    a COVER / SECTION level is a full-bleed single statement — its archetype is
    recorded so the frontend renders it as a cover, and its every block reads
    full-width; a DOC-VIEW level is one reading column of the full document. These
    take precedence over the content packer (a cover is never tiled)."""
    types = _rung_types(rung)
    n = len(types)

    # Slide-type archetype (the slide model on top of the packer). A cover/section
    # is a full-bleed frame; a doc-view is a single reading column. Both stack every
    # block full-width (12 cols) — measure-safe and honouring the inverse-density
    # model (the shallow cover and the deep document are both single-column, the
    # visual variety lives on the content levels between them).
    slide_type = (rung or {}).get("slide_type")
    if slide_type in ("cover", "section", "doc-view") and n >= 1:
        return _finalize(slide_type, [_row((i, 12)) for i in range(n)], types)

    if n < 2:
        return None
    roles = [_role(t) for t in types]

    if profile and profile.get("band_lead"):
        bands = [i for i, r in enumerate(roles) if r == "band"]
        rest = [i for i in range(n) if i not in bands]
        # Only a board treatment when a band exists AND isn't already leading.
        if bands and rest and bands[0] != 0:
            rows = [_row((b, 12)) for b in bands]
            packed, _ = _pack(rest, types, roles)
            return _finalize("band-lead", rows + packed, types)

    # Z-pattern: exactly 4 genuinely heterogeneous, non-band blocks → a 2×2
    # quadrant grid (two 6|6 rows) traced in reading scan-order. Requires all
    # three of visual/text/wide present (a lead label, a visual, a detail, a
    # CTA — the classic Z) so it never overrides the packer's nicer split/tile
    # handling of a homogeneous 4-block rung; any KPI band wants its own band.
    if n == 4 and "band" not in roles and len(set(roles)) >= 3:
        return _finalize("z-pattern", [_row((0, 6), (1, 6)), _row((2, 6), (3, 6))], types)

    rows, paired = _pack(list(range(n)), types, roles)
    if not paired:
        return None
    return _finalize("packed", rows, types)


def derive_layouts(doc: dict[str, Any]) -> int:
    """Set (or clear) ``layout`` on every rung of every facet in-place. Returns
    the number of rungs that received a layout. Idempotent — re-running on an
    already-derived doc reproduces the same result, so it's safe at every save.

    Reads the doc-level ``audience_layout`` hint (set by generation from the
    recipient's cognition, ``audience.layout_profile``) so board/exec decks get
    KPI-band-led rungs while technical decks keep narrative order."""
    profile = doc.get("audience_layout") if isinstance(doc.get("audience_layout"), dict) else None
    count = 0
    for facet in (doc.get("facets") or {}).values():
        for rung in (facet.get("rungs") or {}).values():
            if not isinstance(rung, dict):
                continue
            # A manually-pinned layout (set_layout op) is locked against the
            # auto-derive so a user's arrangement isn't overwritten on save.
            if rung.get("layout_locked"):
                count += 1
                continue
            layout = derive_rung_layout(rung, profile)
            if layout:
                rung["layout"] = layout
                count += 1
            else:
                rung.pop("layout", None)
    return count


__all__ = ["derive_rung_layout", "derive_layouts"]
