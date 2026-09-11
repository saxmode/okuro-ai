# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Canon module tools — tool registry and discovery.
# index: imports | def _text | def get_tools
# AGENT_HEADER_END -->
"""Canon module tools — tool registry and discovery.

Extracted from canon/mcp_server.py for use by the unified okuro.mcp.server.
"""

import json
import logging

from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)


def _text(data) -> list[TextContent]:
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


def get_tools() -> list[Tool]:
    return [
        Tool(
            name="canon_list_tools",
            description="List all registered tools.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="canon_get_tool",
            description="Get tool details by name.",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        Tool(
            name="canon_list_skills",
            description="List all registered skills.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="canon_get_skill",
            description="Get skill details by name.",
            inputSchema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        Tool(
            name="canon_validate",
            description="Validate the tool registry for consistency.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "canon_list_tools":
        from okuro.canon import list_tools
        return _text(list_tools())

    if name == "canon_get_tool":
        from okuro.canon import get_tool
        tool = get_tool(arguments["name"])
        if not tool:
            return _text(f"Tool not found: {arguments['name']}")
        return _text(tool)

    if name == "canon_list_skills":
        from okuro.canon import list_skills
        return _text(list_skills())

    if name == "canon_get_skill":
        from okuro.canon import get_skill
        skill = get_skill(arguments["name"])
        if not skill:
            return _text(f"Skill not found: {arguments['name']}")
        return _text(skill)

    if name == "canon_validate":
        from okuro.canon import validate_registry
        return _text(validate_registry())

    return _text(f"Unknown tool: {name}")
