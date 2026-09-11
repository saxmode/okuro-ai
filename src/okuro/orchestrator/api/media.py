# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: orchestrator.api.media — /api/media router. Request a podcast
#   (or other delivery channel) for a target person on a topic, with an
#   optional recurring schedule. Topic -> briefing artifact -> delivery
#   pipeline. Recurring writes an orchestrator mandate yaml the daemon
#   scheduler picks up.
# index: imports | models | _briefing_body | _build_briefing |
#   _write_recurring | POST create_media | GET list_media |
#   GET media_audio
# AGENT_HEADER_END -->
"""Media page backend — podcast-on-a-topic for a person, optional recurring.

Flow (POST /api/media):
  1. topic -> LLM briefing (bridge.invoke) -> artifact_write(kind=report)
     with ``## sections`` so SourceDocument parses a real outline.
  2. delivery.send(artifact_id, person_id, channel) -> WAV at body_path.
  3. recurring.enabled -> write ~/.okuro/orchestrator/recurring/<id>.yaml
     whose ``description`` instructs an agent to regenerate the channel.

Best-effort (HR-C3 mirror): pipeline failure never 500s — returns
{"success": false, "error": ...}.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/media", tags=["media"])

_DEFAULT_CHANNEL = "podcast"
_DEFAULT_CRON = "0 6 * * *"

# User-facing audio MODE → delivery channel. "podcast" = two-host chat about a
# topic; "summary" (audio summary) = one friendly narrator explaining what went
# on (the single-voice "tts" channel). `channel` stays accepted for advanced
# callers; `mode` is the friendly selector.
_MODE_TO_CHANNEL = {
    "podcast": "podcast",
    "summary": "tts",
    "audio_summary": "tts",
    "audio-summary": "tts",
}


def _resolve_channel(mode: Optional[str], channel: Optional[str]) -> str:
    """Map a user-facing mode to a channel; fall back to explicit channel."""
    if mode:
        m = mode.strip().lower().replace(" ", "_")
        if m in _MODE_TO_CHANNEL:
            return _MODE_TO_CHANNEL[m]
    return (channel or _DEFAULT_CHANNEL).strip() or _DEFAULT_CHANNEL


# ── Request / response models ────────────────────────────────────────


class RecurringSpec(BaseModel):
    enabled: bool = False
    cron: Optional[str] = None
    time: Optional[str] = None  # "HH:MM" — convenience, converted to cron


class MediaRequest(BaseModel):
    topic: str = Field(..., min_length=1)
    person_id: Optional[str] = None
    mode: Optional[str] = None       # "podcast" | "summary" (friendly selector)
    channel: str = _DEFAULT_CHANNEL  # advanced/back-compat; `mode` overrides
    brand_id: Optional[str] = None
    recurring: Optional[RecurringSpec] = None


# ── Helpers ──────────────────────────────────────────────────────────


def _slug(text: str, maxlen: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:maxlen].strip("-")) or "topic"


def _cron_from_time(time_str: Optional[str], cron: Optional[str]) -> str:
    """Resolve a cron expression from an explicit cron or an HH:MM time."""
    if cron and cron.strip():
        return cron.strip()
    if time_str and re.match(r"^\d{1,2}:\d{2}$", time_str.strip()):
        hh, mm = time_str.strip().split(":")
        try:
            h, m = int(hh), int(mm)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return f"{m} {h} * * *"
        except ValueError:
            pass
    return _DEFAULT_CRON


def _briefing_body(topic: str, content: str) -> str:
    """Wrap LLM output so SourceDocument parses ## sections.

    SourceDocument._parse splits on markdown headings. If the LLM already
    emitted ## headings we pass it through; otherwise we wrap the prose in
    a single section so the outline is never empty.
    """
    content = (content or "").strip()
    if re.search(r"^##\s+", content, re.MULTILINE):
        return content
    # No headings — synthesise a minimal multi-section shell so the
    # podcast outline has structure to talk through.
    return (
        f"## Overview\n\n{content or topic}\n\n"
        f"## Why it matters\n\n"
        f"This briefing covers {topic} and what it means going forward.\n"
    )


def _gather_context(topic: str) -> tuple[str, list[str]]:
    """RAG over okuro's brain for the topic — artifacts + memory + progress.

    Thin wrapper over the shared budget-aware retriever
    (``okuro.sense.grounding.gather_context``) so the podcast grounds exactly
    like slides/video/brain-advise. Self-exclusion: skip the media briefing we
    are about to write (same title as the topic) so the brain doesn't feed on
    its own generated podcasts. Web search is opt-in via OKURO_MEDIA_WEBSEARCH.
    """
    from okuro.peer.delivery import progress
    from okuro.sense.grounding import gather_context

    topic_l = topic.strip().lower()
    return gather_context(
        topic,
        budget=9000,
        project=("okuro" if "okuro" in topic_l else None),
        exclude_titles=(topic[:120],),
        web=os.environ.get("OKURO_MEDIA_WEBSEARCH", "1") == "1",
        progress_cb=lambda label, frac: progress.report(label, frac),
    )


def _build_briefing(topic: str) -> dict[str, Any]:
    """topic -> GROUNDED briefing artifact (RAG over okuro's brain).

    Returns {artifact_id, grounded, sources} or {error}.
    """
    context, sources = _gather_context(topic)
    grounded = bool(context.strip())

    from okuro.peer.delivery import progress
    progress.report("writing conversation", 0.22)

    if grounded:
        system_prompt = (
            "You are a briefing writer for a spoken podcast. Write a concise, "
            "engaging briefing on the topic using ONLY the RETRIEVED OKURO CONTEXT "
            "provided below. Do NOT invent facts that are not in the context; where "
            "the context is thin, say what is actually known rather than speculating. "
            "Use markdown with 3-5 '## ' section headings; start at the first '## '."
        )
        prompt = (
            f"TOPIC: {topic}\n\n"
            "RETRIEVED OKURO CONTEXT (the brain — ground every claim in this):\n\n"
            # gather_context already budget-bounds this (head+tail preserved);
            # a generous ceiling here just caps prompt size without re-cutting
            # the tail the retriever deliberately kept.
            f"{context[:12000]}\n\n"
            "Write the briefing now: 3-5 '## ' sections, ~400-700 words, grounded "
            "strictly in the context above."
        )
        summary = f"Grounded briefing on: {topic} — sources: {', '.join(sources)}"
    else:
        system_prompt = (
            "You are a briefing writer for a spoken podcast. No internal okuro "
            "knowledge was found for this topic, so write from general knowledge and "
            "do NOT fabricate okuro-specific facts. Use markdown with 3-5 '## ' "
            "section headings; start at the first '## '."
        )
        prompt = (
            f"Write a podcast briefing on the topic: {topic!r}. 3-5 markdown "
            "sections, ~400-700 words. Do not invent specifics you can't support."
        )
        summary = f"Briefing on: {topic} — general knowledge (no brain sources found)"

    body: str
    try:
        from okuro.bridge.invoke import invoke
        result = invoke(prompt, capability="translate", system_prompt=system_prompt, timeout=120)
        if isinstance(result, dict) and result.get("success"):
            body = _briefing_body(topic, str(result.get("output") or ""))
        else:
            err = (result or {}).get("error") if isinstance(result, dict) else None
            log.warning("media briefing LLM failed: %s — using context/fallback body", err)
            body = _briefing_body(topic, context[:1500])
    except Exception as exc:  # bridge unavailable — never block delivery
        log.warning("media briefing invoke raised: %s — using context/fallback body", exc)
        body = _briefing_body(topic, context[:1500])

    try:
        from okuro.sense.artifacts import artifact_write
        artifact_id = artifact_write(
            kind="report",
            task_id="media",
            title=topic[:120],
            summary=summary,
            body=body,
            media_type="text/markdown",
            created_by="media-page",
        )
    except Exception as exc:
        return {"error": f"artifact_write raised: {exc}"}

    if isinstance(artifact_id, str) and artifact_id.startswith("REJECTED"):
        return {"error": artifact_id}
    return {"artifact_id": artifact_id, "grounded": grounded, "sources": sources}


def _write_recurring(
    *,
    topic: str,
    person_id: Optional[str],
    channel: str,
    brand_id: Optional[str],
    cron: str,
) -> dict[str, Any]:
    """Register a DETERMINISTIC recurring media job.

    Not an orchestrator agent mandate — media generation is a fixed pipeline,
    so the daemon ``media-recurring`` handler runs it directly (see
    peer/delivery/recurring_media.py). This is what keeps a recurring brief
    cheap and unable to wedge the HTTP API.
    """
    try:
        from okuro.peer.delivery.recurring_media import add_job

        job = add_job(
            topic=topic,
            person_id=person_id,
            channel=channel,
            brand_id=brand_id,
            cron=cron,
        )
        return {"id": job["id"], "next_run_at": job["next_run_at"]}
    except Exception as exc:
        log.warning("media recurring add_job failed: %s", exc)
        return {"error": f"recurring write failed: {exc}"}


def _audio_url(delivery_id: str, media_type: Optional[str]) -> Optional[str]:
    """Audio stream URL for a delivery, or None if not audio."""
    if not delivery_id:
        return None
    if media_type and not media_type.startswith("audio/"):
        return None
    return f"/api/media/{delivery_id}/audio"


# ── Endpoints ────────────────────────────────────────────────────────


@router.post("")
def create_media(req: MediaRequest) -> dict[str, Any]:
    """Enqueue a generation job and return IMMEDIATELY.

    Generation (briefing → render → optional recurring) takes minutes, so it
    runs in a background thread (media_jobs) — the request never blocks and the
    Media page shows a live progress bar via GET /api/media/jobs. Generation
    runs OFF the HTTP worker, so it cannot wedge the API.
    """
    topic = req.topic.strip()
    if not topic:
        return {"job_id": None, "status": "failed", "error": "topic is required"}
    channel = _resolve_channel(req.mode, req.channel)

    from okuro.orchestrator.api.media_jobs import create as create_job
    job = create_job(
        topic=topic,
        person_id=req.person_id,
        channel=channel,
        brand_id=req.brand_id,
        recurring=(req.recurring.dict() if req.recurring else None),
    )
    return {"job_id": job["id"], "status": job["status"]}


@router.get("/jobs")
def list_jobs(limit: int = 20) -> dict[str, Any]:
    """Active + recently-finished generation jobs (drives the progress UI)."""
    from okuro.orchestrator.api.media_jobs import list_jobs as _list_jobs
    return {"jobs": _list_jobs(limit=max(1, min(limit, 50)))}


@router.get("")
def list_media(limit: int = 25, channel: str | None = None) -> dict[str, Any]:
    """Recent audio deliveries shaped for the Media page list.

    The list is history, not tied to the generate-mode selector: it returns ALL
    audio deliveries (podcast + summary/tts) newest-first, so switching the mode
    selector never hides recordings that already exist. `channel` is accepted for
    back-compat but no longer filters the list.
    """
    try:
        from okuro.peer.delivery.store import delivery_list

        rows = delivery_list(channel=None, limit=100)
    except Exception as exc:
        log.warning("media list failed: %s", exc)
        return {"media": []}

    lim = max(1, min(limit, 100))
    media: list[dict[str, Any]] = []
    for row in rows:
        if not str(row.get("media_type") or "").startswith("audio/"):
            continue  # audio deliveries only — skip markdown/marp/microsite
        if len(media) >= lim:
            break
        did = row.get("id")
        media_type = row.get("media_type")
        media.append(
            {
                "id": did,
                "title": row.get("title"),
                "person_id": row.get("person_id"),
                "channel": row.get("channel"),
                "created_at": row.get("created_at"),
                "media_type": media_type,
                "success": bool(row.get("success")),
                "audio_url": _audio_url(did, media_type) if row.get("success") else None,
            }
        )
    return {"media": media}


@router.get("/recurring")
def list_recurring() -> dict[str, Any]:
    """List registered recurring media jobs (daemon-run)."""
    try:
        from okuro.peer.delivery.recurring_media import list_jobs
        return {"jobs": list_jobs()}
    except Exception as exc:
        log.warning("list recurring failed: %s", exc)
        return {"jobs": []}


@router.delete("/recurring/{job_id}")
def delete_recurring(job_id: str) -> dict[str, Any]:
    """Remove a recurring media job."""
    try:
        from okuro.peer.delivery.recurring_media import remove_job
        removed = remove_job(job_id)
        return {"removed": removed, "id": job_id}
    except Exception as exc:
        log.warning("delete recurring failed: %s", exc)
        return {"removed": False, "id": job_id, "error": str(exc)}


@router.get("/{delivery_id}/audio")
def media_audio(delivery_id: str, request: Request):
    """Stream a delivery's audio (WAV) as binary for the <audio> player.

    Auth: the global BearerAuthMiddleware accepts ?token=<bearer>, which is
    how <audio src> (no custom headers) authenticates.
    """
    try:
        from okuro.peer.delivery.store import delivery_get

        row = delivery_get(delivery_id, include_body=False, include_blob=True)
    except Exception:
        row = None
    if row is None:
        raise HTTPException(404, f"Delivery {delivery_id} not found")

    media_type = row.get("media_type") or "audio/mpeg"
    body_path = row.get("body_path")
    if body_path:
        from pathlib import Path

        p = Path(body_path)
        if p.exists():
            return FileResponse(p, media_type=media_type, filename=p.name)

    blob = row.get("body_blob")
    if blob:
        if isinstance(blob, str):
            import base64

            try:
                blob = base64.b64decode(blob)
            except Exception:
                blob = blob.encode("utf-8", "ignore")
        return Response(content=blob, media_type=media_type)

    raise HTTPException(404, f"Delivery {delivery_id} has no audio payload")
