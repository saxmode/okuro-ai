# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Bridge API — provider status, routing table, and try-it-out invoke.
# index:
#   imports
#   router
#   models
#   def _require_localhost_br
#   def get_status
#   def get_providers
#   def get_usage_summary
#   def invoke_bridge
# AGENT_HEADER_END -->
"""Bridge API — surface the okuro.bridge subsystem to the web UI.

Wraps ``okuro.bridge`` so the SPA can:
- show which CLIs are installed/available (claude, gemini, codex, local)
- show the capability routing table (analysis→gemini, code→claude, ...)
- run a short try-it-out invocation with a hard timeout cap

The try-it-out is the ONLY mutating/expensive endpoint and is
localhost-guarded. Status and routing are read-only and unauth'd.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("okuro.orchestrator.api.bridge")

router = APIRouter(prefix="/api/bridge", tags=["bridge"])


# ── Helpers ──────────────────────────────────────────────────────────


def _require_localhost_br(request: Request) -> None:
    from okuro.orchestrator.api.main import _require_loopback
    _require_loopback(request)


# Hard cap on try-it-out so a stuck CLI can't hold the worker forever.
_INVOKE_TIMEOUT_MAX = 60


# ── Models ───────────────────────────────────────────────────────────


class ProviderStatus(BaseModel):
    id: str
    type: str  # "cli" | "local-http"
    available: bool
    models: dict[str, str] = {}
    capabilities: list[str] = []
    default_timeout: int = 300


class BridgeStatusResponse(BaseModel):
    status: str  # "ok" | "degraded"
    providers: list[ProviderStatus]
    total: int
    available: int


class RoutingResponse(BaseModel):
    providers: list[ProviderStatus]
    routing: dict[str, str]


class UsageByProvider(BaseModel):
    count: int
    total_duration: float


class UsageResponse(BaseModel):
    period: str
    invocations: int
    by_provider: dict[str, UsageByProvider]


class InvokeRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    capability: Optional[str] = None
    provider: Optional[str] = None
    timeout: Optional[int] = None  # capped at _INVOKE_TIMEOUT_MAX


class InvokeResponse(BaseModel):
    success: bool
    output: str
    provider: str
    model: str
    duration: float
    latency_ms: int
    error: Optional[str] = None


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("/status", response_model=BridgeStatusResponse)
def get_status() -> BridgeStatusResponse:
    """Per-provider health + overall bridge status."""
    from okuro.bridge import list_providers

    provs = list_providers()
    rows = [ProviderStatus(**p) for p in provs]
    available = sum(1 for p in rows if p.available)
    status = "ok" if available > 0 else "degraded"
    return BridgeStatusResponse(
        status=status,
        providers=rows,
        total=len(rows),
        available=available,
    )


@router.get("/providers", response_model=RoutingResponse)
def get_providers() -> RoutingResponse:
    """Providers + routing table — which capability goes to which provider."""
    from okuro.bridge import list_providers, get_routing_table

    rows = [ProviderStatus(**p) for p in list_providers()]
    return RoutingResponse(providers=rows, routing=get_routing_table())


@router.get("/usage", response_model=UsageResponse)
def get_usage_summary(period: str = "week") -> UsageResponse:
    """Invocation counts + total duration by provider for a period."""
    from okuro.bridge.tracker import get_usage

    data = get_usage(period=period)
    by = {
        k: UsageByProvider(count=v["count"], total_duration=v["total_duration"])
        for k, v in data.get("by_provider", {}).items()
    }
    return UsageResponse(
        period=data.get("period", period),
        invocations=data.get("invocations", 0),
        by_provider=by,
    )


@router.post("/invoke", response_model=InvokeResponse)
def invoke_bridge(payload: InvokeRequest, request: Request) -> InvokeResponse:
    """Try-it-out: send a short prompt through the bridge.

    Localhost-only — this subprocesses the user's CLI. Hard timeout cap
    of 60s so a hung CLI can't block the worker indefinitely. Errors
    from the underlying CLI (e.g. gemini's broken arg construction)
    surface inline rather than raising, so the UI can render them.
    """
    _require_localhost_br(request)

    from okuro.bridge.invoke import invoke

    timeout = payload.timeout or _INVOKE_TIMEOUT_MAX
    timeout = max(5, min(timeout, _INVOKE_TIMEOUT_MAX))

    try:
        result = invoke(
            prompt=payload.prompt,
            capability=payload.capability,
            provider=payload.provider,
            timeout=timeout,
        )
    except ValueError as exc:
        # Unknown provider / capability — 400, not 500.
        raise HTTPException(400, str(exc))
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("bridge invoke failed")
        raise HTTPException(500, f"Bridge invoke failed: {exc}")

    duration = float(result.get("duration", 0.0))
    return InvokeResponse(
        success=bool(result.get("success", False)),
        output=str(result.get("output", "")),
        provider=str(result.get("provider", payload.provider or "?")),
        model=str(result.get("model", "")),
        duration=duration,
        latency_ms=int(duration * 1000),
        error=result.get("error") or None,
    )
