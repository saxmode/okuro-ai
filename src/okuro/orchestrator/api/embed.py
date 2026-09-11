# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Embed-tier API — config read/write, detection, switch + reindex.
# index:
#   imports
#   router
#   class EmbedConfigResponse
#   class EmbedConfigUpdate
#   class HardwareDetection
#   class TierOption
#   class TierOptions
#   class SwitchTierResponse
#   class ReindexEstimate
#   def _require_localhost
#   def _serialize_config
#   def get_config
#   def detect_hw
#   def list_tiers
#   def estimate_switch
#   def put_config
#   def get_embed_health
# AGENT_HEADER_END -->
"""Embed-tier API surface — backs the onboarding step and the web
``/settings/embed`` page.

Endpoints:

| Method | Path                              | Purpose                              |
|--------|-----------------------------------|--------------------------------------|
| GET    | /api/embed/config                 | current persisted tier + device      |
| GET    | /api/embed/detect                 | live hardware detection snapshot     |
| GET    | /api/embed/tiers                  | static tier registry (id, dim, ...)  |
| GET    | /api/embed/switch-estimate        | files affected + dim delta + ETA     |
| PUT    | /api/embed/config                 | persist new tier/device + reindex    |
| GET    | /api/embed/health                 | proxy to embed service /health       |

PUT is localhost-only — the same policy as /api/cortex/reindex and
/api/keyring/* — because it can drop the vec table and trigger a long
re-embed. GET endpoints are unauthenticated (read-only, no PII).
"""

from __future__ import annotations

import logging
import os
import time
import urllib.request
import json
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("okuro.orchestrator.api.embed")

router = APIRouter(prefix="/api/embed", tags=["embed"])


# ── Models ────────────────────────────────────────────────────────────


class EmbedConfigResponse(BaseModel):
    tier: str
    device: str
    chosen_at: str
    chosen_by: str
    last_detection: dict


class EmbedConfigUpdate(BaseModel):
    tier: str = Field(..., description="low | high")
    device: str = Field(
        "auto",
        description="auto | cpu | mps | cuda:N",
    )
    chosen_by: str = Field(
        "web",
        description="onboarding | web | cli (auto is reserved for the system)",
    )


class HardwareDetection(BaseModel):
    system: str
    display_gpu_index: Optional[int]
    display_gpu_indices: list[int] = Field(default_factory=list)
    gpus: list[dict]
    cpu_cores: int
    ram_gb: float


class TierOption(BaseModel):
    tier: str
    model_id: str
    dim: int
    description: str


class TierOptions(BaseModel):
    tiers: list[TierOption]
    recommended: str
    recommended_device: str


class ReindexEstimate(BaseModel):
    current_tier: str
    new_tier: str
    current_dim: int
    new_dim: int
    dim_changed: bool
    files_affected: int
    eta_seconds: Optional[int]
    search_disabled_until_done: bool


class SwitchTierResponse(BaseModel):
    tier: str
    device: str
    chosen_at: str
    chosen_by: str
    reindex_required: bool
    reindex_job_id: Optional[str] = None


# ── Localhost gate ────────────────────────────────────────────────────


def _require_localhost(request: Request) -> None:
    """Match the policy used by /api/cortex/reindex + /api/keyring/*."""
    client = request.client
    host = client.host if client else None
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(403, "embed config writes are localhost-only")


def _serialize_config(cfg) -> EmbedConfigResponse:
    return EmbedConfigResponse(
        tier=cfg.tier,
        device=cfg.device,
        chosen_at=cfg.chosen_at,
        chosen_by=cfg.chosen_by,
        last_detection=cfg.last_detection or {},
    )


# ── GETs ──────────────────────────────────────────────────────────────


@router.get("/config", response_model=EmbedConfigResponse)
def get_config() -> EmbedConfigResponse:
    """Return the persisted config, or run-and-write the recommendation."""
    from okuro.embed import config as embed_cfg

    cfg = embed_cfg.load_or_init()
    return _serialize_config(cfg)


@router.get("/detect", response_model=HardwareDetection)
def detect_hw() -> HardwareDetection:
    """Live re-run of capability detection (no side effects)."""
    from okuro.embed import detect as embed_detect

    snap = embed_detect.capabilities()
    return HardwareDetection(**snap)


@router.get("/tiers", response_model=TierOptions)
def list_tiers() -> TierOptions:
    """Static tier registry + recommendation derived from current hardware."""
    from okuro.embed import config as embed_cfg
    from okuro.embed import detect as embed_detect

    detection = embed_detect.capabilities()
    tiers = [
        TierOption(
            tier=spec.tier,
            model_id=spec.model_id,
            dim=spec.dim,
            description=spec.description,
        )
        for spec in embed_cfg.TIERS.values()
    ]
    return TierOptions(
        tiers=tiers,
        recommended=embed_detect.recommended_tier(detection),
        recommended_device=embed_detect.recommended_device(detection),
    )


@router.get("/switch-estimate", response_model=ReindexEstimate)
def estimate_switch(tier: str) -> ReindexEstimate:
    """Surface the warning-modal contents for a hypothetical switch.

    Caller passes the target tier id; we look up the current tier from
    the persisted config + cortex_meta, count the docs that would need
    re-embedding, and produce a coarse ETA from a per-tier throughput
    table (no live benchmark — keeps the modal snappy).
    """
    from okuro.cortex.vectorstore import cortex_meta_get
    from okuro.db import get_db
    from okuro.embed import config as embed_cfg

    if tier not in embed_cfg.TIERS:
        raise HTTPException(400, f"unknown tier {tier!r}")

    cfg = embed_cfg.load_or_init()
    db = get_db()
    current_dim_str = cortex_meta_get(db, "embedding_dim") or str(cfg.dim)
    current_dim = int(current_dim_str)
    new_spec = embed_cfg.TIERS[tier]
    dim_changed = current_dim != new_spec.dim

    files_row = db.fetchone(
        "SELECT COUNT(*) AS n FROM cortex_docs "
        "WHERE doc_type = 'header' AND deleted_at IS NULL"
    )
    files_affected = int(files_row["n"] if files_row else 0) if dim_changed else 0

    # Coarse throughput estimates. cuda:N high-tier ~120 docs/sec, CPU
    # low-tier ~40 docs/sec on commodity laptop. Off by a constant
    # factor is acceptable here — modal copy says "estimated", not
    # "guaranteed".
    if not dim_changed or files_affected == 0:
        eta = 0
    else:
        per_sec = 120 if cfg.device.startswith("cuda:") else 40
        eta = max(5, int(files_affected / per_sec))

    return ReindexEstimate(
        current_tier=cfg.tier,
        new_tier=tier,
        current_dim=current_dim,
        new_dim=new_spec.dim,
        dim_changed=dim_changed,
        files_affected=files_affected,
        eta_seconds=eta if dim_changed else 0,
        search_disabled_until_done=dim_changed,
    )


# ── PUT (localhost-only mutator) ──────────────────────────────────────


def _drop_vec_for_dim_change() -> None:
    """Drop vec_cortex + clear cortex_docs so the next reindex re-embeds."""
    from okuro.db import get_db
    db = get_db()
    with db.write():
        db.execute("DROP TABLE IF EXISTS vec_cortex")
        db.execute("DELETE FROM cortex_docs")


def _repair_vec_dims_when_ready(target_dim: int, timeout_s: float = 120.0) -> None:
    """Wait for the embed service to come back at the new dim, then realign.

    Polls /health until either the reported dimension matches ``target_dim``
    or ``timeout_s`` elapses. Runs in a background thread spawned by the
    PUT /api/embed/config handler — the user's request returns immediately;
    the re-embed happens out-of-band.
    """
    import urllib.request
    from okuro.system.port_registry import embed_url

    deadline = time.time() + timeout_s
    health_url = embed_url().rstrip("/") + "/health"
    ready = False
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=2) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("loaded") and payload.get("dimensions") == target_dim:
                ready = True
                break
        except Exception:
            pass
        time.sleep(2)

    if not ready:
        logger.warning(
            "embed service not ready at dim=%d after %.0fs — vec_* tables "
            "stay misaligned until manual `python -m okuro.embed.repair`",
            target_dim, timeout_s,
        )
        return

    try:
        from okuro.embed.repair import ensure_vec_dims
        result = ensure_vec_dims()
        repaired = sum(1 for t in result["tables"] if "backfilled" in t)
        logger.info("post-switch ensure_vec_dims: %d table(s) realigned", repaired)
    except Exception as exc:  # noqa: BLE001
        logger.warning("post-switch ensure_vec_dims failed: %s", exc)


def _enqueue_reindex_after_switch(new_dim: int, new_tier: str) -> Optional[str]:
    """Reuse the cortex /reindex pipeline to rebuild the index post-switch."""
    from okuro.cortex.vectorstore import cortex_meta_upsert
    from okuro.db import get_db
    from okuro.cortex.roots import registered_roots
    from okuro.orchestrator.api import cortex as cortex_api
    import secrets as _secrets

    cortex_meta_upsert(get_db(), "embedding_tier", new_tier)
    cortex_meta_upsert(get_db(), "embedding_dim", str(new_dim))

    roots = registered_roots()
    if not roots:
        return None
    target = roots[0].path
    project = roots[0].project
    job_id = _secrets.token_urlsafe(8)
    started = time.time()
    with cortex_api._REINDEX_LOCK:
        cortex_api._REINDEX_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "started_at": started,
            "finished_at": None,
            "total": None,
            "indexed": None,
            "indexed_so_far": 0,
            "error": None,
            "root": str(target),
            "project": project,
        }
    # Schedule on the existing reindex worker. If multiple roots are
    # registered we reindex the first one synchronously here and let the
    # daemon's refresh_cortex pick up the rest on its next tick — the
    # alternative (chained background tasks) loses error visibility.
    import threading
    threading.Thread(
        target=cortex_api._run_reindex,
        args=(job_id, target, project),
        daemon=True,
    ).start()
    return job_id


@router.put("/config", response_model=SwitchTierResponse)
def put_config(
    request: Request,
    body: EmbedConfigUpdate,
) -> SwitchTierResponse:
    """Persist a new tier/device + trigger reindex if the dim changed.

    Localhost-only because it can DROP TABLE vec_cortex and kick off a
    long-running re-embed pass.
    """
    _require_localhost(request)

    from okuro.cortex.vectorstore import cortex_meta_get
    from okuro.db import get_db
    from okuro.embed import config as embed_cfg
    from okuro.embed import detect as embed_detect
    from okuro.system.install import install_all_services

    if body.tier not in embed_cfg.TIERS:
        raise HTTPException(400, f"unknown tier {body.tier!r}")
    if body.chosen_by not in {"onboarding", "web", "cli"}:
        # 'auto' is reserved — only the auto-init path may write that label.
        raise HTTPException(400, f"invalid chosen_by {body.chosen_by!r}")

    # Display-GPU safety net: PUT can pin a display GPU (the user has
    # explicitly chosen it from the picker) but we record the warning in
    # the log so support can correlate later Chrome-contention reports.
    if body.device.startswith("cuda:"):
        try:
            idx = int(body.device.split(":", 1)[1])
            if embed_detect.is_display_gpu(idx):
                logger.warning(
                    "user pinned okuro embedding to display GPU cuda:%d — "
                    "this is the configuration that caused months of "
                    "Chrome GPU contention through 2026-05-10",
                    idx,
                )
        except ValueError:
            raise HTTPException(400, f"malformed device {body.device!r}")

    detection = embed_detect.capabilities()
    new_cfg = embed_cfg.EmbedConfig(
        tier=body.tier,  # type: ignore[arg-type]
        device=body.device,
        chosen_by=body.chosen_by,  # type: ignore[arg-type]
        last_detection=detection,
    )
    embed_cfg.write(new_cfg)
    persisted = embed_cfg.load()  # round-trip to pick up chosen_at stamp
    assert persisted is not None

    db = get_db()
    current_dim_str = cortex_meta_get(db, "embedding_dim")
    current_dim = (
        int(current_dim_str) if current_dim_str else persisted.dim
    )
    new_dim = persisted.dim
    dim_changed = current_dim != new_dim

    job_id: Optional[str] = None
    if dim_changed:
        _drop_vec_for_dim_change()
        job_id = _enqueue_reindex_after_switch(new_dim, persisted.tier)

    # Regenerate unit files so CUDA_VISIBLE_DEVICES + OKURO_EMBED_TIER
    # reflect the new choice, and restart okuro-embed so it loads the
    # right model. The orchestrator restarts itself on the next clean
    # cycle — restarting it from inside its own request handler would
    # 500 the response.
    try:
        install_all_services(names=["okuro-embed", "okuro-daemon"], start=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("post-switch service regenerate failed: %s", exc)

    # Realign the 9 non-cortex vec_* tables. Cortex is handled by the
    # reindex pipeline above; the rest are out-of-band re-embeds. Done in
    # a background thread because the embed service is restarting and we
    # must wait for /health before issuing embed_one() calls.
    if dim_changed:
        import threading
        threading.Thread(
            target=_repair_vec_dims_when_ready,
            args=(persisted.dim,),
            daemon=True,
        ).start()

    return SwitchTierResponse(
        tier=persisted.tier,
        device=persisted.device,
        chosen_at=persisted.chosen_at,
        chosen_by=persisted.chosen_by,
        reindex_required=dim_changed,
        reindex_job_id=job_id,
    )


# ── Embed service health proxy ────────────────────────────────────────


@router.get("/health")
def get_embed_health() -> dict:
    """Proxy ``GET /health`` from the embed service so the settings page
    can render live status without bypassing CORS."""
    from okuro.system.port_registry import embed_url

    url = f"{embed_url()}/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {
            "status": "unreachable",
            "detail": str(exc),
            "url": url,
        }
