# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: ONE graph-document store, instantiated per feature. Node-graph CRUD +
#   optimistic-save guard + prior-graph history + append-only change feed +
#   nestable folders, parameterized by table names. okuro-flow and the
#   orchestrator workflow designer share this mechanism but NOT their data.
# index: GraphConflict | GraphDoc | Folder | GraphDocStore
# AGENT_HEADER_END -->
"""Shared storage mechanism for node-graph documents.

Two different products need the same machinery: okuro-flow (``/flow``) is a
standalone VISUALIZER of complex information, and the orchestrator's workflow
designer is an LLM-ORCHESTRATION tool. Both are ReactFlow graphs that need
history, restore, a clobber guard and live sync — and neither should reimplement
any of it. The slides deck builder already copied the pattern once
(``067_slides.sql``: "Mirrors flow_designer (064)"), so this is removing an
existing duplicate, not just preventing a future one.

**Shared mechanism, separate data.** Each feature constructs its own store over
its own tables. There is no ``kind`` discriminator column, deliberately: a
workflow cannot leak into the ``/flow`` gallery because it is not in the same
table at all, rather than because every query remembered to filter.

The behaviours below were bought with incidents and are preserved exactly as
``flow_designer`` had them — see ``tests/flow_designer`` for the characterization
suite that pins them:

* **Clobber guard.** Emptying a populated graph requires explicit
  ``allow_empty``. A stale tab or a load/switch race can never wipe a canvas.
* **Last-write-wins, not strict OCC.** Strict compare-and-swap turned benign
  ``rev`` drift into 409s that clients "resolved" by reloading — silently
  discarding the user's in-progress edits. History is the safety net instead.
* **Every overwrite snapshots the prior graph**, so even an accepted destructive
  save stays recoverable, and a restore is itself reversible.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from okuro.db import get_db

logger = logging.getLogger("okuro.graphdoc")

# keep the change-feed bounded — only the tail matters for live sync
EVENTS_KEEP = 500
# how many prior-graph snapshots to retain per document (clobber recovery)
HISTORY_KEEP = 50

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return s or "flow"


class GraphConflict(Exception):
    """Raised when a save would destroy content without explicit intent.

    Carries the authoritative current doc so the caller can reconcile instead of
    clobbering. (Named for optimistic concurrency historically; today it fires on
    the empty-save guard, which is the invariant still enforced hard.)
    """

    def __init__(self, current: "GraphDoc"):
        super().__init__(f"graph '{current.id}' changed since rev {current.rev} was loaded")
        self.current = current


@dataclass
class GraphDoc:
    """One graph document. ``graph`` is the full ReactFlow payload."""

    id: str
    name: str
    description: str = ""
    graph: dict[str, Any] = field(default_factory=lambda: {"nodes": [], "edges": []})
    node_count: int = 0
    edge_count: int = 0
    rev: int = 0
    folder_id: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "rev": self.rev,
            "folder_id": self.folder_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_detail(self) -> dict:
        return {**self.to_summary(), "graph": self.graph}


@dataclass
class Folder:
    id: str
    name: str
    parent_id: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "parent_id": self.parent_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class GraphDocStore:
    """Graph-document CRUD over one set of tables.

    ``docs_table`` holds the documents; events and history default to
    ``<docs_table>_events`` / ``<docs_table>_history``. ``folders_table`` is
    explicit because okuro-flow's predates the convention (``flow_folders``).

    ``doc_cls`` lets a caller keep its own dataclass name in its public API
    without a second implementation.
    """

    def __init__(self, docs_table: str, *, folders_table: str,
                 events_table: str | None = None, history_table: str | None = None,
                 doc_cls: type = GraphDoc, conflict_cls: type = GraphConflict):
        self.docs = docs_table
        self.events = events_table or f"{docs_table}_events"
        self.history = history_table or f"{docs_table}_history"
        self.folders = folders_table
        self.doc_cls = doc_cls
        self.conflict_cls = conflict_cls

    # ── documents ────────────────────────────────────────────────────────────
    def _row_to_doc(self, row: dict):
        try:
            graph = json.loads(row["graph"]) if row.get("graph") else {"nodes": [], "edges": []}
        except (ValueError, TypeError):
            graph = {"nodes": [], "edges": []}
        return self.doc_cls(
            id=row["id"],
            name=row["name"],
            description=row.get("description") or "",
            graph=graph,
            node_count=int(row.get("node_count") or 0),
            edge_count=int(row.get("edge_count") or 0),
            rev=int(row.get("rev") or 0),
            folder_id=row.get("folder_id") or None,
            created_at=row.get("created_at") or "",
            updated_at=row.get("updated_at") or "",
        )

    def _unique_id(self, db, base: str, *, ignore: str | None = None) -> str:
        """Return ``base`` or ``base-2``/``base-3``… so the id is unique."""
        candidate = base
        n = 1
        while True:
            if candidate == ignore:
                return candidate
            if db.fetchone(f"SELECT id FROM {self.docs} WHERE id = ?", (candidate,)) is None:
                return candidate
            n += 1
            candidate = f"{base}-{n}"

    def list_docs(self) -> list:
        db = get_db()
        rows = db.fetchall(
            f"SELECT id, name, description, '' AS graph, node_count, edge_count, "
            f"rev, folder_id, created_at, updated_at FROM {self.docs} "
            f"ORDER BY updated_at DESC"
        )
        return [self._row_to_doc(r) for r in rows]

    def get_doc(self, doc_id: str):
        db = get_db()
        row = db.fetchone(f"SELECT * FROM {self.docs} WHERE id = ?", (doc_id,))
        return self._row_to_doc(row) if row else None

    def _emit(self, conn, doc_id: str, kind: str, origin: str) -> None:
        conn.execute(
            f"INSERT INTO {self.events} (flow_id, kind, origin) VALUES (?, ?, ?)",
            (doc_id, kind, origin or ""),
        )
        # prune the change-feed to its tail
        conn.execute(
            f"DELETE FROM {self.events} WHERE seq <= "
            f"(SELECT MAX(seq) FROM {self.events}) - ?",
            (EVENTS_KEEP,),
        )

    def save_doc(self, *, id: str | None = None, name: str, description: str = "",
                 graph: dict[str, Any] | None = None, origin: str = "",
                 base_rev: int | None = None, allow_empty: bool = False):
        """Upsert a graph document. See the module docstring for the guarantees."""
        name = (name or "").strip()
        if not name:
            raise ValueError("name required")

        graph = graph if isinstance(graph, dict) else {"nodes": [], "edges": []}
        graph.setdefault("nodes", [])
        graph.setdefault("edges", [])
        node_count = len(graph.get("nodes") or [])
        edge_count = len(graph.get("edges") or [])
        graph_json = json.dumps(graph, separators=(",", ":"))

        db = get_db()
        with db.write() as conn:
            existing_id = (id or "").strip() or None
            existing = None
            if existing_id:
                existing = conn.execute(
                    f"SELECT graph, node_count, edge_count, rev FROM {self.docs} WHERE id = ?",
                    (existing_id,),
                ).fetchone()

            if existing_id and existing:
                doc_id = existing_id
                cur_rev = int(existing["rev"] or 0)
                prior_nodes = int(existing["node_count"] or 0)
                # NOTE: no rev compare-and-swap — see the module docstring. The ONE
                # invariant still enforced hard: emptying a populated graph requires
                # EXPLICIT intent, so a blank canvas can never wipe real content. A
                # rejected empty-save surfaces as 409 so the client reloads and
                # self-heals. Restore and deliberate "clear all" pass allow_empty.
                if node_count == 0 and prior_nodes > 0 and not allow_empty:
                    raise self.conflict_cls(self.get_doc(doc_id))
                # snapshot the prior graph before overwriting (clobber recovery)
                conn.execute(
                    f"INSERT INTO {self.history} "
                    f"(flow_id, graph, node_count, edge_count, rev, origin) "
                    f"VALUES (?, ?, ?, ?, ?, ?)",
                    (doc_id, existing["graph"], int(existing["node_count"] or 0),
                     int(existing["edge_count"] or 0), cur_rev, origin or ""),
                )
                conn.execute(
                    f"DELETE FROM {self.history} WHERE flow_id = ? AND seq <= "
                    f"(SELECT MAX(seq) FROM {self.history} WHERE flow_id = ?) - ?",
                    (doc_id, doc_id, HISTORY_KEEP),
                )
                conn.execute(
                    f"UPDATE {self.docs} SET name = ?, description = ?, graph = ?, "
                    f"node_count = ?, edge_count = ?, rev = ?, updated_at = datetime('now') "
                    f"WHERE id = ?",
                    (name, description or "", graph_json, node_count, edge_count,
                     cur_rev + 1, doc_id),
                )
            else:
                doc_id = self._unique_id(db, slugify(existing_id or name))
                conn.execute(
                    f"INSERT INTO {self.docs} "
                    f"(id, name, description, graph, node_count, edge_count, rev) "
                    f"VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (doc_id, name, description or "", graph_json, node_count, edge_count),
                )
            self._emit(conn, doc_id, "saved", origin)

        saved = self.get_doc(doc_id)
        assert saved is not None
        return saved

    def delete_doc(self, doc_id: str, *, origin: str = "") -> bool:
        db = get_db()
        with db.write() as conn:
            row = conn.execute(
                f"SELECT id FROM {self.docs} WHERE id = ?", (doc_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute(f"DELETE FROM {self.docs} WHERE id = ?", (doc_id,))
            # History dies with its document. Ids are name slugs, so a later
            # document named the same thing gets the SAME id — leaving the
            # snapshots behind let it inherit a stranger's history and "restore"
            # content it never had. Same transaction as the row delete: a crash
            # must not leave a document half-deleted.
            conn.execute(f"DELETE FROM {self.history} WHERE flow_id = ?", (doc_id,))
            # NOT the events rows: the 'deleted' event below is how the web SSE
            # endpoint tells open canvases the doc is gone. That feed is
            # append-only and self-pruning (EVENTS_KEEP).
            self._emit(conn, doc_id, "deleted", origin)
        return True

    # ── history ──────────────────────────────────────────────────────────────
    def list_history(self, doc_id: str, limit: int = 50) -> list[dict]:
        db = get_db()
        return db.fetchall(
            f"SELECT seq, flow_id, node_count, edge_count, rev, origin, ts "
            f"FROM {self.history} WHERE flow_id = ? ORDER BY seq DESC LIMIT ?",
            (doc_id, int(limit)),
        )

    def restore_history(self, doc_id: str, seq: int, *, origin: str = ""):
        db = get_db()
        snap = db.fetchone(
            f"SELECT graph FROM {self.history} WHERE seq = ? AND flow_id = ?",
            (int(seq), doc_id),
        )
        if snap is None:
            raise ValueError(f"history snapshot {seq} not found for flow '{doc_id}'")
        cur = self.get_doc(doc_id)
        if cur is None:
            raise ValueError(f"flow '{doc_id}' not found")
        try:
            graph = json.loads(snap["graph"]) if snap.get("graph") else {"nodes": [], "edges": []}
        except (ValueError, TypeError):
            graph = {"nodes": [], "edges": []}
        # force-write the restored graph — restore is authoritative (allow_empty in
        # case the chosen snapshot itself was empty)
        return self.save_doc(
            id=doc_id, name=cur.name, description=cur.description,
            graph=graph, origin=origin or "restore", allow_empty=True,
        )

    # ── change feed ──────────────────────────────────────────────────────────
    def events_since(self, seq: int) -> list[dict]:
        db = get_db()
        return db.fetchall(
            f"SELECT seq, flow_id, kind, origin, ts FROM {self.events} "
            f"WHERE seq > ? ORDER BY seq ASC",
            (int(seq),),
        )

    def latest_seq(self) -> int:
        db = get_db()
        row = db.fetchone(f"SELECT MAX(seq) AS s FROM {self.events}")
        return int(row["s"]) if row and row.get("s") is not None else 0

    # ── folders ──────────────────────────────────────────────────────────────
    def _folder_unique_id(self, db, base: str, *, ignore: str | None = None) -> str:
        candidate = base
        n = 1
        while True:
            if candidate == ignore:
                return candidate
            if db.fetchone(f"SELECT id FROM {self.folders} WHERE id = ?", (candidate,)) is None:
                return candidate
            n += 1
            candidate = f"{base}-{n}"

    def list_folders(self) -> list[Folder]:
        db = get_db()
        rows = db.fetchall(
            f"SELECT id, name, parent_id, created_at, updated_at FROM {self.folders} "
            f"ORDER BY name COLLATE NOCASE ASC"
        )
        return [Folder(id=r["id"], name=r["name"], parent_id=r.get("parent_id") or None,
                       created_at=r.get("created_at") or "",
                       updated_at=r.get("updated_at") or "")
                for r in rows]

    def create_folder(self, name: str, parent_id: str | None = None) -> Folder:
        name = (name or "").strip()
        if not name:
            raise ValueError("name required")
        db = get_db()
        with db.write() as conn:
            if parent_id and conn.execute(
                    f"SELECT id FROM {self.folders} WHERE id = ?", (parent_id,)).fetchone() is None:
                raise ValueError(f"parent folder '{parent_id}' not found")
            fid = self._folder_unique_id(db, slugify(name))
            conn.execute(
                f"INSERT INTO {self.folders} (id, name, parent_id) VALUES (?, ?, ?)",
                (fid, name, parent_id or None),
            )
        return {f.id: f for f in self.list_folders()}[fid]

    def _descendants(self, db, folder_id: str) -> set[str]:
        """Folder id + all descendant folder ids (cycle-safe)."""
        seen: set[str] = set()
        stack = [folder_id]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            kids = db.fetchall(f"SELECT id FROM {self.folders} WHERE parent_id = ?", (cur,))
            stack.extend(k["id"] for k in kids)
        return seen

    def update_folder(self, folder_id: str, *, name: str | None = None,
                      parent_id: str | None = ...) -> Folder:
        db = get_db()
        with db.write() as conn:
            row = conn.execute(
                f"SELECT id FROM {self.folders} WHERE id = ?", (folder_id,)).fetchone()
            if row is None:
                raise ValueError(f"folder '{folder_id}' not found")
            if name is not None:
                nm = name.strip()
                if not nm:
                    raise ValueError("name required")
                conn.execute(
                    f"UPDATE {self.folders} SET name = ?, updated_at = datetime('now') "
                    f"WHERE id = ?", (nm, folder_id))
            if parent_id is not ...:
                if parent_id is not None:
                    if parent_id in self._descendants(db, folder_id):
                        raise ValueError("cannot nest a folder under itself or a descendant")
                    if conn.execute(f"SELECT id FROM {self.folders} WHERE id = ?",
                                    (parent_id,)).fetchone() is None:
                        raise ValueError(f"parent folder '{parent_id}' not found")
                conn.execute(
                    f"UPDATE {self.folders} SET parent_id = ?, updated_at = datetime('now') "
                    f"WHERE id = ?", (parent_id or None, folder_id))
        return {f.id: f for f in self.list_folders()}[folder_id]

    def delete_folder(self, folder_id: str) -> bool:
        """Child folders and documents reparent to the deleted folder's parent, so
        nothing is orphaned or hidden."""
        db = get_db()
        with db.write() as conn:
            row = conn.execute(
                f"SELECT parent_id FROM {self.folders} WHERE id = ?", (folder_id,)).fetchone()
            if row is None:
                return False
            parent = row.get("parent_id") or None
            conn.execute(f"UPDATE {self.folders} SET parent_id = ? WHERE parent_id = ?",
                         (parent, folder_id))
            conn.execute(f"UPDATE {self.docs} SET folder_id = ? WHERE folder_id = ?",
                         (parent, folder_id))
            conn.execute(f"DELETE FROM {self.folders} WHERE id = ?", (folder_id,))
        return True

    def set_doc_folder(self, doc_id: str, folder_id: str | None, *, origin: str = ""):
        """Move a document into a folder (or ungrouped). Does not bump ``rev`` —
        organising isn't a graph edit — but emits a ``saved`` event so open
        galleries refresh."""
        db = get_db()
        with db.write() as conn:
            if conn.execute(f"SELECT id FROM {self.docs} WHERE id = ?",
                            (doc_id,)).fetchone() is None:
                return None
            if folder_id and conn.execute(f"SELECT id FROM {self.folders} WHERE id = ?",
                                          (folder_id,)).fetchone() is None:
                raise ValueError(f"folder '{folder_id}' not found")
            conn.execute(f"UPDATE {self.docs} SET folder_id = ? WHERE id = ?",
                         (folder_id or None, doc_id))
            self._emit(conn, doc_id, "saved", origin)
        return self.get_doc(doc_id)
