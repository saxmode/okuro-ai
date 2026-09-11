# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Models API — list AI models from the okuro.ai_models registry.
# index:
#   imports
#   router
#   models
#   def list_models_endpoint
#   def get_model_endpoint
# AGENT_HEADER_END -->
"""Models API — surface the okuro.ai_models registry to the web UI.

Returns local GGUF files (scanned from the configured model-store dirs) plus
the subscription-model mapping declared in the bridge config. Shape is
intentionally stable so the /models page can render without guessing:
each row has provider, tier (fast|standard|quality), source, and any
capability tags the bridge config advertises.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("okuro.orchestrator.api.models_api")

router = APIRouter(prefix="/api/models", tags=["models"])


# ── Models ───────────────────────────────────────────────────────────


class ModelEntry(BaseModel):
    id: str  # unique id = alias (provider/tier for subscriptions, filename stem for local)
    name: str  # human-readable name (e.g. "sonnet", "qwen2-5-coder-7b-instruct-q4_k_m")
    alias: str
    source: str  # "local-gguf" | "subscription"
    provider: Optional[str] = None  # "claude" | "antigravity" | "codex" | "local"
    tier: Optional[str] = None  # "fast" | "standard" | "quality"
    path: Optional[str] = None
    size_gb: float = 0.0
    parameters: Optional[str] = None  # "7B", "32B"
    quantization: Optional[str] = None  # "Q4_K_M"
    capabilities: list[str] = []
    prompt_ready: bool = False  # bundle carries an okuro.prompting/v1 block
    prompt_syntax: Optional[str] = None  # chat_template | booru_tags | natural_language


class ModelListResponse(BaseModel):
    models: list[ModelEntry]
    by_source: dict[str, int]
    by_provider: dict[str, int]
    total: int


# ── Helpers ──────────────────────────────────────────────────────────


def _enrich(entry: dict) -> ModelEntry:
    """Add provider+tier to a raw registry dict.

    For subscriptions the alias is ``<provider>/<tier>``; we split that
    back out so the UI can group by provider and sort by tier without
    hand-parsing.
    """
    alias = entry.get("alias") or ""
    provider: Optional[str] = None
    tier: Optional[str] = None
    source = entry.get("source") or ""

    if source == "subscription" and "/" in alias:
        provider, _, tier = alias.partition("/")
    elif source == "local-gguf":
        provider = "local"
        # tier stays None — local GGUFs don't advertise a tier until the
        # bridge config picks one (that mapping lives in bridge/config.py)

    return ModelEntry(
        id=alias or entry.get("name", ""),
        name=entry.get("name", ""),
        alias=alias,
        source=source,
        provider=provider,
        tier=tier,
        path=entry.get("path"),
        size_gb=float(entry.get("size_gb") or 0.0),
        parameters=entry.get("parameters"),
        quantization=entry.get("quantization"),
        capabilities=list(entry.get("capabilities") or []),
        prompt_ready=bool(entry.get("prompt_ready")),
        prompt_syntax=entry.get("prompt_syntax"),
    )


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("", response_model=ModelListResponse)
def list_models_endpoint(
    source: Optional[str] = None,
    provider: Optional[str] = None,
) -> ModelListResponse:
    """List every known model — local GGUFs + subscription models.

    Optional ``source`` and ``provider`` filters short-circuit the
    response without re-scanning (scan always happens once per request).
    """
    from okuro.ai_models import list_models

    raw = list_models()
    rows = [_enrich(m) for m in raw]

    if source:
        rows = [r for r in rows if r.source == source]
    if provider:
        rows = [r for r in rows if r.provider == provider]

    by_source: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    for r in rows:
        by_source[r.source] = by_source.get(r.source, 0) + 1
        p = r.provider or "unknown"
        by_provider[p] = by_provider.get(p, 0) + 1

    return ModelListResponse(
        models=rows,
        by_source=by_source,
        by_provider=by_provider,
        total=len(rows),
    )


@router.get("/{model_id:path}", response_model=ModelEntry)
def get_model_endpoint(model_id: str) -> ModelEntry:
    """Detail for a single model by alias (e.g. ``claude/standard``)."""
    from okuro.ai_models import list_models

    for m in list_models():
        alias = m.get("alias") or ""
        name = m.get("name") or ""
        if alias == model_id or name == model_id:
            return _enrich(m)
    raise HTTPException(404, f"Unknown model: {model_id}")
