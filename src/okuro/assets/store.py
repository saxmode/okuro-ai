# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.assets.store — the unified media bucket. One index (okuro.db
#   `assets` + `asset_tags`, migration 080) over ALL media kinds: image | video |
#   audio | icon | illustration. Studio (ComfyUI) generations register here
#   (source=studio, folder=studio-generations); the Studio UI reads the same
#   bucket filtered+flattened; the assets UI reads it across kinds. Tagging is
#   generalised across every kind (mirrors the icon tags machinery, kind-agnostic).
#   Large media bytes stay ON DISK — referenced by rel_path (bucket-owned, under
#   assets_root) or abs_path (referenced-in-place, e.g. the studio generations
#   dir) — this table is the INDEX, not a blob store. The licensed 35k-icon
#   library.db is untouched; icons keep their own read-only provider.
# index:
#   assets_root / studio_dir
#   register_asset / register_studio_output / sync_studio_dir
#   list_assets / get_asset / resolve_file / kinds_summary
#   add_tags / remove_tag / set_title / delete_asset / list_tags
# AGENT_HEADER_END -->
"""Unified media-bucket storage for okuro.assets.

Design
------
* One row per media file in okuro.db `assets`; tags in `asset_tags` (kind-agnostic).
* Bytes stay on disk. `rel_path` is bucket-owned (written under ``assets_root()``);
  `abs_path` references a file in place (studio keeps writing to
  ``~/.okuro/generations`` so the live ``/image/{name}`` route is unaffected).
  Exactly one of the two is set.
* Studio write path: ``register_studio_output`` is called best-effort after a
  generation persists — a store error never fails a generation.
* Studio read path: ``sync_studio_dir`` scans the generations dir and registers
  any file not yet indexed (auto-heals pre-existing generations), so
  ``list_assets(source="studio")`` returns the full, flat set.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from okuro.db import get_db
from okuro.db.engine import okuro_home

logger = logging.getLogger(__name__)

KINDS = ("image", "video", "audio", "icon", "illustration")
SOURCES = ("studio", "upload", "import", "delivery")
STUDIO_FOLDER = "studio-generations"
DELIVERY_FOLDER = "deliveries"

# mime -> file extension for bucket-owned byte writes.
_EXT_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
    "image/gif": ".gif", "image/svg+xml": ".svg",
    "video/mp4": ".mp4", "video/webm": ".webm",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/ogg": ".ogg",
}
# extension -> (kind, mime) for scanning loose files (studio dir sync).
_KIND_BY_EXT = {
    ".png": ("image", "image/png"), ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"), ".webp": ("image", "image/webp"),
    ".gif": ("image", "image/gif"),
    ".mp4": ("video", "video/mp4"), ".webm": ("video", "video/webm"),
    ".mp3": ("audio", "audio/mpeg"), ".wav": ("audio", "audio/wav"),
    ".ogg": ("audio", "audio/ogg"),
}


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------

def assets_root() -> Path:
    """Root dir for bucket-owned bytes (uploads/imports). Studio files are
    referenced in place and do NOT have to live here."""
    return Path(os.environ.get(
        "OKURO_ASSETS_DIR", str(okuro_home() / "assets")))


def studio_dir() -> Path:
    """The on-disk directory Studio writes generations to (referenced in place)."""
    # Import lazily to avoid a heavy inference import at module load.
    from okuro.inference.gen_tools import _output_dir
    return _output_dir()


def deliveries_dir() -> Path:
    """The on-disk directory the delivery pipeline writes generated audio to
    (podcast / summary / morning-brief MP3s) — mirrors channels' _storage_root."""
    override = os.environ.get("OKURO_DELIVERY_STORAGE")
    return Path(override) if override else okuro_home() / "deliveries"


def _norm_tags(tags: Optional[Iterable[str]]) -> list[str]:
    seen: list[str] = []
    for t in tags or []:
        if t is None:
            continue
        v = str(t).strip().lower()
        if v and v not in seen:
            seen.append(v)
    return seen


def _row_to_dict(row: dict) -> dict:
    d = dict(row)
    if d.get("meta"):
        try:
            d["meta"] = json.loads(d["meta"])
        except (TypeError, ValueError):
            d["meta"] = {}
    else:
        d["meta"] = {}
    return d


# --------------------------------------------------------------------------
# Register
# --------------------------------------------------------------------------

def register_asset(
    *,
    kind: str,
    source: str,
    folder: Optional[str] = None,
    path: Optional[str | Path] = None,
    data: Optional[bytes] = None,
    mime: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    title: Optional[str] = None,
    meta: Optional[dict] = None,
    tags: Optional[Iterable[str]] = None,
    db: Any = None,
) -> dict:
    """Index one media asset. Provide EITHER ``data`` (bytes → written under
    ``assets_root()/folder/<id>.<ext>``, stored as rel_path) OR ``path`` (an
    existing file referenced in place, stored as abs_path).

    Referenced-in-place files dedupe on abs_path: a second register of the same
    path returns the existing row (studio re-scan safe).
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r} (expected one of {KINDS})")
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r} (expected one of {SOURCES})")
    if (data is None) == (path is None):
        raise ValueError("provide exactly one of data= or path=")

    db = db or get_db()
    tags = _norm_tags(tags)
    aid = str(uuid.uuid4())
    rel_path: Optional[str] = None
    abs_path: Optional[str] = None
    byte_size: Optional[int] = None

    if data is not None:
        ext = _EXT_BY_MIME.get(mime or "", "")
        sub = (folder or "").strip("/") or "misc"
        dest_dir = assets_root() / sub
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{aid}{ext}"
        dest.write_bytes(data)
        rel_path = str(Path(sub) / dest.name)
        byte_size = len(data)
    else:
        p = Path(path).expanduser().resolve()
        abs_path = str(p)
        try:
            byte_size = p.stat().st_size
        except OSError:
            byte_size = None
        # Dedupe: return the existing row for this file if already indexed.
        existing = db.fetchone("SELECT * FROM assets WHERE abs_path = ?", (abs_path,))
        if existing:
            out = _row_to_dict(existing)
            if tags:  # merge any new tags onto the existing row
                add_tags(out["id"], tags, db=db)
                out = get_asset(out["id"], db=db) or out
            return out

    meta_json = json.dumps(meta, default=str) if meta else None
    with db.write():
        db.execute(
            "INSERT INTO assets (id, kind, source, folder, rel_path, abs_path, "
            "mime, width, height, byte_size, title, meta) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, kind, source, folder, rel_path, abs_path, mime,
             width, height, byte_size, title, meta_json),
        )
        if tags:
            db.conn.executemany(
                "INSERT OR IGNORE INTO asset_tags (asset_id, tag) VALUES (?, ?)",
                [(aid, t) for t in tags],
            )
    return get_asset(aid, db=db)  # type: ignore[return-value]


def _mime_for(path: str | Path) -> str:
    """Mime from the file's own suffix, reusing the dir-sync table.

    Generated files were PNG-only while ComfyUI was the sole engine, so this was
    hardcoded; cloud providers return JPEG. Same table as ``sync_studio_dir`` so
    the write path and the heal path can never disagree about one file.
    """
    kind_mime = _KIND_BY_EXT.get(Path(str(path)).suffix.lower())
    return kind_mime[1] if kind_mime else "image/png"


def register_studio_output(paths: Iterable[str | Path], result: dict, *, db: Any = None) -> list[dict]:
    """Register each file a Studio generation produced (source=studio,
    folder=studio-generations, kind=image). Auto-tags: family, preset, model
    basename, plus 'studio'. Best-effort: exceptions are swallowed + logged so a
    generation never fails because indexing did."""
    out: list[dict] = []
    family = result.get("family")
    preset = result.get("preset")
    model_id = result.get("model_id")
    tags = ["studio"]
    if family:
        tags.append(str(family))
    if preset:
        tags.append(str(preset))
    if model_id:
        tags.append(Path(str(model_id)).name)
    engine = result.get("engine")
    if engine:
        tags.append(str(engine))
    meta = {
        "prompt": result.get("prompt_used"),
        "model_id": model_id,
        "family": family,
        "preset": preset,
        "tier": result.get("tier"),
        "seed": result.get("seed"),
        "workflow_id": result.get("workflow_id"),
        "license": result.get("license"),
        "prompt_plan": result.get("prompt_plan"),
        "engine": engine,
    }
    for p in paths:
        try:
            out.append(register_asset(
                kind="image", source="studio", folder=STUDIO_FOLDER,
                path=p, mime=_mime_for(p), title=(result.get("prompt_used") or None),
                meta=meta, tags=tags, db=db))
        except Exception as exc:  # never break generation on an index failure
            logger.warning("studio asset register failed for %s: %s", p, exc)
    return out


def sync_studio_dir(*, db: Any = None) -> int:
    """Register any file in the studio generations dir not yet indexed. Returns
    the number newly registered. Auto-heals pre-existing generations so the
    Studio read path shows the full flat set. Cheap: dedupe is a unique index."""
    db = db or get_db()
    d = studio_dir()
    if not d.exists():
        return 0
    known = {r["abs_path"] for r in db.fetchall(
        "SELECT abs_path FROM assets WHERE source = 'studio' AND abs_path IS NOT NULL")}
    n = 0
    for f in sorted(d.iterdir()):
        if not f.is_file():
            continue
        kind_mime = _KIND_BY_EXT.get(f.suffix.lower())
        if not kind_mime:
            continue
        if str(f.resolve()) in known:
            continue
        kind, mime = kind_mime
        try:
            register_asset(
                kind=kind, source="studio", folder=STUDIO_FOLDER,
                path=f, mime=mime, tags=["studio"], db=db)
            n += 1
        except Exception as exc:
            logger.warning("studio dir sync skip %s: %s", f, exc)
    return n


# Delivery-audio filename prefix -> a descriptive tag (the source stays
# "delivery"; the tag lets the UI tell podcast/summary/brief apart).
_DELIVERY_KIND = {"podcast-": "podcast", "summary-": "summary", "morning-brief-": "brief"}


def register_delivery_file(path: str | Path, *, db: Any = None) -> Optional[dict]:
    """Register ONE delivery-audio file into the bucket (kind by extension,
    source=delivery, tagged podcast|summary|brief, title=filename). Idempotent —
    dedupes on abs_path, so an on-write call + the read-time sync never double up.
    Returns the asset row, or None when the extension isn't a media kind. The
    on-write hook the delivery channels call right after they save the file."""
    p = Path(path)
    kind_mime = _KIND_BY_EXT.get(p.suffix.lower())
    if not kind_mime or not p.is_file():
        return None
    kind, mime = kind_mime
    label = next((v for pfx, v in _DELIVERY_KIND.items() if p.name.startswith(pfx)), "delivery")
    return register_asset(
        kind=kind, source="delivery", folder=DELIVERY_FOLDER,
        path=p, mime=mime, title=p.stem, tags=[label, "delivery"], db=db)


def sync_deliveries_dir(*, db: Any = None) -> int:
    """Register any audio (or video) file in the delivery storage dir not yet
    indexed — so generated podcasts / summaries / morning-briefs show up in the
    media bucket just like Studio images. Returns the number newly registered."""
    db = db or get_db()
    d = deliveries_dir()
    if not d.exists():
        return 0
    known = {r["abs_path"] for r in db.fetchall(
        "SELECT abs_path FROM assets WHERE folder = ? AND abs_path IS NOT NULL", (DELIVERY_FOLDER,))}
    n = 0
    for f in sorted(d.iterdir()):
        if not f.is_file() or str(f.resolve()) in known:
            continue
        try:
            if register_delivery_file(f, db=db) is not None:
                n += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("deliveries dir sync skip %s: %s", f, exc)
    # Reconcile the other direction: drop index rows whose delivery file has
    # vanished. The index was append-only, so pruned/rotated delivery mp3s left
    # dead rows that 404 on /file → <audio> fails with MediaError. Bidirectional
    # sync self-heals the media grid + the morning-brief player on next load.
    dead = [
        r["id"]
        for r in db.fetchall(
            "SELECT id, abs_path FROM assets WHERE folder = ? AND abs_path IS NOT NULL",
            (DELIVERY_FOLDER,),
        )
        if r["abs_path"] and not Path(r["abs_path"]).exists()
    ]
    if dead:
        qs = ",".join("?" * len(dead))
        with db.write():
            db.execute(f"DELETE FROM asset_tags WHERE asset_id IN ({qs})", tuple(dead))
            db.execute(f"DELETE FROM assets WHERE id IN ({qs})", tuple(dead))
        logger.info("deliveries dir sync pruned %d dead asset row(s)", len(dead))
    return n


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

def list_assets(
    *,
    kind: Optional[str] = None,
    source: Optional[str] = None,
    folder: Optional[str] = None,
    tag: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 60,
    offset: int = 0,
    db: Any = None,
) -> list[dict]:
    """List assets (newest first) with optional filters. ``tag`` restricts to
    rows carrying that tag; ``q`` is a LIKE over title. Each row carries its
    ``tags`` list and parsed ``meta``."""
    db = db or get_db()
    where: list[str] = []
    params: list[Any] = []
    if kind:
        where.append("a.kind = ?"); params.append(kind)
    if source:
        where.append("a.source = ?"); params.append(source)
    if folder:
        where.append("a.folder = ?"); params.append(folder)
    if tag:
        where.append("a.id IN (SELECT asset_id FROM asset_tags WHERE tag = ?)")
        params.append(tag.strip().lower())
    if q:
        where.append("a.title LIKE ?"); params.append(f"%{q}%")
    sql = "SELECT a.* FROM assets a"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.created_at DESC, a.rowid DESC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    rows = [_row_to_dict(r) for r in db.fetchall(sql, tuple(params))]
    if rows:
        _attach_tags(rows, db)
    return rows


def _attach_tags(rows: list[dict], db: Any) -> None:
    ids = [r["id"] for r in rows]
    ph = ",".join("?" for _ in ids)
    tag_rows = db.fetchall(
        f"SELECT asset_id, tag FROM asset_tags WHERE asset_id IN ({ph}) ORDER BY tag",
        tuple(ids))
    by_id: dict[str, list[str]] = {}
    for tr in tag_rows:
        by_id.setdefault(tr["asset_id"], []).append(tr["tag"])
    for r in rows:
        r["tags"] = by_id.get(r["id"], [])


def get_asset(asset_id: str, *, db: Any = None) -> Optional[dict]:
    db = db or get_db()
    row = db.fetchone("SELECT * FROM assets WHERE id = ?", (asset_id,))
    if not row:
        return None
    d = _row_to_dict(row)
    _attach_tags([d], db)
    return d


def resolve_file(asset: dict) -> Optional[Path]:
    """Absolute path to an asset's bytes on disk, or None if it can't be located."""
    if asset.get("abs_path"):
        p = Path(asset["abs_path"])
        return p if p.exists() else None
    if asset.get("rel_path"):
        p = assets_root() / asset["rel_path"]
        return p if p.exists() else None
    return None


def kinds_summary(*, db: Any = None) -> dict[str, int]:
    """Count of assets per kind (for the assets UI kind filter)."""
    db = db or get_db()
    rows = db.fetchall("SELECT kind, COUNT(*) AS n FROM assets GROUP BY kind")
    return {r["kind"]: int(r["n"]) for r in rows}


def list_tags(*, kind: Optional[str] = None, limit: int = 200, db: Any = None) -> list[dict]:
    """Tag vocabulary across the bucket (or one kind), ordered by frequency."""
    db = db or get_db()
    if kind:
        sql = ("SELECT t.tag AS tag, COUNT(*) AS n FROM asset_tags t "
               "JOIN assets a ON a.id = t.asset_id WHERE a.kind = ? "
               "GROUP BY t.tag ORDER BY n DESC, t.tag LIMIT ?")
        rows = db.fetchall(sql, (kind, int(limit)))
    else:
        rows = db.fetchall(
            "SELECT tag, COUNT(*) AS n FROM asset_tags GROUP BY tag "
            "ORDER BY n DESC, tag LIMIT ?", (int(limit),))
    return [{"tag": r["tag"], "count": int(r["n"])} for r in rows]


# --------------------------------------------------------------------------
# Mutate
# --------------------------------------------------------------------------

def add_tags(asset_id: str, tags: Iterable[str], *, db: Any = None) -> list[str]:
    db = db or get_db()
    norm = _norm_tags(tags)
    if norm:
        with db.write():
            db.conn.executemany(
                "INSERT OR IGNORE INTO asset_tags (asset_id, tag) VALUES (?, ?)",
                [(asset_id, t) for t in norm])
    a = get_asset(asset_id, db=db)
    return a["tags"] if a else []


def remove_tag(asset_id: str, tag: str, *, db: Any = None) -> list[str]:
    db = db or get_db()
    with db.write():
        db.execute("DELETE FROM asset_tags WHERE asset_id = ? AND tag = ?",
                   (asset_id, tag.strip().lower()))
    a = get_asset(asset_id, db=db)
    return a["tags"] if a else []


def set_title(asset_id: str, title: Optional[str], *, db: Any = None) -> Optional[dict]:
    db = db or get_db()
    with db.write():
        db.execute("UPDATE assets SET title = ? WHERE id = ?", (title, asset_id))
    return get_asset(asset_id, db=db)


def delete_asset(asset_id: str, *, remove_bytes: bool = False, db: Any = None) -> bool:
    """Delete an asset row (+ tags). Only removes bytes when ``remove_bytes`` and
    the file is bucket-owned (rel_path) — referenced-in-place files are left on
    disk (they may be shared, e.g. the studio generations dir)."""
    db = db or get_db()
    a = get_asset(asset_id, db=db)
    if not a:
        return False
    with db.write():
        db.execute("DELETE FROM asset_tags WHERE asset_id = ?", (asset_id,))
        db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    if remove_bytes and a.get("rel_path"):
        try:
            (assets_root() / a["rel_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    return True
