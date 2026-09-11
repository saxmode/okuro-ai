# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: read a website and fill the kit's authored inputs — accent, neutrals, typeface, radius, border weight. Nothing else.
# index:
#   imports
#   _EXTRACT_JS
#   ScanResult
#   summarize
#   scan_url
#   scan_urls
# AGENT_HEADER_END -->
"""What a website says about a brand, in the engine's own vocabulary.

The owner, 2026-09-03: *"the new design system allows you to edit simple base
variables. i think the scraping service should check a website and try to fill
in exactly these variables and nothing else."*

So the target list is not a judgement call -- it is `explain.authored_inputs`,
the same set the editor exposes. Of its 18 entries, five are scannable and the
rest are not, for reasons worth stating because the boundary is the whole
design:

    SCANNED
      brand.canonical    the accent: the most-covering colour that is neither
                         the page's own background nor its body ink
      neutrals.white     the dominant background
      neutrals.black     the dominant text colour
      font.family        the most-covering typeface
      radius.base        the most common non-zero border-radius
      border weight      the most common non-zero border-width

    NOT SCANNED, and deliberately
      brand.on_light/on_dark  DERIVED from canonical by the shade rule. A page
                              cannot tell you them; the engine computes them.
      signals.*               a marketing site rarely shows all four states,
                              and a wrong success-green is worse than the
                              base's right one. Left to the base.
      font.slot_1..4          the weight QUAD comes from the installed family's
                              measured faces, not from the page -- a page using
                              600 tells you nothing about which faces exist.
                              The page's weights are reported so a caller can
                              see them, and are not turned into slots here.
      threshold, root         okuro's own constants, not a brand's.

WHAT THIS DROPS FROM THE OLD SCRAPER, and why that is the point. The v0 version
returned a type scale, body size, four weights, three line-heights, letter
spacing and a text transform -- ten keys, of which the kit has a slot for
roughly three. Seven measured values were computed on every scan and thrown
away. This returns what a kit can hold.

WHAT IT KEEPS: area weighting. Frequency is not prominence -- an element count
treats a 10px footer link like the hero headline -- so every tally here is in
rendered pixels. That was the good idea in the old scraper and it is unchanged.

REQUIRES the `browser` extra (Playwright + a chromium download, ~200 MB).
Import it lazily at the call site; okuro must boot without it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger("okuro.design_engine.scan")

#: Runs in the page. One record per VISIBLE element, carrying its rendered area
#: so the caller can weight by prominence. Reads exactly the properties a kit
#: has a slot for -- note `borderRadius` and `borderWidth`, which the v0
#: extractor never collected even though the kit has authored inputs for both.
_EXTRACT_JS = """() => {
  const out = [];
  for (const el of document.querySelectorAll('body, body *')) {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    if (parseFloat(s.opacity) === 0) continue;
    const r = el.getBoundingClientRect();
    const area = Math.max(0, r.width) * Math.max(0, r.height);
    if (area <= 0) continue;

    // Direct text only — an outer <div> must not claim its children's copy,
    // or a wrapper contributes a typeface nobody can read.
    let ownText = '';
    for (const node of el.childNodes) {
      if (node.nodeType === 3) ownText += node.textContent;
    }
    ownText = ownText.trim();

    const rec = {
      area,
      bg: s.backgroundColor,
      color: s.color,
      radius: parseFloat(s.borderTopLeftRadius) || 0,
      border: parseFloat(s.borderTopWidth) || 0,
      borderColor: s.borderTopColor,
    };
    if (ownText.length > 1) {
      rec.text = true;
      rec.family = (s.fontFamily || '').split(',')[0].trim().replace(/['"]/g, '');
      rec.weight = parseInt(s.fontWeight, 10) || 400;
    }
    out.push(rec);
  }
  return out;
}"""

#: A radius at or above this is a PILL, not a corner — a fully rounded button
#: or avatar. Rolling those into the tally would report a brand's corner as
#: 9999px. The engine models a pill as a shape sentinel, not a radius.
_PILL_PX = 100.0

#: Below this a "rounded" corner is a rendering artefact rather than a decision.
_MIN_RADIUS_PX = 1.0


def _hex(raw: str) -> str | None:
    """rgb/rgba -> hex. None for transparent or unparseable."""
    m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)", raw or "")
    if not m:
        return None
    r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
    alpha = float(m.group(4)) if m.group(4) else 1.0
    if alpha < 0.1:
        return None
    return f"#{r:02x}{g:02x}{b:02x}"


@dataclass
class ScanResult:
    """The authored inputs a page could fill, plus what it was read from.

    Every field is optional: a page that shows one colour and one font fills
    two of them, and that is a legitimate result rather than a failure. The
    caller decides what to do with a gap -- `fork_kit` simply keeps the base's
    value, which is the right answer for "rest same as base".
    """

    accent: str | None = None
    background: str | None = None
    foreground: str | None = None
    font_family: str | None = None
    radius_px: float | None = None
    border_px: float | None = None

    #: Reported, not applied. The weight quad is decided by the installed
    #: family's measured faces; these say what the PAGE used, which is useful
    #: for a human choosing a family and useless as a slot value.
    page_weights: list[int] = field(default_factory=list)

    #: How much was looked at, so a thin result is recognisable as thin.
    element_count: int = 0
    #: Runner-up colours, so a caller can offer a choice rather than a guess.
    colour_candidates: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize(samples: list[dict[str, Any]]) -> ScanResult:
    """Per-element records -> the kit inputs a page can fill.

    EVERY TALLY IS IN RENDERED PIXELS. Frequency is not prominence: counting
    elements makes a footer link weigh as much as a full-bleed hero, which is
    how the first version of this produced palettes that did not look like the
    site.
    """
    bg_area: Counter[str] = Counter()
    fg_area: Counter[str] = Counter()
    family_area: Counter[str] = Counter()
    weight_area: Counter[int] = Counter()
    radius_area: Counter[float] = Counter()
    border_area: Counter[float] = Counter()
    accent_area: Counter[str] = Counter()

    for rec in samples:
        area = float(rec.get("area") or 0)
        if area <= 0:
            continue

        bg = _hex(rec.get("bg", ""))
        if bg:
            bg_area[bg] += area
        if rec.get("text"):
            fg = _hex(rec.get("color", ""))
            if fg:
                fg_area[fg] += area
            family = (rec.get("family") or "").strip()
            if family:
                family_area[family] += area
            weight_area[int(rec.get("weight") or 400)] += area

        # A corner is a decision only when it is neither noise nor a pill.
        radius = float(rec.get("radius") or 0)
        if _MIN_RADIUS_PX <= radius < _PILL_PX:
            radius_area[round(radius)] += area

        border = float(rec.get("border") or 0)
        if border > 0:
            border_area[round(border)] += area
            # A BORDER COLOUR IS A GOOD ACCENT CANDIDATE and a bad background
            # one: a brand marks edges and small chrome with its colour far
            # more often than it paints a whole section with it. Weighted by
            # the element's area all the same, so a hairline on a hero counts
            # for the hero.
            edge = _hex(rec.get("borderColor", ""))
            if edge:
                accent_area[edge] += area

    background = bg_area.most_common(1)[0][0] if bg_area else None
    foreground = fg_area.most_common(1)[0][0] if fg_area else None

    # THE ACCENT IS THE COLOUR THAT IS NEITHER THE PAGE NOR ITS INK. Taking the
    # most-covering colour outright returns the background every time; taking
    # the most-covering TEXT colour returns the body ink. So the two structural
    # colours are excluded and the next-largest is the brand's.
    structural = {c for c in (background, foreground) if c}
    for colour, area in bg_area.items():
        accent_area[colour] += area * 0.5  # a painted block counts, at a discount
    ranked = [c for c, _ in accent_area.most_common() if c not in structural]
    accent = ranked[0] if ranked else None

    return ScanResult(
        accent=accent,
        background=background,
        foreground=foreground,
        font_family=family_area.most_common(1)[0][0] if family_area else None,
        radius_px=float(radius_area.most_common(1)[0][0]) if radius_area else None,
        border_px=float(border_area.most_common(1)[0][0]) if border_area else None,
        page_weights=sorted(w for w, _ in weight_area.most_common(4)),
        element_count=len(samples),
        colour_candidates=ranked[:5],
    )


async def _collect(page, url: str, timeout_ms: int) -> list[dict[str, Any]]:
    try:
        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
    except Exception:
        # A site that never goes idle (polling, ads, a live feed) still has a
        # rendered DOM worth reading. Falling back is not a degraded scan.
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    return await page.evaluate(_EXTRACT_JS)


def scan_url(url: str, *, timeout_ms: int = 20_000) -> ScanResult:
    """One page. Raises ImportError without the `browser` extra."""
    return scan_urls([url], timeout_ms=timeout_ms)[0]


def scan_urls(urls: list[str], *, timeout_ms: int = 20_000) -> list[ScanResult]:
    """Several pages, one browser.

    ONE EXTRACTION SCRIPT AND ONE SUMMARIZER for both entry points. The v0
    scraper carried copy-pasted copies of each, which is how its single-URL and
    multi-URL paths came to disagree about what they collected.

    A page that fails is returned as an EMPTY ScanResult rather than raised, so
    one bad URL out of six does not lose the other five. `element_count == 0`
    is the tell.
    """

    async def _run() -> list[ScanResult]:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                results: list[ScanResult] = []
                for url in urls:
                    page = await browser.new_page()
                    try:
                        samples = await _collect(page, url, timeout_ms)
                        results.append(summarize(samples))
                    except Exception as exc:
                        logger.warning("scan failed for %s: %s", url, exc)
                        results.append(ScanResult())
                    finally:
                        await page.close()
                return results
            finally:
                await browser.close()

    return asyncio.run(_run())


__all__ = ["ScanResult", "summarize", "scan_url", "scan_urls"]
