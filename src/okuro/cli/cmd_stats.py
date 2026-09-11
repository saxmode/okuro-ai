# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro stats — telemetry and usage stats.
# index: imports | def stats
# AGENT_HEADER_END -->
"""okuro stats — telemetry and usage stats."""

import click

from .output import console, heading, data_table


@click.command()
@click.option("--provider", default=None, help="Filter by provider.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def stats(provider, as_json):
    """Telemetry and usage statistics."""
    from okuro.telemetry.logger import get_session_info

    session = get_session_info()

    if as_json:
        import json
        console.print(json.dumps(session, indent=2, default=str))
        return

    if not session:
        console.print("[dim]No active session[/dim]")
        return

    heading("Current Session")
    rows = []
    for k, v in session.items():
        rows.append([str(k), str(v)])
    table = data_table(["KEY", "VALUE"], rows)
    console.print(table)
