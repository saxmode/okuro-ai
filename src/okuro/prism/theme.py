# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Resolve an okuro stored brand into a complete, validated prism deck
#   theme (kit-structural formulas ⊕ brand identity tokens → flat CSS-var set).
# index:
#   ResolveError / ResolvedTheme
#   token_manifest
#   color helpers
#   derivations (surfaces / foreground / accent / semantic / borders / chart /
#     typography / geometry / motion / shadows)
#   validation (AA contrast / ramp monotonicity / completeness)
#   resolve_deck_theme (entry)
#   __main__ debug CLI
# AGENT_HEADER_END -->
"""prism deck-theme resolver — the kit ⊕ brand inheritance layer.

`resolve_brand` (stack/registry.py) is a raw slot fan-out: no merge, no
derivation, no fallback, no validation — a thin brand yields a thin, hole-ridden
theme. This module is the kit layer that sits on top of it (per the inheritance
contract artifact 0c8410b4 §5): it takes a stored brand + a prism kit family and
returns ONE flat, fully-populated, WCAG-AA-validated token set that a deck can
render from without any runtime styling guesswork.

Precedence (contract §3.1, highest wins):
    brand explicit token → kit-derived-from-brand-primitive
        → kit-structural default → global literal

Structural formulas (elevation ramp, contrast ladder, accent alphas, border
ramp) come from the board kit spec (artifact 37c7a92a §1/§5). Brand supplies
identity: accent hue, base polarity, fonts, status hues, radius magnitude.

The `mode` argument controls polarity. `None` infers from the brand base
luminance. Forcing a mode opposite the brand's natural polarity (e.g. a dark
brand with `mode="light"`) keeps the brand's identity tokens (accent, fonts,
status hues) but derives fresh surfaces/foreground for the requested polarity —
that is how a light deck is derived from a dark brand.

Color math (contrast_ratio, _fix_contrast, parsing) is reused from
okuro.slides.brand_resolver — same package, single source of truth for WCAG.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from okuro.slides.brand_resolver import (
    _fix_contrast,
    _parse_color,
    _relative_luminance,
    _to_hex,
    contrast_ratio,
)
from okuro.stack.registry import resolve_brand


class ResolveError(ValueError):
    """Raised when a theme cannot be resolved to the kit's complete token set."""


# ── structural constants (kit-owned, brand-independent — board kit spec §5b) ──

_MID_GRAY = "#808080"

# Global literal fallbacks (last resort so a render never crashes — contract
# §3.1 step 4). Chosen restrained/neutral so a brand-less deck is valid, unbranded.
_STRUCTURAL_BASE = {"dark": "#0a0a0a", "light": "#ffffff"}
_KIT_DEFAULT_ACCENT = "#5b8def"  # neutral professional blue for a brand-less deck
_KIT_MONO_STACK = (
    '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, '
    '"Liberation Mono", monospace'
)
_KIT_SANS_STACK = (
    '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, '
    "system-ui, sans-serif"
)

# Semantic / categorical hues the kit owns when a brand lacks them (spec §1d/§1e).
_KIT_WARN = "#ef4444"
_KIT_INFO = "#3b82f6"
_KIT_OK = "#16a34a"
_KIT_GOLD = "#f59e0b"
_KIT_ROSE = "#ec4899"
_KIT_VIOLET = "#a855f7"

# Type-scale role sizes are structural (spec §5b: ratios/sizes are kit-owned;
# only the font FAMILY inherits from the brand). Replaces the hardcoded
# brand_resolver._TYPE_SCALE at the theme layer (contract §3.2).
_TYPE_ROLES = {
    "--fs-eyebrow": "11px",
    "--fs-label": "14px",
    "--fs-body": "17px",
    "--fs-h3": "21px",
    "--fs-lede": "22px",
    "--fs-h2": "38px",
    "--fs-title": "56px",
    "--fs-hero": "64px",
}

# Geometry constants (spec §1i / §5b — width modes & base unit are structural).
_GEOMETRY = {
    "--ms": "8px",
    "--gutter": "64px",
    "--reading-w": "860px",
    "--wide-w": "1480px",
    "--slide-w": "1600px",
    "--nav-h": "56px",
}

_ELEV_STEP = 0.06  # fraction toward mid-gray per elevation step (spec §5 formula)

# The complete token set the "board" kit family requires. A2 consumes this as
# the contract for what tokens.css may reference. Grouped for readability only.
_BOARD_MANIFEST: tuple[str, ...] = (
    # surfaces (elevation ramp)
    "--bg-base", "--bg-elev", "--bg-card", "--bg-subtle", "--bg-overlay",
    # foreground contrast ladder
    "--fg-primary", "--fg-body", "--fg-secondary", "--fg-tertiary", "--fg-muted",
    # accent + alphas + readability companions
    "--accent", "--accent-hover", "--accent-dim", "--accent-soft",
    "--accent-text", "--accent-ink",
    # semantic / status (+ AA-safe text companions)
    "--warn", "--warn-dim", "--info", "--info-dim",
    "--ok", "--gold", "--rose", "--violet",
    "--warn-text", "--info-text", "--ok-text", "--gold-text",
    "--violet-text", "--rose-text",
    # borders
    "--border", "--border-strong", "--border-soft",
    # chart / categorical
    "--chart-1", "--chart-2", "--chart-3", "--chart-4", "--chart-5", "--chart-6",
    # typography families
    "--font-body", "--font-mono",
    # type scale
    *(_TYPE_ROLES.keys()),
    # geometry
    *(_GEOMETRY.keys()),
    # radii
    "--radius-chip", "--radius-input", "--radius-card", "--radius-story",
    "--radius-pill",
    # motion
    "--motion-fast", "--motion-medium", "--motion-slow", "--motion-reveal",
    "--ease",
    # shadows
    "--shadow-xs", "--shadow-sm", "--shadow-md", "--shadow-lg", "--shadow-xl",
)

_MANIFESTS: dict[str, tuple[str, ...]] = {"board": _BOARD_MANIFEST}


def token_manifest(kit_family: str = "board") -> tuple[str, ...]:
    """The complete required-token list for a kit family. A2 depends on this."""
    try:
        return _MANIFESTS[kit_family]
    except KeyError as exc:
        raise ResolveError(
            f"unknown kit family {kit_family!r}; known: {sorted(_MANIFESTS)}"
        ) from exc


# ── color helpers (built on brand_resolver primitives) ────────────────────────

def _lum(color: Optional[str]) -> float:
    rgb = _parse_color(color)
    return _relative_luminance(rgb) if rgb is not None else 0.0


def _mix(a: str, b: str, t: float) -> str:
    """Linear sRGB blend a→b by fraction t∈[0,1]. Deterministic, idempotent."""
    ra, rb = _parse_color(a), _parse_color(b)
    if ra is None or rb is None:
        return a
    t = max(0.0, min(1.0, t))
    return _to_hex(tuple(ra[i] + (rb[i] - ra[i]) * t for i in range(3)))


def _rgba(color: str, alpha: float) -> str:
    rgb = _parse_color(color)
    if rgb is None:
        return color
    a = round(max(0.0, min(1.0, alpha)), 3)
    return f"rgba({rgb[0]}, {rgb[1]}, {rgb[2]}, {a})"


def _is_monospace(primary: str, fallback: str) -> bool:
    return "mono" in f"{primary} {fallback}".lower()


def _pget(d: Any, *path: str) -> Any:
    """Safe nested lookup; returns None if any hop is missing/not a dict."""
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


# ── derivations ───────────────────────────────────────────────────────────────

def _surfaces(pal: dict, base: str, use_brand: bool) -> dict[str, str]:
    """Elevation ramp: mix base toward mid-gray by fixed steps (spec §5 formula,
    §1a). One formula covers both polarities — dark lightens, light darkens —
    which guarantees a monotonic ramp regardless of brand token naming.

    Brand background.elevated/subtle are NOT force-fitted: measured, their order
    inverts the kit ramp (brand subtle #141414 < elevated #1a1a1a, but kit subtle
    must be the LIGHTEST step), so seating them by name would break monotonicity.
    Only base and overlay are inherited when the brand's polarity matches.
    """
    out = {
        "--bg-base": base,
        "--bg-elev": _mix(base, _MID_GRAY, _ELEV_STEP),
        "--bg-card": _mix(base, _MID_GRAY, _ELEV_STEP * 2),
        "--bg-subtle": _mix(base, _MID_GRAY, _ELEV_STEP * 3),
    }
    overlay = _pget(pal, "background", "overlay") if use_brand else None
    out["--bg-overlay"] = overlay if isinstance(overlay, str) else _rgba(out["--bg-elev"], 0.96)
    return out


def _foreground(pal: dict, base: str, use_brand: bool) -> dict[str, str]:
    """5-step contrast ladder, roles fixed by rank (spec §1b/§5b). Brand-named
    tints win when polarity matches; the missing `body` step is always derived
    as the midpoint between primary and secondary. When surfaces were flipped
    (use_brand=False) the brand tints are for the wrong polarity, so the whole
    ladder is interpolated fresh from a max-contrast anchor toward base.
    """
    dark = _lum(base) < 0.5
    anchor = "#f2f2f2" if dark else "#0a0a0a"  # max-contrast endpoint

    def brand(key: str) -> Optional[str]:
        if not use_brand:
            return None
        v = _pget(pal, "foreground", key)
        return v if isinstance(v, str) and _parse_color(v) else None

    primary = brand("primary") or anchor
    secondary = brand("secondary") or _mix(primary, base, 0.34)
    tertiary = brand("tertiary") or _mix(primary, base, 0.55)
    muted = brand("disabled") or brand("muted") or _mix(primary, base, 0.74)
    body = _mix(primary, secondary, 0.5)  # brand has no body role — always derived
    return {
        "--fg-primary": primary,
        "--fg-body": body,
        "--fg-secondary": secondary,
        "--fg-tertiary": tertiary,
        "--fg-muted": muted,
    }


def _accent(pal: dict, base: str) -> dict[str, str]:
    """Accent hue is brand identity; hover + alphas are derived (spec §1c: dim/
    soft are computed by alpha, never stored)."""
    accent = _pget(pal, "accent")
    accent = accent if isinstance(accent, str) and _parse_color(accent) else _KIT_DEFAULT_ACCENT
    hover = _pget(pal, "accent_hover")
    hover = hover if isinstance(hover, str) and _parse_color(hover) else _mix(accent, "#000000", 0.14)
    return {
        "--accent": accent,
        "--accent-hover": hover,
        "--accent-dim": _rgba(accent, 0.15),
        "--accent-soft": _rgba(accent, 0.06),
    }


def _semantic(pal: dict, accent: str, mode: str) -> dict[str, str]:
    """Status hues from the brand where present, else kit hues (spec §1d). The
    light-mode --ok rule: a light/warm accent cannot signal success, so --ok is
    always an explicit green distinct from the accent."""
    warn = _pget(pal, "status", "error") or _KIT_WARN
    info = _pget(pal, "status", "info") or _KIT_INFO
    ok = _pget(pal, "status", "success") or _KIT_OK
    gold = _pget(pal, "status", "warning") or _KIT_GOLD
    # light-mode --ok guard: never let --ok collapse onto a non-green accent.
    if mode == "light" and contrast_ratio(ok, accent) < 1.2:
        ok = _KIT_OK
    return {
        "--warn": warn,
        "--warn-dim": _rgba(warn, 0.12),
        "--info": info,
        "--info-dim": _rgba(info, 0.12),
        "--ok": ok,
        "--gold": gold,
        "--rose": _KIT_ROSE,
        "--violet": _KIT_VIOLET,
    }


def _borders(pal: dict, base: str, fg_primary: str, use_brand: bool) -> dict[str, str]:
    """3-step ramp soft<default<strong in contrast-against-base (spec §1f/§5b)."""
    def brand(key: str) -> Optional[str]:
        if not use_brand:
            return None
        v = _pget(pal, "borders", key)
        return v if isinstance(v, str) and _parse_color(v) else None

    return {
        "--border": brand("default") or _mix(base, fg_primary, 0.14),
        "--border-strong": brand("hover") or _mix(base, fg_primary, 0.24),
        "--border-soft": brand("subtle") or _mix(base, fg_primary, 0.08),
    }


def _chart(sem: dict[str, str], accent: str) -> dict[str, str]:
    """Categorical palette. Neither stored brand carries one (contract §4), so
    derive 6 distinct hues seeded from the resolved accent + semantic set."""
    hues = [accent, sem["--info"], sem["--gold"], sem["--violet"], sem["--rose"], sem["--ok"]]
    return {f"--chart-{i + 1}": h for i, h in enumerate(hues)}


def _typography(typo: dict) -> dict[str, str]:
    """Body family inherits from the brand; mono is the brand's mono if it has
    one, the brand family if it is itself monospace, else a kit mono fallback
    (spec §1g, contract §3.2)."""
    primary = _pget(typo, "primary") or ""
    fallback = _pget(typo, "fallback") or _KIT_SANS_STACK
    body = f'"{primary}", {fallback}' if primary else _KIT_SANS_STACK
    declared_mono = _pget(typo, "mono")
    if isinstance(declared_mono, str) and declared_mono:
        mono = declared_mono if '"' in declared_mono else f'"{declared_mono}", {_KIT_MONO_STACK}'
    elif primary and _is_monospace(primary, str(fallback)):
        mono = body
    else:
        mono = _KIT_MONO_STACK
    return {"--font-body": body, "--font-mono": mono, **_TYPE_ROLES}


def _radii(layout: dict) -> dict[str, str]:
    """Radius magnitude inherits from the brand (spec §5a); the chip/pill anchors
    are structural."""
    return {
        "--radius-chip": "3px",
        "--radius-input": _pget(layout, "radius_sm") or "4px",
        "--radius-card": _pget(layout, "radius_md") or "8px",
        "--radius-story": _pget(layout, "radius_lg") or "14px",
        "--radius-pill": "999px",
    }


def _motion(layout: dict) -> dict[str, str]:
    """UI timing + easing. Brand motion overrides; kit defaults fill the gap
    (spec §1i: UI 0.12–0.18s, reveal 0.5s)."""
    m = layout.get("motion") if isinstance(layout, dict) else None
    m = m if isinstance(m, dict) else {}
    return {
        "--motion-fast": m.get("fast") or "150ms",
        "--motion-medium": m.get("medium") or "300ms",
        "--motion-slow": m.get("slow") or "500ms",
        "--motion-reveal": "500ms",
        "--ease": m.get("easing") or "cubic-bezier(0.2, 0, 0.2, 1)",
    }


def _shadows(layout: dict, mode: str) -> dict[str, str]:
    """5-step elevation shadow ramp. Brand ramp wins; else derive from base
    polarity — darker base → higher-alpha black shadows (spec §3.2)."""
    sh = layout.get("shadows") if isinstance(layout, dict) else None
    steps = ("xs", "sm", "md", "lg", "xl")
    if isinstance(sh, dict) and all(isinstance(sh.get(k), str) for k in steps):
        return {f"--shadow-{k}": sh[k] for k in steps}
    scale = 0.5 if mode == "light" else 1.0

    def a(x: float) -> float:
        return round(x * scale, 2)

    return {
        "--shadow-xs": f"0 1px 2px 0 rgba(0,0,0,{a(0.3)})",
        "--shadow-sm": f"0 1px 3px 0 rgba(0,0,0,{a(0.4)}), 0 1px 2px -1px rgba(0,0,0,{a(0.3)})",
        "--shadow-md": f"0 4px 6px -1px rgba(0,0,0,{a(0.5)}), 0 2px 4px -2px rgba(0,0,0,{a(0.4)})",
        "--shadow-lg": f"0 10px 15px -3px rgba(0,0,0,{a(0.6)}), 0 4px 6px -4px rgba(0,0,0,{a(0.5)})",
        "--shadow-xl": f"0 20px 25px -5px rgba(0,0,0,{a(0.7)}), 0 8px 10px -6px rgba(0,0,0,{a(0.6)})",
    }


# ── v2 design-system consumption (canonical brand model) ──────────────────────
# A v2 brand carries a design_system block: foreground/background, accent per
# mode, ONE font, a 15-step darkening (black) + lightening (white) ramp, chart.
# Elevation + fg ladders are DERIVED by applying the ramps — replacing the
# legacy luminance-delta formulas, which remain the fallback for legacy brands.

_V2_REQUIRED = ("foreground", "background", "font", "white_ramp", "black_ramp")


def _is_v2(ds: Any) -> bool:
    """True if `ds` is a complete-enough v2 design_system to derive from."""
    if not isinstance(ds, dict) or not all(ds.get(k) for k in _V2_REQUIRED):
        return False
    for key in ("white_ramp", "black_ramp"):
        r = ds.get(key)
        if not isinstance(r, list) or len(r) < 5 or not _parse_color(r[0]):
            return False
    return True


def _surfaces_from_ramp(ds: dict, base: str, mode: str) -> dict[str, str]:
    """Elevation ramp sliced from the brand's stored neutral scale (canonical
    model: apply the brand ramp to bg). Dark base rises via the white ramp,
    light base via the black ramp; low indices keep raised surfaces subtle."""
    ramp = ds["white_ramp"] if mode == "dark" else ds["black_ramp"]

    def step(i: int) -> str:
        v = ramp[i] if i < len(ramp) else ramp[-1]
        return v if _parse_color(v) else base

    out = {
        "--bg-base": base,
        "--bg-elev": step(1),
        "--bg-card": step(2),
        "--bg-subtle": step(3),
    }
    out["--bg-overlay"] = _rgba(out["--bg-elev"], 0.96)
    return out


def _foreground_v2(ds: dict, base: str, use_brand: bool, ref_surface: str) -> dict[str, str]:
    """fg ladder: contrast-TARGETED steps from `primary` toward `base`, measured
    against the WORST-CASE surface (`ref_surface` = the most-elevated card) so
    text stays AA on base AND on cards, both polarities. Each role is the
    most-de-emphasized tint still meeting its floor. When surfaces were flipped
    (use_brand=False) the stored fg is the wrong pole, so anchor on a max-contrast
    endpoint for the polarity."""
    dark = _lum(base) < 0.5
    anchor = "#f2f2f2" if dark else "#0a0a0a"
    fg = ds.get("foreground")
    primary = fg if use_brand and _parse_color(fg) else anchor
    out = {"--fg-primary": primary}
    for role, target in _FG_TARGETS:
        out[role] = _step_to_contrast(base, primary, target, ref_surface)
    return out


def _accent_v2(ds: dict, mode: str) -> dict[str, str]:
    """accent_dark/accent_light map to mode; hover + alphas derived (unchanged)."""
    picked = ds.get("accent_dark") if mode == "dark" else ds.get("accent_light")
    accent = picked if isinstance(picked, str) and _parse_color(picked) else _KIT_DEFAULT_ACCENT
    return {
        "--accent": accent,
        "--accent-hover": _mix(accent, "#000000", 0.14),
        "--accent-dim": _rgba(accent, 0.15),
        "--accent-soft": _rgba(accent, 0.06),
    }


def _chart_v2(ds: dict, sem: dict[str, str], accent: str) -> dict[str, str]:
    """Chart set from the brand; padded from derived hues to a full 6."""
    stored = [c for c in (ds.get("chart") or []) if isinstance(c, str) and _parse_color(c)]
    derived = list(_chart(sem, accent).values())
    hues = (stored + [h for h in derived if h not in stored])[:6]
    while len(hues) < 6:
        hues.append(derived[len(hues) % len(derived)])
    return {f"--chart-{i + 1}": h for i, h in enumerate(hues)}


def _typography_v2(ds: dict) -> dict[str, str]:
    """Single brand font → --font-body; --font-mono is the brand font when it is
    itself monospace, else the kit mono fallback (the v2 set has NO mono field —
    machine-data mono is kit-structural, not brand-declared)."""
    font = ds.get("font") or ""
    fallback = ds.get("font_fallback") or _KIT_SANS_STACK
    body = f'"{font}", {fallback}' if font else _KIT_SANS_STACK
    mono = body if font and _is_monospace(font, str(fallback)) else _KIT_MONO_STACK
    return {"--font-body": body, "--font-mono": mono, **_TYPE_ROLES}


# ── text-role readability (AA) ────────────────────────────────────────────────
# Raw accent/semantic colors are fill/border/mark colors and are NOT guaranteed
# legible as TEXT (a light color on a light surface fails AA — a saturated
# yellow such as #ffd400 on white is ≈ 1.5:1). Every colored token that can be TEXT gets a `-text` companion
# stepped (hue-preserving) toward black/white until it reads on the WORST-CASE
# surface — the most-elevated --bg-subtle, farthest from base, so a pass there
# guarantees a pass on base/elev/card too. Meridian precedent: darker gold for text
# (spec §1d). Contrast is polarity-asymmetric, which is why a single fixed step
# (or a --bg-base-only target) undershoots on light themes.

def _readable_on(color: str, surface: str, need: float = 4.5) -> str:
    """`color` stepped toward black (on a light surface) / white (on a dark one)
    until it reaches `need`:1 against `surface`. Unchanged when it already
    passes; best-effort (ramp end) if unreachable."""
    if contrast_ratio(color, surface) >= need:
        return color
    target = "#ffffff" if _lum(surface) < 0.5 else "#000000"
    cur = color
    for i in range(1, 21):
        cur = _mix(color, target, i * 0.05)
        if contrast_ratio(cur, surface) >= need:
            return cur
    return cur


def _accent_ink(accent: str, need: float = 4.5) -> str:
    """The readable text/ink color to sit ON a raw --accent fill: whichever of
    near-black / white has the most contrast on the accent (preferring a pass)."""
    cands = sorted(("#0a0a0a", "#ffffff"), key=lambda c: -contrast_ratio(c, accent))
    for c in cands:
        if contrast_ratio(c, accent) >= need:
            return c
    return cands[0]


def _step_to_contrast(base: str, pole: str, need: float, ref: Optional[str] = None) -> str:
    """The color CLOSEST to `base` on the base→pole axis that still reaches
    `need`:1 against `ref` (default `base`) — the most-de-emphasized tint meeting
    the floor. Used for a contrast-TARGETED fg ladder that holds on both
    polarities AND on the worst-case surface (fixed mix fractions undershoot
    tertiary/muted on light backgrounds, and base-only targets fail on cards)."""
    ref = ref or base
    if contrast_ratio(pole, ref) < need:
        return pole  # even the pole can't reach it — use max contrast
    lo, hi, best = 0.0, 1.0, pole
    for _ in range(24):
        mid = (lo + hi) / 2
        cand = _mix(base, pole, mid)
        if contrast_ratio(cand, ref) >= need:
            best, hi = cand, mid
        else:
            lo = mid
    return best


# Contrast floors per fg role (vs --bg-base). muted is the sole sub-AA role:
# decorative / ≥14px only (spec §1b/§6). body/secondary/tertiary are AA-normal
# safe at any size — so a small label may use --fg-tertiary, never --fg-muted.
_FG_TARGETS = (
    ("--fg-body", 8.0),
    ("--fg-secondary", 5.5),
    ("--fg-tertiary", 4.5),
    ("--fg-muted", 3.0),
)


# ── validation (contract §3.4) ────────────────────────────────────────────────

# text-role → surface pairs and their WCAG floor. tertiary/muted carry the a11y
# rule (spec §1b/§6): decorative or ≥14px, so a 3.0 large-text floor.
_AA_PAIRS: tuple[tuple[str, str, float], ...] = (
    ("--fg-primary", "--bg-base", 4.5),
    ("--fg-body", "--bg-base", 4.5),
    ("--fg-secondary", "--bg-base", 4.5),
    ("--fg-primary", "--bg-card", 4.5),
    ("--fg-body", "--bg-card", 4.5),
    ("--fg-tertiary", "--bg-base", 3.0),
)


def _validate_contrast(tokens: dict[str, str]) -> list[str]:
    """Repair any text-on-surface pair failing its WCAG floor, in place, using
    _fix_contrast against the resolved foreground ladder. Returns warnings."""
    warnings: list[str] = []
    tint_keys = ("--fg-primary", "--fg-body", "--fg-secondary", "--fg-tertiary", "--fg-muted")
    model = {
        "text_tints": [
            (tokens[k], _parse_color(tokens[k]))
            for k in tint_keys
            if _parse_color(tokens.get(k))
        ]
    }
    for fg_key, bg_key, need in _AA_PAIRS:
        fg, bg = tokens.get(fg_key), tokens.get(bg_key)
        if fg is None or bg is None:
            continue
        if contrast_ratio(fg, bg) < need:
            fixed = _fix_contrast(fg, bg, model, need)
            if fixed != fg:
                warnings.append(
                    f"{fg_key} {fg} failed {need}:1 on {bg_key} — repaired to {fixed}"
                )
                tokens[fg_key] = fixed
    return warnings


# Each `-text` token → (raw color token, its -dim alpha). The kit's chip/mark
# idiom places -text OVER the matching -dim tint (not bare surface), so the true
# worst-case surface is that tint composited over the most-elevated text surface
# (--bg-card). Calibrating there also covers plain text on bare base/elev/card.
_TEXT_ROLE_SPECS: tuple[tuple[str, str, float], ...] = (
    ("--accent-text", "--accent", 0.15),
    ("--warn-text", "--warn", 0.12),
    ("--info-text", "--info", 0.12),
    ("--ok-text", "--ok", 0.12),
    ("--gold-text", "--gold", 0.12),
    ("--violet-text", "--violet", 0.12),
    ("--rose-text", "--rose", 0.12),
)


def _text_role_surface(tokens: dict[str, str], raw_key: str, alpha: float) -> Optional[str]:
    """The worst-case surface for a text-role token: its -dim tint (raw@alpha)
    composited over --bg-card — an opaque bg mixed with the hue by alpha."""
    card, raw = tokens.get("--bg-card"), tokens.get(raw_key)
    if card is None or raw is None:
        return None
    return _mix(card, raw, alpha)


def _validate_text_roles(tokens: dict[str, str]) -> list[str]:
    """Assert every text-role color reads (≥4.5:1) on its worst-case surface —
    the matching -dim tint over --bg-card, the surface the chip/mark idiom uses.
    Derivation guarantees this; the guard catches a rare unreachable hue and falls
    back to --fg-primary (AA by construction). Returns warnings."""
    warnings: list[str] = []
    for key, raw_key, alpha in _TEXT_ROLE_SPECS:
        val = tokens.get(key)
        surface = _text_role_surface(tokens, raw_key, alpha)
        if val is None or surface is None or contrast_ratio(val, surface) >= 4.5:
            continue
        fallback = tokens.get("--fg-primary", val)
        tokens[key] = fallback
        warnings.append(
            f"{key} {val} could not reach AA on {surface} (its -dim over card) — "
            f"fell back to {fallback}"
        )
    return warnings


def _validate_monotonic(tokens: dict[str, str], mode: str) -> list[str]:
    """Assert the elevation ramp climbs away from base luminance monotonically
    (direction by polarity). Re-derive a colliding step from base. Warnings only —
    by construction (mix formula) this should never fire."""
    warnings: list[str] = []
    ramp = ["--bg-base", "--bg-elev", "--bg-card", "--bg-subtle"]
    lums = [_lum(tokens[k]) for k in ramp]
    ascending = mode == "dark"  # dark: base darkest → subtle lightest
    for i in range(1, len(ramp)):
        ok = lums[i] > lums[i - 1] if ascending else lums[i] < lums[i - 1]
        if not ok:
            fixed = _mix(tokens["--bg-base"], _MID_GRAY, _ELEV_STEP * i)
            warnings.append(
                f"{ramp[i]} broke elevation monotonicity — re-derived to {fixed}"
            )
            tokens[ramp[i]] = fixed
            lums[i] = _lum(fixed)
    return warnings


def _validate_complete(tokens: dict[str, str], kit_family: str) -> None:
    """Hard error if any required token is missing or empty (contract §3.4).
    resolve_brand silently drops absent slots; the kit layer must not."""
    missing = [k for k in token_manifest(kit_family) if not tokens.get(k)]
    if missing:
        raise ResolveError(
            f"incomplete {kit_family} theme — missing/empty tokens: {missing}"
        )


# ── entry ─────────────────────────────────────────────────────────────────────

@dataclass
class ResolvedTheme:
    """A complete, validated prism deck theme — flat CSS custom properties."""

    brand_id: str
    kit_family: str
    mode: str
    tokens: dict[str, str]
    branded: bool
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "brand_id": self.brand_id,
            "kit_family": self.kit_family,
            "mode": self.mode,
            "branded": self.branded,
            "tokens": self.tokens,
            "manifest": list(token_manifest(self.kit_family)),
            "warnings": self.warnings,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def to_css(self, selector: str = ":root") -> str:
        lines = [f"{selector} {{"]
        lines += [f"  {k}: {v};" for k, v in self.tokens.items()]
        lines.append("}")
        return "\n".join(lines)


def resolve_deck_theme(
    brand_id: str,
    kit_family: str = "board",
    mode: Optional[str] = None,
) -> ResolvedTheme:
    """Resolve a stored brand into a complete, validated prism deck theme.

    Pipeline (contract §3): resolve_brand → merge kit-structural ⊕ brand tokens
    per §3.1 precedence → derive missing (§3.2) → validate AA + monotonicity +
    completeness (§3.4). Never returns a partial theme: missing tokens raise
    ResolveError rather than being silently dropped.

    mode: "dark" | "light" | None. None infers from the brand base luminance.
    Forcing a mode opposite the brand's natural polarity derives fresh surfaces
    for that polarity while keeping brand identity (accent, fonts, status hues).
    """
    if mode not in (None, "dark", "light"):
        raise ResolveError(f"mode must be 'dark', 'light', or None — got {mode!r}")
    token_manifest(kit_family)  # validate kit family early

    resolved = resolve_brand(brand_id)
    design = _pget(resolved, "slots", "design", "data") if resolved else None
    ds = design.get("design_system") if isinstance(design, dict) else None
    v2 = _is_v2(ds)
    visual = _pget(design, "visual") if isinstance(design, dict) else None
    has_visual = isinstance(visual, dict)
    branded = has_visual or v2
    pal = visual.get("palette") if has_visual and isinstance(visual.get("palette"), dict) else {}
    typo = visual.get("typography") if has_visual and isinstance(visual.get("typography"), dict) else {}
    layout = visual.get("layout") if has_visual and isinstance(visual.get("layout"), dict) else {}

    warnings: list[str] = []
    if resolved is None:
        warnings.append(f"brand {brand_id!r} not found — resolving a brand-less kit theme")
    elif not branded:
        warnings.append(f"brand {brand_id!r} has no design slot — surfaces fully derived")
    elif not v2:
        warnings.append(f"brand {brand_id!r} has no design_system (v2) block — legacy formula derivation")

    # Base source: the v2 background when present, else the legacy palette base.
    brand_base = ds["background"] if v2 else _pget(pal, "background", "base")
    brand_base = brand_base if isinstance(brand_base, str) and _parse_color(brand_base) else None
    natural = ("dark" if _lum(brand_base) < 0.5 else "light") if brand_base else None
    resolved_mode = mode or natural or "dark"

    # Use brand surfaces only when the brand has a base AND its polarity matches
    # the requested mode; otherwise surfaces/fg derive from a structural base of
    # the requested polarity (the "derive a light deck from a dark brand" path).
    # Brand identity tokens (accent, fonts, status, chart) are always used.
    use_brand_surfaces = bool(brand_base) and natural == resolved_mode
    base = brand_base if use_brand_surfaces else _STRUCTURAL_BASE[resolved_mode]
    if brand_base and not use_brand_surfaces:
        warnings.append(
            f"mode={resolved_mode} differs from brand polarity ({natural}); "
            f"surfaces derived from structural base {base}, brand identity kept"
        )

    tokens: dict[str, str] = {}
    if v2:
        if use_brand_surfaces:
            tokens.update(_surfaces_from_ramp(ds, base, resolved_mode))
        else:
            tokens.update(_surfaces(pal, base, False))  # forced-opposite: formula
        # fg ladder targets the worst-case TEXT surface (--bg-card, the most-
        # elevated text container; --bg-subtle is a divider surface) so text is
        # AA on cards, not just on base.
        tokens.update(_foreground_v2(ds, base, use_brand_surfaces, tokens["--bg-card"]))
        acc = _accent_v2(ds, resolved_mode)
        tokens.update(acc)
        sem = _semantic(pal, acc["--accent"], resolved_mode)
        tokens.update(sem)
        tokens.update(_borders(pal, base, tokens["--fg-primary"], use_brand_surfaces))
        tokens.update(_chart_v2(ds, sem, acc["--accent"]))
        tokens.update(_typography_v2(ds))
    else:
        tokens.update(_surfaces(pal, base, use_brand_surfaces))
        tokens.update(_foreground(pal, base, use_brand_surfaces))
        acc = _accent(pal, base)
        tokens.update(acc)
        sem = _semantic(pal, acc["--accent"], resolved_mode)
        tokens.update(sem)
        tokens.update(_borders(pal, base, tokens["--fg-primary"], use_brand_surfaces))
        tokens.update(_chart(sem, acc["--accent"]))
        tokens.update(_typography(typo))

    # Text-role readability companions — every colored token that can be TEXT
    # gets a `-text` variant AA-safe on its WORST-CASE surface: the matching -dim
    # tint composited over --bg-card (the chip/mark idiom), which also covers
    # plain text on bare base/elev/card. Raw tokens stay for fills/borders/marks;
    # --accent-ink is the readable ink to sit ON a raw accent fill.
    for key, raw_key, alpha in _TEXT_ROLE_SPECS:
        surface = _text_role_surface(tokens, raw_key, alpha)
        tokens[key] = _readable_on(tokens[raw_key], surface)
    tokens["--accent-ink"] = _accent_ink(tokens["--accent"])

    tokens.update(_GEOMETRY)
    tokens["--ms"] = _pget(layout, "grid_base") or _GEOMETRY["--ms"]
    tokens.update(_radii(layout))
    tokens.update(_motion(layout))
    tokens.update(_shadows(layout, resolved_mode))

    warnings += _validate_monotonic(tokens, resolved_mode)
    warnings += _validate_contrast(tokens)
    warnings += _validate_text_roles(tokens)
    _validate_complete(tokens, kit_family)

    return ResolvedTheme(
        brand_id=brand_id,
        kit_family=kit_family,
        mode=resolved_mode,
        tokens=tokens,
        branded=branded,
        warnings=warnings,
    )


# ── debug CLI:  python -m okuro.prism.theme <brand> [--mode m] [--kit k] [--json] ──

def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m okuro.prism.theme",
        description="Resolve a stored brand into a prism deck theme (debug).",
    )
    parser.add_argument("brand", help="brand id (e.g. okuro)")
    parser.add_argument("--kit", default="board", help="kit family (default: board)")
    parser.add_argument("--mode", choices=("dark", "light"), default=None,
                        help="force polarity (default: infer from brand base)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of CSS")
    parser.add_argument("--css", action="store_true", help="emit only the :root CSS block")
    args = parser.parse_args(argv)

    try:
        theme = resolve_deck_theme(args.brand, kit_family=args.kit, mode=args.mode)
    except ResolveError as exc:
        print(f"error: {exc}")
        return 1

    if args.json:
        print(theme.to_json())
        return 0
    if args.css:
        print(theme.to_css())
        return 0

    print(f"# {theme.brand_id}  kit={theme.kit_family}  mode={theme.mode}  branded={theme.branded}")
    width = max(len(k) for k in theme.tokens)
    for k, v in theme.tokens.items():
        print(f"  {k.ljust(width)}  {v}")
    if theme.warnings:
        print("\n# warnings")
        for w in theme.warnings:
            print(f"  - {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
