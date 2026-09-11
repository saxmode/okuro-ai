# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Managed-repos API — clone/sync/remove/list cloned repositories backing
#   the /repos web route. Clone+ingest run in the background; the UI polls status.
# index:
#   models
#   def _list_repos
#   def _get_repo
#   def _add_repo
#   def _update_repo
#   def _discover_repos
#   def _update_credential
#   def _sync_repo
#   def _remove_repo
# AGENT_HEADER_END -->
"""REST surface for managed repositories.

add/sync do a git clone + code-graph ingest which can take a while, so they run
in a BackgroundTask and return immediately with the row in its 'pending'/
'indexing' state. The frontend polls GET /api/repos (or /{id}) until status is
'ready' or 'error'. lifecycle.* persists every status transition (incl. errors)
to the managed_repos row, so no progress is lost if the request connection drops.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger("okuro.orchestrator.api.repos")

router = APIRouter(prefix="/api/repos", tags=["repos"])

# In-flight states — sync-all skips these so it never double-queues a repo.
_BUSY = frozenset({"pending", "cloning", "indexing"})

# Distinguishes "field omitted" from "field explicitly set to null" on PATCH.
_UNSET: object = object()


# ── Models ───────────────────────────────────────────────────────────


class RepoAddRequest(BaseModel):
    url: str
    workspace: str = "default"
    tier: str = "air"
    name: Optional[str] = None
    token_key: Optional[str] = None
    username: Optional[str] = None
    pr_username: Optional[str] = None


class RepoOut(BaseModel):
    id: str
    url: str
    workspace: str
    name: str
    path: str
    tier: str
    status: str
    last_indexed_sha: Optional[str] = None
    error: Optional[str] = None
    default_branch: Optional[str] = None
    project_id: Optional[str] = None
    token_key: Optional[str] = None
    username: Optional[str] = None
    pr_username: Optional[str] = None
    last_synced_at: Optional[str] = None
    last_change: Optional[dict] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class RepoUpdateRequest(BaseModel):
    """Partial update. A field left unset is untouched; explicit null clears it.

    Uses a sentinel rather than Optional-means-absent because token_key,
    username and pr_username are all nullable columns — "make this repo
    anonymous" and "leave the credential alone" are different requests and
    Optional[str]=None cannot tell them apart.
    """

    url: Optional[str] = _UNSET
    tier: Optional[str] = _UNSET
    default_branch: Optional[str] = _UNSET
    token_key: Optional[str] = _UNSET
    username: Optional[str] = _UNSET
    pr_username: Optional[str] = _UNSET
    verify: bool = True
    force: bool = False

    def changes(self) -> dict:
        return {
            k: v
            for k, v in self.model_dump(exclude={"verify", "force"}).items()
            if v is not _UNSET
        }


class CredentialUpdateRequest(BaseModel):
    """Re-point every repo sharing one identity (host, token_key, username)."""

    token_key: Optional[str] = None
    username: Optional[str] = None
    host: Optional[str] = None
    new_token_key: Optional[str] = None
    new_username: Optional[str] = None
    new_pr_username: Optional[str] = None
    verify: bool = True
    force: bool = False


class RepoAccepted(BaseModel):
    id: str
    status: str
    message: str


class RepoPushResult(BaseModel):
    id: str
    source_branch: Optional[str] = None
    dest_branch: str
    pushed: bool
    up_to_date: bool
    pr_status: str          # opened | failed | skipped
    pr_url: Optional[str] = None
    pr_error: Optional[str] = None
    detail: str


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("", response_model=list[RepoOut])
def _list_repos(workspace: Optional[str] = Query(None)) -> list[RepoOut]:
    from okuro.repos import list_repos

    return [RepoOut(**r) for r in list_repos(workspace)]


@router.get("/{repo_id}", response_model=RepoOut)
def _get_repo(repo_id: str) -> RepoOut:
    from okuro.repos import get_repo

    rec = get_repo(repo_id)
    if not rec:
        raise HTTPException(status_code=404, detail="managed repo not found")
    return RepoOut(**rec)


def _bg_add(payload: RepoAddRequest) -> None:
    from okuro.repos import add_repo

    try:
        add_repo(
            url=payload.url,
            workspace=payload.workspace,
            tier=payload.tier,
            name=payload.name,
            token_key=payload.token_key,
            username=payload.username,
            pr_username=payload.pr_username,
        )
    except Exception as exc:  # noqa: BLE001 — status persisted to the row by add_repo
        logger.warning("repo_add background job failed: %s", exc)


def _bg_sync(repo_id: str, token_key: Optional[str], full: bool = False) -> None:
    from okuro.repos import sync_repo

    try:
        sync_repo(repo_id, token_key=token_key, full=full)
    except Exception as exc:  # noqa: BLE001 — status persisted to the row by sync_repo
        logger.warning("repo_sync background job failed: %s", exc)


def _bg_sync_all(repo_ids: list[str], full: bool = False) -> None:
    """Sync repos one at a time. Sequential on purpose — parallel pulls + ingest
    would hammer the shared embed service and thrash the CPU; per-repo status is
    persisted so the UI's poll shows progress marching through the list."""
    from okuro.repos import sync_repo

    for rid in repo_ids:
        try:
            sync_repo(rid, full=full)
        except Exception as exc:  # noqa: BLE001 — status persisted per row
            logger.warning("repo sync-all: %s failed: %s", rid, exc)


@router.post("", response_model=RepoAccepted, status_code=202)
def _add_repo(payload: RepoAddRequest, background: BackgroundTasks) -> RepoAccepted:
    from okuro.repos.paths import repo_id, slugify
    from okuro.repos.lifecycle import _name_from_url
    from okuro.repos import get_repo

    if payload.tier not in ("air", "advanced", "pro"):
        raise HTTPException(status_code=400, detail="tier must be air|advanced|pro")

    name = payload.name or _name_from_url(payload.url)
    slug = repo_id(slugify(payload.workspace), name)
    if get_repo(slug):
        raise HTTPException(status_code=409, detail=f"repo '{slug}' already managed")

    background.add_task(_bg_add, payload)
    return RepoAccepted(id=slug, status="pending", message="clone + ingest started")


@router.get("/discover/forge", response_model=dict)
def _discover_repos(
    workspace: Optional[str] = Query(None),
    token_key: Optional[str] = Query(None),
    host: Optional[str] = Query(None),
) -> dict:
    """List repos the stored credentials can see, flagging which are already managed.

    With no query params it sweeps every (host, workspace, credential) already in
    the registry. Read-only — adding is still an explicit POST /api/repos.
    """
    from okuro.repos.discovery import discover_repos

    try:
        return discover_repos(workspace=workspace, token_key=token_key, host=host)
    except Exception as exc:  # noqa: BLE001
        logger.exception("repo discovery failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.patch("/{repo_id}", response_model=dict)
def _update_repo(repo_id: str, payload: RepoUpdateRequest) -> dict:
    """Update mutable repo metadata — the credential-rotation path.

    Refuses (200 with `refused`) rather than silently saving a credential that
    fails verification: a saved-but-broken credential looks green in the UI and
    breaks at the next sync, which is strictly worse than a refusal.
    """
    from okuro.repos import update_repo

    changes = payload.changes()
    if not changes:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        return update_repo(repo_id, verify=payload.verify, force=payload.force, **changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("repo update failed for %s", repo_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.patch("/credentials/bulk", response_model=dict)
def _update_credential(payload: CredentialUpdateRequest) -> dict:
    """Re-point a whole credential group in one verified call."""
    from okuro.repos import update_credential

    try:
        return update_credential(
            token_key=payload.token_key,
            username=payload.username,
            host=payload.host,
            new_token_key=payload.new_token_key,
            new_username=payload.new_username,
            new_pr_username=payload.new_pr_username,
            verify=payload.verify,
            force=payload.force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("credential update failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/sync-all", status_code=202)
def _sync_all(background: BackgroundTasks,
              workspace: Optional[str] = Query(None),
              full: bool = Query(False, description="force a full re-walk (re-tier every file)")) -> dict:
    """Sync every managed repo (optionally scoped to a workspace).

    Skips repos already mid-flight (pending/cloning/indexing). Runs the batch in
    a single background task that syncs sequentially; returns immediately with
    the ids accepted. The UI polls GET /api/repos for per-repo status.
    ``full=true`` forces a full re-index of each repo (re-tier the code graph).
    """
    from okuro.repos import list_repos

    ids = [r["id"] for r in list_repos(workspace) if r["status"] not in _BUSY]
    if ids:
        background.add_task(_bg_sync_all, ids, full)
    return {"accepted": len(ids), "ids": ids,
            "message": ("full re-index started" if full else "sync-all started")}


@router.post("/{repo_id}/sync", response_model=RepoAccepted, status_code=202)
def _sync_repo(repo_id: str, background: BackgroundTasks,
               token_key: Optional[str] = Query(None),
               full: bool = Query(False, description="force a full re-walk (re-tier every file)")) -> RepoAccepted:
    from okuro.repos import get_repo

    if not get_repo(repo_id):
        raise HTTPException(status_code=404, detail="managed repo not found")
    background.add_task(_bg_sync, repo_id, token_key, full)
    return RepoAccepted(id=repo_id, status="indexing",
                        message=("full re-index started" if full else "sync started"))


@router.post("/{repo_id}/push", response_model=RepoPushResult)
def _push_repo(repo_id: str,
               token_key: Optional[str] = Query(None),
               username: Optional[str] = Query(None),
               pr_username: Optional[str] = Query(None),
               branch: Optional[str] = Query(None),
               title: Optional[str] = Query(None),
               open_pr: bool = Query(True)) -> RepoPushResult:
    """Push local commits to a fresh branch and open a PR into the default branch.

    Synchronous (push + one REST call) and does NOT re-ingest. Never pushes to
    the default branch directly and never force-pushes: a diverged local branch
    returns 409 telling the caller to sync first. If the branch pushes but the
    PR call fails, returns 200 with pr_status='failed' (the commits are safe;
    open the PR manually).
    """
    from okuro.repos import push_repo, get_repo

    if not get_repo(repo_id):
        raise HTTPException(status_code=404, detail="managed repo not found")
    try:
        return RepoPushResult(**push_repo(
            repo_id, token_key=token_key, username=username,
            pr_username=pr_username, branch=branch, title=title, open_pr=open_pr,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


def _repo_project(repo_id: str) -> str:
    from okuro.repos import get_repo

    rec = get_repo(repo_id)
    if not rec:
        raise HTTPException(status_code=404, detail="managed repo not found")
    return rec.get("project_id") or repo_id


@router.get("/{repo_id}/insights")
def _repo_insights(repo_id: str, top_n: int = Query(15, ge=1, le=100)) -> dict:
    """God nodes (centrality hubs) + detected subsystems (Louvain) for the repo."""
    from okuro.cortex.codegraph import insights_summary

    return insights_summary(_repo_project(repo_id), top_n=top_n)


@router.get("/{repo_id}/stats")
def _repo_stats(repo_id: str) -> dict:
    """Index-proof counts: files, symbols, edges, languages, layers, vectors."""
    from okuro.cortex.codegraph.insights import repo_stats

    return repo_stats(_repo_project(repo_id))


@router.get("/{repo_id}/graph")
def _repo_graph(repo_id: str, max_nodes: int = Query(400, ge=10, le=1500)) -> dict:
    """File-level code graph (nodes=files, edges=calls/imports) for @xyflow."""
    from okuro.cortex.codegraph.insights import file_graph_data

    return file_graph_data(_repo_project(repo_id), max_nodes=max_nodes)


@router.get("/{repo_id}/file")
def _repo_file(repo_id: str, path: str = Query(..., description="repo-relative file path")) -> dict:
    """Drill-down for one file: symbols, imports, out-calls, incoming callers."""
    from okuro.cortex.codegraph.insights import file_detail

    return file_detail(_repo_project(repo_id), path)


@router.get("/{repo_id}/search")
def _repo_search(repo_id: str, q: str = Query(..., min_length=1),
                 n: int = Query(12, ge=1, le=50)) -> dict:
    """Semantic + keyword search scoped to this repo (air tier → BM25-only)."""
    from okuro.repos import get_repo

    rec = get_repo(repo_id)
    if not rec:
        raise HTTPException(status_code=404, detail="managed repo not found")
    project = rec.get("project_id") or repo_id
    root = rec.get("path") or ""
    try:
        from okuro.cortex.vectorstore import VectorStore

        results = VectorStore().search(q, n_results=n, project=project)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"search failed: {exc}")

    def rel(p: str) -> str:
        p = str(p)
        return p[len(root) + 1:] if root and p.startswith(root) else p

    return {
        "query": q,
        "results": [
            {
                "path": rel(r.file_path),
                "score": float(r.relevance),
                "snippet": r.snippet,
                "purpose": r.purpose or None,
            }
            for r in results
        ],
    }


@router.delete("/{repo_id}")
def _remove_repo(repo_id: str, delete_files: bool = Query(False)) -> dict:
    from okuro.repos import remove_repo

    try:
        return remove_repo(repo_id, delete_files=delete_files)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
