# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro rules — inspect behavioral rule/rationale/evidence triples (Meta-Harness P10).
# index: imports | def rules | def list_all | def show | def why
# AGENT_HEADER_END -->
"""okuro rules — inspect behavioral rule triples."""

from __future__ import annotations

import click

from .output import console, ok, warn, info, heading, data_table


@click.group()
def rules():
    """Inspect behavioral rule/rationale/evidence triples."""


@rules.command(name="list")
def list_all():
    """List every section and its rule counts."""
    from okuro.sense.rules import load_all

    heading("Behavioral rules")
    bundles = load_all()
    total = sum(len(b["rules"]) for b in bundles.values())
    with_rat = sum(1 for b in bundles.values() for r in b["rules"] if r["rationale"])
    with_ev = sum(1 for b in bundles.values() for r in b["rules"] if r["evidence"])
    info(f"{total} rules across {len(bundles)} sections  ({with_rat} with rationale, {with_ev} with evidence)")
    console.print(data_table(
        ["section", "label", "count", "rationale", "evidence"],
        [
            [
                sid,
                b["label"],
                str(len(b["rules"])),
                str(sum(1 for r in b["rules"] if r["rationale"])),
                str(sum(1 for r in b["rules"] if r["evidence"])),
            ]
            for sid, b in bundles.items()
        ],
    ))


@rules.command()
@click.argument("section")
def show(section: str):
    """Dump one section's rules with rule/rationale/evidence fields."""
    from okuro.sense.rules import load_section

    try:
        rules_ = load_section(section)
    except KeyError as exc:
        warn(str(exc))
        return
    heading(f"Section: {section}  ({len(rules_)} rules)")
    for r in rules_:
        console.print(f"• [bold]{r['rule']}[/bold]")
        if r.get("rationale"):
            console.print(f"    rationale: {r['rationale']}")
        if r.get("evidence"):
            console.print(f"    evidence:  [dim]{r['evidence']}[/dim]")


@rules.command()
@click.option("--dry-run", is_flag=True, help="Show generated enrichment without writing to profile.")
@click.option("--provider", default="claude", show_default=True)
def enrich(dry_run: bool, provider: str):
    """Use the bridge LLM to fill missing rationales + evidence across all rules."""
    from okuro.sense.rules import enrich as do_enrich

    heading("Rules enrich")
    result = do_enrich(provider=provider, dry_run=dry_run)
    info(f"Targets:   {result['targets']}")
    info(f"Updated:   {result['updated']}")
    info(f"Persisted: {result['persisted']}")
    info(f"Model:     {result.get('model') or '-'}")
    if result.get("error"):
        warn(f"Error: {result['error']}")
        return
    if dry_run and result.get("enrichment"):
        console.print()
        console.print("[bold]Dry-run enrichment preview:[/bold]")
        for sid, rule_map in result["enrichment"].items():
            console.print(f"\n[cyan]{sid}[/cyan]")
            for rule, payload in (rule_map or {}).items():
                console.print(f"  • {rule}")
                console.print(f"    rationale: {payload.get('rationale')}")
                console.print(f"    evidence:  [dim]{payload.get('evidence')}[/dim]")
    ok("Done.")


@rules.command()
@click.argument("query")
@click.option("--limit", default=5, show_default=True)
def why(query: str, limit: int):
    """Fuzzy-match a rule and show WHY it exists (rationale + evidence)."""
    from okuro.sense.rules import find_rule

    matches = find_rule(query, limit=limit)
    if not matches:
        warn("No matching rule. Try a shorter or more distinctive query.")
        return
    heading(f"Matches for {query!r}")
    for m in matches:
        console.print()
        console.print(f"[bold]{m['rule']}[/bold]  [dim](section: {m['section_id']}, score {m['score']})[/dim]")
        if m.get("rationale"):
            console.print(f"  rationale: {m['rationale']}")
        else:
            console.print("  [yellow]no rationale recorded[/yellow]")
        if m.get("evidence"):
            console.print(f"  evidence:  [dim]{m['evidence']}[/dim]")
