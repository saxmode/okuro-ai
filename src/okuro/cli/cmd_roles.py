# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro roles — role framework.
# index: imports | def roles | def roles_list | def roles_match | def roles_get
#   | def roles_restore
# AGENT_HEADER_END -->
"""okuro roles — role framework."""

from pathlib import Path

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


@roles.command("restore")
@click.option("--action", "action_id", required=True,
              help="The structure action whose write should be reversed.")
@click.option("--snapshot", default=None,
              help="A specific snapshot file. Defaults to the newest one "
                   "taken for this action.")
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
def roles_restore(action_id, snapshot, yes):
    """Put an action's affected roles back, from its own pre-write snapshot.

    A COMMAND AND NOT A TOOL, because this is the one step in the update
    pipeline that overwrites current role bodies with older ones. The MCP
    surface classifies it destructive and denies it by default; a person at a
    terminal is the right actor for a rollback.

    Only the roles the action named are touched. The snapshot is a whole-store
    copy because that is the only consistent thing to take, but restoring the
    whole file would undo every unrelated thing that happened since.
    """
    from okuro.db import get_db
    from okuro.roles.structural_update import (
        UpdateRefused,
        restore,
        snapshots_for,
    )

    db = get_db()
    found = snapshots_for(db, action_id)
    if not snapshot and not found:
        console.print(
            f"[red]No pre-write snapshot for action {action_id}.[/red] "
            "Nothing to restore from."
        )
        raise SystemExit(1)
    target = snapshot or str(found[-1])
    heading("Restore role bodies")
    console.print(f"action   : {action_id}")
    console.print(f"snapshot : {target}")
    if not yes:
        click.confirm("Overwrite the affected roles with the snapshot's "
                      "versions?", abort=True)

    try:
        result = restore(db, action_id, actor="cli", snapshot=snapshot)
    except UpdateRefused as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    console.print(
        data_table(["ROLE", "RESTORED FROM"],
                   [[r, Path(result["snapshot"]).name] for r in result["restored"]])
    )
    if result["not_in_snapshot"]:
        console.print(
            "[yellow]Not present in the snapshot (created after it was "
            f"taken): {', '.join(result['not_in_snapshot'])}[/yellow]"
        )
