# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism deck2 STORE — persistence for compiled DeckDoc JSON (the
#   A4 viewer contract), keyed by deck id. Deliberately separate from the legacy
#   facet-tree prism_docs sqlite store: deck2 is a distinct data model (L0-L3
#   DeckDoc vs L1-L4 PrismTree), so it gets its own flat JSON store rather than
#   overloading migration-versioned tables. The /api/prism/deck2 router reads +
#   writes through here; apply_pick mirrors the frontend's applyPick (A/B swap).
# index: deck_path | DeckDestinationError | assert_landed | save_deck | get_deck | list_decks | apply_pick | set_brand | delete_deck | STORE_DIR
# AGENT_HEADER_END -->
"""Flat JSON store for compiled deck2 DeckDocs.

One file per deck (``<STORE_DIR>/<id>.json``). The compiler writes a DeckDoc
here; the viewer fetches it by id and POSTs A/B picks back. No schema migrations,
no concurrency story beyond last-write-wins — decks are small, single-user, and
regenerated on demand.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from okuro.prism.brands import a4_brands, is_a4_brand
from okuro.db.engine import okuro_home

STORE_DIR = okuro_home() / "prism-deck2"
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


def _dir() -> Path:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    return STORE_DIR


def deck_path(deck_id: str) -> Path:
    """Where a deck id lands. Public so callers can ASSERT a destination rather
    than trust a returned id — see :class:`DeckDestinationError`."""
    safe = _SAFE_ID.sub("_", str(deck_id)) or "deck"
    return _dir() / f"{safe}.json"


_path = deck_path  # historical private alias


class DeckDestinationError(RuntimeError):
    """A save reported success but the deck is not in the deck2 store.

    This is the July-27 failure class as an exception. That run's acceptance
    criteria were LLM-judged text ("the deck was saved"), every one passed, and
    the deck had gone to the legacy facet engine. A destination is a fact about
    the filesystem, so it is checked as one.

    Deliberately NOT behind ``prism.strict``: that flag is the rollback for
    QUALITY refusals (quotas, shape typing, capacity), which a user may
    legitimately want to override. There is no build worth shipping whose deck
    is not where the caller was told it is — an escape hatch here would only
    restore the ability to report a success that did not happen.
    """


def assert_landed(deck_id: str) -> Path:
    """Verify ``deck_id`` resolves to a real file inside the deck2 store."""
    store = STORE_DIR.resolve()
    p = deck_path(deck_id).resolve()
    if not p.is_relative_to(store):
        raise DeckDestinationError(
            f"deck {deck_id!r} resolved to {p} — outside the deck2 store ({store})")
    if not p.is_file():
        raise DeckDestinationError(
            f"deck {deck_id!r} reported saved but {p} does not exist — the write "
            "did not land in the deck2 store")
    return p


def save_deck(doc: dict[str, Any]) -> str:
    """Persist a DeckDoc; returns its id. Requires doc['id'].

    Verifies its own write landed in the deck2 store before returning the id, so
    no caller — workflow, compiler or API — can report a save that went nowhere.
    """
    deck_id = str(doc.get("id") or "").strip()
    if not deck_id:
        raise ValueError("save_deck: DeckDoc has no id")
    deck_path(deck_id).write_text(json.dumps(doc, ensure_ascii=False, indent=2))
    assert_landed(deck_id)
    return deck_id


def get_deck(deck_id: str) -> Optional[dict[str, Any]]:
    p = _path(deck_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def list_decks() -> list[dict[str, Any]]:
    """Overview index for the /prism gallery: per deck an id, title, brand, the
    file's mtime (``created`` — the low-touch creation proxy, since a DeckDoc
    carries no own timestamp), the topic count, and the tagline. Sorted newest
    first so the index arrives pre-ordered; the frontend re-sorts on its axes."""
    import datetime as _dt

    out: list[dict[str, Any]] = []
    for p in _dir().glob("*.json"):
        try:
            d = json.loads(p.read_text())
        except Exception:  # noqa: BLE001 — a corrupt file just drops from the index
            continue
        created = _dt.datetime.fromtimestamp(p.stat().st_mtime, _dt.timezone.utc).isoformat()
        out.append({
            "id": d.get("id", p.stem),
            "title": d.get("title", p.stem),
            "brand": d.get("brand", ""),
            "created": created,
            "topics": len(d.get("topics") or []),
            "tagline": d.get("tagline", "") or "",
        })
    out.sort(key=lambda r: r["created"], reverse=True)  # newest first
    return out


def apply_pick(deck_id: str, row: str, col: int, pick: int) -> Optional[dict[str, Any]]:
    """Record an A/B pick on a cell and persist. Mirrors the frontend applyPick:
    row is 'hero' | 'L0' | 'L1' | 'L2' (L3 has no alternates); col is the topic
    index. Returns the updated DeckDoc, or None if the deck is missing."""
    doc = get_deck(deck_id)
    if doc is None:
        return None
    if row == "hero":
        doc.setdefault("hero", {})["pick"] = pick
    elif row in ("L0", "L1", "L2"):
        topics = doc.get("topics") or []
        if 0 <= col < len(topics):
            cell = (topics[col].get("levels") or {}).get(row)
            if cell is not None:
                cell["pick"] = pick
    # L3 / unknown rows are no-ops (doc-view has no A/B alternate).
    save_deck(doc)
    return doc


def set_brand(deck_id: str, brand: str) -> Optional[dict[str, Any]]:
    """Live restyle: switch the deck's brand (VISUALS only — the viewer repaints
    from the CSS-var theme; no content regeneration). Returns the updated DeckDoc,
    or None if missing. Unknown brands are rejected (ValueError)."""
    b = (brand or "").strip()
    if not is_a4_brand(b):
        raise ValueError(f"unknown brand {brand!r}; expected one of {sorted(a4_brands())}")
    doc = get_deck(deck_id)
    if doc is None:
        return None
    doc["brand"] = b
    save_deck(doc)
    return doc


def delete_deck(deck_id: str) -> bool:
    """Delete a stored deck. Returns True if a file was removed."""
    p = _path(deck_id)
    if not p.exists():
        return False
    p.unlink()
    return True
