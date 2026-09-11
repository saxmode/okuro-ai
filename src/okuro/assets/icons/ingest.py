# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Import an icon library pack and (re-)embed icons with okuro.embed.
# index: imports | import_pack | reembed | _rebuild_vec_table | _embed_all
# AGENT_HEADER_END -->
"""Icon library ingestion.

A "pack" is a zip of {library.db, icons/*.svg} produced by tm-icon-manager's
make-library-pack. The pack ships pre-computed vectors from a DIFFERENT model
(nomic-embed-text-v1.5, 768d) — incompatible with okuro.embed's active tier.
So ingestion EXTRACTS the pack to the data dir and RE-EMBEDS every icon with
okuro.embed, rebuilding the vec0 table at the active tier's dimension with a
cosine metric. Names/tags/sets/SVGs are kept as-is.

The library lives OUTSIDE the okuro app DB and the repo (it's licensed).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import db as _db

logger = logging.getLogger("okuro.assets.icons.ingest")

_BATCH = 256


def _default_dest() -> Path:
    import os

    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return (base / "okuro" / "icon-library").resolve()


def _rebuild_vec_table(conn: sqlite3.Connection, dim: int) -> None:
    """Drop any existing vec table + meta and recreate at `dim` with cosine.

    Dropping a vec0 virtual table also drops its shadow tables. The meta table
    is plain SQL; we recreate it to be safe and clear its rows.
    """
    conn.execute("DROP TABLE IF EXISTS icon_embedding")
    conn.execute(
        f"CREATE VIRTUAL TABLE icon_embedding USING vec0("
        f"  icon_id TEXT PRIMARY KEY, embedding float[{dim}] distance_metric=cosine)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS icon_embedding_meta ("
        "  icon_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL,"
        "  model TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute("DELETE FROM icon_embedding_meta")


def _embed_all(conn: sqlite3.Connection, *, batch: int = _BATCH) -> int:
    """Embed every icon's search_text via okuro.embed and fill the vec table.

    Returns the number of icons embedded. The document text is the FTS
    search_text (name + tags + set names) so query and document share content.
    """
    from okuro.embed.client import embed, to_bytes
    from okuro.embed.client import _resolve_tier_spec

    model_id = _resolve_tier_spec().model_id
    now = datetime.now(timezone.utc).isoformat()

    rows = conn.execute(
        "SELECT s.icon_id AS icon_id, s.search_text AS text "
        "FROM icon_search s"
    ).fetchall()
    total = len(rows)
    done = 0
    for start in range(0, total, batch):
        chunk = rows[start : start + batch]
        texts = [(r["text"] or r["icon_id"]) for r in chunk]
        vecs = embed(texts)
        conn.executemany(
            "INSERT OR REPLACE INTO icon_embedding (icon_id, embedding) VALUES (?, ?)",
            [(r["icon_id"], to_bytes(v)) for r, v in zip(chunk, vecs)],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO icon_embedding_meta "
            "(icon_id, content_hash, model, created_at) VALUES (?,?,?,?)",
            [
                (
                    r["icon_id"],
                    hashlib.sha256((t).encode("utf-8")).hexdigest(),
                    model_id,
                    now,
                )
                for r, t in zip(chunk, texts)
            ],
        )
        done += len(chunk)
        conn.commit()
        if done % (batch * 10) == 0 or done == total:
            logger.info("re-embedded %d/%d icons", done, total)
    return done


def reembed(library_path: Path | str | None = None, *, batch: int = _BATCH) -> dict:
    """Rebuild the vec table for an already-extracted library at the active dim."""
    dim = _db.active_embedding_dim()
    if not dim:
        raise _db.LibraryError("okuro.embed unavailable — cannot determine embedding dim")
    conn = _db.open_library(library_path, readonly=False)
    try:
        _rebuild_vec_table(conn, dim)
        n = _embed_all(conn, batch=batch)
        return {"embedded": n, "dim": dim}
    finally:
        conn.close()


def import_pack(
    pack_zip: Path | str,
    dest: Path | str | None = None,
    *,
    reembed_after: bool = True,
    batch: int = _BATCH,
) -> dict:
    """Extract a library pack to the data dir and re-embed.

    Returns {"dest", "icons", "embedded", "dim"}.
    """
    pack = Path(pack_zip).expanduser().resolve()
    if not pack.exists():
        raise _db.LibraryError(f"pack not found: {pack}")
    out = Path(dest).expanduser().resolve() if dest else _default_dest()
    out.mkdir(parents=True, exist_ok=True)

    logger.info("extracting %s → %s", pack.name, out)
    with zipfile.ZipFile(pack) as zf:
        zf.extractall(out)
    if not (out / "library.db").exists():
        raise _db.LibraryError(f"pack did not contain library.db (extracted to {out})")

    result: dict = {"dest": str(out)}
    # icon count (read-only peek).
    conn = _db.open_library(out, readonly=True)
    try:
        result["icons"] = conn.execute("SELECT COUNT(1) AS n FROM icons").fetchone()["n"]
    finally:
        conn.close()

    if reembed_after:
        result.update(reembed(out, batch=batch))
    return result
