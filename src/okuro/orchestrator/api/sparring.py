# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: /api/sparring router — the stateful prism sparring session: list,
#   state, start, move, change-feed. Thin HTTP surface over okuro.prism.sparring,
#   mirroring /api/prism (same shapes, same bearer auth, same events tail).
# index: imports | router | list | events | state | start | move | close | delete
# AGENT_HEADER_END -->
"""okuro·prism sparring API — stateful multi-turn session over HTTP.

Thin surface over ``okuro.prism.sparring`` (model/turn-loop/KG). Auth is the
global bearer middleware (same as every /api router). Mirrors
``okuro.orchestrator.api.prism`` deliberately so the frontend client
(``lib/sparring-api.ts``) reads like a sibling of ``lib/prism-api.ts``.

Unlike prism (facet-tree docs), a sparring session is a turn log; the response
carries both the derived live board (``state``) and, on demand, the full turn
history — so the ``/sparring`` timeline view can render without reshaping.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

logger = logging.getLogger("okuro.orchestrator.api.sparring")

router = APIRouter(prefix="/api/sparring", tags=["sparring"])


@router.get("")
def list_sparring_sessions(topic: str = "") -> dict:
    """List sessions (newest first). Optional ``topic`` filters by its slug."""
    from okuro.prism import sparring

    key = sparring.slugify(topic.strip()) if topic.strip() else None
    sessions = sparring.list_sessions(topic_key=key)
    return {"sessions": [s.to_summary() for s in sessions], "seq": sparring.latest_seq()}


@router.get("/events")
def sparring_events(since: int = 0) -> dict:
    """Change-feed tail — rows with seq > ``since`` (oldest first) + latest seq,
    so an open session view polls for turns added by any process."""
    from okuro.prism import sparring

    return {"events": sparring.events_since(since), "seq": sparring.latest_seq()}


@router.get("/{session_id}")
def get_sparring_session(session_id: str) -> dict:
    """Full turn log + the derived live board."""
    from okuro.prism import sparring

    s = sparring.get_session(session_id)
    if s is None:
        raise HTTPException(404, f"session '{session_id}' not found")
    return {**s.to_detail(), "state": sparring.session_state(s.turns)}


@router.post("/start")
def start_sparring_session(payload: dict) -> dict:
    """Open a session. Body: {topic, person_id?, brand_id?}. If prior sessions
    sparred this topic, the session opens with a KG-recall turn."""
    from okuro.prism import sparring

    topic = (payload.get("topic") or "").strip()
    if not topic:
        raise HTTPException(400, "topic required")
    try:
        s = sparring.start_session(
            topic,
            person_id=(payload.get("person_id") or "").strip() or None,
            brand_id=(payload.get("brand_id") or "").strip() or None,
            origin="web",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"start failed: {exc}")
    return {**s.to_detail(), "state": sparring.session_state(s.turns)}


@router.post("/{session_id}/move")
def move_sparring_session(session_id: str, payload: dict) -> dict:
    """Advance a session by one move. Body: {type, ...} — see
    ``okuro.prism.sparring.advance`` for the move schema. A ``panel`` move runs
    the multi-model red-team (may take up to ~2 min)."""
    from okuro.prism import sparring

    if not (payload.get("type") or "").strip():
        raise HTTPException(400, "move 'type' required")
    try:
        s = sparring.advance(session_id, payload, origin="web")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"move failed: {exc}")
    return {**s.to_detail(), "state": sparring.session_state(s.turns)}


@router.post("/{session_id}/close")
def close_sparring_session(session_id: str) -> dict:
    from okuro.prism import sparring

    s = sparring.close_session(session_id, origin="web")
    if s is None:
        raise HTTPException(404, f"session '{session_id}' not found")
    return {**s.to_detail(), "state": sparring.session_state(s.turns)}


@router.delete("/{session_id}")
def delete_sparring_session(session_id: str, origin: str = "") -> dict:
    from okuro.prism import sparring

    return {"deleted": sparring.delete_session(session_id, origin=origin)}
