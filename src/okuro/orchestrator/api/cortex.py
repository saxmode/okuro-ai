# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Cortex API — semantic + literal codebase search, stats, reindex.
# index:
#   imports
#   router
#   class CortexStats
#   class SearchHit
#   class SearchResponse
#   class CodeHit
#   class CodeSearchResponse
#   class ReindexResponse
#   class ReindexStatus
#   _REINDEX_JOBS
#   def _require_localhost_cx
#   def _safe_path
#   def get_stats
#   def semantic_search
#   def literal_search
#   def route
#   def header
#   def file_slice
#   def post_reindex
#   def reindex_status
# AGENT_HEADER_END -->
"""Cortex API — thin HTTP wrapper over okuro.cortex modules.

Gives the web UI a surface that mirrors the MCP cortex tools:
- GET /api/cortex/stats       — vector + file counts + last scan
- GET /api/cortex/search      — semantic hybrid search
- GET /api/cortex/search-code — literal/regex (ripgrep) search
- GET /api/cortex/route       — top-5 files for a concept
- GET /api/cortex/header      — AGENT_HEADER for one file
- GET /api/cortex/file        — line-bounded file slice
- POST /api/cortex/reindex    — kicks off VectorStore.index_directory
- GET /api/cortex/reindex/status?job=... — background reindex progress

All write endpoints (reindex) are localhost-only, matching the pattern
used by /api/keyring/*. Read endpoints are unauthed because they share
the same cortex data every MCP agent already sees.
"""

from __future__ import annotations

import logging
import os
import secrets as _secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("okuro.orchestrator.api.cortex")

router = APIRouter(prefix="/api/cortex", tags=["cortex"])

# Resolve the repo root — cortex's mcp_tools uses the same env var.
_ROOT: Path = Path(os.environ.get("OKURO_ROOT", ".")).resolve()


# ── Helpers ──────────────────────────────────────────────────────────


def _require_localhost_cx(request: Request) -> None:
    """Mirror keyring router: reuse main's loopback guard."""
    from okuro.orchestrator.api.main import _require_loopback
    _require_loopback(request)


def _safe_path(rel: str) -> Path:
    """Resolve a path relative to _ROOT, rejecting traversal outside it.

    Accepts both absolute paths inside _ROOT and relative paths. Any
    attempt to escape _ROOT via ``..`` raises 400.
    """
    if not rel:
        raise HTTPException(400, "path is required")
    candidate = Path(rel)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (_ROOT / candidate).resolve()
    try:
        resolved.relative_to(_ROOT)
    except ValueError:
        raise HTTPException(400, f"Path escapes OKURO_ROOT: {rel}")
    return resolved


# ── Reindex job tracking ─────────────────────────────────────────────
# In-memory only. Jobs are forgotten on process restart.

_REINDEX_JOBS: dict[str, dict] = {}
_REINDEX_LOCK = threading.Lock()


# ── Response models ──────────────────────────────────────────────────


class CortexStats(BaseModel):
    files_indexed: int
    total_documents: int
    sections_indexed: int
    content_chunks: int
    embedding_model: str
    root: str
    # Index health (F40)
    last_indexed_at: Optional[str] = None
    live_vectors: int = 0
    orphan_vectors: int = 0
    worktree_docs: int = 0
    # Query metrics (F39)
    search_volume: int = 0
    zero_result_rate: float = 0.0
    hit_rate: float = 0.0
    avg_latency_ms: Optional[float] = None


class SearchHit(BaseModel):
    path: str
    score: float
    snippet: Optional[str] = None
    purpose: Optional[str] = None
    matched_section: Optional[str] = None
    keyword_score: Optional[float] = None


class SearchResponse(BaseModel):
    query: str
    results: list[SearchHit]
    # Scope guardrail (shared with the MCP tools via cortex.scope_hints):
    # collision_warning + sibling_roots when a scoped slug has siblings, and
    # no_results guidance so an empty set is never misread as "code absent".
    # None on the happy path.
    scope: Optional[dict] = None


class CodeHit(BaseModel):
    path: str
    line: int
    text: str


class CodeSearchResponse(BaseModel):
    query: str
    backend: str  # "ripgrep" or "python"
    matches: list[CodeHit]


class HeaderResponse(BaseModel):
    path: str
    found: bool
    purpose: Optional[str] = None
    role: Optional[str] = None
    index: list[dict] = Field(default_factory=list)
    raw: Optional[str] = None


class FileSliceResponse(BaseModel):
    path: str
    start_line: int
    end_line: int
    total_lines: int
    content: str


class RegisteredRoot(BaseModel):
    slug: Optional[str]
    path: str
    files: int = 0
    documents: int = 0


class ProjectsResponse(BaseModel):
    schema_version: str
    roots: list[RegisteredRoot]


class RegisterProjectRequest(BaseModel):
    path: str
    slug: Optional[str] = None
    name: Optional[str] = None
    reindex: bool = False


class RegisterProjectResponse(BaseModel):
    slug: str
    name: str
    path: str
    action: str  # "created" | "updated" | "already_active"
    pre_existing_slug: Optional[str] = None
    reindex_job_id: Optional[str] = None


class ReindexResponse(BaseModel):
    job_id: str
    status: str  # "running"
    started_at: float


class ReindexStatus(BaseModel):
    job_id: str
    status: str  # "running" | "done" | "failed"
    started_at: float
    finished_at: Optional[float] = None
    total: Optional[int] = None
    indexed: Optional[int] = None
    error: Optional[str] = None
    root: str


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("/stats", response_model=CortexStats)
def get_stats() -> CortexStats:
    """Return aggregate index counters for the current cortex database."""
    try:
        from okuro.cortex.vectorstore import VectorStore
        vs = VectorStore()
        stats = vs.get_stats()
    except Exception as exc:
        logger.exception("cortex stats failed")
        raise HTTPException(500, f"Cortex stats failed: {exc}")

    # Index health + query metrics (F39/F40) — best-effort, never fail stats.
    health: dict = {}
    qm: dict = {}
    try:
        from okuro.cortex.observability import index_health, query_metrics

        health = index_health()
        qm = query_metrics()
    except Exception:
        logger.debug("cortex observability metrics unavailable", exc_info=True)

    return CortexStats(
        files_indexed=int(stats.get("files_indexed") or 0),
        total_documents=int(stats.get("total_documents") or 0),
        sections_indexed=int(stats.get("sections_indexed") or 0),
        content_chunks=int(stats.get("content_chunks") or 0),
        embedding_model=str(stats.get("embedding_model") or ""),
        root=str(_ROOT),
        last_indexed_at=health.get("last_indexed_at"),
        live_vectors=int(health.get("live_vectors") or 0),
        orphan_vectors=int(health.get("orphan_vectors") or 0),
        worktree_docs=int(health.get("worktree_docs") or 0),
        search_volume=int(qm.get("search_volume") or 0),
        zero_result_rate=float(qm.get("zero_result_rate") or 0.0),
        hit_rate=float(qm.get("hit_rate") or 0.0),
        avg_latency_ms=qm.get("avg_latency_ms"),
    )


@router.get("/search", response_model=SearchResponse)
def semantic_search(
    q: str = Query(..., min_length=1, description="Natural-language query"),
    n: int = Query(10, ge=1, le=50, description="Max results"),
    file_type: Optional[str] = Query(None, description="Filter by file role"),
    project: Optional[str] = Query(None, description="Path prefix filter"),
) -> SearchResponse:
    """Hybrid semantic + keyword search via VectorStore.search."""
    try:
        from okuro.cortex.vectorstore import VectorStore
        vs = VectorStore()
        results = vs.search(
            q,
            n_results=n,
            filter_role=file_type or None,
            filter_path_prefix=project or None,
        )
    except Exception as exc:
        logger.exception("cortex semantic search failed")
        raise HTTPException(500, f"Semantic search failed: {exc}")

    hits = [
        SearchHit(
            path=str(r.file_path),
            score=float(r.relevance),
            snippet=r.snippet,
            purpose=r.purpose or None,
            matched_section=r.matched_section,
            keyword_score=r.keyword_score,
        )
        for r in results
    ]
    # Scope guardrail — same annotation the MCP tools attach (shared module).
    # Never let a hint failure break search.
    scope = None
    try:
        from okuro.cortex.scope_hints import scope_annotation
        scope = scope_annotation(project, len(hits))
    except Exception:
        logger.debug("scope annotation failed", exc_info=True)
    return SearchResponse(query=q, results=hits, scope=scope)


@router.get("/search-code", response_model=CodeSearchResponse)
def literal_search(
    q: str = Query(..., min_length=1, description="Literal/regex pattern"),
    path: Optional[str] = Query(None, description="Subdir relative to OKURO_ROOT"),
    n: int = Query(20, ge=1, le=200, description="Max matches"),
) -> CodeSearchResponse:
    """Ripgrep-backed literal search (with Python fallback)."""
    base = _ROOT
    if path:
        base = _safe_path(path)
        if not base.exists():
            raise HTTPException(404, f"path not found: {path}")

    import shutil
    has_rg = shutil.which("rg") is not None
    matches: list[CodeHit] = []

    if has_rg:
        try:
            cmd = ["rg", "--json", "-m", str(n), q, str(base)]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
            import json as _json
            for line in proc.stdout.splitlines():
                try:
                    obj = _json.loads(line)
                    if obj.get("type") == "match":
                        d = obj["data"]
                        matches.append(CodeHit(
                            path=d["path"]["text"],
                            line=int(d["line_number"]),
                            text=d["lines"]["text"].rstrip("\n"),
                        ))
                        if len(matches) >= n:
                            break
                except Exception:
                    continue
            return CodeSearchResponse(query=q, backend="ripgrep", matches=matches)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "ripgrep search timed out")
        except Exception as exc:
            logger.exception("ripgrep failed, falling back to python")

    # Fallback: same routine as cortex.mcp_tools._py_grep, but local so we
    # don't have to import handle_tool's async machinery.
    from okuro.cortex.mcp_tools import _py_grep
    for m in _py_grep(q, base, n):
        matches.append(CodeHit(path=m["path"], line=m["line"], text=m["text"]))
    return CodeSearchResponse(query=q, backend="python", matches=matches)


@router.get("/route", response_model=SearchResponse)
def route(q: str = Query(..., min_length=1)) -> SearchResponse:
    """Top-5 files for a concept (cortex_route parity)."""
    try:
        from okuro.cortex.vectorstore import VectorStore
        vs = VectorStore()
        results = vs.search(q, n_results=5)
    except Exception as exc:
        logger.exception("cortex route failed")
        raise HTTPException(500, f"Route failed: {exc}")

    hits = [
        SearchHit(
            path=str(r.file_path),
            score=float(r.relevance),
            snippet=r.snippet,
            purpose=r.purpose or None,
            matched_section=r.matched_section,
            keyword_score=r.keyword_score,
        )
        for r in results
    ]
    # Scope guardrail — route has no project scope, so only the empty-result
    # guidance can fire (never let a hint failure break route).
    scope = None
    try:
        from okuro.cortex.scope_hints import scope_annotation
        scope = scope_annotation(None, len(hits))
    except Exception:
        logger.debug("scope annotation failed", exc_info=True)
    return SearchResponse(query=q, results=hits, scope=scope)


@router.get("/header", response_model=HeaderResponse)
def header(path: str = Query(..., min_length=1)) -> HeaderResponse:
    """Return parsed AGENT_HEADER for a file (purpose + section index)."""
    resolved = _safe_path(path)
    if not resolved.is_file():
        raise HTTPException(404, f"File not found: {path}")

    try:
        from okuro.cortex.core import parse_header
        content = resolved.read_text(errors="replace")
        parsed = parse_header(content, file_path=resolved)
    except Exception as exc:
        logger.exception("cortex header parse failed")
        raise HTTPException(500, f"Header parse failed: {exc}")

    if parsed is None:
        return HeaderResponse(path=path, found=False)

    index = []
    for entry in getattr(parsed, "index", []) or []:
        # IndexEntry shape varies; coerce to dict defensively.
        if hasattr(entry, "description"):
            index.append({
                "description": getattr(entry, "description", ""),
                "line": getattr(entry, "line", None),
            })
        elif isinstance(entry, dict):
            index.append(entry)
        else:
            index.append({"description": str(entry), "line": None})

    role_val = getattr(parsed, "role", None)
    role_str = role_val.value if hasattr(role_val, "value") else (str(role_val) if role_val else None)

    return HeaderResponse(
        path=path,
        found=True,
        purpose=getattr(parsed, "purpose", None),
        role=role_str,
        index=index,
    )


@router.get("/file", response_model=FileSliceResponse)
def file_slice(
    path: str = Query(..., min_length=1),
    start: int = Query(1, ge=1, description="First line (1-based)"),
    end: Optional[int] = Query(None, ge=1, description="Last line (1-based, inclusive)"),
    max_lines: int = Query(1000, ge=1, le=5000, description="Cap on returned lines"),
) -> FileSliceResponse:
    """Read a bounded slice of a file. Defaults: first 1000 lines."""
    resolved = _safe_path(path)
    if not resolved.is_file():
        raise HTTPException(404, f"File not found: {path}")

    try:
        text = resolved.read_text(errors="replace")
    except Exception as exc:
        logger.exception("cortex file read failed")
        raise HTTPException(500, f"File read failed: {exc}")

    lines = text.splitlines()
    total = len(lines)
    if end is None:
        end = min(start + max_lines - 1, total)
    end = min(end, total, start + max_lines - 1)
    slice_text = "\n".join(lines[start - 1:end])

    return FileSliceResponse(
        path=path,
        start_line=start,
        end_line=end,
        total_lines=total,
        content=slice_text,
    )


# ── Reindex (background) ─────────────────────────────────────────────


def _run_reindex(job_id: str, root: Path, project: Optional[str] = None) -> None:
    """Worker — runs inside FastAPI BackgroundTasks threadpool."""
    try:
        from okuro.cortex.vectorstore import VectorStore
        from okuro.cortex.roots import project_for_path
        vs = VectorStore()

        # Light progress: the scanner emits a callback per file. We don't
        # know the total ahead of time cheaply, so we just count as we go
        # and surface it via _REINDEX_JOBS[id]["indexed_so_far"].
        counter = {"n": 0}

        def _progress(_path: Path) -> None:
            counter["n"] += 1
            with _REINDEX_LOCK:
                if job_id in _REINDEX_JOBS:
                    _REINDEX_JOBS[job_id]["indexed_so_far"] = counter["n"]

        resolved_project = project or project_for_path(root)
        total, indexed = vs.index_directory(
            root, progress_callback=_progress, project=resolved_project
        )

        with _REINDEX_LOCK:
            if job_id in _REINDEX_JOBS:
                _REINDEX_JOBS[job_id].update({
                    "status": "done",
                    "finished_at": time.time(),
                    "total": total,
                    "indexed": indexed,
                })
    except Exception as exc:
        logger.exception("cortex reindex job %s failed", job_id)
        with _REINDEX_LOCK:
            if job_id in _REINDEX_JOBS:
                _REINDEX_JOBS[job_id].update({
                    "status": "failed",
                    "finished_at": time.time(),
                    "error": str(exc),
                })


@router.post("/reindex", response_model=ReindexResponse)
def post_reindex(
    request: Request,
    background_tasks: BackgroundTasks,
    path: Optional[str] = Query(None, description="Subdir to reindex (default: OKURO_ROOT)"),
    project: Optional[str] = Query(
        None,
        description="Reindex a specific registered project slug (e.g. 'okuro'). "
        "Mutually exclusive with `path`.",
    ),
) -> ReindexResponse:
    """Kick off a reindex. Returns immediately with a job id to poll.

    Scope precedence: `project` slug > explicit `path` > OKURO_ROOT.
    """
    _require_localhost_cx(request)

    if project and path:
        raise HTTPException(400, "Pass either `project` or `path`, not both.")

    resolved_project: Optional[str] = None
    target = _ROOT

    if project:
        from okuro.cortex.roots import registered_roots

        match = next(
            (r for r in registered_roots() if r.project == project), None
        )
        if not match:
            raise HTTPException(
                404,
                f"Unknown project slug '{project}' — not in registered roots.",
            )
        target = match.path
        resolved_project = match.project
    elif path:
        target = _safe_path(path)
        if not target.is_dir():
            raise HTTPException(400, f"Not a directory: {path}")

    job_id = _secrets.token_urlsafe(8)
    started = time.time()
    with _REINDEX_LOCK:
        _REINDEX_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "started_at": started,
            "finished_at": None,
            "total": None,
            "indexed": None,
            "indexed_so_far": 0,
            "error": None,
            "root": str(target),
            "project": resolved_project,
        }

    background_tasks.add_task(_run_reindex, job_id, target, resolved_project)
    return ReindexResponse(job_id=job_id, status="running", started_at=started)


# ── Projects / registered roots ──────────────────────────────────────


@router.get("/projects", response_model=ProjectsResponse)
def list_cortex_projects() -> ProjectsResponse:
    """List every registered cortex root + indexed-file counts per project."""
    from okuro.cortex.roots import registered_roots
    from okuro.cortex.vectorstore import VectorStore

    try:
        stats = VectorStore().get_stats()
    except Exception:
        stats = {"per_project": []}
    per_proj = {p["project"]: p for p in stats.get("per_project", [])}

    roots = []
    for r in registered_roots():
        key = r.project or "__unassigned__"
        info = per_proj.get(key, {})
        roots.append(RegisteredRoot(
            slug=r.project,
            path=str(r.path),
            files=int(info.get("files") or 0),
            documents=int(info.get("documents") or 0),
        ))

    return ProjectsResponse(schema_version="025", roots=roots)


@router.post("/projects", response_model=RegisterProjectResponse)
def register_cortex_project(
    request: Request,
    background_tasks: BackgroundTasks,
    body: RegisterProjectRequest,
) -> RegisterProjectResponse:
    """Register a directory as an indexed cortex root.

    localhost-only because it mutates the projects table (same policy as
    reindex). Pass `reindex: true` to immediately kick off indexing.
    """
    _require_localhost_cx(request)

    from okuro.cortex.roots import register_root

    try:
        result = register_root(body.path, slug=body.slug, name=body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    reindex_job_id: Optional[str] = None
    if body.reindex:
        job_id = _secrets.token_urlsafe(8)
        started = time.time()
        target = Path(result["path"])
        with _REINDEX_LOCK:
            _REINDEX_JOBS[job_id] = {
                "job_id": job_id,
                "status": "running",
                "started_at": started,
                "finished_at": None,
                "total": None,
                "indexed": None,
                "indexed_so_far": 0,
                "error": None,
                "root": str(target),
                "project": result["slug"],
            }
        background_tasks.add_task(_run_reindex, job_id, target, result["slug"])
        reindex_job_id = job_id

    return RegisterProjectResponse(
        slug=result["slug"],
        name=result["name"],
        path=result["path"],
        action=result["action"],
        pre_existing_slug=result.get("pre_existing_slug"),
        reindex_job_id=reindex_job_id,
    )


@router.get("/reindex/status", response_model=ReindexStatus)
def reindex_status(job: str = Query(..., min_length=1)) -> ReindexStatus:
    """Return current state for a reindex job (running/done/failed)."""
    with _REINDEX_LOCK:
        entry = _REINDEX_JOBS.get(job)
    if not entry:
        raise HTTPException(404, f"Unknown reindex job: {job}")

    # While running, fall back to indexed_so_far for the `indexed` field so
    # the progress bar can move.
    indexed_val = entry.get("indexed")
    if entry["status"] == "running" and indexed_val is None:
        indexed_val = entry.get("indexed_so_far") or 0

    return ReindexStatus(
        job_id=entry["job_id"],
        status=entry["status"],
        started_at=entry["started_at"],
        finished_at=entry.get("finished_at"),
        total=entry.get("total"),
        indexed=indexed_val,
        error=entry.get("error"),
        root=entry["root"],
    )
