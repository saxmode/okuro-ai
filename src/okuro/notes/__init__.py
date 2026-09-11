# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-notes — Obsidian-inspired note surface. Markdown format, DB
#   storage, every note auto-fed into the RAG. Public API re-exported from
#   storage so callers do `from okuro.notes import upsert_note, search_notes`.
# index: import
# AGENT_HEADER_END -->
from okuro.notes.storage import (
    upsert_note,
    get_note,
    list_notes,
    delete_note,
    set_archived,
    search_notes,
    backlinks,
    outgoing_links,
    latest_seq,
    events_since,
    note_graph,
    upsert_drawing,
    get_drawing,
    get_drawing_png,
    list_drawings,
    add_image,
    get_image,
    create_folder,
    list_folders,
    rename_folder,
    move_folder,
    delete_folder,
    set_note_folder,
)

__all__ = [
    "upsert_note",
    "get_note",
    "list_notes",
    "delete_note",
    "set_archived",
    "search_notes",
    "backlinks",
    "outgoing_links",
    "latest_seq",
    "events_since",
    "note_graph",
    "upsert_drawing",
    "get_drawing",
    "get_drawing_png",
    "list_drawings",
    "add_image",
    "get_image",
    "create_folder",
    "list_folders",
    "rename_folder",
    "move_folder",
    "delete_folder",
    "set_note_folder",
]
