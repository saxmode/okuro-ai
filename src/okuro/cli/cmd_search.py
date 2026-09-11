# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro search — semantic codebase search.
# index: imports | def search
# AGENT_HEADER_END -->
"""okuro search — semantic codebase search."""

import click

from .output import console, data_table


@click.command()
@click.argument("query")
@click.option("--top", default=5, help="Number of results.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def search(query, top, as_json):
    """Semantic search across indexed codebase."""
    from okuro.cortex.vectorstore import VectorStore

    vs = VectorStore()
    results = vs.search(query, n_results=top)

    if as_json:
        import json
        out = [{"path": str(r.file_path), "score": r.relevance, "snippet": r.snippet} for r in results]
        console.print(json.dumps(out, indent=2, default=str))
        return

    if not results:
        console.print("[dim]No results[/dim]")
        return

    rows = []
    for r in results:
        score = f"{r.relevance:.3f}"
        snippet = (r.snippet[:60] + "...") if r.snippet and len(r.snippet) > 60 else (r.snippet or r.purpose[:60])
        rows.append([str(r.file_path), score, snippet])

    table = data_table(["PATH", "SCORE", "SNIPPET"], rows)
    console.print(table)
