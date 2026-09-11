# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro gpu — GPU allocation status.
# index: imports | def gpu
# AGENT_HEADER_END -->
"""okuro gpu — GPU allocation status."""

import click

from .output import console, data_table, heading


@click.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def gpu(as_json):
    """GPU allocation and VRAM status."""
    from okuro.system.gpu import get_gpu_status

    status = get_gpu_status()

    if as_json:
        import json
        console.print(json.dumps(status, indent=2, default=str))
        return

    gpus = status.get("gpus", [])
    if not gpus:
        console.print("[dim]No GPUs detected[/dim]")
        return

    rows = []
    for g in gpus:
        idx = str(g.get("id", "?"))
        name = g.get("name", "?")
        model = g.get("model", "")
        vram = g.get("vram", {})
        used = vram.get("used_mb", 0)
        total = vram.get("total_mb", 0)
        temp = f"{g.get('temperature_c', '?')}\u00b0C"
        pct = f"{vram.get('utilization_percent', 0)}%"
        util = f"{g.get('utilization_percent', 0)}%"
        rows.append([idx, name, model, f"{used}/{total} MB", pct, temp, util])

    table = data_table(
        ["ID", "NAME", "MODEL", "VRAM", "USED", "TEMP", "UTIL"],
        rows,
    )
    console.print(table)
