# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: HTTP surface for contextual reviews — the web UI's write/read path.
# index: ReviewSubmit | SurfaceEntry | post_review | list_reviews |
#   get_surfaces | post_surfaces
# AGENT_HEADER_END -->
"""Reviews API — "write down, in context, what I don't like".

POST /api/reviews             append one review (may create a todo)
GET  /api/reviews             read reviews, newest first
GET  /api/reviews/surfaces    per-surface rollup (product health)
POST /api/reviews/surfaces    reconcile the frontend's live surface catalogue

Thin over :mod:`okuro.sense.reviews`; all policy (the severity->todo gate,
orphan tombstoning) lives there so the MCP tools and this router cannot drift.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from okuro.sense import reviews as reviews_svc

logger = logging.getLogger("okuro.orchestrator.api.reviews")

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


class ReviewSubmit(BaseModel):
    # Declared identity. Deliberately required: a review that cannot say what
    # it is about is unroutable later, and the frontend always has a surface
    # (the route-level default when no component declared a finer one).
    surface_id: str = Field(..., min_length=1)
    comment: Optional[str] = None
    rating: Optional[int] = Field(None, ge=1, le=5)
    severity: str = "annoyance"
    target_type: str = "unresolved"
    target_id: Optional[str] = None
    # Context / evidence — never identity.
    route: Optional[str] = None
    route_params: Optional[dict[str, Any]] = None
    viewport: Optional[str] = None
    app_version: Optional[str] = None
    # Option D — overlay evidence. A DOM path is a HINT, not identity.
    dom_hint: Optional[str] = None
    screenshot_ref: Optional[str] = None
    project: Optional[str] = None
    make_todo: Optional[bool] = None


class SurfaceEntry(BaseModel):
    surface_id: str = Field(..., min_length=1)
    label: Optional[str] = None
    route: Optional[str] = None


class SurfaceCatalogue(BaseModel):
    surfaces: list[SurfaceEntry] = Field(default_factory=list)


@router.post("")
def post_review(body: ReviewSubmit) -> dict:
    """Append one review. 422 on a rejected vocabulary or an empty review."""
    try:
        return reviews_svc.review_add(
            body.surface_id,
            comment=body.comment,
            rating=body.rating,
            severity=body.severity,
            target_type=body.target_type,
            target_id=body.target_id,
            route=body.route,
            route_params=body.route_params,
            viewport=body.viewport,
            app_version=body.app_version,
            dom_hint=body.dom_hint,
            screenshot_ref=body.screenshot_ref,
            project=body.project,
            make_todo=body.make_todo,
        )
    except ValueError as exc:
        # The service owns the vocabularies; surface its message rather than a
        # bare 500 so the user sees which value was wrong.
        raise HTTPException(422, str(exc)) from exc


@router.get("")
def list_reviews(
    surface_id: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    limit: int = 50,
) -> dict:
    rows = reviews_svc.review_list(
        surface_id, target_type=target_type, target_id=target_id, limit=limit
    )
    return {"reviews": rows, "count": len(rows)}


@router.get("/surfaces")
def get_surfaces(include_orphaned: bool = False) -> dict:
    rows = reviews_svc.surface_summary(include_orphaned=include_orphaned)
    return {"surfaces": rows, "count": len(rows)}


@router.get("/sync")
def sync_status() -> dict:
    """Can this install forward reviews, and if not, what is missing?

    The UI asks this to guide an install that UPDATED into the feature and has
    no org_label yet — the review is already saved locally either way.
    """
    from okuro.sense import review_sync
    return review_sync.readiness()


class SyncConfig(BaseModel):
    sync_enabled: Optional[bool] = None
    org_label: Optional[str] = None


@router.put("/sync")
def put_sync_config(body: SyncConfig) -> dict:
    """Save the two fields the operator owns, then drain the backlog.

    Endpoint and key are shipped defaults — the maintainer's, not the
    operator's — so they are not editable here.
    """
    from okuro.sense import review_sync
    return review_sync.set_config(
        sync_enabled=body.sync_enabled, org_label=body.org_label
    )


@router.post("/sync/retry")
def retry_failed_syncs() -> dict:
    """Release reviews that gave up, once the cause is fixed."""
    from okuro.sense import review_sync
    released = review_sync.retry_failed()
    return {"released": released, **review_sync.sync_pending()}


@router.get("/sync/preview")
def sync_preview() -> dict:
    """The EXACT object that would be transmitted, for the most recent review.

    Informed consent has to be literal: the operator sees the bytes, including
    the comment text, before enabling sync — not a description of them.
    """
    from okuro.sense import review_sync

    rows = reviews_svc.review_list(limit=1)
    sample = rows[0] if rows else {
        "id": "<review id>", "surface_id": "route:/work",
        "severity": "annoyance", "comment": "<your comment text>",
        "rating": None, "route": "/work", "viewport": "1440x900",
        "app_version": None, "created_at": "<timestamp>",
    }
    return {
        "fields": list(review_sync.SENT_FIELDS),
        "payload": review_sync.payload_for(sample),
        "note": (
            "Entity ids, DOM hints and route params are never sent. "
            "The comment text IS sent verbatim."
        ),
    }


@router.post("/sync")
def run_sync(limit: int = 50) -> dict:
    """Drain the queue now. Safe to call repeatedly."""
    from okuro.sense import review_sync
    return review_sync.sync_pending(limit=limit)


@router.post("/surfaces")
def post_surfaces(body: SurfaceCatalogue) -> dict:
    """Reconcile the live catalogue the frontend reports on boot.

    Absent surfaces are tombstoned, never deleted — see reviews.register_surfaces.
    """
    return reviews_svc.register_surfaces(
        [e.model_dump() for e in body.surfaces]
    )
