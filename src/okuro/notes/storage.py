# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro-notes storage. CRUD over notes + note_chunks + note_links +
#   notes_events. On every save a note is re-chunked and re-embedded into
#   vec_note_chunks so it is a first-class RAG citizen; wikilinks are parsed and
#   resolved into note_links (powering backlinks + the note graph). Embedding and
#   vec writes are best-effort: a note always persists even when the embed
#   service is down (vectors get rebuilt later by embed/repair.ensure_vec_dims).
# index: content
# AGENT_HEADER_END -->
"""Storage layer for okuro-notes.

Design notes
------------
* Markdown FORMAT, DB STORAGE — no files, no Syncthing. ``body`` is the markdown
  source of truth; ``note_chunks`` is the embed source of truth (rebuilt on save).
* Chunk-level embedding (not whole-note) so long notes stay searchable — mirrors
  cortex rather than the thoughts/memory whole-blob pattern.
* Embeds are computed OUTSIDE the write transaction (network round-trips) and the
  DB mutations happen inside one ``db.write()`` so the writer lock is never held
  across HTTP. Missing embed service / missing vec table never blocks a save.
"""

from __future__ import annotations

import html
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Optional

from okuro.db import get_db
from okuro.sense.retrieval import candidate_pool

logger = logging.getLogger(__name__)

# Sentinel: distinguishes "caller did not touch this field" from a real value
# on update. folder_id needs it because None means "move to vault root", so a
# title/body autosave must not re-parent a note. body needs it because "" means
# "empty the note", so a rename that mentions no body must not erase one.
_UNSET = object()

# --- chunking --------------------------------------------------------------
# Char-approximation of cortex's token chunker (~4 chars/token, 1024-tok window,
# 18% overlap). Avoids loading a tokenizer per save. Most idea-captures are a
# single chunk; long notes split with overlap so a sentence on a boundary
# survives in both neighbours.
_CHARS_PER_TOKEN = 4
_CHUNK_TOKENS = 1024
_OVERLAP_RATIO = 0.18
_CHUNK_CHARS = _CHUNK_TOKENS * _CHARS_PER_TOKEN
_OVERLAP_CHARS = int(_CHUNK_CHARS * _OVERLAP_RATIO)

# Wikilinks: [[Target]] / [[Target|alias]] / ![[embed]]. The leading '!' marks an
# embed; we still record it as a link. drawing: embeds are media, not links.
_WIKILINK_RE = re.compile(r"!?\[\[([^\]\n]+?)\]\]")

# Leading markdown noise stripped when deriving a title from the first body line:
# heading hashes, blockquote marks, list bullets / numbers, task checkboxes.
_TITLE_STRIP_RE = re.compile(r"^\s*(?:#{1,6}\s+|>\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)")
_TITLE_MAX = 120

# A title is DISPLAY text, so anything in the line that exists only to carry
# formatting or markup is noise in it. Mirrored character-for-character by
# sanitizeTitleLine() in web/frontend/src/pages/notes.tsx — the two must agree,
# or the label the user watches while typing is not the one that gets stored.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# An opener with no closer on the same line — a note that starts with an
# AGENT_HEADER block is the live case.
_HTML_COMMENT_OPEN_RE = re.compile(r"<!--.*$", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_WIKILINK_RE = re.compile(r"!?\[\[([^\]\n]+?)\]\]")
# Emphasis markers must hug their text (CommonMark), so `2 * 3 * 4` keeps its
# stars while `*emphasis*` loses them. `_` additionally refuses to fire
# intra-word, which is what keeps snake_case identifiers intact.
_MD_EMPHASIS_RES = (
    re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*"),
    re.compile(r"__(?!\s)(.+?)(?<!\s)__"),
    re.compile(r"\*(?!\s)(.+?)(?<!\s)\*"),
    re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)"),
    re.compile(r"`(.+?)`"),
)
# Unpaired markers left at either end — '*Written 2026-04-15' opens emphasis it
# never closes. '_' stays out: a leading underscore reads as an identifier.
_MD_DANGLING_RE = re.compile(r"^[*`]+|[*`]+$")
_WS_RE = re.compile(r"\s+")


def _sanitize_title_line(line: str) -> str:
    """Reduce one body line to the plain text a title should show.

    Returns "" when the line carried no text of its own — a lone ``<br />``, an
    unclosed ``<!--`` opener — which is the signal to try the next line rather
    than store markup as a name.
    """
    text = _TITLE_STRIP_RE.sub("", line)
    text = _HTML_COMMENT_RE.sub("", text)
    text = _HTML_COMMENT_OPEN_RE.sub("", text)
    text = _HTML_TAG_RE.sub("", text)
    # After tag removal on purpose: an ESCAPED tag (&lt;b&gt;) is text the author
    # meant to show, and decoding first would let the next pass eat it as markup.
    text = html.unescape(text)
    text = _MD_WIKILINK_RE.sub(lambda m: m.group(1).split("|")[-1].strip(), text)
    text = _MD_IMAGE_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    for rx in _MD_EMPHASIS_RES:
        prev = None
        while prev != text:
            prev = text
            text = rx.sub(r"\1", text)
    text = _WS_RE.sub(" ", text).strip()
    return _MD_DANGLING_RE.sub("", text).strip()


def _derive_title(body: str) -> str:
    """First body line that carries text, sanitised, as a title.

    Fallback for notes saved without an explicit title so the sidebar shows
    something meaningful instead of 'Untitled' (Obsidian/Notion first-line rule).
    """
    for line in body.splitlines():
        stripped = _sanitize_title_line(line)
        if stripped:
            return stripped[:_TITLE_MAX]
    return "Untitled"


def _title_looks_explicit(title: str) -> bool:
    """Whether a passed title reads as a name someone chose."""
    t = (title or "").strip()
    return bool(t) and t != "Untitled"


def _resolve_title(db, note_id, title, body, title_explicit) -> tuple[str, bool]:
    """Pick the stored title and whether it is user-chosen.

    Three caller intents that a bare title string cannot tell apart:
      * a name to set              -> store it, flag 1
      * nothing said about a name  -> keep an explicit one, else re-derive
      * ``title_explicit=False``   -> deliberately back to derived, flag 0
    Without the middle case an autosave (which sends body only, so title="")
    would re-derive over every rename ~800ms after it was typed.
    """
    explicit = title_explicit
    if explicit is None:
        explicit = _title_looks_explicit(title)
    if explicit:
        return title.strip(), True
    if note_id is not None and title_explicit is None:
        row = db.fetchone(
            "SELECT title, title_explicit FROM notes WHERE id = ?", (note_id,)
        )
        if row and row["title_explicit"]:
            return row["title"], True
    return _derive_title(body), False


def _chunk(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= _CHUNK_CHARS:
        return [text]
    chunks: list[str] = []
    step = _CHUNK_CHARS - _OVERLAP_CHARS
    start = 0
    n = len(text)
    while start < n:
        end = min(start + _CHUNK_CHARS, n)
        # Try to end on a paragraph/sentence boundary inside the last 20% so we
        # don't slice mid-word; fall back to the hard window edge.
        if end < n:
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if cut > int(_CHUNK_CHARS * 0.8):
                end = start + cut + 1
        chunks.append(text[start:end].strip())
        if end >= n:
            break
        start = end - _OVERLAP_CHARS
    return [c for c in chunks if c]


def _extract_links(body: str) -> list[str]:
    """Unique wikilink targets in document order. drawing: embeds excluded."""
    seen: dict[str, None] = {}
    for m in _WIKILINK_RE.finditer(body or ""):
        raw = m.group(1).strip()
        target = raw.split("|", 1)[0].strip()        # drop |alias
        target = target.split("#", 1)[0].strip()     # drop #heading anchor
        if not target or target.lower().startswith("drawing:"):
            continue
        seen.setdefault(target, None)
    return list(seen.keys())


def _resolve_link(db, link_text: str) -> tuple[str, Optional[str]]:
    """Resolve a wikilink to (target_type, target_id).

    Phase-1 backbone resolves to other notes by exact title (case-insensitive).
    Richer entity resolution (person/project/thought/artifact/kg) is layered on
    in the linking phase; unresolved links are stored as ghosts (target_id NULL)
    and re-resolved on the next save once the target exists.
    """
    row = db.fetchone(
        "SELECT id FROM notes WHERE lower(title) = lower(?) "
        "AND archived = 0 LIMIT 1",
        (link_text,),
    )
    if row:
        return ("note", row["id"])
    return ("unresolved", None)


# --- embedding (best-effort) ----------------------------------------------

def _embed_chunks(chunks: list[str]) -> list[Optional[bytes]]:
    """Embed each chunk to float32 bytes. None per chunk on any failure so the
    save still proceeds; vectors are rebuilt later by ensure_vec_dims()."""
    if not chunks:
        return []
    try:
        from okuro.embed.client import embed_one, to_bytes
    except Exception:
        return [None] * len(chunks)
    out: list[Optional[bytes]] = []
    for c in chunks:
        try:
            out.append(to_bytes(embed_one(c)))
        except Exception as exc:
            logger.warning("note chunk embed failed: %s", exc)
            out.append(None)
    return out


# Deferred embedding
# ------------------
# Embedding is a network round trip (30s HTTP + 20s fallback timeout). Doing it
# inline made every note save wait on the embed service: a degraded okuro-embed
# stretched a single create to ~50s. That window is what let the notes autosave
# race spawn duplicate rows before its in-flight guard existed.
#
# So the write path no longer embeds. Chunks are inserted with their text, the
# save returns, and vectors are written afterwards on a small background pool.
# A chunk with no row in vec_note_chunks is simply "not yet searchable
# semantically" — a state this module already tolerated (_embed_chunks has
# always been allowed to return None per chunk). backfill_missing_vectors()
# repairs anything the pool dropped, so a crash or a down embed service costs
# latency in search freshness, never data.
_EMBED_POOL: Optional["ThreadPoolExecutor"] = None


def _embed_pool():
    global _EMBED_POOL
    if _EMBED_POOL is None:
        from concurrent.futures import ThreadPoolExecutor

        # Deliberately small: note saves are bursty (autosave), and the embed
        # service is a shared resource. Queueing beats stampeding it.
        _EMBED_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="note-embed")
    return _EMBED_POOL


def _embed_and_store(chunk_ids: list[str], chunks: list[str]) -> None:
    """Embed chunks and write their vectors. Runs off the request path."""
    try:
        vecs = _embed_chunks(chunks)
        db = get_db()
        with db.write():
            for cid, vec in zip(chunk_ids, vecs):
                # The chunk may have been deleted by a newer save that landed
                # while we were embedding. The INSERT is then orphaned, so drop
                # it — a stale vector is worse than a missing one.
                still_there = db.fetchone(
                    "SELECT 1 AS ok FROM note_chunks WHERE id = ?", (cid,)
                )
                if still_there:
                    _write_vec(db, cid, vec)
    except Exception as exc:  # noqa: BLE001 - a background task must never raise
        logger.warning("deferred note embed failed: %s", exc)


def backfill_missing_vectors(limit: int = 500) -> int:
    """Embed note chunks that have no vector yet. Returns how many were fixed.

    Safety net for the deferred path: anything the pool dropped (process exit,
    embed service down) is picked up here. Safe to run repeatedly.
    """
    db = get_db()
    try:
        rows = db.fetchall(
            "SELECT c.id AS id, c.text AS text FROM note_chunks c "
            "LEFT JOIN vec_note_chunks v ON v.id = c.id "
            "WHERE v.id IS NULL LIMIT ?",
            (limit,),
        )
    except Exception as exc:  # vec table may not exist on a fresh DB
        logger.warning("vector backfill query failed: %s", exc)
        return 0
    if not rows:
        return 0
    _embed_and_store([r["id"] for r in rows], [r["text"] for r in rows])
    return len(rows)


def _write_vec(db, chunk_id: str, vec_bytes: Optional[bytes]) -> None:
    if vec_bytes is None:
        return
    try:
        db.execute(
            "INSERT INTO vec_note_chunks (id, embedding) VALUES (?, ?)",
            (chunk_id, vec_bytes),
        )
    except Exception:
        # vec table may not exist yet on a fresh DB (created by ensure_vec_dims).
        pass


def _drop_vec(db, chunk_ids: list[str]) -> None:
    if not chunk_ids:
        return
    try:
        qs = ",".join("?" * len(chunk_ids))
        db.execute(f"DELETE FROM vec_note_chunks WHERE id IN ({qs})", tuple(chunk_ids))
    except Exception:
        pass


# --- public API ------------------------------------------------------------

def _clean_authored_at(value: Optional[str]) -> Optional[str]:
    """Accept a date-shaped authorship stamp, reject anything else.

    The column exists so ranking can trust it, so a caller that passes prose,
    a relative date, or an impossible one must not poison it — the value is
    dropped and the row keeps NULL, which is the honest "unknown". Callers
    that need to know whether a value stuck can read it back from the note.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            datetime.strptime(text, fmt)
            return text
        except ValueError:
            continue
    logger.debug("notes: ignoring unparseable authored_at %r", text[:40])
    return None


def upsert_note(
    note_id: Optional[str] = None,
    title: str = "Untitled",
    body=_UNSET,
    frontmatter: Optional[dict] = None,
    project: Optional[str] = None,
    folder_id=_UNSET,
    origin: str = "",
    sync_embed: bool = False,
    authored_at: Optional[str] = None,
    title_explicit: Optional[bool] = None,
) -> dict:
    """Create (note_id=None) or update a note. Re-chunks, rewrites links,
    appends a notes_events row. Returns the stored note dict.

    A blank / 'Untitled' title falls back to the first body line so every note
    has a meaningful sidebar label without forcing the user to name it.

    ``title_explicit`` records WHO chose the title, which is what makes rename
    survive the next autosave (migration 141). Leave it None and it is inferred
    from ``title``: a real name marks the note explicit, a blank one means "not
    touching the title" and preserves an already-explicit one. Pass False to
    deliberately hand the title back to the first-line rule — that is what an
    emptied rename box does.

    ``body`` omitted keeps the stored markdown (same sentinel reasoning as
    ``folder_id``); pass "" only when you mean to empty the note.

    Embedding happens AFTER the write, off the request path — the save no
    longer waits on the embed service. Pass sync_embed=True when the caller
    needs vectors to exist by the time this returns.

    ``authored_at`` is when the HUMAN wrote it, which is not what ``created_at``
    means — that is when okuro first saw it. Pass it when migrating or
    importing content that predates its arrival here; leave it None when the
    note is being written now (the two coincide) or when authorship is unknown.
    NULL is the honest value for unknown: see migration 117. Omitting it on an
    update never clears a stored value.
    """
    db = get_db()
    if body is _UNSET:
        # A rename passes a title and nothing else; writing the "" default over
        # the stored markdown would delete the note's content to change its name.
        row = (
            db.fetchone("SELECT body FROM notes WHERE id = ?", (note_id,))
            if note_id is not None
            else None
        )
        body = row["body"] if row else ""
    title, explicit = _resolve_title(db, note_id, title, body, title_explicit)
    fm_json = json.dumps(frontmatter or {})
    chunks = _chunk(body)
    new_chunk_ids = [str(uuid.uuid4()) for _ in chunks]
    link_targets = _extract_links(body)

    is_new = note_id is None
    if is_new:
        note_id = str(uuid.uuid4())

    with db.write():
        if is_new:
            db.execute(
                "INSERT INTO notes (id, title, body, frontmatter, project, folder_id, "
                "authored_at, title_explicit) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (note_id, title, body, fm_json, project,
                 None if folder_id is _UNSET else folder_id,
                 _clean_authored_at(authored_at), 1 if explicit else 0),
            )
        else:
            # Only written when supplied — an update that says nothing about
            # authorship must not erase a recorded one.
            cleaned = _clean_authored_at(authored_at)
            if cleaned:
                db.execute(
                    "UPDATE notes SET authored_at = ? WHERE id = ?",
                    (cleaned, note_id),
                )
            flag = 1 if explicit else 0
            if folder_id is _UNSET:
                db.execute(
                    "UPDATE notes SET title = ?, title_explicit = ?, body = ?, "
                    "frontmatter = ?, project = ?, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (title, flag, body, fm_json, project, note_id),
                )
            else:
                db.execute(
                    "UPDATE notes SET title = ?, title_explicit = ?, body = ?, "
                    "frontmatter = ?, project = ?, folder_id = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (title, flag, body, fm_json, project, folder_id, note_id),
                )
            # Purge stale chunks/links on EVERY update, not just folder changes.
            # Chunks are rebuilt below; skipping this leaks a fresh chunk-set per
            # save and bloats the vec index (buries real content under old snapshots).
            old = db.fetchall(
                "SELECT id FROM note_chunks WHERE note_id = ?", (note_id,)
            )
            _drop_vec(db, [r["id"] for r in old])
            db.execute("DELETE FROM note_chunks WHERE note_id = ?", (note_id,))
            db.execute("DELETE FROM note_links WHERE src_note_id = ?", (note_id,))

        for idx, (cid, ctext) in enumerate(zip(new_chunk_ids, chunks)):
            db.execute(
                "INSERT INTO note_chunks (id, note_id, chunk_idx, text) "
                "VALUES (?, ?, ?, ?)",
                (cid, note_id, idx, ctext),
            )
            # Vectors are written after this transaction — see below.

        for lt in link_targets:
            ttype, tid = _resolve_link(db, lt)
            db.execute(
                "INSERT INTO note_links (src_note_id, target_type, target_id, "
                "link_text) VALUES (?, ?, ?, ?)",
                (note_id, ttype, tid, lt),
            )

        # Re-resolve any ghost links elsewhere that now point at this note.
        if title.strip():
            db.execute(
                "UPDATE note_links SET target_type = 'note', target_id = ? "
                "WHERE target_type = 'unresolved' AND lower(link_text) = lower(?)",
                (note_id, title.strip()),
            )

        db.execute(
            "INSERT INTO notes_events (note_id, kind, origin) "
            "VALUES (?, 'saved', ?)",
            (note_id, origin),
        )

    # Embed AFTER the commit, off the request path. sync_embed=True makes this
    # blocking for callers that need the vectors to exist on return (tests,
    # bulk imports that verify searchability).
    if chunks:
        if sync_embed:
            _embed_and_store(new_chunk_ids, chunks)
        else:
            try:
                _embed_pool().submit(_embed_and_store, new_chunk_ids, chunks)
            except Exception as exc:  # pool exhausted / interpreter shutting down
                logger.warning("could not queue note embed, doing it inline: %s", exc)
                _embed_and_store(new_chunk_ids, chunks)

    return get_note(note_id)


def get_note(note_id: str) -> Optional[dict]:
    db = get_db()
    row = db.fetchone("SELECT * FROM notes WHERE id = ?", (note_id,))
    if not row:
        return None
    note = dict(row)
    try:
        note["frontmatter"] = json.loads(note.get("frontmatter") or "{}")
    except (TypeError, ValueError):
        note["frontmatter"] = {}
    return note


def list_notes(
    project: Optional[str] = None,
    archived: bool = False,
    limit: Optional[int] = None,
) -> list[dict]:
    """Note metadata for the sidebar. No bodies — rows are small.

    limit=None means no limit, and that is the default on purpose. The sidebar
    builds a FOLDER TREE from this, so a truncated result is not "fewer notes
    shown", it is a wrong tree. The old default silently capped the list, which
    meant a bulk import could push the user's own notes off the end and look
    exactly like data loss.
    """
    db = get_db()
    sql = "SELECT id, title, project, folder_id, pinned, archived, created_at, " \
          "updated_at, authored_at FROM notes WHERE archived = ?"
    params: list = [1 if archived else 0]
    if project:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY pinned DESC, updated_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in db.fetchall(sql, tuple(params))]


def delete_note(note_id: str) -> bool:
    db = get_db()
    with db.write():
        old = db.fetchall("SELECT id FROM note_chunks WHERE note_id = ?", (note_id,))
        _drop_vec(db, [r["id"] for r in old])
        db.execute("DELETE FROM note_chunks WHERE note_id = ?", (note_id,))
        db.execute("DELETE FROM note_links WHERE src_note_id = ?", (note_id,))
        # Demote inbound links to ghosts rather than deleting them — the target
        # may be recreated; Obsidian keeps the dangling link visible.
        db.execute(
            "UPDATE note_links SET target_type = 'unresolved', target_id = NULL "
            "WHERE target_type = 'note' AND target_id = ?",
            (note_id,),
        )
        cur = db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        db.execute(
            "INSERT INTO notes_events (note_id, kind) VALUES (?, 'deleted')",
            (note_id,),
        )
    return getattr(cur, "rowcount", 0) > 0


def set_archived(note_id: str, archived: bool = True) -> bool:
    db = get_db()
    cur = db.execute(
        "UPDATE notes SET archived = ?, updated_at = datetime('now') WHERE id = ?",
        (1 if archived else 0, note_id),
    )
    return getattr(cur, "rowcount", 0) > 0


def search_notes(
    query: str,
    limit: int = 10,
    project: Optional[str] = None,
) -> list[dict]:
    """Semantic search over note chunks. Embeds the query (asymmetric for Qwen3),
    KNN over vec_note_chunks, then collapses chunk hits to best-distance-per-note.
    Falls back to a LIKE scan when the embed service is unavailable."""
    db = get_db()
    query = (query or "").strip()
    if not query:
        return []

    try:
        from okuro.embed.client import embed_query, to_bytes
        qvec = to_bytes(embed_query(query))
    except Exception:
        return _search_notes_like(db, query, limit, project)

    # Over-fetch chunks; multiple chunks of one note may all rank.
    rows = db.vec_search(
        "vec_note_chunks", qvec, limit=candidate_pool(limit, scoped=True)
    )
    if not rows:
        return _search_notes_like(db, query, limit, project)

    chunk_ids = [r["id"] for r in rows]
    dist = {r["id"]: r["distance"] for r in rows}
    qs = ",".join("?" * len(chunk_ids))
    chunk_rows = db.fetchall(
        f"SELECT id, note_id FROM note_chunks WHERE id IN ({qs})", tuple(chunk_ids)
    )
    best: dict[str, float] = {}
    for cr in chunk_rows:
        nid = cr["note_id"]
        d = dist.get(cr["id"], 9e9)
        if nid not in best or d < best[nid]:
            best[nid] = d
    if not best:
        return []

    note_ids = list(best.keys())
    qs2 = ",".join("?" * len(note_ids))
    sql = f"SELECT id, title, project, updated_at FROM notes " \
          f"WHERE id IN ({qs2}) AND archived = 0"
    params: list = list(note_ids)
    if project:
        sql += " AND project = ?"
        params.append(project)
    notes = {r["id"]: dict(r) for r in db.fetchall(sql, tuple(params))}

    out = []
    for nid in note_ids:
        if nid not in notes:
            continue
        n = notes[nid]
        n["similarity"] = round(1.0 - best[nid], 4)   # cosine: sim = 1 - distance
        out.append(n)
    out.sort(key=lambda x: x["similarity"], reverse=True)
    return out[:limit]


def _search_notes_like(db, query: str, limit: int, project: Optional[str]) -> list[dict]:
    sql = "SELECT id, title, project, updated_at FROM notes " \
          "WHERE archived = 0 AND (title LIKE ? OR body LIKE ?)"
    like = f"%{query}%"
    params: list = [like, like]
    if project:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in db.fetchall(sql, tuple(params))]


def backlinks(note_id: str) -> list[dict]:
    """Notes that link TO this note."""
    db = get_db()
    rows = db.fetchall(
        "SELECT DISTINCT l.src_note_id AS id, n.title, l.link_text "
        "FROM note_links l JOIN notes n ON n.id = l.src_note_id "
        "WHERE l.target_type = 'note' AND l.target_id = ? AND n.archived = 0 "
        "ORDER BY n.updated_at DESC",
        (note_id,),
    )
    return [dict(r) for r in rows]


def outgoing_links(note_id: str) -> list[dict]:
    """Links FROM this note (resolved + ghost)."""
    db = get_db()
    rows = db.fetchall(
        "SELECT target_type, target_id, link_text FROM note_links "
        "WHERE src_note_id = ? ORDER BY id",
        (note_id,),
    )
    return [dict(r) for r in rows]


# --- live-sync change-feed (mirrors flow_designer / slides) ----------------

def latest_seq() -> int:
    db = get_db()
    row = db.fetchone("SELECT COALESCE(MAX(seq), 0) AS s FROM notes_events")
    return int(row["s"]) if row else 0


def events_since(cursor: int) -> list[dict]:
    """notes_events rows with seq > cursor, oldest first."""
    db = get_db()
    rows = db.fetchall(
        "SELECT seq, note_id, kind, origin, ts FROM notes_events "
        "WHERE seq > ? ORDER BY seq ASC LIMIT 200",
        (cursor,),
    )
    return [dict(r) for r in rows]


# --- graph -----------------------------------------------------------------

def note_graph() -> dict:
    """Whole-vault link graph: note nodes + resolved note→note edges. Powers the
    Obsidian-style graph view. Ghost (unresolved) links are excluded — they have
    no target node to draw to."""
    db = get_db()
    nodes = [
        {"id": r["id"], "title": r["title"]}
        for r in db.fetchall(
            "SELECT id, title FROM notes WHERE archived = 0 ORDER BY updated_at DESC"
        )
    ]
    valid = {n["id"] for n in nodes}
    edges = []
    for r in db.fetchall(
        "SELECT DISTINCT src_note_id, target_id FROM note_links "
        "WHERE target_type = 'note' AND target_id IS NOT NULL"
    ):
        if r["src_note_id"] in valid and r["target_id"] in valid:
            edges.append({"source": r["src_note_id"], "target": r["target_id"]})
    return {"nodes": nodes, "edges": edges}


# --- drawings (Excalidraw scenes) ------------------------------------------

def _drawing_meta(row: dict) -> dict:
    """Drawing dict without the PNG blob, scene parsed to a dict."""
    d = {k: row[k] for k in ("id", "note_id", "title", "created_at", "updated_at") if k in row}
    try:
        d["scene"] = json.loads(row.get("scene") or "{}")
    except (TypeError, ValueError):
        d["scene"] = {}
    return d


def upsert_drawing(
    drawing_id: Optional[str] = None,
    note_id: Optional[str] = None,
    title: str = "Untitled drawing",
    scene: Optional[dict] = None,
    png: Optional[bytes] = None,
) -> dict:
    """Create or update an Excalidraw scene. ``png`` is a flattened raster kept
    for inline note embeds + future OCR/caption → RAG."""
    db = get_db()
    scene_json = json.dumps(scene or {})
    is_new = drawing_id is None
    if is_new:
        drawing_id = str(uuid.uuid4())
    with db.write():
        if is_new:
            db.execute(
                "INSERT INTO drawings (id, note_id, title, scene, png_blob) "
                "VALUES (?, ?, ?, ?, ?)",
                (drawing_id, note_id, title, scene_json, png),
            )
        elif png is not None:
            db.execute(
                "UPDATE drawings SET title = ?, scene = ?, png_blob = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (title, scene_json, png, drawing_id),
            )
        else:
            db.execute(
                "UPDATE drawings SET title = ?, scene = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (title, scene_json, drawing_id),
            )
    return get_drawing(drawing_id)


def get_drawing(drawing_id: str) -> Optional[dict]:
    db = get_db()
    row = db.fetchone(
        "SELECT id, note_id, title, scene, created_at, updated_at "
        "FROM drawings WHERE id = ?",
        (drawing_id,),
    )
    return _drawing_meta(dict(row)) if row else None


def get_drawing_png(drawing_id: str) -> Optional[bytes]:
    db = get_db()
    row = db.fetchone("SELECT png_blob FROM drawings WHERE id = ?", (drawing_id,))
    return row["png_blob"] if row and row["png_blob"] else None


def list_drawings(note_id: str) -> list[dict]:
    db = get_db()
    rows = db.fetchall(
        "SELECT id, note_id, title, created_at, updated_at FROM drawings "
        "WHERE note_id = ? ORDER BY updated_at DESC",
        (note_id,),
    )
    return [dict(r) for r in rows]


# --- images (pasted / dropped raster attachments) --------------------------
# Blob-in-DB, mirroring drawings: bytes never touch disk so a note stays a
# self-contained DB unit. Referenced from the body as ![alt](/api/notes/images/<id>).

def add_image(
    data: bytes,
    mime: str = "image/png",
    note_id: Optional[str] = None,
    filename: Optional[str] = None,
) -> dict:
    """Store image bytes, return {id, mime, note_id, filename}. note_id may be
    None when the image is pasted before the note has been saved once."""
    db = get_db()
    image_id = str(uuid.uuid4())
    with db.write():
        db.execute(
            "INSERT INTO note_images (id, note_id, mime, filename, bytes) "
            "VALUES (?, ?, ?, ?, ?)",
            (image_id, note_id, mime, filename, data),
        )
    return {"id": image_id, "note_id": note_id, "mime": mime, "filename": filename}


def get_image(image_id: str) -> Optional[tuple[bytes, str]]:
    """Return (bytes, mime) for an image, or None if it does not exist."""
    db = get_db()
    row = db.fetchone(
        "SELECT bytes, mime FROM note_images WHERE id = ?", (image_id,)
    )
    if not row or row["bytes"] is None:
        return None
    return (row["bytes"], row["mime"] or "image/png")


# --- folders (adjacency-list note tree) ------------------------------------
# parent_id self-reference; NULL parent = root. The tree is walked in memory
# from list_folders() — cheap at vault scale. Deletes reparent children and
# orphan notes rather than cascade, honouring the "never lose a note" rule.

def create_folder(name: str, parent_id: Optional[str] = None) -> dict:
    """Create a folder under parent_id (None = root). Returns the folder dict."""
    db = get_db()
    folder_id = str(uuid.uuid4())
    name = (name or "Untitled folder").strip() or "Untitled folder"
    with db.write():
        db.execute(
            "INSERT INTO folders (id, name, parent_id) VALUES (?, ?, ?)",
            (folder_id, name, parent_id),
        )
    return {"id": folder_id, "name": name, "parent_id": parent_id, "sort": 0}


def list_folders() -> list[dict]:
    """All folders as flat rows; caller builds the tree from (id, parent_id)."""
    db = get_db()
    rows = db.fetchall(
        "SELECT id, name, parent_id, sort, created_at FROM folders "
        "ORDER BY sort, name"
    )
    return [dict(r) for r in rows]


def rename_folder(folder_id: str, name: str) -> bool:
    db = get_db()
    name = (name or "").strip()
    if not name:
        return False
    cur = db.execute(
        "UPDATE folders SET name = ? WHERE id = ?", (name, folder_id)
    )
    return getattr(cur, "rowcount", 0) > 0


def _is_descendant(db, folder_id: str, maybe_ancestor: str) -> bool:
    """True if folder_id is maybe_ancestor or sits below it — used to reject a
    move that would create a cycle."""
    cur: Optional[str] = folder_id
    seen: set[str] = set()
    while cur is not None and cur not in seen:
        if cur == maybe_ancestor:
            return True
        seen.add(cur)
        row = db.fetchone("SELECT parent_id FROM folders WHERE id = ?", (cur,))
        cur = row["parent_id"] if row else None
    return False


def move_folder(folder_id: str, parent_id: Optional[str]) -> bool:
    """Re-parent a folder. Rejects moving a folder into itself or a descendant
    (would orphan a subtree into a cycle)."""
    db = get_db()
    if parent_id is not None and _is_descendant(db, parent_id, folder_id):
        return False
    cur = db.execute(
        "UPDATE folders SET parent_id = ? WHERE id = ?", (parent_id, folder_id)
    )
    return getattr(cur, "rowcount", 0) > 0


def delete_folder(folder_id: str) -> bool:
    """Delete a folder. Its child folders reparent to its parent and its notes
    orphan back to root (folder_id = NULL) — notes are never lost."""
    db = get_db()
    row = db.fetchone("SELECT parent_id FROM folders WHERE id = ?", (folder_id,))
    if not row:
        return False
    parent_id = row["parent_id"]
    with db.write():
        db.execute(
            "UPDATE folders SET parent_id = ? WHERE parent_id = ?",
            (parent_id, folder_id),
        )
        db.execute(
            "UPDATE notes SET folder_id = ? WHERE folder_id = ?",
            (parent_id, folder_id),
        )
        cur = db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    return getattr(cur, "rowcount", 0) > 0


def set_note_folder(note_id: str, folder_id: Optional[str]) -> bool:
    """Move a note into a folder (None = root). Does not touch updated_at so a
    drag-to-folder doesn't reorder the note above unrelated recent edits."""
    db = get_db()
    cur = db.execute(
        "UPDATE notes SET folder_id = ? WHERE id = ?", (folder_id, note_id)
    )
    return getattr(cur, "rowcount", 0) > 0
