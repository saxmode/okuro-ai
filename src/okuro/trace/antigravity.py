### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Antigravity (agy) transcript ingester — reads per-conversation SQLite trajectory DBs into the raw-trace store.
# index: imports | PROVIDER | STEP_TYPES | _varint | _walk_fields | _extract_texts | _extract_timestamp | _conversation_dbs | ingest_file | ingest_antigravity
# AGENT_HEADER_END -->
"""Antigravity transcript ingester — closes okuro's biggest trace blind spot.

Before this module, ``agent_sessions`` held 8663 claude-code, 541 codex and
167 gemini rows and **zero antigravity**, despite antigravity being the live
Google provider since 2026-07-18. Every agy session was invisible to
``trace_index``, ``trace_search``, ``session_trace`` and — once it landed — the
whole ``okuro.sense.interaction`` pipeline, whose findings were therefore
Claude/Codex-only.

Storage shape
-------------
Antigravity does not write JSONL. Each conversation is its own SQLite database
under ``~/.gemini/antigravity-cli/conversations/<uuid>.db`` (121 files at time
of writing). The interesting table is ``steps``::

    steps(idx, step_type, status, metadata, step_payload, step_format, …)

``step_payload`` is a **protobuf** blob with no schema shipped alongside it.

Reading protobuf without the schema
-----------------------------------
Protobuf's wire format is self-describing enough to walk blind: every field
carries a (field_number, wire_type) key, and wire-type 2 is length-delimited.
:func:`_walk_fields` decodes that structure; :func:`_extract_texts` recurses
into nested messages and keeps any length-delimited value that decodes as
mostly-printable UTF-8.

This is a *lossy* read by construction. It recovers the conversational content
— which is what the trace store and the interaction detectors need — and does
not attempt to reconstruct antigravity's full object model. If Google ships
the schema, this module should be replaced rather than extended.

Step-type mapping is EMPIRICAL
------------------------------
The ``step_type`` integers below were derived by sampling 40 conversation DBs
and reading what each type actually carried. They are not documented anywhere
and could change with an agy release. :data:`STEP_TYPES` is therefore a
best-effort map, unknown types degrade to ``'progress'`` rather than being
dropped, and :func:`ingest_antigravity` reports how many events it could not
classify so the drift is visible rather than silent.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from okuro.db.sentinels import armed_executemany

log = logging.getLogger(__name__)

PROVIDER = "antigravity"
TRANSCRIPT_ROOT = Path.home() / ".gemini" / "antigravity-cli" / "conversations"

# Empirically derived over 40 conversation DBs (2026-07-26). Maps agy's
# step_type to the trace store's event `type` vocabulary.
#
#   14, 23  carry the prompt / bootstrap packet entering the model
#   15      model reasoning and prose output
#    5      generated code / document content authored by the model
#    8,9,21,38,51,101  tool traffic (file read, dir list, command, MCP call,
#                      resource list, task result)
#   17,90,98,132,138   harness-side: model-output errors, ephemeral reminders,
#                      permission grants, question prompts — the agy analogue
#                      of a system turn. 17 and 33 were found by the unknown-
#                      type report on the first live run, which is what that
#                      report exists for.
STEP_TYPES: dict[int, str] = {
    14: "user",
    23: "user",
    15: "assistant",
    5: "assistant",
    33: "assistant",
    8: "tool_result",
    9: "tool_result",
    21: "tool_result",
    38: "tool_result",
    51: "tool_result",
    101: "tool_result",
    17: "system",
    90: "system",
    98: "system",
    132: "system",
    138: "system",
}

# Text shorter than this is an id, an enum label or a fragment — not content.
_MIN_TEXT = 24
# Guard against a pathological blob recursing forever.
_MAX_DEPTH = 8
_PRINTABLE_RATIO = 0.95


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Decode a protobuf base-128 varint. Returns (value, new_pos)."""
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise IndexError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _walk_fields(buf: bytes):
    """Yield ``(field_no, wire_type, value)`` for one protobuf message.

    Stops silently at the first malformed byte: these blobs are read without a
    schema, so a partial read is the expected outcome for anything this module
    does not understand, not an error worth raising.
    """
    pos, end = 0, len(buf)
    while pos < end:
        try:
            key, pos = _varint(buf, pos)
        except (IndexError, ValueError):
            return
        field_no, wire = key >> 3, key & 7
        try:
            if wire == 0:
                value, pos = _varint(buf, pos)
                yield field_no, wire, value
            elif wire == 2:
                length, pos = _varint(buf, pos)
                if pos + length > end:
                    return
                yield field_no, wire, buf[pos : pos + length]
                pos += length
            elif wire == 5:
                yield field_no, wire, buf[pos : pos + 4]
                pos += 4
            elif wire == 1:
                yield field_no, wire, buf[pos : pos + 8]
                pos += 8
            else:
                return
        except (IndexError, ValueError):
            return


def _looks_like_text(raw: bytes) -> str | None:
    """Return the decoded string if it reads as human/machine text."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(text) < _MIN_TEXT:
        return None
    printable = sum(1 for c in text if c.isprintable() or c in "\n\t\r")
    if printable / len(text) < _PRINTABLE_RATIO:
        return None
    return text


def _extract_texts(buf: bytes, depth: int = 0) -> list[str]:
    """Recover every text-bearing field, recursing into nested messages."""
    found: list[str] = []
    for _, wire, value in _walk_fields(buf):
        if wire != 2 or not isinstance(value, bytes):
            continue
        text = _looks_like_text(value)
        if text is not None:
            found.append(text)
            continue
        if depth < _MAX_DEPTH:
            found.extend(_extract_texts(value, depth + 1))
    return found


def _extract_timestamp(buf: bytes) -> str | None:
    """Recover the first plausible protobuf Timestamp as ISO8601.

    A Timestamp is ``{1: seconds, 2: nanos}``. Scanning for that shape is
    ambiguous in principle; it is disambiguated by requiring the seconds value
    to land in a sane calendar range rather than by trusting the field number.
    """
    from datetime import datetime, timezone

    def scan(b: bytes, depth: int = 0) -> int | None:
        for field_no, wire, value in _walk_fields(b):
            if wire == 0 and field_no == 1 and 1_500_000_000 < value < 2_500_000_000:
                return value
            if wire == 2 and isinstance(value, bytes) and depth < _MAX_DEPTH:
                got = scan(value, depth + 1)
                if got:
                    return got
        return None

    seconds = scan(buf)
    if not seconds:
        return None
    return (
        datetime.fromtimestamp(seconds, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _conversation_dbs(root: Path | None = None) -> list[Path]:
    """Conversation DBs, excluding sqlite -wal/-shm sidecars."""
    base = root or TRANSCRIPT_ROOT
    if not base.exists():
        return []
    return sorted(p for p in base.glob("*.db") if p.is_file())


def ingest_file(db_path: Path, conn) -> dict:
    """Ingest one conversation DB into the trace store. Idempotent on mtime."""
    session_id = db_path.stem
    try:
        mtime = db_path.stat().st_mtime
    except OSError:
        return {"session_id": session_id, "skipped": "stat failed"}

    existing = conn.execute(
        "SELECT source_mtime FROM agent_session_sources WHERE source_path = ?",
        (str(db_path),),
    ).fetchone()
    if existing and existing[0] is not None and abs(existing[0] - mtime) < 1e-6:
        return {"session_id": session_id, "skipped": "unchanged"}

    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"session_id": session_id, "skipped": f"open failed: {exc}"}

    rows: list[tuple] = []
    unknown_types: set[int] = set()
    first_ts = last_ts = None
    counts = {"user": 0, "assistant": 0}

    try:
        steps = src.execute(
            "SELECT idx, step_type, step_payload FROM steps "
            "WHERE step_payload IS NOT NULL ORDER BY idx"
        ).fetchall()
    except sqlite3.Error as exc:
        src.close()
        return {"session_id": session_id, "skipped": f"no steps table: {exc}"}

    for idx, step_type, payload in steps:
        etype = STEP_TYPES.get(step_type)
        if etype is None:
            unknown_types.add(step_type)
            etype = "progress"

        texts = _extract_texts(payload or b"")
        # Longest wins: shorter siblings are ids and enum labels, and a step
        # carries at most one piece of real content.
        text = max(texts, key=len) if texts else ""
        ts = _extract_timestamp(payload or b"")

        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        if etype in counts:
            counts[etype] += 1

        rows.append((
            f"{session_id}:{idx}",      # uuid — deterministic, so re-ingest upserts
            session_id, None, idx, etype,
            etype if etype in ("user", "assistant", "system") else None,
            ts, None, text, None, None, None, None,
        ))
    src.close()

    if not rows:
        return {"session_id": session_id, "skipped": "no steps"}

    conn.execute(
        """
        INSERT INTO agent_sessions
            (session_id, provider, project_path, git_branch, first_ts, last_ts,
             message_count, user_count, assistant_count, tokens_in, tokens_out,
             model, source_path, source_mtime, indexed_at)
        VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, 0, 0, NULL, ?, ?, datetime('now'))
        ON CONFLICT(session_id) DO UPDATE SET
            first_ts        = excluded.first_ts,
            last_ts         = excluded.last_ts,
            message_count   = excluded.message_count,
            user_count      = excluded.user_count,
            assistant_count = excluded.assistant_count,
            source_mtime    = excluded.source_mtime,
            indexed_at      = datetime('now')
        """,
        (session_id, PROVIDER, first_ts, last_ts, len(rows),
         counts["user"], counts["assistant"], str(db_path), mtime),
    )
    armed_executemany(
        conn,
        """
        INSERT INTO agent_events
            (uuid, session_id, parent_uuid, ord, type, role, timestamp, model,
             text, content_json, tool_name, tokens_in, tokens_out)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uuid) DO UPDATE SET
            type = excluded.type, role = excluded.role,
            timestamp = excluded.timestamp, text = excluded.text
        """,
        rows,
    )
    conn.execute(
        """
        INSERT INTO agent_session_sources (source_path, session_id, source_mtime, indexed_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(source_path) DO UPDATE SET
            source_mtime = excluded.source_mtime, indexed_at = datetime('now')
        """,
        (str(db_path), session_id, mtime),
    )

    return {
        "session_id": session_id,
        "events": len(rows),
        "unknown_step_types": sorted(unknown_types),
    }


def ingest_antigravity(root: Path | None = None) -> dict:
    """Walk every conversation DB and ingest it. Safe to run repeatedly.

    ``unknown_step_types`` is surfaced deliberately: the step-type map is
    empirical, so an agy release that adds or renumbers types must show up as
    a reported number rather than as quietly mistyped events.
    """
    from okuro.db import get_db

    db = get_db()
    files = _conversation_dbs(root)
    if not files:
        return {"provider": PROVIDER, "files": 0, "sessions": 0, "events": 0}

    sessions = events = skipped = 0
    unknown: set[int] = set()

    with db.write():
        conn = db.conn
        for path in files:
            try:
                result = ingest_file(path, conn)
            except Exception as exc:            # one bad DB must not stop the walk
                log.warning("antigravity: %s failed (%s)", path.name, exc)
                skipped += 1
                continue
            if result.get("skipped"):
                skipped += 1
                continue
            sessions += 1
            events += result.get("events", 0)
            unknown.update(result.get("unknown_step_types") or [])

    return {
        "provider": PROVIDER,
        "files": len(files),
        "sessions": sessions,
        "events": events,
        "skipped": skipped,
        "unknown_step_types": sorted(unknown),
    }
