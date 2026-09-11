# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-roles MCP server — role framework.
# index: imports | def _text
# AGENT_HEADER_END -->
"""okuro-roles MCP server — role framework."""

import json
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)
server = Server("okuro-roles")


def _text(data) -> list[TextContent]:
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="roles_list",
            description="List available expert roles.",
            inputSchema={
                "type": "object",
                "properties": {"domain": {"type": "string", "description": "Filter by domain"}},
            },
        ),
        Tool(
            name="roles_get",
            description="Get full role definition.",
            inputSchema={
                "type": "object",
                "properties": {"role_id": {"type": "string"}},
                "required": ["role_id"],
            },
        ),
        Tool(
            name="roles_match",
            description="Find best expert role for a task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task description"},
                    "top_k": {"type": "integer", "default": 3},
                },
                "required": ["task"],
            },
        ),
        Tool(
            name="roles_domains",
            description="List available role domains.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "roles_list":
            from okuro.roles import list_roles
            return _text(list_roles(domain=arguments.get("domain")))

        if name == "roles_get":
            from okuro.roles import get_role
            role = get_role(arguments["role_id"])
            if not role:
                return _text(f"Role not found: {arguments['role_id']}")
            return _text(role)

        if name == "roles_match":
            from okuro.roles.resolver import match_roles
            return _text(match_roles(
                arguments["task"], top_k=arguments.get("top_k", 3)
            ))

        if name == "roles_domains":
            from okuro.roles import get_domains
            return _text(get_domains())

        return _text(f"Unknown tool: {name}")
    except Exception as e:
        logger.exception(f"Tool {name} failed")
        return [TextContent(type="text", text=f"Error: {e}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
