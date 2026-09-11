# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: provides SVG rendering and optional PNG rasterization for scenes
# index:
#   imports
#   def scene_to_svg
#   def write_svg
#   def write_png
#   def _png_via_playwright
#   def render_all_sizes
# AGENT_HEADER_END -->
"""Scene -> SVG string. Optional rasterization to PNG."""
from __future__ import annotations
import os
from pathlib import Path

from .scene import Scene
from .style import DEFS, PALETTE, CANVAS_PRESETS, defs_for_accent
from .primitives import (
    emit_focal, emit_constellation, emit_wave, emit_cone, emit_iso,
    emit_arrows, emit_field, emit_label,
    emit_orbit_rings, emit_lattice, emit_particles,
    emit_permutation, emit_dual_pane, emit_scramble,
    emit_text, emit_axis, emit_tick, emit_bracket, emit_measure,
    emit_sankey, emit_tree,
    emit_callout, emit_corner_legend,
    emit_tick_rings, emit_dimension_stack, emit_proportional_dot,
    emit_sparkline, emit_marey, emit_ridgeline, emit_sorted_matrix,
)
from .animate import emit_animation


# Global gate. Animations are off while layouts are being tuned.
# Re-enable by setting TM_ILL_ANIMATE=1 in the env, or flipping this flag.
EMIT_ANIMATIONS: bool = os.environ.get("TM_ILL_ANIMATE", "0") == "1"


def scene_to_svg(scene: Scene) -> str:
    w = scene.canvas.width
    h = scene.canvas.height

    # Propagate scene.animate down into the primitives that support it.
    # Pydantic v2 disallows direct attr assignment in some configs; use
    # model_copy with update for safety.
    if getattr(scene, "animate", False):
        for p in scene.primitives:
            if p.kind in ("marey", "sankey") and not getattr(p, "animate", False):
                try:
                    p.animate = True  # type: ignore[attr-defined]
                except Exception:
                    pass

    body = []
    # background
    body.append(
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="{PALETTE["bg"]}"/>'
    )

    # primitives
    for p in scene.primitives:
        if p.kind == "focal":
            body.append(emit_focal(p))
        elif p.kind == "constellation":
            body.append(emit_constellation(p))
        elif p.kind == "wave":
            body.append(emit_wave(p))
        elif p.kind == "cone":
            body.append(emit_cone(p))
        elif p.kind == "iso":
            body.append(emit_iso(p))
        elif p.kind == "arrows":
            body.append(emit_arrows(p))
        elif p.kind == "field":
            body.append(emit_field(p, w, h))
        elif p.kind == "label":
            body.append(emit_label(p))
        elif p.kind == "orbit_rings":
            body.append(emit_orbit_rings(p))
        elif p.kind == "lattice":
            body.append(emit_lattice(p))
        elif p.kind == "particles":
            body.append(emit_particles(p))
        elif p.kind == "permutation":
            body.append(emit_permutation(p))
        elif p.kind == "dual_pane":
            body.append(emit_dual_pane(p))
        elif p.kind == "scramble":
            body.append(emit_scramble(p))
        elif p.kind == "text":
            body.append(emit_text(p))
        elif p.kind == "axis":
            body.append(emit_axis(p))
        elif p.kind == "tick":
            body.append(emit_tick(p))
        elif p.kind == "bracket":
            body.append(emit_bracket(p))
        elif p.kind == "measure":
            body.append(emit_measure(p))
        elif p.kind == "sankey":
            body.append(emit_sankey(p))
        elif p.kind == "tree":
            body.append(emit_tree(p))
        elif p.kind == "callout":
            body.append(emit_callout(p))
        elif p.kind == "corner_legend":
            body.append(emit_corner_legend(p))
        elif p.kind == "tick_rings":
            body.append(emit_tick_rings(p))
        elif p.kind == "dimension_stack":
            body.append(emit_dimension_stack(p))
        elif p.kind == "proportional_dot":
            body.append(emit_proportional_dot(p))
        elif p.kind == "sparkline":
            body.append(emit_sparkline(p))
        elif p.kind == "marey":
            body.append(emit_marey(p))
        elif p.kind == "ridgeline":
            body.append(emit_ridgeline(p))
        elif p.kind == "sorted_matrix":
            body.append(emit_sorted_matrix(p))

    # animations (emitted at end so xlink references resolve).
    # Currently gated off: layouts are the focus. Schema still accepts them
    # so cached scenes don't break; we just don't emit the SMIL.
    if EMIT_ANIMATIONS:
        for a in scene.animations:
            if a.target in scene.primitive_ids():
                body.append(emit_animation(a))

    accent = getattr(scene, "accent", "green")
    defs = defs_for_accent(accent)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {w} {h}" width="{w}" height="{h}">'
        f'{defs}'
        + "\n".join(body)
        + "</svg>"
    )
    return svg


def write_svg(scene: Scene, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(scene_to_svg(scene), encoding="utf-8")
    return out


def write_png(scene: Scene, out_path: str | Path,
              size: tuple[int, int] | None = None,
              backend: str = "playwright") -> Path:
    """Rasterize one frozen frame.

    backend:
      "playwright" — Chromium headless, full SVG-filter fidelity (default)
      "cairo"      — cairosvg, fast but drops feBlend/feColorMatrix
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    svg = scene_to_svg(scene)
    w, h = size or (scene.canvas.width, scene.canvas.height)

    if backend == "cairo":
        import cairosvg
        cairosvg.svg2png(
            bytestring=svg.encode("utf-8"),
            write_to=str(out),
            output_width=w, output_height=h,
        )
        return out

    # playwright path
    _png_via_playwright(svg, out, w, h)
    return out


def _png_via_playwright(svg: str, out: Path, w: int, h: int) -> None:
    """Render SVG to PNG using a headless Chromium so all SVG filters apply."""
    from playwright.sync_api import sync_playwright

    html = (
        '<!doctype html><html><head><style>'
        'html,body{margin:0;padding:0;background:#000;}'
        'svg{display:block;}'
        '</style></head><body>'
        + svg.replace(
            'viewBox="0 0 ',  # ensure svg fills the viewport box
            f'width="{w}" height="{h}" viewBox="0 0 ',
            1,
        )
        + '</body></html>'
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": w, "height": h},
                                device_scale_factor=1)
        page.set_content(html, wait_until="load")
        # Pause animations on first frame to keep PNG deterministic.
        page.evaluate(
            "() => document.querySelectorAll('animate, animateTransform, animateMotion')"
            ".forEach(a => { try { a.pauseAnimations && a.pauseAnimations(); "
            "a.setCurrentTime && a.setCurrentTime(0); } catch(e){} });"
        )
        try:
            page.evaluate(
                "() => { const s=document.querySelector('svg'); "
                "if(s && s.pauseAnimations) s.pauseAnimations(); }"
            )
        except Exception:
            pass
        out.parent.mkdir(parents=True, exist_ok=True)
        page.locator("svg").screenshot(path=str(out), omit_background=False)
        browser.close()


def render_all_sizes(scene: Scene, out_dir: str | Path, basename: str,
                     backend: str = "playwright") -> dict[str, Path]:
    """Emit SVG (animated) + PNGs at all 3 canonical sizes."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    results["svg"] = write_svg(scene, out_dir / f"{basename}.svg")
    for name, (w, h) in CANVAS_PRESETS.items():
        results[f"png_{name}"] = write_png(
            scene, out_dir / f"{basename}_{name}.png",
            size=(w, h), backend=backend,
        )
    return results
