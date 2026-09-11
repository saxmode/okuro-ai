# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-slides storage — SQLite CRUD + change-feed for the deck builder.
# index:
#   imports
#   class DeckDoc
#   def slugify
#   def list_decks
#   def get_deck
#   def save_deck
#   def delete_deck
#   def events_since
#   def latest_seq
# AGENT_HEADER_END -->
"""okuro-slides storage — SQLite-backed CRUD + append-only change-feed.

A deck is the full scene IR (see the frontend ``scene.ts``) stored as one JSON
blob in ``deck``: ``{id, title, arrangement, size, transition, background,
slides[]}``. ``arrangement`` and ``slide_count`` are denormalised columns for
cheap listing. Mirrors ``okuro.flow_designer`` deliberately — same persistence
shape, same cross-process change-feed (``slides_events``) so an open deck
live-updates whether the web user or an agent (chat) mutated it.

Last-write-wins (single-user tool); the one hard invariant is that an empty
deck cannot clobber a populated one unless ``allow_empty`` is set.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from okuro.db import get_db

logger = logging.getLogger("okuro.slides")

_EVENTS_KEEP = 500

# The baseline canvas every element coord is authored against, and the ONE
# source of truth for it (``okuro.slides.generate`` imports this as ``_CANVAS``;
# the frontend mirrors it as ``DEFAULT_CANVAS`` in ``components/slides/scene.ts``).
# The frontend reads ``deck.size.w`` unconditionally, so a deck persisted or
# served without a valid ``size`` crashes the whole Slides tab — this layer now
# guarantees one on every write AND every read (``save_deck`` / ``_row_to_doc``),
# so no path (generate, apply_ops, retailor, direct save, legacy row) can leak a
# size-less deck to the renderer.
DEFAULT_CANVAS = {"w": 1280, "h": 720}


def _coerce_size(size: Any) -> dict[str, Any]:
    """A deck's baseline canvas, guaranteed valid — falls back to
    ``DEFAULT_CANVAS`` when ``size`` is missing or malformed (not a dict, or
    non-positive / non-numeric w|h)."""
    if isinstance(size, dict):
        w, h = size.get("w"), size.get("h")
        if (
            isinstance(w, (int, float)) and not isinstance(w, bool)
            and isinstance(h, (int, float)) and not isinstance(h, bool)
            and w > 0 and h > 0
        ):
            return {"w": w, "h": h}
    return dict(DEFAULT_CANVAS)


@dataclass
class DeckDoc:
    """One okuro-slides deck. ``deck`` is the full scene-IR payload."""

    id: str
    title: str
    arrangement: str = "horizontal"
    deck: dict[str, Any] = field(default_factory=dict)
    slide_count: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "arrangement": self.arrangement,
            "slide_count": self.slide_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_detail(self) -> dict:
        return {**self.to_summary(), "deck": self.deck}


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return s or "deck"


def _row_to_doc(row: dict) -> DeckDoc:
    try:
        deck = json.loads(row["deck"]) if row.get("deck") else {}
    except (ValueError, TypeError):
        deck = {}
    if deck:
        # Heal a legacy/malformed row on read so it renders instead of crashing
        # the tab; it re-persists with the size on its next save.
        deck["size"] = _coerce_size(deck.get("size"))
    return DeckDoc(
        id=row["id"],
        title=row["title"],
        arrangement=row.get("arrangement") or "horizontal",
        deck=deck,
        slide_count=int(row.get("slide_count") or 0),
        created_at=row.get("created_at") or "",
        updated_at=row.get("updated_at") or "",
    )


def _unique_id(db, base: str, *, ignore: str | None = None) -> str:
    candidate = base
    n = 1
    while True:
        if candidate == ignore:
            return candidate
        if db.fetchone("SELECT id FROM slides_decks WHERE id = ?", (candidate,)) is None:
            return candidate
        n += 1
        candidate = f"{base}-{n}"


def list_decks() -> list[DeckDoc]:
    db = get_db()
    rows = db.fetchall(
        "SELECT id, title, arrangement, '' AS deck, slide_count, created_at, "
        "updated_at FROM slides_decks ORDER BY updated_at DESC"
    )
    return [_row_to_doc(r) for r in rows]


def get_deck(deck_id: str) -> DeckDoc | None:
    db = get_db()
    row = db.fetchone("SELECT * FROM slides_decks WHERE id = ?", (deck_id,))
    return _row_to_doc(row) if row else None


def _emit(conn, deck_id: str, kind: str, origin: str) -> None:
    conn.execute(
        "INSERT INTO slides_events (deck_id, kind, origin) VALUES (?, ?, ?)",
        (deck_id, kind, origin or ""),
    )
    conn.execute(
        "DELETE FROM slides_events WHERE seq <= "
        "(SELECT MAX(seq) FROM slides_events) - ?",
        (_EVENTS_KEEP,),
    )


def save_deck(
    *,
    id: str | None = None,
    title: str,
    deck: dict[str, Any] | None = None,
    origin: str = "",
    allow_empty: bool = False,
) -> DeckDoc:
    """Upsert a deck. Generates a unique slug id from ``title`` when ``id`` is
    absent. The scene IR is normalised so ``deck.id``/``deck.title`` track the
    row. Emits a ``saved`` change-feed event. An empty deck (no slides) cannot
    overwrite a populated one unless ``allow_empty``."""
    title = (title or "").strip()
    if not title:
        raise ValueError("title required")

    deck = deck if isinstance(deck, dict) else {}
    slides = deck.get("slides") if isinstance(deck.get("slides"), list) else []
    deck["slides"] = slides
    arrangement = deck.get("arrangement") if deck.get("arrangement") in ("horizontal", "vertical") else "horizontal"
    deck["arrangement"] = arrangement
    # Validate on write: no deck may ever persist without a usable size.
    deck["size"] = _coerce_size(deck.get("size"))
    slide_count = len(slides)

    db = get_db()
    with db.write() as conn:
        existing_id = (id or "").strip() or None
        existing = None
        if existing_id:
            existing = conn.execute(
                "SELECT slide_count FROM slides_decks WHERE id = ?", (existing_id,)
            ).fetchone()

        if existing_id and existing:
            deck_id = existing_id
            prior = int(existing["slide_count"] or 0)
            if slide_count == 0 and prior > 0 and not allow_empty:
                raise ValueError("refusing to empty a populated deck without allow_empty")
        else:
            deck_id = _unique_id(db, slugify(existing_id or title))

        deck["id"] = deck_id
        deck["title"] = title
        deck_json = json.dumps(deck, separators=(",", ":"))

        if existing_id and existing:
            conn.execute(
                "UPDATE slides_decks SET title = ?, arrangement = ?, deck = ?, "
                "slide_count = ?, updated_at = datetime('now') WHERE id = ?",
                (title, arrangement, deck_json, slide_count, deck_id),
            )
        else:
            conn.execute(
                "INSERT INTO slides_decks (id, title, arrangement, deck, slide_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (deck_id, title, arrangement, deck_json, slide_count),
            )
        _emit(conn, deck_id, "saved", origin)

    saved = get_deck(deck_id)
    assert saved is not None
    return saved


def delete_deck(deck_id: str, *, origin: str = "") -> bool:
    db = get_db()
    with db.write() as conn:
        if conn.execute("SELECT id FROM slides_decks WHERE id = ?", (deck_id,)).fetchone() is None:
            return False
        conn.execute("DELETE FROM slides_decks WHERE id = ?", (deck_id,))
        _emit(conn, deck_id, "deleted", origin)
    return True


def events_since(seq: int) -> list[dict]:
    db = get_db()
    return db.fetchall(
        "SELECT seq, deck_id, kind, origin, ts FROM slides_events "
        "WHERE seq > ? ORDER BY seq ASC",
        (int(seq),),
    )


def latest_seq() -> int:
    db = get_db()
    row = db.fetchone("SELECT MAX(seq) AS s FROM slides_events")
    return int(row["s"]) if row and row.get("s") is not None else 0
