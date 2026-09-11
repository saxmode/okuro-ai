# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro dashboard — open the web dashboard (always /dashboard route).
# index: imports | def dashboard
# AGENT_HEADER_END -->
"""okuro dashboard — open the web dashboard.

Starts the FastAPI server in-process and opens the browser to /dashboard.
Skips the onboarding-state check: this command is the "I want the app" verb.
Use `okuro init` to (re)run the wizard.
"""

from __future__ import annotations

import click

from . import web_launcher
from .output import console, info, ok, warn


@click.command()
@click.option("--port", default=None, type=int, help="Port to bind (default: 13333, falls back if taken).")
@click.option("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1 — loopback only).")
@click.option("--no-browser", is_flag=True, help="Print the URL, don't open the browser.")
def dashboard(port, host, no_browser):
    """Launch the Okuro web dashboard.

    Prefers a long-running orchestrator service when one is already
    answering on the default port — `okuro service install okuro-orchestrator`
    (or completing the wizard) installs it. Falls back to an in-process
    uvicorn if no daemon is reachable.
    """
    # Rename the process so `ps` / `top` show "okuro-dashboard" instead of
    # the generic python launcher path.
    try:
        import setproctitle
        setproctitle.setproctitle("okuro-dashboard")
    except ImportError:
        pass
    # Prefer an existing orchestrator daemon — avoids spawning a duplicate
    # process every time the user runs `okuro`.
    from okuro.system.port_registry import orchestrator_port

    default_port = port or orchestrator_port()
    if web_launcher.is_server_running(default_port):
        url = f"http://{host}:{default_port}/dashboard"
        info(f"Orchestrator already running on {default_port} — reusing")
        if no_browser or not web_launcher.has_display():
            console.print(f"\n  Open [bold]{url}[/bold] in your browser\n")
        else:
            console.print(f"  [dim]Opening Okuro — close the window to exit (daemon keeps running)[/dim]\n")
            web_launcher.open_in_webview(url, title="Okuro Dashboard")
        return

    # No daemon → spin up an in-process server for this session.
    chosen_port = port or web_launcher.pick_free_port()
    url = f"http://{host}:{chosen_port}/dashboard"

    info(f"Starting okuro at {url}")
    web_launcher.start_server_thread(host, chosen_port)

    if not web_launcher.wait_for_ready(chosen_port):
        warn("Server did not become ready in 30s")
        raise SystemExit(1)

    ok("Ready")

    if no_browser or not web_launcher.has_display():
        console.print(f"\n  Open [bold]{url}[/bold] in your browser\n")
        console.print(f"  [dim]Press Ctrl-C to stop (install `okuro service install okuro-orchestrator` for a persistent daemon)[/dim]\n")
        web_launcher.serve_until_signal()
    else:
        console.print(f"  [dim]Close the window to stop (install `okuro service install okuro-orchestrator` for a persistent daemon)[/dim]\n")
        web_launcher.open_in_webview(url, title="Okuro Dashboard")
