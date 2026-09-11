# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Entry point: ``python -m okuro.daemon`` — runs scheduler + HTTP MCP under per-task supervisors.
# index: imports | async def _run | def main
# AGENT_HEADER_END -->
"""Entry point: ``python -m okuro.daemon``.

Runs the unified daemon in the foreground. Each top-level coroutine is
wrapped in a ``supervised_run`` so a transient crash in one task (e.g.
a cron parse error in the scheduler) does **not** tear down the MCP
HTTP surface mid-call. Sibling tasks keep running; the failed task is
restarted with exponential backoff, and after exceeding its retry
budget it stays dead but the daemon survives.

Tasks supervised here:

1. ``DaemonScheduler`` — cron-driven background tasks (refresh,
   reminders, cortex prune, hygiene, …).
2. ``okuro.mcp.http_server.serve`` — Streamable HTTP MCP server for
   Claude/Codex/Cursor/Gemini.

SIGTERM / SIGINT triggers graceful shutdown of every supervised task.
The supervisor honours ``CancelledError`` and exits without restarting.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from .supervisor import supervised_run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("okuro.daemon")


def _migrate_on_boot() -> list[str]:
    """Apply pending DB migrations once at daemon startup. Returns applied names.

    Distribution keystone: `db.migrate()` is otherwise only reachable via
    `okuro init`/`doctor`/`audit`, so an upgraded install that just restarts
    the daemon would never apply new migrations and would silently run on a
    stale schema. migrate() is idempotent — it only applies migrations not yet
    recorded in `_migrations`. A failure here must NOT crash boot: the daemon
    can still serve on the existing schema, and a real gap surfaces via
    doctor/audit. Always returns a list (empty on no-op or on caught failure).
    """
    try:
        from okuro.db import get_db

        applied = get_db().migrate()
        if applied:
            log.info("migrate-on-boot: applied %d migration(s): %s",
                     len(applied), ", ".join(applied))
        else:
            log.info("migrate-on-boot: schema current (no pending migrations)")
        return applied
    except Exception:
        log.exception("migrate-on-boot failed (continuing on existing schema)")
        return []


async def _run() -> None:
    from okuro.mcp.http_server import serve as serve_mcp

    from ._handlers import uninstall_legacy_mcpd_once
    from .scheduler import DaemonScheduler

    # Apply pending migrations before anything touches the schema.
    _migrate_on_boot()

    # One-time migration: subagent #15 folded okuro-mcpd into okuro-daemon.
    # If the orphan unit is still installed on disk, stop+disable+uninstall
    # it. Gated by a marker file so we only do this once.
    try:
        uninstall_legacy_mcpd_once()
    except Exception:
        log.exception("legacy okuro-mcpd cleanup failed (continuing)")

    scheduler = DaemonScheduler()
    scheduler.load()

    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    supervised_tasks: list[asyncio.Task] = []

    def _shutdown(sig: signal.Signals) -> None:
        log.info("Received %s — shutting down", sig.name)
        stop_event.set()
        scheduler.stop()
        # Cancel each supervised wrapper so its in-flight coroutine and
        # its restart-loop exit cleanly. Cancellation propagates into
        # supervised_run, which re-raises CancelledError and stops.
        for task in supervised_tasks:
            if not task.done():
                task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown, sig)
        except NotImplementedError:
            pass  # Windows / non-unix event loops

    # SIGHUP → live scheduler reload (no restart). Lets the web API apply
    # schedule edits/toggles by writing config.yaml then HUP-ing the daemon.
    def _reload() -> None:
        log.info("Received SIGHUP — reloading scheduler")
        try:
            scheduler.reload()
        except Exception:
            log.exception("scheduler reload failed (keeping previous tasks)")

    try:
        loop.add_signal_handler(signal.SIGHUP, _reload)
    except (NotImplementedError, AttributeError):
        pass  # Windows / non-unix — SIGHUP unavailable

    # Always run the scheduler loop — even with zero enabled tasks it idles
    # cheaply (30s ticks). Running unconditionally lets a SIGHUP reload ADD
    # tasks live (e.g. the user re-enables the last disabled job) without a
    # daemon restart.
    log.info("scheduler ready (%d enabled tasks)", len(scheduler.tasks))

    async def _scheduler_coro() -> None:
        await scheduler.run()

    supervised_tasks.append(
        asyncio.create_task(
            supervised_run(_scheduler_coro, name="scheduler"),
            name="daemon-scheduler",
        )
    )

    # MCP HTTP factory — always supervised.
    async def _mcp_coro() -> None:
        await serve_mcp(stop_event=stop_event)

    supervised_tasks.append(
        asyncio.create_task(
            supervised_run(_mcp_coro, name="mcp-http"),
            name="daemon-mcp-http",
        )
    )

    # Ingress supervisor — runs telegram and other channel adapters
    # as supervised sub-tasks. Driven by the integrations table; if no
    # rows are enabled it just polls quietly.
    from okuro.ingress.supervisor import IngressSupervisor, set_active
    ingress_supervisor = IngressSupervisor()
    set_active(ingress_supervisor)

    async def _ingress_coro() -> None:
        await ingress_supervisor.run(stop_event)

    supervised_tasks.append(
        asyncio.create_task(
            supervised_run(_ingress_coro, name="ingress"),
            name="daemon-ingress",
        )
    )

    # Wait for everything. With supervisors in place, a task only
    # "completes" when (a) it returns cleanly, (b) it exhausts its
    # retry budget, or (c) it is cancelled by the shutdown handler.
    # We use return_exceptions=True so one supervisor giving up does
    # not cancel its siblings — that's the whole point of this audit.
    results = await asyncio.gather(*supervised_tasks, return_exceptions=True)

    stop_event.set()
    scheduler.stop()

    # Surface non-cancellation exceptions so systemd sees a real failure.
    for r in results:
        if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
            raise r


def main() -> None:
    # Rename the process so `ps` / `top` show "okuro-daemon" instead of
    # "python -m okuro.daemon". Best-effort: on macOS this only updates
    # the ps/top columns — Activity Monitor still reads the executable
    # path and shows "Python" there.
    try:
        import setproctitle
        setproctitle.setproctitle("okuro-daemon")
    except ImportError:
        pass
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    finally:
        log.info("okuro daemon stopped")


if __name__ == "__main__":
    main()
