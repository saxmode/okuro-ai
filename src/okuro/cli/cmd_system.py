# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro system — system health overview.
# index: imports | def system
# AGENT_HEADER_END -->
"""okuro system — system health overview."""

import click

from .output import console, ok, warn, fail, heading, kv_table


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def system(as_json):
    """System health overview (GPU, storage, services)."""
    from okuro.system.gpu import get_gpu_status
    from okuro.system.storage import get_storage_status

    gpu = get_gpu_status()
    storage = get_storage_status()

    if as_json:
        import json
        console.print(json.dumps({"gpu": gpu, "storage": storage}, indent=2, default=str))
        return

    heading("GPU")
    if gpu.get("gpus"):
        for g in gpu["gpus"]:
            name = g.get("name", "?")
            model = g.get("model", "")
            vram = g.get("vram", {})
            used = vram.get("used_mb", 0)
            total = vram.get("total_mb", 0)
            temp = g.get("temperature_c", "?")
            pct = vram.get("utilization_percent", 0)
            status_fn = ok if pct < 80 else warn if pct < 95 else fail
            status_fn(f"{name} ({model}): {used}/{total} MB ({pct}%), {temp}\u00b0C")
    else:
        warn("No GPUs detected")

    heading("Storage")
    if storage.get("mounts"):
        for fs in storage["mounts"]:
            path = fs.get("path", "?")
            pct = fs.get("usage_percent", 0)
            free = fs.get("free_gb", 0)
            status_fn = ok if pct < 80 else warn if pct < 90 else fail
            status_fn(f"{path}: {pct}% used, {free:.1f} GB free")
