# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Gemini CLI transcript ingester — walks ~/.gemini/tmp/**/chats/session-*.json.
# index: imports | constants | _flatten_content | _event_to_row | ingest_file | ingest
# AGENT_HEADER_END -->
"""Ingest Gemini CLI session transcripts into ``agent_sessions`` / ``agent_events``.

Gemini CLI writes one JSON file per session under
``~/.gemini/tmp/<project-hash-or-slug>/chats/session-<iso>-<short>.json``.

Unlike claude-code/codex (which use JSONL), gemini writes a *single* JSON
document per session::

    {
      "sessionId": "<uuid>",
      "projectHash": "<sha256>",
      "startTime": "<iso>",
      "lastUpdated": "<iso>",
      "messages": [
         {"id": "<uuid>", "timestamp": "<iso>", "type": "user|gemini|...",
          "content": [...] | "..."},
         ...
      ]
    }

We surface each message as one ``agent_events`` row, mapping ``type`` ∈
{user → user, gemini → assistant, system → system}. Token counts and tool
calls aren't reliably present in the chat-history format — gemini CLI keeps
those in separate ``logs.json`` files which we don't currently parse (the
chat archive is the durable, user-facing record).

Idempotent: keyed by message ``id`` (uuid). Re-running on an unchanged file
is cheap (``source_mtime`` is compared and the file is skipped).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from okuro.db.sentinels import armed_executemany

logger = logging.getLogger(__name__)

PROVIDER = "gemini"
TRANSCRIPT_ROOT = Path("~/.gemini/tmp").expanduser()

_TEXT_TRUNCATE = 200_000


def _flatten_content(content: Any) -> str:
    """Flatten a gemini message ``content`` field to plain text.

    Observed shapes:
      * ``"raw string"`` (most common for assistant turns)
      * ``[{"text": "..."}]``
      * ``[{"type": "...", "text": "..."}]`` (rare)
    """
    if isinstance(content, str):
        return content[:_TEXT_TRUNCATE]
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for it in content:
        if isinstance(it, str):
            parts.append(it)
        elif isinstance(it, dict):
            t = it.get("text")
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(p for p in parts if p)[:_TEXT_TRUNCATE]


_TYPE_MAP = {
    "user": ("user", "user"),
    "gemini": ("assistant", "assistant"),
    "model": ("assistant", "assistant"),  # alternate spelling seen in some cli versions
    "assistant": ("assistant", "assistant"),
    "system": ("system", "system"),
    "tool": ("tool_result", "user"),
}


def _event_to_row(
    msg: dict,
    session_id: str,
    ord_: int,
    *,
    fallback_ts: str | None,
) -> dict | None:
    if not isinstance(msg, dict):
        return None
    raw_type = msg.get("type") or "user"
    mapped = _TYPE_MAP.get(raw_type)
    if mapped is None:
        # Unknown type — keep it as a system row so we don't lose data
        etype, role = "system", "system"
    else:
        etype, role = mapped

    uid = msg.get("id")
    if not uid:
        # Synthesize a stable uuid from session + ord so re-runs dedup
        uid = f"gemini:{session_id}:{ord_}"

    text = _flatten_content(msg.get("content"))
    ts = msg.get("timestamp") or fallback_ts

    try:
        content_blob = json.dumps(msg, ensure_ascii=False)[:_TEXT_TRUNCATE * 2]
    except (TypeError, ValueError):
        content_blob = json.dumps({"_error": "unserializable"}, ensure_ascii=False)

    return {
        "uuid": str(uid),
        "session_id": session_id,
        "parent_uuid": None,
        "ord": ord_,
        "type": etype,
        "role": role,
        "timestamp": ts,
        "model": None,
        "text": text,
        "content_json": content_blob,
        "tool_name": None,
        "tokens_in": None,
        "tokens_out": None,
    }


def ingest_file(path: Path, *, force: bool = False) -> dict:
    """Ingest a single gemini session JSON file. Skips if source_mtime unchanged."""
    from okuro.db import get_db

    db = get_db()
    mtime = path.stat().st_mtime

    if not force:
        existing = db.fetchone(
            "SELECT source_mtime FROM agent_session_sources WHERE source_path = ?",
            (str(path),),
        )
        if existing and existing.get("source_mtime") == mtime:
            return {"session_id": "", "events": 0, "skipped": True}

    try:
        doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        logger.warning("gemini: skip malformed session %s (%s)", path, e)
        # Treat as skipped — we saw the file but produced no rows. This keeps
        # the walker's "ingested" count an honest signal of new data landed.
        return {"session_id": "", "events": 0, "skipped": True}

    if not isinstance(doc, dict):
        logger.warning("gemini: unexpected top-level type in %s (%s)", path, type(doc).__name__)
        return {"session_id": "", "events": 0, "skipped": True}

    session_id = str(doc.get("sessionId") or path.stem)
    project_hash = doc.get("projectHash")
    project_path = doc.get("projectPath") or (str(project_hash) if project_hash else None)
    start_time = doc.get("startTime")
    messages = doc.get("messages")

    if not isinstance(messages, list) or not messages:
        return {"session_id": session_id, "events": 0, "skipped": False}

    rows: list[dict] = []
    for ord_, msg in enumerate(messages):
        try:
            row = _event_to_row(msg, session_id, ord_, fallback_ts=start_time)
        except Exception as e:  # pragma: no cover — never crash the ingester on a bad msg
            logger.warning("gemini: bad message in %s ord=%d (%s)", path, ord_, e)
            continue
        if row is None:
            continue
        rows.append(row)

    if not rows:
        return {"session_id": session_id, "events": 0, "skipped": False}

    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO agent_sessions (
                session_id, provider, project_path, git_branch,
                source_path, source_mtime, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(session_id) DO UPDATE SET
                provider       = excluded.provider,
                project_path   = COALESCE(agent_sessions.project_path, excluded.project_path),
                git_branch     = COALESCE(agent_sessions.git_branch, excluded.git_branch),
                source_path    = excluded.source_path,
                source_mtime   = excluded.source_mtime,
                indexed_at     = datetime('now')
            """,
            (
                session_id, PROVIDER, project_path, None,
                str(path), mtime,
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_session_sources (source_path, session_id, source_mtime, indexed_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(source_path) DO UPDATE SET
                session_id   = excluded.session_id,
                source_mtime = excluded.source_mtime,
                indexed_at   = datetime('now')
            """,
            (str(path), session_id, mtime),
        )
        armed_executemany(
            conn,
            """
            INSERT INTO agent_events (
                uuid, session_id, parent_uuid, ord, type, role, timestamp,
                model, text, content_json, tool_name, tokens_in, tokens_out
            ) VALUES (
                :uuid, :session_id, :parent_uuid, :ord, :type, :role, :timestamp,
                :model, :text, :content_json, :tool_name, :tokens_in, :tokens_out
            )
            ON CONFLICT(uuid) DO UPDATE SET
                parent_uuid=excluded.parent_uuid,
                ord=excluded.ord,
                type=excluded.type,
                role=excluded.role,
                timestamp=excluded.timestamp,
                model=excluded.model,
                text=excluded.text,
                content_json=excluded.content_json,
                tool_name=excluded.tool_name,
                tokens_in=excluded.tokens_in,
                tokens_out=excluded.tokens_out
            """,
            rows,
        )
        conn.execute(
            """
            UPDATE agent_sessions SET
                first_ts        = (SELECT MIN(timestamp)  FROM agent_events WHERE session_id = ?),
                last_ts         = (SELECT MAX(timestamp)  FROM agent_events WHERE session_id = ?),
                message_count   = (SELECT COUNT(*)        FROM agent_events WHERE session_id = ?),
                user_count      = (SELECT COUNT(*)        FROM agent_events WHERE session_id = ? AND type = 'user'),
                assistant_count = (SELECT COUNT(*)        FROM agent_events WHERE session_id = ? AND type = 'assistant'),
                tokens_in       = (SELECT COALESCE(SUM(tokens_in), 0)  FROM agent_events WHERE session_id = ?),
                tokens_out      = (SELECT COALESCE(SUM(tokens_out), 0) FROM agent_events WHERE session_id = ?),
                model           = COALESCE(
                                    (SELECT model FROM agent_events
                                      WHERE session_id = ? AND model IS NOT NULL
                                      ORDER BY timestamp DESC LIMIT 1),
                                    model
                                  )
            WHERE session_id = ?
            """,
            (session_id,) * 9,
        )

    return {"session_id": session_id, "events": len(rows), "skipped": False}


def ingest(*, root: Path | None = None, force: bool = False) -> dict:
    """Walk every gemini session under ``root`` (default ``~/.gemini/tmp``).

    Returns ``{"files": N, "ingested": M, "skipped": K, "events": E}``.
    """
    root = root or TRANSCRIPT_ROOT
    if not root.exists():
        logger.info("gemini: no session root at %s — nothing to ingest", root)
        return {"files": 0, "ingested": 0, "skipped": 0, "events": 0}

    files = 0
    ingested = 0
    skipped = 0
    events = 0
    # Sessions live under <root>/<project>/chats/session-*.json. Glob narrowly
    # so we don't drag in caches or unrelated json blobs.
    for path in root.glob("*/chats/session-*.json"):
        files += 1
        try:
            result = ingest_file(path, force=force)
        except Exception:
            logger.exception("gemini: ingest failed for %s", path)
            continue
        if result["skipped"]:
            skipped += 1
        else:
            ingested += 1
            events += result["events"]

    return {"files": files, "ingested": ingested, "skipped": skipped, "events": events}
