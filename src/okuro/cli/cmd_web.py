# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro web — launch the merged FastAPI (orchestrator API + SPA).
# index: imports | def web
# AGENT_HEADER_END -->
"""okuro web — launch the merged FastAPI (orchestrator API + SPA)."""

import click

from .output import console, ok, info, warn


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def _default_orchestrator_port() -> int:
    from okuro.system.port_registry import orchestrator_port
    return orchestrator_port()


@click.command()
@click.option("--port", default=_default_orchestrator_port, help="Port to bind (default: orchestrator port from port_registry).")
@click.option(
    "--host",
    default="127.0.0.1",
    help="Host to bind. Defaults to loopback. "
         "Use 0.0.0.0 to expose on the LAN — requires --i-know-what-im-doing.",
)
@click.option(
    "--i-know-what-im-doing",
    "ack_lan",
    is_flag=True,
    default=False,
    help="Required when --host is non-loopback. Acknowledges that the full "
         "orchestrator API will be reachable from any host that can route to this machine.",
)
def web(port, host, ack_lan):
    """Launch the Okuro web portal (serves orchestrator API + SPA)."""
    # audit fix #1: default to loopback; require explicit opt-in for LAN exposure.
    # `okuro web` previously bound 0.0.0.0 silently, exposing every API surface
    # (task creation, log read, keyring unlock, service control) to anyone on
    # the LAN. Loopback is the safe default; LAN exposure is now a two-flag
    # ceremony so it can't happen by mistake or by copy-pasted command.
    if not _is_loopback(host) and not ack_lan:
        warn(
            f"Refusing to bind {host}:{port}: non-loopback host requires "
            f"--i-know-what-im-doing. The orchestrator API exposes task "
            f"execution and keyring access — only bind to LAN on a trusted "
            f"network."
        )
        raise click.Abort()
    if not _is_loopback(host):
        warn(
            f"Binding {host}:{port} — full API reachable from any host that "
            f"can route to this machine. Make sure the network is trusted."
        )
    info(f"Starting at http://{host}:{port}")

    import uvicorn

    uvicorn.run(
        "okuro.orchestrator.api.main:app",
        host=host,
        port=port,
        log_level="info",
    )
