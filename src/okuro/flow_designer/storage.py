# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-flow storage — a thin binding of the shared GraphDocStore to
#   okuro-flow's own tables. All mechanism lives in okuro.graphdoc.store; this
#   module is the public surface (names, signatures, dataclass identities) that
#   the web app and the flow_designer_* MCP tools already import.
# index: FlowConflict | FlowDoc | Folder | list_flows | save_flow | delete_flow
#   | list_history | restore_history | events_since | latest_seq | folders
# AGENT_HEADER_END -->
"""okuro-flow storage — SQLite-backed CRUD + append-only change-feed.

okuro-flow is the generic ReactFlow node/graph canvas: one JSON blob per document
(``{"nodes": [...], "edges": [...], "viewport"?: {...}, "settings"?: {...}}``).
It carries **no** orchestration semantics — it is a free-form diagram store, a
standalone visualizer, and it must stay that way. Deliberately separate from
``okuro.flows`` (the legacy star-topology role store); they share nothing but the
word "flow".

Every mutation appends a row to ``flow_designer_events`` so the web SSE endpoint
can fan changes out to open canvases — including changes made by agents in the
separate stdio MCP process. ``origin`` lets a client ignore its own echo.

**The mechanism now lives in ``okuro.graphdoc.store``** and is shared with the
orchestrator's workflow designer — same machinery, separate tables, so a workflow
can never appear in the ``/flow`` gallery. Behaviour here is unchanged; the
characterization suite in ``tests/flow_designer`` pins it.
"""

from __future__ import annotations

import logging
from typing import Any

from okuro.graphdoc.store import EVENTS_KEEP as _EVENTS_KEEP  # noqa: F401 (compat)
from okuro.graphdoc.store import HISTORY_KEEP as _HISTORY_KEEP  # noqa: F401 (compat)
from okuro.graphdoc.store import Folder, GraphConflict, GraphDoc, GraphDocStore, slugify

logger = logging.getLogger("okuro.flow_designer")


class FlowConflict(GraphConflict):
    """Raised when a save would destroy a populated canvas without explicit
    intent. Carries the authoritative current doc (``.current``) so the caller can
    reconcile instead of clobbering."""


class FlowDoc(GraphDoc):
    """One okuro-flow document. ``graph`` is the full ReactFlow payload."""


_store = GraphDocStore(
    "flow_designer",
    folders_table="flow_folders",      # predates the <docs>_folders convention
    doc_cls=FlowDoc,
    conflict_cls=FlowConflict,
)


def list_flows() -> list[FlowDoc]:
    return _store.list_docs()


def get_flow(flow_id: str) -> FlowDoc | None:
    return _store.get_doc(flow_id)


def save_flow(
    *,
    id: str | None = None,
    name: str,
    description: str = "",
    graph: dict[str, Any] | None = None,
    origin: str = "",
    base_rev: int | None = None,
    allow_empty: bool = False,
) -> FlowDoc:
    """Upsert a flow. Generates a unique slug id from ``name`` when ``id`` is
    absent. Emits a ``saved`` change-feed event.

    ``base_rev`` is accepted and ignored: this is a single-user tool, and strict
    compare-and-swap turned every benign rev drift into a 409 that the client
    "resolved" by reloading — silently discarding in-progress edits. Last-write-
    wins plus history is the right trade here.

    The ONE invariant enforced hard: emptying a populated flow requires
    ``allow_empty``, so a blank canvas (stale tab, or the load/switch race) can
    never wipe real content. A rejected empty-save raises ``FlowConflict``.

    Every overwrite snapshots the PRIOR graph into ``flow_designer_history`` so an
    accepted-but-destructive save stays recoverable.
    """
    return _store.save_doc(
        id=id, name=name, description=description, graph=graph,
        origin=origin, base_rev=base_rev, allow_empty=allow_empty,
    )


def delete_flow(flow_id: str, *, origin: str = "") -> bool:
    return _store.delete_doc(flow_id, origin=origin)


def list_history(flow_id: str, limit: int = 50) -> list[dict]:
    """Prior-graph snapshots for a flow, newest first (metadata only — no graph
    blob, to keep the list cheap)."""
    return _store.list_history(flow_id, limit)


def restore_history(flow_id: str, seq: int, *, origin: str = "") -> FlowDoc:
    """Restore a flow's graph from a history snapshot. Re-saves the snapshotted
    graph as a new rev (which itself snapshots the now-current graph first, so a
    restore is also reversible). Raises ``ValueError`` if the snapshot is gone."""
    return _store.restore_history(flow_id, seq, origin=origin)


def events_since(seq: int) -> list[dict]:
    """Change-feed rows with ``seq`` greater than the cursor (oldest first)."""
    return _store.events_since(seq)


def latest_seq() -> int:
    return _store.latest_seq()


# ---------------------------------------------------------------------------
# Folders — nestable adjacency-list tree for organising flows in the gallery.
# A flow's folder_id is a soft reference; deletes reconcile here (children
# reparent to the removed folder's parent) since SQLite has no late FKs.
# ---------------------------------------------------------------------------


def list_folders() -> list[Folder]:
    return _store.list_folders()


def create_folder(name: str, parent_id: str | None = None) -> Folder:
    return _store.create_folder(name, parent_id)


def update_folder(folder_id: str, *, name: str | None = None,
                  parent_id: str | None = ...) -> Folder:
    """Rename and/or reparent a folder. ``parent_id`` sentinel ``...`` = leave as
    is; ``None`` = move to top level. Rejects cycles (can't nest under itself or
    a descendant)."""
    return _store.update_folder(folder_id, name=name, parent_id=parent_id)


def delete_folder(folder_id: str) -> bool:
    """Delete a folder. Its child folders and its flows reparent to the deleted
    folder's parent (so nothing is orphaned or hidden)."""
    return _store.delete_folder(folder_id)


def set_flow_folder(flow_id: str, folder_id: str | None, *, origin: str = "") -> FlowDoc | None:
    """Move a flow into a folder (or to ungrouped when ``folder_id`` is None).
    Does not bump ``rev`` — organising isn't a graph edit — but emits a ``saved``
    event so open galleries refresh."""
    return _store.set_doc_folder(flow_id, folder_id, origin=origin)


__all__ = [
    "FlowConflict", "FlowDoc", "Folder", "slugify",
    "list_flows", "get_flow", "save_flow", "delete_flow",
    "list_history", "restore_history", "events_since", "latest_seq",
    "list_folders", "create_folder", "update_folder", "delete_folder",
    "set_flow_folder",
]
