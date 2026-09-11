# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: REST API for managing ingress integrations (telegram first).
#   Exposes list/get/enable/disable + approve/revoke chat_id + token
#   write to keyring. Consumed by the Settings → Integrations panel.
# index: imports | router | models | dependencies | endpoints
# AGENT_HEADER_END -->
"""REST API for ingress integrations.

Routes mounted under ``/api/integrations``. Locked to the same bearer
auth as the rest of the okuro orchestrator API. Token writes require
an unlocked keyring session (same gate as the keyring API).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("okuro.orchestrator.api.integrations")

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

KEYRING_TOKEN_NAME = "integration/telegram/bot_token"
SUPPORTED_CHANNELS = ("telegram",)


# ── Models ───────────────────────────────────────────────────────────


class IntegrationView(BaseModel):
    channel: str
    enabled: bool
    status: str
    has_token: bool
    allowed_chat_ids: list[int] = Field(default_factory=list)
    pending_chat_id: Optional[int] = None
    last_seen_at: Optional[str] = None
    last_message_at: Optional[str] = None
    last_error: Optional[str] = None
    last_error_at: Optional[str] = None
    adapter_health: Optional[dict] = None


class TokenRequest(BaseModel):
    token: str = Field(..., min_length=10)


class EnableRequest(BaseModel):
    enabled: bool


class RevokeRequest(BaseModel):
    chat_id: int


class ChallengeView(BaseModel):
    question: str
    accepted_patterns: list[str]
    hint: Optional[str] = None


class ChallengeRequest(BaseModel):
    question: str = Field(..., min_length=2)
    accepted_patterns: list[str] = Field(..., min_length=1)
    hint: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────


def _check_channel(channel: str) -> None:
    if channel not in SUPPORTED_CHANNELS:
        raise HTTPException(404, f"unknown channel: {channel}")


def _has_token(channel: str) -> bool:
    """Best-effort check — does the keyring already hold this channel's token?"""
    try:
        from okuro.keyring.storage import KeyringStorage
        ks = KeyringStorage()
        if channel == "telegram":
            return bool(ks.get_key(KEYRING_TOKEN_NAME))
    except Exception:
        logger.debug("keyring read failed for %s", channel, exc_info=True)
    return False


def _adapter_health(channel: str) -> Optional[dict]:
    try:
        from okuro.ingress.supervisor import active_snapshot
        snap = active_snapshot()
        return snap.get("adapters", {}).get(channel)
    except Exception:
        return None


def _view(channel: str) -> IntegrationView:
    from okuro.ingress import storage as istore
    row = istore.get_integration(channel)
    if row is None:
        # Synthesize a zero-state view — frontend uses this to decide
        # whether to show "configure" vs "enable" buttons.
        return IntegrationView(
            channel=channel,
            enabled=False,
            status="stopped",
            has_token=_has_token(channel),
        )
    return IntegrationView(
        channel=row.channel,
        enabled=row.enabled,
        status=row.status,
        has_token=_has_token(row.channel),
        allowed_chat_ids=[int(x) for x in row.config.get("allowed_chat_ids") or []],
        pending_chat_id=row.config.get("pending_chat_id"),
        last_seen_at=row.last_seen_at,
        last_message_at=row.last_message_at,
        last_error=row.last_error,
        last_error_at=row.last_error_at,
        adapter_health=_adapter_health(channel),
    )


def _require_unlocked_keyring(request: Request):
    """Lift the keyring API's session guard so token writes are gated."""
    from okuro.orchestrator.api.keyring import _require_unlocked
    return _require_unlocked(request)


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("")
def list_integrations() -> list[IntegrationView]:
    return [_view(c) for c in SUPPORTED_CHANNELS]


@router.get("/{channel}")
def get_integration(channel: str) -> IntegrationView:
    _check_channel(channel)
    return _view(channel)


@router.post("/{channel}/enabled")
def set_enabled(channel: str, body: EnableRequest) -> IntegrationView:
    _check_channel(channel)
    from okuro.ingress import storage as istore
    row = istore.get_integration(channel)
    cfg = row.config if row else {}
    cfg.setdefault("allowed_chat_ids", [])
    istore.upsert_config(channel, enabled=body.enabled, config=cfg)
    return _view(channel)


@router.post("/{channel}/approve_pending")
def approve_pending(channel: str) -> IntegrationView:
    _check_channel(channel)
    from okuro.ingress import storage as istore
    row = istore.get_integration(channel)
    if row is None:
        raise HTTPException(404, "no integration row")
    cfg = dict(row.config)
    pending = cfg.pop("pending_chat_id", None)
    if pending is None:
        raise HTTPException(409, "no pending chat_id to approve")
    allowed = list({*[int(x) for x in cfg.get("allowed_chat_ids") or []], int(pending)})
    cfg["allowed_chat_ids"] = allowed
    istore.upsert_config(channel, enabled=row.enabled, config=cfg)
    return _view(channel)


@router.post("/{channel}/reject_pending")
def reject_pending(channel: str) -> IntegrationView:
    _check_channel(channel)
    from okuro.ingress import storage as istore
    row = istore.get_integration(channel)
    if row is None:
        raise HTTPException(404, "no integration row")
    cfg = dict(row.config)
    cfg.pop("pending_chat_id", None)
    istore.upsert_config(channel, enabled=row.enabled, config=cfg)
    return _view(channel)


@router.post("/{channel}/revoke")
def revoke_chat(channel: str, body: RevokeRequest) -> IntegrationView:
    _check_channel(channel)
    from okuro.ingress import storage as istore
    row = istore.get_integration(channel)
    if row is None:
        raise HTTPException(404, "no integration row")
    cfg = dict(row.config)
    cfg["allowed_chat_ids"] = [
        int(x) for x in cfg.get("allowed_chat_ids") or [] if int(x) != int(body.chat_id)
    ]
    istore.upsert_config(channel, enabled=row.enabled, config=cfg)
    return _view(channel)


@router.post("/{channel}/token")
def set_token(channel: str, body: TokenRequest, request: Request) -> IntegrationView:
    """Write the bot token into the okuro keyring. Requires an unlocked
    keyring session (same gate as the keyring API)."""
    _check_channel(channel)
    ks = _require_unlocked_keyring(request)
    if channel == "telegram":
        ks.set_key(KEYRING_TOKEN_NAME, body.token.strip())
    return _view(channel)


@router.delete("/{channel}/token")
def delete_token(channel: str, request: Request) -> IntegrationView:
    _check_channel(channel)
    ks = _require_unlocked_keyring(request)
    if channel == "telegram":
        ks.delete_key(KEYRING_TOKEN_NAME)
    return _view(channel)


# ── Challenge bank ───────────────────────────────────────────────────


@router.get("/{channel}/challenges")
def list_challenges(channel: str, request: Request) -> list[ChallengeView]:
    """Reading challenges reveals the questions — gate on keyring unlock."""
    _check_channel(channel)
    ks = _require_unlocked_keyring(request)
    from okuro.ingress import security as sec
    return [
        ChallengeView(
            question=c.question,
            accepted_patterns=c.accepted_patterns,
            hint=c.hint,
        )
        for c in sec.load_bank(channel, store=ks)
    ]


@router.post("/{channel}/challenges")
def add_challenge(channel: str, body: ChallengeRequest, request: Request) -> list[ChallengeView]:
    _check_channel(channel)
    ks = _require_unlocked_keyring(request)
    from okuro.ingress import security as sec
    bank = list(sec.load_bank(channel, store=ks))
    bank.append(sec.Challenge(
        question=body.question.strip(),
        accepted_patterns=[p.strip() for p in body.accepted_patterns if p.strip()],
        hint=(body.hint or "").strip() or None,
    ))
    sec.save_bank(channel, bank, store=ks)
    return [
        ChallengeView(
            question=c.question,
            accepted_patterns=c.accepted_patterns,
            hint=c.hint,
        )
        for c in bank
    ]


@router.delete("/{channel}/challenges/{idx}")
def delete_challenge(channel: str, idx: int, request: Request) -> list[ChallengeView]:
    _check_channel(channel)
    ks = _require_unlocked_keyring(request)
    from okuro.ingress import security as sec
    bank = list(sec.load_bank(channel, store=ks))
    if idx < 0 or idx >= len(bank):
        raise HTTPException(404, "challenge index out of range")
    bank.pop(idx)
    sec.save_bank(channel, bank, store=ks)
    return [
        ChallengeView(
            question=c.question,
            accepted_patterns=c.accepted_patterns,
            hint=c.hint,
        )
        for c in bank
    ]
