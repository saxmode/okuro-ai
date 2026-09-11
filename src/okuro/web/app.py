# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro.web — utility API endpoints + SPA static mount.
# index:
#   imports
#   def api_gpu
#   def api_storage
#   def api_system
#   def api_models
#   def api_doctor
#   def api_bridge_providers
#   def api_flows_list
#   def api_flow_get
#   def api_flow_create
#   def api_flow_update
#   def api_flow_delete
#   def api_fd_list
#   def api_fd_events
#   def api_fd_get
#   def api_fd_upsert
#   def api_fd_create
#   def api_fd_delete
#   def api_wf_list
#   def api_wf_events
#   def api_wf_compile
#   def api_wf_get
#   def api_wf_upsert
#   def api_wf_create
#   def api_wf_delete
#   def mount_spa
# AGENT_HEADER_END -->
"""okuro.web — utility API endpoints + SPA static mount.

This module used to be its own FastAPI app on port 13333. Per decision A1
(merge web + orchestrator) it now exposes:

- `router`: an APIRouter with utility endpoints (system, design, doctor, …)
- `mount_spa(app, dist_dir=None)`: mounts /assets + SPA index.html fallback

Both are wired into `okuro.orchestrator.api.main.app` so a single FastAPI
process serves the SPA + all API endpoints on one port.
"""

import asyncio
import json
import logging
import re
import threading
import time
from pathlib import Path

from fastapi import APIRouter, FastAPI, File, Form, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from okuro.db.engine import okuro_home

router = APIRouter()

log = logging.getLogger("okuro.web")

DIST_DIR = Path(__file__).parent / "dist"


@router.get("/api/gpu")
def api_gpu():
    from okuro.system.gpu import get_gpu_status

    return get_gpu_status()


@router.get("/api/storage")
def api_storage():
    from okuro.system.storage import get_storage_status

    return get_storage_status()


@router.get("/api/host")
def api_host():
    from okuro.system.host import get_host_status

    return get_host_status()


@router.get("/api/system")
def api_system():
    from okuro.system.gpu import get_gpu_status
    from okuro.system.storage import get_storage_status

    return {"gpu": get_gpu_status(), "storage": get_storage_status()}


# /api/roles listing moved to okuro.orchestrator.api.roles (subagent #13) so
# stale/knowledge_count fields stay attached to the listing shape.


@router.get("/api/models")
def api_models():
    """Local + subscription models, shaped for the Models page.

    list_models() returns a flat list; the page needs each row tagged with a
    ``provider``/``tier`` (subscription rows encode both in ``alias`` as
    ``<provider>/<tier>``; local + bundle rows group under "local"), plus
    by_source/by_provider counts for the filter chips.
    """
    from okuro.ai_models import list_models

    raw = list_models()
    models: list[dict] = []
    by_source: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    for m in raw:
        source = m.get("source", "unknown")
        alias = m.get("alias") or ""
        if source == "subscription" and "/" in alias:
            provider, tier = alias.split("/", 1)
        else:
            provider = "local" if source in ("local-gguf", "bundle") else source
            tier = None
        # subscription rows carry no id — the page keys on it, so fall back
        # to the alias (unique per provider/tier) then the name.
        mid = m.get("id") or m.get("alias") or m.get("name") or ""
        models.append({**m, "id": mid, "provider": provider, "tier": tier})
        by_source[source] = by_source.get(source, 0) + 1
        by_provider[provider] = by_provider.get(provider, 0) + 1
    return {
        "models": models,
        "by_source": by_source,
        "by_provider": by_provider,
        "total": len(models),
    }


# In-flight catalog pulls, keyed by catalog_id. A pull is a minutes-long
# download, so the POST kicks off a daemon thread and returns immediately;
# the UI polls /api/models/pull/status and re-fetches /api/models when done.
_PULL_JOBS: dict[str, dict] = {}
_PULL_LOCK = threading.Lock()


@router.get("/api/models/search")
async def api_models_search(query: str = "", modality: str = "text", limit: int = 20):
    """Search the OSS catalog (HuggingFace + Civitai), ranked for THIS box."""
    def _work():
        from okuro.ai_models import discover, rank
        from okuro.ai_models.edition import effective_detection, local_inference_enabled
        from okuro.capability import capabilities

        detection = capabilities()
        if not local_inference_enabled(detection):
            return []  # okuro-air: no local inference → nothing to discover
        entries = discover(query or None, modality=modality, limit=limit)
        ranked = rank(entries, effective_detection(detection), modality=modality)
        return [e.to_dict() for e in ranked]

    return await asyncio.to_thread(_work)


@router.get("/api/models/discoveries")
def api_models_discoveries(query: str = "", status: str = "", limit: int = 100):
    """The durable discoveries list — what the weekly scan found for this box.

    This is the Discover tab's DEFAULT content (no live network round-trip):
    fit-gated candidates the Monday scan filed, newest-and-highest-signal
    first. ``query`` substring-filters the durable list; ``status`` narrows to
    one lifecycle bucket. Dismissed rows are hidden unless explicitly asked for.
    """
    from okuro.ai_models import list_discoveries

    return {
        "discoveries": list_discoveries(
            status=status or None,
            query=query or None,
            limit=limit,
        ),
    }


@router.post("/api/models/discoveries/status")
def api_models_discovery_status(payload: dict):
    """Advance a discovery's lifecycle: acknowledged | installed | dismissed."""
    from fastapi import HTTPException

    from okuro.ai_models import set_status

    catalog_id = (payload or {}).get("catalog_id")
    status = (payload or {}).get("status")
    if not catalog_id or not status:
        raise HTTPException(status_code=400, detail="catalog_id and status required")
    try:
        ok = set_status(catalog_id, status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail=f"unknown catalog_id: {catalog_id}")
    return {"ok": True, "catalog_id": catalog_id, "status": status}


@router.post("/api/models/pull")
def api_models_pull(payload: dict):
    """Start acquiring a catalog entry into the bundle store (background)."""
    from fastapi import HTTPException

    from okuro.ai_models.edition import detect_edition, local_inference_enabled
    from okuro.capability import capabilities

    if not local_inference_enabled(capabilities()):
        raise HTTPException(
            status_code=403,
            detail=f"Local inference is off on okuro-{detect_edition(capabilities())} "
                   f"(no local GPU). Pulling models needs okuro-advanced or okuro-pro.",
        )

    catalog_id = (payload or {}).get("catalog_id")
    if not catalog_id:
        raise HTTPException(status_code=400, detail="catalog_id required")
    force = bool((payload or {}).get("force"))
    display_name = (payload or {}).get("display_name") or catalog_id

    def _now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    with _PULL_LOCK:
        job = _PULL_JOBS.get(catalog_id)
        if job and job.get("state") == "downloading":
            return job
        _PULL_JOBS[catalog_id] = {
            "catalog_id": catalog_id,
            "display_name": display_name,
            "state": "downloading",
            "error": None,
            "bundle_id": None,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "progress": 0.0,
            "started_at": _now(),
            "updated_at": _now(),
        }

    def _on_progress(p: dict) -> None:
        dl = int(p.get("downloaded_bytes", 0) or 0)
        total = int(p.get("total_bytes", 0) or 0)
        with _PULL_LOCK:
            j = _PULL_JOBS.get(catalog_id)
            if not j:
                return
            j["downloaded_bytes"] = dl
            j["total_bytes"] = total
            j["progress"] = round(min(1.0, dl / total), 4) if total > 0 else 0.0
            j["updated_at"] = _now()

    def _work():
        try:
            from okuro.ai_models.acquire import pull
            from okuro.ai_models.discovery import resolve_entry
            from okuro.ai_models.edition import effective_detection
            from okuro.capability import capabilities

            entry = resolve_entry(catalog_id)
            if entry is None:
                raise ValueError(f"unknown catalog_id: {catalog_id}")
            bundle = pull(
                entry,
                detection=effective_detection(capabilities()),
                allow_oversize=force,
                progress_cb=_on_progress,
            )
            # A pulled model is installed — reflect that on its discovery row so
            # the Discover list shows "Installed", not a stale "Pull" button.
            try:
                from okuro.ai_models import mark_installed
                mark_installed(catalog_id)
            except Exception:
                pass
            done = {"state": "done", "error": None, "bundle_id": bundle.id, "progress": 1.0}
        except Exception as exc:  # surface the failure to the poller
            done = {"state": "error", "error": str(exc), "bundle_id": None}
        with _PULL_LOCK:
            j = _PULL_JOBS.get(catalog_id, {"catalog_id": catalog_id, "display_name": display_name})
            j.update(done)
            j["updated_at"] = _now()
            _PULL_JOBS[catalog_id] = j

    threading.Thread(target=_work, name="models-pull", daemon=True).start()
    with _PULL_LOCK:
        return _PULL_JOBS[catalog_id]


@router.get("/api/models/pull/status")
def api_models_pull_status(catalog_id: str):
    """Poll ONE background pull's state: idle | downloading | done | error."""
    with _PULL_LOCK:
        return _PULL_JOBS.get(catalog_id, {"state": "idle", "error": None, "bundle_id": None})


@router.get("/api/models/pull/active")
def api_models_pull_active():
    """All tracked pull jobs (downloading + recently finished) so the client's
    downloads tray rehydrates after a refresh — state lives server-side, not in
    the page. Client removes a finished card via /api/models/pull/dismiss."""
    with _PULL_LOCK:
        return {"jobs": list(_PULL_JOBS.values())}


@router.post("/api/models/pull/dismiss")
def api_models_pull_dismiss(payload: dict):
    """Drop a finished (done/error) job from the tray. Downloading jobs stay."""
    catalog_id = (payload or {}).get("catalog_id")
    with _PULL_LOCK:
        j = _PULL_JOBS.get(catalog_id)
        if j and j.get("state") != "downloading":
            _PULL_JOBS.pop(catalog_id, None)
    return {"ok": True, "catalog_id": catalog_id}


@router.get("/api/models/gpu")
def api_models_gpu():
    """Per-GPU tenants: okuro's leased models + external holders, each enriched
    with its reclaim method (from the provider inventory) so the modal can offer
    the right unload action."""
    from okuro.inference.broker import Broker
    from okuro.inference.inventory import detect_providers, reclaim_for_tenant

    provs = {p.provider: p for p in detect_providers()}
    report = Broker().tenants_report()
    gpus = []
    for idx, g in report.items():
        holders = []
        for e in g.get("external", []):
            p = provs.get(e.get("tenant"))
            endpoint = p.endpoint if p else None
            holders.append({
                **e,
                "reclaim": reclaim_for_tenant(e.get("tenant"), endpoint),
                "endpoint": endpoint,
                "models": p.loaded_models if p else [],
            })
        gpus.append({**g, "external": holders})
    return {"gpus": gpus}


@router.post("/api/models/identify")
def api_models_identify(payload: dict):
    """Identify an unknown VRAM holder by pid (extended patterns → fast model).
    Read-only — no GPU action."""
    from fastapi import HTTPException

    from okuro.capability import _read_cmdline
    from okuro.inference.identify import identify_process

    pid = (payload or {}).get("pid")
    if not pid:
        raise HTTPException(status_code=400, detail="pid required")
    name = (payload or {}).get("name", "")
    return identify_process(int(pid), name=name, cmdline=_read_cmdline(int(pid)))


@router.post("/api/models/reclaim")
def api_models_reclaim(payload: dict):
    """Execute ONE user-chosen reclaim. process_stop is destructive → requires
    confirm=true. Never runs on its own."""
    from fastapi import HTTPException

    from okuro.inference.reclaim import execute_reclaim

    holder = (payload or {}).get("holder") or {}
    if not holder.get("reclaim"):
        raise HTTPException(status_code=400, detail="holder.reclaim required")
    if holder["reclaim"] == "process_stop" and not (payload or {}).get("confirm"):
        raise HTTPException(status_code=400, detail="process_stop requires confirm=true")
    return execute_reclaim(holder)


@router.get("/api/models/edition")
def api_models_edition():
    """This deployment's edition + local-inference availability, so the UI can
    hide the local-model surface on okuro-air (cloud-only)."""
    from okuro.ai_models.catalog import usable_vram_gb
    from okuro.ai_models.edition import detect_edition, local_inference_enabled, vram_ceiling_gb
    from okuro.capability import capabilities

    detection = capabilities()
    edition = detect_edition(detection)
    return {
        "edition": edition,
        "local_inference": local_inference_enabled(detection),
        "vram_ceiling_gb": vram_ceiling_gb(edition),
        "usable_vram_gb": usable_vram_gb(detection),
    }


def _suggest_prompt(name: str, description: str, context: str) -> tuple[str, str]:
    system = (
        "You are a senior project planner. Decompose a project into 5-8 HIGH-LEVEL "
        "epics (phases or workstreams), NOT granular tasks. Each epic has a short title, "
        "a rough duration in working days, and finish-to-start dependencies (ids of epics "
        "that must finish before it can start). Be realistic and concise. Output ONLY JSON."
    )
    user = (
        f"Project: {name}\n"
        f"Description: {description}\n\n"
        f"Existing context from memory (use it, do not repeat it):\n{context}\n\n"
        "Return ONLY this JSON shape, nothing else:\n"
        '{"epics":[{"id":"kebab-slug","title":"...","durationDays":5,"depends_on":["id"]}]}'
    )
    return system, user


def _parse_epics(output: str) -> list[dict]:
    import json
    import re

    match = re.search(r"\{.*\}", output or "", re.S)
    if not match:
        raise RuntimeError("no JSON object in LLM output")
    data = json.loads(match.group(0))
    clean, seen = [], set()
    for e in data.get("epics") or []:
        eid = str(e.get("id") or "").strip()
        if not eid or eid in seen:
            continue
        seen.add(eid)
        clean.append(
            {
                "id": eid,
                "title": str(e.get("title") or eid)[:120],
                "durationDays": max(1, int(e.get("durationDays") or 5)),
                "depends_on": [str(d) for d in (e.get("depends_on") or []) if d],
            }
        )
    ids = {e["id"] for e in clean}
    for e in clean:
        e["depends_on"] = [d for d in e["depends_on"] if d in ids and d != e["id"]]
    return clean


def _humanize_activity(row: dict) -> str | None:
    t = row.get("type")
    if t == "session_start":
        return "Claude Sonnet session started"
    if t == "thinking":
        p = (row.get("preview") or "").strip().replace("\n", " ")
        return ("thinking — " + p[:90]) if p else "thinking…"
    if t == "tool_use":
        return f"using {row.get('name', 'tool')}…"
    if t == "text":
        return "drafting the plan…"
    return None


@router.post("/api/gantt/suggest")
async def api_gantt_suggest(payload: dict):
    """Decompose a project description into a high-level epic graph, STREAMING the
    live inference activity (NDJSON). Each line is {type:activity|done|error,...};
    activity lines drive the UI toast (P1 of Goal -> Gantt -> Orchestrator).
    """
    import asyncio
    import json
    import os
    import tempfile
    from pathlib import Path

    from fastapi import HTTPException
    from fastapi.responses import StreamingResponse

    description = (payload.get("description") or "").strip()
    project = (payload.get("project") or "").strip() or None
    name = (payload.get("name") or "").strip() or project or "Project"
    if not description:
        raise HTTPException(400, "description required")

    async def gen():
        from okuro.bridge.invoke import invoke
        from okuro.sense.memory import read_memory

        def line(obj: dict) -> str:
            return json.dumps(obj) + "\n"

        yield line({"type": "activity", "msg": "pulling related memory…"})
        sink = Path(tempfile.mktemp(suffix=".gantt-activity.jsonl"))
        sink.write_text("")
        holder: dict = {}

        def _run():
            try:
                context = (read_memory(query=description, project=project, limit=6) or "")[:2500] or "(none on file)"
            except Exception:
                context = "(none on file)"
            system, user = _suggest_prompt(name, description, context)
            holder["res"] = invoke(prompt=user, system_prompt=system, timeout=180, activity_sink=sink)

        loop = asyncio.get_event_loop()
        fut = loop.run_in_executor(None, _run)
        yield line({"type": "activity", "msg": "decomposing with Claude Sonnet…"})

        pos, seen_msgs = 0, 0
        while not fut.done():
            await asyncio.sleep(0.4)
            try:
                data = sink.read_text()
            except OSError:
                data = ""
            chunk, pos = data[pos:], len(data)
            for raw in chunk.splitlines():
                if not raw.strip():
                    continue
                try:
                    msg = _humanize_activity(json.loads(raw))
                except Exception:
                    msg = None
                if msg:
                    seen_msgs += 1
                    yield line({"type": "activity", "msg": msg})
        await fut

        try:
            os.remove(sink)
        except OSError:
            pass

        res = holder.get("res") or {}
        if not res.get("success"):
            yield line({"type": "error", "error": res.get("error") or "LLM invoke failed"})
            return
        try:
            epics = _parse_epics(res.get("output") or "")
        except Exception as exc:  # noqa: BLE001
            yield line({"type": "error", "error": f"parse failed: {exc}"})
            return
        yield line({"type": "done", "epics": epics, "provider": res.get("provider"), "model": res.get("model")})

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post("/api/gantt/context")
async def api_gantt_context(payload: dict):
    """RAG pre-fill for the per-epic intake: existing memory relevant to an epic."""
    import asyncio

    query = (payload.get("query") or "").strip()
    project = (payload.get("project") or "").strip() or None
    if not query:
        return {"context": ""}

    def _work() -> dict:
        from okuro.sense.memory import read_memory

        try:
            return {"context": (read_memory(query=query, project=project, limit=6) or "")[:2500]}
        except Exception as exc:  # noqa: BLE001
            return {"context": "", "error": str(exc)}

    return await asyncio.to_thread(_work)


@router.post("/api/stt")
async def api_stt(file: UploadFile = File(...)):
    """Transcribe an uploaded audio clip for browser dictation. Local-first:
    CPU faster-whisper (no GPU, no cloud, stays on LAN per SYS-LAN). Falls back
    to cloud Groq Whisper only when the local engine is unavailable.
    """
    import logging

    from fastapi import HTTPException

    audio = await file.read()
    if not audio:
        raise HTTPException(400, "empty audio")

    # 1) Local CPU whisper — preferred: private, no cloud cost, LAN-only.
    from okuro.voice import whisper_cpu

    if whisper_cpu.is_available():
        try:
            text = await asyncio.to_thread(whisper_cpu.transcribe_bytes, audio)
            return {"text": text, "engine": "faster-whisper"}
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("okuro.web.stt").warning(
                "local STT failed, trying cloud fallback: %s", exc
            )

    # 2) Cloud fallback (Groq Whisper) — only if a key is configured.
    import httpx

    from okuro.keyring.storage import KeyringStorage

    key = KeyringStorage().get_key("groq_api_key")
    if not key:
        raise HTTPException(
            503,
            "STT unavailable: local faster-whisper not installed and no "
            "groq_api_key in keyring",
        )
    fname = file.filename or "clip.webm"
    mime = file.content_type or "audio/webm"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (fname, audio, mime)},
                data={"model": "whisper-large-v3", "response_format": "json"},
            )
            r.raise_for_status()
            return {"text": (r.json().get("text") or "").strip(), "engine": "groq"}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"STT failed: {exc}") from exc


@router.websocket("/api/stt/stream")
async def api_stt_stream(ws: WebSocket):
    """Real-time streaming dictation. Client streams 16 kHz mono Int16LE PCM as
    binary frames; server pushes {type:'delta', committed, interim} as words
    stabilise (LocalAgreement-2) and {type:'final', committed} on stop. CPU-only
    faster-whisper, LAN-local (SYS-LAN) — no GPU, no cloud. Falls back gracefully
    to the batch /api/stt path on the client when this socket is unavailable.
    """
    import numpy as np
    from starlette.websockets import WebSocketDisconnect

    from okuro.voice import whisper_stream

    await ws.accept()
    if not whisper_stream.is_available():
        await ws.send_json({"type": "error", "message": "local streaming STT unavailable"})
        await ws.close()
        return

    lang = ws.query_params.get("lang") or None
    st = await asyncio.to_thread(whisper_stream.StreamingTranscriber, lang)
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                st.feed(pcm)
                committed, interim = await asyncio.to_thread(st.process)
                if committed or interim:
                    await ws.send_json(
                        {"type": "delta", "committed": committed, "interim": interim}
                    )
                continue
            text = msg.get("text")
            if text == "stop":
                final = await asyncio.to_thread(st.finish)
                await ws.send_json({"type": "final", "committed": final})
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("okuro.web.stt").warning("stream STT error: %s", exc)
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


async def _doctor_execute() -> dict:
    """Run the checks once and store the result in the module cache."""
    global _doctor_cache

    from okuro.clock import utc_iso
    from okuro.system.doctor import run_all, to_dict

    results = await asyncio.to_thread(run_all)
    checks = [to_dict(r) for r in results]
    payload = {
        "checks": checks,
        "passed": sum(1 for c in checks if c["status"] == "ok"),
        "warned": sum(1 for c in checks if c["status"] == "warn"),
        "failed": sum(1 for c in checks if c["status"] == "fail"),
    }
    _doctor_cache = {
        "payload": payload,
        "monotonic": time.monotonic(),
        # UTC, because everything okuro stores and compares is UTC.
        "computed_at": utc_iso(),
    }
    return payload


async def _doctor_single_flight() -> dict:
    """Coalesce concurrent callers onto ONE execution.

    Deliberately not ``async with lock: await run()`` — that SERIALIZES:
    caller #2 waits the full run and then pays its own, which is strictly
    worse than the overlap it was meant to fix. Callers await the same Task
    instead, and ``shield`` keeps a client disconnect from cancelling the run
    other callers are still waiting on.
    """
    global _doctor_inflight

    async with _doctor_cache_lock:
        inflight = _doctor_inflight
        if inflight is None or inflight.done():
            inflight = asyncio.ensure_future(_doctor_execute())
            _doctor_inflight = inflight
    return await asyncio.shield(inflight)



# --- /api/doctor: TTL cache + single flight -------------------------------
# Two SPA hooks poll this endpoint continuously (use-activity-stream.ts:72 and
# pages/health.tsx:116), one per mounted component instance per tab. Before the
# cache, that meant overlapping full runs: py-spy caught two threadpool threads
# inside run_all simultaneously, and a single run measured 291.95s while every
# other request degraded from 22ms to 531ms for its whole duration.
#
# The cache is what the pollers are MEANT to hit. `?fresh=1` forces a real run
# for a probe or a human who wants the true cold number; the response always
# stamps which path served it, so a measurement can never silently be a cache
# hit reported as cold. OKURO_DOCTOR_CACHE=0 disables the cache entirely
# (rollback without a deploy).
_DOCTOR_CACHE_TTL_SECONDS = 60.0
_doctor_cache: dict | None = None
_doctor_cache_lock = asyncio.Lock()
_doctor_inflight: "asyncio.Future | None" = None



@router.get("/api/doctor")
async def api_doctor(fresh: bool = False):
    """Run health checks and return structured results.

    Shares the check registry with the CLI ``okuro doctor`` command so
    both surfaces report the same set of probes. Blocking probes (DB migrate,
    embed HTTP, cortex stats) run in the default thread pool via
    ``asyncio.to_thread`` so the FastAPI event loop stays responsive.

    ``?fresh=1`` bypasses the TTL cache. The route is bearer-gated like every
    other ``/api`` path, so the bypass is not anonymously reachable.
    """
    import os

    cache_enabled = os.environ.get("OKURO_DOCTOR_CACHE", "1") != "0"
    entry = _doctor_cache
    if cache_enabled and not fresh and entry is not None:
        age = time.monotonic() - entry["monotonic"]
        if age < _DOCTOR_CACHE_TTL_SECONDS:
            served = dict(entry["payload"])
            served["cache"] = {
                "hit": True,
                "age_seconds": round(age, 3),
                "ttl_seconds": _DOCTOR_CACHE_TTL_SECONDS,
                "computed_at": entry["computed_at"],
                "note": (
                    "served from the TTL cache — the SPA's pollers are meant "
                    "to land here. Use ?fresh=1 to force a real run."
                ),
            }
            return served

    payload = await _doctor_single_flight()
    served = dict(payload)
    entry = _doctor_cache
    served["cache"] = {
        "hit": False,
        "age_seconds": (
            round(time.monotonic() - entry["monotonic"], 3) if entry else 0.0
        ),
        "ttl_seconds": _DOCTOR_CACHE_TTL_SECONDS if cache_enabled else 0.0,
        "computed_at": entry["computed_at"] if entry else None,
        "note": (
            "cache disabled via OKURO_DOCTOR_CACHE=0" if not cache_enabled
            else "forced fresh run (?fresh=1)" if fresh
            else "cache miss or expired — this is a real run"
        ),
    }
    return served


@router.get("/api/bridge/providers")
def api_bridge_providers():
    from okuro.bridge import list_providers, get_routing_table

    return {"providers": list_providers(), "routing": get_routing_table()}


# -- Flows (role-team templates) --

@router.get("/api/flows")
def api_flows_list():
    """List all saved flows — summary shape (id, name, description, role_count)."""
    from okuro.flows import list_flows

    flows = list_flows()
    return {
        "flows": [
            {
                "id": f.id,
                "name": f.name,
                "description": f.description,
                "role_count": len(f.roles),
                "role_ids": [r.role_id for r in f.roles],
                "has_orchestrator": True,
                "created_at": f.created_at,
                "updated_at": f.updated_at,
            }
            for f in flows
        ]
    }


@router.get("/api/flows/{flow_id}")
def api_flow_get(flow_id: str):
    """Return a single flow with full role placements."""
    from okuro.flows import get_flow

    flow = get_flow(flow_id)
    if flow is None:
        return Response(content='{"error":"not found"}', status_code=404,
                        media_type="application/json")
    return flow.to_dict()


def _flow_from_payload(payload: dict, *, fallback_id: str | None = None):
    from okuro.flows import (
        DEFAULT_ORCHESTRATOR_X,
        DEFAULT_ORCHESTRATOR_Y,
        Flow,
        OrchestratorPlacement,
        RolePlacement,
        slugify,
    )

    name = (payload.get("name") or "").strip()
    if not name:
        return None, "name required"
    flow_id = (payload.get("id") or fallback_id or slugify(name)).strip()
    roles_raw = payload.get("roles") or []
    roles: list[RolePlacement] = []
    for r in roles_raw:
        if isinstance(r, str):
            roles.append(RolePlacement(role_id=r))
        elif isinstance(r, dict):
            rid = (r.get("role_id") or "").strip()
            if not rid:
                continue
            roles.append(RolePlacement(
                role_id=rid,
                x=float(r.get("x", 0.0)),
                y=float(r.get("y", 0.0)),
            ))
    orch_raw = payload.get("orchestrator") or {}
    orchestrator = OrchestratorPlacement(
        x=float(orch_raw.get("x", DEFAULT_ORCHESTRATOR_X)),
        y=float(orch_raw.get("y", DEFAULT_ORCHESTRATOR_Y)),
    )
    return Flow(
        id=flow_id,
        name=name,
        description=(payload.get("description") or "").strip(),
        orchestrator=orchestrator,
        roles=roles,
    ), None


@router.post("/api/flows")
def api_flow_create(payload: dict):
    """Create a new flow from JSON payload."""
    from okuro.flows import get_flow, save_flow

    flow, err = _flow_from_payload(payload)
    if err:
        return Response(content=f'{{"error":"{err}"}}', status_code=400,
                        media_type="application/json")
    if get_flow(flow.id) is not None:
        return Response(content='{"error":"id already exists"}', status_code=409,
                        media_type="application/json")
    saved = save_flow(flow)
    return saved.to_dict()


@router.put("/api/flows/{flow_id}")
def api_flow_update(flow_id: str, payload: dict):
    """Upsert a flow at a given id (creates if missing)."""
    from okuro.flows import save_flow

    flow, err = _flow_from_payload(payload, fallback_id=flow_id)
    if err:
        return Response(content=f'{{"error":"{err}"}}', status_code=400,
                        media_type="application/json")
    # Ensure the canonical id matches the path
    flow.id = flow_id
    saved = save_flow(flow)
    return saved.to_dict()


@router.delete("/api/flows/{flow_id}")
def api_flow_delete(flow_id: str):
    from okuro.flows import delete_flow

    ok = delete_flow(flow_id)
    if not ok:
        return Response(content='{"error":"not found"}', status_code=404,
                        media_type="application/json")
    return {"ok": True}


# -- okuro-flow (generic node-graph canvas) --
#
# Independent of the legacy /api/flows star-topology store above. Backed by the
# SQLite flow_designer table (migration 064). Every mutation appends to the
# change-feed; /api/flow-designer/events tails it over SSE so an open canvas
# live-updates when ANY process (web OR the stdio MCP agent) writes a flow.


@router.get("/api/flow-designer")
def api_fd_list():
    """List okuro-flow diagrams (summary shape)."""
    from okuro.flow_designer import list_flows

    return {"flows": [f.to_summary() for f in list_flows()]}


@router.get("/api/flow-designer/events")
async def api_fd_events(request: Request, since: int | None = None):
    """SSE change-feed. Streams `saved`/`deleted` events as flows mutate.

    `since` replays from that seq; omit to receive only future events. Declared
    BEFORE the /{flow_id} route so it is not shadowed as an id of "events".
    """
    from okuro.flow_designer import events_since, latest_seq

    start = since if since is not None else await asyncio.to_thread(latest_seq)

    async def gen():
        cursor = start
        yield ": connected\n\n"
        ticks = 0
        while True:
            if await request.is_disconnected():
                break
            rows = await asyncio.to_thread(events_since, cursor)
            for r in rows:
                cursor = r["seq"]
                yield f"event: {r['kind']}\ndata: {json.dumps(r)}\n\n"
            ticks += 1
            if ticks % 20 == 0:  # heartbeat ~ every 15s (20 * 0.75s)
                yield ": ping\n\n"
            await asyncio.sleep(0.75)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- folders (gallery organisation) --- declared BEFORE /{flow_id} so the
# literal "folders" segment is not captured as a flow id.
@router.get("/api/flow-designer/folders")
def api_fd_folders_list():
    """List flow folders (flat; the tree is rebuilt client-side via parent_id)."""
    from okuro.flow_designer import list_folders

    return {"folders": [f.to_dict() for f in list_folders()]}


@router.post("/api/flow-designer/folders")
def api_fd_folder_create(payload: dict):
    from okuro.flow_designer import create_folder

    try:
        folder = create_folder((payload.get("name") or "").strip(), payload.get("parent_id") or None)
    except ValueError as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=400, media_type="application/json")
    return folder.to_dict()


@router.put("/api/flow-designer/folders/{folder_id}")
def api_fd_folder_update(folder_id: str, payload: dict):
    """Rename and/or reparent a folder. ``parent_id`` present (incl. null) moves it."""
    from okuro.flow_designer import update_folder

    kwargs: dict = {}
    if "name" in payload:
        kwargs["name"] = payload.get("name")
    if "parent_id" in payload:
        kwargs["parent_id"] = payload.get("parent_id") or None
    try:
        folder = update_folder(folder_id, **kwargs)
    except ValueError as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=400, media_type="application/json")
    return folder.to_dict()


@router.delete("/api/flow-designer/folders/{folder_id}")
def api_fd_folder_delete(folder_id: str):
    """Delete a folder; its subfolders and flows reparent to its parent."""
    from okuro.flow_designer import delete_folder

    if not delete_folder(folder_id):
        return Response(content='{"error":"not found"}', status_code=404, media_type="application/json")
    return {"ok": True}


@router.post("/api/flow-designer/{flow_id}/move")
def api_fd_move(flow_id: str, payload: dict):
    """Move a flow into a folder (``folder_id`` null = ungrouped)."""
    from okuro.flow_designer import set_flow_folder

    try:
        moved = set_flow_folder(flow_id, payload.get("folder_id") or None,
                                origin=(payload.get("origin") or "").strip())
    except ValueError as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=400, media_type="application/json")
    if moved is None:
        return Response(content='{"error":"not found"}', status_code=404, media_type="application/json")
    return moved.to_summary()


@router.get("/api/flow-designer/{flow_id}")
def api_fd_get(flow_id: str):
    """Return one okuro-flow with its full graph."""
    from okuro.flow_designer import get_flow

    flow = get_flow(flow_id)
    if flow is None:
        return Response(content='{"error":"not found"}', status_code=404,
                        media_type="application/json")
    return flow.to_detail()


def _fd_save(payload: dict, *, fallback_id: str | None = None):
    """Returns ``(saved, err, conflict)``. ``conflict`` is the current doc when an
    optimistic-concurrency check fails (caller returns 409)."""
    from okuro.flow_designer import FlowConflict, save_flow

    name = (payload.get("name") or "").strip()
    if not name:
        return None, "name required", None
    graph = payload.get("graph")
    if not isinstance(graph, dict):
        graph = {
            "nodes": payload.get("nodes") or [],
            "edges": payload.get("edges") or [],
        }
        if isinstance(payload.get("viewport"), dict):
            graph["viewport"] = payload["viewport"]
        if isinstance(payload.get("settings"), dict):
            graph["settings"] = payload["settings"]
    base_rev = payload.get("base_rev")
    try:
        saved = save_flow(
            id=(payload.get("id") or fallback_id),
            name=name,
            description=(payload.get("description") or "").strip(),
            graph=graph,
            origin=(payload.get("origin") or "").strip(),
            base_rev=int(base_rev) if base_rev is not None else None,
            allow_empty=bool(payload.get("allow_empty")),
        )
    except FlowConflict as exc:
        return None, None, exc.current
    return saved, None, None


@router.put("/api/flow-designer/{flow_id}")
def api_fd_upsert(flow_id: str, payload: dict):
    """Upsert a flow at a given id — the autosave target. Returns 409 with the
    current doc when the client's ``base_rev`` is stale (someone else saved)."""
    saved, err, conflict = _fd_save(payload, fallback_id=flow_id)
    if conflict is not None:
        return Response(
            content=json.dumps({"error": "conflict", "current": conflict.to_detail()}),
            status_code=409, media_type="application/json",
        )
    if err:
        return Response(content=f'{{"error":"{err}"}}', status_code=400,
                        media_type="application/json")
    return saved.to_detail()


@router.post("/api/flow-designer")
def api_fd_create(payload: dict):
    """Create a new flow (id derived from name when absent)."""
    saved, err, _conflict = _fd_save(payload)
    if err:
        return Response(content=f'{{"error":"{err}"}}', status_code=400,
                        media_type="application/json")
    return saved.to_detail()


@router.get("/api/flow-designer/{flow_id}/history")
def api_fd_history(flow_id: str):
    """Prior-graph snapshots for a flow (newest first) — clobber recovery."""
    from okuro.flow_designer import list_history

    return {"history": list_history(flow_id)}


@router.post("/api/flow-designer/{flow_id}/restore")
def api_fd_restore(flow_id: str, payload: dict):
    """Restore a flow's graph from a history snapshot ``seq``."""
    from okuro.flow_designer import restore_history

    seq = payload.get("seq")
    if seq is None:
        return Response(content='{"error":"seq required"}', status_code=400,
                        media_type="application/json")
    try:
        saved = restore_history(flow_id, int(seq),
                                origin=(payload.get("origin") or "restore"))
    except ValueError as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=404,
                        media_type="application/json")
    return saved.to_detail()


@router.delete("/api/flow-designer/{flow_id}")
def api_fd_delete(flow_id: str, origin: str | None = None):
    from okuro.flow_designer import delete_flow

    ok = delete_flow(flow_id, origin=origin or "")
    if not ok:
        return Response(content='{"error":"not found"}', status_code=404,
                        media_type="application/json")
    return {"ok": True}


# -- drawn orchestrator workflows (/workflows) --
#
# The SECOND kind of okuro workflow: a node graph a human arranged by hand,
# compiled to an orchestrator plan with no LLM in the path. Same GraphDocStore
# mechanism as okuro-flow above, over its OWN tables (migration 113), so the two
# surfaces share code but never rows — a workflow appearing in the /flow gallery
# is not a bug that can happen.
#
# The one route okuro-flow has no equivalent for is /compile. The compiler
# RAISES on authoring mistakes on purpose (missing role, bad phase, cycle,
# dangling edge, unknown role); surfacing those to the author is the whole point
# of drawing a workflow instead of prompting for one.


@router.get("/api/workflows")
def api_wf_list():
    """List drawn workflows (summary shape)."""
    from okuro.orchestrator.workflow_store import list_workflows

    return {"workflows": [w.to_summary() for w in list_workflows()]}


@router.get("/api/workflows/events")
async def api_wf_events(request: Request, since: int | None = None):
    """SSE change-feed. Streams `saved`/`deleted` events as workflows mutate.

    `since` replays from that seq; omit to receive only future events. Declared
    BEFORE the /{workflow_id} route so it is not shadowed as an id of "events".
    """
    from okuro.orchestrator.workflow_store import events_since, latest_seq

    start = since if since is not None else await asyncio.to_thread(latest_seq)

    async def gen():
        cursor = start
        yield ": connected\n\n"
        ticks = 0
        while True:
            if await request.is_disconnected():
                break
            rows = await asyncio.to_thread(events_since, cursor)
            for r in rows:
                cursor = r["seq"]
                yield f"event: {r['kind']}\ndata: {json.dumps(r)}\n\n"
            ticks += 1
            if ticks % 20 == 0:  # heartbeat ~ every 15s (20 * 0.75s)
                yield ": ping\n\n"
            await asyncio.sleep(0.75)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- folders (gallery organisation) --- declared BEFORE /{workflow_id} so the
# literal "folders" segment is not captured as a workflow id.
@router.get("/api/workflows/folders")
def api_wf_folders_list():
    """List workflow folders (flat; the tree is rebuilt client-side via parent_id)."""
    from okuro.orchestrator.workflow_store import list_folders

    return {"folders": [f.to_dict() for f in list_folders()]}


@router.post("/api/workflows/folders")
def api_wf_folder_create(payload: dict):
    from okuro.orchestrator.workflow_store import create_folder

    try:
        folder = create_folder((payload.get("name") or "").strip(), payload.get("parent_id") or None)
    except ValueError as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=400, media_type="application/json")
    return folder.to_dict()


@router.put("/api/workflows/folders/{folder_id}")
def api_wf_folder_update(folder_id: str, payload: dict):
    """Rename and/or reparent a folder. ``parent_id`` present (incl. null) moves it."""
    from okuro.orchestrator.workflow_store import update_folder

    kwargs: dict = {}
    if "name" in payload:
        kwargs["name"] = payload.get("name")
    if "parent_id" in payload:
        kwargs["parent_id"] = payload.get("parent_id") or None
    try:
        folder = update_folder(folder_id, **kwargs)
    except ValueError as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=400, media_type="application/json")
    return folder.to_dict()


@router.delete("/api/workflows/folders/{folder_id}")
def api_wf_folder_delete(folder_id: str):
    """Delete a folder; its subfolders and workflows reparent to its parent."""
    from okuro.orchestrator.workflow_store import delete_folder

    if not delete_folder(folder_id):
        return Response(content='{"error":"not found"}', status_code=404, media_type="application/json")
    return {"ok": True}


@router.post("/api/workflows/{workflow_id}/move")
def api_wf_move(workflow_id: str, payload: dict):
    """Move a workflow into a folder (``folder_id`` null = ungrouped)."""
    from okuro.orchestrator.workflow_store import set_workflow_folder

    try:
        moved = set_workflow_folder(workflow_id, payload.get("folder_id") or None,
                                    origin=(payload.get("origin") or "").strip())
    except ValueError as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=400, media_type="application/json")
    if moved is None:
        return Response(content='{"error":"not found"}', status_code=404, media_type="application/json")
    return moved.to_summary()


@router.post("/api/workflows/{workflow_id}/compile")
def api_wf_compile(workflow_id: str, payload: dict | None = None):
    """Compile a drawn workflow into an orchestrator plan — the authoring check.

    Returns the plan on success, or 400 carrying the ``FlowCompileError``
    message. Compilation runs in TWO steps because they catch different classes
    of mistake: ``compile_flow`` catches graph-shaped ones (no role, bad phase,
    cycle, dangling edge) and ``to_phases`` catches plan-shaped ones (a role that
    is not in the catalogue). Only running the first would let a workflow that
    can never dispatch look green in the editor.

    ``params`` / ``phases`` / ``fanout`` mirror ``compile_flow``'s arguments, so
    an author can preview a fan-out expansion without running anything.
    """
    from okuro.orchestrator.flow_compiler import FlowCompileError, to_phases
    from okuro.orchestrator.workflow_store import compile_workflow

    body = payload or {}
    kwargs: dict = {}
    if isinstance(body.get("params"), dict):
        kwargs["params"] = body["params"]
    if isinstance(body.get("phases"), list):
        kwargs["phases"] = body["phases"]
    if isinstance(body.get("fanout"), dict):
        kwargs["fanout"] = body["fanout"]

    try:
        plan = compile_workflow(workflow_id, **kwargs)
        to_phases(plan)  # strict — an unknown role is an authoring mistake
    except FlowCompileError as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=400,
                        media_type="application/json")
    except ValueError as exc:
        # compile_workflow raises plain ValueError only for a missing workflow.
        return Response(content=json.dumps({"error": str(exc)}), status_code=404,
                        media_type="application/json")
    return {
        "plan": plan,
        "subtask_count": sum(len(p["subtasks"]) for p in plan["phases"]),
    }


@router.get("/api/workflows/{workflow_id}")
def api_wf_get(workflow_id: str):
    """Return one drawn workflow with its full graph."""
    from okuro.orchestrator.workflow_store import get_workflow

    wf = get_workflow(workflow_id)
    if wf is None:
        return Response(content='{"error":"not found"}', status_code=404,
                        media_type="application/json")
    return wf.to_detail()


def _wf_save(payload: dict, *, fallback_id: str | None = None):
    """Returns ``(saved, err, conflict)``. ``conflict`` is the current doc when an
    optimistic-concurrency check fails (caller returns 409)."""
    from okuro.orchestrator.workflow_store import WorkflowConflict, save_workflow

    name = (payload.get("name") or "").strip()
    if not name:
        return None, "name required", None
    graph = payload.get("graph")
    if not isinstance(graph, dict):
        graph = {
            "nodes": payload.get("nodes") or [],
            "edges": payload.get("edges") or [],
        }
        if isinstance(payload.get("viewport"), dict):
            graph["viewport"] = payload["viewport"]
        if isinstance(payload.get("settings"), dict):
            graph["settings"] = payload["settings"]
    base_rev = payload.get("base_rev")
    try:
        saved = save_workflow(
            id=(payload.get("id") or fallback_id),
            name=name,
            description=(payload.get("description") or "").strip(),
            graph=graph,
            origin=(payload.get("origin") or "").strip(),
            base_rev=int(base_rev) if base_rev is not None else None,
            allow_empty=bool(payload.get("allow_empty")),
        )
    except WorkflowConflict as exc:
        return None, None, exc.current
    return saved, None, None


@router.put("/api/workflows/{workflow_id}")
def api_wf_upsert(workflow_id: str, payload: dict):
    """Upsert a workflow at a given id — the autosave target. Returns 409 with the
    current doc when the client's ``base_rev`` is stale (someone else saved)."""
    saved, err, conflict = _wf_save(payload, fallback_id=workflow_id)
    if conflict is not None:
        return Response(
            content=json.dumps({"error": "conflict", "current": conflict.to_detail()}),
            status_code=409, media_type="application/json",
        )
    if err:
        return Response(content=f'{{"error":"{err}"}}', status_code=400,
                        media_type="application/json")
    return saved.to_detail()


@router.post("/api/workflows")
def api_wf_create(payload: dict):
    """Create a new workflow (id derived from name when absent)."""
    saved, err, _conflict = _wf_save(payload)
    if err:
        return Response(content=f'{{"error":"{err}"}}', status_code=400,
                        media_type="application/json")
    return saved.to_detail()


@router.get("/api/workflows/{workflow_id}/history")
def api_wf_history(workflow_id: str):
    """Prior-graph snapshots for a workflow (newest first) — clobber recovery."""
    from okuro.orchestrator.workflow_store import list_history

    return {"history": list_history(workflow_id)}


@router.post("/api/workflows/{workflow_id}/restore")
def api_wf_restore(workflow_id: str, payload: dict):
    """Restore a workflow's graph from a history snapshot ``seq``."""
    from okuro.orchestrator.workflow_store import restore_history

    seq = payload.get("seq")
    if seq is None:
        return Response(content='{"error":"seq required"}', status_code=400,
                        media_type="application/json")
    try:
        saved = restore_history(workflow_id, int(seq),
                                origin=(payload.get("origin") or "restore"))
    except ValueError as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=404,
                        media_type="application/json")
    return saved.to_detail()


@router.delete("/api/workflows/{workflow_id}")
def api_wf_delete(workflow_id: str, origin: str | None = None):
    from okuro.orchestrator.workflow_store import delete_workflow

    ok = delete_workflow(workflow_id, origin=origin or "")
    if not ok:
        return Response(content='{"error":"not found"}', status_code=404,
                        media_type="application/json")
    return {"ok": True}


def _flow_chat_prompt(prompt: str, graph_json: str, context: str) -> tuple[str, str]:
    """Build the (system, user) prompt for okuro·flow's drawing agent. The agent
    turns a natural-language request into a ReactFlow node-graph, grounded in
    okuro memory, editing the supplied current graph in place when present."""
    from okuro.flow_designer.mcp_tools import SCHEMA_DOC

    system = (
        "You are okuro·flow's drawing agent. You turn a user's request into a "
        "node-graph diagram that renders live on a ReactFlow canvas. You have "
        "okuro's memory as grounding context.\n\n"
        "GRAPH CONTRACT:\n" + SCHEMA_DOC + "\n\n"
        "RULES:\n"
        "- If a non-empty current graph is supplied, EDIT it: keep stable ids for "
        "nodes you keep, add/modify/remove only what the request implies. Replace "
        "wholesale only when the request is unrelated to what's there.\n"
        "- Lay nodes left-to-right (x ~260/column, y ~160/row) so it reads cleanly.\n"
        "- Choose data.cat per node meaning "
        "(input/process/decision/output/note/title/mdnote).\n"
        "- Give every wired node a data.ports list and connect OUT→IN with edges "
        "(unique edge ids, valid sourceHandle/targetHandle port ids).\n"
        "- Output ONLY one JSON object, no prose, no code fences."
    )
    user = (
        f"Request:\n{prompt}\n\n"
        f"Current graph (JSON — may be empty):\n{graph_json or '{\"nodes\":[],\"edges\":[]}'}\n\n"
        f"okuro context from memory (use it, do not repeat it):\n{context}\n\n"
        'Return ONLY this JSON shape:\n'
        '{"name":"short title","description":"one line","nodes":[...],"edges":[...]}'
    )
    return system, user


def _parse_flow_graph(output: str) -> dict:
    """Extract the {name?, description?, nodes, edges} object from LLM output.
    Lenient: the frontend (applyGraph/normEdge) and storage already coerce
    node/edge shape, so we only enforce that nodes is a non-empty list.

    Ports are the one exception to that leniency — they are normalised HERE
    rather than left to the client, because this route SAVES the graph. A model
    that answers in the legacy ins/outs shape would otherwise persist it, and
    the whole point of the ports[] move is that nothing writes the old shape."""
    import json
    import re

    from okuro.flow_designer.ports import norm_node

    match = re.search(r"\{.*\}", output or "", re.S)
    if not match:
        raise RuntimeError("no JSON object in LLM output")
    data = json.loads(match.group(0))
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise RuntimeError("LLM returned no nodes")
    edges = data.get("edges")
    if not isinstance(edges, list):
        edges = []
    return {
        "name": str(data.get("name") or "").strip(),
        "description": str(data.get("description") or "").strip(),
        "nodes": [norm_node(n) for n in nodes if isinstance(n, dict)],
        "edges": edges,
    }


def _flow_stream_prompt(prompt: str, context: str) -> tuple[str, str]:
    """(system, user) for the STREAMING flow agent — emits JSONL so each node /
    edge renders on the canvas as it is written."""
    from okuro.flow_designer.mcp_tools import SCHEMA_DOC

    system = (
        "You are okuro·flow's drawing agent. Turn the request into a node-graph "
        "that renders live on a ReactFlow canvas, grounded in okuro memory.\n\n"
        "GRAPH CONTRACT:\n" + SCHEMA_DOC + "\n\n"
        "OUTPUT FORMAT — STREAMING JSONL, built INCREMENTALLY so the graph WIRES "
        "ITSELF as it draws. Emit ONE compact single-line JSON object per line "
        "(no prose, no code fences, no wrapping object, no pretty-printing):\n"
        '  1. first line: {"meta":{"name":"short title","description":"one line"}}\n'
        "  2. then build the graph STEP BY STEP in reading order. For each step "
        "emit the node, then IMMEDIATELY the edge(s) that connect it back to a "
        "node already on the canvas:\n"
        '       {"node":{<Node per contract>}}\n'
        '       {"edge":{<Edge wiring this node to an ALREADY-emitted node>}}\n'
        "Do NOT dump all nodes first and all edges last — interleave them so "
        "connections form live. Never emit an edge before BOTH its nodes exist "
        "(a merge/decision node emits its several edges right after it).\n"
        "Choose data.cat per meaning; omit position — the server auto-lays out.\n"
        "PORTS & EDGES — follow EXACTLY so every edge connects:\n"
        "- Give EVERY flow node (cat input/process/decision/output) these ports, "
        "as ONE ordered list on data: "
        '"ports":[{"id":"in","label":"","t":"flow","dir":"in"},'
        '{"id":"out","label":"","t":"flow","dir":"out"}]. '
        "Omit each port's side — it resolves from dir and the node's orient.\n"
        "(title / note / mdnote are standalone annotations — no ports, no edges.)\n"
        '- Every edge MUST use "sourceHandle":"out","targetHandle":"in",'
        '"type":"labeled".\n'
        "- CONNECT THE WHOLE GRAPH: every node after the first gets at least one "
        "incoming edge; decision branches add one edge per branch. Never leave a "
        "flow node unconnected."
    )
    user = (
        f"Request:\n{prompt}\n\n"
        f"okuro context from memory (use it, do not repeat it):\n{context}\n\n"
        "Draw it now as JSONL: the meta line, then for each step a node line "
        "followed immediately by its connecting edge line(s). Nothing else."
    )
    return system, user


_FLOW_POOL = None


def _in_mp_child() -> bool:
    """True inside any multiprocessing worker / forkserver child.

    Pool creation and warm must NEVER run there: a child that imports this
    module would build its OWN pool → its OWN forkserver → another child that
    imports this module → … an unbounded cascade. That cascade was the source
    of the pymp-* tmp-dir spam (one dir per forkserver) and the leaking
    "Okuro" worker chain. ``parent_process()`` is None ONLY in the real server
    process, so this is the reliable gate."""
    import multiprocessing as _mp

    try:
        return _mp.parent_process() is not None
    except Exception:
        return False


def _get_flow_pool():
    """Process pool for the flow draw — claude runs in a worker process (own
    GIL) so the busy orchestrator loop can't starve the reader."""
    global _FLOW_POOL
    if _in_mp_child():
        raise RuntimeError("flow pool must not be created inside a worker process")
    if _FLOW_POOL is None:
        import multiprocessing as _mp
        from concurrent.futures import ProcessPoolExecutor

        try:
            ctx = _mp.get_context("forkserver")
        except ValueError:
            ctx = _mp.get_context("spawn")
        _FLOW_POOL = ProcessPoolExecutor(max_workers=2, mp_context=ctx)
    return _FLOW_POOL


def _flow_pool_noop() -> bool:
    return True


def _cleanup_stale_pymp() -> None:
    """Remove leaked multiprocessing forkserver temp dirs (pymp-*) that no live
    process holds. ProcessPools (chat + flow draw) create one per forkserver;
    on a non-graceful restart they leak into $TMPDIR. In-use-aware — a running
    pool's dir is never touched."""
    import glob
    import os
    import re
    import shutil
    import tempfile

    try:
        inuse: set[str] = set()
        for fd_dir in glob.glob("/proc/*/fd"):
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        tgt = os.readlink(os.path.join(fd_dir, fd))
                    except OSError:
                        continue
                    m = re.search(r"/(pymp-[A-Za-z0-9_]+)", tgt)
                    if m:
                        inuse.add(m.group(1))
            except OSError:
                continue
        for d in glob.glob(os.path.join(tempfile.gettempdir(), "pymp-*")):
            if os.path.basename(d) not in inuse:
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def _warm_flow_pool() -> None:
    """Pre-spawn the forkserver + a worker so the FIRST draw doesn't eat the
    ~55s cold-start, and sweep leaked pymp-* dirs. Background + best-effort."""
    if _in_mp_child():
        return  # a worker must never warm a pool — that is the cascade
    import threading

    def _w() -> None:
        try:
            import time as _t

            _t.sleep(3)  # let module import finish before forkserver forks
            _cleanup_stale_pymp()
            _get_flow_pool().submit(_flow_pool_noop).result(timeout=60)
        except Exception:
            pass

    try:
        threading.Thread(target=_w, name="flow-pool-warm", daemon=True).start()
    except Exception:
        pass


async def _flow_draw_events(prompt: str, request=None):
    """Shared generator for the live flow draw — yields ``{type: …}`` dicts
    (meta / node / edge / activity / done / error), each tagged with ``t`` =
    seconds elapsed (debug marker).

    claude runs in a ProcessPool WORKER (draw_worker.flow_draw_worker) writing
    JSONL events to a sink file; this generator tails the file. The worker's
    separate GIL keeps TTFT ~2-3s — reading it inline on the orchestrator (loop
    or a thread) made it 38-105s because the busy loop hogs the GIL. Stops +
    the worker self-terminates on client disconnect or a deadline."""
    import asyncio
    import json
    import os
    import tempfile
    import time

    from okuro.flow_designer import save_flow
    from okuro.flow_designer.draw_worker import flow_draw_worker

    t0 = time.monotonic()

    def el() -> float:
        return round(time.monotonic() - t0, 1)

    yield {"type": "activity", "msg": "pulling related okuro memory…", "t": el()}
    try:
        from okuro.sense.memory import read_memory

        context = (await asyncio.to_thread(lambda: read_memory(query=prompt, limit=6)) or "")[:2500] or "(none)"
    except Exception:
        context = "(none)"

    system, user = _flow_stream_prompt(prompt, context)

    fd, mcp = tempfile.mkstemp(suffix=".flow-mcp.json")
    os.write(fd, b'{"mcpServers":{}}')
    os.close(fd)
    try:
        from okuro.system import cli_probe

        binary = cli_probe.detect("claude").path
    except Exception:
        binary = "claude"
    # Tooling-bridge profile: this is a stateless draw tool-function, so strip
    # everything the agent bootstrap adds — MCP servers (empty --mcp-config +
    # --strict-mcp-config), CLAUDE.md / user-memory auto-discovery
    # (--setting-sources ""), per-machine env/git/memory blocks
    # (--exclude-dynamic-system-prompt-sections), and the Chrome probe.
    #
    # Model stays SONNET, not haiku: this streams a STRICT JSONL contract (one
    # {"node":…}/{"edge":…} per line, nothing else). haiku ignores it
    # intermittently — wraps the JSON in a ```jsonl fence or replaces it with
    # prose ("Done. The flow shows…"), so the worker parses zero nodes and the
    # canvas stays empty. Sonnet obeys it. (Verified 2026-07-06.)
    cmd = [
        binary, "-p", "--verbose", "--include-partial-messages",
        "--input-format", "stream-json", "--output-format", "stream-json",
        "--strict-mcp-config", "--mcp-config", mcp,
        "--setting-sources", "", "--exclude-dynamic-system-prompt-sections",
        "--no-chrome",
        "--permission-mode", "bypassPermissions", "--model", "sonnet",
        "--system-prompt", system,
    ]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    # Extended thinking made the model plan the WHOLE graph before emitting a
    # single line — ~31s of dead air before the first node (measured: 36s→5s
    # to first node with thinking off). A flow graph is structured output, not
    # a reasoning task, so disable it: the meta + first node now stream in ~4-5s.
    env["MAX_THINKING_TOKENS"] = "0"
    envelope = json.dumps(
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": user}]}}
    )
    sink = tempfile.mktemp(suffix=".flow-events.jsonl")
    open(sink, "w").close()

    yield {"type": "activity", "msg": "drawing with okuro's agent…", "t": el()}

    loop = asyncio.get_event_loop()
    fut = loop.run_in_executor(_get_flow_pool(), flow_draw_worker, cmd, env, envelope, mcp, sink)

    nodes: list = []
    edges: list = []
    meta = {"name": "", "description": ""}
    errored = False
    done = False
    pos = 0
    deadline = time.monotonic() + 180.0
    try:
        while not done:
            if request is not None:
                try:
                    if await request.is_disconnected():
                        break
                except Exception:
                    pass
            if time.monotonic() > deadline:
                yield {"type": "error", "error": "draw timed out"}
                errored = True
                break
            await asyncio.sleep(0.2)
            try:
                with open(sink) as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except OSError:
                chunk = ""
            for raw in chunk.splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                k = item.get("k")
                if k == "meta":
                    meta["name"] = str(item["v"].get("name") or "").strip()
                    meta["description"] = str(item["v"].get("description") or "").strip()
                    yield {"type": "meta", **meta, "t": el()}
                elif k == "node":
                    nodes.append(item["v"])
                    yield {"type": "node", "node": item["v"], "t": el()}
                elif k == "edge":
                    edges.append(item["v"])
                    yield {"type": "edge", "edge": item["v"], "t": el()}
                elif k == "err":
                    yield {"type": "error", "error": f"draw failed: {item.get('v')}"}
                    errored = True
                    done = True
                    break
                elif k == "done":
                    done = True
                    break
            if fut.done() and not chunk:
                done = True
    finally:
        try:
            fut.cancel()
        except Exception:
            pass
        try:
            os.remove(sink)
        except OSError:
            pass

    if errored:
        return
    if not nodes:
        yield {"type": "error", "error": "agent produced no nodes"}
        return
    name = meta["name"] or (prompt[:48] + ("…" if len(prompt) > 48 else ""))
    yield {"type": "activity", "msg": "saving…", "t": el()}
    try:
        saved = await asyncio.to_thread(
            save_flow, name=name, description=meta["description"],
            graph={"nodes": nodes, "edges": edges}, origin="agent-chat",
        )
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "error": f"save failed: {exc}"}
        return
    yield {
        "type": "done", "flow": saved.to_summary(),
        "url": f"/flow?id={saved.id}", "node_count": len(nodes), "edge_count": len(edges), "t": el(),
    }


@router.post("/api/flow-designer/draw-stream")
async def api_fd_draw_stream(payload: dict, request: Request):
    """NDJSON variant of the live flow draw (Chromium streams it incrementally)."""
    import json

    from fastapi import HTTPException
    from fastapi.responses import StreamingResponse

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt required")

    async def gen():
        async for ev in _flow_draw_events(prompt, request):
            yield json.dumps(ev) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.get("/api/flow-designer/draw/sse")
async def api_fd_draw_sse(request: Request):
    """SSE variant — WebKitGTK (pywebview) delivers SSE incrementally (unlike
    fetch ReadableStream, which it buffers), so nodes appear one-by-one. Prompt
    + bearer arrive via the query string (EventSource can't POST or set headers;
    the bearer ``token`` is checked by the global middleware)."""
    import json

    from fastapi import HTTPException
    from fastapi.responses import StreamingResponse

    prompt = (request.query_params.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt required")

    async def gen():
        # 2 KB comment padding up front — WebKit (and some proxies) buffer an
        # SSE stream until a few KB arrive before delivering events; this flushes
        # that buffer so the first activity/node shows immediately.
        yield ":" + (" " * 2048) + "\n\n"
        async for ev in _flow_draw_events(prompt, request):
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/flow-designer/chat")
async def api_fd_chat(payload: dict):
    """Chat-to-draw: turn a prompt into a flow graph, STREAMING the live inference
    activity (NDJSON), then persist it via the agent-write path so any open canvas
    live-updates over SSE. `done` carries the saved flow summary + url.

    Body: {prompt, id?, name?, graph?} — `id`/`graph` scope an edit to the open
    flow; omit for a fresh draw.
    """
    import asyncio
    import json
    import os
    import tempfile
    from pathlib import Path

    from fastapi import HTTPException
    from fastapi.responses import StreamingResponse

    prompt = (payload.get("prompt") or "").strip()
    flow_id = (payload.get("id") or "").strip() or None
    cur_graph = payload.get("graph") if isinstance(payload.get("graph"), dict) else None
    hint_name = (payload.get("name") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt required")

    async def gen():
        from okuro.bridge.invoke import invoke
        from okuro.flow_designer import save_flow
        from okuro.sense.memory import read_memory

        def line(obj: dict) -> str:
            return json.dumps(obj) + "\n"

        yield line({"type": "activity", "msg": "pulling related okuro memory…"})
        try:
            context = (read_memory(query=prompt, limit=6) or "")[:2500] or "(none on file)"
        except Exception:
            context = "(none on file)"

        graph_json = json.dumps(cur_graph)[:8000] if cur_graph else ""
        system, user = _flow_chat_prompt(prompt, graph_json, context)

        sink = Path(tempfile.mktemp(suffix=".flowchat-activity.jsonl"))
        sink.write_text("")
        holder: dict = {}

        def _run():
            holder["res"] = invoke(prompt=user, system_prompt=system, timeout=180, activity_sink=sink)

        loop = asyncio.get_event_loop()
        fut = loop.run_in_executor(None, _run)
        yield line({"type": "activity", "msg": "drawing with okuro's agent…"})

        pos = 0
        while not fut.done():
            await asyncio.sleep(0.4)
            try:
                data = sink.read_text()
            except OSError:
                data = ""
            chunk, pos = data[pos:], len(data)
            for raw in chunk.splitlines():
                if not raw.strip():
                    continue
                try:
                    msg = _humanize_activity(json.loads(raw))
                except Exception:
                    msg = None
                if msg:
                    yield line({"type": "activity", "msg": msg})
        await fut

        try:
            os.remove(sink)
        except OSError:
            pass

        res = holder.get("res") or {}
        if not res.get("success"):
            yield line({"type": "error", "error": res.get("error") or "LLM invoke failed"})
            return
        try:
            parsed = _parse_flow_graph(res.get("output") or "")
        except Exception as exc:  # noqa: BLE001
            yield line({"type": "error", "error": f"parse failed: {exc}"})
            return

        name = parsed["name"] or hint_name or (prompt[:48] + ("…" if len(prompt) > 48 else ""))
        yield line({"type": "activity", "msg": "saving to the canvas…"})
        try:
            saved = await asyncio.to_thread(
                save_flow,
                id=flow_id,
                name=name,
                description=parsed["description"],
                graph={"nodes": parsed["nodes"], "edges": parsed["edges"]},
                origin="agent-chat",
            )
        except Exception as exc:  # noqa: BLE001
            yield line({"type": "error", "error": f"save failed: {exc}"})
            return

        yield line({
            "type": "done",
            "flow": saved.to_summary(),
            "url": f"/flow?id={saved.id}",
            "node_count": len(parsed["nodes"]),
            "edge_count": len(parsed["edges"]),
            "provider": res.get("provider"),
            "model": res.get("model"),
        })

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# -- Stack (tech-stack registry) --

@router.get("/api/stack/layers")
def api_stack_layers(category: str | None = None):
    """List all stack layers, optionally filtered by category."""
    from okuro.stack import list_layers
    return {"layers": list_layers(category=category)}


@router.get("/api/stack/entries")
def api_stack_entries(
    layer: str | None = None,
    status: str | None = None,
    profile: str | None = None,
):
    """List stack entries, optionally filtered."""
    from okuro.stack import list_entries
    return {"entries": list_entries(layer=layer, status=status, profile=profile)}


@router.get("/api/stack/entries/{entry_id:path}")
def api_stack_entry_get(entry_id: str):
    """Get a single entry by ID (supports dotted IDs like fe.framework.react-19)."""
    from okuro.stack import get_entry
    entry = get_entry(entry_id)
    if not entry:
        return Response(content='{"error":"not found"}', status_code=404,
                        media_type="application/json")
    return entry


@router.post("/api/stack/entries")
def api_stack_entry_create(payload: dict):
    """Create or update a stack entry."""
    from okuro.stack import upsert_entry
    eid = (payload.get("id") or "").strip()
    layer = (payload.get("layer") or "").strip()
    name = (payload.get("name") or "").strip()
    if not eid or not layer or not name:
        return Response(content='{"error":"id, layer, name required"}',
                        status_code=400, media_type="application/json")
    return upsert_entry(
        eid,
        layer=layer,
        name=name,
        version=payload.get("version"),
        status=payload.get("status", "trial"),
        rationale=payload.get("rationale", ""),
        use_when=payload.get("use_when"),
        avoid_when=payload.get("avoid_when"),
        depends_on=payload.get("depends_on"),
        docs_url=payload.get("docs_url"),
        owner=payload.get("owner"),
        replaces=payload.get("replaces"),
    )


@router.post("/api/stack/entries/{entry_id:path}/status")
def api_stack_entry_status(entry_id: str, payload: dict):
    """Transition an entry's lifecycle status."""
    from okuro.stack import set_entry_status
    new_status = payload.get("status")
    if not new_status:
        return Response(content='{"error":"status required"}', status_code=400,
                        media_type="application/json")
    result = set_entry_status(entry_id, new_status, actor=payload.get("actor"))
    if not result.get("ok"):
        return Response(
            content=json.dumps(result),
            status_code=400,
            media_type="application/json",
        )
    return result


@router.get("/api/stack/match")
def api_stack_match(need: str, limit: int = 5):
    """Keyword-match entries against a need description."""
    from okuro.stack import match_entries
    return {"matches": match_entries(need, limit=limit)}


@router.get("/api/stack/profiles")
def api_stack_profiles(status: str | None = None, scope: str | None = None):
    """List all profiles. Filter by status or scope (frontend|backend|
    fullstack|agent|other)."""
    from okuro.stack import list_profiles
    return {"profiles": list_profiles(status=status, scope=scope)}


@router.get("/api/stack/profiles/{name}")
def api_stack_profile_get(name: str):
    """Get a resolved profile (entries grouped by category, with deps)."""
    from okuro.stack import resolve_profile
    resolved = resolve_profile(name)
    if not resolved:
        return Response(content='{"error":"not found"}', status_code=404,
                        media_type="application/json")
    return resolved


@router.post("/api/stack/profiles")
def api_stack_profile_create(payload: dict):
    """Create or update a profile."""
    from okuro.stack import upsert_profile
    name = (payload.get("name") or "").strip()
    label = (payload.get("label") or "").strip() or name
    if not name:
        return Response(content='{"error":"name required"}', status_code=400,
                        media_type="application/json")
    return upsert_profile(
        name,
        label=label,
        description=payload.get("description", ""),
        status=payload.get("status", "active"),
        entries=payload.get("entries"),
    )


@router.put("/api/stack/profiles/{name}")
def api_stack_profile_update(name: str, payload: dict):
    """Upsert a profile at a specific name (path takes precedence)."""
    from okuro.stack import upsert_profile
    label = (payload.get("label") or "").strip() or name
    return upsert_profile(
        name,
        label=label,
        description=payload.get("description", ""),
        status=payload.get("status", "active"),
        entries=payload.get("entries"),
    )


@router.post("/api/stack/profiles/{name}/assign")
def api_stack_profile_assign(name: str, payload: dict):
    """Bind a project slug to this profile."""
    from okuro.stack import assign_project_profile
    project_slug = (payload.get("project_slug") or "").strip()
    if not project_slug:
        return Response(content='{"error":"project_slug required"}',
                        status_code=400, media_type="application/json")
    return assign_project_profile(project_slug, name)


@router.get("/api/projects")
def api_projects(
    touched_within_days: float | None = None,
    include_repo: bool = True,
    active_only: bool = True,
    since: str | None = None,
):
    """One compact row per project — the read a projects list is built on.

    Same function the `projects_overview` MCP tool calls, so the SPA and an
    agent session cannot disagree about where a project stands. Costs are flat
    in project count; `include_repo` defaults True (measured 80ms over 97
    projects) because a brain-only verdict is the degraded answer.

    `since` adds a `delta` block to rows that moved after that timestamp — the
    RESUME BOARD's Zone 1. Pass the `previous_seen_at` from
    `GET /api/visits/projects`, never `last_seen_at`: the latter is the visit
    you are currently making, so diffing against it always yields nothing.
    """
    from okuro.sense.overview import projects_overview

    return projects_overview(
        include_repo=include_repo,
        active_only=active_only,
        touched_within_days=touched_within_days,
        since=since,
    )


@router.get("/api/visits/{surface}")
def api_visit_get(surface: str):
    """Read a surface's visit record. Does NOT record a visit."""
    from okuro.sense.visits import get_visit

    return get_visit(surface)


@router.post("/api/visits/{surface}")
def api_visit_mark(surface: str):
    """Record that the user opened `surface` now.

    Shifts the old `last_seen_at` into `previous_seen_at` and returns the
    record after the shift, so the caller gets the reference point to diff
    against in the same round-trip.
    """
    from okuro.sense.visits import mark_visit

    return mark_visit(surface)


@router.get("/api/stack/projects/{project_slug}/profile")
def api_stack_project_profile(project_slug: str):
    """Get the resolved profile currently bound to a project."""
    from okuro.stack import active_profile_for
    resolved = active_profile_for(project_slug)
    if not resolved:
        return Response(content='null', status_code=200,
                        media_type="application/json")
    return resolved


@router.get("/api/stack/validate")
def api_stack_validate():
    """Whole-registry validation."""
    from okuro.stack import validate_registry
    return validate_registry()


@router.get("/api/stack/profiles/{name}/lint")
def api_stack_profile_lint(name: str):
    """Lint a profile for banned/cardinality issues."""
    from okuro.stack import lint_profile
    return lint_profile(name)


@router.get("/api/stack/proposals")
def api_stack_proposals(outcome: str | None = None, limit: int = 50):
    """List proposals (audit log + pending decisions)."""
    from okuro.stack import list_proposals
    return {"proposals": list_proposals(outcome=outcome, limit=limit)}


@router.post("/api/stack/proposals")
def api_stack_propose(payload: dict):
    """File a proposal."""
    from okuro.stack import propose
    entry_id = (payload.get("entry_id") or "").strip()
    rationale = (payload.get("rationale") or "").strip()
    if not entry_id or not rationale:
        return Response(content='{"error":"entry_id and rationale required"}',
                        status_code=400, media_type="application/json")
    return propose(
        entry_id,
        kind=payload.get("kind", "new"),
        proposed_by=payload.get("proposed_by"),
        rationale=rationale,
        payload=payload.get("payload") or {},
    )


@router.post("/api/stack/proposals/{proposal_id}/decide")
def api_stack_proposal_decide(proposal_id: str, payload: dict):
    """Decide a proposal (accept/reject)."""
    from okuro.stack import decide_proposal
    outcome = payload.get("outcome")
    if outcome not in ("accepted", "rejected"):
        return Response(content='{"error":"outcome must be accepted or rejected"}',
                        status_code=400, media_type="application/json")
    result = decide_proposal(proposal_id, outcome,
                             decided_by=payload.get("decided_by"))
    if not result.get("ok"):
        return Response(content=json.dumps(result), status_code=400,
                        media_type="application/json")
    return result


# -- Stack: brands + slot kinds --

@router.get("/api/stack/slot_kinds")
def api_stack_slot_kinds():
    """List brand slot kinds (the configurable plug points)."""
    from okuro.stack import list_slot_kinds
    return {"slot_kinds": list_slot_kinds()}


@router.get("/api/stack/brands")
def api_stack_brands(status: str | None = None):
    """List brands (optionally filtered by status)."""
    from okuro.stack import list_brands
    return {"brands": list_brands(status=status)}


@router.get("/api/stack/brands/{brand_id}")
def api_stack_brand_get(brand_id: str):
    """Get a brand with its raw slot assignments."""
    from okuro.stack import get_brand
    brand = get_brand(brand_id)
    if not brand:
        return Response(content='{"error":"not found"}', status_code=404,
                        media_type="application/json")
    return brand


@router.get("/api/stack/brands/{brand_id}/resolve")
def api_stack_brand_resolve(brand_id: str):
    """Resolve a brand into design tokens + fe stack + be stack + principles."""
    from okuro.stack import resolve_brand
    resolved = resolve_brand(brand_id)
    if not resolved:
        return Response(content='{"error":"not found"}', status_code=404,
                        media_type="application/json")
    return resolved


@router.post("/api/stack/brands")
def api_stack_brand_create(payload: dict):
    """Create or update a brand."""
    from okuro.stack import upsert_brand
    bid = (payload.get("id") or "").strip()
    name = (payload.get("name") or "").strip()
    if not bid or not name:
        return Response(content='{"error":"id and name required"}',
                        status_code=400, media_type="application/json")
    return upsert_brand(
        bid,
        name=name,
        description=payload.get("description", ""),
        status=payload.get("status", "active"),
        slots=payload.get("slots"),
        logo_corner=payload.get("logo_corner"),
    )


@router.put("/api/stack/brands/{brand_id}")
def api_stack_brand_update(brand_id: str, payload: dict):
    """Upsert a brand at a specific id (path takes precedence)."""
    from okuro.stack import upsert_brand
    name = (payload.get("name") or "").strip() or brand_id
    return upsert_brand(
        brand_id,
        name=name,
        description=payload.get("description", ""),
        status=payload.get("status", "active"),
        slots=payload.get("slots"),
        logo_corner=payload.get("logo_corner"),
    )


@router.delete("/api/stack/brands/{brand_id}")
def api_stack_brand_delete(brand_id: str):
    """Delete a brand (cascades slot assignments + project bindings)."""
    from okuro.stack import delete_brand
    return delete_brand(brand_id)


@router.post("/api/stack/brands/{brand_id}/assign")
def api_stack_brand_assign(brand_id: str, payload: dict):
    """Bind a project slug to this brand."""
    from okuro.stack import assign_project_brand
    project_slug = (payload.get("project_slug") or "").strip()
    if not project_slug:
        return Response(content='{"error":"project_slug required"}',
                        status_code=400, media_type="application/json")
    return assign_project_brand(project_slug, brand_id)


@router.get("/api/stack/brands/{brand_id}/lint")
def api_stack_brand_lint(brand_id: str):
    """Lint a brand — slot refs resolve, scope filters hold, required slots
    filled, underlying profiles lint clean."""
    from okuro.stack import lint_brand
    return lint_brand(brand_id)


@router.get("/api/stack/projects/{project_slug}/brand")
def api_stack_project_brand(project_slug: str):
    """Get the resolved brand bound to a project (null if none)."""
    from okuro.stack import active_brand_for
    resolved = active_brand_for(project_slug)
    if not resolved:
        return Response(content='null', status_code=200,
                        media_type="application/json")
    return resolved


# -- Assets · icons (the brand `assets` leg — icon provider) --

@router.get("/api/assets/icons/search")
def api_assets_icon_search(
    q: str,
    set: str | None = None,  # noqa: A002 — query param name
    tag: str | None = None,
    limit: int = 60,
    include_svg: bool = True,
):
    """Hybrid icon search. `set` may be a comma-separated list. Returns inline
    SVG by default so the grid renders without per-icon auth'd requests."""
    from okuro.assets.icons import service
    from okuro.assets.icons.db import LibraryError

    sets = [s for s in (set.split(",") if set else []) if s.strip()] or None
    try:
        return service.search_icons(
            q, limit=limit, tag=tag, set=sets, include_svg=include_svg
        )
    except LibraryError as e:
        return {"count": 0, "results": [], "error": str(e),
                "hint": "Import a library: okuro assets icons import <pack.zip>"}


@router.get("/api/assets/icons/browse")
def api_assets_icon_browse(
    set: str | None = None,  # noqa: A002 — query param name
    tag: str | None = None,
    favorite: bool = False,
    cursor: str | None = None,
    limit: int = 120,
    include_svg: bool = True,
):
    """Browse icons by set/tag/favorite (no search query), keyset-paginated.
    Backs the sidebar filters + infinite scroll. Returns {count, results, next_cursor}."""
    from okuro.assets.icons import service
    from okuro.assets.icons.db import LibraryError

    sets = [s for s in (set.split(",") if set else []) if s.strip()] or None
    try:
        return service.browse_icons(
            set=sets, tag=tag, favorite=(favorite or None),
            cursor=cursor, limit=limit, include_svg=include_svg,
        )
    except LibraryError as e:
        return {"count": 0, "results": [], "next_cursor": None, "error": str(e)}


@router.get("/api/assets/icons/tags")
def api_assets_icon_tags(prefix: str | None = None, limit: int = 100):
    """List tags (optionally by prefix), ordered by frequency — backs the sidebar."""
    from okuro.assets.icons import service
    from okuro.assets.icons.db import LibraryError
    try:
        return service.list_tags(prefix, limit=limit)
    except LibraryError as e:
        return {"count": 0, "tags": [], "error": str(e)}


@router.post("/api/assets/icons/containers/create")
def api_assets_container_create(payload: dict):
    """Create a container. Body: {type: set|group|pack, name, parent_set_id?}."""
    from okuro.assets.icons import edit
    from okuro.assets.icons.db import LibraryError
    try:
        return edit.create_container(payload["type"], payload["name"], payload.get("parent_set_id"))
    except (LibraryError, ValueError, KeyError) as e:
        return {"error": str(e)}


@router.post("/api/assets/icons/containers/rename")
def api_assets_container_rename(payload: dict):
    """Rename a container. Body: {type, id, name}."""
    from okuro.assets.icons import edit
    from okuro.assets.icons.db import LibraryError
    try:
        return edit.rename_container(payload["type"], payload["id"], payload["name"])
    except (LibraryError, ValueError, KeyError) as e:
        return {"error": str(e)}


@router.post("/api/assets/icons/containers/delete")
def api_assets_container_delete(payload: dict):
    """Delete a container (membership cascades; icons untouched). Body: {type, id}."""
    from okuro.assets.icons import edit
    from okuro.assets.icons.db import LibraryError
    try:
        return edit.delete_container(payload["type"], payload["id"])
    except (LibraryError, ValueError, KeyError) as e:
        return {"error": str(e)}


@router.post("/api/assets/icons/containers/move")
def api_assets_container_move(payload: dict):
    """Reparent a set. Body: {id, parent_set_id|null}."""
    from okuro.assets.icons import edit
    from okuro.assets.icons.db import LibraryError
    try:
        return edit.move_set(payload["id"], payload.get("parent_set_id"))
    except (LibraryError, ValueError, KeyError) as e:
        return {"error": str(e)}


@router.post("/api/assets/icons/attach")
def api_assets_attach(payload: dict):
    """Attach/detach icons to a container. Body: {type, container_id, icon_ids, detach?}."""
    from okuro.assets.icons import edit
    from okuro.assets.icons.db import LibraryError
    try:
        fn = edit.detach_icons if payload.get("detach") else edit.attach_icons
        return fn(payload["type"], payload["container_id"], payload.get("icon_ids") or [])
    except (LibraryError, ValueError, KeyError) as e:
        return {"error": str(e)}


@router.post("/api/assets/icons/import")
def api_assets_import(payload: dict):
    """Import SVGs. Body: {items: [{name, svg}], set_id?}. Re-embeds new icons."""
    from okuro.assets.icons import edit
    from okuro.assets.icons.db import LibraryError
    try:
        return edit.import_svgs(payload.get("items") or [], payload.get("set_id"))
    except (LibraryError, ValueError) as e:
        return {"imported": 0, "error": str(e)}


@router.post("/api/assets/icons/delete")
def api_assets_delete(payload: dict):
    """Delete icons (files + rows + FTS + embeddings). Body: {ids: [str]}."""
    from okuro.assets.icons import edit
    from okuro.assets.icons.db import LibraryError
    try:
        return edit.delete_icons(payload.get("ids") or [])
    except (LibraryError, ValueError) as e:
        return {"deleted": 0, "error": str(e)}


@router.post("/api/assets/icons/export")
def api_assets_export(payload: dict):
    """Export icons as a zip of SVGs. Body: {ids: [str]}. Returns application/zip."""
    import io
    import zipfile
    from okuro.assets.icons import service
    from okuro.assets.icons.db import LibraryError
    try:
        files = service.export_svgs(payload.get("ids") or [])
    except LibraryError as e:
        return {"error": str(e)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.writestr(f["file_name"], f["svg"])
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=okuro-icons.zip"})


@router.post("/api/assets/icons/favorite")
def api_assets_icon_favorite(payload: dict):
    """Set favorite flag. Body: {ids: [str], favorite: bool}."""
    from okuro.assets.icons import edit
    from okuro.assets.icons.db import LibraryError
    ids = payload.get("ids") or []
    favorite = bool(payload.get("favorite", True))
    try:
        return edit.set_favorite(ids, favorite)
    except LibraryError as e:
        return {"updated": 0, "error": str(e)}


@router.post("/api/assets/icons/tags")
def api_assets_icon_edit_tags(payload: dict):
    """Edit an icon's tags. Body: {icon_id, add?: [str], remove?: [str]}."""
    from okuro.assets.icons import edit
    from okuro.assets.icons.db import LibraryError
    icon_id = payload.get("icon_id")
    if not icon_id:
        return {"error": "icon_id required"}
    try:
        result = None
        if payload.get("add"):
            result = edit.add_tags(icon_id, payload["add"])
        for name in payload.get("remove", []) or []:
            result = edit.remove_tag(icon_id, name)
        return result or {"icon_id": icon_id, "tags": []}
    except LibraryError as e:
        return {"error": str(e)}


@router.get("/api/assets/icons/sets")
def api_assets_icon_sets():
    """List icon sets with counts."""
    from okuro.assets.icons import service
    from okuro.assets.icons.db import LibraryError
    try:
        return service.list_sets()
    except LibraryError as e:
        return {"sets": [], "error": str(e)}


@router.get("/api/assets/icons/stats")
def api_assets_icon_stats():
    """Icon library summary (counts, embedded, vec status)."""
    from okuro.assets.icons import service
    from okuro.assets.icons.db import LibraryError
    try:
        return service.library_stats()
    except LibraryError as e:
        return {"error": str(e)}


@router.get("/api/assets/icons/{icon_id}")
def api_assets_icon_get(icon_id: str, format: str = "svg"):  # noqa: A002
    """Fetch one icon (svg | base64 | path) by id."""
    from okuro.assets.icons import service
    from okuro.assets.icons.db import LibraryError
    try:
        return service.get_icon(icon_id, format=format)
    except LibraryError as e:
        return {"error": str(e)}


# -- Unified media bucket (all kinds: image|video|audio|icon|illustration) --
# okuro "assets" is the single filestore; icons remain their own read-only
# provider above and are unioned at the view layer. These routes serve the
# non-library media (studio generations, uploads, imports) with tagging.

@router.get("/api/assets/media")
def api_assets_media_list(
    kind: str | None = None,
    source: str | None = None,
    folder: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    limit: int = 60,
    offset: int = 0,
):
    """List media assets across kinds (newest first), with filters + tags/meta."""
    from okuro.assets import store
    try:
        store.sync_studio_dir()  # keep the studio slice authoritative on read
        store.sync_deliveries_dir()  # + generated audio (podcast/summary/brief)
    except Exception:
        pass
    items = store.list_assets(
        kind=kind, source=source, folder=folder, tag=tag, q=q,
        limit=limit, offset=offset)
    return {"count": len(items), "items": items,
            "kinds": store.kinds_summary()}


@router.get("/api/assets/media/tags")
def api_assets_media_tags(kind: str | None = None, limit: int = 200):
    """Tag vocabulary across the bucket (or one kind), by frequency."""
    from okuro.assets import store
    return {"tags": store.list_tags(kind=kind, limit=limit)}


@router.post("/api/assets/media/{asset_id}/tags")
def api_assets_media_edit_tags(asset_id: str, payload: dict):
    """Add/remove tags on a media asset (tagging across all kinds)."""
    from okuro.assets import store
    for t in payload.get("add") or []:
        store.add_tags(asset_id, [t])
    for t in payload.get("remove") or []:
        store.remove_tag(asset_id, t)
    a = store.get_asset(asset_id)
    if not a:
        return {"error": "not found"}
    return {"id": asset_id, "tags": a["tags"]}


@router.get("/api/assets/media/{asset_id}/file")
def api_assets_media_file(asset_id: str):
    """Serve a media asset's bytes by id (any kind)."""
    from okuro.assets import store
    a = store.get_asset(asset_id)
    if not a:
        return Response(status_code=404)
    p = store.resolve_file(a)
    if p is None:
        return Response(status_code=404)
    return FileResponse(str(p), media_type=a.get("mime") or "application/octet-stream")


@router.post("/api/assets/media/{asset_id}/delete")
def api_assets_media_delete(asset_id: str):
    """Delete a media asset by id. Removes the bytes only for bucket-OWNED files
    (rel_path, e.g. studio generations); a referenced-in-place file (abs_path,
    e.g. a delivery MP3) is de-indexed but its original file is left untouched."""
    from okuro.assets import store
    a = store.get_asset(asset_id)
    if not a:
        return {"error": "not found"}
    ok = store.delete_asset(asset_id, remove_bytes=bool(a.get("rel_path")))
    return {"deleted": asset_id} if ok else {"error": "delete failed"}


# -- Principle sets (referenced by a brand's `principles` slot) --

@router.get("/api/principle_sets")
def api_principle_sets(status: str | None = None):
    """List principle sets (project-level constraint bundles)."""
    from okuro.sense.principle_sets import list_principle_sets
    return {"principle_sets": list_principle_sets(status=status)}


@router.get("/api/principle_sets/{set_id}")
def api_principle_set_get(set_id: str):
    """Get a principle set with its members expanded."""
    from okuro.sense.principle_sets import get_principle_set
    result = get_principle_set(set_id)
    if not result:
        return Response(content='{"error":"not found"}', status_code=404,
                        media_type="application/json")
    return result


@router.post("/api/principle_sets")
def api_principle_set_create(payload: dict):
    """Create or update a principle set."""
    from okuro.sense.principle_sets import upsert_principle_set
    sid = (payload.get("id") or "").strip()
    name = (payload.get("name") or "").strip()
    if not sid or not name:
        return Response(content='{"error":"id and name required"}',
                        status_code=400, media_type="application/json")
    return upsert_principle_set(
        sid,
        name=name,
        description=payload.get("description", ""),
        status=payload.get("status", "active"),
        principles=payload.get("principles"),
    )


@router.put("/api/principle_sets/{set_id}")
def api_principle_set_update(set_id: str, payload: dict):
    """Upsert a principle set (path id takes precedence)."""
    from okuro.sense.principle_sets import upsert_principle_set
    name = (payload.get("name") or "").strip() or set_id
    return upsert_principle_set(
        set_id,
        name=name,
        description=payload.get("description", ""),
        status=payload.get("status", "active"),
        principles=payload.get("principles"),
    )


@router.delete("/api/principle_sets/{set_id}")
def api_principle_set_delete(set_id: str):
    """Delete a principle set."""
    from okuro.sense.principle_sets import delete_principle_set
    ok = delete_principle_set(set_id)
    return {"ok": ok, "id": set_id}


@router.get("/api/dashboard/brain")
def api_dashboard_brain():
    """Dashboard brain data — sessions, progress, thoughts, memory, tools, projects."""
    from okuro.db import get_db

    db = get_db()
    result = {}
    try:
        result["sessions"] = db.fetchall(
            "SELECT id, session_id, provider, task_hint, project, started_at, ended_at, "
            "compliance_normalized FROM sessions ORDER BY started_at DESC LIMIT 8"
        )
        result["progress"] = db.fetchall(
            "SELECT id, project, agent, status, summary, updated_at, phase "
            "FROM progress ORDER BY updated_at DESC LIMIT 5"
        )
        result["thoughts"] = db.fetchall(
            "SELECT id, content, status, project, created_at "
            "FROM thoughts WHERE status != 'dismissed' ORDER BY created_at DESC LIMIT 12"
        )
        # Apply the same min_confidence floor read_memory uses (0.3) so the
        # dashboard never surfaces memories the decay pass is about to
        # archive (≤0.05 → deleted; <0.3 → low-signal). Without this filter
        # the sidebar showed "fresh" rows that were actually rotting.
        result["memory"] = db.fetchall(
            "SELECT id, topic, content, project, confidence, source_agent, created_at "
            "FROM agent_memory WHERE confidence >= 0.3 "
            "ORDER BY created_at DESC LIMIT 8"
        )
        result["tools"] = db.fetchall(
            "SELECT id, tool, server, latency_ms, ok, called_at "
            "FROM tool_usage ORDER BY called_at DESC LIMIT 100"
        )
        result["projects"] = db.fetchall(
            "SELECT id, name, active, updated_at FROM projects "
            "WHERE active = 1 ORDER BY updated_at DESC LIMIT 12"
        )
    except Exception as e:
        result["error"] = str(e)
    finally:
        db.close()
    return result


@router.get("/api/brain")
def api_brain():
    """Full /brain page data — larger slices than /api/dashboard/brain (sidebar)."""
    from okuro.db import get_db

    db = get_db()
    result = {}
    try:
        result["sessions"] = db.fetchall(
            "SELECT id, session_id, provider, task_hint, project, started_at, ended_at, "
            "compliance_normalized FROM sessions ORDER BY started_at DESC LIMIT 200"
        )
        # History is a JSON TEXT column — decode per-row so the frontend
        # renders a timeline instead of just the current-state row.
        progress_rows = db.fetchall(
            "SELECT id, project, agent, status, summary, updated_at, "
            "       next_steps, files_touched, history, phase, session_id "
            "FROM progress ORDER BY updated_at DESC LIMIT 200"
        )
        import json as _json
        for r in progress_rows:
            for key in ("history", "files_touched"):
                raw = r.get(key)
                if isinstance(raw, str):
                    try:
                        r[key] = _json.loads(raw) if raw else []
                    except (ValueError, TypeError):
                        r[key] = []
                elif raw is None:
                    r[key] = []
        result["progress"] = progress_rows
        result["thoughts"] = db.fetchall(
            "SELECT id, content, status, project, created_at "
            "FROM thoughts WHERE status != 'dismissed' ORDER BY created_at DESC LIMIT 200"
        )
        # Same confidence floor as the sidebar (/api/dashboard/brain) so the
        # /brain page Memory tab matches the agent-facing surface — without
        # this, agents and humans saw different sets of "current" memories.
        result["memory"] = db.fetchall(
            "SELECT id, topic, content, project, confidence, source_agent, created_at "
            "FROM agent_memory WHERE confidence >= 0.3 "
            "ORDER BY created_at DESC LIMIT 200"
        )
    except Exception as e:
        result["error"] = str(e)
    finally:
        db.close()
    return result


@router.post("/api/dashboard/thought/{thought_id}/status")
def api_dashboard_thought_status(thought_id: str, payload: dict):
    """Update a thought's status (open/in_progress/resolved/dismissed)."""
    status = payload.get("status")
    if status not in {"open", "in_progress", "resolved", "dismissed"}:
        return Response(content='{"error":"invalid status"}', status_code=400,
                        media_type="application/json")
    from okuro.sense.thoughts import update_thought
    msg = update_thought(thought_id, status=status)
    return {"ok": True, "message": msg}


@router.get("/api/dashboard/compliance")
def api_dashboard_compliance():
    """Per-provider compliance scorecard. JSON for the web dashboard."""
    from okuro.cli.db_helpers import get_db
    db = get_db()
    rows = db.fetchall(
        "SELECT provider, total_sessions, scored_sessions, avg_score, avg_normalized, "
        "bootstrap_rate, report_rate, cortex_rate, memory_rate, progress_rate, "
        "last_updated FROM provider_compliance ORDER BY total_sessions DESC"
    )
    return {
        "providers": [
            {
                "provider": r["provider"],
                "total_sessions": r["total_sessions"],
                "scored_sessions": r["scored_sessions"],
                "avg_score": float(r["avg_score"] or 0),
                "avg_normalized": float(r["avg_normalized"] or 0),
                "bootstrap_rate": float(r["bootstrap_rate"] or 0),
                "report_rate": float(r["report_rate"] or 0),
                "cortex_rate": float(r["cortex_rate"] or 0),
                "memory_rate": float(r["memory_rate"] or 0),
                "progress_rate": float(r["progress_rate"] or 0),
                "last_updated": r["last_updated"],
            }
            for r in rows
        ],
    }


@router.post("/api/dashboard/thought")
def api_dashboard_thought_create(payload: dict):
    """Capture a new thought. Called by Cmd-K palette and other web flows."""
    content = (payload.get("content") or "").strip()
    if not content:
        return Response(content='{"error":"content required"}', status_code=400,
                        media_type="application/json")
    category = payload.get("category")
    project = payload.get("project")
    from okuro.sense.thoughts import capture_thought
    thought_id = capture_thought(
        content=content,
        source="web",
        category=category,
        project=project,
    )
    return {"ok": True, "id": thought_id}


@router.get("/api/live-agents")
def api_live_agents():
    """Live agents — processes that have heartbeated recently.

    Drives the pulse live-count, per-agent satellite set, and the Home/Now
    page's Live-Agents zone. Orthogonal to `sessions.ended_at`: an agent
    stays live across many bootstrap → session_report cycles, so the list
    reflects "which CLIs/processes are alive right now?" not "which work
    units are in progress?"
    """
    try:
        from okuro.sense.agents import get_live_agents
        agents = get_live_agents(limit=50)
    except Exception as e:
        return {"agents": [], "error": str(e)}
    return {"agents": agents}


@router.get("/api/dashboard/live-sessions")
def api_dashboard_live_sessions():
    """Deprecated: use /api/live-agents.

    Kept for transitional compatibility. Returns the current live-agents set
    reshaped into the pre-split `sessions`-style envelope so any older UI
    asset loaded from a cached bundle doesn't immediately break.
    """
    try:
        from okuro.sense.agents import get_live_agents
        agents = get_live_agents(limit=50)
    except Exception as e:
        return {"sessions": [], "error": str(e)}
    sessions = [
        {
            "session_id": a.get("current_session_id") or a["id"],
            "provider":   a["provider"],
            "task_hint":  a.get("current_task_hint") or "",
            "project":    a.get("current_project"),
            "started_at": a["started_at"],
        }
        for a in agents
    ]
    return {"sessions": sessions}


@router.get("/api/dashboard/telemetry")
def api_dashboard_telemetry():
    """Telemetry heatmap — tool calls aggregated by hour and provider."""
    import json
    from collections import defaultdict
    from datetime import datetime, timedelta

    telemetry_path = okuro_home() / "telemetry" / "usage.jsonl"
    if not telemetry_path.exists():
        return {"heatmap": {}, "providers": [], "hours": list(range(24))}

    cutoff = datetime.now() - timedelta(days=7)
    counts: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    providers_seen: set[str] = set()

    try:
        with open(telemetry_path) as f:
            for line in f:  # noqa: PLR1702 — per-row try/except inside
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = entry.get("timestamp") or entry.get("ts", "")
                    provider = entry.get("provider", "unknown")
                    # ts may be an ISO string (canonical) or an int/float
                    # epoch-ms (doctor_probe write-health rows). Skip rows
                    # without a usable timestamp — silently dropping them is
                    # fine; they are noise, not signal.
                    if isinstance(ts, str) and ts:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    else:
                        continue
                    if dt.replace(tzinfo=None) > cutoff:
                        hour = dt.hour
                        counts[provider][hour] += 1
                        providers_seen.add(provider)
                except (json.JSONDecodeError, ValueError, AttributeError):
                    continue
    except Exception:
        pass

    return {
        "heatmap": {p: dict(h) for p, h in counts.items()},
        "providers": sorted(providers_seen),
        "hours": list(range(24)),
    }


# -- Schedules (daemon task registry) --

@router.get("/api/schedules")
def api_schedules():
    """List scheduled daemon tasks (builtins + user overrides).

    Powers Health / Schedules. Each row: id, description, cron, handler,
    enabled, last_run (if tracked), next_run (computed from cron).
    """
    from datetime import datetime
    from okuro.daemon.registry import get_all_tasks

    try:
        from croniter import croniter  # type: ignore
    except Exception:  # pragma: no cover — optional dep
        croniter = None

    tasks = get_all_tasks()
    now = datetime.now()

    last_runs = _load_daemon_last_runs()

    rows = []
    for t in tasks:
        next_run = None
        if croniter is not None and t.enabled:
            try:
                next_run = croniter(t.cron, now).get_next(datetime).isoformat()
            except Exception:
                next_run = None
        rows.append({
            "id": t.id,
            "description": t.description,
            "cron": t.cron,
            "handler": t.handler,
            "enabled": t.enabled,
            "run_on_startup": t.run_on_startup,
            "timeout_seconds": t.timeout_seconds,
            "last_run": last_runs.get(t.id),
            "next_run": next_run,
            "kind": t.kind,
            "tier": t.tier,
            "embeds": t.embeds,
        })
    return {"tasks": rows, "count": len(rows)}


def _load_daemon_last_runs() -> dict:
    """Read ~/.okuro/daemon/state.yaml if it exists — best-effort."""
    state_path = okuro_home() / "daemon" / "state.yaml"
    if not state_path.is_file():
        return {}
    try:
        import yaml
        data = yaml.safe_load(state_path.read_text()) or {}
        if isinstance(data, dict):
            runs = data.get("last_runs") or {}
            return runs if isinstance(runs, dict) else {}
    except Exception:
        return {}
    return {}


def _cron_is_valid(expr: str) -> tuple[bool, str]:
    """Validate a 5-field cron via croniter. Returns (ok, reason)."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return False, f"expected 5 fields, got {len(parts)}"
    try:
        from croniter import croniter  # type: ignore
        croniter(expr)
    except Exception as exc:
        return False, str(exc)
    return True, ""


def _schedule_row(task_id: str) -> dict | None:
    """Build the same row shape as ``/api/schedules`` for a single task."""
    from datetime import datetime
    from okuro.daemon.registry import get_all_tasks

    try:
        from croniter import croniter  # type: ignore
    except Exception:
        croniter = None

    now = datetime.now()
    last_runs = _load_daemon_last_runs()
    for t in get_all_tasks():
        if t.id != task_id:
            continue
        next_run = None
        if croniter is not None and t.enabled:
            try:
                next_run = croniter(t.cron, now).get_next(datetime).isoformat()
            except Exception:
                next_run = None
        return {
            "id": t.id,
            "description": t.description,
            "cron": t.cron,
            "handler": t.handler,
            "enabled": t.enabled,
            "run_on_startup": t.run_on_startup,
            "timeout_seconds": t.timeout_seconds,
            "last_run": last_runs.get(t.id),
            "next_run": next_run,
        }
    return None


def _reload_daemon() -> tuple[bool, str | None]:
    """Best-effort SIGHUP to okuro-daemon so a schedule edit applies live."""
    try:
        from okuro.system.service_manager import get_service_manager
        get_service_manager().reload("okuro-daemon")
        return True, None
    except Exception as exc:
        return False, f"daemon reload skipped ({exc}); applies on next daemon start"


@router.patch("/api/schedules/{task_id}")
def api_schedule_patch(task_id: str, payload: dict):
    """Toggle a daemon task and/or edit its cron, then hot-reload the daemon.

    Body: ``{"enabled"?: bool, "cron"?: str}``. Persists an override to
    ``~/.okuro/daemon/config.yaml`` and SIGHUPs okuro-daemon so the change
    applies without a restart. If the daemon is unreachable the override still
    persists (``applied=false``) and takes effect on next daemon start.
    """
    from fastapi import HTTPException
    from okuro.daemon.registry import save_task_override

    payload = payload or {}
    patch: dict = {}
    if "enabled" in payload:
        patch["enabled"] = bool(payload["enabled"])
    if payload.get("cron") is not None:
        cron = str(payload["cron"]).strip()
        ok, why = _cron_is_valid(cron)
        if not ok:
            raise HTTPException(400, f"Invalid cron '{cron}': {why}")
        patch["cron"] = cron
    if not patch:
        raise HTTPException(400, "nothing to update — provide 'enabled' and/or 'cron'")

    try:
        save_task_override(task_id, patch)
    except KeyError:
        raise HTTPException(404, f"unknown daemon task: {task_id}")

    applied, warning = _reload_daemon()
    return {"ok": True, "task": _schedule_row(task_id), "applied": applied, "warning": warning}


@router.delete("/api/schedules/{task_id}")
def api_schedule_reset(task_id: str):
    """Remove a task's override (reset to builtin default) + hot-reload."""
    from fastapi import HTTPException
    from okuro.daemon.registry import BUILTIN_TASKS, clear_task_override

    if not any(t.id == task_id for t in BUILTIN_TASKS):
        raise HTTPException(404, f"unknown daemon task: {task_id}")
    clear_task_override(task_id)
    applied, warning = _reload_daemon()
    return {"ok": True, "task": _schedule_row(task_id), "applied": applied, "warning": warning}


# -- systemd --user .timer units (Layer C of the schedule overview) --

def _list_user_timers() -> list[dict]:
    """Enumerate ~/.config/systemd/user/*.timer via `systemctl show` (robust
    per-property parse — list-timers columns are whitespace-ambiguous)."""
    import re
    import subprocess

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    if not unit_dir.is_dir():
        return []
    units = sorted(p.name for p in unit_dir.glob("*.timer"))

    def _abs_time(raw: str) -> str | None:
        # This systemd renders *USec properties as human wall-clock strings
        # (e.g. "Sun 2026-07-12 02:00:32 CEST"), not µs integers. Keep the
        # string when it's an absolute date (has a 4-digit year); drop
        # sentinels like "" / "n/a".
        raw = (raw or "").strip()
        return raw if re.search(r"\d{4}", raw) else None

    def _schedule_of(props: dict[str, str]) -> str | None:
        cal = props.get("TimersCalendar", "")
        m = re.search(r"OnCalendar=([^;}]+?)\s*(?:;|})", cal)
        if m:
            return m.group(1).strip()
        mono = props.get("TimersMonotonic", "")
        m = re.search(r"OnUnitActiveUSec=([^;}]+?)\s*(?:;|})", mono)
        if m:
            return f"every {m.group(1).strip()} (after last run)"
        m = re.search(r"OnBootUSec=([^;}]+?)\s*(?:;|})", mono)
        if m:
            return f"{m.group(1).strip()} after boot"
        return None

    rows: list[dict] = []
    for unit in units:
        props: dict[str, str] = {}
        try:
            out = subprocess.run(
                ["systemctl", "--user", "show", unit, "-p",
                 "Description,ActiveState,UnitFileState,NextElapseUSecRealtime,"
                 "NextElapseUSecMonotonic,LastTriggerUSec,TimersCalendar,"
                 "TimersMonotonic,Triggers"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in out.splitlines():
                k, _, v = line.partition("=")
                props[k] = v
        except Exception:
            pass
        state = props.get("UnitFileState", "")
        next_abs = _abs_time(props.get("NextElapseUSecRealtime", ""))
        next_mono = (props.get("NextElapseUSecMonotonic", "") or "").strip()
        if next_mono in ("", "infinity"):
            next_mono = ""
        next_run = next_abs or (f"in {next_mono}" if next_mono else None)
        rows.append({
            "unit": unit,
            "description": props.get("Description") or unit,
            "active": props.get("ActiveState") == "active",
            "enabled": state.startswith("enabled"),
            "state": state or "unknown",
            "schedule": _schedule_of(props),
            "next_run": next_run,
            "last_run": _abs_time(props.get("LastTriggerUSec", "")),
            "activates": props.get("Triggers") or None,
            "okuro_owned": unit.startswith("okuro-"),
        })
    return rows


@router.get("/api/timers")
def api_timers():
    """List systemd --user .timer units for the unified schedule overview."""
    return {"timers": _list_user_timers()}


@router.post("/api/timers/{unit}/toggle")
def api_timer_toggle(unit: str, payload: dict):
    """Enable/disable a user .timer (``--now`` also starts/stops it)."""
    from fastapi import HTTPException
    import re
    import subprocess

    if not re.fullmatch(r"[A-Za-z0-9@._-]+\.timer", unit):
        raise HTTPException(400, f"invalid timer unit: {unit!r}")
    unit_path = Path.home() / ".config" / "systemd" / "user" / unit
    if not unit_path.exists():
        raise HTTPException(404, f"timer not found: {unit}")

    enabled = bool((payload or {}).get("enabled"))
    verb = "enable" if enabled else "disable"
    try:
        r = subprocess.run(
            ["systemctl", "--user", verb, "--now", unit],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        raise HTTPException(500, f"systemctl {verb} error: {exc}")
    if r.returncode != 0:
        raise HTTPException(500, f"systemctl {verb} failed: {r.stderr.strip()}")
    return {"ok": True, "unit": unit, "enabled": enabled}


# -- Cortex sidecar coverage + enrichment log --

_COVERAGE_TTL_S = 300  # coverage walk is expensive; serve cached within this window


@router.get("/api/cortex/coverage")
def api_cortex_coverage(refresh: bool = False):
    """Per-project sidecar coverage at the **file** level.

    Covered = eligible source file with a sidecar entry OR an inline
    AGENT_HEADER; stale = sidecar content_hash no longer matches disk.

    The walk is expensive, so results are cached: in-process first, then a
    disk cache (survives restarts) written on each compute and invalidated by
    the scanner after a scan changes sidecars. Pass ``refresh=1`` to force a
    recompute. See okuro.cortex.coverage.
    """
    from okuro.cortex import coverage as _cov

    mem = getattr(api_cortex_coverage, "_cache", None)
    if not refresh and mem and (time.time() - mem[0]) < _COVERAGE_TTL_S:
        return {**mem[1], "cached": "memory", "age_s": round(time.time() - mem[0])}

    if not refresh:
        disk = _cov.load_fresh(_COVERAGE_TTL_S)
        if disk is not None:
            api_cortex_coverage._cache = (time.time(), disk)
            age = round(time.time() - disk.get("computed_at", time.time()))
            return {**disk, "cached": "disk", "age_s": age}

    result = _cov.compute_coverage()
    _cov.save(result)
    api_cortex_coverage._cache = (time.time(), result)
    return {**result, "cached": False, "age_s": 0}


@router.get("/api/cortex/enrichment")
def api_cortex_enrichment(limit: int = 50):
    """Return the most recent enrichment events from ~/.okuro/cortex/enrichment.jsonl.

    Powers the Health / Cortex enrichment-log viewer. Most-recent first.
    """
    log_path = okuro_home() / "cortex" / "enrichment.jsonl"
    if not log_path.is_file():
        return {"events": [], "count": 0}

    limit = max(1, min(500, int(limit)))
    events: list[dict] = []
    try:
        # Read last ~200KB; enough for a few hundred entries without scanning huge logs.
        size = log_path.stat().st_size
        with open(log_path, "rb") as f:
            if size > 200_000:
                f.seek(size - 200_000)
                f.readline()  # discard partial line
            data = f.read().decode("utf-8", errors="replace")
        for line in data.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return {"events": [], "count": 0}

    events.reverse()  # newest first
    return {"events": events[:limit], "count": len(events[:limit])}


# --- okuro-notes ----------------------------------------------------------
# Obsidian-inspired note surface. Markdown format, DB storage, RAG-native.
# /events and /search are declared BEFORE /{note_id} so those literal segments
# are not captured as a note id.
@router.get("/api/notes")
def api_notes_list(
    project: str | None = None, archived: bool = False, limit: int | None = None
):
    """Sidebar list. Unlimited by default — the client builds a folder tree from
    this, so truncating it yields a wrong tree, not just a shorter one. Rows are
    metadata only (no bodies), so the payload stays small.

    `truncated` is returned so an explicit limit can never drop notes silently.
    """
    from okuro.notes import list_notes

    rows = list_notes(project=project, archived=archived, limit=limit)
    truncated = limit is not None and len(rows) == limit
    return {"notes": rows, "count": len(rows), "truncated": truncated}


@router.get("/api/notes/events")
async def api_notes_events(request: Request, since: int | None = None):
    """SSE change-feed. Streams `saved`/`deleted` as notes mutate (web OR MCP)."""
    from okuro.notes import events_since, latest_seq

    start = since if since is not None else await asyncio.to_thread(latest_seq)

    async def gen():
        cursor = start
        yield ": connected\n\n"
        ticks = 0
        while True:
            if await request.is_disconnected():
                break
            rows = await asyncio.to_thread(events_since, cursor)
            for r in rows:
                cursor = r["seq"]
                yield f"event: {r['kind']}\ndata: {json.dumps(r)}\n\n"
            ticks += 1
            if ticks % 20 == 0:
                yield ": ping\n\n"
            await asyncio.sleep(0.75)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/notes/search")
def api_notes_search(q: str, limit: int = 20, project: str | None = None):
    from okuro.notes import search_notes

    rows = search_notes(q, limit=limit, project=project)
    return {"results": rows, "count": len(rows)}


@router.get("/api/notes/graph")
def api_notes_graph():
    from okuro.notes import note_graph

    return note_graph()


# --- note folders --- declared BEFORE /{note_id} so the literal "folders"
# segment is never captured as a note id (mirrors flow-designer folders).
@router.get("/api/notes/folders")
def api_notes_folders_list():
    """List all folders flat; the tree is rebuilt client-side from parent_id."""
    from okuro.notes import list_folders

    return {"folders": list_folders()}


@router.post("/api/notes/folders")
def api_notes_folder_create(payload: dict):
    from okuro.notes import create_folder

    folder = create_folder(
        (payload.get("name") or "").strip(), payload.get("parent_id") or None
    )
    return {"folder": folder}


@router.put("/api/notes/folders/{folder_id}")
def api_notes_folder_update(folder_id: str, payload: dict):
    """Rename and/or reparent. ``parent_id`` present (incl. null) moves it."""
    from okuro.notes import move_folder, rename_folder

    ok = True
    if "name" in payload:
        ok = rename_folder(folder_id, payload.get("name") or "") and ok
    if "parent_id" in payload:
        ok = move_folder(folder_id, payload.get("parent_id") or None) and ok
    return {"success": ok}


@router.delete("/api/notes/folders/{folder_id}")
def api_notes_folder_delete(folder_id: str):
    """Delete a folder; subfolders reparent up, notes fall back to root."""
    from okuro.notes import delete_folder

    return {"success": delete_folder(folder_id)}


@router.put("/api/notes/{note_id}/folder")
def api_notes_set_folder(note_id: str, payload: dict):
    """Move a note into a folder (``folder_id`` null = root). Cheap — no re-embed."""
    from okuro.notes import set_note_folder

    return {"success": set_note_folder(note_id, payload.get("folder_id") or None)}


@router.get("/api/notes/drawings")
def api_notes_drawings_list(note_id: str):
    from okuro.notes import list_drawings

    return {"drawings": list_drawings(note_id)}


@router.post("/api/notes/drawings")
def api_notes_drawing_save(payload: dict):
    import base64

    from okuro.notes import upsert_drawing

    png = None
    raw = payload.get("png_base64")
    if raw:
        try:
            png = base64.b64decode(str(raw).split(",")[-1])
        except Exception:  # noqa: BLE001
            png = None
    d = upsert_drawing(
        drawing_id=payload.get("id"),
        note_id=payload.get("note_id"),
        title=payload.get("title") or "Untitled drawing",
        scene=payload.get("scene") or {},
        png=png,
    )
    return {"drawing": d}


@router.get("/api/notes/drawings/{drawing_id}")
def api_notes_drawing_get(drawing_id: str):
    from okuro.notes import get_drawing

    d = get_drawing(drawing_id)
    if d is None:
        return {"error": f"drawing '{drawing_id}' not found"}
    return {"drawing": d}


@router.get("/api/notes/drawings/{drawing_id}/png")
def api_notes_drawing_png(drawing_id: str):
    from fastapi import HTTPException, Response

    from okuro.notes import get_drawing_png

    png = get_drawing_png(drawing_id)
    if not png:
        raise HTTPException(404, "no png for this drawing")
    return Response(content=png, media_type="image/png")


@router.post("/api/notes/images")
async def api_notes_image_upload(
    file: UploadFile = File(...),
    note_id: str | None = Form(None),
):
    """Store a pasted/dropped image and return its id + embed url. Bytes live in
    the DB (mirrors drawings); the body references it as ![alt](url)."""
    from okuro.notes import add_image

    data = await file.read()
    if not data:
        return {"error": "empty upload"}
    mime = file.content_type or "image/png"
    if not mime.startswith("image/"):
        return {"error": f"unsupported content-type '{mime}'"}
    img = add_image(
        data=data,
        mime=mime,
        note_id=(note_id or None),
        filename=file.filename,
    )
    img["url"] = f"/api/notes/images/{img['id']}"
    return {"image": img}


@router.get("/api/notes/images/{image_id}")
def api_notes_image_get(image_id: str):
    from fastapi import HTTPException, Response

    from okuro.notes import get_image

    got = get_image(image_id)
    if not got:
        raise HTTPException(404, "no such image")
    data, mime = got
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post("/api/notes")
def api_notes_create(payload: dict):
    from okuro.notes import upsert_note

    note = upsert_note(
        title=payload.get("title") or "",
        body=payload.get("body") or "",
        frontmatter=payload.get("frontmatter"),
        project=payload.get("project"),
        folder_id=payload.get("folder_id") or None,
        origin=payload.get("origin") or "web",
    )
    return {"note": note}


@router.get("/api/notes/{note_id}")
def api_notes_get(note_id: str):
    from okuro.notes import get_note

    note = get_note(note_id)
    if note is None:
        return {"error": f"note '{note_id}' not found"}
    return {"note": note}


@router.put("/api/notes/{note_id}")
def api_notes_update(note_id: str, payload: dict):
    from okuro.notes import get_note, upsert_note

    existing = get_note(note_id)
    if existing is None:
        return {"error": f"note '{note_id}' not found"}
    # folder_id forwarded only when the key is present, so a title/body autosave
    # never re-parents the note (storage keeps the existing folder via sentinel).
    extra = {"folder_id": payload["folder_id"] or None} if "folder_id" in payload else {}
    # A `title` key present is a RENAME and pins the title; a blank one is the
    # rename box submitted empty, which hands the note back to the first-line
    # rule. No `title` key at all is an autosave saying nothing about the name,
    # so storage keeps an explicit one and re-derives the rest (migration 141).
    if "title" in payload:
        title = payload["title"] or ""
        extra["title_explicit"] = bool(title.strip()) and title.strip() != "Untitled"
    else:
        title = ""
    note = upsert_note(
        note_id=note_id,
        title=title,
        body=payload.get("body", existing["body"]),
        frontmatter=payload.get("frontmatter", existing["frontmatter"]),
        project=payload.get("project", existing.get("project")),
        origin=payload.get("origin") or "web",
        **extra,
    )
    return {"note": note}


@router.delete("/api/notes/{note_id}")
def api_notes_delete(note_id: str, origin: str | None = None):
    from okuro.notes import delete_note

    return {"success": delete_note(note_id)}


@router.get("/api/notes/{note_id}/backlinks")
def api_notes_backlinks(note_id: str):
    from okuro.notes import backlinks, outgoing_links

    return {"backlinks": backlinks(note_id), "outgoing": outgoing_links(note_id)}


class _ImmutableStatic(StaticFiles):
    """StaticFiles that long-caches content-hashed assets.

    Vanilla StaticFiles emits an ETag + Last-Modified but NO Cache-Control,
    so a webview still issues a conditional GET for every chunk on every
    relaunch — the round-trips read as a slow "initial load" on each lazy
    route (Tasks, Knowledge, …). Files under /assets carry a content hash
    in their name (index-CiY5nT26.js); a content change mints a new name, so
    the old one is safe to cache forever. This makes repeat loads hit cache
    with zero network instead of revalidating.
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers.setdefault(
            "Cache-Control", "public, max-age=31536000, immutable"
        )
        return resp


def mount_spa(app: FastAPI, dist_dir: Path | None = None) -> None:
    """Mount the Vite-built SPA onto an existing FastAPI app.

    Call this AFTER all API routes (routers + @app decorators) are registered,
    otherwise the catch-all SPA fallback will shadow API paths.
    """
    target = dist_dir or DIST_DIR
    if not target.exists():
        return

    assets_dir = target / "assets"
    if assets_dir.exists():
        app.mount("/assets", _ImmutableStatic(directory=assets_dir), name="assets")

    # Files that must never be cached across rebuilds. index.html lives here
    # because every build mints fresh hashed chunk filenames — if the browser
    # keeps an old index.html, its <script src="/assets/index-{oldhash}.js">
    # tag points at a chunk that no longer exists and the lazy routes raise
    # "Importing a module script failed". Hashed /assets/* stay long-cache
    # because the hash changes invalidate them automatically.
    _NO_CACHE = {"sw.js", "manifest.json", "index.html"}
    _NO_CACHE_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    @app.get("/{path:path}", include_in_schema=False)
    async def spa_fallback(path: str):
        # Serve asset file if it exists under dist/, otherwise fall back to index.html.
        file = target / path
        if file.is_file():
            headers = _NO_CACHE_HEADERS if file.name in _NO_CACHE else {}
            return FileResponse(file, headers=headers)
        return FileResponse(target / "index.html", headers=_NO_CACHE_HEADERS)


# Pre-warm the flow-draw process pool so the first draw after startup is fast.
_warm_flow_pool()
