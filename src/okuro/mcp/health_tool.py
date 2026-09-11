# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Synthetic mcp_health + mcp_catalog tools — registry diagnostics, always loadable.
# index: imports | def get_tools | async def handle_tool
# AGENT_HEADER_END -->
"""Synthetic ``mcp_health`` + ``mcp_catalog`` tools.

``mcp_health`` reports the registry's load status: which modules loaded,
which failed (with the exception message), how many tools are exposed,
and which duplicate tool names were resolved by the registry.

``mcp_catalog`` is the live catalogue of the server's own tool surface —
module taxonomy and, on demand, per-tool schemas — derived from the
running registry so it cannot drift. It replaces three hand-maintained
copies of the same taxonomy that each went stale independently.

Design contract:
  * This module MUST stay free of okuro-internal imports at module-import
    time so the registry can always load it — even when every other tool
    module has failed. Registry state is read lazily inside ``handle_tool``.
  * It is the FIRST entry in ``okuro.mcp._registry._MODULES`` so it is
    registered before anything risky.
"""

import json

from mcp.types import Tool, TextContent


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="mcp_health",
            description=(
                "Report MCP registry load health: loaded modules, failed "
                "modules (with errors), total tool count, and duplicate "
                "tool names. Duplicates are resolved first-wins by load "
                "order in registry._MODULES."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="mcp_catalog",
            description=(
                "The LIVE catalogue of this server's own tools, derived "
                "from the running registry — it cannot go stale. Use this "
                "to discover what okuro tools exist, or to read a tool's "
                "JSONSchema. NOT canon_list_tools / canon_get_tool: those "
                "are a registry of installable CLIs and MCP servers and "
                "have no entry for an individual okuro tool. "
                "detail='names' returns module -> tool names; "
                "detail='full' adds description + inputSchema and requires "
                "a module or search filter."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "module": {
                        "type": "string",
                        "description": "Restrict to one registry module (e.g. 'sense', 'cortex', 'trace').",
                    },
                    "search": {
                        "type": "string",
                        "description": "Case-insensitive substring match on tool name + description.",
                    },
                    "detail": {
                        "type": "string",
                        "enum": ["names", "full"],
                        "default": "names",
                        "description": "'full' includes inputSchema; requires module or search.",
                    },
                },
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "mcp_catalog":
        from okuro.mcp import _registry as reg

        return [TextContent(type="text", text=json.dumps(
            reg.get_catalog(
                module=arguments.get("module"),
                search=arguments.get("search"),
                detail=arguments.get("detail", "names"),
            ),
            indent=2,
            default=str,
        ))]

    if name != "mcp_health":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    # Lazy import: registry imports this module, so importing registry at
    # module-load time would create a cycle. Reading state here is fine —
    # by the time mcp_health() is invoked, _load_all() has already run.
    from okuro.mcp import _registry as reg

    payload = {
        "loaded_modules": reg.get_loaded_modules(),
        "failed_modules": reg.get_failed_modules(),
        "tool_count": reg.get_tool_count(),
        "duplicate_tool_names": reg.get_duplicate_tool_names(),
        "strict_mode": reg.is_strict_mode(),
    }
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]
