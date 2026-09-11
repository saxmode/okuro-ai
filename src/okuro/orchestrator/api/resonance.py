# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: /api/resonance router — the communication-compiler surface:
#   ingest → analyze → interview → answer → render. Thin HTTP over okuro.resonance.
# index: imports | router | ingest | analyze | interview | answer | render
# AGENT_HEADER_END -->
"""okuro·resonance API — the communication compiler over HTTP.

Thin surface over ``okuro.resonance`` (ingest/gap/interview/pco_builder/render),
mirroring the 5 MCP tools so the frontend and okuro's own chat can drive the
whole chain: land documents in the brain → gap-gate against the goal →
interview the user for tacit gaps → build a provenance-tagged PCO → render it
audience-fitted into a medium. Auth is the global bearer middleware.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

logger = logging.getLogger("okuro.orchestrator.api.resonance")

router = APIRouter(prefix="/api/resonance", tags=["resonance"])


@router.get("/site/{site_id}", response_class=HTMLResponse)
def resonance_site(site_id: str) -> HTMLResponse:
    """Serve a rendered website (media='website') — a self-contained scroll-reveal
    HTML page stored as a text/html evidence artifact. Bearer-protected like every
    /api route; public sharing would need an explicit auth decision."""
    from okuro.sense.artifacts import artifact_get

    a = artifact_get(site_id)
    if not isinstance(a, dict) or a.get("media_type") != "text/html" or not a.get("body"):
        raise HTTPException(404, "site not found")
    return HTMLResponse(a["body"])


@router.post("/ingest")
def resonance_ingest(payload: dict) -> dict:
    """INTAKE. Body: {text, title, source_ref?, project?}. Returns the ingest
    manifest with artifact_id + claims_added."""
    from okuro.resonance import ingest_document

    text = (payload.get("text") or "").strip()
    title = (payload.get("title") or "").strip()
    if not text or not title:
        raise HTTPException(400, "text and title required")
    try:
        return ingest_document(
            text, title=title, source_ref=payload.get("source_ref"),
            project=payload.get("project"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("ingest failed")
        raise HTTPException(500, str(exc))


@router.post("/analyze")
def resonance_analyze(payload: dict) -> dict:
    """ANALYZE (the gate). Body: {goal, source_artifact_ids?, project?}.
    Returns completeness + requirements + open_questions + ready."""
    from okuro.resonance import analyze_gaps

    goal = (payload.get("goal") or "").strip()
    if not goal:
        raise HTTPException(400, "goal required")
    try:
        return analyze_gaps(
            goal, source_artifact_ids=payload.get("source_artifact_ids"),
            project=payload.get("project"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("analyze failed")
        raise HTTPException(500, str(exc))


@router.post("/interview")
def resonance_interview(payload: dict) -> dict:
    """ENRICH. Body: {goal, source_artifact_ids?, max_q?, project?}. Returns the
    next interview questions (tacit gaps) + completeness + research_gaps."""
    from okuro.resonance import next_questions

    goal = (payload.get("goal") or "").strip()
    if not goal:
        raise HTTPException(400, "goal required")
    try:
        return next_questions(
            goal, source_artifact_ids=payload.get("source_artifact_ids"),
            max_q=int(payload.get("max_q", 3)), project=payload.get("project"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("interview failed")
        raise HTTPException(500, str(exc))


@router.post("/answer")
def resonance_answer(payload: dict) -> dict:
    """ENRICH. Body: {goal, question, answer, project?}. Ingests the Q&A as
    evidence; returns the manifest (artifact_id to add to source_artifact_ids)."""
    from okuro.resonance import record_answer

    goal = (payload.get("goal") or "").strip()
    question = (payload.get("question") or "").strip()
    answer = (payload.get("answer") or "").strip()
    if not (goal and question and answer):
        raise HTTPException(400, "goal, question and answer required")
    try:
        return record_answer(goal, question, answer, project=payload.get("project"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("answer failed")
        raise HTTPException(500, str(exc))


@router.post("/research")
def resonance_research(payload: dict) -> dict:
    """RESEARCH (autonomous enrich). Body: {goal, source_artifact_ids?, max_gaps?,
    project?}. Web-researches research-route gaps and ingests findings. Returns
    new_artifact_ids to add to source_artifact_ids. Slow (web)."""
    from okuro.resonance import resolve_research_gaps

    goal = (payload.get("goal") or "").strip()
    if not goal:
        raise HTTPException(400, "goal required")
    try:
        return resolve_research_gaps(
            goal, source_artifact_ids=payload.get("source_artifact_ids"),
            max_gaps=int(payload.get("max_gaps", 3)), project=payload.get("project"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("research failed")
        raise HTTPException(500, str(exc))


@router.post("/render")
def resonance_render(payload: dict) -> dict:
    """PREPARE + RENDER. Body: {goal, source_artifact_ids?, audience?, media?,
    brand_id?, project?}. Builds a PCO from the brain and renders it for the
    audience into the medium. Returns {pco digest, render result}."""
    from okuro.resonance import render_from_brain

    goal = (payload.get("goal") or "").strip()
    if not goal:
        raise HTTPException(400, "goal required")
    media = payload.get("media") or "prism"
    if media not in ("prism", "slides", "website"):
        raise HTTPException(400, "media must be 'prism', 'slides' or 'website'")
    try:
        return render_from_brain(
            goal, source_artifact_ids=payload.get("source_artifact_ids"),
            audience=payload.get("audience"), media=media,
            brand_id=payload.get("brand_id") or "okuro",
            project=payload.get("project"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("render failed")
        raise HTTPException(500, str(exc))


# ── Durable interview sessions ───────────────────────────────────────────────


@router.post("/session/create")
def resonance_session_create(payload: dict) -> dict:
    """ENRICH. Body: {goal, project?, person_id?, brand_id?, source_artifact_ids?}.
    Opens a durable, resumable interview session. Returns the session."""
    from okuro.resonance import session_create

    goal = (payload.get("goal") or "").strip()
    if not goal:
        raise HTTPException(400, "goal required")
    try:
        return session_create(
            goal, project=payload.get("project"), person_id=payload.get("person_id"),
            brand_id=payload.get("brand_id"),
            source_artifact_ids=payload.get("source_artifact_ids"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("session_create failed")
        raise HTTPException(500, str(exc))


@router.post("/session/next")
def resonance_session_next(payload: dict) -> dict:
    """ENRICH. Body: {session_id, max_q?}. Compute + persist the next questions."""
    from okuro.resonance import session_next

    sid = (payload.get("session_id") or "").strip()
    if not sid:
        raise HTTPException(400, "session_id required")
    try:
        return session_next(sid, max_q=int(payload.get("max_q", 3)))
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("session_next failed")
        raise HTTPException(500, str(exc))


@router.post("/session/answer")
def resonance_session_answer(payload: dict) -> dict:
    """ENRICH. Body: {session_id, question, answer}. Record a Q&A + recompute."""
    from okuro.resonance import session_answer

    sid = (payload.get("session_id") or "").strip()
    question = (payload.get("question") or "").strip()
    answer = (payload.get("answer") or "").strip()
    if not (sid and question and answer):
        raise HTTPException(400, "session_id, question and answer required")
    try:
        return session_answer(sid, question, answer)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("session_answer failed")
        raise HTTPException(500, str(exc))


@router.get("/session/{session_id}")
def resonance_session_get(session_id: str) -> dict:
    """ENRICH. Full session incl. turn log (for resume / UI)."""
    from okuro.resonance import session_get

    s = session_get(session_id)
    if not s:
        raise HTTPException(404, f"session not found: {session_id}")
    return s


@router.get("/sessions")
def resonance_session_list(status: str | None = None, project: str | None = None) -> dict:
    """ENRICH. List sessions (newest first), optional status/project filter."""
    from okuro.resonance import session_list

    return {"sessions": session_list(status=status, project=project)}


@router.post("/actuality/check")
def resonance_actuality_check(payload: dict) -> dict:
    """ACTUALITY. Body: {source_artifact_ids}. Flag stale grounding evidence."""
    from okuro.resonance import check_actuality

    ids = payload.get("source_artifact_ids")
    if not isinstance(ids, list):
        raise HTTPException(400, "source_artifact_ids (array) required")
    try:
        return check_actuality(ids, max_age=payload.get("max_age"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("actuality_check failed")
        raise HTTPException(500, str(exc))


@router.post("/actuality/refresh")
def resonance_actuality_refresh(payload: dict) -> dict:
    """ACTUALITY. Body: {source_artifact_ids, project?}. Re-research stale web
    sources; returns new_artifact_ids to swap in. Slow (web)."""
    from okuro.resonance import refresh_stale

    ids = payload.get("source_artifact_ids")
    if not isinstance(ids, list):
        raise HTTPException(400, "source_artifact_ids (array) required")
    try:
        return refresh_stale(ids, project=payload.get("project"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("actuality_refresh failed")
        raise HTTPException(500, str(exc))
