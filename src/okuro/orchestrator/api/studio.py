# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: /api/studio router — the user-facing generation surface. Thin HTTP over
#          the preset catalog + generation facade so the Studio UI stays model-free:
#          list style presets, check readiness in human terms, generate from a
#          preset + plain prompt, and browse the result library. Never exposes
#          ComfyUI, workflows, nodes, samplers, or checkpoints.
# index:
#   GET  /presets            (style picker)
#   GET  /readiness          (setup_needed | ready, in human words)
#   POST /generate           (preset + prompt -> images)
#   GET  /library            (result tiles — bytes resolve via the asset store)
#   GET  /image/{name}       (serve a just-generated file out of the output dir)
# AGENT_HEADER_END -->
"""okuro·studio API — outcome-based generation over HTTP.

Thin surface over ``okuro.inference.comfy_presets`` + ``okuro.inference.gen_tools``.
The UI shows styles, not models; this router mirrors that: a preset id + a plain
prompt in, image URLs out. Setup/readiness is reported in human states so the UI
never has to know about ComfyUI provisioning, nodes, or workflows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger("okuro.orchestrator.api.studio")

router = APIRouter(prefix="/api/studio", tags=["studio"])


@router.get("/presets")
def list_presets(modality: str | None = None) -> dict:
    """Style presets for the picker (id/label/icon/description/modality/tiers).

    ``needs_gpu`` lets the UI badge the styles that render in the cloud — those
    work on a machine with no GPU and nothing downloaded. The engine NAME stays
    out of this payload deliberately: which renderer runs is okuro's business,
    "does this need my GPU set up" is the user's.
    """
    from okuro.inference import comfy_presets as cp
    return {"presets": [
        {"id": p.id, "label": p.label, "description": p.description,
         "icon": p.icon, "modality": p.modality,
         "tiers": list(cp.TIER_DEFAULTS.keys()), "default_tier": p.default_tier,
         "needs_gpu": p.needs_gpu}
        for p in cp.list_presets(modality)]}


@router.get("/readiness")
def readiness(preset: str = Query(...)) -> dict:
    """Is generation ready for this preset — in human words, no ComfyUI jargon.

    LOCAL presets need BOTH a runtime (okuro's ComfyUI installed, or an explicit
    endpoint hook) AND a model for the preset's family (else setup must still
    download one). A hook implies an externally-managed instance whose models
    okuro doesn't track, so a hook alone counts as ready.

    CLOUD presets need neither — only a bridge media provider that is installed
    AND authenticated. That distinction matters: a CLI that is present but signed
    out is a different fix from one that isn't installed, so the message says
    which.
    """
    from okuro.inference import comfy_install, comfy_presets as cp, gen_tools

    from okuro.bridge import media

    p = cp.get_preset(preset)
    if p is None:
        raise HTTPException(status_code=404, detail=f"unknown preset {preset!r}")

    has_local_choice = cp.ENGINE_LOCAL in p.engines
    cloud = media.availability(media.IMAGE)

    installed = comfy_install.is_installed() if has_local_choice else False
    hooked = bool(os.environ.get("OKURO_COMFYUI_ENDPOINTS")) if has_local_choice else False
    has_model = bool(gen_tools.resolve_model_for_family(p.family)) if has_local_choice else False
    runtime_ready = installed or hooked
    local_ready = has_local_choice and (hooked or (installed and has_model))

    # Ready if ANY choice can run. A style that offers both engines works the
    # moment either side is usable — that is the whole point of the choice axis.
    if local_ready or cloud["available"]:
        state, message = "ready", "Ready to generate"
    elif has_local_choice:
        # okuro CAN fix a local gap itself, so prefer that instruction over the
        # cloud one — a download beats asking the user to install a CLI.
        state = "setup_needed"
        message = ("Download a model for this style" if runtime_ready
                   else "Set up image generation")
    else:
        state, message = "setup_needed", cloud["message"]

    return {
        "preset": p.id, "family": p.family,
        "engines": sorted(p.engines), "needs_gpu": p.needs_gpu,
        "comfy_installed": installed, "has_model": has_model,
        "runtime_ready": runtime_ready,
        # okuro can only self-serve a LOCAL gap; it cannot sign in to a user's CLI.
        "self_service": has_local_choice,
        "provider": cloud["provider"], "provider_state": cloud["state"],
        "state": state, "message": message,
    }


@router.get("/models")
def models(preset: str = Query(...)) -> dict:
    """Curated per-style model picker + the default resolved model (feature C).

    Powers the Studio "Model & prompt" disclosure so a user can see which model
    a style will use and switch to another installed one. Outcome-first stays the
    default; this is opt-in transparency (progressive disclosure, DP02).
    """
    from okuro.inference import gen_tools

    try:
        return gen_tools.candidates_for_preset(preset)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown preset {preset!r}")


class SetupBody(BaseModel):
    preset: str | None = None
    family: str | None = None
    consent: bool = False
    org_revenue_usd: float | None = None


@router.post("/setup")
def setup(body: SetupBody) -> dict:
    """Start first-time setup for a style (Gap A): install engine → download a
    model → prepare it. Returns a job_id; stream progress from
    ``/setup/progress/{job_id}``. Consent is required (installs GPL ComfyUI +
    downloads a model)."""
    from okuro.inference import comfy_presets as cp, studio_setup
    from okuro.inference.studio_jobs import JOBS

    family = body.family
    model_hint = None
    if body.preset:
        p = cp.get_preset(body.preset)
        if p is None:
            raise HTTPException(status_code=404, detail=f"unknown preset {body.preset!r}")
        if cp.ENGINE_LOCAL not in p.engines:
            # Nothing to install: every choice for this style runs through a CLI
            # the user owns and signs into themselves. okuro must not pretend it
            # can automate someone else's vendor login.
            from okuro.bridge import media

            raise HTTPException(status_code=409, detail={
                "state": "manual_setup",
                "message": media.availability(media.IMAGE)["message"]})
        family, model_hint = p.family, p.model_hint
    if not family:
        raise HTTPException(status_code=400, detail="preset or family is required")
    if not body.consent:
        raise HTTPException(
            status_code=400,
            detail="consent required — setup installs the engine and downloads a model")

    def _target(job):
        return studio_setup.run_setup(
            family=family, model_hint=model_hint, consent=True,
            org_revenue_usd=body.org_revenue_usd,
            progress=lambda phase, message, pct=None: job.emit(phase, message, pct))

    job = JOBS.run_async("setup", _target)
    return {"job_id": job.id, "state": job.state}


async def _stream_job(job_id: str):
    """SSE-stream any studio job's progress, then a terminal `done`/`error`.

    Polls the job's ordered event list (no asyncio task juggling) and relays new
    events as they land; the terminal frame carries the job result. Shared by
    setup (Gap A) and generation (Gap C) so the UI has one progress contract.
    """
    from okuro.inference.studio_jobs import JOBS, RUNNING

    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")

    async def _gen():
        yield ": connected\n\n"
        cursor = 0
        while True:
            cursor, new = job.events_since(cursor)
            for ev in new:
                yield f"event: progress\ndata: {json.dumps(ev.to_dict())}\n\n"
            if job.state != RUNNING:
                payload = {"state": job.state, "result": job.result,
                           "error": job.error}
                yield f"event: {job.state}\ndata: {json.dumps(payload)}\n\n"
                return
            # Tight poll so streamed chat tokens relay smoothly (image/setup
            # jobs emit sparse events, so this costs them nothing).
            await asyncio.sleep(0.1)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.get("/setup/progress/{job_id}")
async def setup_progress(job_id: str):
    """SSE stream of a setup job's progress events, then a terminal `done`/`error`."""
    return await _stream_job(job_id)


class GenerateBody(BaseModel):
    preset: str
    prompt: str
    tier: str | None = None
    model_id: str | None = None
    ckpt_name: str | None = None
    seed: int | None = None
    org_revenue_usd: float | None = None
    # Cloud presets only: pin what text may be rendered. A word/phrase allows
    # exactly that; "" forces a text-free image; null (default) leaves the model
    # free — and it WILL invent captions, dates, venues and real-looking credits.
    literal_text: str | None = None
    # Cloud presets only: LIBRARY REFERENCES to edit rather than generate from
    # scratch ("make the jacket red"). Ignored by the local engine.
    #
    # Asset ids or filenames, never paths: these files are read off disk and
    # shipped to a cloud provider, so a client-supplied path would be an
    # exfiltration primitive. Resolved by _resolve_references below.
    reference_images: list[str] | None = None


def _resolve_references(refs: list[str] | None) -> list[str] | None:
    """Turn library references into absolute paths on disk.

    Accepts an ASSET ID (preferred) or a library filename. The id path resolves
    through ``store.resolve_file``, which knows both homes a bucket row can have
    — bytes referenced in place (``abs_path``) and bucket-owned blobs
    (``rel_path``). Without it, a tile that the library happily DISPLAYS could
    not be used as an edit source, because filename resolution only ever finds
    the generations dir.

    Never accepts a path. These files are read off disk and shipped to a cloud
    provider, so a client-supplied path would be an exfiltration primitive: ids
    are looked up server-side, and filenames are stripped to a basename before
    being joined to the generations dir. An unknown reference 404s rather than
    silently generating from scratch when the user asked for an edit.
    """
    if not refs:
        return None
    from okuro.assets import store
    from okuro.inference.gen_tools import _output_dir

    d = _output_dir()
    out = []
    for ref in refs:
        row = store.get_asset(ref)
        if row:
            p = store.resolve_file(row)
            if p and Path(p).is_file():
                out.append(str(p))
                continue
            raise HTTPException(status_code=404,
                                detail=f"asset {ref!r} has no readable file")
        f = d / Path(ref).name
        if not f.is_file():
            raise HTTPException(status_code=404,
                                detail=f"unknown library image {Path(ref).name!r}")
        out.append(str(f))
    return out


def _generate_payload(r: dict) -> dict:
    """Shared result shape for /generate and /generate/stream: image URLs plus
    feature-C transparency — which model ran and how the prompt was shaped."""
    names = [Path(p).name for p in r["paths"]]
    # Address the bytes by asset id, the one resolver that knows both homes a
    # bucket row can have. Indexing is best-effort, so fall back to the
    # output-dir route when it did not run — a just-generated file is always
    # there, which is why this route survives as the narrow fallback.
    ids = r.get("asset_ids") or []
    images = ([f"/api/assets/media/{i}/file" for i in ids]
              if len(ids) == len(names)
              else [f"/api/studio/image/{n}" for n in names])
    return {
        "preset": r["preset"], "tier": r["tier"], "seed": r["seed"],
        "prompt_used": r["prompt_used"], "count": r["count"],
        "images": images,
        "model": {"id": r.get("model_id"), "family": r.get("family")},
        "prompt_plan": r.get("prompt_plan"),
        "engine": r.get("engine"),
    }


@router.post("/generate")
def generate(body: GenerateBody) -> dict:
    """Generate from a style preset + a plain prompt. Returns image URLs.

    No model needs to be named — okuro auto-solves an installed model for the
    preset's family (Gap B). When nothing is installed yet, returns 409 so the
    UI can route the user to setup instead of showing a raw error.
    """
    from okuro.bridge.media import MediaUnavailable
    from okuro.inference import gen_tools

    # Resolve BEFORE the try: this is input validation, and its 404 must reach
    # the client as a 404. Inside, the blanket handler would rewrap it as a 502
    # and a rejected path would read as a server fault.
    refs = _resolve_references(body.reference_images)
    try:
        r = gen_tools.run_from_preset(
            body.prompt, body.preset, tier=body.tier,
            model_id=body.model_id, ckpt_name=body.ckpt_name,
            seed=body.seed, org_revenue_usd=body.org_revenue_usd,
            reference_images=refs, literal_text=body.literal_text)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown preset {body.preset!r}")
    except MediaUnavailable as exc:  # cloud CLI missing or signed out
        raise HTTPException(status_code=409,
                            detail={"state": "manual_setup", "message": str(exc)})
    except ValueError as exc:  # no model available for the family -> needs setup
        raise HTTPException(status_code=409,
                            detail={"state": "setup_needed", "message": str(exc)})
    except Exception as exc:  # licence/edition/generation errors -> friendly 4xx/5xx
        logger.warning("studio generate failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    return _generate_payload(r)


@router.post("/generate/stream")
def generate_stream(body: GenerateBody) -> dict:
    """Start a generation as a job (Gap C) and return a job_id; stream live
    progress from ``/generate/progress/{job_id}``. Same inputs as ``/generate``;
    the terminal `done` event carries the image URLs."""
    from okuro.inference import gen_tools
    from okuro.inference.studio_jobs import JOBS

    # Resolve up front, not inside the job: a bad reference is a request error
    # the caller should see on THIS response, not a job that starts and then
    # fails somewhere down an SSE stream.
    refs = _resolve_references(body.reference_images)

    def _target(job):
        r = gen_tools.run_from_preset(
            body.prompt, body.preset, tier=body.tier,
            model_id=body.model_id, ckpt_name=body.ckpt_name,
            seed=body.seed, org_revenue_usd=body.org_revenue_usd,
            reference_images=refs, literal_text=body.literal_text,
            on_progress=lambda ev: job.emit(
                ev.get("phase", "generate"), ev.get("message", ""), ev.get("pct")),
            on_cancel=job.cancel_requested)
        return _generate_payload(r)

    job = JOBS.run_async("generate", _target)
    return {"job_id": job.id, "state": job.state}


@router.get("/generate/progress/{job_id}")
async def generate_progress(job_id: str):
    """SSE stream of a generation job's live progress, then terminal done/error
    with the image URLs. Mirrors ``/setup/progress`` (one progress contract)."""
    return await _stream_job(job_id)


@router.post("/generate/cancel/{job_id}")
def generate_cancel(job_id: str) -> dict:
    """Cancel a running generation (interrupts ComfyUI, releases the poll). The
    progress stream ends with a terminal `cancelled` event."""
    from okuro.inference.studio_jobs import JOBS

    return {"cancelled": JOBS.cancel(job_id)}


class TextGenerateBody(BaseModel):
    bundle_id: str
    prompt: str
    system_prompt: str | None = None
    temperature: float | None = None


class ChatMessage(BaseModel):
    role: str
    content: str


class TextChatBody(BaseModel):
    bundle_id: str
    messages: list[ChatMessage]
    temperature: float | None = None


@router.get("/text/models")
def text_models() -> dict:
    """Installed LLM bundles you can test, each flagged warm if already served."""
    from okuro.inference.text_serve import list_text_bundles

    return {"models": list_text_bundles()}


@router.get("/text/engines")
def text_engines() -> dict:
    """Text engines currently loaded on a GPU — the 'what's running' overview."""
    from okuro.inference.text_serve import running_engines

    return {"engines": running_engines()}


@router.post("/text/unload")
def text_unload(payload: dict) -> dict:
    """Unload a running text engine — frees its GPU VRAM (kills the process)."""
    from okuro.inference.text_serve import stop_engine

    bundle_id = (payload or {}).get("bundle_id")
    if not bundle_id:
        raise HTTPException(status_code=400, detail="bundle_id required")
    return {"ok": stop_engine(bundle_id), "bundle_id": bundle_id}


@router.post("/text/generate")
def text_generate(body: TextGenerateBody) -> dict:
    """Run a prompt through a local LLM bundle. Starts (or reuses) an engine,
    then generates — returns a job_id; stream progress from the shared
    ``/generate/progress/{job_id}`` (loading → generating → done with the text).
    """
    from okuro.inference.studio_jobs import JOBS

    if not body.bundle_id or not body.prompt.strip():
        raise HTTPException(status_code=400, detail="bundle_id and prompt are required")

    def _target(job):
        from okuro.inference.text_serve import generate_text

        r = generate_text(
            body.bundle_id,
            body.prompt,
            system_prompt=body.system_prompt,
            temperature=0.7 if body.temperature is None else body.temperature,
            on_status=lambda msg: job.emit("generate", msg, None),
        )
        if not r.get("success"):
            # surface the model/engine error as a job failure, not a silent empty
            raise RuntimeError(r.get("error") or "generation failed")
        return r

    job = JOBS.run_async("text", _target)
    return {"job_id": job.id, "state": job.state}


@router.post("/text/chat")
def text_chat(body: TextChatBody) -> dict:
    """Multi-turn chat with a local LLM, streaming token-by-token.

    Returns a job_id; stream it via the shared ``/generate/progress/{job_id}``:
    each token arrives as a progress event ``{phase: "token", message: <delta>}``,
    then a terminal ``done`` with the full reply.
    """
    from okuro.inference.studio_jobs import JOBS

    if not body.bundle_id or not body.messages:
        raise HTTPException(status_code=400, detail="bundle_id and messages are required")

    msgs = [{"role": m.role, "content": m.content} for m in body.messages]

    def _target(job):
        from okuro.inference.text_serve import chat_stream

        r = chat_stream(
            body.bundle_id,
            msgs,
            temperature=0.7 if body.temperature is None else body.temperature,
            on_status=lambda msg: job.emit("status", msg, None),
            on_token=lambda delta: job.emit("token", delta, None),
        )
        if not r.get("success") and not r.get("text"):
            raise RuntimeError(r.get("error") or "chat failed")
        return r

    job = JOBS.run_async("chat", _target)
    return {"job_id": job.id, "state": job.state}


@router.get("/library")
def library(
    limit: int = 60,
    q: str | None = None,
    tag: str | None = None,
) -> dict:
    """Generated results as tiles (newest first), optionally searched/filtered.

    Reads the ONE unified media bucket filtered to the studio source, flattened
    (no nested folders). ``sync_studio_dir`` first indexes any on-disk generation
    not yet registered (heals pre-existing files), so the bucket is authoritative.
    ``q`` searches the tile title (store-native LIKE); ``tag`` restricts to tiles
    carrying that tag.

    Each tile is the FULL asset row (id/kind/source/folder/title/mime/created_at
    /tags/meta) so the UI can open the shared media inspector without a second
    round trip, and its ``url`` resolves BY ASSET ID through the store's own
    byte route. It used to be ``/api/studio/image/{name}``, which rebuilds
    ``_output_dir() / name`` — that only ever finds the generations dir, so every
    row whose bytes live in the bucket instead (``rel_path``, e.g. illustrations
    under OKURO_ASSETS_DIR) 404'd and rendered as an invisible tile. One resolver
    for one bucket; ``name`` stays for the reference picker, which submits
    library FILENAMES.
    """
    from okuro.assets import store
    from pathlib import Path as _P

    try:
        store.sync_studio_dir()
        rows = store.list_assets(
            source="studio", q=q, tag=tag, limit=limit
        )
    except Exception as exc:  # bucket unavailable -> empty, never 500 the UI
        logger.warning("studio library read failed: %s", exc)
        return {"images": []}
    images = []
    for r in rows:
        p = r.get("abs_path") or r.get("rel_path") or ""
        name = _P(p).name
        if not name:
            continue
        images.append({
            "name": name, "url": f"/api/assets/media/{r['id']}/file",
            "id": r["id"], "kind": r["kind"], "tags": r.get("tags", []),
            "meta": r.get("meta", {}),
            "source": r.get("source"), "folder": r.get("folder"),
            "title": r.get("title"), "mime": r.get("mime"),
            "created_at": r.get("created_at"),
        })
    return {"images": images}


# Suffix → media type. The local engine only ever wrote PNG, so this used to be
# hardcoded; cloud providers return JPEG, and a JPEG served as image/png is a lie
# browsers happen to tolerate.
_IMAGE_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}


@router.get("/image/{name}")
def image(name: str):
    """Serve a JUST-generated image by filename (path-traversal-safe).

    Scope is deliberately the output dir only: this backs the results of the
    generation that is on screen, whose files were written there moments ago.
    LIBRARY tiles do not come through here — they carry an asset id and resolve
    through the store (see ``/library``), which is the one resolver that knows
    both byte homes.
    """
    from okuro.inference.gen_tools import _output_dir

    safe = Path(name).name  # strip any directory components
    f = _output_dir() / safe
    if not f.exists() or not f.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(
        str(f),
        media_type=_IMAGE_MEDIA_TYPES.get(f.suffix.lower(), "application/octet-stream"))
