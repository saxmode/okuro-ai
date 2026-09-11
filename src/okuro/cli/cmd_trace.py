# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro trace — manage the raw-trace store backing Meta-Harness P1.
# index: imports | def trace | def index | def status | def search
# AGENT_HEADER_END -->
"""okuro trace — manage the raw-trace store backing Meta-Harness P1."""

from __future__ import annotations

import json

import click

from .output import console, ok, warn, info, heading, data_table


@click.group()
def trace():
    """Inspect and refresh the raw agent-trace store."""


@trace.command()
@click.option("--force", is_flag=True, help="Re-index every transcript even if mtime is unchanged.")
def index(force: bool):
    """Ingest transcripts from all supported providers.

    Currently walks ``~/.claude/projects/**/*.jsonl``. Idempotent — safe to
    re-run. The daemon also runs this every 10 minutes in the background.
    """
    from okuro.trace.claude_code import ingest

    heading("Trace Ingest")
    result = ingest(force=force)
    info(f"Files seen:   {result['files']}")
    info(f"Ingested:     {result['ingested']}")
    info(f"Skipped:      {result['skipped']}")
    info(f"Events added: {result['events']}")
    ok("Done.")


@trace.command()
def status():
    """Show the current state of the trace store."""
    from okuro.db import get_db

    db = get_db()
    heading("Trace Store")

    tot_sessions = db.fetchone("SELECT COUNT(*) AS n FROM agent_sessions")["n"]
    tot_events = db.fetchone("SELECT COUNT(*) AS n FROM agent_events")["n"]
    info(f"Sessions: {tot_sessions:,}")
    info(f"Events:   {tot_events:,}")

    by_provider = db.fetchall(
        "SELECT provider, COUNT(*) AS n FROM agent_sessions GROUP BY provider ORDER BY n DESC"
    )
    console.print(data_table(
        ["provider", "sessions"],
        [[r["provider"], str(r["n"])] for r in by_provider],
        title="By provider",
    ))

    by_type = db.fetchall(
        "SELECT type, COUNT(*) AS n FROM agent_events GROUP BY type ORDER BY n DESC"
    )
    console.print(data_table(
        ["type", "count"],
        [[r["type"], f"{r['n']:,}"] for r in by_type],
        title="By event type",
    ))

    latest = db.fetchall(
        """
        SELECT session_id, provider, project_path, last_ts, assistant_count, model
        FROM agent_sessions ORDER BY last_ts DESC LIMIT 5
        """
    )
    console.print(data_table(
        ["session_id", "provider", "project", "last_ts", "assistant", "model"],
        [[r["session_id"][:8], r["provider"] or "", (r["project_path"] or "")[-30:], r["last_ts"] or "", str(r["assistant_count"]), r["model"] or ""] for r in latest],
        title="Most recent sessions",
    ))


@trace.command()
@click.argument("query")
@click.option("--limit", default=10, show_default=True)
@click.option("--provider", default=None)
def search(query: str, limit: int, provider: str | None):
    """Full-text search across stored traces."""
    import asyncio

    from okuro.trace.mcp_tools import handle_tool

    args = {"query": query, "limit": limit}
    if provider:
        args["provider"] = provider
    result = asyncio.run(handle_tool("trace_search", args))
    payload = json.loads(result[0].text)
    heading(f"trace_search {query!r} — {payload['count']} hits")
    for hit in payload["hits"]:
        console.print(f"[dim]{hit['timestamp'] or '?':<25}[/dim] {hit['session_id'][:8]} [cyan]{hit['type']:<11}[/cyan] {hit['excerpt']}")
