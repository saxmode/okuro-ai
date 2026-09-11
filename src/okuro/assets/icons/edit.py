# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Write operations on the icon library — favorites + tag editing (with FTS rebuild).
# index: imports | set_favorite | add_tags | remove_tag | _rebuild_search_text
# AGENT_HEADER_END -->
"""Mutating operations on the icon library (Phase B/C — organize).

Opens the library read-WRITE (WAL). The library is also read concurrently by
the MCP server + CLI; WAL permits one writer alongside readers. Tag edits
rebuild the icon's FTS ``search_text`` so keyword search stays consistent;
vector re-embedding on tag change is DEFERRED (vectors go slightly stale until
the next full re-embed — acceptable, noted).
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db as _db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rebuild_search_text(conn: sqlite3.Connection, icon_id: str) -> None:
    """Rebuild icon_search.search_text = name + set names + tag names."""
    name_row = conn.execute("SELECT name FROM icons WHERE id = ?", [icon_id]).fetchone()
    if not name_row:
        return
    sets = [r["name"] for r in conn.execute(
        "SELECT s.name AS name FROM icon_set i JOIN sets s ON s.id = i.set_id WHERE i.icon_id = ?",
        [icon_id],
    ).fetchall()]
    packs = [r["name"] for r in conn.execute(
        "SELECT p.name AS name FROM icon_pack ip JOIN packs p ON p.id = ip.pack_id WHERE ip.icon_id = ?",
        [icon_id],
    ).fetchall()]
    groups = [r["name"] for r in conn.execute(
        "SELECT g.name AS name FROM icon_group ig JOIN groups g ON g.id = ig.group_id WHERE ig.icon_id = ?",
        [icon_id],
    ).fetchall()]
    tags = [r["name"] for r in conn.execute(
        "SELECT t.name AS name FROM icon_tag it JOIN tags t ON t.id = it.tag_id WHERE it.icon_id = ?",
        [icon_id],
    ).fetchall()]
    text = " ".join([name_row["name"], *sets, *packs, *groups, *tags]).strip()
    conn.execute("DELETE FROM icon_search WHERE icon_id = ?", [icon_id])
    conn.execute("INSERT INTO icon_search (icon_id, search_text) VALUES (?, ?)", [icon_id, text])


def set_favorite(
    icon_ids: list[str],
    favorite: bool,
    *,
    library_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Set the favorite flag on one or more icons. Returns {"updated": n}."""
    if not icon_ids:
        return {"updated": 0}
    own = conn is None
    if own:
        conn = _db.open_library(library_path, readonly=False)
    try:
        ph = ",".join("?" for _ in icon_ids)
        cur = conn.execute(
            f"UPDATE icons SET favorite = ?, updated_at = ? WHERE id IN ({ph})",
            [1 if favorite else 0, _now(), *icon_ids],
        )
        conn.commit()
        return {"updated": cur.rowcount, "favorite": favorite}
    finally:
        if own:
            conn.close()


def add_tags(
    icon_id: str,
    names: list[str],
    *,
    library_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Attach tags (by name, created if new) to an icon. Returns the icon's tags."""
    own = conn is None
    if own:
        conn = _db.open_library(library_path, readonly=False)
    try:
        for raw in names:
            name = raw.strip()
            if not name:
                continue
            row = conn.execute("SELECT id FROM tags WHERE LOWER(name) = ?", [name.lower()]).fetchone()
            if row:
                tag_id = row["id"]
            else:
                tag_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO tags (id, name, created_at) VALUES (?, ?, ?)",
                    [tag_id, name, _now()],
                )
            conn.execute(
                "INSERT OR IGNORE INTO icon_tag (icon_id, tag_id) VALUES (?, ?)",
                [icon_id, tag_id],
            )
        _rebuild_search_text(conn, icon_id)
        conn.commit()
        return _tags_of(conn, icon_id)
    finally:
        if own:
            conn.close()


def remove_tag(
    icon_id: str,
    name: str,
    *,
    library_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Detach a tag (by name) from an icon. Returns the icon's remaining tags."""
    own = conn is None
    if own:
        conn = _db.open_library(library_path, readonly=False)
    try:
        row = conn.execute("SELECT id FROM tags WHERE LOWER(name) = ?", [name.strip().lower()]).fetchone()
        if row:
            conn.execute(
                "DELETE FROM icon_tag WHERE icon_id = ? AND tag_id = ?", [icon_id, row["id"]]
            )
            _rebuild_search_text(conn, icon_id)
            conn.commit()
        return _tags_of(conn, icon_id)
    finally:
        if own:
            conn.close()


def _tags_of(conn: sqlite3.Connection, icon_id: str) -> dict[str, Any]:
    tags = [r["name"] for r in conn.execute(
        "SELECT t.name AS name FROM icon_tag it JOIN tags t ON t.id = it.tag_id "
        "WHERE it.icon_id = ? ORDER BY t.name",
        [icon_id],
    ).fetchall()]
    return {"icon_id": icon_id, "tags": tags}


# --------------------------------------------------------------------------
# Containers: sets / groups / packs (create / rename / delete / move / attach)
# --------------------------------------------------------------------------
# (table, membership_table, fk_col). Only `set` has a parent (hierarchy).
_CONTAINER = {
    "set": ("sets", "icon_set", "set_id"),
    "group": ("groups", "icon_group", "group_id"),
    "pack": ("packs", "icon_pack", "pack_id"),
}


def _container_def(ctype: str):
    if ctype not in _CONTAINER:
        raise ValueError(f"unknown container type: {ctype} (expected set|group|pack)")
    return _CONTAINER[ctype]


def create_container(
    ctype: str,
    name: str,
    parent_set_id: str | None = None,
    *,
    library_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Create a set/group/pack. parent_set_id applies to sets only."""
    table, _, _ = _container_def(ctype)
    name = name.strip()
    if not name:
        raise ValueError("name required")
    own = conn is None
    if own:
        conn = _db.open_library(library_path, readonly=False)
    try:
        cid = str(uuid.uuid4())
        if ctype == "set":
            conn.execute(
                "INSERT INTO sets (id, name, parent_set_id, created_at) VALUES (?,?,?,?)",
                [cid, name, parent_set_id, _now()],
            )
        else:
            conn.execute(f"INSERT INTO {table} (id, name, created_at) VALUES (?,?,?)", [cid, name, _now()])
        conn.commit()
        return {"id": cid, "type": ctype, "name": name, "parent_set_id": parent_set_id if ctype == "set" else None}
    finally:
        if own:
            conn.close()


def rename_container(
    ctype: str, container_id: str, name: str,
    *, library_path: Path | str | None = None, conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    table, _, _ = _container_def(ctype)
    name = name.strip()
    if not name:
        raise ValueError("name required")
    own = conn is None
    if own:
        conn = _db.open_library(library_path, readonly=False)
    try:
        conn.execute(f"UPDATE {table} SET name = ? WHERE id = ?", [name, container_id])
        # set/pack/group names feed FTS — rebuild affected icons.
        _rebuild_members_fts(conn, ctype, container_id)
        conn.commit()
        return {"id": container_id, "type": ctype, "name": name}
    finally:
        if own:
            conn.close()


def delete_container(
    ctype: str, container_id: str,
    *, library_path: Path | str | None = None, conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Delete a container. Membership rows cascade (FK ON DELETE CASCADE); icons
    themselves are untouched. For sets, children reparent to NULL (FK SET NULL)."""
    table, _, _ = _container_def(ctype)
    own = conn is None
    if own:
        conn = _db.open_library(library_path, readonly=False)
    try:
        members = _member_ids(conn, ctype, container_id)
        conn.execute(f"DELETE FROM {table} WHERE id = ?", [container_id])
        for iid in members:
            _rebuild_search_text(conn, iid)
        conn.commit()
        return {"deleted": container_id, "type": ctype, "members_updated": len(members)}
    finally:
        if own:
            conn.close()


def move_set(
    set_id: str, parent_set_id: str | None,
    *, library_path: Path | str | None = None, conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    own = conn is None
    if own:
        conn = _db.open_library(library_path, readonly=False)
    try:
        if parent_set_id == set_id:
            raise ValueError("a set cannot be its own parent")
        conn.execute("UPDATE sets SET parent_set_id = ? WHERE id = ?", [parent_set_id, set_id])
        conn.commit()
        return {"id": set_id, "parent_set_id": parent_set_id}
    finally:
        if own:
            conn.close()


def attach_icons(
    ctype: str, container_id: str, icon_ids: list[str],
    *, library_path: Path | str | None = None, conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Add icons to a set/group/pack. Rebuilds FTS for each attached icon."""
    _, membership, fk = _container_def(ctype)
    if not icon_ids:
        return {"attached": 0}
    own = conn is None
    if own:
        conn = _db.open_library(library_path, readonly=False)
    try:
        conn.executemany(
            f"INSERT OR IGNORE INTO {membership} (icon_id, {fk}) VALUES (?, ?)",
            [(iid, container_id) for iid in icon_ids],
        )
        for iid in icon_ids:
            _rebuild_search_text(conn, iid)
        conn.commit()
        return {"attached": len(icon_ids), "type": ctype, "container_id": container_id}
    finally:
        if own:
            conn.close()


def detach_icons(
    ctype: str, container_id: str, icon_ids: list[str],
    *, library_path: Path | str | None = None, conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    _, membership, fk = _container_def(ctype)
    if not icon_ids:
        return {"detached": 0}
    own = conn is None
    if own:
        conn = _db.open_library(library_path, readonly=False)
    try:
        ph = ",".join("?" for _ in icon_ids)
        conn.execute(
            f"DELETE FROM {membership} WHERE {fk} = ? AND icon_id IN ({ph})",
            [container_id, *icon_ids],
        )
        for iid in icon_ids:
            _rebuild_search_text(conn, iid)
        conn.commit()
        return {"detached": len(icon_ids), "type": ctype, "container_id": container_id}
    finally:
        if own:
            conn.close()


def _member_ids(conn: sqlite3.Connection, ctype: str, container_id: str) -> list[str]:
    _, membership, fk = _container_def(ctype)
    return [r["icon_id"] for r in conn.execute(
        f"SELECT icon_id FROM {membership} WHERE {fk} = ?", [container_id]
    ).fetchall()]


def _rebuild_members_fts(conn: sqlite3.Connection, ctype: str, container_id: str) -> None:
    for iid in _member_ids(conn, ctype, container_id):
        _rebuild_search_text(conn, iid)


# --------------------------------------------------------------------------
# Import (Phase D) + Delete (Phase F)
# --------------------------------------------------------------------------
import re as _re


def _sanitize_svg(svg: str) -> str:
    """Light sanitize for import: drop xml prolog / doctype / comments, trim.
    (Full svgo optimization is deferred — this keeps the file valid + compact.)"""
    s = svg.strip()
    s = _re.sub(r"<\?xml[^>]*\?>", "", s)
    s = _re.sub(r"<!DOCTYPE[^>]*>", "", s, flags=_re.I)
    s = _re.sub(r"<!--.*?-->", "", s, flags=_re.S)
    return s.strip()


def _embed_icon(conn: sqlite3.Connection, icon_id: str, text: str) -> bool:
    """Embed a single icon's text into vec0 + meta. No-op (returns False) when
    okuro.embed is unavailable — FTS search still works."""
    try:
        from okuro.embed.client import embed_one, to_bytes, is_available, _resolve_tier_spec
        import hashlib
        if not is_available():
            return False
        vec = embed_one(text)
        conn.execute(
            "INSERT OR REPLACE INTO icon_embedding (icon_id, embedding) VALUES (?, ?)",
            [icon_id, to_bytes(vec)],
        )
        conn.execute(
            "INSERT OR REPLACE INTO icon_embedding_meta (icon_id, content_hash, model, created_at) "
            "VALUES (?,?,?,?)",
            [icon_id, hashlib.sha256(text.encode()).hexdigest(), _resolve_tier_spec().model_id, _now()],
        )
        return True
    except Exception:
        return False


def import_svgs(
    items: list[dict],
    set_id: str | None = None,
    *,
    library_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Import SVGs. items = [{name, svg}]. Writes each to the icons dir, inserts
    icon + FTS, optionally attaches to a set, and re-embeds. Returns
    {imported, ids, embedded}."""
    own = conn is None
    if own:
        conn = _db.open_library(library_path, readonly=False)
    try:
        icons_dir = Path(conn.icons_dir)  # type: ignore[attr-defined]
        icons_dir.mkdir(parents=True, exist_ok=True)
        ids: list[str] = []
        embedded = 0
        for item in items:
            svg = _sanitize_svg(item.get("svg") or "")
            name = (item.get("name") or "icon").strip()
            if not svg or "<svg" not in svg.lower():
                continue
            iid = str(uuid.uuid4())
            rel = f"{iid}.svg"
            (icons_dir / rel).write_text(svg, encoding="utf-8")
            now = _now()
            conn.execute(
                "INSERT INTO icons (id,name,file_rel_path,favorite,colorability,created_at,updated_at) "
                "VALUES (?,?,?,0,'fixed',?,?)",
                [iid, name, rel, now, now],
            )
            if set_id:
                conn.execute("INSERT OR IGNORE INTO icon_set (icon_id,set_id) VALUES (?,?)", [iid, set_id])
            _rebuild_search_text(conn, iid)
            # FTS text = name + set names; embed the same.
            txt = conn.execute("SELECT search_text FROM icon_search WHERE icon_id = ?", [iid]).fetchone()
            if _embed_icon(conn, iid, (txt["search_text"] if txt else name)):
                embedded += 1
            ids.append(iid)
        conn.commit()
        return {"imported": len(ids), "ids": ids, "embedded": embedded}
    finally:
        if own:
            conn.close()


def delete_icons(
    icon_ids: list[str],
    *,
    library_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Delete icons: rows (membership cascades via FK), SVG files, FTS rows, and
    embeddings. Returns {deleted}."""
    if not icon_ids:
        return {"deleted": 0}
    own = conn is None
    if own:
        conn = _db.open_library(library_path, readonly=False)
    try:
        icons_dir = Path(conn.icons_dir)  # type: ignore[attr-defined]
        ph = ",".join("?" for _ in icon_ids)
        rels = [r["file_rel_path"] for r in conn.execute(
            f"SELECT file_rel_path FROM icons WHERE id IN ({ph})", icon_ids
        ).fetchall()]
        conn.execute(f"DELETE FROM icon_search WHERE icon_id IN ({ph})", icon_ids)
        for tbl in ("icon_embedding", "icon_embedding_meta"):
            try:
                conn.execute(f"DELETE FROM {tbl} WHERE icon_id IN ({ph})", icon_ids)
            except sqlite3.Error:
                pass
        cur = conn.execute(f"DELETE FROM icons WHERE id IN ({ph})", icon_ids)  # cascades icon_set/tag/pack/group
        conn.commit()
        for rel in rels:
            try:
                (icons_dir / rel).unlink(missing_ok=True)
            except OSError:
                pass
        return {"deleted": cur.rowcount}
    finally:
        if own:
            conn.close()
