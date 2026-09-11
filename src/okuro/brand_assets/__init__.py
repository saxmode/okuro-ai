# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro brand-asset library — uploaded binary assets scoped to a brand.
# index: re-exports from storage
# AGENT_HEADER_END -->
"""okuro brand-assets — a general uploaded-asset library scoped to a brand.

Logos, images, portraits, signatures, backgrounds. Reused by okuro-slides and
(next) okuro-video. Bytes live on disk under ~/.okuro/assets; metadata in the
``brand_assets`` table. Distinct from ``okuro.assets`` (the icon manager) and
``asset_profiles`` (the icon-provider registry).
"""

from okuro.brand_assets.storage import (
    BrandAsset,
    delete_asset,
    get_asset,
    list_assets,
    save_asset,
)

__all__ = ["BrandAsset", "delete_asset", "get_asset", "list_assets", "save_asset"]
