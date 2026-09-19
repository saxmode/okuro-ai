# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Models API — list AI models from the okuro.ai_models registry.
# index:
#   imports
#   router
#   models
#   def list_models_endpoint
#   def inventory_endpoint
#   def consumers_endpoint
#   def swaps_endpoint / propose / show / tick / apply / reject
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


@router.get("/inventory")
def inventory_endpoint(
    store: Optional[str] = None,
    format: Optional[str] = None,
    status: Optional[str] = None,
    refresh: Optional[str] = None,
    twins: bool = False,
    limit: int = 1000,
) -> dict:
    """Unit inventory of the host's configured model stores.

    Default is the cached table plus ``last_scanned`` per store, so opening
    the page costs one query. ``refresh=stat`` runs the live dir-stat walk
    first and merges the deltas — that mode never opens a model file, which is
    what makes it safe to run against the cold store on a page load.
    ``refresh=identity`` additionally fingerprints units and is a
    background/manual job, not a page load.

    Not configured is its own answer: ``{"configured": false, "reason": ...}``
    with HTTP 200, because an okuro with no local model store is a supported
    install, not an error.
    """
    from okuro.ai_models import store_scan

    if refresh is not None and refresh not in store_scan.MODES:
        raise HTTPException(400, f"refresh must be one of {store_scan.MODES}")
    try:
        return store_scan.inventory(
            store=store, fmt=format, status=status,
            refresh_mode=refresh, want_twins=twins,
            limit=max(1, min(int(limit), 10_000)),
        )
    except Exception as exc:  # a store walk must never 500 the models page
        logger.exception("model inventory failed")
        raise HTTPException(500, f"inventory failed: {exc}") from exc


@router.get("/consumers")
def consumers_endpoint(
    unit: Optional[str] = None,
    consumer: Optional[str] = None,
    tier: Optional[str] = None,
    dead: bool = False,
    unreachable: bool = False,
    unreferenced: bool = False,
    refresh: bool = False,
    limit: int = 1000,
) -> dict:
    """Which tool on this host uses which model, with the proving ``file:line``.

    Default is the cached table, so opening the page costs one query.
    ``refresh=true`` re-walks the declared consumer roots and merges the
    deltas; that walk reads config files and runs ``systemctl``/``docker``
    state queries, so it is a button, not a page load.

    Not configured is its own answer: ``{"configured": false, "reason": ...}``
    with HTTP 200 — an okuro with no declared consumers is a supported
    install, not an error, and "nothing is configured" must never render as
    "nothing uses any model".
    """
    from okuro.ai_models import consumers as consumer_scan

    if tier is not None and tier not in consumer_scan.TIERS:
        raise HTTPException(400, f"tier must be one of {consumer_scan.TIERS}")
    try:
        return consumer_scan.consumer_map(
            unit=unit, consumer=consumer, tier=tier, dead=dead,
            unreachable=unreachable, refresh=refresh,
            unreferenced=unreferenced,
            limit=max(1, min(int(limit), 10_000)),
        )
    except Exception as exc:  # a consumer walk must never 500 the models page
        logger.exception("model consumer map failed")
        raise HTTPException(500, f"consumer map failed: {exc}") from exc


@router.get("/fit")
def fit_endpoint(
    unit: Optional[str] = None,
    release: Optional[str] = None,
    size_gb: Optional[float] = None,
    now: bool = False,
    ctx: Optional[int] = None,
) -> dict:
    """Where on this host's hardware a model could run — the placement space.

    Not one GPU's verdict. Ruling 6: a model that does not fit one card may
    still run tensor-split across two, or with its cold experts in system RAM,
    so the answer is the set of feasible placements ranked fast to slow, and
    ``runnable_idle`` is the OR over that set.

    ``now=true`` also decides against what is free on the GPUs at this instant,
    taken from okuro's broker (ledger plus live nvidia-smi). A placement that
    fits idle and not now is a queue, not a refusal.

    Exactly one of ``unit`` or ``release``.
    """
    from okuro.ai_models import fitting

    if bool(unit) == bool(release):
        raise HTTPException(400, "pass exactly one of unit= or release=")
    try:
        hw = fitting.detect_hardware(now=now)
        if unit:
            return fitting.fit_unit(unit, hw, now=now, context=ctx)
        return fitting.fit_release(release, hw, now=now, size_gb=size_gb,
                                   context=ctx)
    except Exception as exc:  # a fit must never 500 the models page
        logger.exception("model fit failed")
        raise HTTPException(500, f"fit failed: {exc}") from exc


@router.get("/lineage")
def lineage_endpoint(
    unit: Optional[str] = None,
    release: Optional[str] = None,
    include_unrelated: bool = False,
) -> dict:
    """What line a model is from, and how it relates to what is installed.

    For a ``release`` this is the question the discovery feed could not answer:
    is this an update to something already here, another quant of it, a variant
    of it, or nothing to do with it. An empty relation list means no installed
    unit of that family — which is NOT the same as unrelated.
    """
    from okuro.ai_models import lineage as lin

    if bool(unit) == bool(release):
        raise HTTPException(400, "pass exactly one of unit= or release=")
    try:
        if unit:
            return lin.unit_lineage(unit)
        return lin.relate_release(release, include_unrelated=include_unrelated)
    except Exception as exc:  # a name parse must never 500 the models page
        logger.exception("model lineage failed")
        raise HTTPException(500, f"lineage failed: {exc}") from exc


@router.get("/swaps")
def swaps_endpoint(
    consumer: Optional[str] = None,
    state: Optional[str] = None,
    unit: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """Every swap plan — proposed line edits that nothing executes.

    A plan is one row per config LINE a replacement would touch, each with the
    rewritten reference in the shape that line already uses and a unified diff
    of that one line. okuro never edits a consumer config: ``apply`` records
    that a person applied it and returns the patch.

    Not configured is its own answer: ``{"configured": false, "reason": ...}``
    with HTTP 200 — a host with no declared consumer roots has nothing to plan
    against, which is not an error.
    """
    from okuro.ai_models import swap

    if state is not None and state not in swap.STATES:
        raise HTTPException(400, f"state must be one of {swap.STATES}")
    try:
        return swap.list_plans(consumer=consumer, state=state, unit=unit,
                               limit=max(1, min(int(limit), 500)))
    except Exception as exc:  # a plan list must never 500 the models page
        logger.exception("swap plan list failed")
        raise HTTPException(500, f"swap list failed: {exc}") from exc


@router.post("/swaps")
def swaps_propose_endpoint(payload: dict) -> dict:
    """Propose a swap: ``{consumer, unit_old, new, mode?, kind?, note?}``."""
    from okuro.ai_models import swap

    body = payload or {}
    missing = [k for k in ("consumer", "unit_old", "new") if not body.get(k)]
    if missing:
        raise HTTPException(400, f"missing: {', '.join(missing)}")
    mode = body.get("mode") or "replace"
    if mode not in swap.MODES:
        raise HTTPException(400, f"mode must be one of {swap.MODES}")
    try:
        return swap.propose(str(body["consumer"]), str(body["unit_old"]),
                            str(body["new"]), mode=mode, kind=body.get("kind"),
                            note=body.get("note"))
    except Exception as exc:
        logger.exception("swap propose failed")
        raise HTTPException(500, f"propose failed: {exc}") from exc


@router.get("/swaps/{plan_group}")
def swap_show_endpoint(plan_group: str) -> dict:
    """One plan, with its checklist, its blockers and every diff."""
    from okuro.ai_models import swap

    res = swap.show(plan_group)
    if not res.get("ok"):
        raise HTTPException(404, res.get("reason") or "no such plan")
    return res


@router.post("/swaps/{plan_group}/tick")
def swap_tick_endpoint(plan_group: str, payload: dict) -> dict:
    """Tick one checklist item: ``{item: <1-based index>}``."""
    from okuro.ai_models import swap

    item = (payload or {}).get("item")
    if item is None:
        raise HTTPException(400, "item (1-based index) required")
    return swap.tick(plan_group, int(item))


@router.post("/swaps/{plan_group}/apply")
def swap_apply_endpoint(plan_group: str) -> dict:
    """Mark a plan applied and return the patch. It edits NO consumer file.

    Refused with ``ok: false`` while any checklist item is unticked or any
    blocker is unresolved — a refusal is a state of the plan, not an HTTP
    error, so the page renders the reason beside the gate it belongs to.
    """
    from okuro.ai_models import swap

    return swap.apply(plan_group)


@router.post("/swaps/{plan_group}/reject")
def swap_reject_endpoint(plan_group: str, payload: dict) -> dict:
    """Close a plan without applying it: ``{why: "..."}``."""
    from okuro.ai_models import swap

    why = (payload or {}).get("why") or ""
    return swap.reject(plan_group, str(why))


# NOTE: every literal route above this line. The catch-all below matches any
# single path segment, so a route registered after it is unreachable.
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
