# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Managed-corpora API — add/sync/remove/list non-git document sources
#   backing the /corpora web route. Fetch+index run in the background; the UI
#   polls status. Includes a /detect probe so the form can resolve a pasted url
#   before committing to it.
# index:
#   models
#   def _list_corpora
#   def _detect
#   def _add_corpus
#   def _sync_corpus
#   def _remove_corpus
#   def _corpus_search
# AGENT_HEADER_END -->
"""REST surface for managed corpora.

Mirrors the managed-repos API deliberately — same background-task shape, same
poll-until-ready contract — because the two are the same operation over
different source kinds, and a user who has used /repos should not have to learn
a second interaction model.

The one addition is ``POST /api/corpora/detect``: the form's primary input is a
pasted url, so it needs to show what that url resolved to (source type, space
key, folder path) BEFORE the user commits. Detection is pure string parsing —
no network — so it is cheap enough to run on every keystroke pause.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger("okuro.orchestrator.api.corpora")

router = APIRouter(prefix="/api/corpora", tags=["corpora"])

# In-flight states — sync-all skips these so it never double-queues a corpus.
_BUSY = frozenset({"pending", "fetching", "indexing"})


# ── Models ───────────────────────────────────────────────────────────


class CorpusAddRequest(BaseModel):
    url: Optional[str] = None
    source_type: Optional[str] = None
    config: Optional[dict] = None
    name: Optional[str] = None
    workspace: str = "default"
    token_key: Optional[str] = None
    username: Optional[str] = None


class CorpusOut(BaseModel):
    id: str
    source_type: str
    source_ref: str
    workspace: str
    name: str
    path: str
    materialized: bool
    config: dict = {}
    status: str
    project_id: Optional[str] = None
    token_key: Optional[str] = None
    username: Optional[str] = None
    item_count: int = 0
    error: Optional[str] = None
    last_change: Optional[dict] = None
    last_synced_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CorpusAccepted(BaseModel):
    id: str
    status: str
    message: str


class DetectRequest(BaseModel):
    url: str


class DetectResult(BaseModel):
    source_type: str
    config: dict
    suggested_name: str
    label: str


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("", response_model=list[CorpusOut])
def _list_corpora(workspace: Optional[str] = Query(None)) -> list[CorpusOut]:
    from okuro.corpus import list_corpora

    return [CorpusOut(**c) for c in list_corpora(workspace)]


@router.get("/source-types")
def _source_types() -> dict:
    """What can be added, and which config keys each kind needs."""
    from okuro.corpus import describe_types

    return {"source_types": describe_types()}


@router.post("/detect", response_model=DetectResult)
def _detect(payload: DetectRequest) -> DetectResult:
    """Resolve a pasted url/path to a source type + config. No network.

    400 rather than a guess when nothing matches: silently defaulting to an
    adapter would surface much later as a confusing "space not found" during
    the background fetch, long after the user could connect it to their input.
    """
    from okuro.corpus.adapters import detect_source
    from okuro.corpus.lifecycle import _default_name

    try:
        detected = detect_source(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    st, config = detected["source_type"], detected["config"]
    if st == "confluence":
        label = f"Confluence space {config['space_key']} on {config['base_url']}"
    elif st == "local_folder":
        label = f"Local folder {config['path']}"
    else:
        label = st
    return DetectResult(
        source_type=st,
        config=config,
        suggested_name=_default_name(st, config),
        label=label,
    )


def _bg_add(payload: CorpusAddRequest) -> None:
    from okuro.corpus import add_corpus

    try:
        add_corpus(
            url=payload.url,
            source_type=payload.source_type,
            config=payload.config or {},
            name=payload.name,
            workspace=payload.workspace,
            token_key=payload.token_key,
            username=payload.username,
        )
    except Exception as exc:  # noqa: BLE001 — status persisted to the row by add_corpus
        logger.warning("corpus_add background job failed: %s", exc)


def _bg_sync(corpus_id: str, full: bool = False) -> None:
    from okuro.corpus import sync_corpus

    try:
        sync_corpus(corpus_id, full=full)
    except Exception as exc:  # noqa: BLE001 — status persisted to the row by sync_corpus
        logger.warning("corpus_sync background job failed: %s", exc)


def _bg_sync_all(corpus_ids: list[str], full: bool = False) -> None:
    """Sync corpora one at a time — same reasoning as repos: parallel fetch +
    embed would hammer the shared embed service, and per-corpus status is
    persisted so the UI's poll shows progress marching through the list."""
    from okuro.corpus import sync_corpus

    for cid in corpus_ids:
        try:
            sync_corpus(cid, full=full)
        except Exception as exc:  # noqa: BLE001 — status persisted per row
            logger.warning("corpus sync-all: %s failed: %s", cid, exc)


@router.post("", response_model=CorpusAccepted, status_code=202)
def _add_corpus(payload: CorpusAddRequest, background: BackgroundTasks) -> CorpusAccepted:
    """Accept a corpus and start fetch+index in the background.

    Resolution and the duplicate check run SYNCHRONOUSLY so a bad url or an
    already-managed name is a 400/409 the user sees immediately, rather than a
    row that silently lands in status='error' a minute later.
    """
    from okuro.corpus import get_corpus
    from okuro.corpus.adapters import detect_source
    from okuro.corpus.lifecycle import _default_name
    from okuro.corpus.paths import corpus_id, slugify

    source_type = payload.source_type
    config = dict(payload.config or {})
    if payload.url:
        try:
            detected = detect_source(payload.url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        source_type = source_type or detected["source_type"]
        config = {**detected["config"], **config}
    if not source_type:
        raise HTTPException(status_code=400, detail="pass either url, or source_type + config")

    try:
        name = payload.name or _default_name(source_type, config)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"cannot derive a name: {exc}")

    slug = corpus_id(slugify(payload.workspace), name)
    if get_corpus(slug):
        raise HTTPException(status_code=409, detail=f"corpus '{slug}' already managed")

    resolved = payload.model_copy(update={"source_type": source_type, "config": config, "name": name})
    background.add_task(_bg_add, resolved)
    return CorpusAccepted(id=slug, status="pending", message="fetch + index started")


@router.post("/sync-all", status_code=202)
def _sync_all(background: BackgroundTasks,
              workspace: Optional[str] = Query(None),
              full: bool = Query(False, description="re-fetch every item and force re-index")) -> dict:
    """Sync every managed corpus (optionally scoped to a workspace)."""
    from okuro.corpus import list_corpora

    ids = [c["id"] for c in list_corpora(workspace) if c["status"] not in _BUSY]
    if ids:
        background.add_task(_bg_sync_all, ids, full)
    return {"accepted": len(ids), "ids": ids,
            "message": ("full re-fetch started" if full else "sync-all started")}


@router.get("/{corpus_id}", response_model=CorpusOut)
def _get_corpus(corpus_id: str) -> CorpusOut:
    from okuro.corpus import get_corpus

    rec = get_corpus(corpus_id)
    if not rec:
        raise HTTPException(status_code=404, detail="managed corpus not found")
    return CorpusOut(**rec)


@router.post("/{corpus_id}/sync", response_model=CorpusAccepted, status_code=202)
def _sync_corpus(corpus_id: str, background: BackgroundTasks,
                 full: bool = Query(False, description="re-fetch every item and force re-index")) -> CorpusAccepted:
    from okuro.corpus import get_corpus

    if not get_corpus(corpus_id):
        raise HTTPException(status_code=404, detail="managed corpus not found")
    background.add_task(_bg_sync, corpus_id, full)
    return CorpusAccepted(id=corpus_id, status="fetching",
                          message=("full re-fetch started" if full else "sync started"))


@router.get("/{corpus_id}/search")
def _corpus_search(corpus_id: str, q: str = Query(..., min_length=1),
                   n: int = Query(12, ge=1, le=50)) -> dict:
    """Semantic search scoped to this corpus.

    The same vectors cortex_search reaches — this endpoint exists so the UI can
    prove a corpus is queryable right after it indexes, not because corpora have
    a private search path.
    """
    from okuro.corpus import get_corpus

    rec = get_corpus(corpus_id)
    if not rec:
        raise HTTPException(status_code=404, detail="managed corpus not found")
    project = rec.get("project_id") or corpus_id
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
        "project": project,
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


@router.delete("/{corpus_id}")
def _remove_corpus(corpus_id: str, delete_files: bool = Query(False)) -> dict:
    from okuro.corpus import remove_corpus

    try:
        return remove_corpus(corpus_id, delete_files=delete_files)
    except ValueError as exc:
        # Two distinct causes, two distinct codes: an unknown id is 404, but a
        # refused delete_files on a folder the user owns is a 409 — the corpus
        # exists and the caller asked for something deliberately not allowed.
        msg = str(exc)
        raise HTTPException(status_code=409 if "refusing to delete" in msg else 404, detail=msg)
