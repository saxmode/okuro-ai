# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism generation job registry — in-memory, thread-safe status
#   store so the gallery can show an in-progress deck (spinner + live phase)
#   from any view while a background generation runs.
# index: imports | _JOBS/_LOCK | create_job | set_phase | complete_job |
#   fail_job | list_jobs | _prune
# AGENT_HEADER_END -->
"""okuro·prism generation jobs — a tiny in-process progress registry.

``POST /api/prism/generate`` returns a ``job_id`` immediately and runs the
(1-5 min, two-LLM-pass) generation on a background thread. That thread pushes
phase updates here ("analyzing" → "writing" → "designing" → done); the gallery
polls ``GET /api/prism/generate/jobs`` every second and renders a spinner tile
per running job with its live phase.

Deliberately in-memory (single-process uvicorn on the LAN box): a generation is
ephemeral and short-lived, so a SQLite table would be bloat (DP09). Finished /
failed jobs linger briefly so the poller catches the terminal state, then are
pruned. If this ever runs multi-worker, swap the dict for the prism_events
change-feed table — the public surface here stays the same.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Optional

# Phase order the pipeline emits — kept here so the frontend and backend agree
# on the label set (okuro.prism.generate calls set_phase with these).
PHASES = ("queued", "analyzing", "writing", "designing")

# Terminal jobs stay listed this long so a ~1s poller reliably observes the
# done/error transition before the row vanishes. Running jobs older than the
# stall TTL are assumed crashed and dropped.
_DONE_TTL = 20.0
_STALL_TTL = 900.0

_JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


def create_job(
    topic: str,
    *,
    brand_id: Optional[str] = None,
    person_id: Optional[str] = None,
    tier: Optional[str] = None,
    diversity: Optional[int] = None,
) -> str:
    """Register a new running job; returns its id."""
    job_id = f"gen_{uuid.uuid4().hex[:12]}"
    with _LOCK:
        _JOBS[job_id] = {
            "id": job_id,
            "topic": topic,
            "brand_id": brand_id or None,
            "person_id": person_id or None,
            "tier": tier or None,
            "diversity": diversity,
            "phase": "queued",
            "status": "running",
            "doc_id": None,
            "title": None,
            "error": None,
            "created": _now(),
            "updated": _now(),
        }
    return job_id


def set_phase(job_id: str, phase: str) -> None:
    """Update a running job's live phase (best-effort — unknown id is a no-op)."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job and job["status"] == "running":
            job["phase"] = phase
            job["updated"] = _now()


def complete_job(job_id: str, *, doc_id: str, title: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            job.update(status="done", phase="done", doc_id=doc_id, title=title, updated=_now())


def fail_job(job_id: str, error: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            job.update(status="error", error=error, updated=_now())


def _prune(now: float) -> None:
    """Drop terminal jobs past _DONE_TTL and stalled running jobs. Caller holds _LOCK."""
    dead = [
        jid for jid, j in _JOBS.items()
        if (j["status"] != "running" and now - j["updated"] > _DONE_TTL)
        or (j["status"] == "running" and now - j["updated"] > _STALL_TTL)
    ]
    for jid in dead:
        _JOBS.pop(jid, None)


def list_jobs() -> list[dict[str, Any]]:
    """Active + recently-terminal jobs, newest first (for the gallery poller)."""
    now = _now()
    with _LOCK:
        _prune(now)
        return sorted((dict(j) for j in _JOBS.values()), key=lambda j: j["created"], reverse=True)
