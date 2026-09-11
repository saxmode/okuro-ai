# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: MCP surface for okuro-notes — note_create / get / list / update /
#   delete / search / backlinks. Lets the agent read AND write the user's notes,
#   so the note vault is a first-class, two-way RAG citizen. Renders live at
#   /notes on save (notes_events change-feed).
# index: imports | def get_tools | async def handle_tool
# AGENT_HEADER_END -->
import json

from mcp.types import TextContent, Tool

_ORIGIN = "mcp"


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="note_create",
            description=(
                "Create a markdown note in okuro·notes. Auto-chunked and embedded "
                "into the RAG on save (searchable via note_search + cortex). "
                "Wikilinks [[Title]] in the body resolve to other notes. "
                "Renders live at /notes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Optional — first body line is used if omitted."},
                    "body": {"type": "string", "description": "Markdown source."},
                    "frontmatter": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": "Typed properties (tags, status, etc.).",
                    },
                    "project": {"type": "string", "description": "Project slug."},
                    "folder_id": {"type": "string", "description": "Containing folder id (omit for root)."},
                    "authored_at": {
                        "type": "string",
                        "description": (
                            "When the HUMAN wrote this, 'YYYY-MM-DD' — NOT when you are "
                            "saving it. Pass it ONLY when the content predates now "
                            "(migrating a note, transcribing something older). Omit it "
                            "for anything written today: created_at already means that. "
                            "Ranking anchors on this, so a guess here mis-sorts the "
                            "user's inbox — omit rather than guess."
                        ),
                    },
                },
                "required": ["body"],
            },
        ),
        Tool(
            name="note_get",
            description="Get one note by id (full markdown body + frontmatter).",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="note_list",
            description="List notes (id, title, project, timestamps). Newest first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "archived": {"type": "boolean", "description": "List archived instead of active."},
                    "limit": {"type": "integer", "default": 100},
                },
            },
        ),
        Tool(
            name="note_update",
            description=(
                "Update an existing note by id. Re-chunks, re-embeds, and rewrites "
                "wikilinks. Pass only the fields you want to change."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {
                        "type": "string",
                        "description": (
                            "Rename the note. Omit it and the title is left "
                            "alone; pass an empty string to hand it back to the "
                            "first-body-line rule."
                        ),
                    },
                    "body": {"type": "string"},
                    "frontmatter": {"type": "object", "additionalProperties": True},
                    "project": {"type": "string"},
                    "folder_id": {"type": ["string", "null"], "description": "Move to folder; null = root. Omit to leave unchanged."},
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="note_delete",
            description="Delete a note by id. Inbound links are demoted to ghost links.",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="note_search",
            description=(
                "Semantic search over note content (chunk-level embeddings, "
                "collapsed to best-match-per-note). Returns notes ranked by "
                "similarity. Falls back to substring match if embedding is down."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                    "project": {"type": "string"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="note_backlinks",
            description="Notes that link TO the given note id (incoming wikilinks).",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        Tool(
            name="note_folders",
            description="List all note folders (id, name, parent_id) for the tree. Root folders have parent_id null.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="note_folder_create",
            description="Create a folder in the note tree. Omit parent_id for a root folder.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "parent_id": {"type": "string", "description": "Parent folder id (omit for root)."},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="note_folder_rename",
            description="Rename a folder by id.",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
                "required": ["id", "name"],
            },
        ),
        Tool(
            name="note_folder_move",
            description="Re-parent a folder. parent_id null = move to root. Rejects cycles.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "parent_id": {"type": ["string", "null"]},
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="note_folder_delete",
            description="Delete a folder. Child folders reparent up; notes fall back to root. Nothing is lost.",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    from okuro import notes

    try:
        if name == "note_create":
            note = notes.upsert_note(
                title=arguments.get("title", ""),
                body=arguments["body"],
                frontmatter=arguments.get("frontmatter"),
                project=(arguments.get("project") or None),
                folder_id=(arguments.get("folder_id") or None),
                origin=_ORIGIN,
                authored_at=(arguments.get("authored_at") or None),
            )
            result = {"success": True, "note": note, "url": "/notes"}

        elif name == "note_get":
            note = notes.get_note(arguments["id"])
            result = note or {"error": f"note '{arguments['id']}' not found"}

        elif name == "note_list":
            rows = notes.list_notes(
                project=(arguments.get("project") or None),
                archived=bool(arguments.get("archived")),
                limit=int(arguments.get("limit", 100)),
            )
            result = {"notes": rows, "count": len(rows)}

        elif name == "note_update":
            existing = notes.get_note(arguments["id"])
            if existing is None:
                result = {"error": f"note '{arguments['id']}' not found"}
            else:
                # folder_id only forwarded when the caller sent the key, so a
                # metadata-only update never re-parents the note (storage sentinel).
                kwargs = {}
                if "folder_id" in arguments:
                    kwargs["folder_id"] = arguments["folder_id"] or None
                # Same title contract as the web PUT: a `title` argument is a
                # rename and pins it, its absence leaves the name alone. Passing
                # the existing title back would mark every body-only update as a
                # deliberate naming and freeze titles nobody chose.
                if "title" in arguments:
                    title = arguments["title"] or ""
                    kwargs["title_explicit"] = (
                        bool(title.strip()) and title.strip() != "Untitled"
                    )
                else:
                    title = ""
                note = notes.upsert_note(
                    note_id=arguments["id"],
                    title=title,
                    body=arguments.get("body", existing["body"]),
                    frontmatter=arguments.get("frontmatter", existing["frontmatter"]),
                    project=arguments.get("project", existing.get("project")),
                    origin=_ORIGIN,
                    **kwargs,
                )
                result = {"success": True, "note": note, "url": "/notes"}

        elif name == "note_delete":
            ok = notes.delete_note(arguments["id"])
            result = {"success": ok}

        elif name == "note_search":
            rows = notes.search_notes(
                arguments["query"],
                limit=int(arguments.get("limit", 10)),
                project=(arguments.get("project") or None),
            )
            result = {"results": rows, "count": len(rows)}

        elif name == "note_backlinks":
            result = {"backlinks": notes.backlinks(arguments["id"])}

        elif name == "note_folders":
            rows = notes.list_folders()
            result = {"folders": rows, "count": len(rows)}

        elif name == "note_folder_create":
            folder = notes.create_folder(
                arguments["name"], parent_id=(arguments.get("parent_id") or None)
            )
            result = {"success": True, "folder": folder}

        elif name == "note_folder_rename":
            ok = notes.rename_folder(arguments["id"], arguments["name"])
            result = {"success": ok}

        elif name == "note_folder_move":
            ok = notes.move_folder(arguments["id"], arguments.get("parent_id") or None)
            result = {"success": ok}

        elif name == "note_folder_delete":
            ok = notes.delete_folder(arguments["id"])
            result = {"success": ok}

        else:
            result = {"error": f"unknown tool '{name}'"}

    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)}

    return [TextContent(type="text", text=json.dumps(result, default=str))]
