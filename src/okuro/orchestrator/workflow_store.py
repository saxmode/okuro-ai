# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Storage for MANUALLY ARRANGED orchestrator workflows — the second
#   binding of the shared GraphDocStore, over its OWN tables. Same mechanism as
#   okuro-flow, no shared rows, so a workflow can never surface in /flow.
# index: WorkflowConflict | WorkflowDoc | list_workflows | save_workflow
#   | get_workflow | delete_workflow | history | events | folders
# AGENT_HEADER_END -->
"""Drawn-workflow storage.

okuro has two kinds of workflow: the LLM-decomposed one the orchestrator plans on
the fly, and the manually arranged one a human (or an agent) draws — a node graph
carrying a role, prompt and acceptance criteria per node, compiled to a plan by
``okuro.orchestrator.flow_compiler``.

This module stores the second kind. It is a thin binding of
``okuro.graphdoc.GraphDocStore``, exactly as ``okuro.flow_designer.storage`` is,
so both get history, restore, the clobber guard and the live change feed from ONE
implementation.

**Separate tables, on purpose.** okuro-flow (``/flow``) is a standalone
visualizer of complex information and must not be corrupted by orchestration
semantics. Because workflows live in their own tables rather than behind a
``kind`` filter, a workflow appearing in the ``/flow`` gallery is not a bug that
can happen — the query would have to name the wrong table.

Node semantics live in the graph JSON and are interpreted only by the compiler;
this layer stays as dumb about them as okuro-flow is about its diagrams.
"""

from __future__ import annotations

import logging
from typing import Any

from okuro.graphdoc.store import Folder, GraphConflict, GraphDoc, GraphDocStore, slugify

logger = logging.getLogger("okuro.orchestrator.workflow_store")


class WorkflowConflict(GraphConflict):
    """Raised when a save would empty a populated workflow without explicit
    intent. Carries the current doc as ``.current``."""


class WorkflowDoc(GraphDoc):
    """One drawn workflow. ``graph`` is the full ReactFlow payload; subtask
    semantics live in each node's ``data`` (see ``flow_compiler``)."""


_store = GraphDocStore(
    "orchestrator_workflows",
    folders_table="orchestrator_workflow_folders",
    doc_cls=WorkflowDoc,
    conflict_cls=WorkflowConflict,
)


def list_workflows() -> list[WorkflowDoc]:
    return _store.list_docs()


def get_workflow(workflow_id: str) -> WorkflowDoc | None:
    return _store.get_doc(workflow_id)


def save_workflow(
    *,
    id: str | None = None,
    name: str,
    description: str = "",
    graph: dict[str, Any] | None = None,
    origin: str = "",
    base_rev: int | None = None,
    allow_empty: bool = False,
) -> WorkflowDoc:
    """Upsert a drawn workflow. Same guarantees as okuro-flow: last-write-wins,
    every overwrite snapshots the prior graph, and emptying a populated workflow
    requires ``allow_empty`` (raising ``WorkflowConflict`` otherwise)."""
    return _store.save_doc(
        id=id, name=name, description=description, graph=graph,
        origin=origin, base_rev=base_rev, allow_empty=allow_empty,
    )


def delete_workflow(workflow_id: str, *, origin: str = "") -> bool:
    return _store.delete_doc(workflow_id, origin=origin)


def list_history(workflow_id: str, limit: int = 50) -> list[dict]:
    return _store.list_history(workflow_id, limit)


def restore_history(workflow_id: str, seq: int, *, origin: str = "") -> WorkflowDoc:
    return _store.restore_history(workflow_id, seq, origin=origin)


def events_since(seq: int) -> list[dict]:
    return _store.events_since(seq)


def latest_seq() -> int:
    return _store.latest_seq()


def list_folders() -> list[Folder]:
    return _store.list_folders()


def create_folder(name: str, parent_id: str | None = None) -> Folder:
    return _store.create_folder(name, parent_id)


def update_folder(folder_id: str, *, name: str | None = None,
                  parent_id: str | None = ...) -> Folder:
    return _store.update_folder(folder_id, name=name, parent_id=parent_id)


def delete_folder(folder_id: str) -> bool:
    return _store.delete_folder(folder_id)


def set_workflow_folder(workflow_id: str, folder_id: str | None, *,
                        origin: str = "") -> WorkflowDoc | None:
    return _store.set_doc_folder(workflow_id, folder_id, origin=origin)


def compile_workflow(workflow_id: str, **kwargs) -> dict:
    """Load a drawn workflow and compile it to an orchestrator plan dict.

    Convenience seam so callers do not have to know that storage and compilation
    are separate concerns. Raises ``ValueError`` if the workflow is missing, and
    ``FlowCompileError`` if the graph is not a valid workflow.
    """
    from okuro.orchestrator.flow_compiler import compile_flow

    doc = get_workflow(workflow_id)
    if doc is None:
        raise ValueError(f"workflow '{workflow_id}' not found")
    return compile_flow(doc, **kwargs)


__all__ = [
    "WorkflowConflict", "WorkflowDoc", "Folder", "slugify",
    "list_workflows", "get_workflow", "save_workflow", "delete_workflow",
    "list_history", "restore_history", "events_since", "latest_seq",
    "list_folders", "create_folder", "update_folder", "delete_folder",
    "set_workflow_folder", "compile_workflow",
]
