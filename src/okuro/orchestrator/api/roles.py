# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Roles API — list/detail/CRUD + knowledge + maintenance surface.
# index:
#   imports
#   router
#   models
#   def _require_localhost_rl
#   def _knowledge_row_to_dict
#   def _load_role_refresh_mandate
#   def list_roles_endpoint
#   def get_role_endpoint
#   def update_role_endpoint
#   def create_role_endpoint
#   def delete_role_endpoint
#   def list_knowledge_endpoint
#   def create_knowledge_endpoint
#   def delete_knowledge_endpoint
#   def maintenance_all_endpoint
#   def run_maintenance_endpoint
#   def run_all_stale_endpoint
#   def maintenance_status_endpoint
#   def maintenance_mandate_endpoint
#   def _load_role_refresh_mandate
#   def _spawn_role_researcher_task
# AGENT_HEADER_END -->
"""Roles API — single-router home for all /api/roles HTTP endpoints.

Consolidates the four role endpoints that used to live in
``okuro.orchestrator.api.main`` plus the listing endpoint that was in
``okuro.web.app`` into one module, and adds the surface the web UI needs
to expose the role self-maintenance loop (migration 011):

- GET    /api/roles                                 listing (raw list shape)
- GET    /api/roles/{id}                            detail + {stale, stats, mandate}
- PUT    /api/roles/{id}                            update (localhost-only)
- POST   /api/roles                                 create (localhost-only)
- DELETE /api/roles/{id}                            delete (localhost-only)
- GET    /api/roles/{id}/knowledge                  list knowledge entries
- POST   /api/roles/{id}/knowledge                  add entry     (localhost-only)
- DELETE /api/roles/{id}/knowledge/{entry_id}       soft-delete   (localhost-only)
- GET    /api/roles/maintenance                     per-role maintenance rollup
- GET    /api/roles/{id}/maintenance/mandate        preview-only mandate payload
- POST   /api/roles/{id}/maintenance/run            spawn role-researcher task (localhost-only)
- POST   /api/roles/maintenance/run-all-stale       spawn bulk role-researcher task (localhost-only)
- GET    /api/roles/maintenance/status              job tracker snapshot (polls orchestrator state)

Since subagent #14 (2026-04-18) the "run" endpoints actually spawn the
``role-researcher`` orchestrator engine with the role-refresh mandate prose
scoped to the target role(s) — same mandate the daily cron uses
(`~/.okuro/orchestrator/recurring/role-refresh.yaml`). The job tracker then
polls the spawned task's ``task.yaml`` for live status + a summary of what
was learned (number of knowledge rows written during the task window).
The lightweight preview path remains available under
``GET /api/roles/{id}/maintenance/mandate``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel, Field
from okuro.orchestrator.yamlfast import yload
from okuro.db.engine import okuro_home

logger = logging.getLogger("okuro.orchestrator.api.roles")

router = APIRouter(prefix="/api/roles", tags=["roles"])


# ── Helpers ──────────────────────────────────────────────────────────


def _require_localhost_rl(request: Request) -> None:
    """Reuse main's loopback guard — same pattern as keyring/cortex/people."""
    from okuro.orchestrator.api.main import _require_loopback

    _require_loopback(request)


_KNOWLEDGE_COLS = (
    "id, role_id, content, type, source_url, session_id, confidence, "
    "created_at, last_accessed, supersedes"
)


def _knowledge_row_to_dict(row) -> dict:
    """Parse a role_knowledge row to a JSON-ready dict."""
    d = dict(row)
    # Normalise: always return all fields with either value or None.
    return {
        "id": d.get("id"),
        "role_id": d.get("role_id"),
        "type": d.get("type"),
        "content": d.get("content"),
        "source_url": d.get("source_url"),
        "session_id": d.get("session_id"),
        "confidence": d.get("confidence"),
        "created_at": d.get("created_at"),
        "last_accessed": d.get("last_accessed"),
        "supersedes": d.get("supersedes"),
    }


def _role_summary(r: dict) -> dict:
    """Extend a list_roles() row with stale flag + knowledge_count.

    Kept cheap — is_stale() and knowledge_count are both SELECTs, so we run
    them inline. 56 roles × 2 queries is ~100 queries per /api/roles call;
    fine for SQLite and avoids a schema migration.
    """
    from okuro.roles.knowledge import is_stale
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        "SELECT COUNT(*) AS cnt FROM role_knowledge "
        "WHERE role_id = ? AND confidence > 0.0",
        (r["id"],),
    )
    knowledge_count = row["cnt"] if row else 0
    return {
        **r,
        "stale": is_stale(r["id"]),
        "knowledge_count": knowledge_count,
    }


# ── Models ───────────────────────────────────────────────────────────


class RoleUpdateRequest(BaseModel):
    description: Optional[str] = None
    tier: Optional[str] = None
    model: Optional[str] = None
    tools: Optional[list[str]] = None
    prompt: Optional[str] = None
    lean_prompt: Optional[str] = None
    micro_prompt: Optional[str] = None
    domain: Optional[str] = None


class RoleCreateRequest(BaseModel):
    role_id: str
    domain: str
    description: str
    tier: str = "standard"
    model: str = "sonnet"
    tools: Optional[list[str]] = None
    prompt: Optional[str] = None
    lean_prompt: Optional[str] = None
    micro_prompt: Optional[str] = None


class KnowledgeCreate(BaseModel):
    type: str = Field(default="research")
    content: str
    source_url: Optional[str] = None
    confidence: Optional[float] = Field(default=0.7, ge=0.0, le=1.0)
    supersedes: Optional[str] = None


# ── Maintenance job tracker ──────────────────────────────────────────
# Per-process, in-memory dict keyed by job_id.  Enough for "did the run start"
# + "is it still going" — not a queue.  Survives until the uvicorn worker
# restarts.  Keys: job_id → {role_id(s), status, started_at, finished_at,
# error, task_id}.  If task_id is set, status queries poll the orchestrator
# task's task.yaml for the live status and count learned rows.


_MAINTENANCE_JOBS: dict[str, dict] = {}

# Path to the role-refresh mandate file (same one the daily cron executes).
_ROLE_REFRESH_YAML = Path(
    os.environ.get(
        "OKURO_ROLE_REFRESH_YAML",
        str(okuro_home() / "orchestrator" / "recurring" / "role-refresh.yaml"),
    )
)


def _load_role_refresh_mandate() -> str:
    """Return the mandate prose from role-refresh.yaml.

    Falls back to a concise built-in mandate if the yaml is missing so the
    UI trigger never no-ops silently.
    """
    try:
        if _ROLE_REFRESH_YAML.is_file():
            data = yload(_ROLE_REFRESH_YAML.read_text()) or {}
            prose = str(data.get("description") or "").strip()
            if prose:
                return prose
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read %s: %s", _ROLE_REFRESH_YAML, exc)
    return (
        "Refresh the knowledge of stale roles. This is a MAINTENANCE SWEEP, "
        "not a research report — the deliverable is the roles_learn entries, "
        "not a narrative. For each scoped role, call roles_maintenance(role_id="
        "<id>), execute the searches, and call roles_learn() with each genuine "
        "finding (type=research, source_url required for concrete findings). "
        "Most roles have NO significant changes — a terse 'no significant "
        "changes' entry of type=research is the correct, expected output and "
        "resets the clock; do NOT manufacture findings. "
        "Acceptance criteria (judge against exactly these): "
        "AC1 every scoped role got >=1 roles_learn entry — a no-change reset "
        "fully satisfies it and is NOT a defect or fabrication; "
        "AC2 do NOT assert aggregate counts you cannot evidence — report only "
        "per-role outcomes actually produced; "
        "AC3 every scoped role's clock is reset; "
        "AC4 concrete findings cite a source_url, no-change entries do not."
    )


def _spawn_role_researcher_task(
    description: str,
    *,
    role_scope_label: str,
) -> Optional[str]:
    """Spawn the orchestrator engine with a role-researcher task.

    Reuses the same subprocess path ``POST /api/tasks`` does (see
    ``spawn_orchestrator`` in ``okuro.orchestrator.api.main``). Returns the
    task_id on success, or None if spawn failed.

    ``role_scope_label`` is a short human tag used only in log context and
    the task directory name suffix (e.g. ``rolemaint-<role-id>``). It does
    NOT affect the decomposer — scope is in the description.
    """
    # Deferred imports to avoid a circular import with main.py at module load.
    from okuro.orchestrator.api.main import (  # type: ignore
        TASKS_DIR,
        spawn_orchestrator,
    )

    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in role_scope_label)
    safe = safe.strip("-")[:40] or "stale"
    task_id = (
        f"task-rolemaint-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe}"
    )

    # Pre-create the task dir so engine startup never races.
    task_path = TASKS_DIR / task_id
    task_path.mkdir(parents=True, exist_ok=True)
    (task_path / "artifacts").mkdir(exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "okuro.orchestrator.engine",
        "--task-id",
        task_id,
        "--required-roles",
        "role-researcher",
        "--yes",
        description,
    ]

    pid = spawn_orchestrator(cmd, task_id)
    if not pid:
        logger.error("Failed to spawn role-researcher engine for %s", task_id)
        return None
    logger.info(
        "Spawned role-researcher task %s for maintenance scope=%s",
        task_id,
        role_scope_label,
    )
    return task_id


def _count_learnings_since(
    role_ids: list[str], started_iso: str
) -> int:
    """Count roles_learn rows written for the given roles after ``started_iso``.

    Used by maintenance_status_endpoint to show "what was learned" without
    parsing the task log. Safe fallback on any DB error: returns 0.
    """
    try:
        from okuro.db import get_db

        db = get_db()
        if not role_ids:
            return 0
        placeholders = ",".join(["?"] * len(role_ids))
        row = db.fetchone(
            f"SELECT COUNT(*) AS cnt FROM role_knowledge "
            f"WHERE role_id IN ({placeholders}) "
            f"AND created_at >= ? AND confidence > 0.0",
            (*role_ids, started_iso),
        )
        return int(row["cnt"]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def _read_task_status(task_id: str) -> Optional[dict]:
    """Read task.yaml for a spawned orchestrator task.

    Returns a dict with keys {status, description, current_phase} or None if
    the task dir / yaml doesn't exist yet (engine is still booting).
    """
    try:
        from okuro.orchestrator.api.main import TASKS_DIR  # type: ignore

        yaml_path = TASKS_DIR / task_id / "task.yaml"
        if not yaml_path.is_file():
            return None
        data = yload(yaml_path.read_text()) or {}
        return {
            "status": data.get("status", "unknown"),
            "description": data.get("description", ""),
            "current_phase": data.get("current_phase"),
        }
    except Exception:  # noqa: BLE001
        return None


def _refresh_job_from_orchestrator(job: dict) -> dict:
    """Enrich a tracked job dict with fresh state from its spawned task.

    Mutates+returns the job dict in place. No-op for jobs without task_id
    (e.g. ones that failed to spawn). Derives the UI ``status`` field:
    - orchestrator 'done'     → 'done'   + ``learned`` count set
    - orchestrator 'failed'   → 'failed'
    - anything else (pending, planning, active) → 'running'
    """
    task_id = job.get("task_id")
    if not task_id:
        return job
    # Already terminal — don't re-poll.
    if job.get("status") in ("done", "failed"):
        return job

    ts = _read_task_status(task_id)
    if ts is None:
        # Engine hasn't written task.yaml yet; stay in running.
        return job

    orch_status = ts["status"]
    if orch_status == "done":
        job["status"] = "done"
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        role_ids = job.get("role_ids") or (
            [job["role_id"]] if job.get("role_id") else []
        )
        job["learned"] = _count_learnings_since(
            role_ids, job.get("started_at") or ""
        )
    elif orch_status in ("failed", "cancelled"):
        job["status"] = "failed"
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        job["error"] = f"Orchestrator task {task_id} ended: {orch_status}"
    else:
        job["status"] = "running"
        job["orchestrator_phase"] = orch_status

    return job


# ── Listing / detail / CRUD ──────────────────────────────────────────


@router.get("")
def list_roles_endpoint(domain: Optional[str] = None):
    """List roles with stale + knowledge_count fields.

    Raw-list shape (frontend contract — not ``{roles, count}``).
    """
    from okuro.roles import list_roles

    try:
        rows = list_roles(domain=domain)
    except Exception as exc:  # noqa: BLE001
        logger.exception("list_roles failed")
        raise HTTPException(500, f"Role listing failed: {exc}")

    return [_role_summary(r) for r in rows]


@router.get("/maintenance")
def maintenance_all_endpoint():
    """Per-role maintenance rollup — used by /agents > Roles header.

    Returns a list with one entry per role. ``mandate_summary`` is short
    (the first search query) so the frontend can render a card without
    a second round-trip.
    """
    from okuro.db import get_db
    from okuro.roles.knowledge import is_stale

    try:
        db = get_db()
        rows = db.fetchall(
            "SELECT role_id, domain, maintenance_schedule, last_maintained "
            "FROM roles ORDER BY domain, role_id"
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("maintenance rollup failed")
        raise HTTPException(500, f"Maintenance rollup failed: {exc}")

    out: list[dict] = []
    for r in rows:
        role_id = r["role_id"]
        stale = is_stale(role_id)
        schedule = r["maintenance_schedule"] or "monthly"
        last_maintained = r["last_maintained"]
        knowledge_row = db.fetchone(
            "SELECT COUNT(*) AS cnt FROM role_knowledge "
            "WHERE role_id = ? AND confidence > 0.0",
            (role_id,),
        )
        knowledge_count = knowledge_row["cnt"] if knowledge_row else 0

        # Build a compact mandate summary — don't materialise the full thing
        # for every role on every call (that's what /api/roles/{id} is for).
        mandate_summary = f"{r['domain']} · {schedule}"

        out.append(
            {
                "role_id": role_id,
                "domain": r["domain"],
                "stale": stale,
                "schedule": schedule,
                "last_maintained": last_maintained,
                "next_due": None,  # left to the frontend — schedule + last_maintained is enough
                "mandate_summary": mandate_summary,
                "knowledge_count": knowledge_count,
            }
        )

    return out


@router.get("/maintenance/status")
def maintenance_status_endpoint(job_id: Optional[str] = None):
    """Return maintenance job status(es), polled from the spawned task.

    - No ``job_id`` → snapshot of all tracked jobs (recent first), each
      refreshed from the underlying orchestrator task.
    - With ``job_id`` → single job record or 404, refreshed.
    """
    if job_id:
        job = _MAINTENANCE_JOBS.get(job_id)
        if not job:
            raise HTTPException(404, f"Unknown maintenance job: {job_id}")
        return _refresh_job_from_orchestrator(job)

    # Newest first.  dict insertion order is stable in Py3.7+.
    items = [
        _refresh_job_from_orchestrator(j) for j in _MAINTENANCE_JOBS.values()
    ]
    items.sort(key=lambda j: j.get("started_at") or "", reverse=True)
    return {"jobs": items, "count": len(items)}


@router.post("/maintenance/run-all-stale")
def run_all_stale_endpoint(request: Request):
    """Spawn ONE role-researcher task scoped to every stale role.

    Mirrors the daily cron (``role-refresh.yaml``) — a single engine run
    sweeps all stale roles. Returns job tracking info immediately.
    """
    _require_localhost_rl(request)

    from okuro.db import get_db
    from okuro.roles.knowledge import is_stale

    db = get_db()
    rows = db.fetchall("SELECT role_id FROM roles WHERE maturity != 'draft'")
    stale_ids = [r["role_id"] for r in rows if is_stale(r["role_id"])]

    job_id = uuid.uuid4().hex
    started = datetime.now(timezone.utc).isoformat()

    if not stale_ids:
        # Nothing to do — record a done job so UI can show the zero-count.
        _MAINTENANCE_JOBS[job_id] = {
            "job_id": job_id,
            "role_ids": [],
            "role_count": 0,
            "status": "done",
            "started_at": started,
            "finished_at": started,
            "error": None,
            "task_id": None,
            "learned": 0,
        }
        return {
            "job_id": job_id,
            "role_count": 0,
            "started_at": started,
            "task_id": None,
            "role_ids": [],
        }

    mandate_prose = _load_role_refresh_mandate()
    description = (
        f"{mandate_prose}\n\n"
        f"Scope: run maintenance for the following stale roles only — "
        f"{', '.join(stale_ids)}. "
        "Do not call roles_maintenance() without filters; iterate only these "
        "role_ids, execute each mandate, and call roles_learn() per finding."
    )

    task_id = _spawn_role_researcher_task(
        description, role_scope_label=f"bulk-{len(stale_ids)}"
    )

    _MAINTENANCE_JOBS[job_id] = {
        "job_id": job_id,
        "role_ids": stale_ids,
        "role_count": len(stale_ids),
        "status": "running" if task_id else "failed",
        "started_at": started,
        "finished_at": None,
        "error": None if task_id else "Engine spawn failed",
        "task_id": task_id,
    }

    return {
        "job_id": job_id,
        "role_count": len(stale_ids),
        "role_ids": stale_ids,
        "task_id": task_id,
        "started_at": started,
    }


@router.get("/{role_id}")
def get_role_endpoint(role_id: str):
    """Get full role detail, extended with {stale, stats, mandate}.

    Mirrors the advisory block that ``roles_get`` MCP tool attaches.
    """
    from okuro.roles.knowledge import get_knowledge_stats, is_stale
    from okuro.roles.maintainer import get_maintenance_mandate
    from okuro.roles.registry import get_role as _get_role

    try:
        role = _get_role(role_id)
        if not role:
            raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")

        stale = is_stale(role_id)
        role["maintenance"] = {
            "stale": stale,
            "stats": get_knowledge_stats(role_id),
            "mandate": get_maintenance_mandate(role_id) if stale else None,
        }
        return role
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Role detail failed for %s", role_id)
        raise HTTPException(status_code=500, detail=f"Role lookup failed: {exc}")


@router.put("/{role_id}")
def update_role_endpoint(role_id: str, req: RoleUpdateRequest, request: Request):
    """Update role fields."""
    _require_localhost_rl(request)

    from okuro.db import get_db
    from okuro.roles.registry import get_role as _get_role

    try:
        role = _get_role(role_id)
        if not role:
            raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")

        db = get_db()
        updates: dict = {}
        for field in (
            "description",
            "tier",
            "model",
            "prompt",
            "lean_prompt",
            "micro_prompt",
            "domain",
        ):
            val = getattr(req, field, None)
            if val is not None:
                updates[field] = val
        if req.tools is not None:
            updates["tools"] = json.dumps(req.tools)

        if not updates:
            return {"status": "no changes"}

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [role_id]
        db.execute(f"UPDATE roles SET {set_clause} WHERE role_id = ?", values)
        return {"status": "updated", "role_id": role_id, "fields": len(updates)}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Role update failed for %s", role_id)
        raise HTTPException(status_code=500, detail=f"Role update failed: {exc}")


@router.post("/{role_id}/promote")
def promote_role_endpoint(role_id: str, request: Request):
    """Promote a draft role to active.

    Drafts are created by the Phase 0 ``role-designer`` step after a
    capability gap is accepted. The resolver excludes ``maturity='draft'``
    from match results, so a draft never surfaces in the panel proposer
    until the user explicitly promotes it via this endpoint.

    Idempotent: calling on an already-active role is a no-op.
    """
    _require_localhost_rl(request)

    from okuro.roles.registry import get_role as _get_role, update_role_info

    role = _get_role(role_id)
    if not role:
        raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")

    current = (role.get("maturity") or "active").lower()
    if current == "active":
        return {"status": "already_active", "role_id": role_id}
    if current != "draft":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Role '{role_id}' has maturity='{current}' — only 'draft' "
                f"can be promoted. Use PUT /api/roles/{role_id} for other "
                f"maturity transitions."
            ),
        )

    update_role_info(role_id, {"maturity": "active"})

    # Backfill the vec_roles embedding if missing. Roles loaded via
    # seed_from_catalog (the path role-designer uses when writing
    # catalog YAML) populate the `roles` table but not the vector
    # index — so even after promotion the proposer's match_roles()
    # vec_search returns nothing for them. Without this, "promoted"
    # roles are invisible to the panel proposer.
    try:
        from okuro.db import get_db
        db = get_db()
        existing_vec = db.fetchone(
            "SELECT id FROM vec_roles WHERE id = ?", (role_id,)
        )
        if not existing_vec:
            description = (role.get("description") or "").strip()
            if description:
                from okuro.embed import embed_one
                from okuro.embed.client import to_bytes
                emb = embed_one(description)
                db.execute(
                    "INSERT INTO vec_roles (id, embedding) VALUES (?, ?)",
                    (role_id, to_bytes(emb)),
                )
    except Exception as exc:
        # Promotion already succeeded; embedding backfill is a
        # convenience. Log but don't fail the request.
        import logging
        logging.getLogger(__name__).warning(
            "promote_role: vec_roles backfill failed for %s: %s",
            role_id, exc,
        )

    return {"status": "promoted", "role_id": role_id, "from": "draft", "to": "active"}


@router.post("")
def create_role_endpoint(req: RoleCreateRequest, request: Request):
    """Create a new role."""
    _require_localhost_rl(request)

    from okuro.db import get_db
    from okuro.roles.registry import get_role as _get_role

    try:
        existing = _get_role(req.role_id)
        if existing:
            raise HTTPException(
                status_code=409, detail=f"Role '{req.role_id}' already exists"
            )

        db = get_db()
        tools_json = json.dumps(req.tools) if req.tools else None
        db.execute(
            "INSERT INTO roles (role_id, domain, description, tier, model, tools, "
            "prompt, lean_prompt, micro_prompt, maturity, sessions, learnings, maintenance_schedule) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, 0, 'monthly')",
            (
                req.role_id,
                req.domain,
                req.description,
                req.tier,
                req.model,
                tools_json,
                req.prompt,
                req.lean_prompt,
                req.micro_prompt,
            ),
        )
        return {"status": "created", "role_id": req.role_id}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Role create failed")
        raise HTTPException(status_code=500, detail=f"Role creation failed: {exc}")


@router.delete("/{role_id}")
def delete_role_endpoint(role_id: str, request: Request):
    """Hard-delete a role."""
    _require_localhost_rl(request)

    from okuro.db import get_db
    from okuro.roles.registry import get_role as _get_role

    try:
        existing = _get_role(role_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")

        db = get_db()
        db.execute("DELETE FROM roles WHERE role_id = ?", (role_id,))
        return {"status": "deleted", "role_id": role_id}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Role delete failed for %s", role_id)
        raise HTTPException(status_code=500, detail=f"Role deletion failed: {exc}")


# ── Knowledge ───────────────────────────────────────────────────────


@router.get("/{role_id}/knowledge")
def list_knowledge_endpoint(
    role_id: str,
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    limit: int = Query(default=50, ge=1, le=500),
    type: Optional[str] = None,
):
    """List knowledge entries for a role.

    Reads the table directly (not read_knowledge) so soft-deleted rows
    (confidence=0) can be surfaced when ``min_confidence=0``. Default
    ``min_confidence=0`` — UI decides what to hide.
    """
    from okuro.db import get_db
    from okuro.roles.registry import get_role as _get_role

    try:
        role = _get_role(role_id)
        if not role:
            raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")

        db = get_db()
        if type:
            rows = db.fetchall(
                f"SELECT {_KNOWLEDGE_COLS} FROM role_knowledge "
                "WHERE role_id = ? AND confidence >= ? AND type = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (role_id, min_confidence, type, limit),
            )
        else:
            rows = db.fetchall(
                f"SELECT {_KNOWLEDGE_COLS} FROM role_knowledge "
                "WHERE role_id = ? AND confidence >= ? "
                "ORDER BY created_at DESC LIMIT ?",
                (role_id, min_confidence, limit),
            )

        return [_knowledge_row_to_dict(r) for r in rows]
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Knowledge list failed for %s", role_id)
        raise HTTPException(status_code=500, detail=f"Knowledge list failed: {exc}")


@router.post("/{role_id}/knowledge")
def create_knowledge_endpoint(role_id: str, body: KnowledgeCreate, request: Request):
    """Add a knowledge entry. Wraps ``write_knowledge``."""
    _require_localhost_rl(request)

    from okuro.roles.knowledge import write_knowledge
    from okuro.roles.registry import get_role as _get_role

    try:
        role = _get_role(role_id)
        if not role:
            raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")

        result = write_knowledge(
            role_id=role_id,
            learning=body.content,
            type=body.type,
            source_url=body.source_url,
            supersedes=body.supersedes,
            confidence=body.confidence if body.confidence is not None else 0.7,
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Knowledge create failed for %s", role_id)
        raise HTTPException(status_code=500, detail=f"Knowledge create failed: {exc}")


@router.delete("/{role_id}/knowledge/{entry_id}")
def delete_knowledge_endpoint(role_id: str, entry_id: str, request: Request):
    """Soft-delete a knowledge entry by dropping its confidence to 0."""
    _require_localhost_rl(request)

    from okuro.db import get_db
    from okuro.roles.registry import get_role as _get_role

    try:
        role = _get_role(role_id)
        if not role:
            raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")

        db = get_db()
        existing = db.fetchone(
            "SELECT id FROM role_knowledge WHERE id = ? AND role_id = ?",
            (entry_id, role_id),
        )
        if not existing:
            raise HTTPException(
                status_code=404, detail=f"Knowledge entry '{entry_id}' not found"
            )
        db.execute(
            "UPDATE role_knowledge SET confidence = 0.0 WHERE id = ?",
            (entry_id,),
        )
        return {"status": "deleted", "entry_id": entry_id, "soft": True}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Knowledge delete failed for %s/%s", role_id, entry_id)
        raise HTTPException(status_code=500, detail=f"Knowledge delete failed: {exc}")


# ── Single-role maintenance trigger ──────────────────────────────────


@router.get("/{role_id}/maintenance/mandate")
def maintenance_mandate_endpoint(role_id: str):
    """Preview-only: return the research mandate dict for one role.

    Lightweight, read-only — does NOT spawn any orchestrator task. Used by
    the UI's "Preview mandate" secondary button so the user can see what
    the research would cover before triggering the real run.
    """
    from okuro.roles.maintainer import get_maintenance_mandate
    from okuro.roles.registry import get_role as _get_role

    role = _get_role(role_id)
    if not role:
        raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")

    try:
        mandate = get_maintenance_mandate(role_id=role_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Mandate preview failed for %s", role_id)
        raise HTTPException(500, f"Mandate preview failed: {exc}")
    return mandate


@router.post("/{role_id}/maintenance/run")
def run_maintenance_endpoint(role_id: str, request: Request):
    """Spawn a real role-researcher orchestrator task scoped to ONE role.

    Returns immediately with ``{job_id, task_id, status:"running", started_at}``.
    Poll ``GET /api/roles/maintenance/status?job_id=X`` for updates — once
    the spawned task reaches ``done`` the response includes ``learned`` (the
    number of knowledge rows written for this role during the task window).
    """
    _require_localhost_rl(request)

    from okuro.roles.registry import get_role as _get_role

    role = _get_role(role_id)
    if not role:
        raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")

    mandate_prose = _load_role_refresh_mandate()
    description = (
        f"{mandate_prose}\n\n"
        f"Scope: role_id={role_id} only. Call roles_maintenance(role_id="
        f"'{role_id}') for the mandate, execute the searches, and call "
        f"roles_learn(role_id='{role_id}', ...) with each genuine finding. "
        "Do not iterate other roles."
    )

    job_id = uuid.uuid4().hex
    started = datetime.now(timezone.utc).isoformat()

    task_id = _spawn_role_researcher_task(description, role_scope_label=role_id)

    _MAINTENANCE_JOBS[job_id] = {
        "job_id": job_id,
        "role_id": role_id,
        "role_ids": [role_id],
        "status": "running" if task_id else "failed",
        "started_at": started,
        "finished_at": None,
        "error": None if task_id else "Engine spawn failed",
        "task_id": task_id,
    }

    return {
        "job_id": job_id,
        "status": "running" if task_id else "failed",
        "started_at": started,
        "role_id": role_id,
        "task_id": task_id,
    }
