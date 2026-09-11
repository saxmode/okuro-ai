# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Resonance WEBSITE adapter — render a PCO as a self-contained
#   scroll-to-reveal HTML page in brand colors (gold-standard reference mechanic).
#   Proves media-agnostic fan-out with a genuinely non-deck backend.
# index:
#   def _brand_tokens
#   def _sections_from_brief
#   def build_html
#   def render_website
# AGENT_HEADER_END -->
"""The website media adapter — one PCO → a scroll-reveal web page.

The third media backend (alongside prism + slides), and the one that proves the
seam is truly media-agnostic: a website is not a deck. Reproduces the reference
"legacy architecture" gold standard — full-height sections, one accent over a
grey ramp, and a one-shot IntersectionObserver reveal (opacity + 16px translate,
staggered, reduced-motion killswitch).

Pipeline: content_brief (PCO + audience, from the render seam) → LLM writes
audience-fitted section copy → deterministic HTML builder wraps it in the fixed
reveal engine with the brand's tokens. The engine/design is fixed (quality
guaranteed); only the copy is generated. Provenance survives: each point keeps
its source as a hover title.
"""

from __future__ import annotations

import html
import json
import logging
from typing import Any, Optional

log = logging.getLogger("okuro.resonance.website")

# okuro's own defaults — near-black elevation + one accent. Used only when the
# caller supplies no brand.
#
# These were a CUSTOMER's colours and typeface until 2026-08-05, which meant a
# generated site with no brand specified came out looking like that customer. okuro
# ships one brand: its own. A user's brand comes from their brand registry.
_DEFAULTS = {"background": "#0a0a0a", "text": "#f0f0f0",
             "accent": "#11d425",
             "font": "'JetBrains Mono Variable', ui-monospace, monospace"}

_SECTION_SYSTEM = (
    "You write the copy for a scroll-to-reveal web page from a content brief. "
    "Fit the wording to the AUDIENCE noted in the brief; do NOT change the facts. "
    "Return ONLY JSON:\n"
    '{"title": str, "hero": {"eyebrow": str, "title": str, "lede": str}, '
    '"sections": [{"eyebrow": str, "title": str, "lede": str, '
    '"points": [{"text": str, "source_ref": str}]}]}\n'
    "One section per narrative beat. eyebrow = 2-4 word kicker. title <= 12 words. "
    "lede <= 30 words. points = the beat's claims, keeping each claim's source_ref. "
    "No markdown, no HTML."
)


def _brand_tokens(brand_id: str) -> dict[str, str]:
    """Resolve {background, text, accent, font} for a brand — defensive, with
    okuro defaults so the page always looks intentional."""
    tok = dict(_DEFAULTS)
    try:
        from okuro.prism.generate import _brand_design
        data = _brand_design(brand_id) or {}
        color = data.get("color") or {}
        pal = color.get("palette") or color
        bg = (pal.get("background") or {})
        fg = (pal.get("foreground") or {})
        if isinstance(bg, dict) and bg.get("base"):
            tok["background"] = bg["base"]
        if isinstance(fg, dict) and fg.get("primary"):
            tok["text"] = fg["primary"]
        if pal.get("accent"):
            tok["accent"] = pal["accent"] if isinstance(pal["accent"], str) else tok["accent"]
        typo = data.get("typography") or {}
        fam = ", ".join(x for x in (typo.get("primary"), typo.get("fallback")) if x)
        if fam:
            tok["font"] = fam
    except Exception as exc:  # noqa: BLE001
        log.info("brand token resolve failed: %s", exc)
    return tok


def _sections_from_brief(content_brief: str, provider: Optional[str]) -> dict[str, Any]:
    """LLM: content brief → structured, audience-fitted page sections."""
    from okuro.bridge.invoke import invoke

    res = invoke(prompt=content_brief, system_prompt=_SECTION_SYSTEM,
                 capability="standard", provider=provider, timeout=180)
    if not (isinstance(res, dict) and res.get("success")):
        raise RuntimeError(f"website copy generation failed: {(res or {}).get('error')}")
    out = res.get("output") or ""
    start, end = out.find("{"), out.rfind("}")
    raw = out[start:end + 1] if start >= 0 and end > start else out
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        import json_repair
        return json_repair.loads(raw)


def build_html(data: dict[str, Any], tokens: dict[str, str]) -> str:
    """Wrap the sections in the fixed scroll-reveal engine + brand tokens."""
    e = html.escape
    hero = data.get("hero") or {}
    sections = data.get("sections") or []

    def point_li(p: dict[str, Any]) -> str:
        src = p.get("source_ref") or ""
        title_attr = f' title="source: {e(str(src))}"' if src else ""
        cite = f'<span class="src">{e(str(src))}</span>' if src else ""
        return f'<li class="reveal"{title_attr}>{e(str(p.get("text", "")))}{cite}</li>'

    body_sections = []
    for s in sections:
        pts = "".join(point_li(p) for p in (s.get("points") or []))
        body_sections.append(f"""
      <section class="slide">
        <p class="eyebrow reveal">{e(str(s.get("eyebrow", "")))}</p>
        <h2 class="reveal">{e(str(s.get("title", "")))}</h2>
        <p class="lede reveal">{e(str(s.get("lede", "")))}</p>
        <ul class="points">{pts}</ul>
      </section>""")

    title = e(str(data.get("title") or "Resonance"))
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg: {tokens['background']}; --fg: {tokens['text']};
    --accent: {tokens['accent']}; --font: {tokens['font']};
    --muted: color-mix(in srgb, var(--fg) 55%, transparent);
  }}
  * {{ box-sizing: border-box; margin: 0; }}
  html {{ scroll-behavior: smooth; }}
  body {{ background: var(--bg); color: var(--fg); font-family: var(--font);
    line-height: 1.7; -webkit-font-smoothing: antialiased; }}
  .slide {{ min-height: 100vh; display: flex; flex-direction: column;
    justify-content: center; max-width: 860px; margin: 0 auto; padding: 8vh 24px; }}
  .hero h1 {{ font-size: clamp(2.4rem, 7vw, 4rem); line-height: 1.05; letter-spacing: -0.02em; }}
  .eyebrow {{ text-transform: uppercase; letter-spacing: 0.25em; font-size: 0.72rem;
    color: var(--accent); margin-bottom: 1rem; }}
  h2 {{ font-size: clamp(1.8rem, 4.5vw, 3rem); line-height: 1.1; letter-spacing: -0.01em; }}
  .lede {{ font-size: 1.25rem; color: var(--muted); margin-top: 1rem; max-width: 70ch; }}
  .points {{ list-style: none; margin-top: 2rem; display: grid; gap: 0.9rem; }}
  .points li {{ position: relative; padding-left: 1.25rem; max-width: 68ch; }}
  .points li::before {{ content: ""; position: absolute; left: 0; top: 0.7em;
    width: 8px; height: 8px; border-radius: 50%; background: var(--accent); }}
  .src {{ display: block; font-size: 0.72rem; color: var(--muted); margin-top: 0.2rem;
    word-break: break-all; }}
  .reveal {{ opacity: 0; transform: translateY(16px);
    transition: opacity 0.5s ease, transform 0.5s ease; transition-delay: var(--rd, 0s); }}
  .reveal.in-view {{ opacity: 1; transform: none; }}
  @media (prefers-reduced-motion: reduce) {{
    .reveal, .reveal.in-view {{ opacity: 1; transform: none; transition: none; }} }}
</style></head><body>
  <section class="slide hero">
    <p class="eyebrow reveal">{e(str(hero.get("eyebrow", "")))}</p>
    <h1 class="reveal">{e(str(hero.get("title") or title))}</h1>
    <p class="lede reveal">{e(str(hero.get("lede", "")))}</p>
  </section>
  {''.join(body_sections)}
  <script>
    (function () {{
      var els = document.querySelectorAll('.reveal');
      els.forEach(function (el, i) {{ el.style.setProperty('--rd', ((i % 6) * 60) + 'ms'); }});
      var io = new IntersectionObserver(function (entries) {{
        entries.forEach(function (en) {{
          if (en.isIntersecting) {{ en.target.classList.add('in-view'); io.unobserve(en.target); }}
        }});
      }}, {{ rootMargin: '-10% 0px -10% 0px', threshold: 0.05 }});
      els.forEach(function (el) {{ io.observe(el); }});
    }})();
  </script>
</body></html>"""


def render_website(content_brief: str, person_id: Optional[str],
                   brand_id: str, provider: Optional[str]) -> dict[str, Any]:
    """Adapter: content brief → self-contained scroll-reveal HTML page.

    Stored as a text/html evidence artifact; served at /api/resonance/site/{id}.
    Signature matches the MEDIA_ADAPTERS contract.
    """
    data = _sections_from_brief(content_brief, provider)
    tokens = _brand_tokens(brand_id)
    page = build_html(data, tokens)

    from okuro.sense.artifacts import artifact_write
    sid = artifact_write(
        kind="evidence", title=str(data.get("title") or "Resonance site"),
        summary="Resonance scroll-reveal website", body=page,
        media_type="text/html", created_by="resonance.website", confidence=0.9)
    return {"id": sid, "url": f"/api/resonance/site/{sid}", "media": "website",
            "title": data.get("title")}
