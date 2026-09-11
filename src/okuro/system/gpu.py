# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: GPU metrics collector using nvidia-smi.
# index: imports | def get_gpu_status | def _parse_gpu_element
# AGENT_HEADER_END -->
"""GPU metrics collector using nvidia-smi."""

import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

# GPU naming — configurable via ~/.okuro/config.yaml in future
GPU_NAMES: dict[int, str] = {}


def get_gpu_status(
    gpu_id: Optional[int] = None,
    include_processes: bool = False,
    gpu_names: dict[int, str] | None = None,
) -> dict:
    """Get GPU status via nvidia-smi XML output.

    Args:
        gpu_id: Specific GPU index. None for all GPUs.
        include_processes: Include running processes on GPU.
        gpu_names: Optional name mapping {0: "GPU0", 1: "GPU1"}.
    """
    names = gpu_names or GPU_NAMES
    cmd = ["nvidia-smi", "-q", "-x"]
    if gpu_id is not None:
        cmd.extend(["-i", str(gpu_id)])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return {
                "error": f"nvidia-smi failed: {proc.stderr.strip()}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        root = ET.fromstring(proc.stdout)
        gpus = [
            _parse_gpu_element(gpu_elem, i, include_processes, names)
            for i, gpu_elem in enumerate(root.findall("gpu"))
        ]
        return {"gpus": gpus, "timestamp": datetime.now(timezone.utc).isoformat()}

    except FileNotFoundError:
        return {
            "error": "nvidia-smi not found — NVIDIA drivers may not be installed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except subprocess.TimeoutExpired:
        return {
            "error": "nvidia-smi timed out after 30s",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {
            "error": f"Failed to get GPU status: {e}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def _parse_gpu_element(
    gpu_elem: ET.Element,
    index: int,
    include_processes: bool,
    names: dict[int, str],
) -> dict:
    def get_text(path: str, default: str = "N/A") -> str:
        elem = gpu_elem.find(path)
        return elem.text.strip() if elem is not None and elem.text else default

    def parse_val(text: str) -> float:
        try:
            return float(text.split()[0])
        except (ValueError, IndexError):
            return 0.0

    model = get_text("product_name")
    mem_total = parse_val(get_text("fb_memory_usage/total", "0 MiB"))
    mem_used = parse_val(get_text("fb_memory_usage/used", "0 MiB"))
    mem_free = parse_val(get_text("fb_memory_usage/free", "0 MiB"))
    temp = parse_val(get_text("temperature/gpu_temp", "0 C"))
    power_draw = parse_val(get_text("gpu_power_readings/power_draw", "0 W"))
    power_limit = parse_val(get_text("gpu_power_readings/current_power_limit", "0 W"))
    gpu_util = parse_val(get_text("utilization/gpu_util", "0 %"))

    info = {
        "id": index,
        "name": names.get(index, f"GPU{index}"),
        "model": model,
        "vram": {
            "total_mb": int(mem_total),
            "used_mb": int(mem_used),
            "free_mb": int(mem_free),
            "utilization_percent": round(mem_used / mem_total * 100, 1) if mem_total > 0 else 0,
        },
        "temperature_c": int(temp),
        "power": {"draw_w": round(power_draw, 1), "limit_w": round(power_limit, 1)},
        "utilization_percent": int(gpu_util),
    }

    if include_processes:
        info["processes"] = [
            {
                "pid": p.findtext("pid", "N/A").strip(),
                "name": p.findtext("process_name", "N/A").strip(),
                "memory_mb": int(parse_val(p.findtext("used_memory", "0 MiB").strip())),
            }
            for p in gpu_elem.findall(".//process_info")
        ]

    return info
