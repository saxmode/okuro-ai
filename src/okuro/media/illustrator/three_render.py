# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: provides 3D rendering functionality for various templates using Three.js and headless Chromium
# index:
#   imports
#   def _variant_cube_lattice
#   def _variant_embedding_cloud
#   def _variant_axis_3d
#   def build_html
#   def render_to_png
#   def render_volume
# AGENT_HEADER_END -->
"""Three.js (WebGL) rendering path.

Used for templates whose semantics genuinely live in 3D: cube_lattice,
embedding_cloud, axis_3d, tensor_stack, frustum, helix. Renders to PNG
via headless Chromium (playwright) -- the same Chromium we already use
for SVG-with-filters rasterization.

Style stays locked: pure black, hairline white, single chromatic accent
on focal elements, monospace typography overlay.
"""
from __future__ import annotations
import html
import json
from pathlib import Path
from typing import Literal

from .style import PALETTE


# ---------------------------------------------------------------- HTML

# We use ES module imports from unpkg. Playwright's Chromium has internet
# in the default sandbox (LAN-only host); update-blocked envs should
# vendor the modules.
PAGE_TEMPLATE = r"""<!doctype html><html><head><meta charset="utf-8">
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&display=swap');
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#000;overflow:hidden;
          font-family:'JetBrains Mono',ui-monospace,Menlo,monospace;color:#e0e0e0;}
#stage{position:relative;width:__W__px;height:__H__px;}
canvas{display:block;}
.layer{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none;}
.eyebrow{position:absolute;left:32px;top:24px;font-size:10px;letter-spacing:4px;color:#909090;text-transform:uppercase;}
.title{position:absolute;left:32px;bottom:32px;font-size:22px;font-weight:500;letter-spacing:1px;color:#e0e0e0;text-transform:uppercase;}
.meta{position:absolute;right:32px;bottom:32px;font-size:11px;letter-spacing:2px;color:#909090;text-transform:uppercase;}
.measure{position:absolute;left:50%;top:24px;transform:translateX(-50%);font-size:11px;letter-spacing:2px;color:#909090;text-transform:uppercase;}
.center{position:absolute;left:50%;top:50%;transform:translate(-50%, 36px);font-size:11px;letter-spacing:2px;color:#e0e0e0;text-transform:uppercase;}
.dotgrid{background-image:radial-gradient(rgba(58,58,58,0.6) 0.6px, transparent 0.7px);background-size:24px 24px;opacity:0.45;}
</style>
</head>
<body>
<div id="stage">
  <div class="layer dotgrid"></div>
  <div class="layer">
    <div class="eyebrow">__EYEBROW__</div>
    <div class="title">__TITLE__</div>
    <div class="meta">__META__</div>
    __MEASURE__
    __CENTER__
  </div>
</div>
<script type="importmap">
{ "imports": { "three": "https://unpkg.com/three@0.160.0/build/three.module.js" } }
</script>
<script type="module">
import * as THREE from 'three';

const stage = document.getElementById('stage');
const W = __W__, H = __H__;

const scene = new THREE.Scene();
scene.background = null;

const camera = new THREE.PerspectiveCamera(34, W / H, 0.1, 100);
camera.position.set(__CAM_X__, __CAM_Y__, __CAM_Z__);
camera.lookAt(0, 0, 0);

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
renderer.setSize(W, H);
renderer.setPixelRatio(2);
renderer.setClearColor(0x000000, 0);
stage.insertBefore(renderer.domElement, stage.firstChild);

const STROKE     = 0xe0e0e0;
const STROKE_DIM = 0x6e6e6e;
const STROKE_FAINT = 0x2a2a2a;
const FOCAL      = 0xf4f1e8;
const FRINGE_R   = 0xff3344;
const FRINGE_G   = 0x22c55e;
const FRINGE_B   = 0x3b82f6;

function lineMat(opts = {}) {
  return new THREE.LineBasicMaterial({ color: STROKE, transparent: true, opacity: 1.0, ...opts });
}
function lineGeom(p1, p2) {
  return new THREE.BufferGeometry().setFromPoints([p1, p2]);
}
function addLine(p1, p2, opts={}) {
  scene.add(new THREE.Line(lineGeom(p1, p2), lineMat(opts)));
}

// Variant body --------------------------------------------------------
__VARIANT__
// ---------------------------------------------------------------------

renderer.render(scene, camera);
window.__rendered = true;
</script>
</body></html>
"""


# ---------------------------------------------------------------- variants

def _variant_cube_lattice(n: int = 4, highlight: tuple[int, int, int] | None = None,
                           seed: int = 0) -> str:
    hi = highlight or (n // 2, n // 2, n // 2)
    return f"""
{{
  const N = {n};
  const half = 1.0;
  // cube edges
  const cubeEdges = new THREE.EdgesGeometry(new THREE.BoxGeometry(2, 2, 2));
  scene.add(new THREE.LineSegments(cubeEdges, lineMat({{ opacity: 0.95 }})));
  // 3-axis chromatic fringes on the cube edges
  const fringeOff = 0.012;
  for (const [dx, color] of [[-fringeOff, FRINGE_R], [fringeOff, FRINGE_B]]) {{
    const m = new THREE.LineBasicMaterial({{ color: color, transparent: true, opacity: 0.7 }});
    const ce = new THREE.LineSegments(cubeEdges, m);
    ce.position.x = dx;
    scene.add(ce);
  }}
  // internal grid lines along each axis
  const faint = lineMat({{ opacity: 0.16 }});
  for (let i = 1; i < N; i++) {{
    const t = -1 + 2 * i / N;
    // X-direction lines on +/- Y faces, +/- Z faces
    for (const fy of [-1, 1]) {{
      addLine(new THREE.Vector3(-1, fy, t), new THREE.Vector3(1, fy, t), {{ opacity: 0.16 }});
      addLine(new THREE.Vector3(t, fy, -1), new THREE.Vector3(t, fy, 1), {{ opacity: 0.16 }});
    }}
    for (const fz of [-1, 1]) {{
      addLine(new THREE.Vector3(-1, t, fz), new THREE.Vector3(1, t, fz), {{ opacity: 0.16 }});
      addLine(new THREE.Vector3(t, -1, fz), new THREE.Vector3(t, 1, fz), {{ opacity: 0.16 }});
    }}
    for (const fx of [-1, 1]) {{
      addLine(new THREE.Vector3(fx, -1, t), new THREE.Vector3(fx, 1, t), {{ opacity: 0.16 }});
      addLine(new THREE.Vector3(fx, t, -1), new THREE.Vector3(fx, t, 1), {{ opacity: 0.16 }});
    }}
  }}
  // highlight cell at (hi[0], hi[1], hi[2]) -- a small filled cube + glow
  const hi = [{hi[0]}, {hi[1]}, {hi[2]}];
  const cellSize = 2 / N;
  const hx = -1 + cellSize * (hi[0] + 0.5);
  const hy = -1 + cellSize * (hi[1] + 0.5);
  const hz = -1 + cellSize * (hi[2] + 0.5);
  const sph = new THREE.SphereGeometry(0.06, 24, 24);
  const focal = new THREE.Mesh(sph, new THREE.MeshBasicMaterial({{ color: FOCAL }}));
  focal.position.set(hx, hy, hz);
  scene.add(focal);
  // glow halos (concentric)
  for (const [r, op] of [[0.16, 0.35], [0.28, 0.18], [0.45, 0.08]]) {{
    const halo = new THREE.Mesh(
      new THREE.SphereGeometry(r, 16, 16),
      new THREE.MeshBasicMaterial({{ color: 0xffffff, transparent: true, opacity: op }})
    );
    halo.position.copy(focal.position);
    scene.add(halo);
  }}
}}
"""


def _variant_embedding_cloud(n: int = 200, clusters: int = 3, seed: int = 7) -> str:
    return f"""
{{
  const N = {n};
  const CL = {clusters};
  // bounding box edges
  const box = new THREE.EdgesGeometry(new THREE.BoxGeometry(2.4, 2.4, 2.4));
  scene.add(new THREE.LineSegments(box, lineMat({{ opacity: 0.4 }})));

  // pseudo-random with seed
  let s = {seed};
  function rnd() {{ s = (s * 9301 + 49297) % 233280; return s / 233280; }}

  // place cluster centers in the cube
  const centers = [];
  for (let c = 0; c < CL; c++) {{
    centers.push([(rnd() - 0.5) * 1.4, (rnd() - 0.5) * 1.4, (rnd() - 0.5) * 1.4]);
  }}

  // points: each one belongs to a cluster, gaussian-jittered around center
  const positions = new Float32Array(N * 3);
  const colors = new Float32Array(N * 3);
  for (let i = 0; i < N; i++) {{
    const ci = i % CL;
    const c = centers[ci];
    positions[i*3+0] = c[0] + (rnd() - 0.5) * 0.55;
    positions[i*3+1] = c[1] + (rnd() - 0.5) * 0.55;
    positions[i*3+2] = c[2] + (rnd() - 0.5) * 0.55;
    // color shift per cluster -- subtle
    if (ci === 0) {{ colors[i*3]=1.0; colors[i*3+1]=0.95; colors[i*3+2]=0.92; }}
    else if (ci === 1) {{ colors[i*3]=0.92; colors[i*3+1]=0.98; colors[i*3+2]=1.0; }}
    else {{ colors[i*3]=0.95; colors[i*3+1]=1.0; colors[i*3+2]=0.92; }}
  }}
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  geo.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
  const mat = new THREE.PointsMaterial({{
    size: 0.045, vertexColors: true, transparent: true, opacity: 0.95,
    sizeAttenuation: true,
  }});
  scene.add(new THREE.Points(geo, mat));

  // bright cluster centroids (focals)
  for (const c of centers) {{
    const sph = new THREE.SphereGeometry(0.05, 16, 16);
    const m = new THREE.Mesh(sph, new THREE.MeshBasicMaterial({{ color: FOCAL }}));
    m.position.set(c[0], c[1], c[2]);
    scene.add(m);
  }}
}}
"""


def _variant_axis_3d(label_x: str = "X", label_y: str = "Y",
                     label_z: str = "Z", show_vector: bool = True,
                     vector_to: tuple[float, float, float] = (0.7, 0.5, 0.6)) -> str:
    return f"""
{{
  const len = 1.4;
  // axes follow house style: monochrome white line with a subtle RGB
  // chromatic-aberration triple offset along the local perpendicular.
  // Saturated axes are off-aesthetic for Architecture-Noir.
  function chromAxis(end) {{
    const dir = end.clone().normalize();
    // pick a stable perpendicular for the offset
    const up = Math.abs(dir.y) < 0.9
      ? new THREE.Vector3(0,1,0) : new THREE.Vector3(1,0,0);
    const perp = new THREE.Vector3().crossVectors(dir, up).normalize();
    const off = 0.012;
    function line(color, dx, opacity) {{
      const o = perp.clone().multiplyScalar(dx);
      const m = new THREE.LineBasicMaterial({{
        color: color, transparent: true, opacity: opacity, blending: THREE.AdditiveBlending
      }});
      const g = new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(0,0,0).add(o), end.clone().add(o),
      ]);
      scene.add(new THREE.Line(g, m));
    }}
    line(FRINGE_R, -off, 0.55);
    line(FRINGE_B,  off, 0.55);
    line(STROKE,   0.0, 0.95);
    // arrow head: cone in stroke color
    const cone = new THREE.Mesh(
      new THREE.ConeGeometry(0.03, 0.10, 12),
      new THREE.MeshBasicMaterial({{ color: STROKE }})
    );
    cone.position.copy(end);
    cone.lookAt(end.clone().multiplyScalar(2));
    scene.add(cone);
  }}
  chromAxis(new THREE.Vector3(len, 0, 0));
  chromAxis(new THREE.Vector3(0, len, 0));
  chromAxis(new THREE.Vector3(0, 0, len));

  // tick marks every 0.25 along each axis
  const tickLen = 0.04;
  for (let t = 0.25; t < len; t += 0.25) {{
    addLine(new THREE.Vector3(t, -tickLen, 0), new THREE.Vector3(t, tickLen, 0), {{ opacity: 0.5 }});
    addLine(new THREE.Vector3(-tickLen, t, 0), new THREE.Vector3(tickLen, t, 0), {{ opacity: 0.5 }});
    addLine(new THREE.Vector3(0, -tickLen, t), new THREE.Vector3(0, tickLen, t), {{ opacity: 0.5 }});
  }}

  // vector from origin to (vx, vy, vz)
  const vx={vector_to[0]}, vy={vector_to[1]}, vz={vector_to[2]};
  if ({str(show_vector).lower()}) {{
    const target = new THREE.Vector3(vx, vy, vz);
    const m = new THREE.LineBasicMaterial({{ color: FOCAL, linewidth: 2 }});
    const g = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0,0,0), target]);
    scene.add(new THREE.Line(g, m));
    // tip
    const tip = new THREE.Mesh(
      new THREE.SphereGeometry(0.05, 16, 16),
      new THREE.MeshBasicMaterial({{ color: FOCAL }})
    );
    tip.position.copy(target);
    scene.add(tip);
    // halo at tip
    const halo = new THREE.Mesh(
      new THREE.SphereGeometry(0.18, 16, 16),
      new THREE.MeshBasicMaterial({{ color: 0xffffff, transparent: true, opacity: 0.18 }})
    );
    halo.position.copy(target);
    scene.add(halo);
  }}
}}
"""


# ---------------------------------------------------------------- API

VolumeKind = Literal["cube_lattice", "embedding_cloud", "axis_3d"]


def build_html(*, kind: VolumeKind, eyebrow: str = "", title: str = "",
                meta: str = "", center_label: str = "",
                measure_label: str = "",
                width: int = 1200, height: int = 600,
                cam: tuple[float, float, float] = (3.0, 1.8, 3.4),
                seed: int = 0,
                # variant params
                n: int = 4,
                clusters: int = 3,
                highlight: tuple[int, int, int] | None = None,
                vector_to: tuple[float, float, float] = (0.7, 0.5, 0.6)) -> str:
    if kind == "cube_lattice":
        variant = _variant_cube_lattice(n=n, highlight=highlight, seed=seed)
    elif kind == "embedding_cloud":
        variant = _variant_embedding_cloud(n=n, clusters=clusters, seed=seed)
    elif kind == "axis_3d":
        variant = _variant_axis_3d(vector_to=vector_to)
    else:
        raise ValueError(f"unknown 3D kind: {kind}")

    measure_html = (
        f'<div class="measure">{html.escape(measure_label)}</div>'
        if measure_label else ""
    )
    center_html = (
        f'<div class="center">{html.escape(center_label)}</div>'
        if center_label else ""
    )

    return (
        PAGE_TEMPLATE
        .replace("__W__", str(width))
        .replace("__H__", str(height))
        .replace("__EYEBROW__", html.escape(eyebrow))
        .replace("__TITLE__", html.escape(title))
        .replace("__META__", html.escape(meta))
        .replace("__MEASURE__", measure_html)
        .replace("__CENTER__", center_html)
        .replace("__CAM_X__", str(cam[0]))
        .replace("__CAM_Y__", str(cam[1]))
        .replace("__CAM_Z__", str(cam[2]))
        .replace("__VARIANT__", variant)
    )


_VENDOR_THREE = Path(__file__).parent / "vendor" / "three.module.js"
_THREE_UNPKG_URL = "https://unpkg.com/three@0.160.0/build/three.module.js"


def render_to_png(html_doc: str, out_path: str | Path,
                   width: int = 1200, height: int = 600) -> Path:
    """Render Three.js HTML to PNG via headless Chromium.

    Intercepts the unpkg three.module.js request and serves the vendored
    copy from ./vendor/ if present — keeps 3D rendering offline-capable
    on LAN-only hosts."""
    from playwright.sync_api import sync_playwright

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    vendored = _VENDOR_THREE.exists()
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": width, "height": height},
                                device_scale_factor=2)
        if vendored:
            three_body = _VENDOR_THREE.read_text()
            def _route(route, request):
                if request.url == _THREE_UNPKG_URL:
                    route.fulfill(
                        status=200,
                        content_type="application/javascript",
                        body=three_body,
                    )
                else:
                    route.continue_()
            page.route("**/*", _route)
        page.set_content(html_doc, wait_until="load")
        # wait for the WebGL render to complete
        page.wait_for_function(
            "() => window.__rendered === true",
            timeout=15000,
        )
        page.wait_for_timeout(200)
        page.locator("#stage").screenshot(path=str(out), omit_background=False)
        browser.close()
    return out


def render_volume(*, kind: VolumeKind, out_path: str | Path,
                   width: int = 1200, height: int = 600, **kw) -> Path:
    doc = build_html(kind=kind, width=width, height=height, **kw)
    return render_to_png(doc, out_path, width=width, height=height)
