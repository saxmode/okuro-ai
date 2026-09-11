# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Daily digest API — unresolved thoughts, action items, forgotten ideas.
# index:
#   imports
#   router
#   models
#   def _parse_meta
#   def _get_digest
# AGENT_HEADER_END -->
"""Daily digest API — structured JSON shape for the home-page digest card.

The MCP tool ``daily_digest`` returns markdown; this endpoint returns
the same underlying data as three typed lists so the SPA can render
collapsible sections with counts and per-item links.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger("okuro.orchestrator.api.digest")

router = APIRouter(prefix="/api/digest", tags=["digest"])


# ── Models ───────────────────────────────────────────────────────────


class ThoughtRef(BaseModel):
    id: str
    content: str
    category: Optional[str] = None
    status: Optional[str] = None
    project: Optional[str] = None
    created_at: Optional[str] = None


class ActionItem(BaseModel):
    id: str  # composite: "<thought_id>#<index>" so each is stably addressable
    thought_id: str
    text: str
    project: Optional[str] = None
    created_at: Optional[str] = None


class TodoItem(BaseModel):
    id: str
    title: str
    detail: Optional[str] = None
    status: str
    priority: int
    project: Optional[str] = None
    due_at: Optional[str] = None
    source: Optional[str] = None
    created_at: Optional[str] = None


class DigestResponse(BaseModel):
    unresolved_thoughts: list[ThoughtRef]
    action_items: list[ActionItem]
    forgotten_ideas: list[ThoughtRef]
    todos: list[TodoItem]
    generated_at: str


# ── Helpers ──────────────────────────────────────────────────────────


def _parse_meta(raw) -> dict:
    if isinstance(raw, str):
        try:
            return json.loads(raw) or {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return raw or {}


# ── Endpoint ─────────────────────────────────────────────────────────


@router.get("", response_model=DigestResponse)
def _get_digest(limit: int = 5, forgotten_days: int = 14) -> DigestResponse:
    """Three parallel sections — all derived from ``thoughts`` table.

    - ``unresolved_thoughts``: status='open', most recent N
    - ``action_items``: any open thought whose metadata contains
      ``action_items[]``, flattened so each item is its own row
    - ``forgotten_ideas``: status='open', surface_count=0, older than
      ``forgotten_days`` days

    Each of these is capped at ``limit`` rows to keep the home page
    lightweight; the UI can link to /brain for the full list.
    """
    from okuro.db import get_db

    db = get_db()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=forgotten_days)).strftime("%Y-%m-%d")

    unresolved: list[ThoughtRef] = []
    actions: list[ActionItem] = []
    forgotten: list[ThoughtRef] = []
    todos: list[TodoItem] = []

    try:
        # Unresolved thoughts — most recent open
        rows = db.fetchall(
            """
            SELECT id, content, metadata, status, project, created_at
              FROM thoughts
             WHERE status = 'open'
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (limit,),
        )
        for r in rows:
            meta = _parse_meta(r["metadata"])
            unresolved.append(
                ThoughtRef(
                    id=r["id"],
                    content=r["content"] or "",
                    category=meta.get("category"),
                    status=r["status"],
                    project=r["project"],
                    created_at=r["created_at"],
                )
            )

        # Action items — flatten metadata.action_items[]
        rows = db.fetchall(
            """
            SELECT id, content, metadata, project, created_at
              FROM thoughts
             WHERE status = 'open'
               AND json_extract(metadata, '$.action_items') IS NOT NULL
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (limit * 2,),  # room to flatten to N items
        )
        for r in rows:
            meta = _parse_meta(r["metadata"])
            items = meta.get("action_items") or []
            for idx, item in enumerate(items):
                if len(actions) >= limit:
                    break
                text = item if isinstance(item, str) else str(item)
                actions.append(
                    ActionItem(
                        id=f"{r['id']}#{idx}",
                        thought_id=r["id"],
                        text=text,
                        project=r["project"],
                        created_at=r["created_at"],
                    )
                )
            if len(actions) >= limit:
                break

        # Todos — dedicated `todos` table (distinct from thoughts). Surfaces
        # the user-facing actionable list ordered by priority then due date
        # then recency. Open + doing are both "active" from the Now-page POV.
        rows = db.fetchall(
            """
            SELECT id, title, detail, status, priority, project,
                   due_at, source, created_at
              FROM todos
             WHERE status IN ('open', 'doing')
               AND id NOT LIKE 'stream-stub-%'
             ORDER BY priority DESC,
                      CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,
                      due_at ASC,
                      created_at DESC
             LIMIT ?
            """,
            (limit,),
        )
        for r in rows:
            todos.append(
                TodoItem(
                    id=r["id"],
                    title=r["title"] or "",
                    detail=r["detail"],
                    status=r["status"],
                    priority=r["priority"] or 3,
                    project=r["project"],
                    due_at=r["due_at"],
                    source=r["source"],
                    created_at=r["created_at"],
                )
            )

        # Forgotten ideas — open, never re-surfaced, older than cutoff
        rows = db.fetchall(
            """
            SELECT id, content, metadata, status, project, created_at
              FROM thoughts
             WHERE status = 'open'
               AND surface_count = 0
               AND created_at < ?
             ORDER BY created_at ASC
             LIMIT ?
            """,
            (cutoff, limit),
        )
        for r in rows:
            meta = _parse_meta(r["metadata"])
            forgotten.append(
                ThoughtRef(
                    id=r["id"],
                    content=r["content"] or "",
                    category=meta.get("category"),
                    status=r["status"],
                    project=r["project"],
                    created_at=r["created_at"],
                )
            )
    except Exception as exc:  # pragma: no cover — defensive; empty DB = empty sections
        logger.warning("digest query failed: %s", exc)

    # Surface-log every thought shown to the UI. Dedup per context so a
    # thought producing multiple action_item rows logs once; a thought
    # appearing in two lists logs once per list (= per context).
    # Wrapped so a surfacing-log failure never breaks /api/digest.
    #
    # CRITICAL: do NOT log the forgotten-ideas bucket. Its own filter is
    # `surface_count = 0`, so logging the surface immediately ejects the
    # thought from the bucket on the next /api/digest call. The bucket
    # exists exactly to keep nudging unengaged thoughts every digest
    # until the user acts on them. Same fix as okuro.sense.thoughts
    # daily_digest (commit 034b631) — mirrored here so the web home page
    # has the same self-extinguish protection as the MCP digest tool.
    try:
        from okuro.sense.surface import log_thought_surface

        by_context: dict[str, set[str]] = {
            "api_digest_unresolved": {t.id for t in unresolved},
            "api_digest_action_items": {a.thought_id for a in actions},
        }
        for context, ids in by_context.items():
            for tid in ids:
                log_thought_surface(tid, context)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("digest surface-log failed: %s", exc)

    return DigestResponse(
        unresolved_thoughts=unresolved,
        action_items=actions,
        forgotten_ideas=forgotten,
        todos=todos,
        generated_at=now.isoformat(),
    )
