# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Deterministically snap a generated deck's colors + type to brand tokens and enforce WCAG contrast.
# index:
#   color helpers (hex/rgb, luminance, contrast, ΔE)
#   palette extraction
#   type scale
#   resolve_brand_tokens (entry)
# AGENT_HEADER_END -->
"""Post-generation brand-fidelity resolver for okuro·slides.

Pure functions, no LLM. The LLM is only *suggested* the brand tokens in the
generation prompt, so generated decks drift (wrong accent shade, greys that
fail WCAG contrast on dark backgrounds). This module is the deterministic
correction pass that runs right after `_normalize`, before save:

  1. Palette snap  — every element color/bg that is a near-match of a brand
                     token snaps to the exact token; deck background + any
                     near-accent color snap to the exact brand accent.
  2. WCAG contrast — every text color is checked against its effective
                     background; if it fails 4.5:1 (3:1 for large text) it is
                     nudged toward the brand's high-contrast text token until
                     it passes, preferring the nearest passing brand text tint.
  3. Type scale    — fontSize values snap to the nearest modular-scale step.

Conservative + idempotent: re-running changes nothing; a deck with no brand
design dict passes through byte-identical. Geometry is never touched.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

# Modular type scale — sizes snap to the nearest step for visual consistency.
# Continues into DISPLAY sizes (90–176) so an intentional hero numeral / full-
# bleed oversized figure (the archetype grid.py's full-bleed slot wants) snaps to
# a nearby large step instead of being crushed down to a 76px body-max.
_TYPE_SCALE: tuple[int, ...] = (13, 15, 18, 22, 28, 36, 48, 64, 76, 90, 112, 140, 176)

# Near-match thresholds in CIE76 ΔE (≈2.3 = just-noticeable difference).
# Snap an off-brand color onto the nearest brand token only within this band —
# tight enough that distinct hues stay distinct.
_SNAP_THRESHOLD = 18.0
# Wider band that identifies a color as "essentially the brand accent" → force
# the exact accent. ~40 catches a same-hue accent drift (#22c55e→#8ff0a4, ΔE≈27)
# without pulling in a differently-hued color (blue #7aa2ff is ΔE≈98 away).
_ACCENT_THRESHOLD = 40.0

_CONTRAST_NORMAL = 4.5
_CONTRAST_LARGE = 3.0
_LARGE_FONT_PX = 24


# ── color helpers ────────────────────────────────────────────────────────────

def _parse_color(value: Any) -> Optional[tuple[int, int, int]]:
    """Parse '#rgb' / '#rrggbb' / 'rgb(...)' / 'rgba(...)' → (r,g,b), else None.

    rgba alpha is ignored (we only reason about the opaque chroma). Returns None
    for transparent / unparseable / named colors so callers leave them alone.
    """
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if not s or s == "transparent":
        return None
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            return None
        try:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        except ValueError:
            return None
    if s.startswith("rgb"):
        inside = s[s.find("(") + 1 : s.rfind(")")]
        parts = [p.strip() for p in inside.split(",")]
        if len(parts) < 3:
            return None
        try:
            return tuple(max(0, min(255, int(round(float(p))))) for p in parts[:3])  # type: ignore[return-value]
        except ValueError:
            return None
    return None


def _to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(round(c)))) for c in rgb))


def _is_opaque(value: Any) -> bool:
    """True if the color string is a fully-opaque, parseable chroma (not rgba<1)."""
    if not isinstance(value, str):
        return False
    s = value.strip().lower()
    if s.startswith("rgba"):
        inside = s[s.find("(") + 1 : s.rfind(")")]
        parts = [p.strip() for p in inside.split(",")]
        if len(parts) == 4:
            try:
                return float(parts[3]) >= 0.999
            except ValueError:
                return False
    return _parse_color(value) is not None


def _srgb_channel(c: int) -> float:
    cs = c / 255.0
    return cs / 12.92 if cs <= 0.04045 else ((cs + 0.055) / 1.055) ** 2.4


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * _srgb_channel(r) + 0.7152 * _srgb_channel(g) + 0.0722 * _srgb_channel(b)


def contrast_ratio(c1: Any, c2: Any) -> float:
    """WCAG contrast ratio between two colors, (L1+0.05)/(L2+0.05). 0.0 if either
    is unparseable. #000 vs #fff == 21.0."""
    a, b = _parse_color(c1), _parse_color(c2)
    if a is None or b is None:
        return 0.0
    la, lb = _relative_luminance(a), _relative_luminance(b)
    hi, lo = (la, lb) if la >= lb else (lb, la)
    return (hi + 0.05) / (lo + 0.05)


def _to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """sRGB → CIELAB (D65). Used for hue-aware ΔE — redmean conflates green/blue
    at equal distance, which would mis-snap a blue drift onto a green accent."""
    r, g, b = (_srgb_channel(c) for c in rgb)
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    xn, yn, zn = 0.95047, 1.0, 1.08883

    def f(t: float) -> float:
        return t ** (1.0 / 3.0) if t > 0.008856 else 7.787 * t + 16.0 / 116.0

    fx, fy, fz = f(x / xn), f(y / yn), f(z / zn)
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def _delta_e(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """CIE76 ΔE — Euclidean distance in CIELAB. ~2.3 = just-noticeable."""
    la, lb = _to_lab(a), _to_lab(b)
    return sum((x - y) ** 2 for x, y in zip(la, lb)) ** 0.5


# ── palette extraction ───────────────────────────────────────────────────────

def _walk_hexes(node: Any, out: list[str]) -> None:
    """Collect every parseable color string anywhere under a palette subtree."""
    if isinstance(node, str):
        if _parse_color(node) is not None:
            out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            _walk_hexes(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_hexes(v, out)


def _palette(design: dict[str, Any]) -> dict[str, Any]:
    """Locate the palette block whether design is the raw slot data
    ({visual:{palette}}) or already a {palette/typography} dict."""
    vis = design.get("visual") if isinstance(design.get("visual"), dict) else design
    pal = vis.get("palette") if isinstance(vis, dict) else None
    return pal if isinstance(pal, dict) else {}


def _brand_model(design: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Build the working model from a brand design dict, or None if no palette.

    {
      accent:      "#rrggbb" | None,
      background:   "#rrggbb" | None,          # deck base background
      allowed_raw:  ["#..", "rgba(..)", ...],  # snap target token strings
      text_tints:   [(hex, rgb)],              # foreground tints for contrast
    }
    """
    pal = _palette(design)
    if not pal:
        return None

    allowed_raw: list[str] = []
    _walk_hexes(pal, allowed_raw)
    if not allowed_raw:
        return None

    accent = pal.get("accent") if _parse_color(pal.get("accent")) else None

    bg_block = pal.get("background")
    background = None
    if isinstance(bg_block, dict):
        background = bg_block.get("base") or bg_block.get("subtle") or bg_block.get("elevated")
    if not _parse_color(background):
        background = None

    fg_block = pal.get("foreground")
    text_tints: list[tuple[str, tuple[int, int, int]]] = []
    if isinstance(fg_block, dict):
        for key in ("primary", "secondary", "tertiary", "inverse", "disabled"):
            v = fg_block.get(key)
            rgb = _parse_color(v)
            if rgb is not None:
                text_tints.append((str(v), rgb))
    # de-dup raw tokens preserving order
    seen: set[str] = set()
    allowed_raw = [c for c in allowed_raw if not (c in seen or seen.add(c))]

    return {
        "accent": accent,
        "background": background,
        "allowed_raw": allowed_raw,
        "text_tints": text_tints,
    }


def _snap_color(value: Any, model: dict[str, Any]) -> Any:
    """Snap one color to the nearest brand token within threshold. Already-exact
    tokens and unparseable values are returned unchanged (idempotent)."""
    rgb = _parse_color(value)
    if rgb is None:
        return value

    # Exact accent already → leave.
    accent = model.get("accent")
    if accent and isinstance(value, str) and value.strip().lower() == accent.strip().lower():
        return value

    # Near-accent → force to exact accent.
    if accent and _delta_e(rgb, _parse_color(accent)) <= _ACCENT_THRESHOLD:
        return accent

    # General snap to nearest allowed token.
    best_tok: Optional[str] = None
    best_d = _SNAP_THRESHOLD
    for tok in model["allowed_raw"]:
        trgb = _parse_color(tok)
        if trgb is None:
            continue
        d = _delta_e(rgb, trgb)
        if d == 0.0:
            return value  # already exactly a token
        if d < best_d:
            best_d, best_tok = d, tok
    return best_tok if best_tok is not None else value


# ── type scale ───────────────────────────────────────────────────────────────

def _snap_font_size(size: Any) -> Any:
    if not isinstance(size, (int, float)) or isinstance(size, bool):
        return size
    return min(_TYPE_SCALE, key=lambda step: abs(step - size))


# ── contrast correction ──────────────────────────────────────────────────────

def _effective_bg(el: dict[str, Any], underlying_bg: str, deck_bg: str) -> str:
    """The background actually behind this element's text."""
    own = el.get("bg")
    if _is_opaque(own):
        return own  # type: ignore[return-value]
    if _is_opaque(underlying_bg):
        return underlying_bg
    return deck_bg


def _required_ratio(el: dict[str, Any]) -> float:
    fs = el.get("fontSize")
    if isinstance(fs, (int, float)) and not isinstance(fs, bool) and fs >= _LARGE_FONT_PX:
        return _CONTRAST_LARGE
    return _CONTRAST_NORMAL


def _fix_contrast(color: Any, bg: str, model: dict[str, Any], need: float) -> Any:
    """If `color` fails `need` against `bg`, return the nearest brand text tint
    that passes (or a luminance-nudged version that passes). Untouched if it
    already passes or is unparseable."""
    rgb = _parse_color(color)
    if rgb is None:
        return color
    if contrast_ratio(color, bg) >= need:
        return color  # already passes — never alter

    # Prefer the nearest brand text tint that passes.
    passing = [
        (tint, _delta_e(rgb, trgb))
        for tint, trgb in model["text_tints"]
        if contrast_ratio(tint, bg) >= need
    ]
    if passing:
        passing.sort(key=lambda p: p[1])
        return passing[0][0]

    # No brand tint passes (rare) → nudge lightness toward the most-contrasting
    # direction until it passes. Pure black/white as terminal fallback.
    bg_rgb = _parse_color(bg)
    if bg_rgb is None:
        return color
    target = (255, 255, 255) if _relative_luminance(bg_rgb) < 0.5 else (0, 0, 0)
    cur = list(rgb)
    for _ in range(20):
        cur = [cur[i] + (target[i] - cur[i]) * 0.2 for i in range(3)]
        cand = _to_hex((round(cur[0]), round(cur[1]), round(cur[2])))
        if contrast_ratio(cand, bg) >= need:
            return cand
    return _to_hex(target)


# ── entry ────────────────────────────────────────────────────────────────────

def _resolve_element(el: dict[str, Any], model: dict[str, Any], underlying_bg: str, deck_bg: str) -> None:
    """Mutate one element in place: snap palette, snap type, fix text contrast.
    Recurses into frame children (effective bg propagates through opaque frames)."""
    if not isinstance(el, dict):
        return

    if "bg" in el:
        el["bg"] = _snap_color(el["bg"], model)
    if "color" in el:
        el["color"] = _snap_color(el["color"], model)
    if "fontSize" in el:
        el["fontSize"] = _snap_font_size(el["fontSize"])

    # Contrast (text only) against the effective background behind it.
    if el.get("kind") == "text" and "color" in el:
        eff = _effective_bg(el, underlying_bg, deck_bg)
        el["color"] = _fix_contrast(el["color"], eff, model, _required_ratio(el))

    if el.get("kind") == "frame":
        child_bg = el["bg"] if _is_opaque(el.get("bg")) else underlying_bg
        for child in el.get("children") or []:
            _resolve_element(child, model, child_bg, deck_bg)


def resolve_brand_tokens(deck: dict[str, Any], design: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Return a brand-corrected copy of `deck`.

    - No brand design dict (None/{}/no palette) → deck returned unchanged.
    - Palette snap + WCAG contrast + type-scale snap, conservatively.
    - Idempotent: resolve(resolve(deck)) == resolve(deck).
    Never raises on a malformed deck — best-effort, geometry untouched.
    """
    if not isinstance(deck, dict) or not isinstance(design, dict) or not design:
        return deck
    model = _brand_model(design)
    if model is None:
        return deck

    out = copy.deepcopy(deck)

    # Force the deck background to the exact brand base background.
    if model.get("background"):
        out["background"] = model["background"]
    deck_bg = out.get("background") or model.get("background") or "#000000"

    for slide in out.get("slides") or []:
        if not isinstance(slide, dict):
            continue
        # Underlying bg = topmost opaque box covering the slide, else deck bg.
        underlying = deck_bg
        for el in slide.get("elements") or []:
            if isinstance(el, dict) and el.get("kind") == "box" and _is_opaque(el.get("bg")):
                underlying = el["bg"]
        for el in slide.get("elements") or []:
            _resolve_element(el, model, underlying, deck_bg)

    return out
