# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Docker container metrics collector.
# index: imports | def get_docker_status | def _parse_container | def _categorize
# AGENT_HEADER_END -->
"""Docker container metrics collector."""

import json
import re
import subprocess
from datetime import datetime, timezone
from typing import Optional


def get_docker_status(
    filter_name: Optional[str] = None,
    include_stopped: bool = False,
    category: Optional[str] = None,
) -> dict:
    """Get Docker container status."""
    cmd = ["docker", "ps", "--format", "{{json .}}"]
    if include_stopped:
        cmd.insert(2, "-a")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return {
                "error": f"docker ps failed: {proc.stderr.strip()}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        containers = []
        for line in proc.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                c = _parse_container(json.loads(line))
                if filter_name and filter_name.lower() not in c["name"].lower():
                    continue
                if category and c["category"] != category:
                    continue
                containers.append(c)
            except json.JSONDecodeError:
                continue

        containers.sort(key=lambda c: (c["category"], c["name"]))

        running = sum(1 for c in containers if c["status"] == "running")
        unhealthy = sum(1 for c in containers if c["health"] == "unhealthy")

        return {
            "containers": containers,
            "summary": {
                "total": len(containers),
                "running": running,
                "stopped": len(containers) - running,
                "unhealthy": unhealthy,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except FileNotFoundError:
        return {"error": "docker not found", "timestamp": datetime.now(timezone.utc).isoformat()}
    except subprocess.TimeoutExpired:
        return {"error": "docker ps timed out", "timestamp": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        return {"error": str(e), "timestamp": datetime.now(timezone.utc).isoformat()}


def _parse_container(data: dict) -> dict:
    name = data.get("Names", "unknown")
    status_str = data.get("Status", "")
    is_running = "Up" in status_str
    is_healthy = "(healthy)" in status_str
    is_unhealthy = "(unhealthy)" in status_str

    port = None
    ports_str = data.get("Ports", "")
    if ports_str:
        m = re.search(r":(\d+)->", ports_str)
        if m:
            port = int(m.group(1))

    return {
        "name": name,
        "image": data.get("Image", "unknown"),
        "status": "running" if is_running else "stopped",
        "health": "healthy" if is_healthy else ("unhealthy" if is_unhealthy else "none"),
        "uptime": status_str.replace("Up ", "").split(" (")[0] if is_running else None,
        "port": port,
        "category": _categorize(name),
    }


def _categorize(name: str) -> str:
    name_lower = name.lower()
    categories = {
        "ai": ["comfyui", "automatic1111", "forge", "llama", "open-webui", "xtts", "hunyuan"],
        "apps": ["n8n", "directus", "paperless", "stirling", "radicale", "apprise"],
        "infra": ["prometheus", "grafana", "node_exporter", "cadvisor", "dnsmasq", "caddy"],
    }
    for cat, patterns in categories.items():
        if any(p in name_lower for p in patterns):
            return cat
    return "other"
