# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: /api/voice/tts/* — read + set the delivery-pipeline voices (podcast /
#   summary / morning brief). Exposes ONE roster: the engine this machine's tier
#   actually renders with (pro -> qwen, air/advanced -> kokoro), its two standard
#   voices (female + male), which of them is okuro, and whether okuro's house
#   post-fx is on. Everything resolves through peer.delivery.tts_settings (single
#   source of truth); this API is a thin read/validate/write + preview-serving
#   surface over it. Mirrors the STT tier API's localhost-write policy.
# index:
#   response models
#   _require_localhost / _engine / _config_response
#   GET /config  PUT /config  POST /previews  GET /preview/{engine}/{voice}
# AGENT_HEADER_END -->
"""Delivery-voice settings API.

Read-only ``GET /config`` and preview playback are safe from any LAN client
(voice roster + sample clips). The mutating ``PUT /config`` is localhost-only,
matching the STT-tier / keyring policy: a voice change rewrites
``~/.okuro/tts-config.yaml`` and must not be driveable from another LAN host.

**One tier, one roster.** The engine is not a user choice — it follows the
hardware edition (``tts.active_engine``), so offering every engine's voices was
offering voices the machine will never speak in. This surface reports the active
engine and only its roster.

Previews: Kokoro renders lazily on first request (CPU, ~1s/clip). Qwen cannot —
its model load is ~47s, so the whole roster is rendered as one background batch
(``POST /previews``) and the UI polls ``previews`` in ``GET /config`` instead of
holding a request open for two minutes.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from okuro.peer.delivery import tts_previews, tts_settings

logger = logging.getLogger("okuro.orchestrator.api.voice_tts")

router = APIRouter(prefix="/api/voice/tts", tags=["voice"])


# ── response models ─────────────────────────────────────────────────────
class VoiceOption(BaseModel):
    id: str
    label: str
    gender: str            # female | male
    has_preview: bool


class PreviewStatus(BaseModel):
    ready: bool            # every roster voice has a current clip
    pending: bool          # a batch render is in flight
    missing: list[str]
    total: int


class TTSConfigResponse(BaseModel):
    sample_text: str
    engine: str            # qwen | kokoro — what this machine renders with
    edition: str           # air | advanced | pro
    # current selections
    female: str
    male: str
    okuro_gender: str      # which of the two IS okuro
    speed: float
    # okuro's house post-fx chain — engine-agnostic, one global switch. The tuned
    # values stay in the YAML (a fx-tuner surface, not a settings one); this is
    # only whether any of it is applied.
    fx_enabled: bool
    # the roster for THIS tier
    voices: list[VoiceOption]
    previews: PreviewStatus
    speed_min: float
    speed_max: float


class TTSConfigUpdate(BaseModel):
    female: str | None = None
    male: str | None = None
    okuro_gender: str | None = None
    speed: float | None = None
    fx_enabled: bool | None = None


def _require_localhost(request: Request) -> None:
    client = request.client
    host = client.host if client else None
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(403, "TTS voice writes are localhost-only")


def _engine() -> tuple[str, str]:
    """(engine, edition) for this machine — Kokoro floor on any detection fault."""
    try:
        from okuro.peer.delivery import tts
        engine = tts.active_engine()
        edition = tts._edition()
    except Exception as exc:  # noqa: BLE001
        logger.warning("engine detection failed (%s) — reporting the kokoro floor", exc)
        return "kokoro", "air"
    # MOSS/Orpheus are pin-only escape hatches with no gendered roster; the
    # settings surface only speaks for the two engines a tier can select.
    return (engine if engine in ("qwen", "kokoro") else "kokoro"), edition


def _labels(engine: str, ids: list[str]) -> dict[str, dict]:
    """{id: {label, gender}} for a roster — the UI needs both to group and name."""
    if engine == "qwen":
        return {v: dict(meta) for v, meta in tts_settings.QWEN_VOICE_CATALOGUE.items()}
    # Kokoro ids are the label (af_heart, am_michael…) — they're the vocabulary
    # the docs and the community use, so renaming them would only obscure them.
    return {
        v: {"label": v, "gender": tts_settings.kokoro_gender(v) or "female"}
        for v in ids
    }


def _speed_field(engine: str) -> str:
    return "qwen_speed" if engine == "qwen" else "kokoro_speed"


def _config_response() -> TTSConfigResponse:
    engine, edition = _engine()
    s = tts_settings.current()
    ids = tts_previews.roster(engine)
    meta = _labels(engine, ids)
    have = tts_previews.list_previews().get(engine, {})

    voices = [
        VoiceOption(
            id=v,
            label=meta.get(v, {}).get("label", v),
            gender=meta.get(v, {}).get("gender", "female"),
            has_preview=v in have,
        )
        for v in ids
        # A voice with no readable gender can't be placed in either slot, so
        # offering it would be offering an unselectable option.
        if meta.get(v, {}).get("gender") in tts_settings.GENDERS
    ]
    pair = tts_settings.voice_pair(engine, s) or ("", "")

    return TTSConfigResponse(
        sample_text=tts_previews.SAMPLE_TEXT,
        engine=engine,
        edition=edition,
        female=pair[0],
        male=pair[1],
        okuro_gender=s.okuro_gender,
        speed=getattr(s, _speed_field(engine)),
        fx_enabled=bool((s.voice_fx or {}).get("enabled")),
        voices=voices,
        previews=PreviewStatus(**tts_previews.preview_status(engine)),
        speed_min=tts_settings.SPEED_MIN,
        speed_max=tts_settings.SPEED_MAX,
    )


# ── GETs ────────────────────────────────────────────────────────────────
@router.get("/config", response_model=TTSConfigResponse)
def get_config() -> TTSConfigResponse:
    """This tier's engine, its voice roster, the current pair + preview readiness."""
    return _config_response()


@router.get("/preview/{engine}/{voice}")
def get_preview(engine: str, voice: str) -> FileResponse:
    """Serve a voice's ``SAMPLE_TEXT`` preview mp3.

    Kokoro previews render lazily on first request (CPU, ~1s). Qwen previews are
    NOT rendered here — the batch is minutes long; ``POST /previews`` starts it.
    404 while a clip is absent.
    """
    engine = engine.strip().lower()
    if engine == "kokoro":
        tts_previews.ensure_kokoro_previews()  # lazy: fills any missing clip
    path = tts_previews.preview_path(engine, voice)
    if not path.exists():
        raise HTTPException(404, f"no preview for {engine}/{voice}")
    return FileResponse(str(path), media_type="audio/mpeg", filename=path.name)


# ── mutate ──────────────────────────────────────────────────────────────
@router.post("/previews", response_model=PreviewStatus)
def post_previews(request: Request) -> PreviewStatus:
    """Start a background render of this tier's missing previews. Idempotent.

    Localhost-only: it burns a GPU load. Returns immediately — poll
    ``GET /config``. Qwen renders its whole roster on ONE model load, because the
    load is the cost (~47s) and paying it per voice would be ~7 minutes.
    """
    _require_localhost(request)
    engine, _ = _engine()
    started = tts_previews.ensure_previews_async(engine)
    if started:
        logger.info("voice previews: %s batch started", engine)
    return PreviewStatus(**tts_previews.preview_status(engine))


@router.put("/config", response_model=TTSConfigResponse)
def put_config(update: TTSConfigUpdate, request: Request) -> TTSConfigResponse:
    """Persist the pair / okuro's gender / pace / fx switch to ``~/.okuro/tts-config.yaml``.

    Partial: only provided fields change, and only for the ACTIVE engine — the
    tier decides the engine, so there is nothing to say about the other one.
    Validated by ``tts_settings``, which falls back rather than raising: an id of
    the wrong gender for its slot reverts to the shipped default instead of
    breaking the next brief. The response is the EFFECTIVE config, so the UI
    always shows what was actually stored.

    Takes effect on the next render.
    """
    _require_localhost(request)
    engine, _ = _engine()
    s = tts_settings.current()
    merged = s.to_dict()
    female_field, male_field, _ = tts_settings._ENGINE_PAIR[engine]
    if update.female:
        merged[female_field] = update.female
    if update.male:
        merged[male_field] = update.male
    if update.okuro_gender:
        merged["okuro_gender"] = update.okuro_gender
    if update.speed is not None:
        merged[_speed_field(engine)] = update.speed
    if update.fx_enabled is not None:
        # Flip the switch, keep the founder's tuned values. The fx block is not
        # an engine field: the chain is applied at the synth seam, so this one
        # flag governs every voice the machine renders, whatever the tier.
        fx = dict(merged.get("voice_fx") or tts_settings.VOICE_FX)
        fx["enabled"] = update.fx_enabled
        merged["voice_fx"] = fx

    try:
        validated = tts_settings._validate(merged)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    tts_settings.write(validated)
    pair = tts_settings.voice_pair(engine, validated) or ("", "")
    logger.info(
        "TTS voices set via web: engine=%s female=%s male=%s okuro=%s @%.2fx fx=%s",
        engine, pair[0], pair[1], validated.okuro_gender,
        getattr(validated, _speed_field(engine)),
        "on" if (validated.voice_fx or {}).get("enabled") else "off",
    )
    return _config_response()
