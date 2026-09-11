# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: provides SVG emitters for various 2D scene primitives
# index:
#   imports
#   def _rng
#   def emit_focal
#   def _spring_layout
#   def emit_constellation
#   def emit_wave
#   def _sin_path
#   def emit_cone
#   def emit_iso
#   def emit_arrows
#   def emit_field
#   def emit_label
#   def emit_orbit_rings
#   def emit_lattice
#   def emit_particles
#   def emit_permutation
#   def emit_dual_pane
#   def _pane_block
#   def emit_scramble
#   def emit_text
#   def _xml_escape
#   def emit_axis
#   def emit_tick
#   def emit_bracket
#   def emit_measure
#   def emit_sankey
#   def emit_tree
#   def emit_callout
#   def emit_corner_legend
#   def emit_tick_rings
#   def emit_dimension_stack
#   def emit_proportional_dot
#   def emit_sparkline
#   def emit_marey
#   def _densify
#   def emit_ridgeline
#   def emit_sorted_matrix
# AGENT_HEADER_END -->
"""SVG emitters for each primitive. Style is locked here, not in the LLM."""
from __future__ import annotations
import math
import random
from .scene import (
    FocalPoint, Constellation, WaveStack, Cone, IsoVolume, ArrowStack,
    FieldPrim, Label, OrbitRings, Lattice, ParticleCloud,
    Permutation, DualPane, ScrambleBlock,
    Text, Axis, Tick, Bracket, Measure,
    Sankey, Tree,
    Callout, CornerLegend, TickRings, DimensionStack, ProportionalDot,
    Sparkline, MareyGrid, Ridgeline, SortedMatrix,
)
from .grid import DEFAULT as GRID, TYPE_SCALE
from .style import PALETTE, FONT_FAMILY


def _rng(seed: int) -> random.Random:
    return random.Random(seed)


def emit_focal(p: FocalPoint) -> str:
    out = []
    out.append(f'<g id="{p.id}">')
    # outer halo (radial fade)
    out.append(
        f'<circle cx="{p.cx}" cy="{p.cy}" r="{p.halo_r}" fill="url(#halo)"/>'
    )
    # inner bloom — duplicate of core blurred deeply, sits behind chromatic core
    out.append(
        f'<circle cx="{p.cx}" cy="{p.cy}" r="{p.r}" '
        f'fill="{PALETTE["focal"]}" filter="url(#bloom_strong)" opacity="0.85"/>'
    )
    # chromatic-fringed core
    fl = ' filter="url(#chromatic)"' if p.chromatic else ""
    out.append(
        f'<circle cx="{p.cx}" cy="{p.cy}" r="{p.r}" fill="{PALETTE["focal"]}"{fl}/>'
    )
    out.append("</g>")
    return "\n".join(out)


def _spring_layout(n_nodes: int, edges: list[tuple[int, int]],
                   bbox: tuple[float, float, float, float],
                   seed: int) -> list[tuple[float, float]]:
    """Force-directed layout: edges pull connected nodes together,
    repulsion pushes everyone apart. Anchors topology in geometry."""
    import networkx as nx
    G = nx.Graph()
    G.add_nodes_from(range(n_nodes))
    valid_edges = [(a, b) for a, b in edges if 0 <= a < n_nodes and 0 <= b < n_nodes]
    G.add_edges_from(valid_edges)
    pos = nx.spring_layout(G, seed=seed, k=0.7, iterations=80)
    x0, y0, x1, y1 = bbox
    pad = 30
    pts: list[tuple[float, float]] = []
    for i in range(n_nodes):
        nx_, ny_ = pos[i]
        x = x0 + pad + (nx_ + 1) / 2 * (x1 - x0 - 2 * pad)
        y = y0 + pad + (1 - ny_) / 2 * (y1 - y0 - 2 * pad)
        pts.append((x, y))
    return pts


def emit_constellation(p: Constellation) -> str:
    rng = _rng(p.seed)
    x0, y0, x1, y1 = p.bbox
    if p.edges:
        # Topology-aware placement when we know the connections.
        pts = _spring_layout(p.n_stars, p.edges, p.bbox, p.seed)
    else:
        # No edges -> random scatter (sky-like).
        pts = [(rng.uniform(x0 + 40, x1 - 40), rng.uniform(y0 + 40, y1 - 40))
               for _ in range(p.n_stars)]
    out = [f'<g id="{p.id}">']
    # edges (lines)
    for a, b in p.edges:
        if 0 <= a < len(pts) and 0 <= b < len(pts):
            ax, ay = pts[a]
            bx, by = pts[b]
            out.append(
                f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" y2="{by:.1f}" '
                f'stroke="{PALETTE["stroke"]}" stroke-width="0.8" opacity="0.7"/>'
            )
    # stars: halo first (behind), then bloomed core, then crisp chromatic dot on top.
    for i, (x, y) in enumerate(pts):
        out.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="22" fill="url(#halo)" opacity="0.55"/>'
        )
        out.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.4" fill="{PALETTE["focal"]}" '
            f'filter="url(#bloom)" opacity="0.9"/>'
        )
        out.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.4" fill="{PALETTE["focal"]}" '
            f'filter="url(#chromatic)"/>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_wave(p: WaveStack) -> str:
    out = [f'<g id="{p.id}">']
    out.append(
        f'<line x1="0" y1="{p.y}" x2="{p.width}" y2="{p.y}" '
        f'stroke="{PALETTE["stroke_dim"]}" stroke-width="0.8" opacity="0.5"/>'
    )
    # vertical dotted gridlines
    step = p.wavelength / 4
    x = 0.0
    while x <= p.width:
        out.append(
            f'<line x1="{x:.1f}" y1="{p.y - p.amp - 30}" x2="{x:.1f}" '
            f'y2="{p.y + p.amp + 30}" stroke="{PALETTE["stroke_faint"]}" '
            f'stroke-width="1" stroke-dasharray="1 5" opacity="0.7"/>'
        )
        x += step
    # waves
    for i in range(p.waves):
        phase = i * math.pi / p.waves
        path = _sin_path(p.width, p.y, p.amp, p.wavelength, phase, samples=400)
        opacity = 1.0 if i == 0 else 0.45
        sw = 1.6 if i == 0 else 1.0
        fl = ' filter="url(#chromatic)"' if (p.chromatic and i == 0) else ""
        out.append(
            f'<path d="{path}" fill="none" stroke="{PALETTE["stroke"]}" '
            f'stroke-width="{sw}" opacity="{opacity}"{fl}/>'
        )
    # extrema markers on the primary wave (peaks + troughs alternating).
    # Match FlashAttention reference: glowing white dots at peaks,
    # smaller chromatic-only ringed dots at troughs.
    primary_phase = 0.0
    n_periods = max(1, int(p.width / p.wavelength))
    for k in range(n_periods * 2 + 1):
        # peak at quarter-wavelength offsets
        ex = (k + 0.25) * p.wavelength / 2 if k % 2 == 0 else \
             (k + 0.25) * p.wavelength / 2
        # peak: t = (2k+1)/4 wavelength -> sin = +/-1
        x_peak = (2 * k + 1) * p.wavelength / 4
        if x_peak > p.width:
            break
        sign = 1 if k % 2 == 0 else -1   # peak then trough, alternating
        y_peak = p.y - sign * p.amp
        if k % 2 == 0:
            # filled glowing peak
            out.append(
                f'<circle cx="{x_peak:.1f}" cy="{y_peak:.1f}" r="6" '
                f'fill="{PALETTE["focal"]}" filter="url(#bloom)" opacity="0.9"/>'
            )
            out.append(
                f'<circle cx="{x_peak:.1f}" cy="{y_peak:.1f}" r="6" '
                f'fill="{PALETTE["focal"]}" filter="url(#chromatic)"/>'
            )
        else:
            # outlined trough marker (just the chromatic ring)
            out.append(
                f'<circle cx="{x_peak:.1f}" cy="{y_peak:.1f}" r="5" '
                f'fill="none" stroke="{PALETTE["stroke"]}" stroke-width="1.2" '
                f'filter="url(#chromatic)"/>'
            )
    out.append("</g>")
    return "\n".join(out)


def _sin_path(width: float, y: float, amp: float, wl: float,
              phase: float, samples: int = 200) -> str:
    pts = []
    for i in range(samples + 1):
        x = i * width / samples
        yy = y + amp * math.sin(2 * math.pi * x / wl + phase)
        pts.append(f"{x:.1f},{yy:.1f}")
    return "M " + " L ".join(pts)


def emit_cone(p: Cone) -> str:
    ax, ay = p.apex
    out = [f'<g id="{p.id}">']
    # vertical axis
    out.append(
        f'<line x1="{ax}" y1="{ay}" x2="{ax}" y2="{p.base_y + 20}" '
        f'stroke="{PALETTE["stroke_dim"]}" stroke-width="0.6" opacity="0.5"/>'
    )
    # rings (ellipses, increasing as cone widens)
    for i in range(1, p.rings + 1):
        t = i / p.rings
        cy = ay + (p.base_y - ay) * t
        rx = p.base_rx * t
        ry = p.base_ry * t
        # solid ring
        out.append(
            f'<ellipse cx="{ax}" cy="{cy}" rx="{rx:.1f}" ry="{ry:.1f}" '
            f'fill="none" stroke="{PALETTE["stroke_dim"]}" stroke-width="0.6" '
            f'opacity="0.6"/>'
        )
        # dotted ring just inside
        out.append(
            f'<ellipse cx="{ax}" cy="{cy}" rx="{rx*0.94:.1f}" ry="{ry*0.94:.1f}" '
            f'fill="none" stroke="{PALETTE["stroke"]}" stroke-width="1" '
            f'stroke-dasharray="1 4" opacity="0.7"/>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_iso(p: IsoVolume) -> str:
    s = p.size / 2
    cx, cy = p.cx, p.cy
    # isometric projection: 30-degree axes
    cos30 = math.cos(math.radians(30))
    sin30 = math.sin(math.radians(30))
    # corners of a cube
    def proj(x, y, z):
        # x: right axis, y: left axis, z: vertical
        px = cx + (x - y) * cos30
        py = cy + (x + y) * sin30 - z
        return px, py
    out = [f'<g id="{p.id}">']
    if p.shape == "cube":
        edges = [
            ((-s, -s, -s), (s, -s, -s)),
            ((s, -s, -s), (s, s, -s)),
            ((s, s, -s), (-s, s, -s)),
            ((-s, s, -s), (-s, -s, -s)),
            ((-s, -s,  s), (s, -s,  s)),
            ((s, -s,  s), (s, s,  s)),
            ((s, s,  s), (-s, s,  s)),
            ((-s, s,  s), (-s, -s,  s)),
            ((-s, -s, -s), (-s, -s,  s)),
            ((s, -s, -s), (s, -s,  s)),
            ((s, s, -s), (s, s,  s)),
            ((-s, s, -s), (-s, s,  s)),
        ]
    elif p.shape == "octa":
        # octahedron
        a = s
        verts = [(a,0,0), (-a,0,0), (0,a,0), (0,-a,0), (0,0,a), (0,0,-a)]
        edges = [(verts[i], verts[j]) for i in range(6) for j in range(i+1,6)
                 if sum(abs(verts[i][k]-verts[j][k]) for k in range(3)) <= 2*a + 0.001]
    else:  # prism: 4-sided, taller
        h = s * 1.4
        edges = [
            ((-s, -s, -h), (s, -s, -h)),
            ((s, -s, -h), (s, s, -h)),
            ((s, s, -h), (-s, s, -h)),
            ((-s, s, -h), (-s, -s, -h)),
            ((-s, -s,  h), (s, -s,  h)),
            ((s, -s,  h), (s, s,  h)),
            ((s, s,  h), (-s, s,  h)),
            ((-s, s,  h), (-s, -s,  h)),
            ((-s, -s, -h), (-s, -s,  h)),
            ((s, -s, -h), (s, -s,  h)),
            ((s, s, -h), (s, s,  h)),
            ((-s, s, -h), (-s, s,  h)),
        ]
    for (a, b) in edges:
        ax, ay = proj(*a)
        bx, by = proj(*b)
        # If dotted_floor is on, route floor-face edges + back-vertical edges
        # to dotted construction. Front silhouette stays solid.
        if p.dotted_floor:
            zmax = max(c[2] for c in (a, b))
            zmin = min(c[2] for c in (a, b))
            on_floor = zmax == zmin and zmax == min(c[2] for c in [v for e in edges for v in e])
            on_back = (a[0] == b[0] and a[0] > 0) or (a[1] == b[1] and a[1] > 0)
            if on_floor or on_back:
                out.append(
                    f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" y2="{by:.1f}" '
                    f'stroke="{PALETTE["stroke"]}" stroke-width="0.9" '
                    f'stroke-dasharray="1 5" opacity="0.55"/>'
                )
                continue
        out.append(
            f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" y2="{by:.1f}" '
            f'stroke="{PALETTE["stroke"]}" stroke-width="1" opacity="0.85"/>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_arrows(p: ArrowStack) -> str:
    out = [f'<g id="{p.id}">']
    sx, sy = p.start
    for i in range(p.count):
        x = sx + i * p.spacing
        opacity = 1.0 - (i * 0.18)
        sw = 1.4 - (i * 0.15)
        size = p.size * (1 - i * 0.15)
        # 3D arrow: outline of a tetrahedral arrowhead pointing right
        # front face triangle
        path = (
            f'M {x:.1f} {sy - size/2:.1f} '
            f'L {x + size:.1f} {sy:.1f} '
            f'L {x:.1f} {sy + size/2:.1f} '
            f'L {x + size*0.35:.1f} {sy:.1f} Z'
        )
        fl = ' filter="url(#chromatic)"' if i == 0 else ""
        out.append(
            f'<path d="{path}" fill="none" stroke="{PALETTE["stroke"]}" '
            f'stroke-width="{sw:.2f}" opacity="{opacity:.2f}"{fl}/>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_field(p: FieldPrim, w: int, h: int) -> str:
    if p.pattern == "dotgrid":
        return (
            f'<rect id="{p.id}" x="0" y="0" width="{w}" height="{h}" '
            f'fill="url(#dotgrid)" opacity="{p.opacity}"/>'
        )
    if p.pattern == "horizon":
        return (
            f'<g id="{p.id}" opacity="{p.opacity}">'
            f'<line x1="0" y1="{h*0.62}" x2="{w}" y2="{h*0.62}" '
            f'stroke="{PALETTE["stroke_faint"]}" stroke-width="1" '
            f'stroke-dasharray="2 6"/>'
            f'</g>'
        )
    if p.pattern == "hex":
        # Tiled isometric hex floor (Pasted reference). We tile manually
        # rather than via <pattern> because hex tiling needs a 2-cell
        # offset. Cell size in px.
        out = [f'<g id="{p.id}" opacity="{p.opacity}">']
        sz = 28.0  # tile radius
        sw = 0.5
        col_step = sz * 1.5
        row_step = sz * math.sqrt(3)
        # how many fit
        n_cols = int(w / col_step) + 2
        n_rows = int(h / row_step) + 2
        for r in range(-1, n_rows):
            for c in range(-1, n_cols):
                cx = c * col_step
                cy = r * row_step + (row_step / 2 if c % 2 else 0)
                pts = []
                for k in range(6):
                    ang = math.radians(60 * k)
                    px = cx + sz * math.cos(ang)
                    py = cy + sz * math.sin(ang)
                    pts.append(f"{px:.1f},{py:.1f}")
                out.append(
                    f'<polygon points="{" ".join(pts)}" fill="none" '
                    f'stroke="{PALETTE["stroke_faint"]}" stroke-width="{sw}" '
                    f'opacity="0.6"/>'
                )
        out.append("</g>")
        return "\n".join(out)
    # isoaxis: faint isometric axes converging to center
    cx, cy = w / 2, h / 2
    out = [f'<g id="{p.id}" opacity="{p.opacity}">']
    for ang in (30, 150, 270):
        rad = math.radians(ang)
        ex = cx + math.cos(rad) * max(w, h)
        ey = cy + math.sin(rad) * max(w, h)
        out.append(
            f'<line x1="{cx}" y1="{cy}" x2="{ex:.1f}" y2="{ey:.1f}" '
            f'stroke="{PALETTE["stroke_faint"]}" stroke-width="1" '
            f'stroke-dasharray="1 6"/>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_label(p: Label) -> str:
    text = p.text
    if p.case == "upper":
        text = text.upper()
    elif p.case == "lower":
        text = text.lower()
    return (
        f'<text id="{p.id}" x="{p.x}" y="{p.y}" '
        f'font-family="{FONT_FAMILY}" font-size="{p.size}" '
        f'fill="{PALETTE["stroke"]}" opacity="{p.opacity}" '
        f'letter-spacing="2">{text}</text>'
    )


def emit_orbit_rings(p: OrbitRings) -> str:
    out = [f'<g id="{p.id}">']
    for i in range(p.rings):
        r = p.base_r + i * p.spacing
        opacity = 1.0 - (i / max(1, p.rings)) * 0.55
        # solid circle, hairline
        fl = ' filter="url(#chromatic)"' if (p.chromatic and i == 0) else ""
        out.append(
            f'<circle cx="{p.cx}" cy="{p.cy}" r="{r:.1f}" fill="none" '
            f'stroke="{PALETTE["stroke"]}" stroke-width="0.8" '
            f'opacity="{opacity:.2f}"{fl}/>'
        )
        # dotted twin just inside, every other ring
        if i % 2 == 0:
            out.append(
                f'<circle cx="{p.cx}" cy="{p.cy}" r="{r - 6:.1f}" fill="none" '
                f'stroke="{PALETTE["stroke"]}" stroke-width="1" '
                f'stroke-dasharray="1 5" opacity="{opacity*0.5:.2f}"/>'
            )
    out.append("</g>")
    return "\n".join(out)


def emit_lattice(p: Lattice) -> str:
    half_w = (p.cols - 1) * p.spacing / 2
    half_h = (p.rows - 1) * p.spacing / 2
    x0 = p.cx - half_w
    y0 = p.cy - half_h
    out = [f'<g id="{p.id}">']
    # edges first (so dots overlay)
    if p.show_edges:
        for r in range(p.rows):
            y = y0 + r * p.spacing
            out.append(
                f'<line x1="{x0:.1f}" y1="{y:.1f}" '
                f'x2="{x0 + (p.cols-1)*p.spacing:.1f}" y2="{y:.1f}" '
                f'stroke="{PALETTE["stroke_faint"]}" stroke-width="1" '
                f'opacity="0.55"/>'
            )
        for c in range(p.cols):
            x = x0 + c * p.spacing
            out.append(
                f'<line x1="{x:.1f}" y1="{y0:.1f}" '
                f'x2="{x:.1f}" y2="{y0 + (p.rows-1)*p.spacing:.1f}" '
                f'stroke="{PALETTE["stroke_faint"]}" stroke-width="1" '
                f'opacity="0.55"/>'
            )
    # nodes
    highlight = set(p.highlight)
    for r in range(p.rows):
        for c in range(p.cols):
            x = x0 + c * p.spacing
            y = y0 + r * p.spacing
            if (c, r) in highlight or (r, c) in highlight:
                out.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{p.node_r * 2:.1f}" '
                    f'fill="{PALETTE["focal"]}" filter="url(#chromatic)"/>'
                )
                out.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="14" '
                    f'fill="url(#halo)" opacity="0.5"/>'
                )
            else:
                out.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{p.node_r:.1f}" '
                    f'fill="{PALETTE["stroke"]}" opacity="0.6"/>'
                )
    out.append("</g>")
    return "\n".join(out)


def emit_particles(p: ParticleCloud) -> str:
    rng = _rng(p.seed)
    x0, y0, x1, y1 = p.bbox
    fx, fy = p.focus
    out = [f'<g id="{p.id}">']
    for _ in range(p.n):
        if p.density_falloff == "uniform":
            x = rng.uniform(x0, x1)
            y = rng.uniform(y0, y1)
        elif p.density_falloff == "gaussian":
            x = rng.gauss(fx, (x1 - x0) / 6)
            y = rng.gauss(fy, (y1 - y0) / 6)
        else:  # ring
            ang = rng.uniform(0, 2 * math.pi)
            r = rng.gauss((min(x1 - x0, y1 - y0) / 2) * 0.55,
                          (min(x1 - x0, y1 - y0) / 2) * 0.10)
            x = fx + r * math.cos(ang)
            y = fy + r * math.sin(ang)
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            continue
        op = rng.uniform(0.18, 0.85)
        rr = p.radius * rng.uniform(0.4, 1.4)
        out.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rr:.2f}" '
            f'fill="{PALETTE["stroke"]}" opacity="{op:.2f}"/>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_permutation(p: Permutation) -> str:
    rng = _rng(p.seed)
    n = max(2, p.n)
    # equally spaced y positions
    ys = [p.top_y + i * (p.bottom_y - p.top_y) / (n - 1) for i in range(n)]
    # shuffle mapping (no fixed points if possible)
    order = list(range(n))
    rng.shuffle(order)
    out = [f'<g id="{p.id}">']
    # connecting lines first (so dots overlay)
    for i, j in enumerate(order):
        out.append(
            f'<line x1="{p.left_x:.1f}" y1="{ys[i]:.1f}" '
            f'x2="{p.right_x:.1f}" y2="{ys[j]:.1f}" '
            f'stroke="{PALETTE["stroke"]}" stroke-width="0.8" '
            f'opacity="0.55"/>'
        )
    # left column dots (input)
    for y in ys:
        out.append(
            f'<circle cx="{p.left_x:.1f}" cy="{y:.1f}" r="3.5" '
            f'fill="{PALETTE["stroke"]}" opacity="0.85"/>'
        )
    # right column dots (output) -- chromatic on these
    fl = ' filter="url(#chromatic)"' if p.chromatic else ""
    for y in ys:
        out.append(
            f'<circle cx="{p.right_x:.1f}" cy="{y:.1f}" r="3.5" '
            f'fill="{PALETTE["focal"]}" opacity="0.95"{fl}/>'
        )
    # column hairlines
    for x in (p.left_x, p.right_x):
        out.append(
            f'<line x1="{x:.1f}" y1="{p.top_y - 30:.1f}" '
            f'x2="{x:.1f}" y2="{p.bottom_y + 30:.1f}" '
            f'stroke="{PALETTE["stroke_faint"]}" stroke-width="1" '
            f'stroke-dasharray="1 5" opacity="0.6"/>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_dual_pane(p: DualPane) -> str:
    """Two side-by-side blocks (one ordered, one scrambled) + arrow between."""
    out = [f'<g id="{p.id}">']
    rng = _rng(p.seed)
    canvas_w, canvas_h = 1200, 600
    pane_w = (canvas_w - 200) / 2
    pad = 40
    arrow_gap = 80
    left_cx = pad + pane_w / 2
    right_cx = canvas_w - pad - pane_w / 2

    out.append(_pane_block(p.left_kind, left_cx, canvas_h / 2,
                            p.cols, p.rows, p.spacing,
                            rng=_rng(p.seed)))
    out.append(_pane_block(p.right_kind, right_cx, canvas_h / 2,
                            p.cols, p.rows, p.spacing,
                            rng=_rng(p.seed + 1)))

    if p.arrow:
        ay = canvas_h / 2
        ax0 = left_cx + (p.cols - 1) * p.spacing / 2 + 20
        ax1 = right_cx - (p.cols - 1) * p.spacing / 2 - 20
        # shaft
        out.append(
            f'<line x1="{ax0:.1f}" y1="{ay}" x2="{ax1 - 18:.1f}" y2="{ay}" '
            f'stroke="{PALETTE["stroke"]}" stroke-width="1.4" filter="url(#chromatic)"/>'
        )
        # head
        out.append(
            f'<path d="M {ax1:.1f} {ay} L {ax1 - 18:.1f} {ay - 9} '
            f'L {ax1 - 18:.1f} {ay + 9} Z" fill="{PALETTE["focal"]}" '
            f'filter="url(#chromatic)"/>'
        )
    out.append("</g>")
    return "\n".join(out)


def _pane_block(kind: str, cx: float, cy: float, cols: int, rows: int,
                spacing: float, rng) -> str:
    """Render one pane's content."""
    half_w = (cols - 1) * spacing / 2
    half_h = (rows - 1) * spacing / 2
    x0 = cx - half_w
    y0 = cy - half_h
    out = []

    if kind == "lattice_ordered":
        # neat grid
        for r in range(rows):
            for c in range(cols):
                x = x0 + c * spacing
                y = y0 + r * spacing
                out.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" '
                    f'fill="{PALETTE["stroke"]}" opacity="0.85"/>'
                )
    elif kind == "lattice_scrambled":
        # same N nodes, shuffled positions within bbox
        slots = [(x0 + c*spacing, y0 + r*spacing) for r in range(rows) for c in range(cols)]
        rng.shuffle(slots)
        for x, y in slots:
            jx = rng.uniform(-spacing*0.35, spacing*0.35)
            jy = rng.uniform(-spacing*0.35, spacing*0.35)
            out.append(
                f'<circle cx="{x+jx:.1f}" cy="{y+jy:.1f}" r="2.2" '
                f'fill="{PALETTE["stroke"]}" opacity="0.85"/>'
            )
    elif kind == "particles_uniform":
        for _ in range(cols * rows):
            x = rng.uniform(x0 - 10, x0 + (cols-1)*spacing + 10)
            y = rng.uniform(y0 - 10, y0 + (rows-1)*spacing + 10)
            out.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.8" '
                f'fill="{PALETTE["stroke"]}" opacity="0.7"/>'
            )
    elif kind == "particles_clustered":
        for _ in range(cols * rows):
            x = rng.gauss(cx, spacing*1.2)
            y = rng.gauss(cy, spacing*1.0)
            out.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.8" '
                f'fill="{PALETTE["stroke"]}" opacity="0.85"/>'
            )
    elif kind == "constellation":
        n = cols * rows
        pts = [(rng.uniform(x0, x0 + (cols-1)*spacing),
                rng.uniform(y0, y0 + (rows-1)*spacing)) for _ in range(n)]
        # a few edges
        for _ in range(n // 3):
            i, j = rng.randrange(n), rng.randrange(n)
            ax, ay = pts[i]; bx, by = pts[j]
            out.append(
                f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" y2="{by:.1f}" '
                f'stroke="{PALETTE["stroke"]}" stroke-width="0.6" opacity="0.4"/>'
            )
        for x, y in pts:
            out.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" '
                f'fill="{PALETTE["focal"]}" opacity="0.9"/>'
            )
    return "\n".join(out)


def emit_scramble(p: ScrambleBlock) -> str:
    rng = _rng(p.seed)
    half_w = (p.cols - 1) * p.spacing / 2
    half_h = (p.rows - 1) * p.spacing / 2
    x0 = p.cx - half_w
    y0 = p.cy - half_h
    out = [f'<g id="{p.id}">']
    # very faint slot grid (so the chaos reads against order)
    for r in range(p.rows):
        for c in range(p.cols):
            x = x0 + c * p.spacing
            y = y0 + r * p.spacing
            out.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="0.6" '
                f'fill="{PALETTE["stroke_faint"]}" opacity="0.7"/>'
            )
    # chaotic nodes
    max_off = p.spacing * p.chaos * 0.55
    for r in range(p.rows):
        for c in range(p.cols):
            x = x0 + c * p.spacing + rng.uniform(-max_off, max_off)
            y = y0 + r * p.spacing + rng.uniform(-max_off, max_off)
            chrom = ' filter="url(#chromatic)"' if (rng.random() < 0.07) else ""
            out.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{p.node_r:.1f}" '
                f'fill="{PALETTE["stroke"]}" opacity="0.85"{chrom}/>'
            )
    out.append("</g>")
    return "\n".join(out)


# --- Swiss typography + construction primitives ------------------------------

def emit_text(p: Text) -> str:
    spec = TYPE_SCALE[p.slot]
    text = p.text
    if spec["case"] == "upper":
        text = text.upper()
    elif spec["case"] == "lower":
        text = text.lower()
    x = GRID.x(p.col)
    y = GRID.snap_baseline(GRID.y(p.row))
    anchor = {"left": "start", "center": "middle", "right": "end"}[p.align]
    weight = spec["weight"]
    fill_op = p.opacity if p.slot in ("eyebrow", "annotation", "value") else 1.0
    size = spec["size"] * (p.size_mult if p.size_mult > 0 else 1.0)
    return (
        f'<text id="{p.id}" x="{x:.1f}" y="{y:.1f}" '
        f'font-family="{FONT_FAMILY}" font-size="{size}" '
        f'font-weight="{weight}" letter-spacing="{spec["tracking"]}" '
        f'text-anchor="{anchor}" fill="{PALETTE["stroke"]}" '
        f'opacity="{fill_op}">{_xml_escape(text)}</text>'
    )


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def emit_axis(p: Axis) -> str:
    x1, y1 = GRID.x(p.from_col), GRID.y(p.from_row)
    x2, y2 = GRID.x(p.to_col),   GRID.y(p.to_row)
    out = [
        f'<g id="{p.id}">',
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{PALETTE["stroke_dim"]}" stroke-width="{p.weight}" '
        f'opacity="{p.opacity}"/>',
    ]
    if p.ticks > 0:
        # determine orientation: horizontal axis -> vertical ticks, vice-versa
        horiz = abs(y2 - y1) < abs(x2 - x1)
        for i in range(p.ticks + 1):
            t = i / p.ticks if p.ticks else 0
            tx = x1 + (x2 - x1) * t
            ty = y1 + (y2 - y1) * t
            if horiz:
                out.append(
                    f'<line x1="{tx:.1f}" y1="{ty - 4:.1f}" '
                    f'x2="{tx:.1f}" y2="{ty + 4:.1f}" '
                    f'stroke="{PALETTE["stroke_dim"]}" stroke-width="{p.weight}" '
                    f'opacity="{p.opacity}"/>'
                )
            else:
                out.append(
                    f'<line x1="{tx - 4:.1f}" y1="{ty:.1f}" '
                    f'x2="{tx + 4:.1f}" y2="{ty:.1f}" '
                    f'stroke="{PALETTE["stroke_dim"]}" stroke-width="{p.weight}" '
                    f'opacity="{p.opacity}"/>'
                )
    out.append("</g>")
    return "\n".join(out)


def emit_tick(p: Tick) -> str:
    x = GRID.x(p.col)
    y = GRID.y(p.row)
    half = p.length * GRID.module_w / 2
    out = [f'<g id="{p.id}">']
    if p.orientation == "v":
        out.append(
            f'<line x1="{x:.1f}" y1="{y - half:.1f}" '
            f'x2="{x:.1f}" y2="{y + half:.1f}" '
            f'stroke="{PALETTE["stroke"]}" stroke-width="0.8"/>'
        )
    else:
        out.append(
            f'<line x1="{x - half:.1f}" y1="{y:.1f}" '
            f'x2="{x + half:.1f}" y2="{y:.1f}" '
            f'stroke="{PALETTE["stroke"]}" stroke-width="0.8"/>'
        )
    if p.label:
        spec = TYPE_SCALE["annotation"]
        if p.label_side == "below":
            lx, ly = x, y + half + 14
            anchor = "middle"
        elif p.label_side == "above":
            lx, ly = x, y - half - 6
            anchor = "middle"
        elif p.label_side == "right":
            lx, ly = x + half + 8, y + 4
            anchor = "start"
        else:
            lx, ly = x - half - 8, y + 4
            anchor = "end"
        out.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="{spec["size"]}" '
            f'letter-spacing="{spec["tracking"]}" text-anchor="{anchor}" '
            f'fill="{PALETTE["stroke"]}" opacity="0.85">'
            f'{_xml_escape(p.label.upper())}</text>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_bracket(p: Bracket) -> str:
    x1, y1, x2, y2 = GRID.bbox(p.from_col, p.from_row, p.to_col, p.to_row)
    d = p.depth * GRID.module_w
    out = [f'<g id="{p.id}">']
    if p.side == "bottom":
        path = (f'M {x1:.1f} {y2 - d:.1f} L {x1:.1f} {y2:.1f} '
                f'L {x2:.1f} {y2:.1f} L {x2:.1f} {y2 - d:.1f}')
        cap_x, cap_y = (x1 + x2) / 2, y2 + 4
        anchor = "middle"
    elif p.side == "top":
        path = (f'M {x1:.1f} {y1 + d:.1f} L {x1:.1f} {y1:.1f} '
                f'L {x2:.1f} {y1:.1f} L {x2:.1f} {y1 + d:.1f}')
        cap_x, cap_y = (x1 + x2) / 2, y1 - 8
        anchor = "middle"
    elif p.side == "left":
        path = (f'M {x1 + d:.1f} {y1:.1f} L {x1:.1f} {y1:.1f} '
                f'L {x1:.1f} {y2:.1f} L {x1 + d:.1f} {y2:.1f}')
        cap_x, cap_y = x1 - 6, (y1 + y2) / 2
        anchor = "end"
    else:  # right
        path = (f'M {x2 - d:.1f} {y1:.1f} L {x2:.1f} {y1:.1f} '
                f'L {x2:.1f} {y2:.1f} L {x2 - d:.1f} {y2:.1f}')
        cap_x, cap_y = x2 + 6, (y1 + y2) / 2
        anchor = "start"
    out.append(
        f'<path d="{path}" fill="none" stroke="{PALETTE["stroke_dim"]}" '
        f'stroke-width="0.8" opacity="0.8"/>'
    )
    if p.label:
        spec = TYPE_SCALE["annotation"]
        out.append(
            f'<text x="{cap_x:.1f}" y="{cap_y:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="{spec["size"]}" '
            f'letter-spacing="{spec["tracking"]}" text-anchor="{anchor}" '
            f'fill="{PALETTE["stroke"]}" opacity="0.85">'
            f'{_xml_escape(p.label.upper())}</text>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_measure(p: Measure) -> str:
    x1, y1 = GRID.x(p.from_col), GRID.y(p.from_row)
    x2, y2 = GRID.x(p.to_col),   GRID.y(p.to_row)
    out = [f'<g id="{p.id}">']
    # main dimension line
    out.append(
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{PALETTE["stroke_dim"]}" stroke-width="0.8" '
        f'opacity="0.85"/>'
    )
    # end caps (perpendicular short marks)
    horiz = abs(y2 - y1) < abs(x2 - x1)
    cap = 5
    if horiz:
        for cx in (x1, x2):
            out.append(
                f'<line x1="{cx:.1f}" y1="{y1 - cap:.1f}" '
                f'x2="{cx:.1f}" y2="{y1 + cap:.1f}" '
                f'stroke="{PALETTE["stroke_dim"]}" stroke-width="0.8"/>'
            )
    else:
        for cy in (y1, y2):
            out.append(
                f'<line x1="{x1 - cap:.1f}" y1="{cy:.1f}" '
                f'x2="{x1 + cap:.1f}" y2="{cy:.1f}" '
                f'stroke="{PALETTE["stroke_dim"]}" stroke-width="0.8"/>'
            )
    # center label, with a small backed rectangle to break the line
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    spec = TYPE_SCALE["value"]
    label_w = max(40, len(p.label) * 8)
    out.append(
        f'<rect x="{mx - label_w/2:.1f}" y="{my - 9:.1f}" '
        f'width="{label_w}" height="18" fill="{PALETTE["bg"]}"/>'
    )
    out.append(
        f'<text x="{mx:.1f}" y="{my + 4:.1f}" '
        f'font-family="{FONT_FAMILY}" font-size="{spec["size"]}" '
        f'font-weight="{spec["weight"]}" '
        f'letter-spacing="{spec["tracking"]}" text-anchor="middle" '
        f'fill="{PALETTE["stroke"]}" opacity="1.0">'
        f'{_xml_escape(p.label.upper())}</text>'
    )
    out.append("</g>")
    return "\n".join(out)


# --- Sankey ------------------------------------------------------------------

def emit_sankey(p: Sankey) -> str:
    """Two-column flow diagram. Node heights and ribbon widths scale with
    total flow through them. Bezier ribbons connect left -> right."""
    n_left = len(p.left_labels)
    n_right = len(p.right_labels)

    # totals per node
    out_per_left = [0.0] * n_left
    in_per_right = [0.0] * n_right
    for src, dst, val in p.flows:
        if 0 <= src < n_left and 0 <= dst < n_right:
            out_per_left[src] += val
            in_per_right[dst] += val
    total_left = sum(out_per_left) or 1.0
    total_right = sum(in_per_right) or 1.0

    # geometry
    x_left = GRID.x(p.left_col)
    x_right = GRID.x(p.right_col)
    y_top = GRID.y(p.top_row)
    y_bot = GRID.y(p.bottom_row)
    span_y = y_bot - y_top
    bar_w = p.node_w * GRID.module_w
    gap_px = 6  # vertical gap between stacked nodes

    # node y ranges (top, bot) per side
    def _stack(totals: list[float], grand_total: float
               ) -> list[tuple[float, float]]:
        ranges: list[tuple[float, float]] = []
        avail = span_y - gap_px * (len(totals) - 1)
        y = y_top
        for v in totals:
            h = avail * (v / grand_total) if grand_total else 0
            ranges.append((y, y + h))
            y += h + gap_px
        return ranges

    left_ranges = _stack(out_per_left, total_left)
    right_ranges = _stack(in_per_right, total_right)

    out = [f'<g id="{p.id}">']

    # ribbons (drawn first so nodes overlay)
    # cursors: how much of each node we've consumed from the top
    left_cursor = [0.0] * n_left
    right_cursor = [0.0] * n_right
    has_focal = (0 <= p.focal_flow_idx < len(p.flows))
    for fi, (src, dst, val) in enumerate(p.flows):
        if not (0 <= src < n_left and 0 <= dst < n_right):
            continue
        # vertical extent of this ribbon at the left and right ends
        l_top, l_bot = left_ranges[src]
        r_top, r_bot = right_ranges[dst]
        l_h = (l_bot - l_top) * (val / out_per_left[src]) if out_per_left[src] else 0
        r_h = (r_bot - r_top) * (val / in_per_right[dst]) if in_per_right[dst] else 0
        ly0 = l_top + left_cursor[src]
        ly1 = ly0 + l_h
        ry0 = r_top + right_cursor[dst]
        ry1 = ry0 + r_h
        left_cursor[src] += l_h
        right_cursor[dst] += r_h
        # bezier ribbon as a closed path
        cx1 = (x_left + x_right) / 2
        d = (
            f"M {x_left + bar_w:.1f} {ly0:.1f} "
            f"C {cx1:.1f} {ly0:.1f} {cx1:.1f} {ry0:.1f} {x_right:.1f} {ry0:.1f} "
            f"L {x_right:.1f} {ry1:.1f} "
            f"C {cx1:.1f} {ry1:.1f} {cx1:.1f} {ly1:.1f} {x_left + bar_w:.1f} {ly1:.1f} "
            f"Z"
        )
        # When a focal flow is set, desaturate non-focal ribbons and amp up
        # the focal one with chromatic accent — "where does X actually go?"
        is_focal = has_focal and fi == p.focal_flow_idx
        if has_focal and not is_focal:
            fop = 0.05
            sop = 0.10
            extra = ""
        elif is_focal:
            fop = 0.55
            sop = 0.95
            extra = ' filter="url(#chromatic)"'
        else:
            fop = 0.18
            sop = 0.40
            extra = ""
        if p.animate:
            begin = f"{fi * 0.18:.2f}s"
            anim = (
                f'<animate attributeName="fill-opacity" '
                f'from="0" to="{fop}" begin="{begin}" '
                f'dur="1.4s" fill="freeze"/>'
            )
            initial_fop = "0"
        else:
            anim = ""
            initial_fop = f"{fop}"
        out.append(
            f'<path d="{d}" fill="{PALETTE["stroke"]}" '
            f'fill-opacity="{initial_fop}" stroke="{PALETTE["stroke_dim"]}" '
            f'stroke-width="{1.0 if is_focal else 0.4}" '
            f'stroke-opacity="{sop}"{extra}>{anim}</path>'
        )

    # nodes (vertical bars). If a node spans most of the figure (a single
    # source/sink), pin its label at the bar's TOP instead of its midline
    # so it doesn't visually sit inside the ribbon band.
    span_threshold = 0.55 * span_y
    for i, (y0_, y1_) in enumerate(left_ranges):
        out.append(
            f'<rect x="{x_left:.1f}" y="{y0_:.1f}" '
            f'width="{bar_w:.1f}" height="{(y1_ - y0_):.1f}" '
            f'fill="{PALETTE["focal"]}" opacity="0.95"/>'
        )
        spec = TYPE_SCALE["annotation"]
        bar_h = y1_ - y0_
        ly = (y0_ - 8) if bar_h > span_threshold else ((y0_ + y1_) / 2 + 4)
        out.append(
            f'<text x="{x_left - 8:.1f}" y="{ly:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="{spec["size"]}" '
            f'letter-spacing="{spec["tracking"]}" text-anchor="end" '
            f'fill="{PALETTE["stroke"]}" opacity="0.85">'
            f'{_xml_escape(p.left_labels[i].upper())}</text>'
        )
    for j, (y0_, y1_) in enumerate(right_ranges):
        out.append(
            f'<rect x="{x_right:.1f}" y="{y0_:.1f}" '
            f'width="{bar_w:.1f}" height="{(y1_ - y0_):.1f}" '
            f'fill="{PALETTE["focal"]}" opacity="0.95" '
            f'filter="url(#chromatic)"/>'
        )
        spec = TYPE_SCALE["annotation"]
        bar_h = y1_ - y0_
        ry = (y0_ - 8) if bar_h > span_threshold else ((y0_ + y1_) / 2 + 4)
        out.append(
            f'<text x="{x_right + bar_w + 8:.1f}" y="{ry:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="{spec["size"]}" '
            f'letter-spacing="{spec["tracking"]}" text-anchor="start" '
            f'fill="{PALETTE["stroke"]}" opacity="0.85">'
            f'{_xml_escape(p.right_labels[j].upper())}</text>'
        )

    out.append("</g>")
    return "\n".join(out)


# --- Tree --------------------------------------------------------------------

def emit_tree(p: Tree) -> str:
    """Top-down hierarchical tree. Walker-style: leaves equally spaced,
    parents centered over their children. Edges are L-shaped polylines."""
    n = len(p.nodes)
    if n == 0:
        return ""

    # build children map
    children: list[list[int]] = [[] for _ in range(n)]
    root = -1
    for i, par in enumerate(p.parents):
        if par == -1:
            root = i
        elif 0 <= par < n:
            children[par].append(i)
    if root == -1:
        root = 0  # fallback

    # depth per node (BFS)
    depth = [0] * n
    stack = [root]
    while stack:
        nxt: list[int] = []
        for node in stack:
            for c in children[node]:
                depth[c] = depth[node] + 1
                nxt.append(c)
        stack = nxt
    max_depth = max(depth) if depth else 0

    # x position: assign leaves first, then center parents over children
    leaf_idx = [0]
    x_unit: list[float] = [0.0] * n
    def _walk(node: int) -> None:
        if not children[node]:
            x_unit[node] = float(leaf_idx[0])
            leaf_idx[0] += 1
            return
        for c in children[node]:
            _walk(c)
        kids = children[node]
        x_unit[node] = (x_unit[kids[0]] + x_unit[kids[-1]]) / 2
    _walk(root)
    n_leaves = leaf_idx[0] or 1

    # map to grid pixels
    cx0, cx1 = p.bbox_cols
    rx0, rx1 = p.bbox_rows
    x_min = GRID.x(cx0)
    x_max = GRID.x(cx1)
    y_min = GRID.y(rx0)
    y_max = GRID.y(rx1)
    def px(node: int) -> tuple[float, float]:
        # x: 0..n_leaves-1 -> x_min..x_max
        if n_leaves > 1:
            X = x_min + x_unit[node] / (n_leaves - 1) * (x_max - x_min)
        else:
            X = (x_min + x_max) / 2
        # y: depth 0..max_depth -> y_min..y_max
        if max_depth > 0:
            Y = y_min + depth[node] / max_depth * (y_max - y_min)
        else:
            Y = y_min
        return X, Y

    out = [f'<g id="{p.id}">']
    # edges: L-shape (parent -> midpoint -> child)
    for i, par in enumerate(p.parents):
        if par == -1 or par >= n:
            continue
        ex, ey = px(par)
        cx_, cy_ = px(i)
        midy = (ey + cy_) / 2
        path = (
            f"M {ex:.1f} {ey:.1f} L {ex:.1f} {midy:.1f} "
            f"L {cx_:.1f} {midy:.1f} L {cx_:.1f} {cy_:.1f}"
        )
        out.append(
            f'<path d="{path}" fill="none" stroke="{PALETTE["stroke_dim"]}" '
            f'stroke-width="0.7" opacity="0.7"/>'
        )

    # nodes
    for i in range(n):
        nx_, ny_ = px(i)
        chrom = i == root
        fl = ' filter="url(#chromatic)"' if chrom else ""
        r = 5 if chrom else 3.5
        out.append(
            f'<circle cx="{nx_:.1f}" cy="{ny_:.1f}" r="{r}" '
            f'fill="{PALETTE["focal"]}" opacity="0.95"{fl}/>'
        )
        if chrom:
            out.append(
                f'<circle cx="{nx_:.1f}" cy="{ny_:.1f}" r="18" '
                f'fill="url(#halo)" opacity="0.55"/>'
            )
        # label below the node
        spec = TYPE_SCALE["annotation"]
        out.append(
            f'<text x="{nx_:.1f}" y="{ny_ + 18:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="{spec["size"]}" '
            f'letter-spacing="{spec["tracking"]}" text-anchor="middle" '
            f'fill="{PALETTE["stroke"]}" opacity="0.85">'
            f'{_xml_escape(p.nodes[i].upper())}</text>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_callout(p: Callout) -> str:
    """Architectural-style leader line: vertical from anchor, then horizontal
    to the label block. The label sits beyond the elbow, two stacked lines."""
    ax = GRID.x(p.anchor_col)
    ay = GRID.y(p.anchor_row)
    ex = GRID.x(p.label_col)        # elbow x = label x
    ey = GRID.y(p.label_row)        # elbow y = anchor row aligned (run vertical first)
    # The bend lands at (ex, ay) — vertical from anchor to elbow row was confusing,
    # so we elbow at (ex, ay) instead: vertical leg is ANCHOR-X aligned to LABEL-Y? No.
    # Better: vertical leg from (ax, ay) -> (ax, ey), horizontal leg (ax, ey) -> (ex, ey).
    bend_x, bend_y = ax, ey
    out = [f'<g id="{p.id}">']
    if p.tip:
        # tiny ringed dot at anchor — gives the leader a "foot"
        out.append(
            f'<circle cx="{ax:.1f}" cy="{ay:.1f}" r="2.4" fill="none" '
            f'stroke="{PALETTE["stroke"]}" stroke-width="0.8" opacity="0.95"/>'
        )
        out.append(
            f'<circle cx="{ax:.1f}" cy="{ay:.1f}" r="1.0" fill="{PALETTE["stroke"]}" '
            f'opacity="0.95"/>'
        )
    # leader polyline: anchor -> (ax, ey) -> (ex, ey)
    out.append(
        f'<polyline points="{ax:.1f},{ay:.1f} {bend_x:.1f},{bend_y:.1f} '
        f'{ex:.1f},{ey:.1f}" fill="none" stroke="{PALETTE["stroke_dim"]}" '
        f'stroke-width="0.8" opacity="0.85"/>'
    )
    # tiny terminal tick at the label end (perpendicular to horizontal leg)
    out.append(
        f'<line x1="{ex:.1f}" y1="{ey - 3:.1f}" x2="{ex:.1f}" y2="{ey + 3:.1f}" '
        f'stroke="{PALETTE["stroke_dim"]}" stroke-width="0.8" opacity="0.85"/>'
    )
    # label block: 2 stacked text lines
    text_anchor = "start" if p.side == "right" else "end"
    text_dx = 8 if p.side == "right" else -8
    if p.label:
        out.append(
            f'<text x="{ex + text_dx:.1f}" y="{ey - 3:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="9" letter-spacing="2" '
            f'text-anchor="{text_anchor}" fill="{PALETTE["stroke_dim"]}" '
            f'text-transform="uppercase">{_xml_escape(p.label.upper())}</text>'
        )
    if p.value:
        out.append(
            f'<text x="{ex + text_dx:.1f}" y="{ey + 12:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="13" letter-spacing="0.5" '
            f'text-anchor="{text_anchor}" fill="{PALETTE["stroke"]}" '
            f'font-weight="500">{_xml_escape(p.value)}</text>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_corner_legend(p: CornerLegend) -> str:
    """Distill-style off-canvas figure identity. Pinned to one of 4 corners.
    Stack: small uppercase title -> 1-line subtitle -> mini scale rule ->
    'fig. NN' footer. Hairline separators above title and below subtitle."""
    # Anchor positions on the 12x6 grid
    margin_col = 0.5
    margin_row = 0.5
    if p.corner == "tl":
        x, y = GRID.x(margin_col), GRID.y(margin_row)
    elif p.corner == "tr":
        x, y = GRID.x(12 - margin_col - 2.5), GRID.y(margin_row)
    elif p.corner == "bl":
        x, y = GRID.x(margin_col), GRID.y(6 - margin_row - 1.0)
    else:  # br
        x, y = GRID.x(12 - margin_col - 2.5), GRID.y(6 - margin_row - 1.0)

    block_w = GRID.module_w * 2.5    # ~250px on wide preset
    out = [f'<g id="{p.id}">']
    # top hairline rule
    out.append(
        f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x + block_w:.1f}" y2="{y:.1f}" '
        f'stroke="{PALETTE["stroke_dim"]}" stroke-width="0.8" opacity="0.7"/>'
    )
    # title
    if p.title:
        out.append(
            f'<text x="{x:.1f}" y="{y + 14:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="10" letter-spacing="3" '
            f'fill="{PALETTE["stroke"]}" text-transform="uppercase" '
            f'font-weight="500">{_xml_escape(p.title.upper())}</text>'
        )
    # subtitle
    if p.subtitle:
        out.append(
            f'<text x="{x:.1f}" y="{y + 30:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="10" letter-spacing="1" '
            f'fill="{PALETTE["stroke_dim"]}">{_xml_escape(p.subtitle)}</text>'
        )
    # mini scale glyph: a short hairline rule with two end ticks + center label
    if p.scale_label:
        scale_y = y + 46
        scale_w = block_w * 0.45
        out.append(
            f'<line x1="{x:.1f}" y1="{scale_y:.1f}" '
            f'x2="{x + scale_w:.1f}" y2="{scale_y:.1f}" '
            f'stroke="{PALETTE["stroke"]}" stroke-width="0.8"/>'
        )
        out.append(
            f'<line x1="{x:.1f}" y1="{scale_y - 3:.1f}" '
            f'x2="{x:.1f}" y2="{scale_y + 3:.1f}" '
            f'stroke="{PALETTE["stroke"]}" stroke-width="0.8"/>'
        )
        out.append(
            f'<line x1="{x + scale_w:.1f}" y1="{scale_y - 3:.1f}" '
            f'x2="{x + scale_w:.1f}" y2="{scale_y + 3:.1f}" '
            f'stroke="{PALETTE["stroke"]}" stroke-width="0.8"/>'
        )
        out.append(
            f'<text x="{x + scale_w + 8:.1f}" y="{scale_y + 3:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="9" letter-spacing="2" '
            f'fill="{PALETTE["stroke_dim"]}" text-transform="uppercase">'
            f'{_xml_escape(p.scale_label.upper())}</text>'
        )
    # fig. NN footer right-aligned
    if p.fig:
        out.append(
            f'<text x="{x + block_w:.1f}" y="{y + 64:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="9" letter-spacing="2" '
            f'text-anchor="end" fill="{PALETTE["stroke_dim"]}" '
            f'text-transform="uppercase">{_xml_escape(p.fig.upper())}</text>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_tick_rings(p: TickRings) -> str:
    """Concentric instrument-panel rings + radial ticks at 12 angles.
    Stacks behind a focal motif to add "dial" texture."""
    out = [f'<g id="{p.id}">']
    # rings
    for i in range(p.rings):
        r = p.base_r + i * p.spacing
        op = 0.55 if i == p.rings - 1 else 0.30
        sw = 0.8 if i == p.rings - 1 else 0.5
        out.append(
            f'<circle cx="{p.cx}" cy="{p.cy}" r="{r:.1f}" fill="none" '
            f'stroke="{PALETTE["stroke_dim"]}" stroke-width="{sw}" '
            f'opacity="{op:.2f}"/>'
        )
    # outer-ring radial ticks every 360/n degrees
    r_outer = p.base_r + (p.rings - 1) * p.spacing
    tick_inset = 6
    tick_len = 10
    for k in range(p.n_ticks):
        ang = -math.pi / 2 + 2 * math.pi * k / p.n_ticks
        x1 = p.cx + (r_outer - tick_inset) * math.cos(ang)
        y1 = p.cy + (r_outer - tick_inset) * math.sin(ang)
        x2 = p.cx + (r_outer - tick_inset + tick_len) * math.cos(ang)
        y2 = p.cy + (r_outer - tick_inset + tick_len) * math.sin(ang)
        major = (k % 3 == 0)
        out.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" '
            f'x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{PALETTE["stroke" if major else "stroke_dim"]}" '
            f'stroke-width="{0.9 if major else 0.6}" '
            f'opacity="{0.85 if major else 0.55}"/>'
        )
    # scale label at 1 o'clock
    if p.scale_label:
        ang = -math.pi / 2 + math.pi / 6   # 1 o'clock-ish
        lx = p.cx + (r_outer + 18) * math.cos(ang)
        ly = p.cy + (r_outer + 18) * math.sin(ang) + 4
        out.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="9" letter-spacing="2" '
            f'fill="{PALETTE["stroke_dim"]}" text-transform="uppercase">'
            f'{_xml_escape(p.scale_label.upper())}</text>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_dimension_stack(p: DimensionStack) -> str:
    """Spec-sheet column: title + n rows of (label . . . . value)."""
    x0 = GRID.x(p.col)
    y0 = GRID.y(p.row)
    w_px = p.width_cols * GRID.module_w
    line_h = 18
    out = [f'<g id="{p.id}">']
    if p.title:
        out.append(
            f'<line x1="{x0:.1f}" y1="{y0 - 8:.1f}" '
            f'x2="{x0 + w_px:.1f}" y2="{y0 - 8:.1f}" '
            f'stroke="{PALETTE["stroke_dim"]}" stroke-width="0.8"/>'
        )
        out.append(
            f'<text x="{x0:.1f}" y="{y0 + 6:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="10" letter-spacing="3" '
            f'fill="{PALETTE["stroke"]}" text-transform="uppercase" '
            f'font-weight="500">{_xml_escape(p.title.upper())}</text>'
        )
        y_cursor = y0 + 22
    else:
        y_cursor = y0
    for label, value in p.items:
        # label left
        out.append(
            f'<text x="{x0:.1f}" y="{y_cursor + 4:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="10" letter-spacing="1.5" '
            f'fill="{PALETTE["stroke_dim"]}" text-transform="uppercase">'
            f'{_xml_escape(label.upper())}</text>'
        )
        # dotted leader between
        out.append(
            f'<line x1="{x0 + 70:.1f}" y1="{y_cursor:.1f}" '
            f'x2="{x0 + w_px - 6:.1f}" y2="{y_cursor:.1f}" '
            f'stroke="{PALETTE["stroke_dim"]}" stroke-width="0.7" '
            f'stroke-dasharray="1 4" opacity="0.7"/>'
        )
        # value right-aligned
        out.append(
            f'<text x="{x0 + w_px:.1f}" y="{y_cursor + 4:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="11" letter-spacing="0.5" '
            f'fill="{PALETTE["stroke"]}" text-anchor="end" font-weight="500">'
            f'{_xml_escape(value)}</text>'
        )
        y_cursor += line_h
    out.append("</g>")
    return "\n".join(out)


def emit_proportional_dot(p: ProportionalDot) -> str:
    """Dot with radius=value*max_r inside a hairline scale ring at max_r."""
    v = max(0.0, min(1.0, p.value))
    r_inner = max(1.5, v * p.max_r)
    out = [f'<g id="{p.id}">']
    # outer scale ring
    out.append(
        f'<circle cx="{p.cx}" cy="{p.cy}" r="{p.max_r:.1f}" fill="none" '
        f'stroke="{PALETTE["stroke_dim"]}" stroke-width="0.6" '
        f'stroke-dasharray="1 3" opacity="0.6"/>'
    )
    # filled inner dot
    fl = ' filter="url(#chromatic)"' if p.chromatic else ""
    out.append(
        f'<circle cx="{p.cx}" cy="{p.cy}" r="{r_inner:.1f}" '
        f'fill="{PALETTE["focal"]}"{fl} opacity="0.95"/>'
    )
    if p.label:
        out.append(
            f'<text x="{p.cx:.1f}" y="{p.cy + p.max_r + 14:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="9" letter-spacing="2" '
            f'text-anchor="middle" fill="{PALETTE["stroke_dim"]}" '
            f'text-transform="uppercase">{_xml_escape(p.label.upper())}</text>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_sparkline(p: Sparkline) -> str:
    """Mini polyline glyph: 0..1 values → polyline in a bbox."""
    if not p.values:
        return ""
    n = len(p.values)
    out = [f'<g id="{p.id}">']
    # baseline
    bottom_y = p.y + p.height
    if p.style == "bar":
        bar_w = (p.width / n) * 0.7
        gap = (p.width / n) * 0.3
        for i, v in enumerate(p.values):
            x = p.x + i * (bar_w + gap)
            h = max(1.0, v * p.height)
            y = bottom_y - h
            out.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
                f'height="{h:.1f}" fill="{PALETTE["stroke"]}" opacity="0.85"/>'
            )
    else:
        pts = []
        for i, v in enumerate(p.values):
            x = p.x + (i / max(1, n - 1)) * p.width
            y = bottom_y - max(0.0, min(1.0, v)) * p.height
            pts.append(f"{x:.1f},{y:.1f}")
        if p.style == "area":
            poly = (f"{p.x:.1f},{bottom_y:.1f} " + " ".join(pts) +
                    f" {p.x + p.width:.1f},{bottom_y:.1f}")
            out.append(
                f'<polygon points="{poly}" fill="{PALETTE["stroke"]}" '
                f'opacity="0.18"/>'
            )
        fl = ' filter="url(#chromatic)"' if p.chromatic else ""
        out.append(
            f'<polyline points="{" ".join(pts)}" fill="none" '
            f'stroke="{PALETTE["stroke"]}" stroke-width="1.0" '
            f'opacity="0.95"{fl}/>'
        )
        # baseline tick at left + right ends
        out.append(
            f'<line x1="{p.x:.1f}" y1="{bottom_y:.1f}" '
            f'x2="{p.x:.1f}" y2="{bottom_y - 3:.1f}" '
            f'stroke="{PALETTE["stroke_dim"]}" stroke-width="0.6"/>'
        )
        out.append(
            f'<line x1="{p.x + p.width:.1f}" y1="{bottom_y:.1f}" '
            f'x2="{p.x + p.width:.1f}" y2="{bottom_y - 3:.1f}" '
            f'stroke="{PALETTE["stroke_dim"]}" stroke-width="0.6"/>'
        )
        if p.terminal_dot:
            last_v = max(0.0, min(1.0, p.values[-1]))
            tx = p.x + p.width
            ty = bottom_y - last_v * p.height
            out.append(
                f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="2.2" '
                f'fill="{PALETTE["focal"]}" filter="url(#chromatic)"/>'
            )
    out.append("</g>")
    return "\n".join(out)


def emit_marey(p: MareyGrid) -> str:
    """Marey space-time chart: orthogonal grid + station rows + time cols
    + n diagonal trajectories."""
    if not p.stations or not p.trajectories:
        return ""
    x0 = GRID.x(p.bbox_cols[0])
    x1 = GRID.x(p.bbox_cols[1])
    y0 = GRID.y(p.bbox_rows[0])
    y1 = GRID.y(p.bbox_rows[1])
    n_stations = len(p.stations)
    n_ticks = max(2, p.time_ticks)
    out = [f'<g id="{p.id}">']
    # background grid: horizontal lines per station, vertical ticks per time
    for i in range(n_stations):
        y = y0 + (y1 - y0) * (i / max(1, n_stations - 1))
        out.append(
            f'<line x1="{x0:.1f}" y1="{y:.1f}" x2="{x1:.1f}" y2="{y:.1f}" '
            f'stroke="{PALETTE["stroke_faint"]}" stroke-width="0.7" '
            f'opacity="0.55"/>'
        )
        # station label on left
        out.append(
            f'<text x="{x0 - 8:.1f}" y="{y + 4:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="10" letter-spacing="2" '
            f'text-anchor="end" fill="{PALETTE["stroke_dim"]}" '
            f'text-transform="uppercase">{_xml_escape(p.stations[i].upper())}</text>'
        )
    for k in range(n_ticks):
        x = x0 + (x1 - x0) * (k / (n_ticks - 1))
        out.append(
            f'<line x1="{x:.1f}" y1="{y0:.1f}" x2="{x:.1f}" y2="{y1 + 4:.1f}" '
            f'stroke="{PALETTE["stroke_faint"]}" stroke-width="0.5" '
            f'stroke-dasharray="1 4" opacity="0.45"/>'
        )
    # baseline rule along the bottom
    out.append(
        f'<line x1="{x0:.1f}" y1="{y1 + 4:.1f}" x2="{x1:.1f}" y2="{y1 + 4:.1f}" '
        f'stroke="{PALETTE["stroke_dim"]}" stroke-width="0.7"/>'
    )
    # trajectories
    for ti, traj in enumerate(p.trajectories):
        if not traj:
            continue
        is_focal = (ti == p.focal_idx)
        pts = []
        for tnorm, st_idx in traj:
            x = x0 + (x1 - x0) * max(0.0, min(1.0, tnorm))
            si = max(0.0, min(float(n_stations - 1), float(st_idx)))
            y = y0 + (y1 - y0) * (si / max(1, n_stations - 1))
            pts.append(f"{x:.1f},{y:.1f}")
        sw = 1.4 if is_focal else 0.8
        op = 0.95 if is_focal else 0.55
        fl = ' filter="url(#chromatic)"' if is_focal else ""
        # Approx polyline length for stroke-dash reveal animation
        poly_len = 0.0
        for k in range(1, len(pts)):
            x1c, y1c = (float(c) for c in pts[k - 1].split(","))
            x2c, y2c = (float(c) for c in pts[k].split(","))
            poly_len += ((x2c - x1c) ** 2 + (y2c - y1c) ** 2) ** 0.5
        anim_inner = ""
        if p.animate and poly_len > 0:
            # progressive reveal: each trajectory starts staggered by 5%
            # of its index, total 4-second reveal
            begin = f"{ti * 0.25:.2f}s"
            anim_inner = (
                f'<animate attributeName="stroke-dashoffset" '
                f'from="{poly_len:.1f}" to="0" begin="{begin}" '
                f'dur="3.5s" fill="freeze"/>'
            )
            extra_attrs = (
                f' stroke-dasharray="{poly_len:.1f}" '
                f'stroke-dashoffset="{poly_len:.1f}"'
            )
        else:
            extra_attrs = ""
        out.append(
            f'<polyline points="{" ".join(pts)}" fill="none" '
            f'stroke="{PALETTE["stroke"]}" stroke-width="{sw}" '
            f'opacity="{op}"{fl}{extra_attrs}>{anim_inner}</polyline>'
        )
        # dots at each crossing
        for x_y in pts:
            x_str, y_str = x_y.split(",")
            out.append(
                f'<circle cx="{x_str}" cy="{y_str}" r="{2.2 if is_focal else 1.4}" '
                f'fill="{PALETTE["focal"] if is_focal else PALETTE["stroke_dim"]}" '
                f'opacity="{0.95 if is_focal else 0.7}"/>'
            )
        # focal label at the right end
        if is_focal and p.trajectory_labels and ti < len(p.trajectory_labels):
            last_x, last_y = pts[-1].split(",")
            out.append(
                f'<text x="{float(last_x) + 8:.1f}" y="{float(last_y) + 4:.1f}" '
                f'font-family="{FONT_FAMILY}" font-size="10" letter-spacing="2" '
                f'fill="{PALETTE["stroke"]}" text-transform="uppercase">'
                f'{_xml_escape(p.trajectory_labels[ti].upper())}</text>'
            )
    out.append("</g>")
    return "\n".join(out)


def _densify(values: list[float], target_n: int = 64) -> list[float]:
    """Linear-interpolate a short value list up to target_n samples so a
    polyline reads as a smooth curve regardless of how few values the LLM
    supplied. Pass-through when len(values) >= target_n."""
    m = len(values)
    if m == 0 or m >= target_n:
        return values
    out: list[float] = []
    for i in range(target_n):
        t = i * (m - 1) / max(1, target_n - 1)
        i0 = int(t)
        i1 = min(m - 1, i0 + 1)
        f = t - i0
        out.append(values[i0] * (1 - f) + values[i1] * f)
    return out


def emit_ridgeline(p: Ridgeline) -> str:
    """Joy Division ridgeline: n stacked hairline curves. Each row's
    polyline is densified to ~64 samples so short value lists still
    read as smooth curves; the bg-fill polygon extends below the local
    row to fully occlude lower rows even with high overlap."""
    if not p.rows:
        return ""
    x0 = GRID.x(p.bbox_cols[0])
    x1 = GRID.x(p.bbox_cols[1])
    y0 = GRID.y(p.bbox_rows[0])
    y1 = GRID.y(p.bbox_rows[1])
    n = len(p.rows)
    avail = y1 - y0
    eff_rows = n - p.overlap * (n - 1) if n > 1 else 1
    row_h = avail / max(1.0, eff_rows)
    step = row_h * (1 - p.overlap)
    out = [f'<g id="{p.id}">']
    # draw bottom-up so upper rows overlay lower
    for i in reversed(range(n)):
        label, values = p.rows[i]
        if not values:
            continue
        is_focal = (i == p.focal_idx)
        baseline = y0 + i * step + row_h
        # densify before drawing
        dense = _densify(values, 64)
        pts = []
        m = len(dense)
        for k, v in enumerate(dense):
            x = x0 + (x1 - x0) * (k / max(1, m - 1))
            yv = baseline - max(0.0, min(1.0, v)) * row_h
            pts.append(f"{x:.1f},{yv:.1f}")
        # bg-fill area: extend BELOW the local baseline by `step` so the
        # polygon fully occludes lower rows (was clipping at baseline only,
        # which let lower-row peaks bleed through under high overlap).
        floor_y = baseline + step
        area = (f"{x0:.1f},{floor_y:.1f} {x0:.1f},{baseline:.1f} "
                + " ".join(pts) +
                f" {x1:.1f},{baseline:.1f} {x1:.1f},{floor_y:.1f}")
        out.append(
            f'<polygon points="{area}" fill="{PALETTE["bg"]}" opacity="1.0"/>'
        )
        sw = 1.4 if is_focal else 0.9
        fl = ' filter="url(#chromatic)"' if is_focal else ""
        out.append(
            f'<polyline points="{" ".join(pts)}" fill="none" '
            f'stroke="{PALETTE["stroke"]}" stroke-width="{sw}" '
            f'opacity="{0.95 if is_focal else 0.65}"{fl}/>'
        )
        out.append(
            f'<text x="{x1 + 8:.1f}" y="{baseline - 4:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="10" letter-spacing="2" '
            f'fill="{PALETTE["stroke" if is_focal else "stroke_dim"]}" '
            f'text-transform="uppercase">'
            f'{_xml_escape(label.upper())}</text>'
        )
    out.append("</g>")
    return "\n".join(out)


def emit_sorted_matrix(p: SortedMatrix) -> str:
    """Bertin-style reorderable matrix: rows × cols cells with value glyphs."""
    if not p.matrix or not p.matrix[0]:
        return ""
    n_rows = len(p.matrix)
    n_cols = len(p.matrix[0])
    x0 = GRID.x(p.bbox_cols[0])
    x1 = GRID.x(p.bbox_cols[1])
    y0 = GRID.y(p.bbox_rows[0])
    y1 = GRID.y(p.bbox_rows[1])
    cell_w = (x1 - x0) / n_cols
    cell_h = (y1 - y0) / n_rows
    cell_sz = min(cell_w, cell_h)
    out = [f'<g id="{p.id}">']
    # row labels (left of grid)
    for i in range(min(n_rows, len(p.row_labels))):
        cy = y0 + (i + 0.5) * cell_h
        out.append(
            f'<text x="{x0 - 8:.1f}" y="{cy + 4:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="9" letter-spacing="2" '
            f'text-anchor="end" fill="{PALETTE["stroke_dim"]}" '
            f'text-transform="uppercase">'
            f'{_xml_escape(p.row_labels[i].upper())}</text>'
        )
    # column labels (rotated 90 above grid)
    for j in range(min(n_cols, len(p.col_labels))):
        cx = x0 + (j + 0.5) * cell_w
        out.append(
            f'<text x="{cx:.1f}" y="{y0 - 10:.1f}" '
            f'font-family="{FONT_FAMILY}" font-size="9" letter-spacing="2" '
            f'text-anchor="start" fill="{PALETTE["stroke_dim"]}" '
            f'text-transform="uppercase" '
            f'transform="rotate(-60 {cx:.1f} {y0 - 10:.1f})">'
            f'{_xml_escape(p.col_labels[j].upper())}</text>'
        )
    # cells
    for i in range(n_rows):
        for j in range(n_cols):
            v = max(0.0, min(1.0, float(p.matrix[i][j])))
            cx = x0 + (j + 0.5) * cell_w
            cy = y0 + (i + 0.5) * cell_h
            if p.glyph == "fill":
                # filled square scaled by value
                sz = max(2.0, v * cell_sz * 0.85)
                out.append(
                    f'<rect x="{cx - sz / 2:.1f}" y="{cy - sz / 2:.1f}" '
                    f'width="{sz:.1f}" height="{sz:.1f}" '
                    f'fill="{PALETTE["focal"]}" opacity="{0.85 * v + 0.1:.2f}"/>'
                )
            else:
                # proportional dot
                r = max(1.0, v * cell_sz * 0.4)
                out.append(
                    f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" '
                    f'fill="{PALETTE["focal"]}" opacity="{0.85 * v + 0.1:.2f}"/>'
                )
    # cluster outline box (chromatic)
    if p.cluster_box:
        r0, c0, r1, c1 = p.cluster_box
        bx0 = x0 + c0 * cell_w
        by0 = y0 + r0 * cell_h
        bx1 = x0 + (c1 + 1) * cell_w
        by1 = y0 + (r1 + 1) * cell_h
        out.append(
            f'<rect x="{bx0:.1f}" y="{by0:.1f}" '
            f'width="{(bx1 - bx0):.1f}" height="{(by1 - by0):.1f}" '
            f'fill="none" stroke="{PALETTE["stroke"]}" stroke-width="1.2" '
            f'filter="url(#chromatic)"/>'
        )
    out.append("</g>")
    return "\n".join(out)


EMITTERS = {
    "focal": emit_focal,
    "constellation": emit_constellation,
    "wave": emit_wave,
    "cone": emit_cone,
    "iso": emit_iso,
    "arrows": emit_arrows,
    "field": None,  # special: needs canvas
    "label": emit_label,
    "orbit_rings": emit_orbit_rings,
    "lattice": emit_lattice,
    "particles": emit_particles,
    "permutation": emit_permutation,
    "dual_pane": emit_dual_pane,
    "scramble": emit_scramble,
    "text": emit_text,
    "axis": emit_axis,
    "tick": emit_tick,
    "bracket": emit_bracket,
    "measure": emit_measure,
    "sankey": emit_sankey,
    "tree": emit_tree,
    "callout": emit_callout,
    "corner_legend": emit_corner_legend,
    "tick_rings": emit_tick_rings,
    "dimension_stack": emit_dimension_stack,
    "proportional_dot": emit_proportional_dot,
    "sparkline": emit_sparkline,
    "marey": emit_marey,
    "ridgeline": emit_ridgeline,
    "sorted_matrix": emit_sorted_matrix,
}
