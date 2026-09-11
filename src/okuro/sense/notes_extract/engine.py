# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: extract_once — watermark, extract, route, ledger. Daemon entry point.
# index:
#   def _body_hash
#   def _pending_notes
#   def _known_keys
#   def _adopt_legacy_key
#   def _route_todo
#   def _route_signal
#   def _route_thought
#   ROUTES
#   def _record
#   def _mark_extracted
#   def extract_once
# AGENT_HEADER_END -->
"""Note extraction engine — the live intake for okuro-notes.

One entry point, :func:`extract_once`, called by
``okuro.daemon._handlers:notes_extract`` on the cron schedule. Per note:

    changed? ──no──> skip (watermark hit, zero cost)
       │yes
       ▼
    extract_items() ──> route by kind ──> ledger row ──> watermark

Routing is eager by decision (2026-07-15): prose commitments become todos
directly, not triage-first signals. The reasoning — a missed commitment costs
more than a dismissable wrong one, and every routed item carries a rationale
explaining itself, so a bad call is legible rather than mysterious.

Replaces the Obsidian intake path (``sense/bridge/obsidian.py``), which fed
notes into ``thoughts`` untyped and untagged, and the never-wired
``orchestrator/signals/channels/obsidian.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Callable

from okuro.sense.notes_extract.extractor import (
    MIN_BODY_LEN,
    BridgeUnavailable,
    ExtractedItem,
    extract_items,
    item_key,
    verified_span,
)

log = logging.getLogger(__name__)

# Notes are few and cheap to skip, but an LLM call per note is not — cap a
# single tick so a bulk paste or an import can never fan out into a hundred
# bridge calls (DP09 / DP01).
DEFAULT_MAX_NOTES_PER_RUN = 5

# Below this the model is guessing. Items under it are dropped rather than
# routed — an eager router earns its keep only if it stays quiet when unsure.
MIN_CONFIDENCE = 0.55


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def _pending_notes(db, limit: int) -> list[dict]:
    """Return non-archived notes whose body changed since last extraction.

    SQL narrows to eligible candidates (not archived, long enough to say
    something, never-extracted first); the hash comparison itself happens here
    because SQLite has no sha256. Newest-first, so a burst of edits triages the
    freshest notes when the per-run cap bites.
    """
    rows = db.fetchall(
        """SELECT n.id, n.title, n.body, n.project, s.content_hash AS prev_hash
             FROM notes n
             LEFT JOIN note_extract_state s ON s.note_id = n.id
            WHERE n.archived = 0
              AND LENGTH(TRIM(n.body)) >= ?
            ORDER BY n.updated_at DESC""",
        (MIN_BODY_LEN,),
    )

    pending: list[dict] = []
    for r in rows:
        if r["prev_hash"] == _body_hash(r.get("body") or ""):
            continue
        pending.append(r)
        if len(pending) >= limit:
            break
    return pending


def _known_keys(db, note_id: str) -> set[str]:
    rows = db.fetchall(
        "SELECT item_key FROM note_extractions WHERE note_id = ?", (note_id,)
    )
    return {r["item_key"] for r in rows}


def _known_spans(db, note_id: str) -> list[str]:
    """Every verified source span already extracted from this note."""
    rows = db.fetchall(
        "SELECT span_norm FROM note_extractions "
        " WHERE note_id = ? AND span_norm IS NOT NULL AND span_norm != ''",
        (note_id,),
    )
    return [r["span_norm"] for r in rows]


def _overlapping_span(span: str, known: list[str]) -> str | None:
    """Return the stored span this one re-quotes, or None.

    ``item_key`` anchors on the note's own words, which fixes re-extraction
    when the model quotes the SAME span twice. It cannot fix a re-quote with
    different boundaries: one sentence yielded "meridian.example.com fails to
    communicate what Meridian can do for users" on one run and "...what Meridian
    is capable of" on the next — two spans, two keys, two signals for one
    observation.

    Containment in either direction means both quotes point at the same run of
    the user's words, so the item is the same evidence seen twice. This is not
    fuzzy matching on resemblance — the doctrine in ``extractor._normalize``
    still holds. Both sides are verbatim note text, verified present in the
    body and past ``_MIN_SPAN_LEN``; only the quote boundaries moved.

    The known cost: a long sentence carrying TWO commitments, where the model
    quotes the whole sentence for one and a fragment for the other, collapses
    to one item. That risk already existed for an exact re-quote — this widens
    it. Every suppression is logged and counted in ``skipped_span_overlap`` so
    it stays visible rather than silent, which is the property the doctrine
    actually protects.
    """
    for stored in known:
        if span in stored or stored in span:
            return stored
    return None


def _adopt_legacy_key(db, note_id: str, legacy_key: str, new_key: str,
                      span: str = "") -> bool:
    """Re-file an already-routed item under its source-span key.

    ``item_key`` used to hash the model's wording; it now hashes the note's
    (see extractor.item_key). Every item routed under the old scheme is in the
    ledger under a key the new scheme will never produce again — so without
    this, the first run after the change re-routes the whole backlog and mints
    one final duplicate for every item ever extracted.

    Copying the ledger row under the new key makes the NEXT run recognise it,
    converging silently. Returns True if the legacy row existed and was
    adopted. The original row stays: it is the historical record of what was
    actually routed, and dropping it would strand the audit trail.
    """
    row = db.fetchone(
        "SELECT kind, routed_to, target_id, text, topic, entities, context, "
        "       rationale, confidence "
        "  FROM note_extractions WHERE note_id = ? AND item_key = ?",
        (note_id, legacy_key),
    )
    if row is None:
        return False
    db.execute(
        """INSERT OR IGNORE INTO note_extractions
           (id, note_id, item_key, kind, routed_to, target_id, text,
            topic, entities, context, rationale, confidence, span_norm)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()), note_id, new_key, row["kind"], row["routed_to"],
            row["target_id"], row["text"], row["topic"], row["entities"],
            row["context"], row["rationale"], row["confidence"], span or None,
        ),
    )
    return True


# ── routes ───────────────────────────────────────────────────────────────────
# Each route writes the item into its destination and returns (routed_to,
# target_id). The tag block is written into BOTH the destination row and the
# ledger: the destination needs it to render itself, the ledger needs it to
# answer cross-cutting questions ("everything my notes said about Meridian").


def _tags(item: ExtractedItem, note: dict) -> dict[str, Any]:
    """The tag block — identical shape wherever an item lands."""
    return {
        # Mirrors the proactive-scanner evidence contract (scanner/bucket/
        # source_kind/source_id) so the inbox reducer and any future consumer
        # read note items through the same keys as every other producer.
        "scanner": "notes_extract",
        "bucket": "note",
        "source_kind": "note",
        "source_id": note["id"],
        # Explicit, because source_id is a note id here — NOT a project slug.
        # The reducer derives an inbox row's project from this key; see
        # inbox/reducer.py::_signal_project.
        "project": note.get("project"),
        # What it is about.
        "topic": item.topic,
        "entities": item.entities,
        # Where it came from.
        "context": item.context,
        "note_id": note["id"],
        "note_title": note.get("title") or "Untitled",
        # Why it is on the list.
        "rationale": item.rationale,
        "item_kind": item.kind,
        "confidence": round(item.confidence, 2),
    }


def _route_todo(item: ExtractedItem, note: dict, tags: dict,
                key: str) -> tuple[str, str]:
    from okuro.sense import todos as todos_svc

    todo = todos_svc.todo_add(
        title=item.text[:120],
        detail=item.rationale or None,
        priority=item.priority,
        project=note.get("project"),
        # 'ingress' is the todos vocabulary for an external intake scan —
        # documented on todo_add as "e.g. obsidian vault scan". Notes are now
        # that intake.
        source="ingress",
        # Producer-prefixed, not a bare note id: the inbox reducer treats raw
        # ingress as firehose (kind='research', weight 0.3, surface cap 3) and
        # only promotes producers listed in _CURATED_SAVE_PREFIXES, matched by
        # source_event_id prefix. A commitment the user wrote in their own note
        # is the opposite of a firehose, so notes-extract is on that list.
        #
        # Per-ITEM, not per-note: _find_active_duplicate dedups on
        # source_event_id, so a bare note id made every commitment a note yields
        # share one id — N distinct commitments collapsed to 1 stored (silent
        # data loss, reproduced 2026-07-20). item_key (the ledger's canonical
        # per-item identity, normalise-then-hash) keeps distinct commitments
        # distinct while a re-extraction of the same text stays idempotent. The
        # note id stays the middle segment, so the reducer's `split(':')[1]`
        # still recovers it for source-note-date ranking (that date is a batch
        # import timestamp, not authorship — see inbox/reducer.py::_pull_todos).
        #
        # `key` is computed once in extract_once and passed in — it is the
        # ledger's identity for this item, and recomputing it here from
        # item.text alone would silently drop the source-span anchor.
        source_event_id=f"notes-extract:{note['id']}:{key}",
        context=tags,
    )
    return "todos", todo["id"]


def _route_signal(item: ExtractedItem, note: dict, tags: dict,
                  key: str) -> tuple[str, str]:
    from okuro.sense import signals as signals_svc

    sig = signals_svc.signal_add(
        source="notes",
        source_ref=f"note:{note['id']}#{key}",
        # Questions are open-loop, never alarming on their own.
        severity="info" if item.kind == "question" else item.severity,
        summary=item.text[:200],
        evidence=tags,
        # No suggested_action: for a note signal it would only restate the
        # summary, which renders as a duplicate "Suggested action:" line under
        # the body (inbox.inbox_detail). signal_promote already falls back to
        # summary when it is absent, so the promote path is unaffected. A
        # signal is an observation — if the note had named the action, the
        # extractor would have classified it as a todo.
    )
    return "signals", sig["id"]


def _route_thought(item: ExtractedItem, note: dict, tags: dict,
                   key: str) -> tuple[str, str]:
    from okuro.sense.thoughts import capture_thought

    thought_id = capture_thought(
        content=item.text,
        source="notes",
        category="idea",
        project=note.get("project"),
    )
    return "thoughts", thought_id


ROUTES: dict[str, Callable[[ExtractedItem, dict, dict, str], tuple[str, str]]] = {
    "todo": _route_todo,
    "signal": _route_signal,
    "question": _route_signal,
    "idea": _route_thought,
}


def _record(db, note_id: str, key: str, item: ExtractedItem,
            routed_to: str, target_id: str, span: str = "") -> None:
    """Append the ledger row. UNIQUE(item_key) is the idempotence backstop.

    ``span`` is the verified normalised source span (empty when the item is
    keyed on the model's wording) — the anchor a later run compares against.
    """
    db.execute(
        """INSERT OR IGNORE INTO note_extractions
           (id, note_id, item_key, kind, routed_to, target_id, text,
            topic, entities, context, rationale, confidence, span_norm)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()), note_id, key, item.kind, routed_to, target_id,
            item.text, item.topic, json.dumps(item.entities), item.context,
            item.rationale, item.confidence, span or None,
        ),
    )


def _mark_extracted(db, note_id: str, content_hash: str, item_count: int) -> None:
    db.execute(
        """INSERT INTO note_extract_state (note_id, content_hash, item_count, extracted_at)
           VALUES (?, ?, ?, datetime('now'))
           ON CONFLICT(note_id) DO UPDATE SET
               content_hash = excluded.content_hash,
               item_count   = excluded.item_count,
               extracted_at = excluded.extracted_at""",
        (note_id, content_hash, item_count),
    )


def extract_once(max_notes: int = DEFAULT_MAX_NOTES_PER_RUN) -> dict:
    """Extract from every changed note, route the items, return a summary.

    Returns::

        {
          "notes_scanned": int,
          "notes_extracted": int,   # notes that reached the bridge
          "routed": {"todos": n, "signals": n, "thoughts": n},
          "skipped_known": int,     # items already in the ledger
          "skipped_span_overlap": int,  # re-quotes of an already-extracted span
          "skipped_low_conf": int,
          "errors": {note_id: "msg", ...},
        }

    A note's watermark advances only after its items are routed. A bridge
    failure leaves the watermark untouched so the next tick retries; every
    other exception is logged per-note and never escapes into the daemon.
    """
    from okuro.db import get_db

    db = get_db()
    notes = _pending_notes(db, max_notes)

    routed = {"todos": 0, "signals": 0, "thoughts": 0}
    errors: dict[str, str] = {}
    skipped_known = 0
    skipped_low_conf = 0
    skipped_span_overlap = 0
    extracted = 0

    for note in notes:
        note_id = note["id"]
        body = note.get("body") or ""

        try:
            items = extract_items(
                title=note.get("title") or "",
                body=body,
                project=note.get("project"),
            )
        except BridgeUnavailable as exc:
            # Watermark deliberately NOT advanced — retry next tick.
            log.warning("notes_extract: bridge down for note %s: %s", note_id, exc)
            errors[note_id] = str(exc)
            continue
        except Exception as exc:
            log.exception("notes_extract: extraction failed for %s", note_id)
            errors[note_id] = str(exc)
            continue

        extracted += 1
        known = _known_keys(db, note_id)
        known_spans = _known_spans(db, note_id)
        routed_count = 0

        for item in items:
            # Anchored to the note's own words where the model quoted them
            # faithfully; falls back to its wording where it did not.
            span = verified_span(item.source_span, body)
            key = item_key(note_id, item.text, item.source_span, body)
            if key in known:
                skipped_known += 1
                continue
            # One-time convergence: the same item under the pre-2026-07-25
            # wording-based key. Re-file it and move on — it is already routed.
            legacy_key = item_key(note_id, item.text)
            if legacy_key != key and legacy_key in known:
                if _adopt_legacy_key(db, note_id, legacy_key, key, span):
                    known.add(key)
                    if span:
                        known_spans.append(span)
                    skipped_known += 1
                    continue
            # Same words, different quote boundaries — one observation the
            # model re-quoted on a later run. See _overlapping_span.
            if span:
                overlaps = _overlapping_span(span, known_spans)
                if overlaps is not None:
                    skipped_span_overlap += 1
                    log.info(
                        "notes_extract: suppressed re-quote of an extracted span "
                        "(note=%s, item=%r, stored span=%r)",
                        note_id, item.text[:60], overlaps[:60],
                    )
                    continue
            if item.confidence < MIN_CONFIDENCE:
                skipped_low_conf += 1
                continue

            route = ROUTES.get(item.kind)
            if route is None:
                continue

            try:
                tags = _tags(item, note)
                routed_to, target_id = route(item, note, tags, key)
                _record(db, note_id, key, item, routed_to, target_id, span)
            except Exception as exc:
                # One bad item must not cost the rest of the note.
                log.exception("notes_extract: routing failed for %s", item.text[:60])
                errors[note_id] = str(exc)
                continue

            known.add(key)
            if span:
                known_spans.append(span)
            routed[routed_to] += 1
            routed_count += 1

        _mark_extracted(db, note_id, _body_hash(body), routed_count)

    log.info(
        "notes_extract: scanned=%d extracted=%d routed=%s skipped_known=%d "
        "skipped_span_overlap=%d skipped_low_conf=%d errors=%d",
        len(notes), extracted, routed, skipped_known, skipped_span_overlap,
        skipped_low_conf, len(errors),
    )

    return {
        "notes_scanned": len(notes),
        "notes_extracted": extracted,
        "routed": routed,
        "skipped_known": skipped_known,
        "skipped_span_overlap": skipped_span_overlap,
        "skipped_low_conf": skipped_low_conf,
        "errors": errors,
    }


__all__ = ["extract_once", "DEFAULT_MAX_NOTES_PER_RUN", "MIN_CONFIDENCE"]
