# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: IngressSupervisor — runs alongside the scheduler inside
#   okuro-daemon. Reads the integrations table, instantiates enabled
#   adapters, runs each under a supervised_run restart loop, and tears
#   them down when disabled. Exposes a snapshot for the /health endpoint.
# index: imports | constants | _build_adapter | IngressSupervisor |
#   default_on_message | snapshot
# AGENT_HEADER_END -->
"""Run all enabled ingress adapters under the okuro-daemon supervisor.

Lifecycle:

    daemon.__main__
        └── supervised_run("ingress")  ← daemon-level supervisor
                └── IngressSupervisor.run()
                        ├── poll integrations every CONFIG_POLL_SECONDS
                        ├── for each enabled row → start adapter task
                        │       └── supervised_run("ingress.<name>")  ← restart-with-backoff
                        │               └── adapter.run(stop_event, on_message)
                        └── for each disabled row → cancel adapter task

The default ``on_message`` callback persists every envelope as a
captured thought with ``category="ingress-unrouted"`` until step 4 wires
in the keyword router. Nothing is lost in the meantime.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from okuro.daemon.supervisor import supervised_run

from .adapter import IngressAdapter, IngressMessage, OnMessage
from . import storage

log = logging.getLogger("okuro.ingress.supervisor")

# How often the supervisor re-reads ``integrations`` to pick up
# enable/disable flips made through the web UI. 30 s feels live enough
# for a user toggling a checkbox; lower would just hammer SQLite for no
# real-world latency win.
CONFIG_POLL_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------

def _build_adapter(channel: str) -> Optional[IngressAdapter]:
    """Instantiate a known adapter by channel name. Returns None for
    unrecognized channels so a stale row can't crash the supervisor."""
    if channel == "telegram":
        from .adapters.telegram import TelegramAdapter
        return TelegramAdapter()
    log.warning("unknown ingress channel %r — ignoring row", channel)
    return None


# ---------------------------------------------------------------------------
# Default router (placeholder until step 4)
# ---------------------------------------------------------------------------

async def default_on_message(envelope: IngressMessage) -> None:
    """Route every envelope through the dispatcher (router + security
    gate + tool executor)."""
    from .dispatcher import dispatch
    try:
        await dispatch(envelope)
    except Exception:
        log.exception(
            "dispatcher failed for envelope (channel=%s, from=%s)",
            envelope.channel, envelope.from_id,
        )


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

class IngressSupervisor:
    """Owns the set of running adapter tasks and reconciles with config."""

    def __init__(self, on_message: OnMessage | None = None) -> None:
        self._on_message: OnMessage = on_message or default_on_message
        # channel → (adapter, asyncio.Task, per-adapter stop_event)
        self._running: dict[str, tuple[IngressAdapter, asyncio.Task, asyncio.Event]] = {}

    async def run(self, stop_event: asyncio.Event) -> None:
        """Reconcile loop. Returns when ``stop_event`` is set."""
        log.info("ingress supervisor starting")
        try:
            while not stop_event.is_set():
                try:
                    await self._reconcile()
                except Exception:
                    # Reconciliation failure must not kill the supervisor
                    # — sibling adapters keep running, and the daemon-level
                    # supervisor wraps us anyway.
                    log.exception("ingress reconcile failed")

                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=CONFIG_POLL_SECONDS
                    )
                except asyncio.TimeoutError:
                    continue
        finally:
            await self._shutdown_all()
            log.info("ingress supervisor stopped")

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    async def _reconcile(self) -> None:
        enabled_rows = storage.list_enabled()
        enabled = {r.channel for r in enabled_rows}
        running = set(self._running.keys())

        # Start newly-enabled adapters.
        for channel in enabled - running:
            adapter = _build_adapter(channel)
            if adapter is None:
                continue
            self._start_adapter(channel, adapter)

        # Stop adapters whose row was disabled or removed.
        for channel in running - enabled:
            await self._stop_adapter(channel)

    def _start_adapter(self, channel: str, adapter: IngressAdapter) -> None:
        per_stop = asyncio.Event()

        async def factory() -> None:
            await adapter.run(per_stop, self._on_message)

        task = asyncio.create_task(
            supervised_run(factory, name=f"ingress.{channel}"),
            name=f"ingress-{channel}",
        )
        self._running[channel] = (adapter, task, per_stop)
        log.info("started ingress adapter %r", channel)

    async def _stop_adapter(self, channel: str) -> None:
        entry = self._running.pop(channel, None)
        if entry is None:
            return
        _adapter, task, per_stop = entry
        per_stop.set()
        try:
            await asyncio.wait_for(task, timeout=10.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception:
            log.exception("ingress adapter %r raised on shutdown", channel)
        storage.mark_status(channel, "stopped")
        log.info("stopped ingress adapter %r", channel)

    async def _shutdown_all(self) -> None:
        for channel in list(self._running.keys()):
            await self._stop_adapter(channel)

    # ------------------------------------------------------------------
    # Health / introspection
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Per-adapter health snapshot for the /health endpoint."""
        adapters: dict[str, dict] = {}
        for channel, (adapter, task, _stop) in self._running.items():
            entry = adapter.health()
            entry["task_done"] = task.done()
            entry["task_cancelled"] = task.cancelled() if task.done() else False
            adapters[channel] = entry
        return {"adapters": adapters, "running_count": len(adapters)}


# Module-level singleton — daemon writes here at startup, /health reads.
_ACTIVE: Optional[IngressSupervisor] = None


def set_active(supervisor: IngressSupervisor) -> None:
    global _ACTIVE
    _ACTIVE = supervisor


def active_snapshot() -> dict:
    """Snapshot of the live supervisor, or an empty shape if not booted."""
    if _ACTIVE is None:
        return {"adapters": {}, "running_count": 0}
    return _ACTIVE.snapshot()
