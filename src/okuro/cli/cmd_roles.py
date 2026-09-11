# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro roles — role framework.
# index: imports | def roles | def roles_list | def roles_match | def roles_get
# AGENT_HEADER_END -->
"""okuro roles — role framework."""

import click

from .output import console, data_table, heading


@click.group()
def roles():
    """Browse and match expert roles."""


@roles.command("list")
@click.option("--domain", default=None, help="Filter by domain.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def roles_list(domain, as_json):
    """List available roles."""
    from okuro.roles import list_roles

    result = list_roles(domain=domain)

    if as_json:
        import json
        console.print(json.dumps(result, indent=2, default=str))
        return

    if not result:
        console.print("[dim]No roles found[/dim]")
        return

    rows = []
    for r in result:
        rows.append([
            r.get("id", "?"),
            r.get("name", "?"),
            r.get("domain", "?"),
        ])
    table = data_table(["ID", "NAME", "DOMAIN"], rows)
    console.print(table)


@roles.command("match")
@click.argument("task")
@click.option("--top", default=3, help="Number of matches to show.")
def roles_match(task, top):
    """Find best role for a task."""
    from okuro.roles.resolver import match_roles

    matches = match_roles(task, top_k=top)
    if not matches:
        console.print("[dim]No matching roles[/dim]")
        return

    rows = []
    for m in matches:
        rows.append([
            m.get("id", "?"),
            m.get("domain", "?"),
            f"{m.get('similarity', 0):.2f}",
            m.get("match_type", "?"),
        ])
    table = data_table(["ID", "DOMAIN", "SIMILARITY", "TYPE"], rows)
    console.print(table)


@roles.command("get")
@click.argument("role_id")
def roles_get(role_id):
    """Show full role definition."""
    from okuro.roles import get_role

    role = get_role(role_id)
    if not role:
        console.print(f"[red]Role not found: {role_id}[/red]")
        raise SystemExit(1)

    import json
    console.print(json.dumps(role, indent=2, default=str))
