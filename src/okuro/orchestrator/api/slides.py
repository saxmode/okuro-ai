# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: /api/slides router — CRUD + change-feed for okuro-slides decks.
# index: imports | router | list | get | save | delete | events
# AGENT_HEADER_END -->
"""okuro-slides API — deck CRUD + change-feed.

Thin HTTP surface over ``okuro.slides.storage``. Auth is the global bearer
middleware (same as every other /api router). The change-feed endpoint lets an
open deck poll for cross-process edits (web user or agent/chat).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger("okuro.orchestrator.api.slides")

router = APIRouter(prefix="/api/slides", tags=["slides"])


@router.get("")
def list_slides() -> dict:
    from okuro.slides import latest_seq, list_decks

    return {"decks": [d.to_summary() for d in list_decks()], "seq": latest_seq()}


@router.get("/events")
def slides_events(since: int = 0) -> dict:
    """Change-feed tail — rows with seq > ``since`` (oldest first) + the latest
    seq, so an open deck can poll for edits made by any process."""
    from okuro.slides import events_since, latest_seq

    return {"events": events_since(since), "seq": latest_seq()}


@router.get("/variants")
def slides_variants(source: str) -> dict:
    """List a deck's audience variants. ``source`` may be the source deck id OR
    any variant id (resolved to its source). Returns {source, variants[]} where
    each entry carries variantOf + variantMeta for the UI's variant tabs."""
    from okuro.slides.retailor import list_variants

    src = (source or "").strip()
    if not src:
        raise HTTPException(400, "source required")
    return list_variants(src)


@router.get("/{deck_id}")
def get_slides(deck_id: str) -> dict:
    from okuro.slides import get_deck

    doc = get_deck(deck_id)
    if doc is None:
        raise HTTPException(404, f"deck '{deck_id}' not found")
    return doc.to_detail()


@router.post("/generate")
def generate_slides(payload: dict) -> dict:
    """Generate a recipient-tailored, brand-styled deck from a topic and persist
    it. Body: {topic, person_id?, brand_id?}. Sync (runs in the threadpool) —
    one LLM call, ~10-40s. Returns the saved deck detail."""
    from okuro.slides.generate import generate_deck

    topic = (payload.get("topic") or "").strip()
    if not topic:
        raise HTTPException(400, "topic required")
    try:
        return generate_deck(
            topic,
            person_id=(payload.get("person_id") or "").strip() or None,
            brand_id=(payload.get("brand_id") or "okuro").strip(),
            mode=(payload.get("mode") or "fast").strip(),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"generate failed: {exc}")


@router.get("/generate/sse")
def generate_slides_sse(request: Request):
    """Streaming generate — SSE activity events ('drafting outline…', 'outline → …',
    'laying out…', 'saving…') then a final ``done`` with the saved deck. Query:
    prompt/topic, person_id?, brand_id?, mode? (fast|quality). Token via ?token=."""
    import json as _json

    from fastapi.responses import StreamingResponse

    from okuro.slides.generate import generate_deck_events

    q = request.query_params
    topic = (q.get("prompt") or q.get("topic") or "").strip()
    if not topic:
        raise HTTPException(400, "topic required")
    person_id = (q.get("person_id") or "").strip() or None
    brand_id = (q.get("brand_id") or "okuro").strip()
    mode = (q.get("mode") or "fast").strip()

    def gen():
        yield ":" + (" " * 2048) + "\n\n"  # flush WebKit's SSE buffer
        try:
            for ev in generate_deck_events(topic, person_id=person_id, brand_id=brand_id, mode=mode):
                yield f"data: {_json.dumps(ev)}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"data: {_json.dumps({'type': 'error', 'error': str(exc)})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/edit")
def edit_slides(payload: dict) -> dict:
    """Apply a natural-language edit/review to an existing deck (structured ops).
    Body: {deck_id, instruction}. The open canvas live-updates via the change-feed."""
    from okuro.slides.generate import edit_deck

    deck_id = (payload.get("deck_id") or payload.get("id") or "").strip()
    instruction = (payload.get("instruction") or "").strip()
    if not deck_id:
        raise HTTPException(400, "deck_id required")
    if not instruction:
        raise HTTPException(400, "instruction required")
    try:
        return edit_deck(deck_id, instruction)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"edit failed: {exc}")


@router.post("/retailor")
def retailor_slides(payload: dict) -> dict:
    """Re-tailor a deck to a recipient + density/jargon/language axes. Body:
    {deck_id, person_id?, density?, jargon?, language?, as_variant?}. Text-only
    edit via scoped ops (geometry + ids stable). as_variant (default true) saves
    a NEW linked variant; false edits in place. Returns the saved deck detail."""
    from okuro.slides.retailor import retailor_deck

    deck_id = (payload.get("deck_id") or payload.get("id") or "").strip()
    if not deck_id:
        raise HTTPException(400, "deck_id required")
    try:
        return retailor_deck(
            deck_id,
            person_id=(payload.get("person_id") or "").strip() or None,
            density=(payload.get("density") or "").strip() or None,
            jargon=(payload.get("jargon") or "").strip() or None,
            language=(payload.get("language") or "").strip() or None,
            as_variant=bool(payload.get("as_variant", True)),
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"retailor failed: {exc}")


@router.post("")
def save_slides(payload: dict) -> dict:
    """Create or update a deck. Body: {id?, title, deck, origin?, allow_empty?}.
    ``deck`` is the full scene IR (size, transition, background, arrangement,
    slides[])."""
    from okuro.slides import save_deck

    title = (payload.get("title") or "").strip()
    deck = payload.get("deck") if isinstance(payload.get("deck"), dict) else {}
    if not title:
        title = str(deck.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    try:
        doc = save_deck(
            id=(payload.get("id") or "").strip() or None,
            title=title,
            deck=deck,
            origin=(payload.get("origin") or "").strip(),
            allow_empty=bool(payload.get("allow_empty")),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return doc.to_detail()


@router.delete("/{deck_id}")
def delete_slides(deck_id: str, origin: str = "") -> dict:
    from okuro.slides import delete_deck

    return {"deleted": delete_deck(deck_id, origin=origin)}
