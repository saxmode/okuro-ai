# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro probe-conventions — manually re-run the provider tool-naming probe.
# index: imports | def probe_conventions
# AGENT_HEADER_END -->
"""``okuro probe-conventions`` — manually re-run the convention probe.

The probe normally fires automatically when bootstrap detects a CLI
version change. Run it by hand when you want to refresh the cache without
waiting for the next session bootstrap, or to validate a fresh provider
install.
"""

import os

import click


@click.command("probe-conventions")
@click.option(
    "--provider",
    "providers",
    multiple=True,
    help="Probe a specific provider id. Repeat for multiple. "
         "Default: every provider with a known CLI (claude-code, codex, antigravity).",
)
@click.option(
    "--timeout",
    type=int,
    default=None,
    help="Per-provider probe timeout in seconds "
         "(default: $OKURO_PROBE_TIMEOUT or 90).",
)
@click.option(
    "--no-probe",
    is_flag=True,
    default=False,
    help="Hard-skip every probe (same as setting OKURO_DISABLE_PROBE=1). "
         "Useful for billing-quota-sensitive sessions.",
)
def probe_conventions(
    providers: tuple[str, ...],
    timeout: int | None,
    no_probe: bool,
) -> None:
    """Probe each provider's CLI to capture how it surfaces okuro tools."""
    if no_probe or os.environ.get("OKURO_DISABLE_PROBE") == "1":
        click.echo("probe disabled (--no-probe / OKURO_DISABLE_PROBE=1); skipping.")
        return

    from okuro.sense.providers.probe import (
        _PROVIDER_CLI,
        _record_probe_result,
        probe_provider,
    )

    targets = list(providers) if providers else list(_PROVIDER_CLI.keys())
    click.echo(f"probing {len(targets)} provider(s)...")

    for pid in targets:
        result = probe_provider(pid, timeout=timeout)
        _record_probe_result(result)
        if result["ok"] == "yes" and result["tool_name"]:
            click.echo(
                f"  {pid:14s} → {result['tool_name']}    "
                f"(cli={result['cli_version']})"
            )
        else:
            click.echo(
                f"  {pid:14s} → FAILED    error={result['error']}"
            )
    click.echo()
    click.echo("results cached at ~/.okuro/cache/observed_conventions.yaml")
