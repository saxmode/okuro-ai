# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: okuro edition gate — the SSOT for whether local inference is offered
#          on this deployment, and at what VRAM ceiling. air = cloud-only (no
#          GPU) → the ENTIRE local-models/inference/research surface is off;
#          advanced = GPU ≤16GB; pro = GPU >16GB. Every local-inference
#          entrypoint (discovery, engine, optimizer, suggestions, daemon task,
#          Models page) must gate on local_inference_enabled().
# index:
#   def detect_edition
#   def local_inference_enabled
#   def vram_ceiling_gb
#   def effective_detection
# AGENT_HEADER_END -->
"""Edition gate for local inference.

okuro ships in three editions, defined by the local GPU:

    air       no GPU  → cloud-only, NO local inference (whole surface off)
    advanced  ≤16 GB  → local inference, 16 GB ceiling
    pro       >16 GB  → local inference, full VRAM

The runtime signal is detected GPU VRAM (``catalog.usable_vram_gb``); an
explicit ``OKURO_EDITION`` env var overrides it when the sold plan differs from
the raw hardware. This is the single place that decision lives — surfaces call
``local_inference_enabled()`` rather than re-deriving it, so a GPU-less (air)
box never discovers, researches, serves, or suggests local models.
"""

from __future__ import annotations

import os
from typing import Optional

from .catalog import usable_vram_gb

AIR = "air"
ADVANCED = "advanced"
PRO = "pro"

_ADVANCED_CEILING_GB = 16.0


def detect_edition(detection: Optional[dict] = None) -> str:
    """Resolve the edition for this deployment.

    Precedence: explicit ``OKURO_EDITION`` (air|advanced|pro) → else derived
    from detected usable GPU VRAM (0 → air, ≤16 → advanced, else pro).
    """
    env = os.environ.get("OKURO_EDITION", "").strip().lower()
    if env in (AIR, ADVANCED, PRO):
        return env
    vram = usable_vram_gb(detection or {})
    if vram <= 0:
        return AIR
    if vram <= _ADVANCED_CEILING_GB:
        return ADVANCED
    return PRO


def local_inference_enabled(detection: Optional[dict] = None) -> bool:
    """True iff this deployment may run local models (edition != air)."""
    return detect_edition(detection) != AIR


def vram_ceiling_gb(edition: str) -> Optional[float]:
    """VRAM ceiling for an edition — 0 for air, 16 for advanced, None (uncapped
    beyond the actual GPU) for pro."""
    if edition == AIR:
        return 0.0
    if edition == ADVANCED:
        return _ADVANCED_CEILING_GB
    return None


def effective_detection(detection: Optional[dict] = None) -> dict:
    """Detection with each GPU's VRAM clamped to the edition ceiling.

    Fit-gating (catalog.rank / acquire) uses this so an explicit
    OKURO_EDITION=advanced on a physically larger box still caps model choice
    at 16 GB. Without an override it is a no-op (advanced hardware is already
    ≤16 GB). air returns no usable GPU, so nothing fits.
    """
    detection = detection or {}
    cap = vram_ceiling_gb(detect_edition(detection))
    if cap is None:
        return detection
    gpus = []
    for g in detection.get("gpus", []):
        g2 = dict(g)
        g2["vram_gb"] = min(float(g.get("vram_gb", 0) or 0), cap)
        gpus.append(g2)
    return {**detection, "gpus": gpus}
