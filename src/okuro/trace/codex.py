# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Codex CLI transcript ingester — walks ~/.codex/sessions/**/rollout-*.jsonl.
# index: imports | constants | _flatten_content_items | _call_text | _output_text | _session_kind | _event_to_row | ingest_file | ingest
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

TWO THINGS THIS FILE GOT WRONG UNTIL 2026-09-17, both of the same shape — a
field read without asking what it means:

* ``token_count.info`` carries BOTH ``total_token_usage`` (a running total)
  and ``last_token_usage`` (the turn's delta). The cumulative one was stored
  per event and then SUMMED into the session, so a 163-turn session reported
  1,333,331,774 input tokens against a true 18,767,971 — the store's largest
  codex figure was 79,404,932,212. Reading the delta makes the existing sum
  correct; see the comment at the ``token_count`` branch for the arithmetic
  that identifies which field is which.

* The payload-type table listed one tool-call shape and codex 0.154 emits
  four. ``custom_tool_call``, ``web_search_call`` and ``tool_search_call``
  (with their outputs) fell through to ``return None`` and never became rows
  at all — 5,903 events across the 650 files on this host, 2,484 of them
  genuine tool calls that no tool-adoption figure could see.

Session provenance is recorded, not inferred: ``session_meta.originator``
says whether a person, ``codex exec``, or Codex Desktop's external importer
produced the file, and that lands in ``agent_sessions.session_kind``. Without
it, 278 ``codex exec`` runs and 94 imported conversations counted as sessions
in every rate.

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

# response_item payload types that ARE a tool call, mapped to the tool name to
# record. None means "the payload carries its own `name`" — that is
# custom_tool_call, whose name is the tool the model invoked (exec, apply_patch
# …). The other two are single-purpose calls with no name field of their own,
# so the type IS the name.
_CALL_TYPES: dict[str, str | None] = {
    "custom_tool_call": None,
    "web_search_call": "web_search",
    "tool_search_call": "tool_search",
}

# …and their results.
_OUTPUT_TYPES = frozenset({"custom_tool_call_output", "tool_search_output"})

# session_meta.payload.originator values that mean a program, not a person,
# started this session. `codex_exec` is `codex exec`, the non-interactive
# runner okuro's own bridge_invoke shells out to — 278 of 650 rollout files on
# this host (measured 2026-09-17), every one of which counts as a session in
# any rate computed today.
_AGENT_ORIGINATORS = frozenset({"codex_exec"})

# Codex Desktop writes a rollout file per conversation it IMPORTS from
# elsewhere. The file is real; the session never ran here. Every timestamp in
# it is the import moment — which is why 94 such files landed with
# first_ts == last_ts to the millisecond and were read as corrupt.
_IMPORT_ORIGINATORS = frozenset({"Codex Desktop"})


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


def _call_text(ptype: str, payload: dict) -> str:
    """Readable argument text for a tool call, whatever field carries it."""
    if ptype == "custom_tool_call":
        raw = payload.get("input")
    elif ptype == "web_search_call":
        raw = payload.get("action")
    else:
        raw = payload.get("arguments")
    if isinstance(raw, str):
        return raw[:_TEXT_TRUNCATE]
    try:
        return json.dumps(raw, ensure_ascii=False, default=str)[:_TEXT_TRUNCATE]
    except (TypeError, ValueError):
        return "<unserializable>"


def _output_text(ptype: str, payload: dict) -> str:
    """Readable result text for a tool output, whatever field carries it.

    ``tool_search_output.tools`` is a list of TOOL DESCRIPTORS, not of content
    items, and must not go through ``_flatten_content_items``: that helper
    renders an unknown item kind as its bare type, so a namespace listing
    flattens to ``[namespace]`` and the tool names — the only part anyone
    would search for — are gone.
    """
    if ptype == "tool_search_output":
        raw = payload.get("tools")
    else:
        raw = payload.get("output")
        if isinstance(raw, str):
            return raw[:_TEXT_TRUNCATE]
        if isinstance(raw, list):
            flat = _flatten_content_items(raw)
            if flat:
                return flat
    if isinstance(raw, str):
        return raw[:_TEXT_TRUNCATE]
    try:
        return json.dumps(raw, ensure_ascii=False, default=str)[:_TEXT_TRUNCATE]
    except (TypeError, ValueError):
        return ""


def _session_kind(payload: dict) -> str:
    """Who started this session, from ``session_meta``.

    VOCABULARY IS okuro's EXISTING ONE, not a new one:
    ``interaction.turns.session_kind`` already answers this question with
    ``subagent`` / ``human-facing`` and is the store's single definition
    (distill/triage.py delegates to it rather than re-deriving, explicitly so
    that one definition exists). That function reads a claude-code id prefix
    and so cannot speak for codex at all — but its words are the right ones
    and a second vocabulary for one concept is how two answers start
    disagreeing with nothing able to adjudicate.

    ``imported`` is the one case the incumbent cannot express: a session id
    says nothing about whether the conversation happened on this machine.

    * ``imported``     — Codex Desktop wrote the file from a conversation that
                         ran somewhere else. Not a session here at all.
    * ``subagent``     — `codex exec` (the non-interactive runner okuro's own
                         bridge shells out to) or a subagent thread spawn,
                         which codex encodes by making `source` a dict.
    * ``human-facing`` — anything else: codex-tui, codex_cli_rs, codex_vscode.

    No ``unknown`` branch: every rollout file carries a ``session_meta`` line
    with an ``originator``, and a file without one does not reach here (the
    caller leaves the column NULL).
    """
    originator = payload.get("originator")
    if originator in _IMPORT_ORIGINATORS:
        return "imported"
    if originator in _AGENT_ORIGINATORS:
        return "subagent"
    # A dict `source` is a subagent thread_spawn record. A string source
    # ("cli" / "exec" / "vscode") is a human at a front-end.
    if isinstance(payload.get("source"), dict):
        return "subagent"
    return "human-facing"


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
        elif ptype in _CALL_TYPES:
            # THE PAYLOAD-TYPE TABLE WENT STALE AND THE COST WAS INVISIBLE.
            # codex 0.154 emits tool calls under four names, not one. This
            # branch's absence dropped 2,484 custom_tool_call rows, 366
            # web_search_call, 280 tool_search_call and their outputs —
            # measured across all 650 rollout files under ~/.codex/sessions on
            # 2026-09-17 — and every one of them was a TOOL CALL that
            # trace_stats could not see, because an unhandled type returns None
            # and never reaches agent_events at all.
            etype = "assistant"
            role = "assistant"
            tool_name = _CALL_TYPES[ptype] or payload.get("name")
            text = f"[tool_use {tool_name}] {_call_text(ptype, payload)}"
            model = last_model
        elif ptype in _OUTPUT_TYPES:
            etype = "tool_result"
            role = "user"
            text = _output_text(ptype, payload)
        elif ptype == "agent_message":
            # Inter-agent dispatch (author -> recipient), not a model turn.
            # Typed `system` so it is never counted as an assistant message:
            # these are subagent task handoffs and counting them as replies
            # would inflate exactly the numerator this work exists to protect.
            etype = "system"
            role = "system"
            text = (
                f"[agent_message {payload.get('author')} -> "
                f"{payload.get('recipient')}] "
                + _flatten_content_items(payload.get("content"))
            )
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
            #
            # `last_token_usage` IS THE DELTA; `total_token_usage` IS A RUNNING
            # TOTAL. The session rollup in ingest_file SUMs the per-event values,
            # so reading the cumulative field squares the count. Measured on
            # rollout-2026-09-13T16-52-02-01a09b40-…jsonl (163 token_count
            # events): summing total_token_usage.input_tokens gives
            # 1,333,331,774 — which is exactly the tokens_in this store held for
            # that session — while summing last_token_usage.input_tokens gives
            # 18,767,971, equal to the FINAL total_token_usage.input_tokens to
            # the token. Sum-of-deltas == final-cumulative is the arithmetic
            # that identifies which field is which, and it is why the fix is
            # the delta and not "take the last row".
            etype = "progress"
            role = None
            info = payload.get("info") or {}
            usage = info.get("last_token_usage") if isinstance(info, dict) else None
            if not isinstance(usage, dict):
                # Older codex builds emitted only the cumulative field. One row
                # of it is still better than nothing, and a transcript with a
                # single token_count event reads identically either way.
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
    session_kind: str | None = None

    for line_no, raw in _iter_lines(path):
        # Pull session metadata opportunistically — it should be the first line
        # but we tolerate it appearing later.
        if not isinstance(raw, dict):
            continue
        top_type = raw.get("type")
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        if top_type == "session_meta":
            project_path = project_path or payload.get("cwd")
            session_kind = session_kind or _session_kind(payload)
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
                session_kind, source_path, source_mtime, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(session_id) DO UPDATE SET
                provider       = excluded.provider,
                project_path   = COALESCE(agent_sessions.project_path, excluded.project_path),
                git_branch     = COALESCE(agent_sessions.git_branch, excluded.git_branch),
                -- excluded FIRST, unlike the two above: a re-ingest reads the
                -- marker again from the same file and must be able to CORRECT
                -- a NULL or a stale kind. project_path and git_branch keep the
                -- opposite order because a later line can legitimately lack
                -- them; session_meta either has an originator or the row is
                -- not written at all.
                session_kind   = COALESCE(excluded.session_kind, agent_sessions.session_kind),
                source_path    = excluded.source_path,
                source_mtime   = excluded.source_mtime,
                indexed_at     = datetime('now')
            """,
            (
                session_id, PROVIDER, project_path, git_branch, session_kind,
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
