# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism storage — SQLite CRUD + change-feed for the facet-tree doc store.
# index:
#   imports
#   class PrismDoc
#   def slugify
#   def list_docs
#   def get_doc
#   def save_doc
#   def delete_doc
#   def events_since
#   def latest_seq
# AGENT_HEADER_END -->
"""okuro·prism storage — SQLite-backed CRUD + append-only change-feed.

A PrismDoc is the full facet tree stored as one JSON blob in ``doc``:
``{title, brand_id, entry_facet_id, facets: {facet_id: Facet}}``. ``brand_id``
and ``facet_count`` are denormalised columns for cheap listing/filtering.
Unlike ``okuro.slides`` (variantOf lives inside the deck blob), prism's
retailor lineage is the first-class ``variant_of`` column set by migration
073. Mirrors ``okuro.slides.storage`` otherwise — same cross-process
change-feed (``prism_events``) so an open doc live-updates whether the web
user or an agent (chat/MCP) mutated it.

Last-write-wins (single-user tool); the one hard invariant is that an empty
doc cannot clobber a populated one unless ``allow_empty`` is set.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from okuro.db import get_db

logger = logging.getLogger("okuro.prism")

_EVENTS_KEEP = 500


@dataclass
class PrismDoc:
    """One okuro·prism doc. ``doc`` is the full facet-tree payload."""

    id: str
    title: str
    brand_id: str | None = None
    variant_of: str | None = None
    doc: dict[str, Any] = field(default_factory=dict)
    facet_count: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "brand_id": self.brand_id,
            "variant_of": self.variant_of,
            "facet_count": self.facet_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_detail(self) -> dict:
        return {**self.to_summary(), "doc": self.doc}


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return s or "doc"


def _row_to_doc(row: dict) -> PrismDoc:
    try:
        doc = json.loads(row["doc"]) if row.get("doc") else {}
    except (ValueError, TypeError):
        doc = {}
    # Normalise legacy rung keys (glance/brief/working/expert) → L1–L4 on read, so
    # a deck persisted before the slide-model rename renders unchanged.
    try:
        from okuro.prism.rungs import normalize_doc_rungs

        normalize_doc_rungs(doc)
    except Exception:  # noqa: BLE001 — normalisation never blocks a load
        pass
    return PrismDoc(
        id=row["id"],
        title=row["title"],
        brand_id=row.get("brand_id"),
        variant_of=row.get("variant_of"),
        doc=doc,
        facet_count=int(row.get("facet_count") or 0),
        created_at=row.get("created_at") or "",
        updated_at=row.get("updated_at") or "",
    )


def _unique_id(db, base: str, *, ignore: str | None = None) -> str:
    candidate = base
    n = 1
    while True:
        if candidate == ignore:
            return candidate
        if db.fetchone("SELECT id FROM prism_docs WHERE id = ?", (candidate,)) is None:
            return candidate
        n += 1
        candidate = f"{base}-{n}"


def list_docs() -> list[PrismDoc]:
    db = get_db()
    rows = db.fetchall(
        "SELECT id, title, brand_id, variant_of, '' AS doc, facet_count, "
        "created_at, updated_at FROM prism_docs ORDER BY updated_at DESC"
    )
    return [_row_to_doc(r) for r in rows]


def get_doc(doc_id: str) -> PrismDoc | None:
    db = get_db()
    row = db.fetchone("SELECT * FROM prism_docs WHERE id = ?", (doc_id,))
    return _row_to_doc(row) if row else None


def _emit(conn, doc_id: str, kind: str, origin: str) -> None:
    conn.execute(
        "INSERT INTO prism_events (doc_id, kind, origin) VALUES (?, ?, ?)",
        (doc_id, kind, origin or ""),
    )
    conn.execute(
        "DELETE FROM prism_events WHERE seq <= "
        "(SELECT MAX(seq) FROM prism_events) - ?",
        (_EVENTS_KEEP,),
    )


_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$")
_ITEM_SEPS = (" — ", " – ", " - ", ": ", " = ")


def _split_item(item: str) -> tuple[str, str | None]:
    """Split a ``TERM<sep>explanation`` list item into (title, body). No sep →
    (item, None). Used to turn a labelled bullet into a titled card."""
    for sep in _ITEM_SEPS:
        if sep in item:
            title, body = item.split(sep, 1)
            return title.strip(), (body.strip() or None)
    return item.strip(), None


def _cards_from_run(run: list[str]) -> dict[str, Any] | None:
    """A contiguous markdown-list run → a ``cards`` block, or None if it would
    not render cleanly (so it stays prose in the body). Clean = 2..7 items, each
    either a short label (≤40) or a ``TERM<sep>rest`` whose TERM is short (≤52)."""
    if not (2 <= len(run) <= 7):
        return None
    cards: list[dict[str, Any]] = []
    for item in run:
        title, body = _split_item(item)
        if body is None:
            if not title or len(title) > 40:
                return None
            cards.append({"title": title})
        else:
            if not title or len(title) > 52:
                return None
            cards.append({"title": title, "body": body})
    return {"type": "cards", "cards": cards}


def _promote_inline_lists(doc: dict[str, Any]) -> None:
    """Promote inlined markdown list SETS in rung bodies into ``cards`` blocks, so
    a set renders as a visual MODULE instead of prose bullets — the entry (brief)
    rung is the worst offender. Mutates ``doc`` in place at the single save
    chokepoint, so EVERY generation path (generate / build_deck / edit / retailor)
    benefits (DP10). Idempotent: a promoted run leaves no list in the body, so
    re-running is a no-op. Runs that would not render cleanly are left untouched."""
    facets = doc.get("facets")
    if not isinstance(facets, dict):
        return
    for facet in facets.values():
        rungs = facet.get("rungs") if isinstance(facet, dict) else None
        if not isinstance(rungs, dict):
            continue
        for rung in rungs.values():
            if not isinstance(rung, dict):
                continue
            body = rung.get("body")
            if not isinstance(body, str) or "\n" not in body:
                continue
            lines = body.split("\n")
            kept: list[str] = []
            new_cards: list[dict[str, Any]] = []
            i = 0
            while i < len(lines):
                if _LIST_ITEM_RE.match(lines[i]):
                    start, run = i, []
                    while i < len(lines):
                        m = _LIST_ITEM_RE.match(lines[i])
                        if not m:
                            break
                        run.append(m.group(1).strip())
                        i += 1
                    block = _cards_from_run(run)
                    if block is not None:
                        new_cards.append(block)
                    else:
                        kept.extend(lines[start:i])
                    continue
                kept.append(lines[i])
                i += 1
            if new_cards:
                rung["body"] = "\n".join(kept).strip()
                existing = rung.get("blocks")
                rung["blocks"] = (existing if isinstance(existing, list) else []) + new_cards


def save_doc(
    *,
    id: str | None = None,
    title: str,
    brand_id: str | None = None,
    variant_of: str | None = None,
    doc: dict[str, Any] | None = None,
    origin: str = "",
    allow_empty: bool = False,
) -> PrismDoc:
    """Upsert a doc. Generates a unique slug id from ``title`` when ``id`` is
    absent. The facet tree is normalised so ``doc.id``/``doc.title`` track the
    row. Emits a ``saved`` change-feed event. An empty doc (no facets) cannot
    overwrite a populated one unless ``allow_empty``."""
    title = (title or "").strip()
    if not title:
        raise ValueError("title required")

    doc = doc if isinstance(doc, dict) else {}
    facets = doc.get("facets") if isinstance(doc.get("facets"), dict) else {}
    doc["facets"] = facets
    facet_count = len(facets)

    # Canonicalise rung vocabulary at the write chokepoint too — an op/generate
    # path that still emits a legacy key is normalised before layout + persistence,
    # so the stored form is always L1–L4 (no half-migrated docs).
    try:
        from okuro.prism.rungs import normalize_doc_rungs

        normalize_doc_rungs(doc)
    except Exception as exc:  # noqa: BLE001 — never block a save over normalisation
        logger.info("rung normalisation skipped: %s", exc)

    # Promote inlined markdown list SETS to `cards` blocks BEFORE layout derives —
    # a set is a visual module, not prose bullets (the brief/entry rung especially).
    # Runs before derive_layouts so the new blocks get a grid. Never blocks a save.
    try:
        _promote_inline_lists(doc)
    except Exception as exc:  # noqa: BLE001
        logger.info("inline-list promotion skipped: %s", exc)

    # Slide model — stamp each level's SLIDE TYPE (cover/section/content/doc-view)
    # at the write chokepoint so every path (generate / build / edit / retailor)
    # renders the same slide model, and the layout engine below can pick a slide-
    # aware archetype. Tag-only + idempotent; never fabricates content.
    try:
        from okuro.prism.slides import stamp_slide_types

        stamp_slide_types(doc)
    except Exception as exc:  # noqa: BLE001 — never block a save over slide tagging
        logger.info("slide-type stamping skipped: %s", exc)

    # Template binding — select a DESIGNED slide template + fill its slots per rung
    # (the intentional-layout replacement for the greedy packer). Runs after slide
    # types so it can key off them; extractive + fail-soft; a rung with no fitting
    # template renders as the legacy block stack.
    try:
        from okuro.prism.templates import stamp_templates

        stamp_templates(doc)
    except Exception as exc:  # noqa: BLE001 — never block a save over template binding
        logger.info("template stamping skipped: %s", exc)

    # Layout engine (composition layer) — derive each rung's grid arrangement
    # from its block shape at the single write chokepoint, so every path
    # (generate / edit / retailor / apply_ops) renders consistent, index-fresh
    # layouts. Best-effort: never block a save over layout.
    try:
        from okuro.prism.layout import derive_layouts

        derive_layouts(doc)
    except Exception as exc:  # noqa: BLE001
        logger.info("layout derivation skipped: %s", exc)

    db = get_db()
    with db.write() as conn:
        existing_id = (id or "").strip() or None
        existing = None
        if existing_id:
            existing = conn.execute(
                "SELECT facet_count FROM prism_docs WHERE id = ?", (existing_id,)
            ).fetchone()

        if existing_id and existing:
            doc_id = existing_id
            prior = int(existing["facet_count"] or 0)
            if facet_count == 0 and prior > 0 and not allow_empty:
                raise ValueError("refusing to empty a populated doc without allow_empty")
        else:
            doc_id = _unique_id(db, slugify(existing_id or title))

        doc["id"] = doc_id
        doc["title"] = title
        if brand_id:
            doc["brand_id"] = brand_id
        doc_json = json.dumps(doc, separators=(",", ":"))

        if existing_id and existing:
            conn.execute(
                "UPDATE prism_docs SET title = ?, brand_id = ?, variant_of = COALESCE(?, variant_of), "
                "doc = ?, facet_count = ?, updated_at = datetime('now') WHERE id = ?",
                (title, brand_id, variant_of, doc_json, facet_count, doc_id),
            )
        else:
            conn.execute(
                "INSERT INTO prism_docs (id, title, brand_id, variant_of, doc, facet_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (doc_id, title, brand_id, variant_of, doc_json, facet_count),
            )
        _emit(conn, doc_id, "saved", origin)

    saved = get_doc(doc_id)
    assert saved is not None
    return saved


_UNSET = object()
_CORNERS = {"tl", "tr", "bl", "br"}


def update_doc_meta(
    doc_id: str,
    *,
    brand_id: Any = _UNSET,
    logo_corner: Any = _UNSET,
) -> PrismDoc:
    """Change a doc's brand and/or per-deck logo corner in place, WITHOUT
    touching its facet content. Emits a ``saved`` change-feed event so any open
    ``/prism`` viewer re-themes live (the viewer resolves theme reactively from
    ``brand_id``). ``logo_corner`` is the per-deck override (``tl``/``tr``/``bl``
    /``br``); passing ``None`` clears it so the deck falls back to the brand
    default. Args left unset are preserved."""
    existing = get_doc(doc_id)
    if existing is None:
        raise ValueError(f"doc '{doc_id}' not found")

    doc = existing.doc if isinstance(existing.doc, dict) else {}
    new_brand = existing.brand_id

    if brand_id is not _UNSET:
        new_brand = (brand_id or "").strip() or None
        if new_brand:
            doc["brand_id"] = new_brand
        else:
            doc.pop("brand_id", None)

    if logo_corner is not _UNSET:
        lc = (logo_corner or "").strip().lower() or None
        if lc is None:
            doc.pop("logo_corner", None)
        elif lc in _CORNERS:
            doc["logo_corner"] = lc
        else:
            raise ValueError("logo_corner must be one of tl/tr/bl/br")

    doc_json = json.dumps(doc, separators=(",", ":"))
    db = get_db()
    with db.write() as conn:
        conn.execute(
            "UPDATE prism_docs SET brand_id = ?, doc = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (new_brand, doc_json, doc_id),
        )
        _emit(conn, doc_id, "saved", "set_meta")

    saved = get_doc(doc_id)
    assert saved is not None
    return saved


def delete_doc(doc_id: str, *, origin: str = "") -> bool:
    db = get_db()
    with db.write() as conn:
        if conn.execute("SELECT id FROM prism_docs WHERE id = ?", (doc_id,)).fetchone() is None:
            return False
        conn.execute("DELETE FROM prism_docs WHERE id = ?", (doc_id,))
        _emit(conn, doc_id, "deleted", origin)
    return True


def events_since(seq: int) -> list[dict]:
    db = get_db()
    return db.fetchall(
        "SELECT seq, doc_id, kind, origin, ts FROM prism_events "
        "WHERE seq > ? ORDER BY seq ASC",
        (int(seq),),
    )


def latest_seq() -> int:
    db = get_db()
    row = db.fetchone("SELECT MAX(seq) AS s FROM prism_events")
    return int(row["s"]) if row and row.get("s") is not None else 0
