# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: orchestrator.api.media_jobs — in-memory async job tracker for the
#   Media page progress UI. POST /api/media enqueues a job and returns
#   immediately; a background thread runs the briefing->delivery pipeline,
#   reporting stage+progress via the ContextVar sink so the list shows a live
#   progress bar. Generation runs OFF the request thread (no API wedge).
# index: imports | def create | def list_jobs | def _run | def _update | def _prune
# AGENT_HEADER_END -->
"""Ephemeral media generation jobs (progress tracking).

Jobs live in memory only — lost on restart, which is fine: the finished
podcast persists in the deliveries table and shows in the normal list. The
job's sole purpose is to surface a live progress bar while the ~5-minute
render runs. Each job runs in a daemon thread that installs a progress sink
(peer.delivery.progress) so the podcast channel reports into it.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from typing import Any, Optional

log = logging.getLogger("okuro.orchestrator.api.media_jobs")

_JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()
_MAX_RETAINED = 50


def _now() -> str:
    return datetime.utcnow().isoformat()


def _update(job_id: str, **kw: Any) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            job.update(kw)
            job["updated_at"] = _now()


def _prune() -> None:
    if len(_JOBS) <= _MAX_RETAINED:
        return
    finished = sorted(
        (j for j in _JOBS.values() if j["status"] != "generating"),
        key=lambda j: j["updated_at"],
    )
    for j in finished[: len(_JOBS) - _MAX_RETAINED]:
        _JOBS.pop(j["id"], None)


def create(
    *,
    topic: str,
    person_id: Optional[str],
    channel: str,
    brand_id: Optional[str],
    recurring: Optional[dict],
) -> dict[str, Any]:
    """Enqueue a generation job and start it in the background. Returns the job."""
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "topic": topic,
        "person_id": person_id,
        "channel": channel,
        "status": "generating",   # generating | done | failed
        "stage": "queued",
        "progress": 0.0,
        "delivery_id": None,
        "recurring_id": None,
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    with _LOCK:
        _JOBS[job_id] = job
        _prune()
    threading.Thread(
        target=_run,
        args=(job_id, topic, person_id, channel, brand_id, recurring),
        daemon=True,
    ).start()
    return dict(job)


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    """Active + recently-finished jobs, newest first."""
    with _LOCK:
        items = sorted(_JOBS.values(), key=lambda j: j["created_at"], reverse=True)
        return [dict(j) for j in items[:limit]]


def _run(
    job_id: str,
    topic: str,
    person_id: Optional[str],
    channel: str,
    brand_id: Optional[str],
    recurring: Optional[dict],
) -> None:
    from okuro.peer.delivery import progress as P

    def sink(stage: str, frac: float) -> None:
        _update(job_id, stage=stage, progress=round(frac, 3))

    token = P.set_sink(sink)
    try:
        _update(job_id, stage="researching okuro brain", progress=0.02)
        from okuro.orchestrator.api.media import _build_briefing
        briefing = _build_briefing(topic)
        if briefing.get("error"):
            _update(job_id, status="failed", stage="briefing", error=briefing["error"])
            return

        from okuro.peer.delivery.pipeline import send as delivery_send
        result = delivery_send(
            briefing["artifact_id"],
            person_id=person_id,
            channel=channel,
            brand_id=brand_id,
            title=topic[:120],
            created_by="media-page",
        )
        if not result.get("success"):
            _update(job_id, status="failed", stage="render",
                    error=result.get("error") or "render failed")
            return

        recurring_id = None
        if recurring and recurring.get("enabled"):
            try:
                from okuro.peer.delivery.recurring_media import add_job
                from okuro.orchestrator.api.media import _cron_from_time
                cron = _cron_from_time(recurring.get("time"), recurring.get("cron"))
                rec = add_job(topic=topic, person_id=person_id, channel=channel,
                              brand_id=brand_id, cron=cron)
                recurring_id = rec.get("id")
            except Exception as exc:
                log.warning("media job recurring registration failed: %s", exc)

        _update(job_id, status="done", stage="ready", progress=1.0,
                delivery_id=result.get("delivery_id"), recurring_id=recurring_id)
    except Exception as exc:
        log.exception("media job %s failed", job_id)
        _update(job_id, status="failed", error=str(exc))
    finally:
        P.reset_sink(token)
