# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism illustrator adapter — a thin shim onto okuro's NATIVE
#   illustration capability (okuro.media.illustrate). Kept so prism's callers
#   (visualizer.bake_visualizer) have a stable topic→inline-SVG entry point; the
#   engine now runs IN-PROCESS (okuro.media.illustrator) on okuro's own models —
#   no external :3070 service.
# index: def illustrate
# AGENT_HEADER_END -->
"""Prism adapter for okuro's native illustration capability.

Previously this reached an external tm-illustrator service over HTTP (:3070).
The engine has been ported into okuro (``okuro.media.illustrator``) and unified
behind ``okuro.media.illustrate``; this module now delegates there, so a prism
deck's visual hooks are produced on okuro's own bridge/model registry with no
external process. Same contract: returns ``{"svg": str, ...}`` or ``{}``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("okuro.prism.illustrator")


def illustrate(topic: str, *, category: Optional[str] = None, **_ignored: Any) -> dict[str, Any]:
    """Fetch an inline "Architecture Noir" SVG for ``topic`` via okuro's native
    illustration capability. Returns ``{"svg": str, "topic": str, "category":
    str|None}`` or ``{}`` on any failure. Never raises."""
    t = (topic or "").strip()
    if not t:
        return {}
    try:
        from okuro.media.illustrate import illustrate as media_illustrate

        # persist=False: a prism deck inlines the SVG in the doc; it doesn't need
        # a separate asset-store row per hook.
        res = media_illustrate(t, style="noir", fmt="svg", category=category, persist=False)
    except Exception as exc:  # noqa: BLE001 — a visual hook must never break a build
        log.info("illustrate(%s) failed: %s", t[:40], exc)
        return {}
    svg = res.get("svg")
    if not svg:
        return {}
    return {"svg": svg, "topic": t, "category": category}


__all__ = ["illustrate"]
