# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: PRISM v4 W5 Phase D — MULTI-PERSPECTIVE lens overlays + luminance-aware
#   brand logo. The audience-translation engine (Phase B) re-words a topic's ladder
#   per persona keeping structure identical (clean-insertion); this module folds
#   each persona's rewritten ladders into the base DeckDoc as per-cell ``lensSlides``
#   overlays and emits the ``lenses`` tab descriptors — so ONE deck carries the
#   three-perspective framing the hand-authored reference board did, switchable in the
#   viewer. Also resolves a brand's logo into a luminance-aware ``brandLogo`` (real
#   registered asset embedded as a data-URI when present, else a monochrome
#   wordmark synthesized in both ink polarities), self-contained for offline export.
# index: attach_lens_overlays | resolve_brand_logo | synthesize_brand_logo
# AGENT_HEADER_END -->
"""Lens overlays + brand logo for the Phase-D benchmark deck.

``attach_lens_overlays`` is a pure structural merge — it never re-authors; the
per-persona ladders were already rewritten + entailment-gated upstream, and their
structure is byte-identical to the base (same claims/ids/counts), so the same
``_cell_for_level`` path that built the base cell builds each lens overlay.
"""
from __future__ import annotations

import base64
import html
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# deck brand id -> registry brand id (registry keeps display casing) + wordmark text.
_BRAND_NAME = {"northwind": "Northwind", "meridian": "Meridian", "okuro": "okuro"}
_REGISTRY_ID = {"northwind": "Northwind", "meridian": "Meridian", "okuro": "okuro"}


# ── multi-perspective lens overlays ───────────────────────────────────────────


def attach_lens_overlays(
    doc: dict[str, Any],
    lens_variants: list[tuple[dict[str, Any], list[Any]]],
    *,
    inplace: bool = True,
) -> dict[str, Any]:
    """Fold per-persona rewritten ladders into ``doc`` as per-cell ``lensSlides``
    overlays and set ``doc['lenses']``.

    ``lens_variants`` = ``[(lens, ladders), ...]`` where ``lens`` is a
    ``{"id", "persona": {"mark","name","lens"}}`` descriptor and ``ladders`` is the
    persona's rewritten LadderDoc list (parallel to ``doc['topics']``). The overlay
    slides come from the SAME adapter path as the base cell, so archetype + level
    layout are identical and only the wording differs.
    """
    from okuro.prism.authoring.to_deckdoc import _LADDER_TO_DECK, _cell_for_level

    target = doc if inplace else {**doc}
    topics = target.get("topics") or []
    for lens, ladders in lens_variants:
        lid = str(lens.get("id") or lens.get("persona", {}).get("name") or "lens")
        for i, ladder in enumerate(ladders):
            if i >= len(topics):
                break
            num = f"{i + 1:02d}"
            levels = topics[i].get("levels") or {}
            for lvl, deck_lvl in _LADDER_TO_DECK.items():
                cell = levels.get(deck_lvl)
                if not cell or lvl not in ladder.units:
                    continue               # honest level-collapse: skip absent levels
                slide = _cell_for_level(ladder, lvl, num)["slide"]
                cell.setdefault("lensSlides", {})[lid] = slide
    target["lenses"] = [
        {"id": str(lens.get("id") or lens.get("persona", {}).get("name") or "lens"),
         "persona": lens.get("persona") or {"mark": "a", "name": lid, "lens": ""}}
        for lens, _ in lens_variants
    ]
    return target


# ── luminance-aware brand logo ────────────────────────────────────────────────

_MIME_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml"}


def _wordmark_svg(name: str, ink: str) -> str:
    """A clean monochrome wordmark in ``ink`` — a mark square + the brand name. The
    logo IS the brand asset; the two ink polarities are what makes placement
    luminance-aware. Self-contained (no external refs), export-safe."""
    safe = html.escape(name or "brand")
    w = 44 + max(3, len(safe)) * 13
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} 44" '
        f'role="img" aria-label="{safe} logo">'
        f'<rect x="6" y="13" width="18" height="18" rx="2" fill="{ink}"/>'
        f'<text x="34" y="29" font-family="ui-monospace,SFMono-Regular,Menlo,monospace" '
        f'font-size="21" font-weight="700" letter-spacing="0.5" fill="{ink}">{safe}</text>'
        f"</svg>"
    )


def _embed_asset_svg(path: str, mime: str) -> Optional[str]:
    """Wrap a raster/vector brand-asset file into a self-contained inline <svg> via a
    data-URI <image>, so an exported deck renders it offline. Returns None on any
    read error (caller falls back to the wordmark)."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if mime == "image/svg+xml":
        # already vector — return the SVG bytes directly (must start with <svg).
        txt = raw.decode("utf-8", "ignore").strip()
        return txt if txt.startswith("<svg") else None
    b64 = base64.b64encode(raw).decode("ascii")
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 60" role="img" aria-label="logo">'
        f'<image href="data:{mime};base64,{b64}" x="0" y="0" width="200" height="60" '
        'preserveAspectRatio="xMinYMid meet"/></svg>'
    )


def synthesize_brand_logo(brand: str, *, corner: str = "tl") -> dict[str, Any]:
    """A luminance-aware wordmark logo (no registered asset needed). ``dark`` is the
    logo for a DARK canvas (light ink); ``light`` for a LIGHT canvas (dark ink)."""
    name = _BRAND_NAME.get(brand, brand or "okuro")
    return {"corner": corner,
            "dark": _wordmark_svg(name, "#f4f4f5"),
            "light": _wordmark_svg(name, "#0a0a0a")}


def resolve_brand_logo(brand: str, *, corner: str = "tl") -> dict[str, Any]:
    """Resolve a brand's logo into a luminance-aware ``brandLogo``. Prefers a
    registered logo asset (embedded self-contained), picking a bg_target='dark'
    asset for the dark polarity and 'light' for the light polarity; falls back to a
    'both' asset, then to a synthesized monochrome wordmark for any missing
    polarity — so the deck always ships a luminance-aware logo."""
    logos: list[Any] = []
    try:
        from okuro.brand_assets.storage import list_assets
        logos = list_assets(brand, "logo") or list_assets(_REGISTRY_ID.get(brand, brand), "logo")
    except Exception as exc:
        logger.warning("resolve_brand_logo: asset lookup failed (%s) — wordmark", exc)

    if not logos:
        return synthesize_brand_logo(brand, corner=corner)

    def pick(target: str) -> Optional[Any]:
        return (next((l for l in logos if l.bg_target == target), None)
                or next((l for l in logos if l.bg_target == "both"), None)
                or logos[0])

    wm = synthesize_brand_logo(brand, corner=corner)
    out = {"corner": corner, "dark": wm["dark"], "light": wm["light"]}
    for polarity in ("dark", "light"):
        a = pick(polarity)
        if a:
            ext = Path(a.path).suffix.lower()
            svg = _embed_asset_svg(a.path, a.mime or _MIME_BY_EXT.get(ext, "image/png"))
            if svg:
                out[polarity] = svg
    return out


__all__ = ["attach_lens_overlays", "resolve_brand_logo", "synthesize_brand_logo"]
