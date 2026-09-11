# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: `okuro canon` — inspect and deploy okuro's agent-facing surface (MCP
#   registration + instruction files) across every installed provider.
# index: imports | def canon | def status | def deploy | def unregister
# AGENT_HEADER_END -->
"""``okuro canon`` — the headless door to provider registration.

Canon owns what an agent sees: its instruction files AND the MCP registration
that gives it tools. Until now that surface could only be deployed through the
onboarding wizard's ``/canon/deploy`` endpoint, so install.sh and update.sh had
no way to register or refresh it — a fresh install was only wired up if the
user finished the wizard, and an update corrected nothing.

``status`` is read-only and answers the question a "registered: yes" light
cannot: registered *as what*, and does it still match what canon would write.
"""

from pathlib import Path

import click

from .output import data_table, fail, heading, info, ok, warn


@click.group("canon")
def canon():
    """Inspect and deploy okuro's agent-facing surface (MCP + instructions)."""


@canon.command("status")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http", "auto"]),
    default=None,
    help="Compare against what canon would write for this transport "
         "(default: $OKURO_MCP_TRANSPORT, else stdio).",
)
@click.option(
    "--base-home",
    type=click.Path(path_type=Path),
    default=None,
    help="Inspect configs under this home instead of the real user home "
         "(e.g. ~/.okuro-agent-home for the subagent surface).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON.")
def status(transport: str | None, base_home: Path | None, as_json: bool):
    """Show how okuro is registered with each provider."""
    from rich.console import Console

    from .mcp_config import registration_status

    try:
        st = registration_status(base_home=base_home, transport=transport)
    except ValueError as exc:
        fail(str(exc))
        raise SystemExit(2)

    if as_json:
        import json

        click.echo(json.dumps(st, indent=2))
        return

    console = Console()
    heading(f"MCP registration — expected transport: {st['transport_expected']}")
    info(f"home: {st['base_home']}")

    rows = []
    for name, p in st["providers"].items():
        if not p["supported"]:
            state = "n/a on this OS"
        elif not p["config_exists"]:
            state = "no config"
        elif not p["registered"]:
            state = "NOT registered"
        elif p["stale"]:
            state = "stale"
        else:
            state = "ok"
        rows.append([
            name,
            state,
            p["transport"] or "-",
            ", ".join(p["legacy_found"]) or "-",
        ])
    console.print(data_table(["provider", "state", "transport", "legacy"], rows))

    stale = [n for n, p in st["providers"].items() if p["stale"]]
    missing = [
        n for n, p in st["providers"].items()
        if p["supported"] and p["config_exists"] and not p["registered"]
    ]
    if stale:
        warn(f"stale (redeploy would rewrite): {', '.join(stale)}")
    if missing:
        warn(f"not registered: {', '.join(missing)}")
    if not stale and not missing:
        ok("every detected provider matches what canon would write")

    for u in st["unmanaged"]:
        warn(f"{u['scope']}: {u['server']} — {u['note']}")


@canon.command("deploy")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http", "auto"]),
    default=None,
    help="Transport to register (default: $OKURO_MCP_TRANSPORT, else stdio). "
         "'auto' probes the daemon and picks http when it is reachable.",
)
@click.option(
    "--base-home",
    type=click.Path(path_type=Path),
    default=None,
    help="Write MCP configs under this home instead of the real user home. "
         "MCP ONLY — instruction files and hooks always target the real home, "
         "so they are skipped entirely when this is given.",
)
@click.option(
    "--target",
    "targets",
    multiple=True,
    help="Limit instruction/hook deployment to these providers. Repeatable. "
         "Default: every detected provider.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the MCP entries that would be written; touch nothing.",
)
def deploy(
    transport: str | None,
    base_home: Path | None,
    targets: tuple[str, ...],
    dry_run: bool,
):
    """Deploy okuro's surface: MCP registration + instructions + hooks.

    The same work POST /api/onboarding/canon/deploy does — both call
    canon.deploy_surface, so install.sh and the wizard cannot drift apart.
    """
    import json

    from okuro.canon.deploy import deploy_surface

    from .mcp_config import _resolve_transport, generate_mcp_entries

    try:
        resolved = _resolve_transport(transport)
    except ValueError as exc:
        fail(str(exc))
        raise SystemExit(2)

    if dry_run:
        heading(f"canon deploy --dry-run (transport={resolved})")
        info("MCP entries that would be written:")
        click.echo(json.dumps(generate_mcp_entries(resolved), indent=2))
        info("Instruction files and hooks would also be written per provider.")
        return

    results = deploy_surface(
        targets=list(targets) or None,
        base_home=base_home,
        transport=resolved,
    )
    heading(f"canon deploy (transport={resolved})")

    scope = results.get("_scope")
    if scope and scope.get("mcp_only"):
        warn(f"MCP only — {scope['reason']}")

    if "_mcp_error" in results:
        fail(f"MCP config write failed: {results['_mcp_error']['error']}")

    for name, r in sorted(results.items()):
        if name.startswith("_") or name == "notes":
            continue
        if r.get("error"):
            fail(f"{name}: {r['error']}")
            continue
        if r.get("skipped"):
            info(f"{name}: {r['skipped']}")
            continue
        bits = []
        mcp = r.get("mcp")
        if mcp:
            bits.append(
                f"mcp {mcp['added']} added/{mcp['updated']} updated"
                if (mcp["added"] or mcp["updated"]) else "mcp up to date"
            )
        if r.get("instructions") is not None:
            bits.append(f"{len(r['instructions'])} instruction file(s)")
        if r.get("hooks"):
            bits.append(f"{len(r['hooks'])} hook(s)")
        (ok if bits else info)(f"{name}: {', '.join(bits) or 'nothing to do'}")

    tp = results.get("_tool_protocol", {})
    if tp.get("path"):
        ok(f"TOOL-PROTOCOL.md: {tp['path']}")
    elif tp.get("error"):
        fail(f"TOOL-PROTOCOL.md: {tp['error']}")


@canon.command("unregister")
@click.option(
    "--base-home",
    type=click.Path(path_type=Path),
    default=None,
    help="Remove from configs under this home instead of the real user home.",
)
@click.confirmation_option(
    prompt="Remove okuro's MCP entries from every provider config?"
)
def unregister(base_home: Path | None):
    """Remove okuro's MCP entries from every provider config."""
    from .mcp_config import unregister_all_configs

    results = unregister_all_configs(base_home)
    heading("canon unregister")
    for provider, count in results.items():
        if provider.startswith("_"):
            continue
        if count < 0:
            fail(f"{provider}: {results.get(f'_{provider}_error', 'failed')}")
        elif count:
            ok(f"{provider}: {count} removed")
        else:
            info(f"{provider}: nothing to remove")
