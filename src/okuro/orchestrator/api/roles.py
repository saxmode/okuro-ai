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
#   def spawn_role_task
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


def _role_summary(r: dict, checks_by_role: dict[str, list[dict]] | None = None) -> dict:
    """Extend a list_roles() row with stale flag + knowledge_count.

    Kept cheap — is_stale() and knowledge_count are both SELECTs, so we run
    them inline. 56 roles × 2 queries is ~100 queries per /api/roles call;
    fine for SQLite and avoids a schema migration.

    ``checks_by_role`` is READ, never fetched. The caller lists 104 roles and
    has to do the grouped read once; doing it here would have added a third
    per-role query to a function whose docstring already apologises for two,
    and ``check_summaries_by_role`` exists precisely so a fleet-wide page does
    not have to. Absent, the fit is computed without checks — a missing defect
    line, not a wrong number.
    """
    from okuro.roles.knowledge import is_stale
    from okuro.roles.fit import compute_fit, fit_summary
    from okuro.orchestrator.api.roles_fit import SOURCED_DEFINITION
    from okuro.db import get_db

    db = get_db()
    # Was a COUNT(*). The rows themselves are what the knowledge fit segment
    # needs, and len() is the same number — so this stays one query, not two.
    #
    # NO `confidence > 0` FILTER. Suppressed rows are counted and named inside
    # compute_fit instead, so the fit view's total matches the table's count.
    # As a SQL filter it silently shrank the total and nothing said why.
    knowledge_rows = [
        dict(k)
        for k in db.fetchall(
            "SELECT content, source_url, created_at, confidence "
            "FROM role_knowledge WHERE role_id = ?",
            (r["id"],),
        )
    ]
    knowledge_count = len(knowledge_rows)

    # The list row renders a five-segment bar, so it needs five numbers per
    # role. fit_summary drops the defect strings — those are what
    # GET /api/roles/{id}/fit is for, and shipping 103 roles' worth of them
    # into a list nobody reads them from is the payload nobody asked for.
    fit: dict | None = None
    try:
        bodies = db.fetchone(
            "SELECT prompt, lean_prompt, micro_prompt FROM roles WHERE role_id = ?",
            (r["id"],),
        )
        fit = fit_summary(
            compute_fit(
                {**r, **(dict(bodies) if bodies else {})},
                knowledge_rows,
                sourced_definition=SOURCED_DEFINITION,
                check_rows=(checks_by_role or {}).get(r["id"], []),
            )
        )
    except Exception as exc:  # noqa: BLE001 — analytics must not 500 the list
        logger.exception("fit computation failed for role %s", r["id"])
        fit = {"error": str(exc)}

    return {
        **r,
        "stale": is_stale(r["id"]),
        "knowledge_count": knowledge_count,
        "fit": fit,
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
    """The REST shape of a knowledge write.

    It carries ``outcome`` and ``run_id`` for the same reason ``roles_learn``
    does: both doors call ``write_knowledge``, so a parameter missing from one
    of them does not make that door safer, it makes it LIE. Before this, a
    no-change report posted over HTTP could not say so and had to hope the text
    heuristic caught it, and a cited URL could never earn ``fetch_verified``
    however honestly it was fetched — the same write meant two different things
    depending on which door it came through.
    """

    type: str = Field(default="research")
    content: str
    source_url: Optional[str] = None
    confidence: Optional[float] = Field(default=0.7, ge=0.0, le=1.0)
    supersedes: Optional[str] = None
    outcome: Optional[str] = Field(
        default=None,
        description=(
            "no_change | error | skipped. Set when this entry reports an "
            "OUTCOME rather than carrying a fact; it is filed as a check, and "
            "only no_change resets the role's maintenance clock."
        ),
    )
    run_id: Optional[str] = Field(
        default=None,
        description=(
            "The maintenance run this entry belongs to. Required together "
            "with source_url for fetch_verified."
        ),
    )


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


def spawn_role_task(
    description: str,
    *,
    role_scope_label: str,
    required_role: str = "role-researcher",
    task_prefix: str = "rolemaint",
) -> Optional[str]:
    """Spawn the orchestrator engine with ONE required role.

    Reuses the same subprocess path ``POST /api/tasks`` does (see
    ``spawn_orchestrator`` in ``okuro.orchestrator.api.main``). Returns the
    task_id on success, or None if spawn failed.

    ``role_scope_label`` is a short human tag used only in log context and
    the task directory name suffix (e.g. ``rolemaint-<role-id>``). It does
    NOT affect the decomposer — scope is in the description.

    **Why the role name is a parameter now.** This function had
    ``role-researcher`` written into its argv, which made it the
    role-refresh spawner rather than the role spawner. The structure-research
    dispatch needs the identical machinery — pre-create the task dir, same
    subprocess path, same failure handling — against a DIFFERENT required
    role. Copying it would have produced two spawners that agree today and
    drift on the first edit to either, which is the class DP10/DP11 names.
    The default keeps both existing callers byte-identical in behaviour.
    """
    # Deferred imports to avoid a circular import with main.py at module load.
    from okuro.orchestrator.api.main import (  # type: ignore
        TASKS_DIR,
        spawn_orchestrator,
    )

    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in role_scope_label)
    safe = safe.strip("-")[:40] or "stale"
    task_id = (
        f"task-{task_prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe}"
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
        required_role,
        "--yes",
        description,
    ]

    pid = spawn_orchestrator(cmd, task_id)
    if not pid:
        logger.error("Failed to spawn %s engine for %s", required_role, task_id)
        return None
    logger.info(
        "Spawned %s task %s for scope=%s",
        required_role,
        task_id,
        role_scope_label,
    )
    return task_id


def _spawn_role_researcher_task(
    description: str,
    *,
    role_scope_label: str,
) -> Optional[str]:
    """The role-refresh spawn, kept as its own name at its two call sites."""
    return spawn_role_task(description, role_scope_label=role_scope_label)


def _api_base_url() -> str:
    """Where a dispatched subagent reaches this API.

    Read from ``port_registry.orchestrator_port()``, which is the one place
    that resolves ``OKURO_PORT`` → config → default, rather than written into
    the brief as a literal. The host is loopback because the endpoint the brief
    points at is loopback-only; a brief naming any other host would be telling
    the agent to do something the guard refuses.

    The first version of this brief said "POST to /api/roles/knowledge-fetch"
    with no scheme, host or port. An agent in a subprocess has no browser
    origin to resolve that against, so it either guessed a port or gave up —
    and giving up looks exactly like an agent that chose not to cite anything.
    """
    from okuro.system.port_registry import orchestrator_port

    return f"http://127.0.0.1:{orchestrator_port()}"


def _maintenance_run_brief(run_id: str) -> str:
    """The paragraph that lets a sweep agent EARN verification instead of claim it.

    Three things the sweep could not previously express, all of them now
    mechanical rather than rhetorical:

    * a no-change outcome has a parameter, so the agent stops writing a
      knowledge row that says nothing;
    * a failed check has its own outcome, so six weeks of a broken feed stop
      reading as six quiet weeks;
    * a citation can be BACKED, by asking the server to fetch the page and
      store the body, after which ``fetch_verified`` is a join rather than a
      courtesy.

    The run_id is a correlation key, not a credential — it is the server that
    fetches, so minting an id buys an agent nothing but the ability to tie its
    own citations to its own fetches.
    """
    return (
        f"\n\nrun_id for this sweep: {run_id}\n"
        "Pass run_id='" + run_id + "' on EVERY roles_learn call in this run.\n"
        "- Found nothing material: roles_learn(..., outcome='no_change'). That "
        "is the correct and expected output and it resets the role's clock. Do "
        "NOT write a knowledge entry that says 'no significant change' — it is "
        "filed as a check now, not as a learning, and manufacturing a finding "
        "to avoid the word is the failure this run is judged on.\n"
        "- Could not check (fetch failed, source down, search unusable): "
        "outcome='error'. Did not check at all: outcome='skipped'. Neither "
        "resets the clock, which is the point — a dead source must stay "
        "visible.\n"
        "- Found something real: cite it. To make the citation count, first\n"
        "    curl -sS -X POST " + _api_base_url() + "/api/roles/knowledge-fetch \\\n"
        "      -H 'Content-Type: application/json' \\\n"
        "      -d '{\"url\": \"<the page>\", \"run_id\": \"" + run_id + "\"}'\n"
        "  The SERVER fetches and stores the body; you do not send one. Then "
        "pass that same url as source_url together with run_id on roles_learn. "
        "Only then does the entry read fetch_verified.\n"
        "- A fetch you cannot record is NOT a reason to skip the finding and "
        "NOT a reason to invent a citation. The server refuses addresses that "
        "point inward, non-web ports, oversize bodies and pages it cannot "
        "reach, and an entry whose fetch did not land simply reads "
        "fetch_verified=0. That is the design: the row still says what you "
        "found and the store does not claim to have checked something it "
        "never saw. Write the finding either way.\n"
    )


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
    from okuro.db import get_db
    from okuro.roles import list_roles
    from okuro.roles.checks import check_summaries_by_role

    try:
        rows = list_roles(domain=domain)
    except Exception as exc:  # noqa: BLE001
        logger.exception("list_roles failed")
        raise HTTPException(500, f"Role listing failed: {exc}")

    # ONE grouped read for the whole page, not one per row. The lean variant,
    # because this renders a count and a defect line and never the prose.
    try:
        checks = check_summaries_by_role(get_db())
    except Exception as exc:  # noqa: BLE001 — a listing without checks still lists
        logger.warning("check summaries unavailable for the roles list: %s", exc)
        checks = {}

    return [_role_summary(r, checks) for r in rows]


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

    from okuro.roles.source_poll import new_run_id

    run_id = new_run_id()
    mandate_prose = _load_role_refresh_mandate()
    description = (
        f"{mandate_prose}\n\n"
        f"Scope: run maintenance for the following stale roles only — "
        f"{', '.join(stale_ids)}. "
        "Do not call roles_maintenance() without filters; iterate only these "
        "role_ids, execute each mandate, and call roles_learn() per finding."
        + _maintenance_run_brief(run_id)
    )

    task_id = _spawn_role_researcher_task(
        description, role_scope_label=f"bulk-{len(stale_ids)}"
    )

    _MAINTENANCE_JOBS[job_id] = {
        "job_id": job_id,
        "run_id": run_id,
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
    """Update role fields.

    Goes through ``roles.write.update_role_fields``, which is what makes this
    endpoint equal to POST rather than a hole beside it. Before that it was a
    bare UPDATE: no vocabulary gate (a tier POST refuses at 422 was reachable
    by editing an existing role), no re-embed (a rewritten description left
    ``vec_roles`` matching the old wording forever) and no ``updated_at``
    (every staleness reader believed the row was as old as its last sweep).
    """
    _require_localhost_rl(request)

    from okuro.roles.registry import get_role as _get_role
    from okuro.roles.write import RoleWriteRefused, update_role_fields

    try:
        role = _get_role(role_id)
        if not role:
            raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")

        updates: dict = {}
        for field in (
            "description",
            "tier",
            "model",
            "prompt",
            "lean_prompt",
            "micro_prompt",
            "domain",
            "tools",
        ):
            val = getattr(req, field, None)
            if val is not None:
                updates[field] = val

        if not updates:
            return {"status": "no changes"}

        result = update_role_fields(role_id, updates, actor="api:PUT /api/roles")
        return {
            "status": "updated",
            "role_id": role_id,
            "fields": len(result["fields"]),
            "embedded": result["embedded"],
        }
    except RoleWriteRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc))
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

    from okuro.roles.registry import get_role as _get_role
    from okuro.roles.write import RolePromotionRefused, promote_role as _promote

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

    # The helper owns the transition AND everything promotion exposes: tags,
    # the vector, updated_at. It embeds when the vector is missing OR STALE —
    # this endpoint used to check only for missing, so a role edited while in
    # draft went live matching the wording it had before the edit.
    # Promotion itself must not fail on a derived step.
    try:
        result = _promote(role_id, actor="api:POST /api/roles/{id}/promote")
        embedded = result["embedded"]
    except RolePromotionRefused as exc:
        # MUST be caught ABOVE the fallback below. That fallback exists so a
        # dead embed service cannot block a promotion, and it does its job by
        # setting maturity='active' by hand — which means that catching the
        # structure refusal there would have promoted the very role the gate
        # just refused, and reported "embedded: failed" as if the only problem
        # were the vector. The gate would have been unreachable through HTTP.
        raise HTTPException(
            status_code=422,
            detail={
                "error": (
                    f"Role '{role_id}' is not structurally complete enough to "
                    f"promote. Nothing was changed; it is still a draft."
                ),
                "missing": exc.missing,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("promote_role: post-steps failed for %s: %s", role_id, exc)
        from okuro.roles.registry import update_role_info

        update_role_info(role_id, {"maturity": "active"})
        embedded = "failed"

    return {
        "status": "promoted",
        "role_id": role_id,
        "from": "draft",
        "to": "active",
        "embedded": embedded,
    }


@router.post("")
def create_role_endpoint(req: RoleCreateRequest, request: Request):
    """Create a new role."""
    _require_localhost_rl(request)

    from okuro.roles.registry import get_role as _get_role
    from okuro.roles.write import RoleWriteRefused, upsert_role

    try:
        existing = _get_role(req.role_id)
        if existing:
            raise HTTPException(
                status_code=409, detail=f"Role '{req.role_id}' already exists"
            )

        # The vocabulary gate, the vector and the tags all live in the write
        # helper now. They used to be installed here, which is why the three
        # other birth paths each shipped without some of them — the guarantees
        # belonged to this endpoint rather than to the table.
        #
        # `embedded` is still REPORTED rather than swallowed. If the embed
        # service is down the role exists but cannot be matched, and answering
        # a bare 201 would tell the caller the opposite of what is true. It
        # self-heals at the next daemon start, which is a restart the caller
        # did not ask for and would otherwise never hear about.
        written = upsert_role(
            {
                "role_id": req.role_id,
                "domain": req.domain,
                "description": req.description,
                "tier": req.tier,
                "model": req.model,
                "tools": req.tools,
                "prompt": req.prompt,
                "lean_prompt": req.lean_prompt,
                "micro_prompt": req.micro_prompt,
                "maturity": "active",
                "maintenance_schedule": "monthly",
            },
            actor="api:POST /api/roles",
        )
        result = {
            "status": "created",
            "role_id": req.role_id,
            "embedded": written["embedded"],
        }
        # `embedded` is a STRING ("written"/"unchanged"/"failed"/"skipped").
        # A truthiness test here would be permanently False and the warning
        # would never fire — the exact silence this block exists to break.
        if written["embedded"] == "failed":
            result["warning"] = (
                "role created but not embedded — it will not be returned by "
                "roles_match until a vector exists. The next daemon start "
                "backfills it; `python -m okuro.embed.repair` forces it sooner."
            )
        elif written["embedded"] == "skipped":
            result["warning"] = (
                "role created with no description, so there was nothing to "
                "embed — it will never be returned by roles_match. Give it a "
                "description via PUT /api/roles/{role_id}."
            )
        return result
    except RoleWriteRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Role create failed")
        raise HTTPException(status_code=500, detail=f"Role creation failed: {exc}")


@router.delete("/{role_id}")
def delete_role_endpoint(role_id: str, request: Request):
    """Hard-delete a role, vector included.

    Deleting only the ``roles`` row left an orphan in ``vec_roles`` that kept
    answering similarity queries for a role that no longer existed — and a
    later role reusing the id would have inherited the dead one's embedding.
    """
    _require_localhost_rl(request)

    from okuro.roles.write import delete_role as _delete

    try:
        if not _delete(role_id, actor="api:DELETE /api/roles"):
            raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")
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
            outcome=body.outcome,
            run_id=body.run_id,
        )
        return result
    except ValueError as exc:
        # An unknown `outcome` — write_knowledge names the bad word, and a
        # caller who typed it deserves a 400 rather than a 500 that reads like
        # okuro broke.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    from okuro.roles.source_poll import new_run_id

    run_id = new_run_id()
    mandate_prose = _load_role_refresh_mandate()
    description = (
        f"{mandate_prose}\n\n"
        f"Scope: role_id={role_id} only. Call roles_maintenance(role_id="
        f"'{role_id}') for the mandate, execute the searches, and call "
        f"roles_learn(role_id='{role_id}', ...) with each genuine finding. "
        "Do not iterate other roles."
        + _maintenance_run_brief(run_id)
    )

    job_id = uuid.uuid4().hex
    started = datetime.now(timezone.utc).isoformat()

    task_id = _spawn_role_researcher_task(description, role_scope_label=role_id)

    _MAINTENANCE_JOBS[job_id] = {
        "job_id": job_id,
        "run_id": run_id,
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
