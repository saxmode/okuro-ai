# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-system MCP server — system health, GPU, storage, Docker, ports, services.
# index: imports | def _text
# AGENT_HEADER_END -->
"""okuro-system MCP server — system health, GPU, storage, Docker, ports, services."""

import json
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)
server = Server("okuro-system")


def _text(data) -> list[TextContent]:
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="sysinfo_gpu_status",
            description="GPU VRAM, temperature, power, processes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "gpu_id": {"type": "integer", "description": "Specific GPU index. Omit for all."},
                },
            },
        ),
        Tool(
            name="sysinfo_storage_status",
            description="Disk usage, RAID health, free space.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="sysinfo_docker_status",
            description="Docker container states.",
            inputSchema={
                "type": "object",
                "properties": {"category": {"type": "string", "description": "Filter: ai, apps, infra"}},
            },
        ),
        Tool(
            name="sysinfo_port_status",
            description="Find available ports in a range.",
            inputSchema={
                "type": "object",
                "properties": {
                    "range_start": {"type": "integer", "default": 8000},
                    "range_end": {"type": "integer", "default": 8100},
                },
            },
        ),
        Tool(
            name="sysinfo_service_health",
            description="HTTP health checks on services.",
            inputSchema={
                "type": "object",
                "properties": {
                    "services": {"type": "array", "items": {"type": "string"}, "description": "Service names to check"},
                },
            },
        ),
        Tool(
            name="sysinfo_system_overview",
            description="Combined quick summary of GPU, storage, Docker.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "sysinfo_gpu_status":
            from okuro.system.gpu import get_gpu_status
            return _text(get_gpu_status(gpu_id=arguments.get("gpu_id")))

        if name == "sysinfo_storage_status":
            from okuro.system.storage import get_storage_status
            return _text(get_storage_status())

        if name == "sysinfo_docker_status":
            from okuro.system.docker import get_docker_status
            return _text(get_docker_status(category=arguments.get("category")))

        if name == "sysinfo_port_status":
            from okuro.system.ports import get_port_status
            return _text(get_port_status(
                range_start=arguments.get("range_start", 8000),
                range_end=arguments.get("range_end", 8100),
            ))

        if name == "sysinfo_service_health":
            from okuro.system.services import get_service_health
            return _text(get_service_health(services=arguments.get("services")))

        if name == "sysinfo_system_overview":
            from okuro.system.gpu import get_gpu_status
            from okuro.system.storage import get_storage_status
            return _text({"gpu": get_gpu_status(), "storage": get_storage_status()})

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
