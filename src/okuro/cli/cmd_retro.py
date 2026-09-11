# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro retro — run and inspect session-retro batches (Meta-Harness P5).
# index: imports | def retro | def run | def list_batches | def show
# AGENT_HEADER_END -->
"""okuro retro — causal post-mortems over low-score sessions."""

from __future__ import annotations

import json

import click

from .output import console, ok, warn, info, heading, data_table


@click.group()
def retro():
    """Run and inspect session retros."""


@retro.command()
@click.option("--window-days", default=14, show_default=True)
@click.option("--max-per-cohort", default=10, show_default=True)
@click.option("--provider", default="claude", show_default=True, help="Bridge provider id (claude / gemini / codex / local).")
def run(window_days: int, max_per_cohort: int, provider: str):
    """Run one retro batch now. Writes gotcha memories for supported patterns."""
    from okuro.sense.retros import run_retros

    heading("Session Retro")
    info(f"window_days={window_days}  max_per_cohort={max_per_cohort}  provider={provider}")
    result = run_retros(
        window_days=window_days,
        max_per_cohort=max_per_cohort,
        provider=provider,
    )
    if result.get("skipped_reason"):
        warn(result["skipped_reason"])
        return
    if result.get("error"):
        warn(f"error: {result['error']}")
    info(f"batch_id:  {result.get('batch_id')}")
    info(f"cohorts:   {result.get('low')} low vs {result.get('high')} high")
    info(f"patterns:  {result.get('patterns', 0)}")
    info(f"memories:  {len(result.get('memories') or [])} written")
    ok("Done.")


@retro.command("list")
@click.option("--limit", default=5, show_default=True)
def list_batches(limit: int):
    """List recent retro batches with per-pattern summaries."""
    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        "SELECT batch_id, ran_at, low_count, high_count, patterns_json, model, error "
        "FROM session_retro_batches ORDER BY ran_at DESC LIMIT ?",
        (limit,),
    )
    if not rows:
        warn("No retro batches yet. Run `okuro retro run` to create one.")
        return
    heading(f"Retro batches — last {len(rows)}")
    for r in rows:
        try:
            patterns = json.loads(r["patterns_json"] or "[]")
        except (TypeError, ValueError):
            patterns = []
        err = f"  ERROR: {r['error']}" if r.get("error") else ""
        info(
            f"{r['ran_at']}  batch={r['batch_id'][:8]}  "
            f"lows={r['low_count']} highs={r['high_count']} model={r['model'] or '-'}{err}"
        )
        if patterns:
            console.print(data_table(
                ["pattern", "evidence", "rec"],
                [
                    [
                        p.get("name", "?"),
                        str(len(p.get("evidence", []))),
                        (p.get("recommendation") or "")[:80],
                    ]
                    for p in patterns
                ],
            ))


@retro.command()
@click.argument("batch_id")
def show(batch_id: str):
    """Show one retro batch in full detail."""
    from okuro.db import get_db

    db = get_db()
    b = db.fetchone("SELECT * FROM session_retro_batches WHERE batch_id LIKE ?", (f"{batch_id}%",))
    if not b:
        warn(f"No batch matching {batch_id}")
        return
    heading(f"Retro batch {b['batch_id']}")
    info(f"Ran at:    {b['ran_at']}")
    info(f"Window:    {b['window_days']} days")
    info(f"Cohorts:   {b['low_count']} low, {b['high_count']} high")
    info(f"Model:     {b['model'] or '-'}")
    if b.get("error"):
        warn(f"Error: {b['error']}")
    try:
        patterns = json.loads(b["patterns_json"] or "[]")
    except (TypeError, ValueError):
        patterns = []
    for p in patterns:
        console.print()
        console.print(f"[bold]{p.get('name')}[/bold] — {p.get('description')}")
        console.print(f"  Evidence: {p.get('evidence')}")
        console.print(f"  Recommendation: {p.get('recommendation')}")
        if p.get("_memory_id"):
            console.print(f"  Memory: {p['_memory_id']}")
        if p.get("_rejected"):
            console.print(f"  [yellow]Rejected: {p['_rejected']}[/yellow]")
