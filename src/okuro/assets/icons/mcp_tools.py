# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: MCP surface for okuro.assets.icons — search/get/list/stats over the icon library.
# index: imports | _text | get_tools | handle_tool
# AGENT_HEADER_END -->
"""MCP tools for the icon asset provider.

Exposes the ported engine to agents:
    assets_icon_search   hybrid (BM25 + vector) search, optional set/tag filter
    assets_icon_get      fetch one icon's SVG/metadata by id
    assets_icon_sets     list sets + counts
    assets_icon_tags     list tags + counts
    assets_icon_stats    library summary

Registered in okuro.mcp._registry._MODULES under key "assets". Library-missing
errors are returned as readable text (not raised) so a fresh install degrades
gracefully instead of failing tool discovery.
"""

from __future__ import annotations

import json

from mcp.types import TextContent, Tool

from . import service
from .db import LibraryError


def _text(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="assets_icon_search",
            description=(
                "Search the icon library by meaning or keyword. Hybrid search fuses "
                "semantic vector similarity with BM25 over names + tags + set names. "
                "Returns ranked icons (id, name, kebab_name, tags, sets, score). Filter "
                "by set (e.g. 'lucide' — parent collections walk down to children), tag, "
                "or favorite. Use this to discover the right icon before get_icon."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language description, keyword, or concept"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Max results (default 20)"},
                    "tag": {"type": "string", "description": "Require this tag name (case-insensitive)"},
                    "set": {
                        "description": "Restrict to one set name or an array of set names (case-insensitive; parent collections include children).",
                        "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                    },
                    "favorite": {"type": "boolean", "description": "Only favorited icons"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="assets_icon_get",
            description="Fetch a single icon by id (from assets_icon_search). Returns SVG + metadata. format: svg (default) | base64 | path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Icon id (uuid)"},
                    "format": {"type": "string", "enum": ["svg", "base64", "path"], "description": "Return format, default svg"},
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="assets_icon_sets",
            description="List icon sets in the library with icon counts. Browse what's available before searching.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="assets_icon_tags",
            description="List tags in the library (optionally by prefix), ordered by frequency. Discover the tag vocabulary before constraining a search.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "Case-insensitive prefix filter"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Max tags (default 100)"},
                },
            },
        ),
        Tool(
            name="assets_icon_stats",
            description="Summarize the icon library: total icons, embedded count, tags, sets, favorites.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "assets_icon_search":
            return _text(
                service.search_icons(
                    arguments["query"],
                    limit=arguments.get("limit", service.DEFAULT_LIMIT),
                    tag=arguments.get("tag"),
                    set=arguments.get("set"),
                    favorite=arguments.get("favorite"),
                )
            )
        if name == "assets_icon_get":
            return _text(service.get_icon(arguments["id"], format=arguments.get("format", "svg")))
        if name == "assets_icon_sets":
            return _text(service.list_sets())
        if name == "assets_icon_tags":
            return _text(service.list_tags(arguments.get("prefix"), limit=arguments.get("limit", 100)))
        if name == "assets_icon_stats":
            return _text(service.library_stats())
        return _text({"error": f"unknown tool: {name}"})
    except LibraryError as e:
        return _text({"error": str(e), "hint": "Install an icon library: `okuro assets icons import <pack.zip>`"})
