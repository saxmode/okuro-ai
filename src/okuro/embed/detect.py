# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Back-compat shim — hardware probe promoted to okuro.capability.
# index: re-exports
# AGENT_HEADER_END -->
"""Back-compat shim — the hardware capability probe was promoted to the
shared ``okuro.capability`` module (P0 of the local-inference plan).

Existing importers (``okuro.embed.config``, ``orchestrator.api.embed``, the
onboarding + settings surfaces) keep importing ``okuro.embed.detect``
unchanged; new consumers (inference broker, model-catalog ranking, tier
resolution) import ``okuro.capability`` directly. Both names resolve to the
same implementation.
"""

from __future__ import annotations

from okuro.capability import (  # noqa: F401
    _HIGH_TIER_VRAM_THRESHOLD_GB,
    _cpu_cores,
    _detect_display_gpu_index_linux,
    _detect_display_gpu_index_macos,
    _detect_gpus_nvml,
    _display_gpu_indices_linux,
    _drm_card_bdf,
    _has_connected_connector,
    _nvidia_pci_bdf_to_index,
    _ram_gb,
    capabilities,
    is_display_gpu,
    recommended_device,
    recommended_tier,
)

__all__ = [
    "capabilities",
    "recommended_tier",
    "recommended_device",
    "is_display_gpu",
    "_HIGH_TIER_VRAM_THRESHOLD_GB",
]
