# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: MCP surface for the unified media bucket (okuro.assets.store) — list +
#   tag media of ANY kind (image|video|audio|icon|illustration) from one
#   filestore. Complements the icon-only tools in okuro.assets.icons.mcp_tools
#   (which stay pointed at the licensed read-only library). Registered under the
#   "assets_media" key in okuro.mcp._registry._MODULES.
# index: imports | _text | get_tools | handle_tool
# AGENT_HEADER_END -->
"""MCP tools for the unified media bucket.

    assets_list   list media assets across kinds, filter by kind/source/folder/tag
    assets_tag    add/remove tags on a media asset (tagging across all kinds)
"""

from __future__ import annotations

import json

from mcp.types import TextContent, Tool


def _text(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="assets_list",
            description=(
                "List media assets from okuro's single media bucket, across all "
                "kinds (image, video, audio, icon, illustration). Everything the "
                "Studio generates lands here (source='studio', folder="
                "'studio-generations'). Filter by kind, source (studio|upload|"
                "import), folder, tag, or a title substring. Returns rows with "
                "id, kind, source, folder, mime, tags, meta (prompt/model/preset "
                "for studio images), and created_at. Newest first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(_kinds())},
                    "source": {"type": "string", "enum": ["studio", "upload", "import"]},
                    "folder": {"type": "string", "description": "e.g. 'studio-generations'"},
                    "tag": {"type": "string", "description": "Require this tag (case-insensitive)"},
                    "q": {"type": "string", "description": "Title substring match"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "default 60"},
                    "offset": {"type": "integer", "minimum": 0},
                },
            },
        ),
        Tool(
            name="assets_tag",
            description=(
                "Add and/or remove tags on a media asset in the bucket (works for "
                "every kind, not just icons). Returns the asset's resulting tag list."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Asset id (from assets_list)"},
                    "add": {"type": "array", "items": {"type": "string"}},
                    "remove": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id"],
            },
        ),
    ]


def _kinds():
    from okuro.assets.store import KINDS
    return KINDS


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    from okuro.assets import store

    if name == "assets_list":
        try:
            store.sync_studio_dir()
        except Exception:
            pass
        items = store.list_assets(
            kind=arguments.get("kind"), source=arguments.get("source"),
            folder=arguments.get("folder"), tag=arguments.get("tag"),
            q=arguments.get("q"), limit=arguments.get("limit", 60),
            offset=arguments.get("offset", 0))
        return _text({"count": len(items), "items": items,
                      "kinds": store.kinds_summary()})
    if name == "assets_tag":
        aid = arguments["id"]
        for t in arguments.get("add") or []:
            store.add_tags(aid, [t])
        for t in arguments.get("remove") or []:
            store.remove_tag(aid, t)
        a = store.get_asset(aid)
        if not a:
            return _text({"error": "not found", "id": aid})
        return _text({"id": aid, "tags": a["tags"]})
    return _text({"error": f"unknown tool: {name}"})
