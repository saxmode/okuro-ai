# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: peer.delivery.recurring_media — deterministic recurring media briefs.
#   A recurring brief is a FIXED pipeline (topic -> briefing artifact ->
#   delivery), NOT an agent task. Jobs live in a JSON store; the daemon
#   'media-recurring' handler ticks and runs DUE jobs sequentially (no
#   thundering herd) off the HTTP worker. Replaces the old orchestrator-
#   mandate yaml approach that spawned an LLM agent per fire and wedged the API.
# index: imports | def _store_path | def _load | def _save | def _slug |
#   def add_job | def list_jobs | def remove_job | def generate_once | def tick
# AGENT_HEADER_END -->
"""Deterministic recurring media generation.

Why not an orchestrator agent mandate (the old approach)? Media generation
needs no judgement — it's `topic -> briefing -> delivery.send`. Wrapping a
fixed function in an LLM agent is expensive, non-deterministic, and heavy
(two concurrent fires at 06:00 starved the HTTP threadpool). So a recurring
brief is a plain daemon handler that calls the pipeline directly.

The daemon ``media-recurring`` task (registry.py) ticks every 5 min, runs DUE
jobs **sequentially** with small jitter, and reschedules each via croniter.
Sequential execution in the daemon thread is what kills the herd — the API is
never in the loop.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("okuro.peer.delivery.recurring_media")

_DEFAULT_CRON = "0 6 * * *"


def _store_path() -> Path:
    override = os.environ.get("OKURO_MEDIA_DIR")
    base = Path(override).expanduser() if override else (
        Path(os.environ.get("HOME", str(Path.home()))) / ".okuro" / "media"
    )
    return base / "recurring.json"


def _load() -> list[dict[str, Any]]:
    p = _store_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text() or "[]")
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(jobs: list[dict[str, Any]]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(jobs, indent=2, ensure_ascii=False))
    tmp.replace(p)


def _slug(text: str, maxlen: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:maxlen].strip("-")) or "topic"


def _next_run(cron: str, last_run_at: str = "") -> str:
    """Reschedule via the orchestrator's croniter helper (UTC-naive)."""
    from okuro.orchestrator.recurring import compute_next_run
    return compute_next_run(cron, last_run_at)


# ── Job CRUD ─────────────────────────────────────────────────────────


def add_job(
    *,
    topic: str,
    person_id: str | None = None,
    channel: str = "podcast",
    brand_id: str | None = None,
    cron: str = _DEFAULT_CRON,
) -> dict[str, Any]:
    """Register a recurring media job. Returns the stored job dict."""
    jobs = _load()
    job = {
        "id": f"media-{_slug(topic)}-{uuid.uuid4().hex[:6]}",
        "topic": topic,
        "person_id": person_id,
        "channel": channel,
        "brand_id": brand_id,
        "cron": cron or _DEFAULT_CRON,
        "created_at": datetime.utcnow().isoformat(),
        "last_run_at": "",
        "next_run_at": _next_run(cron or _DEFAULT_CRON, ""),
        "run_count": 0,
    }
    jobs.append(job)
    _save(jobs)
    return job


def list_jobs() -> list[dict[str, Any]]:
    return _load()


def remove_job(job_id: str) -> bool:
    jobs = _load()
    kept = [j for j in jobs if j.get("id") != job_id]
    if len(kept) == len(jobs):
        return False
    _save(kept)
    return True


# ── Generation ───────────────────────────────────────────────────────


def generate_once(
    topic: str,
    person_id: str | None,
    channel: str = "podcast",
    brand_id: str | None = None,
) -> dict[str, Any]:
    """The deterministic pipeline: topic -> briefing artifact -> delivery.

    Shared by the daemon tick AND callable directly. Never raises.
    """
    try:
        from okuro.orchestrator.api.media import _build_briefing
        briefing = _build_briefing(topic)
        if briefing.get("error"):
            return {"success": False, "error": briefing["error"]}
        from okuro.peer.delivery.pipeline import send as delivery_send
        return delivery_send(
            briefing["artifact_id"],
            person_id=person_id,
            channel=channel,
            brand_id=brand_id,
            title=topic[:120],
            created_by="media-recurring",
        )
    except Exception as exc:  # never let a bad job kill the daemon tick
        log.warning("recurring_media generate_once failed: %s", exc)
        return {"success": False, "error": str(exc)}


def tick() -> dict[str, Any]:
    """Daemon handler — run DUE recurring media jobs sequentially.

    Sequential + jitter = no thundering herd. Runs in the daemon thread, so a
    slow synth never blocks the orchestrator HTTP API. Each job reschedules via
    its cron regardless of success so a failure doesn't re-fire every tick.
    """
    jobs = _load()
    if not jobs:
        return {"due": 0, "ran": []}

    now = datetime.utcnow()
    ran: list[dict[str, Any]] = []
    changed = False

    for job in jobs:
        nxt = job.get("next_run_at")
        if not nxt:
            continue
        try:
            due = datetime.fromisoformat(nxt) <= now
        except (TypeError, ValueError):
            due = False
        if not due:
            continue

        # Small jitter so multiple due jobs never start at the same instant.
        time.sleep(random.uniform(0.0, 4.0))
        result = generate_once(
            job.get("topic", ""),
            job.get("person_id"),
            job.get("channel", "podcast"),
            job.get("brand_id"),
        )
        ok = bool(result.get("success"))

        job["last_run_at"] = now.isoformat()
        job["next_run_at"] = _next_run(job.get("cron", _DEFAULT_CRON), job["last_run_at"])
        job["run_count"] = int(job.get("run_count", 0)) + 1
        changed = True
        ran.append({"id": job.get("id"), "ok": ok, "error": result.get("error")})
        log.info("media-recurring: ran %s ok=%s", job.get("id"), ok)

    if changed:
        _save(jobs)
    return {"due": len(ran), "ran": ran}
