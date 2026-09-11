# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro run — decompose and execute tasks via orchestrator.
# index: imports | def run
# AGENT_HEADER_END -->
"""okuro run — decompose and execute tasks via orchestrator."""

import click

from .output import console, ok, fail, heading, info


@click.command()
@click.argument("task")
@click.option("--dry-run", is_flag=True, help="Show decomposition without executing.")
@click.option("--provider", default=None, help="Force a specific agent provider.")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed execution output.")
def run(task, dry_run, provider, verbose):
    """Decompose and execute a task via the orchestrator."""
    heading("Task")
    info(task)

    try:
        from okuro.orchestrator.decomposer import decompose_task
        from okuro.orchestrator.config import load_config

        config = load_config()
        phases = decompose_task(task, config=config)

        heading("Decomposition")
        for phase in phases:
            info(f"Phase {phase.id}: {phase.name}")
            for st in phase.subtasks:
                info(f"  {st.id}. {st.description[:60]} [dim]({st.role})[/dim]")

        if dry_run:
            console.print("\n  [dim]Dry run — not executing[/dim]")
            return

        heading("Execution")
        # The engine's canonical entry is the argparse-driven CLI in
        # okuro.orchestrator.engine.main — there is no execute_pipeline
        # function. For programmatic invocation from this Click wrapper,
        # spawn that CLI as a subprocess so arg parsing + signal handling
        # stay in one place. Previously this branch crashed with
        # ImportError on `execute_pipeline`.
        import shlex
        import subprocess
        import sys
        cmd = [
            sys.executable, "-m", "okuro.orchestrator.engine",
            task, "--yes",
        ]
        if provider:
            cmd.extend(["--preferred-cli", provider])
        info(f"$ {shlex.join(cmd)}")
        rc = subprocess.call(cmd)
        if rc == 0:
            ok("Pipeline finished")
        else:
            fail(f"Engine exited with status {rc}")
            raise SystemExit(rc)

    except ImportError as e:
        fail(f"Orchestrator not available: {e}")
        raise SystemExit(1)
    except Exception as e:
        fail(f"Execution failed: {e}")
        if verbose:
            import traceback
            console.print_exception()
        raise SystemExit(1)
