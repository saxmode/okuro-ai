# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: provides architecture-noir style tokens and reusable SVG defs
# index: def _hex_to_rgb01 | def defs_for_accent
# AGENT_HEADER_END -->
"""Architecture-Noir style tokens + reusable SVG <defs>."""
from __future__ import annotations

PALETTE = {
    "bg":          "#000000",
    "stroke":      "#e0e0e0",
    "stroke_dim":  "#6e6e6e",
    "stroke_faint":"#2a2a2a",
    "focal":       "#f4f1e8",
    "fringe_r":    "#ff3344",
    "fringe_g":    "#22c55e",
    "fringe_b":    "#3b82f6",
}

CANVAS_PRESETS = {
    "wide":   (1200, 600),
    "hd":     (1920, 1080),
    "square": (1080, 1080),
}

# SVG <defs> block. Provides:
#   #glow         soft radial glow on focal points
#   #chromatic    1-2px RGB fringe on focal edges
#   #dotgrid      faint dotted construction grid (pattern)
DEFS = """
<defs>
  <filter id="glow" x="-200%" y="-200%" width="500%" height="500%">
    <feGaussianBlur stdDeviation="6" result="b"/>
    <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>
  <filter id="glow_soft" x="-200%" y="-200%" width="500%" height="500%">
    <feGaussianBlur stdDeviation="14"/>
  </filter>
  <!-- Bloom: deep multi-stop gaussian merged behind the source.
       Used on focal anchors and per-dot lights to match reference plates. -->
  <filter id="bloom" x="-300%" y="-300%" width="700%" height="700%">
    <feGaussianBlur in="SourceGraphic" stdDeviation="3"  result="b1"/>
    <feGaussianBlur in="SourceGraphic" stdDeviation="9"  result="b2"/>
    <feGaussianBlur in="SourceGraphic" stdDeviation="22" result="b3"/>
    <feMerge>
      <feMergeNode in="b3"/>
      <feMergeNode in="b2"/>
      <feMergeNode in="b1"/>
      <feMergeNode in="SourceGraphic"/>
    </feMerge>
  </filter>
  <!-- Strong bloom for hero focal points (Kimi-K2 sun, attention peaks) -->
  <filter id="bloom_strong" x="-400%" y="-400%" width="900%" height="900%">
    <feGaussianBlur in="SourceGraphic" stdDeviation="4"  result="b1"/>
    <feGaussianBlur in="SourceGraphic" stdDeviation="14" result="b2"/>
    <feGaussianBlur in="SourceGraphic" stdDeviation="36" result="b3"/>
    <feMerge>
      <feMergeNode in="b3"/>
      <feMergeNode in="b2"/>
      <feMergeNode in="b1"/>
      <feMergeNode in="SourceGraphic"/>
    </feMerge>
  </filter>
  <filter id="chromatic" x="-10%" y="-10%" width="120%" height="120%">
    <feOffset in="SourceGraphic" dx="-1.5" dy="0" result="r0"/>
    <feColorMatrix in="r0" values="1 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 1 0" result="r"/>
    <feOffset in="SourceGraphic" dx="0" dy="0" result="g0"/>
    <feColorMatrix in="g0" values="0 0 0 0 0  0 1 0 0 0  0 0 0 0 0  0 0 0 1 0" result="g"/>
    <feOffset in="SourceGraphic" dx="1.5" dy="0" result="b0"/>
    <feColorMatrix in="b0" values="0 0 0 0 0  0 0 0 0 0  0 0 1 0 0  0 0 0 1 0" result="b"/>
    <feBlend in="r" in2="g" mode="screen" result="rg"/>
    <feBlend in="rg" in2="b" mode="screen"/>
  </filter>
  <pattern id="dotgrid" x="0" y="0" width="24" height="24" patternUnits="userSpaceOnUse">
    <circle cx="1" cy="1" r="0.6" fill="#3a3a3a"/>
  </pattern>
  <radialGradient id="halo">
    <stop offset="0%" stop-color="#ffffff" stop-opacity="0.55"/>
    <stop offset="60%" stop-color="#ffffff" stop-opacity="0.06"/>
    <stop offset="100%" stop-color="#ffffff" stop-opacity="0"/>
  </radialGradient>
</defs>
"""

FONT_FAMILY = "JetBrains Mono, ui-monospace, Menlo, monospace"


# Per-scene accent palette: the G-channel of the chromatic split is swapped
# to this color. R + B stay anchored. Values are (R_hex, G_hex, B_hex)
# where the middle ('G') is the user's chosen accent.
ACCENTS = {
    "green":  ("#ff3344", "#22c55e", "#3b82f6"),
    "amber":  ("#ff3344", "#f59e0b", "#3b82f6"),
    "red":    ("#ff3344", "#ef4444", "#3b82f6"),
    "blue":   ("#ff3344", "#3b82f6", "#a855f7"),
    "violet": ("#ff3344", "#a855f7", "#3b82f6"),
    "cyan":   ("#ff3344", "#22d3ee", "#3b82f6"),
}


def _hex_to_rgb01(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255.0,
            int(h[2:4], 16) / 255.0,
            int(h[4:6], 16) / 255.0)


def defs_for_accent(accent: str) -> str:
    """Build DEFS with the chromatic filter using the given accent for the
    middle channel. Falls back to green if accent unknown."""
    rgb = ACCENTS.get(accent, ACCENTS["green"])
    r_r, r_g, r_b = _hex_to_rgb01(rgb[0])
    g_r, g_g, g_b = _hex_to_rgb01(rgb[1])
    b_r, b_g, b_b = _hex_to_rgb01(rgb[2])
    # feColorMatrix maps SourceGraphic.RGB -> tinted single-color via the
    # column-multiplier matrix. We want each fringe layer to take SOURCE
    # luminance and re-emit it as a constant tint; alpha kept.
    def _mat(r: float, g: float, b: float) -> str:
        # output_R = r * (R+G+B)/3 effectively; but simpler: collapse to color
        # using identity-like rows. Use (r, g, b) constants in the 5th column
        # (additive) and zero out source.
        return (f"0 0 0 0 {r:.3f}  "
                f"0 0 0 0 {g:.3f}  "
                f"0 0 0 0 {b:.3f}  "
                f"0 0 0 1 0")
    return f"""
<defs>
  <filter id="glow" x="-200%" y="-200%" width="500%" height="500%">
    <feGaussianBlur stdDeviation="6" result="b"/>
    <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>
  <filter id="glow_soft" x="-200%" y="-200%" width="500%" height="500%">
    <feGaussianBlur stdDeviation="14"/>
  </filter>
  <filter id="bloom" x="-300%" y="-300%" width="700%" height="700%">
    <feGaussianBlur in="SourceGraphic" stdDeviation="3"  result="b1"/>
    <feGaussianBlur in="SourceGraphic" stdDeviation="9"  result="b2"/>
    <feGaussianBlur in="SourceGraphic" stdDeviation="22" result="b3"/>
    <feMerge>
      <feMergeNode in="b3"/>
      <feMergeNode in="b2"/>
      <feMergeNode in="b1"/>
      <feMergeNode in="SourceGraphic"/>
    </feMerge>
  </filter>
  <filter id="bloom_strong" x="-400%" y="-400%" width="900%" height="900%">
    <feGaussianBlur in="SourceGraphic" stdDeviation="4"  result="b1"/>
    <feGaussianBlur in="SourceGraphic" stdDeviation="14" result="b2"/>
    <feGaussianBlur in="SourceGraphic" stdDeviation="36" result="b3"/>
    <feMerge>
      <feMergeNode in="b3"/>
      <feMergeNode in="b2"/>
      <feMergeNode in="b1"/>
      <feMergeNode in="SourceGraphic"/>
    </feMerge>
  </filter>
  <filter id="chromatic" x="-10%" y="-10%" width="120%" height="120%">
    <feOffset in="SourceGraphic" dx="-1.5" dy="0" result="r0"/>
    <feColorMatrix in="r0" values="{_mat(r_r, r_g, r_b)}" result="r"/>
    <feOffset in="SourceGraphic" dx="0" dy="0" result="g0"/>
    <feColorMatrix in="g0" values="{_mat(g_r, g_g, g_b)}" result="g"/>
    <feOffset in="SourceGraphic" dx="1.5" dy="0" result="b0"/>
    <feColorMatrix in="b0" values="{_mat(b_r, b_g, b_b)}" result="b"/>
    <feBlend in="r" in2="g" mode="screen" result="rg"/>
    <feBlend in="rg" in2="b" mode="screen"/>
  </filter>
  <pattern id="dotgrid" x="0" y="0" width="24" height="24" patternUnits="userSpaceOnUse">
    <circle cx="1" cy="1" r="0.6" fill="#3a3a3a"/>
  </pattern>
  <radialGradient id="halo">
    <stop offset="0%" stop-color="#ffffff" stop-opacity="0.55"/>
    <stop offset="60%" stop-color="#ffffff" stop-opacity="0.06"/>
    <stop offset="100%" stop-color="#ffffff" stop-opacity="0"/>
  </radialGradient>
</defs>
"""
