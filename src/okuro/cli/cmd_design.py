# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro design — the design systems the engine can open.
# index: imports | def design | def design_list | def design_get | def design_tokens
# AGENT_HEADER_END -->
"""okuro design — design systems, read from the engine.

REPOINTED, NOT REWRITTEN. All three questions were always right — which design
systems exist, what is in one, what does it emit — and only the store was
wrong: they asked `okuro.design`, the v0 YAML profile layer retired in p10.
They now ask `design_engine`, which is the one module that owns a design
system.

ONE CAPABILITY IS GONE ON PURPOSE: `--format json`. v0's `generate_json`
produced a token dump beside the CSS, and the 2026-09-03 capability audit found
no non-CSS consumer anywhere. The engine emits a stylesheet; `design get` still
hands back the kit itself as JSON, which is the question a caller reaching for
`--format json` was usually asking.
"""

import click

from .output import console, data_table


@click.group()
def design():
    """Manage design systems."""


@design.command("list")
def design_list():
    """List the design systems this okuro can open."""
    from okuro.design_engine import store

    kits = store.list_kits()
    if not kits:
        console.print("[dim]No design systems found[/dim]")
        return

    rows = []
    for kit in kits:
        # A kit that will not parse is LISTED, not hidden — a user store is a
        # directory a human can edit, so a broken file is a normal state and a
        # list that omits it is a list that lies.
        try:
            brand = store.load(kit["id"])
            colour, font = brand.brand.canonical, brand.font.family
        except Exception:
            colour, font = "[red]unreadable[/red]", ""
        rows.append([kit["id"], kit["origin"], colour, font])

    console.print(data_table(["ID", "ORIGIN", "COLOUR", "TYPEFACE"], rows))


@design.command("get")
@click.argument("kit_id")
def design_get(kit_id):
    """Show a design system's authored brand."""
    import json

    from okuro.design_engine import store

    try:
        brand = store.load(kit_id)
    except Exception as exc:
        console.print(f"[red]Cannot open {kit_id}: {exc}[/red]")
        raise SystemExit(1)

    console.print(json.dumps(brand.model_dump(mode="json"), indent=2, sort_keys=True))


@design.command("tokens")
@click.argument("kit_id")
def design_tokens(kit_id):
    """Emit a design system's stylesheet — the same CSS `/engine.css` serves."""
    from okuro.design_engine import emit as emitter
    from okuro.design_engine import kits as shipped
    from okuro.design_engine import store

    try:
        brand = store.load(kit_id)
    except Exception as exc:
        console.print(f"[red]Cannot open {kit_id}: {exc}[/red]")
        raise SystemExit(1)

    # The shipped kit carries its own font sources; a user's does not.
    sources = shipped.get(kit_id).font_sources if kit_id in shipped.KITS else None
    click.echo(emitter.emit(brand, font_sources=sources).css)
