# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Claude Code transcript ingester — walks ~/.claude/projects/**/*.jsonl.
# index: imports | KEEP_TYPES | _flatten_content | _session_stats | _upsert_session | _upsert_events | ingest_file | ingest
# AGENT_HEADER_END -->
"""Ingest Claude Code transcripts into ``agent_sessions`` / ``agent_events``.

Claude Code writes one JSONL file per session under
``~/.claude/projects/<escaped-cwd>/<session-id>.jsonl``. Each line is a
typed event; we keep only the types that carry agent-proposer-relevant
signal and skip housekeeping noise (file snapshots, queue ops, etc).

Kept types → ``agent_events.type``:

* ``user``        user turn
* ``assistant``   model turn (may include thinking / text / tool_use items)
* ``system``      shell stdout, hook output, meta
* ``progress``    Claude Code native progress events (hook firings)
* ``tool_result`` synthesized from user-turn tool_result items

Idempotent: keyed by event ``uuid``. Re-running on an unchanged file is
cheap (mtime is compared and the file is skipped).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable
from okuro.db.sentinels import armed_executemany

logger = logging.getLogger(__name__)

PROVIDER = "claude-code"
TRANSCRIPT_ROOT = Path("~/.claude/projects").expanduser()

KEEP_TYPES = {"user", "assistant", "system", "progress"}
SKIP_TYPES = {"file-history-snapshot", "queue-operation", "last-prompt", "permission-mode"}

_TEXT_TRUNCATE = 200_000  # per-event cap to keep FTS index bounded


def _flatten_content(content: Any) -> tuple[str, str | None]:
    """Return (text, tool_name) for an event's content.

    Assistant ``message.content`` is a list of items of type
    ``thinking`` / ``text`` / ``tool_use`` / ``tool_result``; user
    content may be a string or a list of ``tool_result`` items.
    """
    tool_name: str | None = None
    parts: list[str] = []

    if isinstance(content, str):
        return content[:_TEXT_TRUNCATE], None

    if not isinstance(content, list):
        return "", None

    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(item.get("text", ""))
        elif kind == "thinking":
            parts.append(item.get("thinking", ""))
        elif kind == "tool_use":
            tn = item.get("name", "")
            tool_name = tool_name or tn
            inp = item.get("input", {})
            try:
                parts.append(f"[tool_use {tn}] {json.dumps(inp, ensure_ascii=False)}")
            except (TypeError, ValueError):
                parts.append(f"[tool_use {tn}] <unserializable>")
        elif kind == "tool_result":
            c = item.get("content")
            if isinstance(c, str):
                parts.append(f"[tool_result] {c}")
            elif isinstance(c, list):
                for sub in c:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        parts.append(f"[tool_result] {sub.get('text','')}")

    text = "\n".join(p for p in parts if p)
    return text[:_TEXT_TRUNCATE], tool_name


def _iter_events(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                logger.debug("skip malformed line in %s: %s", path, e)


def _event_to_row(raw: dict, session_id: str, ord_: int) -> dict | None:
    """Transform a raw Claude Code JSONL event into an ``agent_events`` row.

    Returns ``None`` for events that should be skipped.
    """
    etype = raw.get("type")
    if etype in SKIP_TYPES or etype not in KEEP_TYPES:
        return None

    uid = raw.get("uuid")
    if not uid:
        return None

    ts = raw.get("timestamp")
    parent_uuid = raw.get("parentUuid")

    text = ""
    tool_name: str | None = None
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    role: str | None = None

    if etype == "assistant":
        msg = raw.get("message", {}) or {}
        model = msg.get("model")
        usage = msg.get("usage", {}) or {}
        tokens_in = (
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
        ) or None
        tokens_out = usage.get("output_tokens") or None
        role = msg.get("role", "assistant")
        text, tool_name = _flatten_content(msg.get("content"))
        content_json = msg
    elif etype == "user":
        msg = raw.get("message", {}) or {}
        role = msg.get("role", "user")
        text, tool_name = _flatten_content(msg.get("content"))
        # user events that carry tool_result items: reclassify for easier querying
        if tool_name is None and isinstance(msg.get("content"), list):
            for it in msg["content"]:
                if isinstance(it, dict) and it.get("type") == "tool_result":
                    etype = "tool_result"
                    break
        content_json = msg
    elif etype == "system":
        role = "system"
        text, _ = _flatten_content(raw.get("content"))
        content_json = {k: raw.get(k) for k in ("subtype", "level", "content", "toolUseID")}
    elif etype == "progress":
        content_json = raw.get("data", {}) or {}
        try:
            text = json.dumps(content_json, ensure_ascii=False)[:_TEXT_TRUNCATE]
        except (TypeError, ValueError):
            text = ""
    else:
        return None

    try:
        content_blob = json.dumps(content_json, ensure_ascii=False)
    except (TypeError, ValueError):
        content_blob = json.dumps({"_error": "unserializable"}, ensure_ascii=False)

    return {
        "uuid": uid,
        "session_id": session_id,
        "parent_uuid": parent_uuid,
        "ord": ord_,
        "type": etype,
        "role": role,
        "timestamp": ts,
        "model": model,
        "text": text,
        "content_json": content_blob,
        "tool_name": tool_name,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def ingest_file(path: Path, *, force: bool = False) -> dict:
    """Ingest a single transcript file. Skips if source_mtime unchanged.

    Returns ``{"session_id", "events", "skipped"}``.
    """
    from okuro.db import get_db

    db = get_db()
    session_id = path.stem
    mtime = path.stat().st_mtime

    if not force:
        # Per-source idempotency check — each physical file may contribute
        # to a session_id that other files also contribute to (CC can
        # copy/resume a session across project dirs).
        existing = db.fetchone(
            "SELECT source_mtime FROM agent_session_sources WHERE source_path = ?",
            (str(path),),
        )
        if existing and existing.get("source_mtime") == mtime:
            return {"session_id": session_id, "events": 0, "skipped": True}

    # First pass: collect session-level stats and all event rows
    rows: list[dict] = []
    first_ts: str | None = None
    last_ts: str | None = None
    user_count = 0
    assistant_count = 0
    tokens_in = 0
    tokens_out = 0
    last_model: str | None = None
    project_path: str | None = None
    git_branch: str | None = None

    for ord_, raw in enumerate(_iter_events(path)):
        project_path = project_path or raw.get("cwd")
        git_branch = git_branch or raw.get("gitBranch")
        row = _event_to_row(raw, session_id, ord_)
        if row is None:
            continue
        ts = row["timestamp"]
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        if row["type"] == "user":
            user_count += 1
        elif row["type"] == "assistant":
            assistant_count += 1
            if row["tokens_in"]:
                tokens_in += row["tokens_in"]
            if row["tokens_out"]:
                tokens_out += row["tokens_out"]
            if row["model"]:
                last_model = row["model"]
        rows.append(row)

    if not rows:
        return {"session_id": session_id, "events": 0, "skipped": False}

    # write() is not reentrant; use the raw conn directly for a single
    # atomic session+events batch.
    with db.write() as conn:
        # Ensure the session row exists (seed identity + provider). Other
        # columns are filled by the post-event aggregation below so the
        # stats always reflect the sum across every source contributing
        # to this session_id.
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
                session_id, PROVIDER, project_path, git_branch,
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
        # Upsert events; FTS triggers keep the index synced. Named binds map
        # directly to the dict rows we built above.
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
        # Re-aggregate session-level stats from the full event set so
        # stats reflect every source contributing to this session_id.
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
                                      WHERE session_id = ? AND type = 'assistant' AND model IS NOT NULL
                                      ORDER BY timestamp DESC LIMIT 1),
                                    model
                                  )
            WHERE session_id = ?
            """,
            (session_id,) * 9,
        )

    return {"session_id": session_id, "events": len(rows), "skipped": False}


def ingest(*, root: Path | None = None, force: bool = False) -> dict:
    """Walk every transcript under ``root`` (default ``~/.claude/projects``).

    Returns ``{"files": N, "ingested": M, "skipped": K, "events": E}``.
    """
    root = root or TRANSCRIPT_ROOT
    if not root.exists():
        return {"files": 0, "ingested": 0, "skipped": 0, "events": 0}

    files = 0
    ingested = 0
    skipped = 0
    events = 0
    for path in root.rglob("*.jsonl"):
        files += 1
        try:
            result = ingest_file(path, force=force)
        except Exception:  # pragma: no cover — defensive; one bad file shouldn't kill the run
            logger.exception("trace ingest failed for %s", path)
            continue
        if result["skipped"]:
            skipped += 1
        else:
            ingested += 1
            events += result["events"]

    return {"files": files, "ingested": ingested, "skipped": skipped, "events": events}
