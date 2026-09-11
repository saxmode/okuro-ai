# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: defines scene schema with various animations and primitives for 3D visualization
# index:
#   imports
#   class PulseAnim
#   class DriftAnim
#   class PhaseAnim
#   class AdvanceAnim
#   class OrbitAnim
#   class ShuffleAnim
#   class FocalPoint
#   class Constellation
#   class WaveStack
#   class Cone
#   class IsoVolume
#   class ArrowStack
#   class FieldPrim
#   class OrbitRings
#   class Lattice
#   class ParticleCloud
#   class Permutation
#   class DualPane
#   class ScrambleBlock
#   class Label
#   class Text
#   class Axis
#   class Tick
#   class Bracket
#   class Measure
#   class Sankey
#   class Tree
#   class Callout
#   class CornerLegend
#   class TickRings
#   class DimensionStack
#   class ProportionalDot
#   class MareyGrid
#   class Ridgeline
#   class SortedMatrix
#   class Sparkline
#   class Canvas
#   class Volume3D
#   class Scene
# AGENT_HEADER_END -->
"""Scene schema. Validated; this is the contract LLM output must satisfy."""
from __future__ import annotations
from typing import Literal, Annotated, Union
from pydantic import BaseModel, Field as PField

# ---- animations ----------------------------------------------------------

class PulseAnim(BaseModel):
    kind: Literal["pulse"] = "pulse"
    target: str
    period_s: float = 2.4
    min_scale: float = 0.85
    max_scale: float = 1.15

class DriftAnim(BaseModel):
    kind: Literal["drift"] = "drift"
    target: str
    period_s: float = 18.0
    dx: float = 6.0
    dy: float = 4.0

class PhaseAnim(BaseModel):
    kind: Literal["phase"] = "phase"
    target: str
    period_s: float = 6.0
    # for waves only
    amp_px: float = 0.0

class AdvanceAnim(BaseModel):
    kind: Literal["advance"] = "advance"
    target: str
    period_s: float = 3.5
    distance_px: float = 60.0

class OrbitAnim(BaseModel):
    kind: Literal["orbit"] = "orbit"
    target: str
    period_s: float = 12.0
    radius: float = 40.0

class ShuffleAnim(BaseModel):
    """Random small displacement cycle -- evokes scrambling, encryption, chaos."""
    kind: Literal["shuffle"] = "shuffle"
    target: str
    period_s: float = 1.6
    jitter: float = 6.0  # px max displacement

Animation = Annotated[
    Union[PulseAnim, DriftAnim, PhaseAnim, AdvanceAnim, OrbitAnim, ShuffleAnim],
    PField(discriminator="kind"),
]

# ---- primitives ----------------------------------------------------------

class FocalPoint(BaseModel):
    kind: Literal["focal"] = "focal"
    id: str
    cx: float
    cy: float
    r: float = 8.0
    halo_r: float = 60.0
    chromatic: bool = True

class Constellation(BaseModel):
    kind: Literal["constellation"] = "constellation"
    id: str
    n_stars: int = 18
    edges: list[tuple[int, int]] = PField(default_factory=list)
    bbox: tuple[float, float, float, float] = (0, 0, 1200, 600)
    seed: int = 0

class WaveStack(BaseModel):
    kind: Literal["wave"] = "wave"
    id: str
    waves: int = 2
    amp: float = 180
    wavelength: float = 280
    y: float = 300
    width: float = 1200
    chromatic: bool = True

class Cone(BaseModel):
    """Cone/funnel of orbital rings."""
    kind: Literal["cone"] = "cone"
    id: str
    apex: tuple[float, float] = (600, 90)
    base_y: float = 580
    rings: int = 8
    base_rx: float = 540
    base_ry: float = 90
    dotted_alt: bool = False  # alternate solid/dotted rings (Kimi-K2 cone)

class IsoVolume(BaseModel):
    """Isometric cube/prism wireframe."""
    kind: Literal["iso"] = "iso"
    id: str
    cx: float = 600
    cy: float = 300
    size: float = 280
    shape: Literal["cube", "prism", "octa"] = "cube"
    dotted_floor: bool = False  # render floor + back walls as dotted (MFU iso)

class ArrowStack(BaseModel):
    kind: Literal["arrows"] = "arrows"
    id: str
    count: int = 4
    start: tuple[float, float] = (120, 300)
    spacing: float = 180
    size: float = 160

class FieldPrim(BaseModel):
    """Faint dotted/dashed construction lines (non-focal background)."""
    kind: Literal["field"] = "field"
    id: str
    pattern: Literal["dotgrid", "isoaxis", "horizon", "hex"] = "dotgrid"
    opacity: float = 0.35


class OrbitRings(BaseModel):
    """Concentric circular rings around a center, like an atom or radar."""
    kind: Literal["orbit_rings"] = "orbit_rings"
    id: str
    cx: float = 600
    cy: float = 300
    rings: int = 5
    base_r: float = 60
    spacing: float = 50
    chromatic: bool = False


class Lattice(BaseModel):
    """2D grid of dots/nodes optionally connected by edges, evokes mesh / kv-cache."""
    kind: Literal["lattice"] = "lattice"
    id: str
    cols: int = 12
    rows: int = 6
    cx: float = 600
    cy: float = 300
    spacing: float = 60
    node_r: float = 2.5
    show_edges: bool = True
    highlight: list[tuple[int, int]] = PField(default_factory=list)


class ParticleCloud(BaseModel):
    """Density field of small particles, used for entropy/probability/diffusion."""
    kind: Literal["particles"] = "particles"
    id: str
    n: int = 240
    bbox: tuple[float, float, float, float] = (0, 0, 1200, 600)
    seed: int = 0
    radius: float = 1.6
    density_falloff: Literal["uniform", "gaussian", "ring"] = "uniform"
    focus: tuple[float, float] = (600, 300)


class Permutation(BaseModel):
    """Bipartite mapping: N input nodes (left col) -> N output nodes (right col),
    connected by lines in a SHUFFLED order. Encryption, routing, hashing, S-box."""
    kind: Literal["permutation"] = "permutation"
    id: str
    n: int = 8
    left_x: float = 360
    right_x: float = 840
    top_y: float = 100
    bottom_y: float = 500
    chromatic: bool = True
    seed: int = 0


class DualPane(BaseModel):
    """Two contrasting blocks side-by-side with an arrow between -- before/after,
    plaintext/ciphertext, input/output, ordered/scrambled."""
    kind: Literal["dual_pane"] = "dual_pane"
    id: str
    left_kind: Literal["lattice_ordered", "lattice_scrambled",
                       "particles_uniform", "particles_clustered",
                       "constellation"] = "lattice_ordered"
    right_kind: Literal["lattice_ordered", "lattice_scrambled",
                        "particles_uniform", "particles_clustered",
                        "constellation"] = "lattice_scrambled"
    cols: int = 8
    rows: int = 8
    spacing: float = 32
    seed: int = 0
    arrow: bool = True


class ScrambleBlock(BaseModel):
    """Lattice with each node randomly offset from its slot. Evokes diffusion,
    avalanche, scrambling. Pair with shuffle animation for full effect."""
    kind: Literal["scramble"] = "scramble"
    id: str
    cols: int = 16
    rows: int = 8
    cx: float = 600
    cy: float = 300
    spacing: float = 50
    node_r: float = 2.0
    seed: int = 0
    chaos: float = 0.6  # 0.0 = perfect grid, 1.0 = full random in cell

class Label(BaseModel):
    kind: Literal["label"] = "label"
    id: str
    text: str
    x: float
    y: float
    size: int = 14
    case: Literal["upper", "lower", "as-is"] = "upper"
    opacity: float = 0.7


# Swiss-grid-aware typography ----------------------------------------------

class Text(BaseModel):
    """Slot-driven typography. Slot determines size, tracking, case, weight.
    Position is in GRID UNITS (col, row), not pixels."""
    kind: Literal["text"] = "text"
    id: str
    slot: Literal["eyebrow", "annotation", "value",
                  "caption", "title", "display"] = "annotation"
    text: str
    col: float
    row: float
    align: Literal["left", "center", "right"] = "left"
    opacity: float = 0.85
    size_mult: float = 1.0   # multiplier on the slot's base font-size


# Construction primitives (Swiss precision marks) --------------------------

class Axis(BaseModel):
    """A measurement axis -- horizontal or vertical line with optional ticks.
    Coordinates in GRID UNITS."""
    kind: Literal["axis"] = "axis"
    id: str
    from_col: float
    from_row: float
    to_col: float
    to_row: float
    ticks: int = 0          # number of evenly spaced tick marks (0 = none)
    weight: float = 0.6     # stroke width
    opacity: float = 0.7


class Tick(BaseModel):
    """A single tick mark with optional label. Hangs off an axis or stands alone."""
    kind: Literal["tick"] = "tick"
    id: str
    col: float
    row: float
    orientation: Literal["v", "h"] = "v"  # tick direction
    length: float = 0.16    # in modules (16 px default)
    label: str | None = None
    label_side: Literal["above", "below", "left", "right"] = "below"


class Bracket(BaseModel):
    """L-shaped bracket bracketing a region with an optional label.
    Useful to call out 'this section is X'."""
    kind: Literal["bracket"] = "bracket"
    id: str
    from_col: float
    from_row: float
    to_col: float
    to_row: float
    side: Literal["top", "bottom", "left", "right"] = "bottom"
    depth: float = 0.18     # how far the bracket arms extend (modules)
    label: str | None = None


class Measure(BaseModel):
    """Dimension line with end caps and a center value -- engineering drawing feel."""
    kind: Literal["measure"] = "measure"
    id: str
    from_col: float
    from_row: float
    to_col: float
    to_row: float
    label: str  # the measured value, e.g. "256 BIT" or "n=10"


class Sankey(BaseModel):
    """Many-to-many flow diagram. Two columns of nodes (left, right) with
    bezier ribbons whose width = flow value. For data lineage, traffic,
    energy budgets, market share."""
    kind: Literal["sankey"] = "sankey"
    id: str
    left_labels: list[str]
    right_labels: list[str]
    flows: list[tuple[int, int, float]] = PField(default_factory=list)
    # column geometry in grid units (col coords)
    left_col: float = 2.5
    right_col: float = 9.5
    top_row: float = 1.2
    bottom_row: float = 4.8
    node_w: float = 0.10  # column-bar width in modules
    # Optional: index into `flows` of the focal lineage to trace. When set,
    # all OTHER ribbons render at lowest contrast and the focal ribbon
    # gets full chromatic accent. -1 disables.
    focal_flow_idx: int = -1
    animate: bool = False           # emit ribbon fill-opacity timeline


class Tree(BaseModel):
    """Hierarchical top-down tree. Each node lists its parent index (root=-1)."""
    kind: Literal["tree"] = "tree"
    id: str
    nodes: list[str]            # display names; index = node id
    parents: list[int]          # parents[i] = parent index, or -1 for root
    bbox_cols: tuple[float, float] = (1.5, 10.5)
    bbox_rows: tuple[float, float] = (1.2, 4.8)


class Callout(BaseModel):
    """Architectural-style leader line from anchor (col, row) out to a margin
    label block. Polyline elbows to keep alignment with the Swiss grid:
    a vertical run from the anchor, then a horizontal run to the label.
    Label is a 2-line stack (top: small uppercase tag, bottom: value)."""
    kind: Literal["callout"] = "callout"
    id: str
    anchor_col: float
    anchor_row: float
    # absolute target position on the grid (the elbow lands at the bend)
    label_col: float
    label_row: float
    label: str = ""             # top line, uppercase, small
    value: str = ""             # bottom line, larger, can be a number or short word
    # which side does the label sit on? affects elbow direction + text-anchor
    side: Literal["left", "right"] = "right"
    # tip glyph: a tiny ringed dot at the anchor point so the leader has a foot
    tip: bool = True


class CornerLegend(BaseModel):
    """Pinned corner block: title + subtitle + mini scale glyph + fig number.
    The Distill.pub corner-identity primitive — turns any plate into a
    'published figure' rather than a chart. Anchored to one of 4 corners."""
    kind: Literal["corner_legend"] = "corner_legend"
    id: str
    corner: Literal["tl", "tr", "bl", "br"] = "br"
    title: str = ""             # uppercase, JetBrains Mono small
    subtitle: str = ""          # one line of context
    fig: str = ""               # e.g. "fig. 03" — bottom right of the block
    # mini scale: a hairline rule with two end ticks and a centered label.
    # Used to communicate "this is the unit" / "n=10" / "256-bit". Empty=skip.
    scale_label: str = ""


class TickRings(BaseModel):
    """Concentric instrument-panel rings around a center point with radial
    tick marks at given angles + a radial scale label at one o'clock.
    The dressing layer that turns central_radial into a 'dial'."""
    kind: Literal["tick_rings"] = "tick_rings"
    id: str
    cx: float = 600
    cy: float = 300
    rings: int = 4
    base_r: float = 60
    spacing: float = 38
    n_ticks: int = 12              # radial ticks around the outermost ring
    scale_label: str = ""          # e.g. "0..1.0 confidence"


class DimensionStack(BaseModel):
    """Spec-sheet column: n labelled rows, dotted leader to a right-aligned
    value. Conveys 'feels measured' texture without real data — slot text
    only. Anchored at (col, row) on the grid; grows downward."""
    kind: Literal["dimension_stack"] = "dimension_stack"
    id: str
    col: float = 1.0
    row: float = 1.5
    width_cols: float = 2.5
    items: list[tuple[str, str]] = PField(default_factory=list)
    title: str = ""                # optional column heading


class ProportionalDot(BaseModel):
    """Dot whose radius encodes a numeric value, with a hairline outer ring
    at scale max so the relative size is readable instantly. Replaces a
    bare dot when the data point IS the glyph."""
    kind: Literal["proportional_dot"] = "proportional_dot"
    id: str
    cx: float
    cy: float
    value: float = 0.5             # 0..1 normalised
    max_r: float = 18              # outer ring radius
    label: str = ""                # below-the-glyph label
    chromatic: bool = False


class MareyGrid(BaseModel):
    """Marey-style space-time chart: orthogonal hairline grid + stations on
    the left axis + time on the bottom axis + n diagonal trajectories whose
    slope encodes speed. One trajectory can be chromatically focal."""
    kind: Literal["marey"] = "marey"
    id: str
    stations: list[str] = PField(default_factory=list)
    time_ticks: int = 8
    # Each trajectory: list of (time_norm 0..1, station_index) waypoints
    trajectories: list[list[tuple[float, float]]] = PField(default_factory=list)
    trajectory_labels: list[str] = PField(default_factory=list)
    focal_idx: int = -1
    bbox_cols: tuple[float, float] = (3.0, 11.0)
    bbox_rows: tuple[float, float] = (1.2, 4.6)
    animate: bool = False           # emit progressive trajectory reveal


class Ridgeline(BaseModel):
    """N stacked hairline curves (Joy Division 'Unknown Pleasures' aesthetic).
    Each row: a list of 0..1 values. One row can be chromatically focal."""
    kind: Literal["ridgeline"] = "ridgeline"
    id: str
    rows: list[tuple[str, list[float]]] = PField(default_factory=list)
    focal_idx: int = -1
    bbox_cols: tuple[float, float] = (1.0, 11.0)
    bbox_rows: tuple[float, float] = (1.2, 4.6)
    overlap: float = 0.45          # fraction of row height curves overlap by


class SortedMatrix(BaseModel):
    """Reorderable matrix: rows × cols of cells, each cell encodes a 0..1
    value as filled-square OR proportional-dot. Optional cluster outline
    box. Bertin's reorderable matrix tradition."""
    kind: Literal["sorted_matrix"] = "sorted_matrix"
    id: str
    row_labels: list[str] = PField(default_factory=list)
    col_labels: list[str] = PField(default_factory=list)
    matrix: list[list[float]] = PField(default_factory=list)
    cluster_box: tuple[int, int, int, int] | None = None  # (r0,c0,r1,c1) inclusive
    glyph: Literal["fill", "dot"] = "fill"
    bbox_cols: tuple[float, float] = (2.0, 10.0)
    bbox_rows: tuple[float, float] = (1.2, 4.6)


class Sparkline(BaseModel):
    """Mini polyline glyph: list of 0..1 values mapped to a bbox.
    Hairline stroke + optional terminal dot at the last value. Used
    inside small_multiples cells and inline beside dimension labels."""
    kind: Literal["sparkline"] = "sparkline"
    id: str
    values: list[float] = PField(default_factory=list)
    x: float = 0
    y: float = 0
    width: float = 80
    height: float = 22
    terminal_dot: bool = True
    chromatic: bool = False
    style: Literal["line", "area", "bar"] = "line"


Primitive = Annotated[
    Union[
        FocalPoint, Constellation, WaveStack, Cone, IsoVolume, ArrowStack,
        FieldPrim, Label, OrbitRings, Lattice, ParticleCloud,
        Permutation, DualPane, ScrambleBlock,
        Text, Axis, Tick, Bracket, Measure,
        Sankey, Tree,
        Callout, CornerLegend, TickRings, DimensionStack, ProportionalDot,
        Sparkline, MareyGrid, Ridgeline, SortedMatrix,
    ],
    PField(discriminator="kind"),
]

# ---- scene ---------------------------------------------------------------

class Canvas(BaseModel):
    width: int = 1200
    height: int = 600
    preset: Literal["wide", "hd", "square"] = "wide"

class Volume3D(BaseModel):
    """Payload for 3D scenes. Consumed by three_render.render_volume()."""
    kind: Literal["cube_lattice", "embedding_cloud", "axis_3d"] = "cube_lattice"
    eyebrow: str = ""
    title: str = ""
    meta: str = ""
    center_label: str = ""
    measure_label: str = ""
    n: int = 4
    clusters: int = 3
    highlight: tuple[int, int, int] | None = None
    vector_to: tuple[float, float, float] = (0.7, 0.5, 0.6)
    cam: tuple[float, float, float] = (3.0, 1.8, 3.4)


class Scene(BaseModel):
    topic: str
    category: str | None = None
    canvas: Canvas = Canvas()
    seed: int = 0
    render_mode: Literal["svg", "3d"] = "svg"
    primitives: list[Primitive] = PField(default_factory=list)
    animations: list[Animation] = PField(default_factory=list)
    volume: Volume3D | None = None
    # Per-scene accent for chromatic aberration. The G-channel of the RGB
    # split is swapped to this color; R and B stay anchored. "green" is the
    # default house style.
    accent: Literal["green", "amber", "red", "blue", "violet", "cyan"] = "green"
    # When True, emitters that support timeline animation (marey, sankey)
    # add inline SMIL animations that play during webm recording. PNG path
    # leaves this False so animations are never visible at frame 0.
    animate: bool = False

    def primitive_ids(self) -> set[str]:
        return {p.id for p in self.primitives}
