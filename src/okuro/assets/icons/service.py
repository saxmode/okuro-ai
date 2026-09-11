# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Icon engine ops — search_icons / get_icon / list_sets / list_tags / library_stats.
# index: imports | constants | _fts_match | _vec_match | search_icons
#        | get_icon | list_sets | list_tags | library_stats
# AGENT_HEADER_END -->
"""Icon search/serve operations, ported from tm-icon-manager src/mcp/server.ts.

Hybrid search fuses BM25 full-text (names + tags + set/pack/group names) with
semantic vector similarity, via reciprocal-rank fusion. Embeddings are produced
by okuro.embed's active tier (query side asymmetric via embed_query); the vector
path degrades to FTS-only when okuro.embed is unavailable.

C5 FIX (vs the TS engine): a set filter is pushed INTO the FTS SQL (subtree
subquery) so oversampling happens *within* the allowed sets. The TS engine
oversampled globally then post-filtered, so a narrow set (lucide ~5%) was
crowded out by a dominant one (sl-ultimate ~49%) and returned 0.
"""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path
from typing import Any

from . import db as _db
from .search import build_fts_query, rrf_fuse

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# Asymmetric-query instruction for instruction-tuned tiers (Qwen3). Domain-
# specific so the query embedding lands in icon space, not the role-retrieval
# default baked into embed_query.
_ICON_QUERY_INSTRUCTION = (
    "Given a search phrase, retrieve the icon whose name and tags best match it."
)


def _kebab(name: str) -> str:
    """Lowercase kebab-case form of an icon name (import-friendly)."""
    out = []
    prev_dash = False
    for ch in name.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")


def _subtree_filter_sql(set_names: list[str]) -> tuple[str, list[str]]:
    """Build an ``icon_id IN (... subtree ...)`` SQL fragment + params.

    Returns ("", []) when no usable names — caller omits the clause.
    """
    cleaned = [n.strip().lower() for n in set_names if n and n.strip()]
    if not cleaned:
        return "", []
    ph = ",".join("?" for _ in cleaned)
    sql = (
        f" AND icon_id IN ("
        f"  WITH RECURSIVE subtree(id) AS ("
        f"    SELECT id FROM sets WHERE LOWER(name) IN ({ph}) "
        f"    UNION ALL "
        f"    SELECT s.id FROM sets s JOIN subtree ON s.parent_set_id = subtree.id"
        f"  ) SELECT icon_id FROM icon_set WHERE set_id IN (SELECT id FROM subtree)"
        f")"
    )
    return sql, cleaned


def _fts_match(
    conn: sqlite3.Connection,
    raw: str,
    limit: int,
    set_names: list[str] | None,
) -> list[tuple[str, float]]:
    fts_query = build_fts_query(raw)
    if not fts_query:
        return []
    clause, params = ("", [])
    if set_names:
        clause, params = _subtree_filter_sql(set_names)
    try:
        rows = conn.execute(
            f"SELECT icon_id, bm25(icon_search) AS score FROM icon_search "
            f"WHERE icon_search MATCH ?{clause} ORDER BY score LIMIT ?",
            [fts_query, *params, limit],
        ).fetchall()
        return [(r["icon_id"], r["score"]) for r in rows]
    except sqlite3.Error:
        return []


def _vec_match(conn: sqlite3.Connection, qvec_bytes: bytes, limit: int) -> list[tuple[str, float]]:
    try:
        rows = conn.execute(
            "SELECT icon_id, distance FROM icon_embedding "
            "WHERE embedding MATCH ? AND k = ?",
            [qvec_bytes, limit],
        ).fetchall()
        return [(r["icon_id"], r["distance"]) for r in rows]
    except sqlite3.Error:
        return []


def search_icons(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    tag: str | None = None,
    set: str | list[str] | None = None,  # noqa: A002 — mirrors the tool arg name
    favorite: bool | None = None,
    include_svg: bool = False,
    library_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Hybrid search. Returns {"count", "results": [...]}.

    The vector path is used only when the library has embeddings AND okuro.embed
    can produce a query vector; otherwise results are BM25-only. When
    ``include_svg`` is set, each result carries inline ``svg`` text (for grid
    rendering without per-icon auth'd requests).
    """
    own = conn is None
    if own:
        conn = _db.open_library(library_path)
    try:
        k = max(1, min(int(limit), MAX_LIMIT))
        oversample = min(k * 4, MAX_LIMIT * 2)
        set_names = (
            ([set] if isinstance(set, str) else list(set)) if set else None
        )

        fts = _fts_match(conn, query, oversample, set_names)

        vec: list[tuple[str, float]] = []
        if getattr(conn, "vec_loaded", False) and _db.count_embeddings(conn) > 0:
            try:
                from okuro.embed.client import embed_query, to_bytes

                qvec = embed_query(query, instruction=_ICON_QUERY_INSTRUCTION)
                # When set-filtering, the vec KNN can't be pushed-filtered, so
                # widen oversample and intersect below to keep narrow sets alive.
                vlimit = MAX_LIMIT * 2 if set_names else oversample
                vec = _vec_match(conn, to_bytes(qvec), vlimit)
            except Exception:
                vec = []

        fused = rrf_fuse(fts, vec)
        ids = list(fused.keys())
        if not ids:
            return {"count": 0, "results": []}

        icon_map = _db.fetch_icons_by_ids(conn, ids)
        tag_map = _db.fetch_tags_for_icons(conn, ids)
        set_map = _db.fetch_sets_for_icons(conn, ids)

        allowed_ids: set[str] | None = None
        if set_names:
            allowed_set_ids = _db.resolve_set_subtree_ids(conn, set_names)
            if allowed_set_ids:
                ph = ",".join("?" for _ in ids)
                sph = ",".join("?" for _ in allowed_set_ids)
                rows = conn.execute(
                    f"SELECT DISTINCT icon_id FROM icon_set "
                    f"WHERE icon_id IN ({ph}) AND set_id IN ({sph})",
                    [*ids, *allowed_set_ids],
                ).fetchall()
                allowed_ids = {r["icon_id"] for r in rows}
            else:
                allowed_ids = set()

        tag_lc = tag.lower() if tag else None
        ranked: list[dict] = []
        for icon_id in ids:
            icon = icon_map.get(icon_id)
            if not icon:
                continue
            if favorite is True and icon.get("favorite") != 1:
                continue
            if allowed_ids is not None and icon_id not in allowed_ids:
                continue
            tags = tag_map.get(icon_id, [])
            if tag_lc and not any(t.lower() == tag_lc for t in tags):
                continue
            f = fused[icon_id]
            entry = {
                "id": icon["id"],
                "name": icon["name"],
                "kebab_name": _kebab(icon["name"]),
                "tags": tags,
                "sets": set_map.get(icon_id, []),
                "favorite": icon.get("favorite") == 1,
                "colorability": icon.get("colorability"),
                "score": round(f["score"], 6),
                "vec_score": round(f["vec_score"], 4) if f["vec_score"] is not None else None,
                "fts_score": round(f["fts_score"], 4) if f["fts_score"] is not None else None,
            }
            if include_svg:
                try:
                    entry["svg"] = (Path(conn.icons_dir) / icon["file_rel_path"]).read_text(  # type: ignore[attr-defined]
                        encoding="utf-8"
                    )
                except OSError:
                    entry["svg"] = None
            ranked.append(entry)

        ranked.sort(key=lambda r: r["score"], reverse=True)
        ranked = ranked[:k]
        return {"count": len(ranked), "results": ranked}
    finally:
        if own:
            conn.close()


_CURSOR_SEP = "\x1f"


def browse_icons(
    *,
    set: str | list[str] | None = None,  # noqa: A002 — mirrors the tool arg name
    tag: str | None = None,
    favorite: bool | None = None,
    cursor: str | None = None,
    limit: int = 120,
    include_svg: bool = True,
    library_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """List icons by set/tag/favorite, ordered by name, keyset-paginated.

    No FTS/vector — this is the browse path (empty search box + a sidebar
    filter). Returns {"count", "results", "next_cursor"}. next_cursor is a
    `name\\x1fid` token to pass back for the next page (None when exhausted).
    """
    own = conn is None
    if own:
        conn = _db.open_library(library_path)
    try:
        k = max(1, min(int(limit), 600))
        where: list[str] = []
        params: list[Any] = []
        joins = ""

        set_names = ([set] if isinstance(set, str) else list(set)) if set else None
        if set_names:
            allowed = _db.resolve_set_subtree_ids(conn, set_names)
            if not allowed:
                return {"count": 0, "results": [], "next_cursor": None}
            sph = ",".join("?" for _ in allowed)
            where.append(f"i.id IN (SELECT icon_id FROM icon_set WHERE set_id IN ({sph}))")
            params.extend(allowed)
        if tag:
            where.append(
                "i.id IN (SELECT it.icon_id FROM icon_tag it JOIN tags t ON t.id = it.tag_id "
                "WHERE LOWER(t.name) = ?)"
            )
            params.append(tag.strip().lower())
        if favorite is True:
            where.append("i.favorite = 1")
        if cursor and _CURSOR_SEP in cursor:
            c_name, c_id = cursor.split(_CURSOR_SEP, 1)
            where.append("(i.name > ? OR (i.name = ? AND i.id > ?))")
            params.extend([c_name, c_name, c_id])

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT i.id, i.name, i.file_rel_path, i.favorite, i.colorability "
            f"FROM icons i{joins}{clause} ORDER BY i.name, i.id LIMIT ?",
            [*params, k + 1],
        ).fetchall()

        has_more = len(rows) > k
        rows = rows[:k]
        ids = [r["id"] for r in rows]
        tag_map = _db.fetch_tags_for_icons(conn, ids)
        set_map = _db.fetch_sets_for_icons(conn, ids)

        results: list[dict] = []
        for r in rows:
            entry = {
                "id": r["id"],
                "name": r["name"],
                "kebab_name": _kebab(r["name"]),
                "tags": tag_map.get(r["id"], []),
                "sets": set_map.get(r["id"], []),
                "favorite": r["favorite"] == 1,
                "colorability": r["colorability"],
            }
            if include_svg:
                try:
                    entry["svg"] = (Path(conn.icons_dir) / r["file_rel_path"]).read_text(  # type: ignore[attr-defined]
                        encoding="utf-8"
                    )
                except OSError:
                    entry["svg"] = None
            results.append(entry)

        next_cursor = None
        if has_more and results:
            last = results[-1]
            next_cursor = f"{last['name']}{_CURSOR_SEP}{last['id']}"
        return {"count": len(results), "results": results, "next_cursor": next_cursor}
    finally:
        if own:
            conn.close()


def get_icon(
    icon_id: str,
    *,
    format: str = "svg",  # noqa: A002 — mirrors the tool arg name
    library_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Fetch one icon by id. format = svg | base64 | path."""
    own = conn is None
    if own:
        conn = _db.open_library(library_path)
    try:
        row = conn.execute(
            "SELECT id, name, file_rel_path, favorite, colorability FROM icons WHERE id = ?",
            [icon_id],
        ).fetchone()
        if not row:
            return {"error": f"no icon with id={icon_id}"}
        tags = _db.fetch_tags_for_icons(conn, [icon_id]).get(icon_id, [])
        sets = _db.fetch_sets_for_icons(conn, [icon_id]).get(icon_id, [])
        abs_path = Path(conn.icons_dir) / row["file_rel_path"]  # type: ignore[attr-defined]
        meta = {
            "id": row["id"],
            "name": row["name"],
            "kebab_name": _kebab(row["name"]),
            "tags": tags,
            "sets": sets,
            "favorite": row["favorite"] == 1,
            "colorability": row["colorability"],
            "path": str(abs_path),
        }
        if format == "path":
            return meta
        try:
            svg = abs_path.read_text(encoding="utf-8")
        except OSError as e:
            return {**meta, "error": f"svg not readable: {e}"}
        if format == "base64":
            b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
            return {**meta, "data_uri": f"data:image/svg+xml;base64,{b64}"}
        return {**meta, "svg": svg}
    finally:
        if own:
            conn.close()


def list_sets(
    *, library_path: Path | str | None = None, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    own = conn is None
    if own:
        conn = _db.open_library(library_path)
    try:
        rows = conn.execute(
            "SELECT s.id AS id, s.name AS name, s.parent_set_id AS parent_set_id, "
            "COUNT(iset.icon_id) AS icon_count "
            "FROM sets s LEFT JOIN icon_set iset ON iset.set_id = s.id "
            "GROUP BY s.id ORDER BY s.name"
        ).fetchall()
        return {"sets": [dict(r) for r in rows]}
    finally:
        if own:
            conn.close()


def list_tags(
    prefix: str | None = None,
    *,
    limit: int = 100,
    library_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    own = conn is None
    if own:
        conn = _db.open_library(library_path)
    try:
        limit = max(1, min(int(limit), 500))
        pfx = (prefix or "").strip().lower()
        if pfx:
            rows = conn.execute(
                "SELECT t.name AS name, COUNT(it.icon_id) AS icon_count "
                "FROM tags t LEFT JOIN icon_tag it ON it.tag_id = t.id "
                "WHERE LOWER(t.name) LIKE ? "
                "GROUP BY t.id ORDER BY icon_count DESC, t.name LIMIT ?",
                [f"{pfx}%", limit],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT t.name AS name, COUNT(it.icon_id) AS icon_count "
                "FROM tags t LEFT JOIN icon_tag it ON it.tag_id = t.id "
                "GROUP BY t.id ORDER BY icon_count DESC, t.name LIMIT ?",
                [limit],
            ).fetchall()
        return {"count": len(rows), "tags": [dict(r) for r in rows]}
    finally:
        if own:
            conn.close()


def library_stats(
    *, library_path: Path | str | None = None, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    own = conn is None
    if own:
        conn = _db.open_library(library_path)
    try:
        def _count(sql: str) -> int:
            try:
                r = conn.execute(sql).fetchone()
                return int(r["n"]) if r else 0
            except sqlite3.Error:
                return 0

        return {
            "icons_total": _count("SELECT COUNT(1) AS n FROM icons"),
            "icons_embedded": _db.count_embeddings(conn),
            "icons_favorite": _count("SELECT COUNT(1) AS n FROM icons WHERE favorite = 1"),
            "tags_total": _count("SELECT COUNT(1) AS n FROM tags"),
            "sets_total": _count("SELECT COUNT(1) AS n FROM sets"),
            "vec_loaded": bool(getattr(conn, "vec_loaded", False)),
        }
    finally:
        if own:
            conn.close()


def export_svgs(
    icon_ids: list[str],
    *,
    library_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Gather SVG text + names for the given icons (for zip export). Read-only.
    Returns [{id, name, kebab_name, file_name, svg}]."""
    if not icon_ids:
        return []
    own = conn is None
    if own:
        conn = _db.open_library(library_path)
    try:
        ph = ",".join("?" for _ in icon_ids)
        rows = conn.execute(
            f"SELECT id, name, file_rel_path FROM icons WHERE id IN ({ph})", icon_ids
        ).fetchall()
        out: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        for r in rows:
            try:
                svg = (Path(conn.icons_dir) / r["file_rel_path"]).read_text(encoding="utf-8")  # type: ignore[attr-defined]
            except OSError:
                continue
            base = _kebab(r["name"]) or r["id"]
            n = seen.get(base, 0)
            seen[base] = n + 1
            fname = f"{base}.svg" if n == 0 else f"{base}-{n}.svg"
            out.append({"id": r["id"], "name": r["name"], "kebab_name": base, "file_name": fname, "svg": svg})
        return out
    finally:
        if own:
            conn.close()
