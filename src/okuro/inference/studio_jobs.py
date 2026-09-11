# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: In-process job + progress registry for the Studio's long operations
#          (setup orchestration = Gap A, generation with live progress = Gap C).
#          A job runs on a background thread and appends ordered progress events;
#          the REST layer starts a job (returns a job_id) and streams its events
#          over SSE by polling the event list. One transport for both flows
#          (DP10) so the UI has a single progress contract: {phase, message, pct}.
# index:
#   JobState / ProgressEvent / Job        (contracts)
#   class JobManager                       (create / get / run_async / events_since)
#   JOBS                                   (process-wide singleton)
# AGENT_HEADER_END -->
"""Studio job registry — progress for setup + generation.

The Studio has two long operations the UI must show progress for: first-time
setup (install ComfyUI + download a model + author a workflow — minutes) and a
generation (10-60 s). Both are modelled as a *job*: a background thread does the
work and calls ``job.emit(phase, message, pct)``; the REST layer polls
``events_since(job_id, cursor)`` and relays new events as SSE. Deliberately
in-process + thread-based (not asyncio) — the work is blocking I/O (git/pip/
network/subprocess) and a polled event list is trivially verifiable, avoiding
the "200 OK but zero bytes" asyncio-scheduling trap the preview stream hit.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

RUNNING = "running"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"


@dataclass
class ProgressEvent:
    phase: str          # engine | model | link | workflow | ready | generate | error
    message: str        # human-facing ("Downloading model…")
    pct: Optional[int] = None   # 0-100 within the whole job, best-effort
    data: dict = field(default_factory=dict)  # optional structured extras

    def to_dict(self) -> dict:
        d = {"phase": self.phase, "message": self.message}
        if self.pct is not None:
            d["pct"] = self.pct
        if self.data:
            d["data"] = self.data
        return d


@dataclass
class Job:
    id: str
    kind: str                       # setup | generate
    state: str = RUNNING
    events: list[ProgressEvent] = field(default_factory=list)
    result: Any = None
    error: Optional[str] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def request_cancel(self) -> None:
        """Signal the worker to stop. Idempotent; the worker observes it via
        :meth:`cancel_requested` and ends the job in the CANCELLED state."""
        self._cancel.set()

    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def emit(self, phase: str, message: str, pct: Optional[int] = None,
             **data) -> None:
        with self._lock:
            self.events.append(ProgressEvent(phase, message, pct, data or {}))

    def events_since(self, cursor: int) -> tuple[int, list[ProgressEvent]]:
        """New events after ``cursor`` + the new cursor. Cheap to poll."""
        with self._lock:
            return len(self.events), self.events[cursor:]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id, "kind": self.kind, "state": self.state,
                "events": [e.to_dict() for e in self.events],
                "result": self.result, "error": self.error,
            }


class JobManager:
    """Process-wide registry of Studio jobs. Thread-safe."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str) -> Job:
        job = Job(id=uuid.uuid4().hex, kind=kind)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        """Request cancellation of a running job. Returns False for an unknown or
        already-finished job."""
        job = self.get(job_id)
        if job is None or job.state != RUNNING:
            return False
        job.request_cancel()
        return True

    def run_async(self, kind: str, target: Callable[[Job], Any]) -> Job:
        """Create a job and run ``target(job)`` on a daemon thread.

        ``target`` emits progress via ``job.emit`` and returns the result (stored
        on the job). Any exception is captured as the job error; the job always
        ends in a terminal state so a poller/SSE stream can stop.
        """
        job = self.create(kind)

        def _run() -> None:
            try:
                job.result = target(job)
                job.state = DONE
                job.emit("ready", "Ready", 100)
            except Exception as exc:  # noqa: BLE001 — surfaced to the client
                # A cancel the worker honoured lands here (or a mid-flight error
                # after cancel was requested) — report it as cancelled, not a
                # failure, so the UI shows a clean stop.
                if job.cancel_requested():
                    job.state = CANCELLED
                    job.emit("cancelled", "Cancelled")
                else:
                    job.error = str(exc)
                    job.state = ERROR
                    job.emit("error", str(exc))

        threading.Thread(target=_run, name=f"studio-job-{job.id[:8]}",
                         daemon=True).start()
        return job


# Process-wide singleton the REST layer shares across requests.
JOBS = JobManager()
