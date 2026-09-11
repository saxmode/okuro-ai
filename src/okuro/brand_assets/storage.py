# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: brand-asset storage — disk bytes + SQLite metadata CRUD.
# index:
#   imports
#   class BrandAsset
#   def _assets_dir
#   def save_asset
#   def list_assets
#   def get_asset
#   def delete_asset
# AGENT_HEADER_END -->
"""Brand-asset storage — bytes on disk, metadata in SQLite.

Layout: ``~/.okuro/assets/<brand_id>/<id>.<ext>``. The DB row carries the kind,
name, mime, path and size. Mirrors okuro's binary convention (media lives under
~/.okuro/media). Best-effort + dependency-free (no image lib): width/height are
left null unless a caller supplies them.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from okuro.db import get_db

_VALID_KINDS = {"logo", "image", "portrait", "signature", "background", "other"}
_VALID_BG_TARGETS = {"dark", "light", "both"}

# mime → file extension (best-effort; falls back to .bin)
_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}


@dataclass
class BrandAsset:
    id: str
    brand_id: str
    kind: str
    name: str
    mime: str
    path: str
    width: Optional[int] = None
    height: Optional[int] = None
    bytes: int = 0
    created_at: str = ""
    bg_target: str = "both"

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "brand_id": self.brand_id,
            "kind": self.kind,
            "name": self.name,
            "mime": self.mime,
            "width": self.width,
            "height": self.height,
            "bytes": self.bytes,
            "created_at": self.created_at,
            "bg_target": self.bg_target,
            "url": f"/api/brand-assets/{self.id}/raw",
        }


def _assets_dir(brand_id: str) -> Path:
    base = Path(os.environ.get("HOME", str(Path.home()))) / ".okuro" / "assets" / brand_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def _row(r: dict) -> BrandAsset:
    return BrandAsset(
        id=r["id"],
        brand_id=r.get("brand_id") or "default",
        kind=r.get("kind") or "image",
        name=r.get("name") or "",
        mime=r.get("mime") or "application/octet-stream",
        path=r.get("path") or "",
        width=r.get("width"),
        height=r.get("height"),
        bytes=int(r.get("bytes") or 0),
        created_at=r.get("created_at") or "",
        bg_target=r.get("bg_target") or "both",
    )


def save_asset(
    *,
    brand_id: str,
    kind: str,
    name: str,
    data: bytes,
    mime: str,
    width: Optional[int] = None,
    height: Optional[int] = None,
    bg_target: str = "both",
) -> BrandAsset:
    """Persist uploaded bytes to disk + a metadata row. Returns the asset."""
    brand_id = (brand_id or "default").strip() or "default"
    kind = kind if kind in _VALID_KINDS else "image"
    bg_target = bg_target if bg_target in _VALID_BG_TARGETS else "both"
    name = (name or "asset").strip() or "asset"
    asset_id = uuid.uuid4().hex
    ext = _EXT.get((mime or "").lower(), os.path.splitext(name)[1] or ".bin")
    dest = _assets_dir(brand_id) / f"{asset_id}{ext}"
    dest.write_bytes(data)

    db = get_db()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO brand_assets (id, brand_id, kind, name, mime, path, width, height, bytes, bg_target) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (asset_id, brand_id, kind, name, mime or "application/octet-stream",
             str(dest), width, height, len(data), bg_target),
        )
    got = get_asset(asset_id)
    assert got is not None
    return got


def list_assets(brand_id: Optional[str] = None, kind: Optional[str] = None) -> list[BrandAsset]:
    db = get_db()
    clauses = []
    params: list = []
    if brand_id:
        clauses.append("brand_id = ?")
        params.append(brand_id)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.fetchall(
        f"SELECT * FROM brand_assets{where} ORDER BY created_at DESC", tuple(params)
    )
    return [_row(r) for r in rows]


def get_asset(asset_id: str) -> Optional[BrandAsset]:
    db = get_db()
    row = db.fetchone("SELECT * FROM brand_assets WHERE id = ?", (asset_id,))
    return _row(row) if row else None


def delete_asset(asset_id: str) -> bool:
    asset = get_asset(asset_id)
    if asset is None:
        return False
    try:
        Path(asset.path).unlink(missing_ok=True)
    except OSError:
        pass
    db = get_db()
    with db.write() as conn:
        conn.execute("DELETE FROM brand_assets WHERE id = ?", (asset_id,))
    return True
