# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Codex CLI transcript ingester — walks ~/.codex/sessions/**/rollout-*.jsonl.
# index: imports | constants | _flatten_content_items | _event_to_row | ingest_file | ingest
# AGENT_HEADER_END -->
"""Ingest Codex CLI transcripts into ``agent_sessions`` / ``agent_events``.

Codex (codex_cli_rs) writes one JSONL file per session under
``~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<iso>-<session-id>.jsonl``.

Each line has the shape::

    {"timestamp": "<iso>", "type": "<top-type>", "payload": {...}}

Top-level ``type`` values observed:

* ``session_meta``     — session header (id, cwd, model_provider, cli_version, base_instructions, git)
* ``response_item``    — a discrete model/tool/function step
                         (payload.type ∈ {message, reasoning, function_call, function_call_output})
* ``event_msg``        — UI-side stream event
                         (payload.type ∈ {user_message, agent_message, agent_reasoning,
                          token_count, ...})
* ``turn_context``     — per-turn cwd / sandbox / approval policy snapshot
* (others — ignored as housekeeping)

We reuse the claude-code shape (``user`` / ``assistant`` / ``system`` / ``tool_result``) so
the unified query surface keeps working. Token counts come from ``event_msg.token_count``
and are aggregated onto the most-recent assistant event (codex emits them out-of-band).

Idempotent: codex events have no native uuid, so we synthesize one as
``codex:<session_id>:<line_no>``. Re-running on an unchanged file is cheap
(``source_mtime`` is compared and the file is skipped).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable
from okuro.db.sentinels import armed_executemany

logger = logging.getLogger(__name__)

PROVIDER = "codex"
TRANSCRIPT_ROOT = Path("~/.codex/sessions").expanduser()

_TEXT_TRUNCATE = 200_000  # per-event cap to keep FTS index bounded

# Session-id is encoded in the filename: rollout-<iso>-<uuid>.jsonl
_FNAME_RE = re.compile(r"rollout-[\dT\-]+-([0-9a-f-]+)\.jsonl$", re.IGNORECASE)


def _flatten_content_items(items: Any) -> str:
    """Flatten a list of codex content items (input_text / output_text / etc) to plain text."""
    if isinstance(items, str):
        return items[:_TEXT_TRUNCATE]
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        kind = it.get("type")
        if kind in ("input_text", "output_text", "summary_text", "text"):
            parts.append(str(it.get("text", "")))
        elif kind == "input_image":
            parts.append("[input_image]")
        else:
            # Unknown item kind — preserve type only, skip body
            parts.append(f"[{kind}]")
    return "\n".join(p for p in parts if p)[:_TEXT_TRUNCATE]


def _iter_lines(path: Path) -> Iterable[tuple[int, dict]]:
    """Yield (line_no, parsed_json) for every non-blank line. Bad lines are skipped + logged."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("codex: skip malformed line %s:%d (%s)", path, line_no, e)


def _event_to_row(
    raw: dict,
    session_id: str,
    line_no: int,
    *,
    last_model: str | None,
) -> dict | None:
    """Transform one codex JSONL line into an ``agent_events`` row.

    Returns ``None`` for housekeeping/skip lines. Be tolerant: codex log
    format may change between CLI versions, so wrap field reads in
    ``.get()`` and never assume nesting depth.
    """
    top_type = raw.get("type")
    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    ts = raw.get("timestamp")
    uid = f"codex:{session_id}:{line_no}"

    etype: str | None = None
    role: str | None = None
    text: str = ""
    tool_name: str | None = None
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None

    if top_type == "session_meta":
        # Keep as a system event so the row count includes it (and ts/cwd are queryable).
        etype = "system"
        role = "system"
        text = f"[session_meta] cli={payload.get('cli_version')} provider={payload.get('model_provider')}"
    elif top_type == "response_item":
        ptype = payload.get("type")
        if ptype == "message":
            msg_role = payload.get("role")
            if msg_role == "assistant":
                etype = "assistant"
                role = "assistant"
                text = _flatten_content_items(payload.get("content"))
                model = last_model
            elif msg_role == "user":
                etype = "user"
                role = "user"
                text = _flatten_content_items(payload.get("content"))
            else:
                # 'developer' / 'system' framing turns from codex itself
                etype = "system"
                role = msg_role or "system"
                text = _flatten_content_items(payload.get("content"))
        elif ptype == "reasoning":
            etype = "assistant"
            role = "assistant"
            text = _flatten_content_items(payload.get("summary"))
            if not text:
                text = "[reasoning]"
            model = last_model
        elif ptype == "function_call":
            etype = "assistant"
            role = "assistant"
            tool_name = payload.get("name")
            args = payload.get("arguments")
            try:
                args_text = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            except (TypeError, ValueError):
                args_text = "<unserializable>"
            text = f"[tool_use {tool_name}] {args_text}"
            model = last_model
        elif ptype == "function_call_output":
            etype = "tool_result"
            role = "user"
            out = payload.get("output")
            text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
        else:
            return None
    elif top_type == "event_msg":
        ptype = payload.get("type")
        if ptype == "user_message":
            # event_msg user_message duplicates the response_item user message — skip.
            return None
        if ptype == "agent_message":
            # event_msg agent_message duplicates response_item assistant — skip to avoid double counting.
            return None
        if ptype == "agent_reasoning":
            return None  # also duplicated by response_item reasoning
        if ptype == "token_count":
            # Token counts surface here, not on the assistant turn itself. We emit a
            # cheap progress row so totals are visible per-event but we don't
            # double-count with the assistant's own tokens (assistant rows have None).
            etype = "progress"
            role = None
            info = payload.get("info") or {}
            usage = info.get("total_token_usage") if isinstance(info, dict) else None
            if isinstance(usage, dict):
                tokens_in = usage.get("input_tokens")
                tokens_out = usage.get("output_tokens")
            try:
                text = json.dumps({"token_count": usage}, ensure_ascii=False)[:_TEXT_TRUNCATE]
            except (TypeError, ValueError):
                text = ""
        else:
            return None
    else:
        # turn_context, unknown types — housekeeping, skip
        return None

    if etype is None:
        return None

    try:
        content_blob = json.dumps(raw, ensure_ascii=False)[:_TEXT_TRUNCATE * 2]
    except (TypeError, ValueError):
        content_blob = json.dumps({"_error": "unserializable"}, ensure_ascii=False)

    return {
        "uuid": uid,
        "session_id": session_id,
        "parent_uuid": None,
        "ord": line_no,
        "type": etype,
        "role": role,
        "timestamp": ts,
        "model": model,
        "text": text[:_TEXT_TRUNCATE],
        "content_json": content_blob,
        "tool_name": tool_name,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def _session_id_from_path(path: Path) -> str:
    """Extract the codex uuid from a rollout filename. Falls back to the stem if parse fails."""
    m = _FNAME_RE.search(path.name)
    if m:
        return m.group(1)
    return path.stem


def ingest_file(path: Path, *, force: bool = False) -> dict:
    """Ingest a single codex transcript file. Skips if source_mtime unchanged.

    Returns ``{"session_id", "events", "skipped"}``.
    """
    from okuro.db import get_db

    db = get_db()
    session_id = _session_id_from_path(path)
    mtime = path.stat().st_mtime

    if not force:
        existing = db.fetchone(
            "SELECT source_mtime FROM agent_session_sources WHERE source_path = ?",
            (str(path),),
        )
        if existing and existing.get("source_mtime") == mtime:
            return {"session_id": session_id, "events": 0, "skipped": True}

    rows: list[dict] = []
    project_path: str | None = None
    git_branch: str | None = None
    cli_model: str | None = None
    last_model: str | None = None

    for line_no, raw in _iter_lines(path):
        # Pull session metadata opportunistically — it should be the first line
        # but we tolerate it appearing later.
        if not isinstance(raw, dict):
            continue
        top_type = raw.get("type")
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        if top_type == "session_meta":
            project_path = project_path or payload.get("cwd")
            git = payload.get("git") or {}
            if isinstance(git, dict):
                git_branch = git_branch or git.get("branch")
            # codex doesn't always surface a model name — prefer model_provider as a tag
            mp = payload.get("model_provider")
            if mp and not cli_model:
                cli_model = f"codex/{mp}"
                last_model = cli_model
        try:
            row = _event_to_row(raw, session_id, line_no, last_model=last_model)
        except Exception as e:  # pragma: no cover — defensive: never crash on a bad line
            logger.warning("codex: bad event in %s:%d (%s)", path, line_no, e)
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
        # Re-aggregate session-level stats. Token counts come from progress
        # events (codex emits them out-of-band) so include those in the sum.
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
    """Walk every codex transcript under ``root`` (default ``~/.codex/sessions``).

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
        except Exception:
            logger.exception("codex: ingest failed for %s", path)
            continue
        if result["skipped"]:
            skipped += 1
        else:
            ingested += 1
            events += result["events"]

    return {"files": files, "ingested": ingested, "skipped": skipped, "events": events}
