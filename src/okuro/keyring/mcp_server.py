# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Okuro Keyring MCP Server — encrypted secrets vault for AI agents.
# index: imports | def _get_storage | def run
# AGENT_HEADER_END -->
"""Okuro Keyring MCP Server — encrypted secrets vault for AI agents."""

import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .storage import KeyringStorage

server = Server("okuro-keyring")

_storage = None


def _get_storage() -> KeyringStorage:
    global _storage
    if _storage is None:
        _storage = KeyringStorage()  # resolves password via OS keyring / env var
    return _storage


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="keyring_get",
            description="Retrieve a secret value by name from the encrypted keyring.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The secret name"}
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="keyring_set",
            description="Store a secret in the encrypted keyring.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The secret name"},
                    "value": {"type": "string", "description": "The secret value"},
                },
                "required": ["name", "value"],
            },
        ),
        Tool(
            name="keyring_list",
            description="List all secret names in the keyring (no values returned).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="keyring_delete",
            description="Delete a secret from the keyring.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The secret name"}
                },
                "required": ["name"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        storage = _get_storage()
    except RuntimeError as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    try:
        if name == "keyring_get":
            value = storage.get_key(arguments["name"])
            if value is None:
                result = {"error": f"Key '{arguments['name']}' not found"}
            else:
                result = {"name": arguments["name"], "value": value}
        elif name == "keyring_set":
            storage.add_key(arguments["name"], arguments["value"])
            result = {"success": True, "name": arguments["name"]}
        elif name == "keyring_list":
            keys = storage.list_keys()
            result = {"keys": keys, "count": len(keys)}
        elif name == "keyring_delete":
            deleted = storage.delete_key(arguments["name"])
            result = {"success": deleted, "name": arguments["name"]}
        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as e:
        result = {"error": str(e)}

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
