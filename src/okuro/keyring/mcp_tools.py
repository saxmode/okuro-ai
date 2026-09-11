# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Keyring module tools — encrypted secrets vault.
# index: imports | def _get_storage | def get_tools
# AGENT_HEADER_END -->
"""Keyring module tools — encrypted secrets vault.

Extracted from keyring/mcp_server.py for use by the unified okuro.mcp.server.
"""

import json

from mcp.types import Tool, TextContent


_storage = None


def _get_storage():
    global _storage
    if _storage is None:
        from okuro.keyring.storage import KeyringStorage
        _storage = KeyringStorage()
    return _storage


def get_tools() -> list[Tool]:
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


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
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
