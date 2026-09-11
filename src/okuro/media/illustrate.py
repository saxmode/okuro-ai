# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro MEDIA — the unified ILLUSTRATION capability. One entry point that
#   routes a topic to the right on-box engine: a mono "Architecture Noir" VECTOR
#   illustration (okuro.media.illustrator, GPU-free) or a PHOTOREAL raster image
#   (okuro.inference ComfyUI, GPU/edition-gated) — both on okuro's own model
#   registry, no external service. Persists the result to the okuro asset store.
# index: def illustrate
# AGENT_HEADER_END -->
"""okuro illustration capability — vector-noir or photoreal-raster, one call.

This is the ``B`` seam of the tm-illustrator port: illustration creation is now
an okuro capability, not an external :3070 service. It routes by ``style``:

* ``noir`` / ``vector`` / ``auto`` — the ported vector engine
  (``okuro.media.illustrator``): an LLM composes a Scene (via okuro's native
  bridge), rendered to a deterministic SVG (or PNG/WebM). GPU-free, on-brand for
  the mono ``architecture-noir`` family.
* ``photoreal`` / ``raster`` — okuro's local ComfyUI (``inference.gen_tools``),
  the same engine ``prism.scene`` uses. GPU + licence/edition-gated: unavailable
  gracefully returns ``{}``.

Both paths use okuro's own models (bridge capability routing for compose, the
ComfyUI model registry for raster). Every success is indexed in the okuro asset
store so an illustration is a durable, reusable deliverable — not an ephemeral
blob. Graceful by construction: any failure returns ``{}``.
"""

from __future__ import annotations

import base64
import logging
import tempfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("okuro.media.illustrate")

_NOIR = {"noir", "vector", "auto", "", None}
_RASTER = {"photoreal", "raster", "studio"}
_MIME = {"svg": "image/svg+xml", "png": "image/png", "webm": "video/webm"}


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def _persist(data: bytes, *, kind: str, mime: str, title: str, meta: dict) -> Optional[str]:
    """Index bytes in the okuro asset store; return the asset id (or None)."""
    try:
        from okuro.assets.store import register_asset

        row = register_asset(
            kind=kind, source="studio", data=data, mime=mime,
            folder="illustrations", title=title[:120], meta=meta,
            tags=["illustration", meta.get("style", "noir")],
        )
        return row.get("id")
    except Exception as exc:  # noqa: BLE001 — persistence never blocks the deliverable
        log.info("illustrate: asset persist skipped: %s", exc)
        return None


def _noir(topic: str, category: Optional[str], fmt: str, force: bool,
          width: int, height: int) -> dict[str, Any]:
    from okuro.media.illustrator.brief import load_or_compose_scene
    from okuro.media.illustrator.render import scene_to_svg

    scene, cache_hit = load_or_compose_scene(topic, category=category, force=force)

    if fmt == "svg":
        svg = scene_to_svg(scene)
        if not svg.strip().startswith("<svg"):
            return {}
        return {"data": svg.encode("utf-8"), "svg": svg, "mime": _MIME["svg"], "cache_hit": cache_hit}

    # PNG / WebM need a rendered file (chromium-backed) — graceful if unavailable.
    suffix = ".png" if fmt == "png" else ".webm"
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            out = Path(tf.name)
        if fmt == "png":
            from okuro.media.illustrator.render import write_png
            write_png(scene, out)
        else:
            from okuro.media.illustrator.video import write_webm
            write_webm(scene, out)
        data = out.read_bytes()
        out.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 — no chromium / render fault → skip fmt
        log.info("illustrate: noir %s render unavailable: %s", fmt, exc)
        return {}
    if not data:
        return {}
    return {"data": data, "src": _data_uri(data, _MIME[fmt]), "mime": _MIME[fmt], "cache_hit": cache_hit}


def _raster(topic: str, width: int, height: int) -> dict[str, Any]:
    try:
        from okuro.inference import gen_tools

        result = gen_tools.run_generation(topic, task="text-to-image", width=width, height=height)
    except Exception as exc:  # noqa: BLE001 — ComfyUI/edition-gated → skip
        log.info("illustrate: photoreal (ComfyUI) unavailable: %s", exc)
        return {}
    paths = (result or {}).get("paths") or []
    if not paths:
        return {}
    try:
        data = Path(paths[0]).read_bytes()
    except OSError:
        return {}
    return {"data": data, "src": _data_uri(data, _MIME["png"]), "mime": _MIME["png"], "cache_hit": False}


def illustrate(
    topic: str,
    *,
    style: str = "noir",
    fmt: str = "svg",
    category: Optional[str] = None,
    width: int = 1200,
    height: int = 600,
    caption: Optional[str] = None,
    persist: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Create an illustration for ``topic`` and (optionally) index it in the asset
    store. ``style`` = noir|vector|auto (vector SVG, GPU-free) or photoreal|raster
    (ComfyUI, GPU/edition-gated). ``fmt`` = svg|png|webm (noir) — raster is always
    png. Returns ``{}`` on any failure, else a normalized block:
    ``{style, fmt, mime, svg?|src?, caption, asset_id?, cache_hit}``. Never raises."""
    t = (topic or "").strip()
    if not t:
        return {}
    st = (style or "").strip().lower()
    f = (fmt or "svg").strip().lower()

    try:
        if st in _RASTER:
            out = _raster(t, width, height)
            resolved_style, kind = "photoreal", "image"
        elif st in _NOIR:
            if f not in _MIME:
                f = "svg"
            out = _noir(t, category, f, force, width, height)
            resolved_style = "noir"
            kind = "illustration" if f == "svg" else ("video" if f == "webm" else "image")
        else:
            return {}
    except Exception as exc:  # noqa: BLE001 — the capability never raises (compose/render faults → {})
        log.info("illustrate(%s) failed: %s", t[:40], exc)
        return {}
    if not out:
        return {}

    block: dict[str, Any] = {
        "style": resolved_style,
        "fmt": "png" if resolved_style == "photoreal" else f,
        "mime": out["mime"],
        "caption": caption or t[:80],
        "cache_hit": out.get("cache_hit", False),
    }
    if out.get("svg"):
        block["svg"] = out["svg"]
    if out.get("src"):
        block["src"] = out["src"]
    if persist and out.get("data"):
        aid = _persist(out["data"], kind=kind, mime=out["mime"], title=t,
                       meta={"style": resolved_style, "category": category, "fmt": block["fmt"]})
        if aid:
            block["asset_id"] = aid
    return block


__all__ = ["illustrate"]
