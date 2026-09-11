# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: `okuro deploy` — discoverable alias for scripts/update.sh, the full
#   deploy pipeline (build frontend → stop services → migrate → start → verify).
# index: imports | def deploy
# AGENT_HEADER_END -->
"""`okuro deploy` — run the canonical full-deploy pipeline.

A code+schema change needs three independent things done in order: rebuild
the frontend (dist/), apply DB migrations (the orchestrator does NOT migrate
at boot), and restart the backend services on the new code. Doing them
piecemeal is easy to half-do — a bare service restart leaves the schema
stale. scripts/update.sh already sequences all of it safely (migrate runs
with services stopped, ordered restart, verify). This command is a thin,
discoverable alias that execs it and forwards any flags through.
"""

import os
from pathlib import Path

import click

from .output import fail


# src/okuro/cli/cmd_deploy.py → parents[3] == repo root (holds scripts/).
_UPDATE_SH = Path(__file__).resolve().parents[3] / "scripts" / "update.sh"


@click.command(context_settings=dict(ignore_unknown_options=True))
@click.argument("update_args", nargs=-1, type=click.UNPROCESSED)
def deploy(update_args):
    """Full deploy: build frontend, migrate DB, restart services, verify.

    Thin alias for scripts/update.sh — extra args/flags are forwarded to it.
    """
    if not _UPDATE_SH.is_file():
        fail(f"deploy script not found: {_UPDATE_SH}")
        raise SystemExit(1)
    # exec replaces this process so update.sh owns stdout/stderr + exit code.
    os.execvp("bash", ["bash", str(_UPDATE_SH), *update_args])
