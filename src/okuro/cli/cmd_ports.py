# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro ports — show who holds this install's ports; re-pick them.
# index:
#   imports
#   def ports
#   def show
#   def reassign
# AGENT_HEADER_END -->
"""``okuro ports`` — this install's service ports, and who actually holds them.

``show`` answers the question the 2026-09-16 incident could not: is the
thing answering on 13333 OUR orchestrator, or a second install's? It
renders the same one-line verdict every other surface renders, from
``okuro.system.install_identity``.

``reassign`` re-picks a free port set for THIS install and rewrites the
three places a port is recorded — config, unit files, MCP client configs
— then prints the restart order. It never restarts anything itself:
the install being re-ported is frequently the one the operator is
currently talking through, and a CLI that kills its own transport
mid-command is a worse outage than the collision.
"""

from __future__ import annotations

import click

from .output import console, ok, warn, fail, info


@click.group()
def ports():
    """Service ports for this okuro install."""


@ports.command("show")
def show():
    """Who holds this install's orchestrator / embed / daemon ports."""
    from okuro.system.install_identity import this_install, verify_all
    from okuro.system.port_registry import config_path, service_ports

    mine = this_install()
    console.print()
    console.print(f"  [bold]install[/bold]  {mine['install_root'] or '?'}")
    console.print(f"  [bold]home[/bold]     {mine['install_home']}")
    console.print(f"  [bold]user[/bold]     uid {mine['uid']} ({mine['username']})")
    console.print(f"  [bold]config[/bold]   {config_path()}")
    console.print()

    declared = service_ports()
    verdicts = verify_all()
    problems = 0
    for service in ("orchestrator", "embed", "daemon"):
        port = declared.get(service)
        if not port:
            info(f"{service}: no port declared (never booted on this install)")
            continue
        verdict = verdicts.get(service)
        if verdict is None:
            continue
        if verdict.state == "ours":
            ok(verdict.line())
        elif verdict.state == "down":
            warn(verdict.line())
        else:
            fail(verdict.line())
            problems += 1

    console.print()
    if problems:
        info(
            "Move the OTHER install's ports (run `okuro ports reassign` as that "
            "account), or re-port this one with `okuro ports reassign`."
        )
    raise SystemExit(1 if problems else 0)


@ports.command("reassign")
@click.option(
    "--yes", is_flag=True, help="Skip the confirmation prompt."
)
@click.option(
    "--no-units", is_flag=True, help="Do not rewrite this install's unit files."
)
@click.option(
    "--no-clients", is_flag=True, help="Do not rewrite MCP client configs."
)
def reassign(yes, no_units, no_clients):
    """Re-pick free ports for THIS install and rewrite config + units + clients."""
    from okuro.system.port_assign import reassign_ports
    from okuro.system.port_registry import service_ports

    before = service_ports()
    console.print()
    console.print(
        "  current: orchestrator {orchestrator}, embed {embed}, "
        "daemon {daemon}".format(**{k: v or "-" for k, v in before.items()})
    )
    if not yes:
        click.confirm(
            "  Re-pick this install's ports and rewrite config, units and "
            "MCP client configs?",
            abort=True,
        )

    try:
        result = reassign_ports(
            apply_units=not no_units, apply_clients=not no_clients
        )
    except RuntimeError as exc:
        fail(str(exc))
        raise SystemExit(1)

    after = result["after"]
    ok(
        "new ports: orchestrator {orchestrator}, embed {embed}, "
        "daemon {daemon}".format(**after)
    )
    info(f"config: {result['config']}")

    units = result.get("units") or {}
    if "_error" in units:
        warn(f"unit rewrite failed: {units['_error']}")
    elif units:
        info(f"units rewritten: {', '.join(sorted(k for k in units if not k.startswith('_')))}")

    clients = result.get("clients") or {}
    if "_error" in clients:
        warn(f"client config rewrite failed: {clients['_error']}")
    elif clients:
        info(f"client configs rewritten: {', '.join(sorted(clients))}")

    console.print()
    console.print("  [bold]Restart, in this order:[/bold]")
    for unit in result["restart"]:
        console.print(f"    systemctl --user restart {unit}")
    console.print()
    info("Then reconnect any MCP client (in Claude Code: /mcp reconnect okuro).")
