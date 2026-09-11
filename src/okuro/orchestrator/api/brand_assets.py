# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: /api/brand-assets router — upload/list/serve/delete brand assets.
# index: imports | router | list | upload | raw | delete
# AGENT_HEADER_END -->
"""Brand-asset library API.

Multipart upload + listing + raw serving + delete over
``okuro.brand_assets.storage``. Auth is the global bearer middleware, which
also accepts ``?token=`` on the query string — so an ``<img src>`` can load a
raw asset (no custom headers) by appending the token.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

logger = logging.getLogger("okuro.orchestrator.api.brand_assets")

router = APIRouter(prefix="/api/brand-assets", tags=["brand-assets"])

_MAX_BYTES = 25_000_000  # 25 MB per asset


@router.get("")
def list_brand_assets(brand_id: Optional[str] = None, kind: Optional[str] = None) -> dict:
    from okuro.brand_assets import list_assets

    return {"assets": [a.to_summary() for a in list_assets(brand_id, kind)]}


@router.post("")
async def upload_brand_asset(
    file: UploadFile = File(...),
    brand_id: str = Form("default"),
    kind: str = Form("image"),
    name: str = Form(""),
    bg_target: str = Form("both"),
) -> dict:
    from okuro.brand_assets import save_asset

    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > _MAX_BYTES:
        raise HTTPException(400, "file too large (25MB max)")
    asset = save_asset(
        brand_id=brand_id,
        kind=kind,
        name=(name or file.filename or "asset"),
        data=data,
        mime=file.content_type or "application/octet-stream",
        bg_target=bg_target,
    )
    return asset.to_summary()


@router.get("/{asset_id}/raw")
def raw_brand_asset(asset_id: str):
    from okuro.brand_assets import get_asset

    asset = get_asset(asset_id)
    if asset is None:
        raise HTTPException(404, f"asset '{asset_id}' not found")
    return FileResponse(asset.path, media_type=asset.mime, filename=asset.name)


@router.delete("/{asset_id}")
def delete_brand_asset(asset_id: str) -> dict:
    from okuro.brand_assets import delete_asset

    return {"deleted": delete_asset(asset_id)}
