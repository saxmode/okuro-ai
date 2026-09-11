# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Scene — generate a hero image ON-BOX (ComfyUI) to make an abstract
#   stake concrete, and embed it as a self-contained data-URI `image` block. No
#   new block type: it fills prism's existing `image` block (DP09). Board-
#   confidential: the pixels are synthesized on the RTX box, nothing leaves.
# index:
#   def _encode_data_uri
#   def scene
# AGENT_HEADER_END -->
"""okuro·prism scene — the deck that shows, not just tells.

The multimodal counterpart to audio-brief: given a prompt, it drives okuro's
local ComfyUI (``inference.gen_tools.run_generation`` — advanced/pro, license-
gated, GPU) to render an image, re-encodes it to a light JPEG, and returns an
``image`` block with a ``data:`` URI. Reuses the existing image renderer and the
``img-src data:`` CSP allowance — no new block type, no serving route.

Graceful by construction: if ComfyUI isn't installed, no workflow/model is ready
for the task, or the licence gate blocks it, ``scene`` returns ``{}`` and the
caller attaches nothing. A deck is never blocked over an optional visual.
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("okuro.prism.scene")

# Cap the embedded image so a data-URI stays board-light (~150-350 KB) instead
# of a multi-MB raw PNG bloating the doc JSON.
_MAX_EDGE = 1024
_JPEG_QUALITY = 85


def _encode_data_uri(img_path: Path) -> Optional[str]:
    """Re-encode a generated image to a downscaled JPEG data URI. Falls back to
    the raw bytes as PNG if Pillow is unavailable. None on any read failure."""
    try:
        raw = img_path.read_bytes()
    except OSError as exc:
        log.warning("scene: cannot read generated image %s: %s", img_path, exc)
        return None
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail((_MAX_EDGE, _MAX_EDGE))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:  # noqa: BLE001 — Pillow missing / decode fail → raw PNG
        log.info("scene: JPEG re-encode unavailable (%s), embedding raw PNG", exc)
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def scene(
    prompt: str,
    *,
    model_id: Optional[str] = None,
    width: int = 1024,
    height: int = 576,
    negative_extra: str = "",
    caption: Optional[str] = None,
    source_ref: Optional[str] = None,
) -> dict[str, Any]:
    """Generate a hero image on-box → a self-contained ``image`` block.

    Returns ``{}`` when generation is unavailable (ComfyUI not installed, no
    ready workflow/model, licence-gated, or empty output) — the caller skips it.
    """
    if not (prompt or "").strip():
        return {}
    try:
        from okuro.inference import gen_tools
        result = gen_tools.run_generation(
            prompt, model_id=model_id, task="text-to-image",
            width=width, height=height, negative_extra=negative_extra,
        )
    except Exception as exc:  # noqa: BLE001 — ComfyError family + anything else → skip
        log.warning("scene: generation unavailable: %s", exc)
        return {}

    paths = (result or {}).get("paths") or []
    if not paths:
        return {}
    src = _encode_data_uri(Path(paths[0]))
    if not src:
        return {}

    block: dict[str, Any] = {"type": "image", "src": src, "caption": caption or prompt[:80]}
    if source_ref:
        block["source_ref"] = source_ref
    return block


__all__ = ["scene"]
