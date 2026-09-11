# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Licensed icon-library DB access — path resolution, sqlite-vec load, row helpers.
# index: imports | resolve_library_path | open_library | active_embedding_dim
#        | fetch_icons_by_ids | fetch_tags_for_icons | fetch_sets_for_icons
#        | resolve_set_subtree_ids | count_embeddings | LibraryError
# AGENT_HEADER_END -->
"""Access to the icon library SQLite file.

The library (35k SVGs + vectors) is a SEPARATE, licensed, data-dir file — never
in the okuro app DB and never in the repo. Resolution order:

    1. $TM_ICON_LIBRARY            (dir containing library.db + icons/)
    2. $OKURO_ICON_LIBRARY         (okuro-native override)
    3. $XDG_DATA_HOME/okuro/icon-library  (default install location)
    4. ~/.local/share/okuro/icon-library

Schema is owned by the library file itself (created at ingest). This module
opens it read-only for serving and loads the sqlite-vec extension so the
``icon_embedding`` vec0 table is queryable. If the extension fails to load,
callers degrade to FTS-only.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


class LibraryError(RuntimeError):
    """Raised when the icon library is missing or unopenable."""


class LibraryConnection(sqlite3.Connection):
    """sqlite3.Connection subclass that carries library context.

    Plain sqlite3.Connection forbids arbitrary attributes; a subclass allows
    ``icons_dir`` + ``vec_loaded`` to ride along with the handle.
    """

    icons_dir: Path
    vec_loaded: bool = False


def resolve_library_path() -> Path:
    """Return the directory containing ``library.db`` and ``icons/``.

    Raises LibraryError if no library is configured/found.
    """
    for env in ("TM_ICON_LIBRARY", "OKURO_ICON_LIBRARY"):
        val = os.environ.get(env, "").strip()
        if val:
            return Path(val).expanduser().resolve()

    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    candidate = (base / "okuro" / "icon-library").resolve()
    if (candidate / "library.db").exists():
        return candidate

    raise LibraryError(
        "No icon library configured. Set TM_ICON_LIBRARY (or OKURO_ICON_LIBRARY) "
        "to a directory containing library.db + icons/, or import a pack via "
        "`okuro assets icons import <pack.zip>`."
    )


def open_library(library_path: Path | str | None = None, *, readonly: bool = True) -> sqlite3.Connection:
    """Open the library DB and load sqlite-vec. Read-only by default (serving).

    Returns a sqlite3.Connection with row_factory = sqlite3.Row. The connection
    carries ``.icons_dir`` (Path) and ``.vec_loaded`` (bool) attributes.
    """
    path = Path(library_path) if library_path else resolve_library_path()
    db_path = path / "library.db"
    if not db_path.exists():
        raise LibraryError(f"library.db not found at {db_path}")

    if readonly:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, factory=LibraryConnection
        )
    else:
        # Write path: WAL + a generous busy timeout so writes coexist with the
        # MCP server + CLI, which hold the library open read-only (WAL allows
        # one writer alongside many readers).
        conn = sqlite3.connect(str(db_path), factory=LibraryConnection, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    vec_loaded = False
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        vec_loaded = True
    except Exception:
        # Vector path unavailable — search degrades to FTS-only.
        vec_loaded = False

    # Attach helper attributes (sqlite3.Connection allows arbitrary attrs).
    conn.icons_dir = path / "icons"  # type: ignore[attr-defined]
    conn.vec_loaded = vec_loaded  # type: ignore[attr-defined]
    return conn


def active_embedding_dim() -> int | None:
    """Dimension of okuro.embed's active tier, or None if embed unavailable."""
    try:
        from okuro.embed.client import _resolve_tier_spec

        return _resolve_tier_spec().dim
    except Exception:
        return None


# --------------------------------------------------------------------------
# Row helpers (ported from server.ts fetch* functions)
# --------------------------------------------------------------------------

def fetch_icons_by_ids(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    ph = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, name, file_rel_path, favorite, colorability "
        f"FROM icons WHERE id IN ({ph})",
        ids,
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def fetch_tags_for_icons(conn: sqlite3.Connection, ids: list[str]) -> dict[str, list[str]]:
    if not ids:
        return {}
    ph = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT it.icon_id AS icon_id, t.name AS name "
        f"FROM icon_tag it JOIN tags t ON t.id = it.tag_id "
        f"WHERE it.icon_id IN ({ph}) ORDER BY t.name",
        ids,
    ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["icon_id"], []).append(r["name"])
    return out


def fetch_sets_for_icons(conn: sqlite3.Connection, ids: list[str]) -> dict[str, list[str]]:
    if not ids:
        return {}
    ph = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT iset.icon_id AS icon_id, s.name AS name "
        f"FROM icon_set iset JOIN sets s ON s.id = iset.set_id "
        f"WHERE iset.icon_id IN ({ph}) ORDER BY s.name",
        ids,
    ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["icon_id"], []).append(r["name"])
    return out


def resolve_set_subtree_ids(conn: sqlite3.Connection, names: list[str]) -> set[str]:
    """Resolve set names to the set of their ids + all descendant set ids.

    Parent collections walk down: "tabler" matches tabler/outline + tabler/filled.
    Case-insensitive on name.
    """
    cleaned = [n.strip().lower() for n in names if n and n.strip()]
    if not cleaned:
        return set()
    ph = ",".join("?" for _ in cleaned)
    rows = conn.execute(
        f"WITH RECURSIVE subtree(id) AS ("
        f"  SELECT id FROM sets WHERE LOWER(name) IN ({ph}) "
        f"  UNION ALL "
        f"  SELECT s.id FROM sets s JOIN subtree ON s.parent_set_id = subtree.id"
        f") SELECT id FROM subtree",
        cleaned,
    ).fetchall()
    return {r["id"] for r in rows}


def count_embeddings(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT COUNT(1) AS n FROM icon_embedding_meta").fetchone()
        return int(row["n"]) if row else 0
    except sqlite3.Error:
        return 0
