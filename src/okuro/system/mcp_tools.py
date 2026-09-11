# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: System module tools — GPU, storage, Docker, ports, services.
# index: imports | def _text | def get_tools
# AGENT_HEADER_END -->
"""System module tools — GPU, storage, Docker, ports, services.

Extracted from system/mcp_server.py for use by the unified okuro.mcp.server.
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
        Tool(
            name="sysinfo_disk_health",
            description=(
                "SMART health for every physical disk (SATA + NVMe): per-drive "
                "ok/warn/fail + plain-language findings + raw attributes (the "
                "why-drilldown)."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="recheck_disk_health",
            description=(
                "Verify-on-fix: after the user says 'I replaced the drive', "
                "re-read SMART for that device and resolve or keep-open its "
                "health signal. Pass the drive serial or /dev path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "device_ref": {
                        "type": "string",
                        "description": "Drive serial number or /dev path (e.g. /dev/sda).",
                    },
                },
                "required": ["device_ref"],
            },
        ),
    ]


async def handle_tool(name: str, arguments: dict) -> list[TextContent]:
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

    if name == "sysinfo_disk_health":
        from okuro.system.smart import get_disk_health
        return _text(get_disk_health())

    if name == "recheck_disk_health":
        from okuro.system.smart import recheck_disk_health
        device_ref = arguments.get("device_ref")
        if not device_ref:
            return _text("recheck_disk_health requires 'device_ref' (drive serial or /dev path).")
        return _text(recheck_disk_health(device_ref))

    return _text(f"Unknown tool: {name}")
