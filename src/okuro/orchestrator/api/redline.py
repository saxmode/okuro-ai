# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: redline HTTP surface — the bearer-gated write/read routes plus the
#   capability-in-path serve route that renders a document into a sandbox.
# index:
#   models | POST /api/redline/open | GET /api/redline/documents
#   POST|GET /api/redline/comments | PATCH /api/redline/comments/{id}
#   GET /api/redline/doc/{id}/v/{seq}/serve/{token}/{subpath}
# AGENT_HEADER_END -->
"""redline API.

Two auth models on purpose, and the split is the security design:

* Everything the SPA calls carries the GLOBAL bearer. The parent page holds it
  and performs every write; the framed document never sees it.
* The serve route carries a PER-DOCUMENT token in the path, because a browser
  resolving ``./style.css`` or a ``@font-face`` cannot attach a header. That
  token is minted by ``redline_open``, lives in the API process only, expires
  in 8 hours and buys the bytes of exactly one version of one document.

The preview route's in-path token is the global API bearer, and a sandboxed
frame can read it out of ``location.pathname`` — which under ``DELIVERABLE_CSP``
was measured beaconing to an external host with ZERO violations. That is the
whole reason this route mints its own credential and serves under
``REDLINE_CSP``, which carries no ``https:`` in any fetch directive.

Thin over :mod:`okuro.sense.redline`; all policy lives there.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from okuro.sense import redline as redline_svc

logger = logging.getLogger("okuro.orchestrator.api.redline")

router = APIRouter(prefix="/api/redline", tags=["redline"])

# @font-face is a CORS-gated fetch. The document is framed with
# sandbox="allow-scripts" and NO allow-same-origin, so its origin is the string
# "null" and both measured webfaces failed with "Access to font at ... from
# origin 'null' has been blocked by CORS policy". This header flips them to
# loaded. Set in the HANDLER, on this route only — never in middleware, never
# on any other route. Safe here because the frame can then read only bytes it
# is already rendering, under a token scoped to that one document version.
_CORS_HEADER = {"Access-Control-Allow-Origin": "*"}

_HTML_SUFFIXES = {".html", ".htm"}


def _overlay_bundle_path() -> Path:
    """Absolute path to the built in-frame overlay.

    Emitted by ``pnpm build:overlay`` into web/dist-overlay/overlay.js — a
    sibling of web/dist, kept out of it because the SPA build runs with
    ``emptyOutDir: true`` and would wipe it on every rebuild.
    """
    from okuro.web.app import DIST_DIR

    return DIST_DIR.parent / "dist-overlay" / "overlay.js"


class OpenRequest(BaseModel):
    kind: str = Field(..., description="file | artifact | prism")
    ref: str = Field(..., min_length=1)
    title: Optional[str] = None
    project: Optional[str] = None
    reload: bool = False
    # Re-open an EXISTING version read-only — the version switcher's call.
    version_seq: Optional[int] = None
    # Optional element crops. OFF by default: it launches a headless browser,
    # and the viewer opens a document on every mount.
    shots: bool = False


class CommentCreate(BaseModel):
    version_id: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)
    anchor: Optional[dict[str, Any]] = None
    parent_id: Optional[str] = None
    author: str = "owner"


class CommentPatch(BaseModel):
    status: Optional[str] = None          # 'done' resolves, 'open' reopens
    note: Optional[str] = None
    after_excerpt: Optional[str] = None
    done_by: Optional[str] = None
    reply: Optional[str] = None
    reanchor_version_id: Optional[str] = None
    reanchor_anchor: Optional[dict[str, Any]] = None
    reanchor_note: Optional[str] = None


@router.post("/open")
def post_open(body: OpenRequest) -> dict:
    """Mint a version + token and return the serve URL."""
    try:
        return redline_svc.open_document(
            body.kind,
            body.ref,
            title=body.title,
            project=body.project,
            reload=body.reload,
            version_seq=body.version_seq,
            shots=body.shots,
        )
    except redline_svc.RedlineError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/documents")
def get_documents() -> dict:
    """Every document, newest first, with its current version."""
    from okuro.db import get_db

    db = get_db()
    rows = [dict(r) for r in db.fetchall(
        "SELECT * FROM redline_documents ORDER BY created_at DESC", ()
    )]
    out = []
    for row in rows:
        current = db.fetchone(
            "SELECT seq, id FROM redline_versions WHERE document_id = ? "
            "ORDER BY seq DESC LIMIT 1",
            (row["id"],),
        )
        current = dict(current) if current else {}
        out.append({
            "id": row["id"],
            "kind": row["kind"],
            "ref": row["ref"],
            "title": row["title"],
            "project": row["project"],
            "tombstoned_at": row["tombstoned_at"],
            "version_seq": current.get("seq"),
            "version_id": current.get("id"),
        })
    return {"documents": out}


@router.get("/documents/{document_id}/versions")
def get_versions(document_id: str) -> dict:
    """Every version of one document, newest first — the version switcher.

    Design v1.1 §6.1 lists the routes the MCP tools needed; the viewer's
    switcher is the first caller that needs ``redline_versions`` over HTTP,
    and a second implementation of the same query in the SPA would be a second
    place for it to drift.
    """
    try:
        return redline_svc.versions(document_id)
    except redline_svc.RedlineError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/comments")
def post_comment(body: CommentCreate) -> dict:
    """Create a comment. Called by the PARENT page, never by the frame."""
    try:
        return redline_svc.add_comment(
            body.version_id,
            body.body,
            anchor=body.anchor,
            parent_id=body.parent_id,
            author=body.author,
        )
    except redline_svc.RedlineError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/comments")
def get_comments(
    document_id: str,
    version_id: Optional[str] = None,
    status: str = "open",
) -> dict:
    try:
        # The viewer resolves each anchor in the browser to place its bubble,
        # so the HTTP shape carries the anchor. The MCP shape does not.
        return redline_svc.list_comments(
            document_id, version_id=version_id, status=status, include_anchor=True
        )
    except redline_svc.RedlineError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/comments/{comment_id}")
def patch_comment(comment_id: str, body: CommentPatch) -> dict:
    """Status toggle, reply, or a MANUAL re-anchor. One call, one intent."""
    try:
        if body.reply:
            return redline_svc.reply(comment_id, body.reply)
        if body.reanchor_anchor and body.reanchor_version_id:
            return redline_svc.reanchor(
                comment_id,
                body.reanchor_version_id,
                body.reanchor_anchor,
                note=body.reanchor_note,
            )
        if body.status == "done":
            return redline_svc.resolve_comment(
                comment_id,
                body.note or "",
                after_excerpt=body.after_excerpt,
                done_by=body.done_by,
            )
        if body.status == "open":
            return redline_svc.reopen_comment(comment_id)
    except redline_svc.RedlineError as exc:
        raise HTTPException(422, str(exc)) from exc
    raise HTTPException(422, "nothing to change — send status, reply or a re-anchor")


@router.get("/doc/{document_id}/v/{seq}/serve/{token}/{subpath:path}")
def serve_document(document_id: str, seq: int, token: str, subpath: str):
    """Serve one version's bytes under a per-document capability token.

    Auth happens HERE: the global bearer middleware exempts this path prefix
    because the credential is a path segment, not a header — the same
    capability-in-path model as ``/api/preview/{id}/serve/`` and ``/api/q/``.
    Unlike preview, the credential is NOT the global bearer, and presenting
    the global bearer here is a 403 rather than a pass.
    """
    from okuro.orchestrator.api.security_headers import REDLINE_CSP

    try:
        redline_svc.check_token(token, document_id, seq)
    except redline_svc.RedlineTokenError as exc:
        raise HTTPException(exc.status, exc.detail) from exc

    headers = {"Content-Security-Policy": REDLINE_CSP, **_CORS_HEADER}

    if subpath == f"{redline_svc.RESERVED_SUBPATH}/overlay.js":
        bundle = _overlay_bundle_path()
        if not bundle.is_file():
            # A 404 would look like a broken route. The stub says what is
            # missing and how to get it, and the document still renders.
            return PlainTextResponse(
                "/* redline overlay: not built — run `pnpm build:overlay` in "
                "src/okuro/web/frontend. */\n",
                media_type="application/javascript",
                headers=headers,
            )
        return FileResponse(
            bundle, media_type="application/javascript", headers=headers
        )

    try:
        document, version, target = redline_svc.resolve_served_file(
            document_id, seq, subpath
        )
    except redline_svc.RedlineError as exc:
        raise HTTPException(403, str(exc)) from exc
    if not target.is_file():
        raise HTTPException(404, f"File not found: {subpath}")

    if target.suffix.lower() in _HTML_SUFFIXES:
        html = target.read_text(encoding="utf-8", errors="replace")
        html = redline_svc.inject_overlay(
            html, document_id, version["id"], int(seq), token
        )
        return Response(
            content=html,
            media_type="text/html; charset=utf-8",
            headers=headers,
        )
    return FileResponse(target, headers=headers)
