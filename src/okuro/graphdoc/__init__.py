# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.graphdoc — ONE node-graph document store, instantiated per
#   feature over its own tables. Shared by okuro-flow (a standalone visualizer)
#   and the orchestrator workflow designer (an LLM-orchestration tool).
# index: re-exports from store
# AGENT_HEADER_END -->
"""Shared node-graph document storage: CRUD, clobber guard, history, change feed.

Shared MECHANISM, separate DATA — each feature gets its own tables, so one
product's documents can never surface in another's gallery.
"""

from okuro.graphdoc.store import (
    EVENTS_KEEP,
    HISTORY_KEEP,
    Folder,
    GraphConflict,
    GraphDoc,
    GraphDocStore,
    slugify,
)

__all__ = [
    "EVENTS_KEEP",
    "HISTORY_KEEP",
    "Folder",
    "GraphConflict",
    "GraphDoc",
    "GraphDocStore",
    "slugify",
]
