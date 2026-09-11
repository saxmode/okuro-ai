# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W2 — the render layer. Compose a solved Placement into a
#   12-col CSS-grid slide over the SHIPPED kit CSS (the kit gallery pipeline),
#   render it once via Playwright to (a) measure cell/slide heights for the
#   measure-guard, (b) screenshot the proof, and (c) ARM the rendered==gallery
#   contract for solver output (contract_test.check_contract on pipeline
#   fragments). Zero styling authored beyond kit classes + composed.css geometry.
# index:
#   compose_slide_html / SlideRenderer (measure/screenshot) / make_measure_fn
#   check_solver_contract
# AGENT_HEADER_END -->
"""Render solver output through the kit gallery pipeline.

A ``Placement`` becomes ``.composed-slide > .composed-row > .composed-cell`` with
each cell's ``grid-column: span N`` set from the solved spans and each cell's body
built by ``renderers.build`` (gallery-parity fragments). The page links the
shipped ``board/*.css`` so the SAME hand-authored CSS paints the slide — the
anti-drift guarantee. One render yields the measured heights the measure-guard
needs; the same fragments feed ``check_contract`` so drift from the gallery is a
CI failure (v4.1 C).
"""
from __future__ import annotations

import functools
import hashlib
import http.server
import json
import socket
import socketserver
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from okuro.prism.solver.arrangements import arrangement_ids_for_spans
from okuro.prism.solver.candidates import Placement
from okuro.prism.solver.families import get_family
from okuro.prism.solver.renderers import EXT_MAX_FILL, build, gallery_max_html
from okuro.prism.solver.schema import Claim, SlidePlan
from okuro.prism.solver.solve import MeasureReport

KIT_ROOT = Path(__file__).resolve().parents[1] / "kit"
_CONTRACT_REF = KIT_ROOT / "tests" / "contract_reference.json"

_CSS = ["board/tokens.css", "board/theme.css", "board/components.css",
        "board/components-ext.css", "board/zoom-stage.css", "gallery/composed.css"]


def _cell_content(unit_idx, plan, claims, col_span: int = 12) -> str:
    u = plan.units[unit_idx]
    items = u.resolved_items(claims)
    chars = u.resolved_chars(claims)
    text = " ".join(claims[c].text for c in u.claim_ids if c in claims)
    # REAL per-item content (engine 2): the unit's claims' mined (label,detail,meta)
    # triples, truncated to what this level renders (N of M). Empty -> builders fall
    # back to synthetic fill. col_span drives width-aware orphan-free tiling.
    cells: list[tuple[str, str, str]] = []
    for cid in u.claim_ids:
        if cid in claims:
            cells.extend(claims[cid].content_items())
    return build(u.component, {"items": items, "chars": chars, "text": text,
                              "col_span": col_span, "cells": cells[:items]})


def compose_slide_html(
    p: Placement, plan: SlidePlan, claims: dict[str, Claim], brand: str,
    css_base: str | None,
) -> str:
    """HTML for one composed slide, rendered by the SAME kit builders as the gallery.

    ``css_base`` set → a full standalone document linking the served kit CSS at that
    base (gallery / screenshot / measure paths). ``css_base=None`` → just the
    ``.composed-slide`` fragment (no <html>/<head>/<link>) for embedding in a host
    that already loads the kit CSS — the deck2 viewer's ComposedCell. Same markup
    either way, so what ships is byte-identical to what the gallery/contract test
    verifies (no re-render, no cross-language drift)."""
    fam = get_family(plan.family)
    rows_html = []
    for r in p.rows:
        cells = []
        for c in r.cells:
            span = f"grid-column: span {c.span};"
            if c.unit is None:
                cells.append(f'<div class="composed-cell spacer" style="{span}"></div>')
            else:
                u = plan.units[c.unit]
                body = _cell_content(c.unit, plan, claims, c.span)
                cells.append(
                    f'<div class="composed-cell" style="{span}" data-unit="{c.unit}" '
                    f'data-component="{u.component}" data-span="{c.span}" '
                    f'data-emphasis="{u.emphasis}">{body}</div>'
                )
        gap = f"gap:{fam.gutter_px}px; margin-bottom:{fam.gutter_px}px;"
        # P4.1: name the row grammar in the DOM. Derived from the solved spans via
        # the arrangement bridge, so it is a READING of the geometry, never a
        # restatement of what the plan asked for — the two can disagree and that
        # disagreement is exactly what the eyes-on-glass proof needs to show.
        # Empty (no library arrangement names this shape) renders as "" rather
        # than a guess.
        arrs = ",".join(arrangement_ids_for_spans(r.spans, plan.family))
        rows_html.append(
            f'<div class="composed-row" data-arrangement="{arrs}" '
            f'data-spans="{"|".join(str(s) for s in r.spans)}" style="{gap}">'
            + "".join(cells) + "</div>"
        )
    body = "\n".join(rows_html)
    # data-archetype="composed" makes the composed slide a first-class citizen of
    # the numeric gate, which keys its per-slide checks off `[data-archetype]`.
    # Additive only: the gate reads composed-ness from the CSS class, so a slide
    # whose HTML was persisted before this marker existed still gates correctly.
    # data-theme rides on the slide itself so the fragment path (css_base=None)
    # still themes without the <html data-theme> wrapper it does not emit.
    slide = (f'<div class="composed-slide" data-archetype="composed" '
             f'data-theme="{brand}" data-level="{plan.level}" '
             f'data-family="{plan.family}">{body}</div>')
    if css_base is None:
        return slide  # embeddable fragment: host supplies the kit CSS + theme root
    links = "\n".join(f'<link rel="stylesheet" href="{css_base}/{c}">' for c in _CSS)
    return (f'<!doctype html><html lang="en" data-theme="{brand}"><head>'
            f'<meta charset="utf-8">{links}</head><body>{slide}</body></html>')


# ── measurement JS ────────────────────────────────────────────────────────────

_MEASURE_JS = r"""
() => {
  const slide = document.querySelector('.composed-slide');
  const cells = Array.from(document.querySelectorAll('.composed-cell[data-unit]'));
  const sb = slide.getBoundingClientRect();
  // P4.2: BOXES, not just heights. The geometry linter's collision and visual-
  // imbalance legs are pure box arithmetic, so the measure path has to carry the
  // full rect (x/y/w/h) for the slide, every cell, and every painted leaf inside
  // a cell. Heights alone cannot see an overlap or locate a centroid.
  const box = (el) => {
    const r = el.getBoundingClientRect();
    return {x: r.left - sb.left, y: r.top - sb.top, w: r.width, h: r.height};
  };
  // A painted leaf = an element with no element children that occupies area. It is
  // what the eye reads as visual mass, so it is what the centroid weighs.
  const leaves = [];
  for (const c of cells) {
    for (const el of c.querySelectorAll('*')) {
      if (el.children.length) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      const st = getComputedStyle(el);
      if (st.visibility === 'hidden' || st.display === 'none') continue;
      if (parseFloat(st.opacity) < 0.05) continue;   // aeslides min_opacity
      leaves.push({unit: +c.getAttribute('data-unit'), ...box(el)});
    }
  }
  return {
    slide_height: Math.round(sb.height),
    slide: {x: 0, y: 0, w: sb.width, h: sb.height},
    rows: Array.from(document.querySelectorAll('.composed-row')).map((r) => ({
      arrangement: r.getAttribute('data-arrangement') || '',
      spans: r.getAttribute('data-spans') || '',
      ...box(r),
    })),
    cells: cells.map((c) => ({
      unit: +c.getAttribute('data-unit'),
      component: c.getAttribute('data-component'),
      span: +c.getAttribute('data-span'),
      height: Math.round(c.getBoundingClientRect().height),
      ...box(c),
    })),
    leaves: leaves,
  };
}
"""

# skeleton (tag + sorted class tree, text elided) — identical rule to contract_test.
_SKELETON_JS = r"""
(sel) => {
  const skel = (el) => {
    const cls = Array.from(el.classList).sort().join('.');
    const kids = Array.from(el.children).map(skel).join('');
    return '<' + el.tagName.toLowerCase() + (cls ? '.' + cls : '') + '>' + kids;
  };
  const el = document.querySelector(sel);
  return el ? skel(el) : null;
}
"""


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


class SlideRenderer:
    """Holds a served kit + a Playwright page; renders/measures composed slides."""

    def __init__(self, page, base: str) -> None:
        self.page = page
        self.base = base

    def _load(self, html: str) -> None:
        self.page.set_content(html, wait_until="networkidle")
        self.page.wait_for_timeout(80)

    def measure(
        self, p: Placement, plan: SlidePlan, claims: dict[str, Claim], brand: str,
        viewport: int = 1920,
    ) -> dict:
        self.page.set_viewport_size({"width": viewport, "height": 1080})
        self._load(compose_slide_html(p, plan, claims, brand, self.base))
        return self.page.evaluate(_MEASURE_JS)

    def screenshot(
        self, p: Placement, plan: SlidePlan, claims: dict[str, Claim], brand: str,
        out_path: Path, viewport: int = 1920,
    ) -> None:
        self.page.set_viewport_size({"width": viewport, "height": 1080})
        self._load(compose_slide_html(p, plan, claims, brand, self.base))
        el = self.page.query_selector(".composed-slide")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        el.screenshot(path=str(out_path))

    def screenshot_bytes(
        self, p: Placement, plan: SlidePlan, claims: dict[str, Claim], brand: str,
        viewport: int = 1920,
    ) -> bytes:
        """P4.2 PIXEL LEG. The whitespace check is a local-variance map over
        PIXELS — the DOM cannot see it, because a cell can be full-height and
        visually empty. ``screenshot`` writes a file for the proof harness; this
        returns the PNG in memory so the linter can measure without touching disk.
        """
        self.page.set_viewport_size({"width": viewport, "height": 1080})
        self._load(compose_slide_html(p, plan, claims, brand, self.base))
        el = self.page.query_selector(".composed-slide")
        return el.screenshot(type="png")

    def measure_and_shoot(
        self, p: Placement, plan: SlidePlan, claims: dict[str, Claim], brand: str,
        viewport: int = 1920,
    ) -> tuple[dict, bytes]:
        """Both legs from ONE render — the linter needs boxes AND pixels of the
        same paint. Measuring and screenshotting separately re-renders, and a
        re-render is a second sample: fonts settle, and the two legs would then
        describe different images."""
        self.page.set_viewport_size({"width": viewport, "height": 1080})
        self._load(compose_slide_html(p, plan, claims, brand, self.base))
        boxes = self.page.evaluate(_MEASURE_JS)
        el = self.page.query_selector(".composed-slide")
        return boxes, el.screenshot(type="png")

    def fragment_skeleton(self, component: str, html: str) -> str:
        page_html = (f'<!doctype html><html data-theme="okuro"><head><meta charset="utf-8">'
                     + "".join(f'<link rel="stylesheet" href="{self.base}/{c}">' for c in _CSS)
                     + f'</head><body><div id="probe">{html}</div></body></html>')
        self._load(page_html)
        return self.page.evaluate(_SKELETON_JS, "#probe > *")


def make_measure_fn(renderer: SlideRenderer, viewport: int = 1920, lint: bool = False):
    """A MeasureFn for solve.solve: renders once, returns overflow/orphan report.
    Orphan = a content cell shorter than a row-unit sitting alone in its row.

    ``lint=True`` (P4.3) additionally runs the geometry linter over the SAME
    render — one paint, both verdicts — and attaches the ``LintReport`` so
    ``solve`` can hard-filter collisions and rerank near-ties on geometry. Left
    off by default so existing callers keep their exact render cost.
    """
    from okuro.prism.solver.solve import _ORPHAN_MIN_PX

    def fn(p: Placement, plan: SlidePlan, claims: dict[str, Claim], brand: str) -> MeasureReport:
        if lint:
            from okuro.prism.solver.geometry_lint import lint_geometry

            m, png = renderer.measure_and_shoot(p, plan, claims, brand, viewport)
            report = lint_geometry(m, png)
        else:
            m = renderer.measure(p, plan, claims, brand, viewport)
            report = None
        heights = {c["unit"]: c["height"] for c in m["cells"]}
        orphan = False
        for r in p.rows:
            content_cells = [c for c in r.cells if c.unit is not None]
            if len(content_cells) == 1 and len(r.cells) == 1:
                h = heights.get(content_cells[0].unit, 0)
                if h < _ORPHAN_MIN_PX:
                    orphan = True
        detail = f'{m["slide_height"]}px, {len(m["cells"])} cells'
        if report is not None:
            detail += (f', geometry {"clean" if report.clean else "FAILED"} '
                       f'(score {report.composition_score():.3f})')
        return MeasureReport(
            slide_height_px=float(m["slide_height"]),
            overflow=False,   # budget comparison is applied in solve() per level
            orphan=orphan,
            detail=detail,
            lint=report,
        )

    return fn


@contextmanager
def serving_kit() -> Iterator[SlideRenderer]:
    """Serve the kit over http + a headless page, yield a SlideRenderer."""
    from playwright.sync_api import sync_playwright

    port = _free_port()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(KIT_ROOT))
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    httpd.allow_reuse_address = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(device_scale_factor=1)
            try:
                yield SlideRenderer(page, base)
            finally:
                browser.close()
    finally:
        httpd.shutdown()


def check_solver_contract(renderer: SlideRenderer) -> list[str]:
    """ARM the rendered==gallery contract for solver output (v4.1 C): render each
    pipeline component fragment at gallery max fill, capture its skeleton, and
    diff against contract_reference.json. Returns the drift list ([] = armed)."""
    reference = json.loads(_CONTRACT_REF.read_text())["corpus"]
    drift: list[str] = []
    for comp, html in gallery_max_html().items():
        key = f"component:{comp}"
        skel = renderer.fragment_skeleton(comp, html)
        ref = reference.get(key)
        if ref is None:
            drift.append(f"{key}: no reference specimen")
        elif skel is None:
            drift.append(f"{key}: pipeline produced no fragment")
        elif _hash(skel) != ref["skeleton_hash"]:
            drift.append(f"{key}: skeleton drift — pipeline {_hash(skel)} != gallery {ref['skeleton_hash']}")
    return drift


__all__ = [
    "compose_slide_html", "SlideRenderer", "make_measure_fn", "serving_kit",
    "check_solver_contract", "KIT_ROOT",
]
