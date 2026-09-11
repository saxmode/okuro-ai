# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism visualizer bake path — turn an okuro visual OUTPUT into a
#   self-contained `visualizer` block, baked at attach time. Wires the (until now
#   orphaned) scene()/audio_brief() producers plus the assets icon store behind
#   one entry point so an agent can drop a generated image, an audio brief, or an
#   icon onto a facet. Payload is baked once (data URI / inline svg) — no live
#   client fetch, no auth surface. Graceful: any fault returns {} (attach nothing).
# index: def bake_visualizer
# AGENT_HEADER_END -->
"""Bake a prism ``visualizer`` block from an okuro output source.

``source`` selects the producer:
  * ``studio``      — a ComfyUI-generated hero image (``scene`` → data-URI). Returns
                      {} when ComfyUI/model/workflow isn't ready (user-gated).
  * ``illustrator`` — an on-brand "Architecture Noir" SVG (``illustrate`` →
                      tm-illustrator :3070 → inline svg). The mono-native visual
                      hook: conceptual, not photoreal.
  * ``podcast``     — a spoken brief (``audio_brief`` → MP3 data-URI + transcript).
  * ``icon``        — an okuro-assets glyph (``get_icon`` → inline svg).

Each underlying producer already degrades to {} on failure; this normalizes
their differing block shapes onto the single ``visualizer`` renderer.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("okuro.prism.visualizer")


def bake_visualizer(
    source: str,
    *,
    prompt: Optional[str] = None,
    text: Optional[str] = None,
    icon_id: Optional[str] = None,
    category: Optional[str] = None,
    caption: Optional[str] = None,
    standalone: bool = False,
    source_ref: Optional[str] = None,
) -> dict[str, Any]:
    """Produce a ``visualizer`` block for ``source`` (studio|illustrator|podcast|
    icon), or {} when the source is unavailable/empty."""
    src = (source or "").strip().lower()
    block: dict[str, Any] = {}

    try:
        if src == "studio":
            from okuro.prism.scene import scene

            img = scene((prompt or "").strip(), caption=caption)
            if not img.get("src"):
                return {}
            block = {"type": "visualizer", "source": "studio", "src": img["src"], "caption": caption or img.get("caption")}

        elif src == "illustrator":
            from okuro.prism.illustrator import illustrate

            ill = illustrate((prompt or "").strip(), category=category)
            if not ill.get("svg"):
                return {}
            # Inline SVG rides the same renderer branch as an icon (source→svg),
            # so no frontend change is needed to show an illustrator hook.
            block = {"type": "visualizer", "source": "illustrator", "svg": ill["svg"], "caption": caption}

        elif src == "podcast":
            from okuro.prism.audio import audio_brief

            ab = audio_brief((text or "").strip(), caption=caption)
            if not ab.get("src"):
                return {}
            block = {
                "type": "visualizer", "source": "podcast", "src": ab["src"],
                "transcript": ab.get("transcript"), "duration": ab.get("duration"), "caption": caption,
            }

        elif src == "icon":
            from okuro.assets.icons import service as icons

            res = icons.get_icon((icon_id or "").strip(), format="svg")
            if not isinstance(res, dict) or not res.get("svg"):
                return {}
            block = {"type": "visualizer", "source": "icon", "svg": res["svg"], "caption": caption or res.get("name")}

        else:
            return {}
    except Exception as exc:  # noqa: BLE001 — a media bake must never raise into the caller
        log.warning("bake_visualizer(%s) failed: %s", src, exc)
        return {}

    if standalone:
        block["display"] = "standalone"
    if source_ref:
        block["source_ref"] = source_ref
    return {k: v for k, v in block.items() if v is not None}


__all__ = ["bake_visualizer"]
