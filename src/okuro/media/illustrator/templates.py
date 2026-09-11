# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: provides a set of templates for generating 3D scenes based on a structured input brief
# index:
#   imports
#   class Brief
#   def _typography
#   def _bg
#   def _corner_legend
#   def _auto_callouts
#   def _transformation_bipartite
#   def _central_radial
#   def _axis_flow
#   def _stratified
#   def _field_with_focus
#   def _contrast_pair
#   def _sankey
#   def _tree
#   def _wave_stack
#   def _annotated_specimen
#   def _small_multiples
#   def _marey_grid
#   def _ridgeline_stack
#   def _sorted_matrix
#   def _flow_with_focal_thread
#   def _volume_3d
#   def render_brief
# AGENT_HEADER_END -->
"""Swiss layout templates.

A template is a Python function that, given a small Brief, emits a fully
populated Scene with all primitives placed on the 12x6 grid. The LLM's job
becomes: (a) pick a template name, (b) supply the template's slots
(eyebrow text, title text, motif params, optional labels). The template
owns positioning, type hierarchy, construction marks. Geometry is no
longer up for negotiation.
"""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field as PField

from .grid import DEFAULT as GRID
from .scene import (
    Scene, Canvas, Volume3D,
    FieldPrim, FocalPoint, Constellation, WaveStack, Cone, IsoVolume,
    ArrowStack, OrbitRings, Lattice, ParticleCloud,
    Permutation, DualPane, ScrambleBlock,
    Text, Axis, Tick, Bracket, Measure,
    Sankey, Tree,
    Callout, CornerLegend, TickRings, DimensionStack, ProportionalDot,
    Sparkline, MareyGrid, Ridgeline, SortedMatrix,
)


# ---------------------------------------------------------------- Brief

class Brief(BaseModel):
    """Structured semantic input to a template. Plain dataclass-ish.
    Most fields are optional so different templates can use different subsets."""
    template: Literal[
        "transformation_bipartite",
        "central_radial",
        "axis_flow",
        "stratified",
        "field_with_focus",
        "contrast_pair",
        "wave_stack",
        "sankey",
        "tree",
        "volume_3d",
        "annotated_specimen",
        "small_multiples",
        "marey_grid",
        "ridgeline_stack",
        "sorted_matrix",
        "flow_with_focal_thread",
    ]
    topic: str
    category: str | None = None
    seed: int = 0

    # Austerity controls how much typographic chrome is rendered:
    #   editorial (default) -- eyebrow + title + meta + caption
    #   poster              -- title only; max negative space, hero plate
    austerity: Literal["editorial", "poster"] = "editorial"

    # Verbosity controls the dressing layer (callouts, corner legend, scales)
    # that lifts a "math diagram" into an "editorial plate":
    #   lean (default) -- only the motif + Swiss type slots
    #   rich           -- the motif AUTO-DRESSED with leader-line callouts
    #                     pointing at salient parts and a corner legend block
    verbosity: Literal["lean", "rich"] = "lean"

    # Per-scene chromatic accent. Default green; LLM may pick amber/red/blue/
    # violet/cyan when the topic carries a tonal cue (warm = amber/red,
    # cool = blue/cyan, dramatic = violet).
    accent: Literal["green", "amber", "red", "blue", "violet", "cyan"] = "green"

    # Optional callout list — short tag + value pairs the LLM judged worth
    # surfacing. When verbosity=rich, templates anchor these to the motif and
    # route them out to the margins. When verbosity=lean, they are ignored.
    callouts: list[tuple[str, str]] = PField(default_factory=list)

    # Type slots (always allowed)
    eyebrow: str = ""
    title: str = ""
    meta: str = ""        # bottom-right small annotation
    caption: str = ""     # one-line description below title

    # Annotations / parts
    left_label: str | None = None
    right_label: str | None = None
    center_label: str | None = None     # axis center label (e.g. "KEY")
    measure_label: str | None = None    # dimension line label

    # Motif knobs (varies by template)
    motif: str | None = None            # which motif primitive to plug in
    n: int = 8                          # cardinality (n_stars, permutation n, etc.)
    rings: int = 5
    chaos: float = 0.6
    iso_shape: Literal["cube", "prism", "octa"] = "cube"
    pane_left: str = "lattice_ordered"
    pane_right: str = "lattice_scrambled"
    parts: list[str] = PField(default_factory=list)  # axis_flow / stratified

    # Constellation edges (for field_with_focus / network topics)
    edges: list[tuple[int, int]] = PField(default_factory=list)

    # Sankey
    sankey_left:  list[str] = PField(default_factory=list)
    sankey_right: list[str] = PField(default_factory=list)
    sankey_flows: list[tuple[int, int, float]] = PField(default_factory=list)

    # Tree
    tree_nodes:   list[str] = PField(default_factory=list)
    tree_parents: list[int] = PField(default_factory=list)

    # volume_3d
    volume_kind: Literal["cube_lattice", "embedding_cloud", "axis_3d"] = "cube_lattice"
    n_3d: int = 4
    clusters: int = 3
    highlight_cell: tuple[int, int, int] | None = None
    vector_to: tuple[float, float, float] = (0.7, 0.5, 0.6)

    # small_multiples — list of cells, each (title, stat, [v0..vn]) where
    # values are 0..1 floats for the sparkline. Up to 12 cells (3 rows x 4 cols).
    multiples: list[tuple[str, str, list[float]]] = PField(default_factory=list)
    highlight_cell_idx: int = -1   # which cell index gets the chromatic accent

    # marey_grid
    stations: list[str] = PField(default_factory=list)
    time_ticks: int = 8
    trajectories: list[list[tuple[float, float]]] = PField(default_factory=list)
    trajectory_labels: list[str] = PField(default_factory=list)
    focal_idx: int = -1            # also reused by ridgeline + flow_with_focal_thread

    # ridgeline_stack
    ridges: list[tuple[str, list[float]]] = PField(default_factory=list)

    # sorted_matrix
    matrix_rows: list[str] = PField(default_factory=list)
    matrix_cols: list[str] = PField(default_factory=list)
    matrix: list[list[float]] = PField(default_factory=list)
    cluster_box: tuple[int, int, int, int] | None = None
    matrix_glyph: Literal["fill", "dot"] = "fill"


# ---------------------------------------------------------------- Helpers

def _typography(brief: Brief, *, title_row: float = 5.25,
                eyebrow_row: float = 0.85, meta_row: float = 5.25,
                caption_row: float | None = None) -> list:
    """Standard Swiss type stack: eyebrow TL, title BL, meta BR, optional caption.
    Modes:
      poster -> title only.
      rich   -> title only too (eyebrow + meta live inside the corner legend).
      editorial -> full stack."""
    out: list = []
    poster = brief.austerity == "poster"
    rich = brief.verbosity == "rich"
    suppress_chrome = poster or rich
    # eyebrow only renders in editorial mode (suppressed for poster + rich)
    if brief.eyebrow and not suppress_chrome:
        out.append(Text(id="t_eyebrow", slot="eyebrow",
                        text=brief.eyebrow, col=1.0, row=eyebrow_row,
                        size_mult=1.15))   # slightly larger so it asserts itself
    if brief.title:
        # Poster bumps the title ~30% — title is the only chrome, give it weight.
        title_mult = 1.3 if poster else 1.0
        out.append(Text(id="t_title", slot="title",
                        text=brief.title, col=1.0, row=title_row,
                        size_mult=title_mult))
    if brief.meta and not suppress_chrome:
        out.append(Text(id="t_meta", slot="annotation",
                        text=brief.meta, col=11.0, row=meta_row, align="right"))
    if brief.caption and caption_row is not None and not suppress_chrome:
        out.append(Text(id="t_caption", slot="caption",
                        text=brief.caption, col=1.0, row=caption_row))
    return out


def _bg(opacity: float = 0.06, pattern: str = "dotgrid"):
    return FieldPrim(id="bg", pattern=pattern, opacity=opacity)  # type: ignore[arg-type]


# ---------------------------------------------------------------- Dressings

def _corner_legend(b: Brief, *, fig_n: int = 1) -> CornerLegend | None:
    """Auto-dress: build the off-canvas figure identity from Brief slots.
    Returns None when there's nothing to show (avoid empty stamps)."""
    if not (b.eyebrow or b.title or b.meta):
        return None
    return CornerLegend(
        id="legend",
        corner="br",
        title=b.eyebrow or b.title,           # top line: domain tag
        subtitle=b.meta or "",                # one-line meta
        scale_label=b.measure_label or "",    # repurposed as the scale
        fig=f"fig. {fig_n:02d}",
    )


def _auto_callouts(b: Brief, anchors: list[tuple[float, float]]) -> list[Callout]:
    """Map Brief.callouts onto anchor points, alternating margin sides.
    `anchors` is a list of (col, row) on the 12x6 grid; we cycle through
    them. The label is sent to the right margin if anchor is in the left
    half, else to the left margin — keeps leaders short."""
    if not b.callouts or not anchors:
        return []
    out: list[Callout] = []
    for i, (label, value) in enumerate(b.callouts):
        ac, ar = anchors[i % len(anchors)]
        side = "right" if ac < 6.0 else "left"
        # Stack labels vertically alternating top/bottom row to avoid collisions
        # Pick a label_col near the corresponding margin.
        if side == "right":
            lc = 11.0
        else:
            lc = 1.0
        # rotate label rows so multiple callouts don't stack on the same y
        lr_options = [1.4, 2.0, 4.2, 4.8]
        lr = lr_options[i % len(lr_options)]
        out.append(Callout(
            id=f"cb_{i}", anchor_col=ac, anchor_row=ar,
            label_col=lc, label_row=lr,
            label=label, value=value, side=side,
        ))
    return out


# ---------------------------------------------------------------- Templates


def _transformation_bipartite(b: Brief) -> Scene:
    """Left zone <-> right zone with axis + center key + brackets.
    Use for: encryption, hashing, compression, before/after, transformation."""
    motif = b.motif or "permutation"
    primitives: list = [_bg()]

    # Construction: faint axis through the center, brackets around zones
    primitives.append(Axis(id="ax", from_col=1.0, from_row=3.0,
                            to_col=11.0, to_row=3.0, ticks=0,
                            weight=0.5, opacity=0.45))

    # Brackets calling out the input and output zones
    primitives.append(Bracket(id="bL", from_col=2.4, from_row=1.05,
                               to_col=4.4, to_row=4.95, side="left",
                               depth=0.20, label=b.left_label or "input"))
    primitives.append(Bracket(id="bR", from_col=7.6, from_row=1.05,
                               to_col=9.6, to_row=4.95, side="right",
                               depth=0.20, label=b.right_label or "output"))

    # Top dimension line if measure_label provided
    if b.measure_label:
        primitives.append(Measure(id="m1", from_col=3.5, from_row=0.55,
                                   to_col=8.5, to_row=0.55,
                                   label=b.measure_label))

    # The motif itself
    if motif == "permutation":
        primitives.append(Permutation(
            id="motif", n=b.n,
            left_x=GRID.x(3.5), right_x=GRID.x(8.5),
            top_y=GRID.y(1.4), bottom_y=GRID.y(4.6),
            chromatic=True, seed=b.seed,
        ))
    elif motif == "dual_pane":
        primitives.append(DualPane(
            id="motif",
            left_kind=b.pane_left, right_kind=b.pane_right,
            cols=8, rows=8, spacing=GRID.module_w * 0.34, seed=b.seed,
            arrow=False,  # axis already provides the directionality
        ))
    elif motif == "scramble":
        primitives.append(ScrambleBlock(
            id="motif", cols=14, rows=6,
            cx=GRID.x(6), cy=GRID.y(3),
            spacing=GRID.module_w * 0.55, node_r=2.0,
            seed=b.seed, chaos=b.chaos,
        ))

    # Center tick = the "key" / transform
    if b.center_label:
        primitives.append(Tick(id="key", col=6.0, row=3.0,
                                orientation="v", length=0.20,
                                label=b.center_label, label_side="below"))

    # Auto-dressings: anchor callouts on the 4 zone corners + legend
    if b.verbosity == "rich":
        anchors = [(3.4, 1.2), (8.6, 1.2), (3.4, 4.8), (8.6, 4.8)]
        primitives.extend(_auto_callouts(b, anchors))
        legend = _corner_legend(b)
        if legend is not None:
            primitives.append(legend)

    primitives.extend(_typography(b))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _central_radial(b: Brief) -> Scene:
    """Center anchor with radial elements. Use for: scope, defense, atom,
    agent loop, layered, observe/act."""
    # Iso motif gets a hex-tile floor backdrop (Pasted reference: hex
    # ground + glowing iso cube). Other motifs use the dot grid.
    use_hex = (b.motif == "iso")
    primitives: list = [_bg(opacity=0.10 if use_hex else 0.06,
                             pattern="hex" if use_hex else "dotgrid")]
    cx, cy = 6.0, 3.0  # grid center

    # Faint crosshair construction
    primitives.append(Axis(id="ax_h", from_col=1.0, from_row=cy,
                            to_col=11.0, to_row=cy,
                            weight=0.4, opacity=0.30))
    primitives.append(Axis(id="ax_v", from_col=cx, from_row=0.5,
                            to_col=cx, to_row=5.5,
                            weight=0.4, opacity=0.30))

    motif = b.motif or "orbit_rings"
    if motif == "orbit_rings":
        # In rich mode, lay a TickRings dial behind the orbit rings to
        # add instrument-panel texture (Nightingale-style) before the motif.
        if b.verbosity == "rich":
            primitives.append(TickRings(
                id="dial",
                cx=GRID.x(cx), cy=GRID.y(cy),
                rings=4, base_r=GRID.module_w * 0.4,
                spacing=GRID.module_w * 0.40, n_ticks=24,
                scale_label=b.measure_label or "",
            ))
        primitives.append(OrbitRings(
            id="motif", cx=GRID.x(cx), cy=GRID.y(cy),
            rings=b.rings, base_r=GRID.module_w * 0.55,
            spacing=GRID.module_w * 0.34, chromatic=True,
        ))
    elif motif == "iso":
        primitives.append(IsoVolume(
            id="motif", cx=GRID.x(cx), cy=GRID.y(cy),
            size=GRID.module_w * 2.4, shape=b.iso_shape,
        ))
    elif motif == "constellation":
        # Dial backdrop also reinforces the field-of-influence reading.
        if b.verbosity == "rich":
            primitives.append(TickRings(
                id="dial",
                cx=GRID.x(cx), cy=GRID.y(cy),
                rings=2, base_r=GRID.module_w * 1.4,
                spacing=GRID.module_w * 0.5, n_ticks=12,
                scale_label="",
            ))
        primitives.append(Constellation(
            id="motif", n_stars=b.n,
            bbox=(GRID.x(2.5), GRID.y(1.0), GRID.x(9.5), GRID.y(5.0)),
            seed=b.seed,
            edges=[(i, (i*3+1) % b.n) for i in range(b.n)],
        ))

    primitives.append(FocalPoint(
        id="focal", cx=GRID.x(cx), cy=GRID.y(cy),
        r=7, halo_r=44, chromatic=True,
    ))

    if b.center_label:
        primitives.append(Tick(id="lbl", col=cx, row=cy + 0.6,
                                orientation="v", length=0.0,
                                label=b.center_label, label_side="below"))

    # Auto-dressings: leader-line callouts pinned to motif extremes + corner legend
    if b.verbosity == "rich":
        # 4 cardinal anchors around the central motif
        anchors = [(cx, 1.4), (10.0, cy), (cx, 4.6), (2.0, cy)]
        primitives.extend(_auto_callouts(b, anchors))
        legend = _corner_legend(b)
        if legend is not None:
            primitives.append(legend)

    primitives.extend(_typography(b))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _axis_flow(b: Brief) -> Scene:
    """Horizontal axis with ticks at sequential stops. Use for: pipeline,
    supply chain, sequence of stages, OODA loop unrolled."""
    primitives: list = [_bg()]
    parts = b.parts or [f"step {i+1}" for i in range(b.n)]
    n = len(parts)

    primitives.append(Axis(id="ax", from_col=1.5, from_row=3.0,
                            to_col=10.5, to_row=3.0,
                            ticks=n - 1, weight=0.7, opacity=0.7))

    # Tick + label per part
    span = (10.5 - 1.5)
    for i, part in enumerate(parts):
        col = 1.5 + (span * i / max(1, n - 1))
        primitives.append(Tick(id=f"tk_{i}", col=col, row=3.0,
                                orientation="v", length=0.22,
                                label=part, label_side="below"))
        # focal node at each stop
        primitives.append(FocalPoint(
            id=f"node_{i}", cx=GRID.x(col), cy=GRID.y(3.0),
            r=5, halo_r=18, chromatic=(i == n - 1),
        ))

    # Arrow head at far right
    arrow_x = GRID.x(10.5)
    arrow_y = GRID.y(3.0)
    # we render the arrow head as part of the axis manually:
    # using ArrowStack of count=1
    primitives.append(ArrowStack(
        id="head", count=1,
        start=(arrow_x - 24, arrow_y),
        spacing=200, size=24,
    ))

    # Auto-dressings: callouts above each stage tick (alternating top/bottom)
    if b.verbosity == "rich":
        anchors = []
        for i in range(min(len(b.callouts), n)):
            col = 1.5 + (span * i / max(1, n - 1))
            anchors.append((col, 3.0))
        primitives.extend(_auto_callouts(b, anchors))
        legend = _corner_legend(b)
        if legend is not None:
            primitives.append(legend)

    primitives.extend(_typography(b))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _stratified(b: Brief) -> Scene:
    """Horizontal strata (memory hierarchy, abstraction layers)."""
    primitives: list = [_bg()]
    parts = b.parts or [f"L{i}" for i in range(min(b.n, 6))]
    n = len(parts)
    top, bot = 1.2, 4.8
    # Truncate over-long labels so they don't blow past the margin.
    def _trunc(s: str, mx: int = 22) -> str:
        return s if len(s) <= mx else s[: mx - 1] + "…"

    for i, part in enumerate(parts):
        row = top + (bot - top) * (i / max(1, n - 1))
        # stratum line — starts further right to leave room for left labels
        primitives.append(Axis(id=f"st_{i}", from_col=3.5, from_row=row,
                                to_col=10.0, to_row=row,
                                weight=0.6, opacity=0.55))
        # label on left, LEFT-aligned at col 0.5 → grows rightward up to
        # the axis start (col 3.0). Truncate long labels to fit.
        primitives.append(Text(id=f"st_l_{i}", slot="annotation",
                                text=_trunc(part), col=0.5, row=row + 0.06,
                                align="left"))
        # samples along the line — tightened spacing to fit new axis range
        for k in range(7):
            col = 4.0 + k * 0.92
            primitives.append(FocalPoint(
                id=f"st_n_{i}_{k}", cx=GRID.x(col), cy=GRID.y(row),
                r=2.4, halo_r=8, chromatic=(i == 0 and k == 3),
            ))

    # Auto-dressings: anchor callouts at the right edge of each stratum,
    # route them to the right margin column with row-aligned label rows
    # so they don't collide with the left-side stratum names.
    if b.verbosity == "rich":
        callouts = b.callouts[:n]   # at most one per stratum
        for i, (label, value) in enumerate(callouts):
            row = top + (bot - top) * (i / max(1, n - 1))
            primitives.append(Callout(
                id=f"st_cb_{i}",
                anchor_col=10.0, anchor_row=row,
                label_col=11.0, label_row=row,
                label=label, value=value, side="right",
            ))
        legend = _corner_legend(b)
        if legend is not None:
            primitives.append(legend)

    primitives.extend(_typography(b))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _field_with_focus(b: Brief) -> Scene:
    """Sparse constellation field + bright anchor + selected edges.
    If brief.edges is provided, uses force-directed layout (topology-aware)."""
    primitives: list = [_bg(opacity=0.04)]
    edges = b.edges if b.edges else [
        (0, (i * 5 + 1) % (b.n or 22)) for i in range(min(8, (b.n or 22) - 1))
    ]
    primitives.append(Constellation(
        id="motif", n_stars=b.n or 22,
        bbox=(GRID.x(1.5), GRID.y(1.2), GRID.x(10.5), GRID.y(4.8)),
        seed=b.seed,
        edges=edges,
    ))
    primitives.append(FocalPoint(
        id="focal", cx=GRID.x(6), cy=GRID.y(3),
        r=8, halo_r=48, chromatic=True,
    ))
    if b.center_label:
        primitives.append(Tick(id="lbl", col=6.0, row=3.0,
                                orientation="v", length=0.0,
                                label=b.center_label, label_side="above"))

    # Auto-dressings for field_with_focus: 4 cardinal anchors + corner legend
    if b.verbosity == "rich":
        anchors = [(6.0, 1.4), (10.0, 3.0), (6.0, 4.6), (2.0, 3.0)]
        primitives.extend(_auto_callouts(b, anchors))
        legend = _corner_legend(b)
        if legend is not None:
            primitives.append(legend)

    primitives.extend(_typography(b))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _contrast_pair(b: Brief) -> Scene:
    """Two zones with annotations + arrow. Lighter than transformation_bipartite."""
    primitives: list = [_bg()]
    primitives.append(DualPane(
        id="motif",
        left_kind=b.pane_left, right_kind=b.pane_right,
        cols=7, rows=7, spacing=GRID.module_w * 0.34,
        seed=b.seed, arrow=True,
    ))
    if b.left_label:
        primitives.append(Text(id="ll", slot="annotation",
                                text=b.left_label, col=2.5, row=4.85, align="center"))
    if b.right_label:
        primitives.append(Text(id="rl", slot="annotation",
                                text=b.right_label, col=9.5, row=4.85, align="center"))

    # Auto-dressings: callouts on the left/right pane corners
    if b.verbosity == "rich":
        anchors = [(2.5, 1.5), (9.5, 1.5), (2.5, 4.5), (9.5, 4.5)]
        primitives.extend(_auto_callouts(b, anchors))
        legend = _corner_legend(b)
        if legend is not None:
            primitives.append(legend)

    primitives.extend(_typography(b))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _sankey(b: Brief) -> Scene:
    """Two-column flow with proportional ribbons. Many-to-many flows."""
    primitives: list = [_bg(opacity=0.05)]
    if not b.sankey_left or not b.sankey_right:
        # Fallback: 3-to-3 even split
        b.sankey_left = b.sankey_left or ["A", "B", "C"]
        b.sankey_right = b.sankey_right or ["X", "Y", "Z"]
        b.sankey_flows = b.sankey_flows or [
            (0, 0, 1.0), (0, 1, 0.5),
            (1, 1, 1.0), (1, 2, 0.7),
            (2, 0, 0.6), (2, 2, 1.2),
        ]
    primitives.append(Sankey(
        id="motif",
        left_labels=b.sankey_left,
        right_labels=b.sankey_right,
        flows=b.sankey_flows,
        left_col=3.0, right_col=9.0,
        top_row=1.3, bottom_row=4.7,
    ))

    # Auto-dressings: callouts pinned above/below the flow band
    if b.verbosity == "rich":
        anchors = [(3.0, 1.0), (9.0, 1.0), (3.0, 5.0), (9.0, 5.0)]
        primitives.extend(_auto_callouts(b, anchors))
        legend = _corner_legend(b)
        if legend is not None:
            primitives.append(legend)

    primitives.extend(_typography(b))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _tree(b: Brief) -> Scene:
    """Top-down hierarchical tree. Use for taxonomies, dependency trees."""
    primitives: list = [_bg(opacity=0.05)]
    if not b.tree_nodes:
        b.tree_nodes = ["root", "A", "B", "A1", "A2", "B1"]
        b.tree_parents = [-1, 0, 0, 1, 1, 2]
    primitives.append(Tree(
        id="motif",
        nodes=b.tree_nodes,
        parents=b.tree_parents,
        bbox_cols=(1.5, 10.5),
        bbox_rows=(1.4, 4.4),
    ))

    # Auto-dressings: anchor callouts at the 4 corners of the tree bbox
    if b.verbosity == "rich":
        anchors = [(2.0, 1.4), (10.0, 1.4), (2.0, 4.4), (10.0, 4.4)]
        primitives.extend(_auto_callouts(b, anchors))
        legend = _corner_legend(b)
        if legend is not None:
            primitives.append(legend)

    primitives.extend(_typography(b))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _wave_stack(b: Brief) -> Scene:
    """Wave occupies content. Used for physics, signals, oscillation."""
    primitives: list = [_bg()]
    primitives.append(WaveStack(
        id="motif", waves=2, amp=GRID.module_w * 1.6,
        wavelength=GRID.module_w * 2.6, y=GRID.y(3),
        width=GRID.width, chromatic=True,
    ))
    # Skip the inline AMP measure when verbosity=rich (callouts already
    # supply the dimensional metadata; the measure line crosses callouts).
    if b.measure_label and b.verbosity != "rich":
        primitives.append(Measure(id="m_amp",
                                   from_col=10.6, from_row=1.4,
                                   to_col=10.6, to_row=4.6,
                                   label=b.measure_label))

    # Auto-dressings: anchor callouts well clear of wave amplitude (rows 1.5
    # and 4.5 sit just outside the typical peaks/troughs).
    if b.verbosity == "rich":
        anchors = [(2.5, 1.5), (5.5, 4.5), (8.5, 1.5)]
        primitives.extend(_auto_callouts(b, anchors))
        legend = _corner_legend(b)
        if legend is not None:
            primitives.append(legend)

    primitives.extend(_typography(b))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _annotated_specimen(b: Brief) -> Scene:
    """ONE focal subject in the center surrounded by a radial halo of leader
    lines pointing to k labelled facts in the margins. Patek Philippe meets
    Distill.pub. 'Anatomy of X' / model card / benchmark-of-X topics.

    The focal motif is picked by `motif`:
      orbit_rings | iso | constellation | focal_only
    """
    primitives: list = [_bg(opacity=0.04)]
    cx, cy = 6.0, 3.0

    # Light construction crosshair, lower contrast than central_radial
    primitives.append(Axis(id="ax_h", from_col=2.0, from_row=cy,
                            to_col=10.0, to_row=cy,
                            weight=0.3, opacity=0.18))
    primitives.append(Axis(id="ax_v", from_col=cx, from_row=1.0,
                            to_col=cx, to_row=5.0,
                            weight=0.3, opacity=0.18))

    motif = b.motif or "orbit_rings"
    if motif == "orbit_rings":
        primitives.append(OrbitRings(
            id="motif", cx=GRID.x(cx), cy=GRID.y(cy),
            rings=b.rings or 3, base_r=GRID.module_w * 0.5,
            spacing=GRID.module_w * 0.30, chromatic=True,
        ))
    elif motif == "iso":
        primitives.append(IsoVolume(
            id="motif", cx=GRID.x(cx), cy=GRID.y(cy),
            size=GRID.module_w * 1.8, shape=b.iso_shape,
            dotted_floor=True,
        ))
    elif motif == "constellation":
        primitives.append(Constellation(
            id="motif", n_stars=b.n or 9,
            bbox=(GRID.x(4.5), GRID.y(2.0), GRID.x(7.5), GRID.y(4.0)),
            seed=b.seed,
            edges=[(0, i) for i in range(1, b.n or 9)],
        ))
    # always have a bright anchor at the dead center
    primitives.append(FocalPoint(
        id="anchor", cx=GRID.x(cx), cy=GRID.y(cy),
        r=8, halo_r=52, chromatic=True,
    ))
    if b.center_label:
        primitives.append(Tick(id="lbl", col=cx, row=cy + 0.55,
                                orientation="v", length=0.0,
                                label=b.center_label, label_side="below"))

    # Radial leader-line halo: k callouts evenly distributed around the
    # focal anchor, alternating left/right margins. This IS the template's
    # signature — we emit them even when verbosity=lean (they're the motif),
    # filling in stub values when LLM didn't supply real ones.
    callouts = b.callouts or [
        ("attribute", str(i + 1)) for i in range(4)
    ]
    k = min(8, len(callouts))
    # When 5+ facts: switch to a single DimensionStack on the right margin
    # to avoid spaghetti leader-line crossings. Tighter, more "spec sheet".
    # When ≤4: keep the radial leader halo (it's the signature look).
    if k >= 5:
        primitives.append(DimensionStack(
            id="sp_dim", col=9.4, row=1.2, width_cols=2.4,
            title=b.eyebrow.split("//")[-1].strip() if b.eyebrow else "facts",
            items=list(callouts[:k]),
        ))
    else:
        # 8 radial anchor positions around motif at radius ~1.2 modules
        anchor_positions = [
            (cx, cy - 1.4),         # N
            (cx + 2.0, cy - 1.0),   # NE
            (cx + 2.4, cy),         # E
            (cx + 2.0, cy + 1.0),   # SE
            (cx, cy + 1.4),         # S
            (cx - 2.0, cy + 1.0),   # SW
            (cx - 2.4, cy),         # W
            (cx - 2.0, cy - 1.0),   # NW
        ]
        label_rows = [1.2, 1.6, 2.4, 3.6, 4.4, 4.4, 3.6, 2.4]
        for i, (label, value) in enumerate(callouts[:k]):
            ac, ar = anchor_positions[i]
            side = "right" if ac >= cx else "left"
            lc = 11.0 if side == "right" else 1.0
            lr = label_rows[i]
            primitives.append(Callout(
                id=f"sp_{i}", anchor_col=ac, anchor_row=ar,
                label_col=lc, label_row=lr,
                label=label, value=value, side=side, tip=True,
            ))

    # Always emit a corner legend — the figure-identity is what makes this
    # template feel "published" rather than "diagram".
    legend = _corner_legend(b)
    if legend is not None:
        primitives.append(legend)

    primitives.extend(_typography(b))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _small_multiples(b: Brief) -> Scene:
    """3 rows x 4 cols of mini-plates. Each cell = micro-axis + sparkline +
    title (top-left) + stat (bottom-right). One cell can be chromatically
    highlighted. Tufte/Bloomberg contact-sheet aesthetic.

    Use for: comparisons across n variants of the same shape — A/B,
    year-over-year, country-by-country, model-by-model, week-by-week."""
    primitives: list = [_bg(opacity=0.04)]
    cells = b.multiples or [
        (f"item {i+1}", "—", [0.2, 0.5, 0.4, 0.7, 0.6, 0.8])
        for i in range(12)
    ]
    cells = cells[:12]

    # Cell layout on 12x6 grid (1.0..11.0 horiz, 1.0..5.0 vert):
    cols, rows = 4, 3
    avail_w_cols = 10.0
    avail_h_rows = 4.0
    cell_w = (avail_w_cols - 0.4 * (cols - 1)) / cols   # 2.2 cols
    cell_h = (avail_h_rows - 0.25 * (rows - 1)) / rows  # 1.17 rows
    gap_w = 0.4
    gap_h = 0.25

    for idx, (title, stat, values) in enumerate(cells):
        r = idx // cols
        c = idx % cols
        col0 = 1.0 + c * (cell_w + gap_w)
        row0 = 1.0 + r * (cell_h + gap_h)
        col1 = col0 + cell_w
        row1 = row0 + cell_h
        is_focal = (idx == b.highlight_cell_idx)

        # cell frame: top + bottom rules + light L-tick at top-left
        top_op = 0.7 if is_focal else 0.45
        primitives.append(Axis(
            id=f"sm_{idx}_top", from_col=col0, from_row=row0,
            to_col=col1, to_row=row0,
            weight=0.7 if is_focal else 0.5, opacity=top_op,
        ))
        primitives.append(Axis(
            id=f"sm_{idx}_bot", from_col=col0, from_row=row1,
            to_col=col1, to_row=row1,
            weight=0.5, opacity=0.35,
        ))

        # title text top-left (small uppercase)
        primitives.append(Text(
            id=f"sm_{idx}_t", slot="annotation",
            text=title, col=col0 + 0.05,
            row=row0 + 0.20, align="left",
        ))
        # stat text bottom-right (small)
        primitives.append(Text(
            id=f"sm_{idx}_s", slot="annotation",
            text=stat, col=col1 - 0.05,
            row=row1 - 0.10, align="right",
        ))
        # sparkline body inside cell — clamped, leaves space for title/stat
        spark_x = GRID.x(col0) + 8
        spark_y = GRID.y(row0 + 0.45)
        spark_w = GRID.module_w * cell_w - 16
        spark_h = (GRID.y(row1 - 0.30) - GRID.y(row0 + 0.45))
        primitives.append(Sparkline(
            id=f"sm_{idx}_l",
            values=[max(0.0, min(1.0, v)) for v in (values or [0.5])],
            x=spark_x, y=spark_y,
            width=spark_w, height=max(8.0, spark_h),
            terminal_dot=True, chromatic=is_focal, style="line",
        ))

    # Always render typography; corner legend optional via verbosity
    if b.verbosity == "rich":
        legend = _corner_legend(b)
        if legend is not None:
            primitives.append(legend)

    primitives.extend(_typography(b, title_row=5.55, eyebrow_row=0.55,
                                    meta_row=5.55))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _marey_grid(b: Brief) -> Scene:
    """Marey-style space-time chart. Stations on left axis (rows), time
    on bottom axis (columns), n diagonal trajectories whose slope encodes
    speed. Use for: pipelines, training curves, version evolution, transit."""
    primitives: list = [_bg(opacity=0.04)]
    stations = b.stations or [f"S{i}" for i in range(5)]
    trajectories = b.trajectories or [
        [(0.0, 0), (0.3, 1), (0.55, 2), (0.85, 3), (1.0, 4)],
        [(0.0, 0), (0.5, 2), (1.0, 4)],
    ]
    primitives.append(MareyGrid(
        id="motif", stations=stations, time_ticks=b.time_ticks,
        trajectories=trajectories,
        trajectory_labels=b.trajectory_labels,
        focal_idx=b.focal_idx,
    ))
    if b.verbosity == "rich":
        anchors = [(2.5, 1.4), (10.0, 1.4), (2.5, 4.6), (10.0, 4.6)]
        primitives.extend(_auto_callouts(b, anchors))
        legend = _corner_legend(b)
        if legend is not None:
            primitives.append(legend)
    primitives.extend(_typography(b))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _ridgeline_stack(b: Brief) -> Scene:
    """Joy Division ridgeline stack of n hairline curves with one focal row.
    Use for: model comparisons, distributions, signal stacks."""
    primitives: list = [_bg(opacity=0.04)]
    rows = b.ridges or [
        (f"row {i+1}", [0.2, 0.4, 0.7, 0.5, 0.3, 0.6, 0.5, 0.4])
        for i in range(6)
    ]
    primitives.append(Ridgeline(
        id="motif", rows=rows, focal_idx=b.focal_idx,
    ))
    if b.verbosity == "rich":
        legend = _corner_legend(b)
        if legend is not None:
            primitives.append(legend)
    primitives.extend(_typography(b))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _sorted_matrix(b: Brief) -> Scene:
    """Reorderable matrix: rows × cols cells with value glyphs. Use for:
    confusion matrix, attention map, co-occurrence, feature×model grids."""
    primitives: list = [_bg(opacity=0.04)]
    rows = b.matrix_rows or [f"r{i}" for i in range(6)]
    cols = b.matrix_cols or [f"c{j}" for j in range(8)]
    matrix = b.matrix or [
        [((i * 7 + j * 3) % 10) / 10.0 for j in range(len(cols))]
        for i in range(len(rows))
    ]
    primitives.append(SortedMatrix(
        id="motif",
        row_labels=rows, col_labels=cols, matrix=matrix,
        cluster_box=b.cluster_box, glyph=b.matrix_glyph,
    ))
    if b.verbosity == "rich":
        legend = _corner_legend(b)
        if legend is not None:
            primitives.append(legend)
    primitives.extend(_typography(b))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _flow_with_focal_thread(b: Brief) -> Scene:
    """Sankey with one chromatic lineage traced source-to-sink, all other
    flows desaturated. Use for: 'where does X actually go?' — budgets,
    energy flow, attention routing, training data → output."""
    primitives: list = [_bg(opacity=0.04)]
    if not b.sankey_left or not b.sankey_right:
        b.sankey_left = b.sankey_left or ["A", "B", "C"]
        b.sankey_right = b.sankey_right or ["X", "Y", "Z"]
        b.sankey_flows = b.sankey_flows or [
            (0, 0, 1.0), (0, 1, 0.4),
            (1, 1, 0.8), (1, 2, 1.2),
            (2, 0, 0.5), (2, 2, 0.7),
        ]
    # Default focal: index 0 (LLM should override)
    focal = b.focal_idx if b.focal_idx >= 0 else 0
    primitives.append(Sankey(
        id="motif",
        left_labels=b.sankey_left,
        right_labels=b.sankey_right,
        flows=b.sankey_flows,
        left_col=3.0, right_col=9.0,
        top_row=1.3, bottom_row=4.7,
        focal_flow_idx=focal,
    ))
    if b.verbosity == "rich":
        anchors = [(3.0, 1.0), (9.0, 1.0), (3.0, 5.0), (9.0, 5.0)]
        primitives.extend(_auto_callouts(b, anchors))
        legend = _corner_legend(b)
        if legend is not None:
            primitives.append(legend)
    primitives.extend(_typography(b))
    return Scene(topic=b.topic, category=b.category, seed=b.seed,
                  canvas=Canvas(), primitives=primitives, animations=[],
                  accent=b.accent)


def _volume_3d(b: Brief) -> Scene:
    """3D scene marker. SVG primitives are empty; render path branches on
    scene.render_mode == '3d' and consumes scene.volume via three_render."""
    canvas = Canvas()
    volume = Volume3D(
        kind=b.volume_kind,
        eyebrow=b.eyebrow,
        title=b.title,
        meta=b.meta,
        center_label=b.center_label or "",
        measure_label=b.measure_label or "",
        n=b.n_3d,
        clusters=b.clusters,
        highlight=b.highlight_cell,
        vector_to=b.vector_to,
    )
    return Scene(
        topic=b.topic, category=b.category, seed=b.seed,
        canvas=canvas, render_mode="3d", primitives=[], animations=[],
        volume=volume,
    )


# ---------------------------------------------------------------- registry

TEMPLATES = {
    "transformation_bipartite": _transformation_bipartite,
    "central_radial":           _central_radial,
    "axis_flow":                _axis_flow,
    "stratified":               _stratified,
    "field_with_focus":         _field_with_focus,
    "contrast_pair":            _contrast_pair,
    "wave_stack":                _wave_stack,
    "sankey":                    _sankey,
    "tree":                      _tree,
    "volume_3d":                 _volume_3d,
    "annotated_specimen":        _annotated_specimen,
    "small_multiples":           _small_multiples,
    "marey_grid":                _marey_grid,
    "ridgeline_stack":           _ridgeline_stack,
    "sorted_matrix":             _sorted_matrix,
    "flow_with_focal_thread":    _flow_with_focal_thread,
}


def render_brief(brief: Brief) -> Scene:
    fn = TEMPLATES[brief.template]
    return fn(brief)
