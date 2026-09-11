# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro CLI — main entry point and command registration.
# index: imports | def _should_reexec_under_renamed_python | def _maybe_reexec_under_renamed_python | class AliasGroup | def cli | def _register_commands
# AGENT_HEADER_END -->
"""okuro CLI — main entry point and command registration."""

import os
import platform
import sys
from pathlib import Path


def _should_reexec_under_renamed_python() -> bool:
    """True iff this CLI invocation should re-exec under okuro-cli on Darwin.

    The pip console-script at ``<venv>/bin/okuro`` carries a ``#!python``
    shebang. The kernel resolves it and execs bare ``$VENV/bin/python``,
    which makes proc_name read ``python`` (Activity Monitor / Dock / ps -o
    comm see this — argv-rewriting via ``setproctitle`` cannot change it on
    Darwin). Re-execing under ``Okuro.app/Contents/MacOS/okuro-cli`` flips
    proc_name to ``okuro-cli`` AND lets Activity Monitor pull the bundle's
    icon. Launchd-installed services already point at renamed binaries
    directly so they don't take this hop.

    Skip cases:
        * non-Darwin platforms (Linux already shows the venv path; the
          icon mechanism is macOS-only)
        * already running under a renamed copy
        * test runs (``PYTEST_CURRENT_TEST`` set) — re-exec would replace
          the pytest process mid-run and lose all reporting
        * ``OKURO_NO_REEXEC=1`` escape hatch for debugging
        * imports / non-CLI invocations (e.g. ``python -c "import okuro"``)
    """
    if platform.system() != "Darwin":
        return False
    if not Path(sys.executable).name.startswith("python"):
        return False
    if os.environ.get("OKURO_NO_REEXEC") or "PYTEST_CURRENT_TEST" in os.environ:
        return False
    # Only re-exec on real CLI entry. ``__name__ == "__main__"`` covers
    # ``python -m okuro.cli.main``; sys.argv[0] basename ``okuro`` covers the
    # pip console-script at ``<venv>/bin/okuro`` and any symlink to it.
    invoked_as_main = __name__ == "__main__"
    invoked_as_console_script = bool(sys.argv) and Path(sys.argv[0]).name == "okuro"
    return invoked_as_main or invoked_as_console_script


def _maybe_reexec_under_renamed_python() -> None:
    """Re-exec under the renamed ``okuro-cli`` interpreter when appropriate.

    See ``_should_reexec_under_renamed_python`` for the decision logic and
    full rationale. Failure to re-exec is purely cosmetic — the CLI still
    works under the original interpreter, just with the wrong proc_name.
    Never crash the user's command for an Activity Monitor improvement.
    """
    if not _should_reexec_under_renamed_python():
        return
    try:
        from okuro.system.interpreter import ensure_okuro_interpreter

        target = ensure_okuro_interpreter("okuro-cli")
        if Path(sys.executable).resolve() == target.resolve():
            return  # already running under the renamed copy
        os.execv(
            str(target),
            [str(target), "-m", "okuro.cli.main", *sys.argv[1:]],
        )
    except Exception:
        pass


_maybe_reexec_under_renamed_python()


# setproctitle must run BEFORE click dispatch so every sub-command's ps /
# Activity Monitor row reads 'Okuro' instead of the bare venv python path.
# Individual commands override with a more specific title (e.g. cmd_dashboard
# → 'okuro-dashboard', cmd_init → 'okuro-init'). On macOS the renamed-copy
# re-exec above is what actually fixes proc_name; setproctitle still rewrites
# argv (visible to ps -f) for consistency with Linux. Capitalized 'Okuro'
# matches the bundle's CFBundleName.
try:
    import setproctitle

    setproctitle.setproctitle("Okuro")
except ImportError:  # pragma: no cover — setproctitle is a hard dep
    pass

import click

from okuro import __version__


class AliasGroup(click.Group):
    """Click group with abbreviated command matching."""

    def get_command(self, ctx, cmd_name):
        rv = super().get_command(ctx, cmd_name)
        if rv is not None:
            return rv
        matches = [c for c in self.list_commands(ctx) if c.startswith(cmd_name)]
        if len(matches) == 1:
            return super().get_command(ctx, matches[0])
        return None


@click.group(cls=AliasGroup, invoke_without_command=True)
@click.version_option(__version__, prog_name="okuro")
@click.pass_context
def cli(ctx):
    """Okuro — personal AI agent infrastructure.

    Running ``okuro`` with no arguments opens the web app:
    the onboarding wizard on a fresh install, the dashboard once set up.
    """
    if ctx.invoked_subcommand is not None:
        return

    # No subcommand → route to wizard (fresh install) or dashboard (returning user)
    from .cmd_dashboard import dashboard as dashboard_cmd
    from .cmd_init import init as init_cmd
    from .db_helpers import get_db

    first_run = _is_first_run(get_db)
    if first_run:
        ctx.invoke(init_cmd)
    else:
        ctx.invoke(dashboard_cmd)


def _is_first_run(get_db_fn) -> bool:
    """True when onboarding has not yet been completed.

    The onboarding backend stamps ``profile.onboarding.completed_at`` on
    ``POST /api/onboarding/complete`` (see orchestrator/api/onboarding.py).
    We read the same field out of the ``user_profile`` JSON.

    Any error reading the DB → assume first-run and let the wizard surface
    the real issue. Missing DB, missing row, missing field all count as
    first-run.
    """
    import json

    try:
        db = get_db_fn()
        row = db.fetchone("SELECT profile FROM user_profile WHERE id = 1")
        db.close()
    except Exception:
        return True

    if row is None:
        return True

    try:
        profile = json.loads(row.get("profile") or "{}")
    except (TypeError, json.JSONDecodeError):
        return True

    completed_at = (profile.get("onboarding") or {}).get("completed_at")
    return not completed_at


# --- Lazy command loading ---

def _register_commands():
    from .cmd_system import system
    from .cmd_gpu import gpu
    from .cmd_stats import stats
    from .cmd_keys import keys
    from .cmd_roles import roles
    from .cmd_search import search
    from .cmd_design import design
    from .cmd_models import models
    from .cmd_bridge import bridge
    from .cmd_cortex import cortex
    from .cmd_codegraph import codegraph
    from .cmd_init import init
    from .cmd_migrate import migrate
    from .cmd_deploy import deploy
    from .cmd_doctor import doctor
    from .cmd_run import run
    from .cmd_audit import audit
    from .cmd_web import web
    from .cmd_service import service
    from .cmd_dashboard import dashboard
    from .cmd_trace import trace
    from .cmd_distill import distill
    from .cmd_retro import retro
    from .cmd_memory import memory
    from .cmd_rules import rules
    from .cmd_uninstall import uninstall
    from .cmd_backup import backup
    from .cmd_git_guards import setup_git_guards
    from .cmd_worktree import wt
    from .cmd_voice import voice
    from .cmd_probe import probe_conventions
    from .cmd_canon import canon
    from .cmd_release import release

    cli.add_command(system)
    cli.add_command(gpu)
    cli.add_command(stats)
    cli.add_command(keys)
    cli.add_command(roles)
    cli.add_command(search)
    cli.add_command(design)
    cli.add_command(models)
    cli.add_command(bridge)
    cli.add_command(cortex)
    cli.add_command(codegraph)
    cli.add_command(init)
    cli.add_command(migrate)
    cli.add_command(deploy)
    cli.add_command(backup)
    cli.add_command(doctor)
    cli.add_command(run)
    cli.add_command(audit)
    cli.add_command(web)
    cli.add_command(service)
    cli.add_command(dashboard)
    cli.add_command(trace)
    cli.add_command(retro)
    cli.add_command(distill)
    cli.add_command(memory)
    cli.add_command(rules)
    cli.add_command(uninstall)
    cli.add_command(setup_git_guards)
    cli.add_command(wt)
    cli.add_command(voice)
    cli.add_command(probe_conventions)
    cli.add_command(canon)
    cli.add_command(release)

    # A command whose feature is OFF is dropped from the group, so it does not
    # appear in `okuro --help` and is not invocable.
    #
    # AFTER the block, not guarding each add_command: the imports above are a
    # flat block with no try/except, so nothing here can make a module optional
    # anyway — this gates VISIBILITY, which is what a maturity switch is for.
    # Dropping afterwards also means a gated name is spelled once, in
    # okuro.features, instead of twice.
    from okuro.features import withheld_commands
    for name in withheld_commands():
        cli.commands.pop(name, None)


_register_commands()


# Module-level entry so `python -m okuro.cli.main` works (used by the
# Okuro.app bundle launcher to invoke the CLI through a renamed Python
# copy living inside Contents/MacOS/, which the pip-installed
# `okuro = okuro.cli.main:cli` console script bypasses).
if __name__ == "__main__":
    cli()
