# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.ai_models (目録) — AI model registry.
# index: none
# AGENT_HEADER_END -->
"""okuro.ai_models (目録) — AI model registry."""

from .bundle import Bundle, BundleFile, bundles_root, load_bundle, scan_bundles, write_bundle
from .catalog import (
    CatalogEntry,
    CatalogFormat,
    from_civitai_model,
    from_hf_model,
    rank,
    score_potential,
    score_quality,
    usable_vram_gb,
)
from .acquire import AcquisitionError, pull
from .credentials import auth_header, credential_status, resolve_token
from .discovery import discover, resolve_entry, search_civitai, search_hf
from .discoveries import list_discoveries, mark_installed, set_status, upsert_discovery
from .registry import list_models, get_model, scan_models

__all__ = [
    "pull",
    "AcquisitionError",
    "resolve_entry",
    "resolve_token",
    "auth_header",
    "credential_status",
    "discover",
    "search_hf",
    "search_civitai",
    "list_models",
    "get_model",
    "scan_models",
    "list_discoveries",
    "upsert_discovery",
    "set_status",
    "mark_installed",
    "Bundle",
    "BundleFile",
    "bundles_root",
    "load_bundle",
    "scan_bundles",
    "write_bundle",
    "CatalogEntry",
    "CatalogFormat",
    "from_hf_model",
    "from_civitai_model",
    "rank",
    "score_potential",
    "score_quality",
    "usable_vram_gb",
]
