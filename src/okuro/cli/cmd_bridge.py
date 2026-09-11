# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro bridge — LLM invocation via routing.
# index: imports | def bridge | def bridge_invoke | def bridge_providers
# AGENT_HEADER_END -->
"""okuro bridge — LLM invocation via routing."""

import click

from .output import console, data_table, heading


@click.group()
def bridge():
    """LLM invocation via hashi routing."""


@bridge.command("invoke")
@click.argument("prompt", required=False)
@click.option("--prompt-file", "prompt_file", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Read the prompt from a file (for large prompts, e.g. full bootstrap packets).")
@click.option("--capability", default="quality", help="Routing capability (quality, fast-draft, deep-research, analysis).")
@click.option("--provider", default=None, help="Force a specific provider.")
def bridge_invoke(prompt, prompt_file, capability, provider):
    """Send a prompt to an LLM via routing. Provide PROMPT or --prompt-file."""
    from okuro.bridge import invoke

    result = invoke(prompt=prompt, prompt_file=prompt_file,
                    capability=capability, provider=provider)
    if not result.get("success"):
        console.print(f"[red]{result.get('error') or 'invocation failed'}[/red]")
        raise SystemExit(1)
    click.echo(result.get("output", ""))


@bridge.command("providers")
def bridge_providers():
    """List available LLM providers and routing."""
    from okuro.bridge import list_providers, get_routing_table

    providers = list_providers()
    routing = get_routing_table()

    heading("Providers")
    rows = []
    for p in providers:
        rows.append([p.get("name", "?"), p.get("status", "?"), str(p.get("models", "?"))])
    console.print(data_table(["NAME", "STATUS", "MODELS"], rows))

    if routing:
        heading("Routing")
        rows = []
        for cap, target in routing.items():
            rows.append([str(cap), str(target)])
        console.print(data_table(["CAPABILITY", "PROVIDER"], rows))
