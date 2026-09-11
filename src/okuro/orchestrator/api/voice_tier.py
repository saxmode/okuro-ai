# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: /api/voice/stt/* — read the STT tier catalogue (labels + who-it's-for
#   descriptions) and let the user pick a tier from Settings. Mirrors the embed
#   tier API; all descriptions come from voice.tier.TIERS (single source of
#   truth), never duplicated here.
# index:
#   response models
#   _require_localhost / _serialize
#   GET /tiers  GET /config  PUT /tier
# AGENT_HEADER_END -->
"""STT tier settings API.

Read-only ``GET /tiers`` is safe from any LAN client (it only exposes hardware
capability + static copy). The mutating ``PUT /tier`` is localhost-only, matching
the embed / keyring / reindex policy — a tier change rewrites a file under
``~/.okuro`` and should not be driveable from another host on the LAN.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from okuro.voice import tier as stt_tier

logger = logging.getLogger("okuro.orchestrator.api.voice_tier")

router = APIRouter(prefix="/api/voice/stt", tags=["voice"])


# ── response models ─────────────────────────────────────────────────────
class STTTierOption(BaseModel):
    tier: str
    label: str
    stream_model: str
    batch_model: str
    needs_gpu: bool
    characteristics: str
    best_for: str


class STTTiersResponse(BaseModel):
    tiers: list[STTTierOption]
    recommended: str  # highest tier this hardware can run
    recommended_device: str  # e.g. "cuda:1" | "cpu" | "mps"
    entitled: str  # subscription ceiling (okuro-pro seam)
    effective: str  # what dictation will actually use right now
    reason: str  # env-override | settings | auto
    selected: str | None  # the user's saved choice, or null (= auto)
    stream_backend: str  # human string of the resolved streaming backend
    batch_backend: str


class STTTierUpdate(BaseModel):
    tier: str


class STTTierResult(BaseModel):
    effective: str
    reason: str
    stream_backend: str
    batch_backend: str


def _require_localhost(request: Request) -> None:
    client = request.client
    host = client.host if client else None
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(403, "STT tier writes are localhost-only")


def _tiers_response() -> STTTiersResponse:
    detection = stt_tier._detection()
    effective, reason = stt_tier.resolve_tier(detection)
    from okuro.capability import recommended_device

    saved = stt_tier.load()
    stream_b = stt_tier.resolve_stt("stream")
    batch_b = stt_tier.resolve_stt("batch")
    return STTTiersResponse(
        tiers=[
            STTTierOption(
                tier=s.tier,
                label=s.label,
                stream_model=s.stream_model,
                batch_model=s.batch_model,
                needs_gpu=s.needs_gpu,
                characteristics=s.characteristics,
                best_for=s.best_for,
            )
            for s in stt_tier.TIERS.values()
        ],
        recommended=stt_tier.hardware_tier(detection),
        recommended_device=recommended_device(detection),
        entitled=stt_tier.entitled_tier(),
        effective=effective,
        reason=reason,
        selected=saved.tier if saved else None,
        stream_backend=stream_b.describe(),
        batch_backend=batch_b.describe(),
    )


# ── GETs ────────────────────────────────────────────────────────────────
@router.get("/tiers", response_model=STTTiersResponse)
def list_tiers() -> STTTiersResponse:
    """Static tier catalogue + this host's capability + the effective choice."""
    return _tiers_response()


@router.get("/config", response_model=STTTiersResponse)
def get_config() -> STTTiersResponse:
    """Alias of /tiers — the Settings page reads one combined payload."""
    return _tiers_response()


# ── mutate ──────────────────────────────────────────────────────────────
@router.put("/tier", response_model=STTTierResult)
def put_tier(update: STTTierUpdate, request: Request) -> STTTierResult:
    """Persist the user's tier choice to ``~/.okuro/stt-config.yaml``.

    Clamped to the subscription entitlement — the UI never offers a tier above
    it, and this is the server-side backstop. Takes effect on the next dictation
    (models reload lazily against the new resolved backend; no restart).
    """
    _require_localhost(request)
    choice = (update.tier or "").strip().lower()
    if choice not in stt_tier.TIERS:
        raise HTTPException(422, f"invalid tier {choice!r} — one of {list(stt_tier.TIERS)}")

    ceiling = stt_tier.entitled_tier()
    if stt_tier._rank(choice) > stt_tier._rank(ceiling):  # type: ignore[arg-type]
        raise HTTPException(403, f"tier {choice!r} exceeds your entitlement ({ceiling})")

    cfg = stt_tier.load() or stt_tier.STTConfigFile()
    cfg.tier = choice  # type: ignore[assignment]
    cfg.chosen_by = "web"
    cfg.last_detection = stt_tier._detection()
    stt_tier.write(cfg)
    logger.info("STT tier set to %s via web settings", choice)

    effective, reason = stt_tier.resolve_tier()
    return STTTierResult(
        effective=effective,
        reason=reason,
        stream_backend=stt_tier.resolve_stt("stream").describe(),
        batch_backend=stt_tier.resolve_stt("batch").describe(),
    )
