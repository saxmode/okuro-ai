# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Preview API — REST + SSE wrapper around okuro.orchestrator.preview launcher.
# index:
#   imports
#   models
#   helpers
#   GET  /api/preview/{task_id}
#   POST /api/preview/{task_id}/start
#   POST /api/preview/{task_id}/stop
#   GET  /api/preview/{task_id}/logs
#   GET  /api/preview/{task_id}/file
# AGENT_HEADER_END -->
"""Preview REST + SSE endpoints.

Thin wrapper around :mod:`okuro.orchestrator.preview.launcher` — never
duplicate launcher logic here, only adapt it to HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re as _re
import shutil
from pathlib import Path, PurePosixPath
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from okuro.orchestrator.preview import (
    BUILD_LOG_FILE,
    KNOWN_TEMPLATES,
    LauncherError,
    PreviewState,
    ProposerMiss,
    RecipeError,
    load_recipe,
    logs_tail as launcher_logs_tail,
    propose_recipe as launcher_propose_recipe,
    save_recipe as launcher_save_recipe,
    start_background as launcher_start_background,
    status as launcher_status,
    stop as launcher_stop,
)
from okuro.db.engine import okuro_home

logger = logging.getLogger("okuro.orchestrator.api.preview")

# Mirror api/main.py — OKURO_ROOT defaults to ~/.okuro/orchestrator.
OKURO_ROOT = Path(os.environ.get("OKURO_ROOT", okuro_home() / "orchestrator"))
TASKS_DIR = OKURO_ROOT / "tasks"

router = APIRouter(prefix="/api/preview", tags=["preview"])

_TASK_ID_RE = _re.compile(r"^task(?:-[a-z]+)?-\d{8}-\d{6}(?:-[A-Za-z0-9_-]+)?$")


# ── Models ──────────────────────────────────────────────────────────


class PreviewStateResponse(BaseModel):
    state: str
    slug: str
    unit: Optional[str] = None
    port: Optional[int] = None
    url: Optional[str] = None
    started_at: Optional[str] = None
    recipe_present: bool = False
    last_error: Optional[str] = None


class StartRequest(BaseModel):
    force_rebuild: bool = False
    auto_detect: bool = True


class RecipeBody(BaseModel):
    """Free-form recipe body — schema is enforced by the recipe validator."""

    body: dict


# ── Helpers ─────────────────────────────────────────────────────────


def _resolve_task_dir(task_id: str) -> Path:
    if not _TASK_ID_RE.match(task_id):
        raise HTTPException(400, f"Invalid task_id format: {task_id}")
    task_dir = (TASKS_DIR / task_id).resolve()
    if not str(task_dir).startswith(str(TASKS_DIR.resolve())):
        raise HTTPException(400, "Invalid task_id: path traversal detected")
    if not task_dir.exists():
        raise HTTPException(404, f"Task not found: {task_id}")
    return task_dir


def _serve_url(task_id: str, file: str) -> str:
    """Build the capability-token URL for the directory-serving endpoint.

    The bearer rides in a PATH segment (not a header or query) so the
    browser preserves it when resolving the document's RELATIVE sub-
    resources — ``./contract/tokens.css``, sibling ``.html`` iframes —
    which can carry neither an Authorization header nor a ``?token=``
    query of their own. Same capability-in-path model as ``/api/q/``.

    ``_API_TOKEN`` is imported lazily to avoid a circular import (main.py
    imports this router at module load). Handing the token to an already-
    authenticated caller (every /status response is bearer-gated) is not a
    new disclosure — the same client can fetch it from /api/auth/token.
    """
    from okuro.orchestrator.api.main import _API_TOKEN  # lazy: avoid cycle

    parts = "/".join(quote(seg) for seg in PurePosixPath(file).parts)
    return f"/api/preview/{task_id}/serve/{quote(_API_TOKEN, safe='')}/{parts}"


def _to_response(state: PreviewState, *, task_id: str, task_dir: Path) -> PreviewStateResponse:
    data = state.to_dict()
    # For non-serve kinds (doc/image/download) the launcher leaves url=None
    # because it doesn't own HTTP routing — fill it in here so callers have
    # a single field to open regardless of recipe kind.
    if state.state == "ready" and not data.get("url"):
        try:
            recipe = load_recipe(task_dir, slug_default=task_id)
        except RecipeError:
            recipe = None
        if recipe is not None and not recipe.expects_serve() and recipe.file:
            # Serve through /serve/{token}/ so a doc's relative sub-resources
            # (CSS/JS/iframes) resolve under the same authed path. Falls back
            # to the legacy single-file /file endpoint shape implicitly for
            # any client that still hits it.
            data["url"] = _serve_url(task_id, recipe.file)
    return PreviewStateResponse(**data)


# ── Endpoints ───────────────────────────────────────────────────────


@router.get("/{task_id}", response_model=PreviewStateResponse)
def get_status(task_id: str) -> PreviewStateResponse:
    task_dir = _resolve_task_dir(task_id)
    return _to_response(
        launcher_status(task_dir, slug_default=task_id),
        task_id=task_id, task_dir=task_dir,
    )


@router.post("/{task_id}/start", response_model=PreviewStateResponse)
def post_start(task_id: str, body: StartRequest | None = None) -> PreviewStateResponse:
    """Kick off a build/serve in the background; return immediately.

    Previously this endpoint blocked for the full duration of npm ci +
    vite build + ready check (often minutes), keeping the SPA's
    PreviewButton stuck on "Starting…" the whole time. The launcher now
    spawns a daemon thread; we return ``state="building"`` synchronously
    and rely on the SPA polling /status (or, when wired, the WS
    ``preview_state_changed`` event) to follow the lifecycle.

    Errors that can be diagnosed BEFORE the thread spawns (recipe parse,
    proposer miss, stale recipe) still surface as 409 here so the user
    sees the right toast. Build/serve failures land in runtime.json with
    state="failed" and last_error — the SPA picks them up on next poll.
    """
    task_dir = _resolve_task_dir(task_id)
    force = bool(body and body.force_rebuild)
    auto_detect = True if body is None else body.auto_detect
    try:
        return _to_response(
            launcher_start_background(
                task_dir, slug_default=task_id,
                force_rebuild=force, auto_detect=auto_detect,
                on_state=_make_state_pusher(task_id, task_dir),
            ),
            task_id=task_id, task_dir=task_dir,
        )
    except RecipeError as exc:
        # 409 = well-formed request, pre-conditions not met.
        # If auto-detect ran and missed, the launcher attaches diagnostic
        # attributes the SPA uses to render a helpful toast.
        if getattr(exc, "proposer_miss", False):
            raise HTTPException(409, detail={
                "error": "auto_detect_miss",
                "message": str(exc),
                "scanned": getattr(exc, "scanned", []),
                "known_templates": getattr(exc, "known_templates", list(KNOWN_TEMPLATES)),
                # P5.6 — what the scan DID find, so the UI can offer a choice
                # instead of telling the user to write a preview.yaml.
                "candidates": getattr(exc, "candidates", []),
            }) from exc
        raise HTTPException(409, str(exc)) from exc
    except LauncherError as exc:
        raise HTTPException(500, str(exc)) from exc


def _make_state_pusher(task_id: str, task_dir: Path):
    """Build an ``on_state`` callback that broadcasts every launcher
    transition over the task WebSocket as ``preview_state_changed``.

    The callback runs on the background launcher thread, NOT on the
    asyncio event loop — we hand the broadcast off via
    ``run_coroutine_threadsafe`` so we don't block the launcher waiting
    for the broadcast to land on every connected client.

    The loop reference comes from ``event_watcher.loop`` — same pattern
    used by ``broadcast_agent_event``. Best-effort: if the manager or
    loop is unreachable (test harness, pre-boot), the push is dropped
    silently — runtime.json is still authoritative for state, so the
    SPA's polling fallback recovers within ~1.5 s.
    """
    import asyncio
    try:
        from okuro.orchestrator.api.main import ws_manager, event_watcher  # type: ignore
    except Exception:  # noqa: BLE001 — keeps module importable in unit tests
        return None

    def _push(state: PreviewState) -> None:
        loop = getattr(event_watcher, "loop", None)
        if loop is None or not loop.is_running():
            return
        payload = {
            "type": "preview_state_changed",
            "task_id": task_id,
            "preview": _to_response(state, task_id=task_id, task_dir=task_dir).model_dump(),
        }
        try:
            asyncio.run_coroutine_threadsafe(
                ws_manager.broadcast_to_task(task_id, payload), loop,
            )
        except Exception as exc:  # noqa: BLE001 — broadcast failures must never block the launcher
            logger.debug("preview WS push failed for %s: %s", task_id, exc)

    return _push


@router.get("/{task_id}/recipe/proposal")
def get_recipe_proposal(task_id: str) -> dict:
    """Run the proposer and return the draft recipe **without saving**."""
    task_dir = _resolve_task_dir(task_id)
    try:
        return launcher_propose_recipe(task_dir, slug_default=task_id)
    except ProposerMiss as miss:
        raise HTTPException(409, detail={
            "error": "auto_detect_miss",
            "message": str(miss),
            "scanned": miss.scanned,
            "known_templates": list(miss.known),
            "candidates": miss.candidates,
        }) from miss


@router.put("/{task_id}/recipe", response_model=PreviewStateResponse)
def put_recipe(task_id: str, payload: RecipeBody) -> PreviewStateResponse:
    """Save a user-supplied recipe to the task dir, then return current status."""
    task_dir = _resolve_task_dir(task_id)
    try:
        launcher_save_recipe(task_dir, payload.body)
    except RecipeError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _to_response(
        launcher_status(task_dir, slug_default=task_id),
        task_id=task_id, task_dir=task_dir,
    )


@router.post("/{task_id}/stop", response_model=PreviewStateResponse)
def post_stop(task_id: str) -> PreviewStateResponse:
    task_dir = _resolve_task_dir(task_id)
    try:
        return _to_response(
            launcher_stop(task_dir, slug_default=task_id),
            task_id=task_id, task_dir=task_dir,
        )
    except LauncherError as exc:
        raise HTTPException(500, str(exc)) from exc


@router.get("/{task_id}/logs")
def get_logs(task_id: str, tail: int = 200, stream: bool = False):
    """Tail the unified preview log (build log + journal merge).

    Without ``?stream=true``: one-shot JSON ``{"lines": [...]}`` with
    the last ``tail`` lines from the merged build log + journal.

    With ``?stream=true``: a Server-Sent Events stream that keeps the
    drawer live. Tails ``preview.build.log`` (always written, regardless
    of recipe kind — covers npm ci/build/nbconvert/ffmpeg/state markers)
    AND, when a serve unit exists, ``journalctl -f`` for the runtime
    log. The two are interleaved in arrival order; the SSE event tags
    ``source=build|service`` so the SPA can colour them differently
    if it wants.

    Build-log SSE works on every host (including macOS where journalctl
    is missing) — that's the fix for the previous "drawer renders nothing"
    bug for kind=doc/image/audio/video/slides/notebook/external.
    """
    task_dir = _resolve_task_dir(task_id)
    if not stream:
        return {"lines": launcher_logs_tail(task_dir, slug_default=task_id, lines=tail)}

    state = launcher_status(task_dir, slug_default=task_id)
    build_log_path = task_dir / BUILD_LOG_FILE
    has_journal = bool(state.unit) and shutil.which("journalctl") is not None
    journal_unit = state.unit if has_journal else None

    async def _gen():
        """Replay the build-log tail, then poll for new appends every
        ~500 ms while also reading from journalctl -f when a unit exists.

        Single async generator (no inner queue/multiplex) — the previous
        queue-based design returned a 200 OK but emitted zero bytes,
        likely because the inner ``asyncio.create_task`` calls didn't
        get scheduled before StreamingResponse iterated the outer
        generator. A direct loop keeps things simple and verifiable.
        """
        # 0. Send a comment frame immediately so the EventSource client
        # sees the connection is alive even before the first log line
        # exists (kind=doc tasks finish before any subprocess output).
        yield ": connected\n\n"

        if not build_log_path.exists():
            build_log_path.touch()

        # 1. Replay the last ~8 KB of the build log so a freshly-opened
        # drawer always has context, then follow appends.
        fh = build_log_path.open("r", errors="replace")
        try:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            if size > 8192:
                fh.readline()  # discard partial first line
            for line in fh.readlines():
                yield f"event: build\ndata: {line.rstrip()}\n\n"
        except OSError:
            pass

        # 2. Optional journal pump — kept on the main loop via
        # asyncio.subprocess so we can interleave reads with the
        # build-log poll without spawning a task.
        journal_proc = None
        if journal_unit:
            try:
                journal_proc = await asyncio.create_subprocess_exec(
                    "journalctl", "--user-unit", journal_unit,
                    "-f", "--no-pager", "-o", "cat", "-n", "0",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except FileNotFoundError:
                journal_proc = None

        try:
            # 3. Follow loop: alternate between build-log readline (sync,
            # non-blocking at EOF returns "") and journal readline (async,
            # bounded by short timeout). Heartbeat comment every ~15 s so
            # proxies don't reap an idle stream.
            heartbeat_at = asyncio.get_event_loop().time() + 15.0
            while True:
                emitted = False

                # Build-log tail
                line = fh.readline()
                while line:
                    yield f"event: build\ndata: {line.rstrip()}\n\n"
                    emitted = True
                    line = fh.readline()

                # Journal tail (one line per pass; non-blocking via wait_for)
                if journal_proc and journal_proc.stdout is not None:
                    try:
                        jline = await asyncio.wait_for(
                            journal_proc.stdout.readline(), timeout=0.4,
                        )
                        if jline:
                            yield f"event: service\ndata: {jline.decode(errors='replace').rstrip()}\n\n"
                            emitted = True
                    except asyncio.TimeoutError:
                        pass

                # Heartbeat / yield to loop when nothing happened
                now = asyncio.get_event_loop().time()
                if not emitted:
                    if now >= heartbeat_at:
                        yield ": ping\n\n"
                        heartbeat_at = now + 15.0
                    await asyncio.sleep(0.5)
        finally:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
            if journal_proc is not None:
                try:
                    journal_proc.terminate()
                    await asyncio.wait_for(journal_proc.wait(), timeout=2)
                except (ProcessLookupError, asyncio.TimeoutError):
                    try:
                        journal_proc.kill()
                    except ProcessLookupError:
                        pass

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable proxy buffering (Caddy / nginx)
            "Connection": "keep-alive",
        },
    )


@router.get("/{task_id}/file")
def get_file(task_id: str):
    """Serve the recipe's `file:` artifact for kind=doc/image/download."""
    task_dir = _resolve_task_dir(task_id)
    try:
        recipe = load_recipe(task_dir, slug_default=task_id)
    except RecipeError as exc:
        raise HTTPException(409, str(exc)) from exc

    if not recipe.file:
        raise HTTPException(409, f"Recipe kind={recipe.kind!r} has no `file:` field.")

    file_path = Path(recipe.file)
    if not file_path.is_absolute():
        file_path = (recipe.cwd / file_path).resolve()

    # Containment: file must live inside the task dir or the recipe cwd.
    allowed_roots = {recipe.cwd.resolve(), task_dir.resolve()}
    if not any(str(file_path).startswith(str(root)) for root in allowed_roots):
        raise HTTPException(400, "file path escapes recipe cwd / task dir.")

    if not file_path.exists():
        raise HTTPException(404, f"File not found: {recipe.file}")

    # Served deliverables get a relaxed, deliverable-scoped CSP — the app's
    # strict default-src/script-src (set by SecurityHeadersMiddleware via
    # setdefault) blocks the inline <script> standalone decks rely on, which
    # renders them BLANK. Setting the header here pre-empts the middleware's
    # setdefault so only this endpoint is relaxed; the SPA stays strict.
    from okuro.orchestrator.api.security_headers import DELIVERABLE_CSP

    return FileResponse(
        file_path,
        headers={"Content-Security-Policy": DELIVERABLE_CSP},
    )


@router.get("/{task_id}/serve/{token}/{subpath:path}")
def serve_asset(task_id: str, token: str, subpath: str):
    """Serve any file under the recipe cwd for a kind=doc deliverable.

    Capability-in-path auth: ``token`` is a PATH segment so the browser
    preserves it when resolving the document's relative sub-resources
    (``./contract/tokens.css``, sibling ``.html`` iframes), which can't
    attach an Authorization header or a ``?token=`` query themselves. The
    global bearer middleware exempts ``/api/preview/{id}/serve/`` for
    exactly this reason (same model as ``/api/q/``); auth happens HERE.

    Without this, a doc with relative assets served single-file via /file
    rendered unstyled — every sub-resource resolved against the API path
    and 404'd (or 401'd through the bearer gate).
    """
    import secrets as _secrets

    from okuro.orchestrator.api.main import _API_TOKEN  # lazy: avoid cycle

    if not _secrets.compare_digest(token, _API_TOKEN):
        raise HTTPException(403, "Invalid preview capability token")

    task_dir = _resolve_task_dir(task_id)
    try:
        recipe = load_recipe(task_dir, slug_default=task_id)
    except RecipeError as exc:
        raise HTTPException(409, str(exc)) from exc

    # Resolve subpath under the recipe cwd; reject traversal escapes.
    base = recipe.cwd.resolve()
    file_path = (base / subpath).resolve()
    allowed_roots = {base, task_dir.resolve()}
    if not any(str(file_path).startswith(str(root)) for root in allowed_roots):
        raise HTTPException(400, "path escapes recipe cwd / task dir.")
    if not file_path.is_file():
        raise HTTPException(404, f"File not found: {subpath}")

    from okuro.orchestrator.api.security_headers import DELIVERABLE_CSP

    return FileResponse(
        file_path,
        headers={"Content-Security-Policy": DELIVERABLE_CSP},
    )
