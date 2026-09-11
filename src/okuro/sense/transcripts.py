# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Verbatim per-message transcript ingest + search (mempalace sweeper port).
# index: imports | adapters | def _deterministic_id | class ClaudeJSONLAdapter | class GeminiAdapter | class CodexAdapter | def sweep | def transcript_search | def transcript_session_messages
# AGENT_HEADER_END -->
"""Verbatim per-message transcript ingest + search.

Adapted from mempalace/sweeper.py. One row per message, idempotent on the
deterministic ID ``sha256(source:session_id:message_uuid)``. Resume-safe —
re-running on a partially ingested transcript writes only new rows.

Provider-agnostic by design: each ``source`` plugs in via the ``_ADAPTERS``
dict. Adapters are pure iterators yielding normalized message dicts; storage
is shared.

Schema: see migration ``030_transcripts.sql`` (transcript_messages +
vec_transcript_messages).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

# Per-message embed cap. Long assistant turns (tool-use chains) bloat the
# embedding cost; we already store the full verbatim content, so capping
# the embedding input is a cost optimization, not a fidelity loss.
_EMBED_CHAR_CAP = 4000


# ---------------------------------------------------------------------------
# Deterministic ID
# ---------------------------------------------------------------------------


def _deterministic_id(source: str, session_id: str, message_uuid: str) -> str:
    """Stable hash so re-runs upsert the same row."""
    h = hashlib.sha256()
    h.update(source.encode("utf-8"))
    h.update(b":")
    h.update(session_id.encode("utf-8"))
    h.update(b":")
    h.update(message_uuid.encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Content normalization
# ---------------------------------------------------------------------------


def _flatten_content(content) -> str:
    """Normalize Claude/MCP message content to plain text.

    User messages are usually strings; assistant messages are lists of
    content blocks like ``[{"type": "text", "text": "..."}]``.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                # Surface the tool name + input as searchable text.
                parts.append(
                    f"[tool_use:{block.get('name', '?')}] "
                    f"{json.dumps(block.get('input', {}), ensure_ascii=False)}"
                )
            elif btype == "tool_result":
                tr = block.get("content", "")
                if isinstance(tr, list):
                    tr = _flatten_content(tr)
                parts.append(f"[tool_result] {tr}")
            elif btype == "thinking":
                # Skip — internal reasoning, not for verbatim transcript.
                continue
        return "\n".join(p for p in parts if p)
    return str(content)


# ---------------------------------------------------------------------------
# Source adapter registry (mempalace RFC 002 minus the bloat)
# ---------------------------------------------------------------------------
#
# Adapters are class instances exposing a stable contract. Compared to the
# previous dict-of-functions, this lets:
#   - 3rd-party adapters register without editing this file
#     (``register_adapter(MyAdapter())``)
#   - per-adapter metadata (``default_extension``, ``describe()``) feed the
#     sweep walker and tooling without sprawling switch-cases
#   - subclasses override one method (``ingest``) instead of forking a free
#     function
#
# The contract intentionally stops well short of mempalace RFC 002. We don't
# carry transformations / privacy classes / capabilities / spec_version —
# okuro doesn't need them today and DP09 (no-bloat) says don't pre-add
# infrastructure.


class TranscriptSourceAdapter:
    """Contract for a transcript source adapter.

    Subclass and implement :meth:`ingest`. Override
    :attr:`default_extension` when the source isn't ``.jsonl``.
    """

    name: str = ""
    default_extension: str = ".jsonl"
    description: str = ""

    def ingest(self, path: Path) -> Iterator[dict]:
        """Yield normalized message dicts.

        Each yielded dict MUST carry: ``session_id``, ``message_uuid``,
        ``role``, ``content``. Optional: ``ts``, ``metadata``. Adapters
        skip records they can't form a stable ID for.
        """
        raise NotImplementedError

    def describe(self) -> dict:
        return {
            "name": self.name,
            "default_extension": self.default_extension,
            "description": self.description,
        }


class ClaudeJSONLAdapter(TranscriptSourceAdapter):
    name = "claude-jsonl"
    default_extension = ".jsonl"
    description = "Claude Code session JSONL — ~/.claude/projects/<slug>/<sid>.jsonl"

    def ingest(self, path: Path) -> Iterator[dict]:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type")
                if rtype not in ("user", "assistant"):
                    continue
                msg = rec.get("message") or {}
                role = msg.get("role") or rtype
                content = _flatten_content(msg.get("content"))
                if not content:
                    continue
                uuid = rec.get("uuid") or msg.get("id") or ""
                session_id = rec.get("sessionId") or rec.get("session_id") or ""
                ts = rec.get("timestamp") or ""
                if not uuid or not session_id:
                    # Without both we can't form a stable ID; skip rather than
                    # write a synthesized one that fights the dedup invariant.
                    continue
                yield {
                    "session_id": session_id,
                    "message_uuid": uuid,
                    "role": role,
                    "content": content,
                    "ts": ts,
                    "metadata": {
                        "model": msg.get("model"),
                        "type": rtype,
                        "parent_uuid": rec.get("parentUuid"),
                    },
                }


class GeminiAdapter(TranscriptSourceAdapter):
    name = "gemini"
    default_extension = ""
    description = "Gemini CLI transcript adapter — placeholder until format is locked"

    def ingest(self, path: Path) -> Iterator[dict]:
        return iter(())


class CodexAdapter(TranscriptSourceAdapter):
    name = "codex"
    default_extension = ""
    description = "Codex CLI transcript adapter — placeholder until format is locked"

    def ingest(self, path: Path) -> Iterator[dict]:
        return iter(())


# Registry. ``register_adapter`` is the public extension point.
_ADAPTERS: dict[str, TranscriptSourceAdapter] = {}


def register_adapter(adapter: TranscriptSourceAdapter) -> None:
    """Register a TranscriptSourceAdapter instance. Replaces by name."""
    if not adapter.name:
        raise ValueError("adapter.name must be set")
    _ADAPTERS[adapter.name] = adapter


def list_adapters() -> list[dict]:
    """Public introspection — returns describe() for every registered adapter."""
    return [a.describe() for a in _ADAPTERS.values()]


# Built-in adapters.
register_adapter(ClaudeJSONLAdapter())
register_adapter(GeminiAdapter())
register_adapter(CodexAdapter())


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def sweep(path: str, source: str = "claude-jsonl", project: str | None = None,
          embed: bool = True) -> dict:
    """Idempotent verbatim message ingest from a transcript file or directory.

    Args:
        path: Transcript file OR a directory containing transcripts. When a
              directory is passed we recursively scan files matching the
              source's typical extension (``.jsonl`` for ``claude-jsonl``).
        source: Adapter key (see ``_ADAPTERS``).
        project: Project slug to associate (None = system-wide).
        embed: Whether to embed each message for vector search.

    Returns:
        dict with scanned/written/skipped counts.
    """
    if source not in _ADAPTERS:
        return {"error": f"unknown source {source!r}; known: {sorted(_ADAPTERS)}"}

    adapter = _ADAPTERS[source]
    target = Path(os.path.expanduser(path))
    if not target.exists():
        return {"error": f"path not found: {target}"}

    files: list[Path]
    if target.is_dir():
        ext = adapter.default_extension
        if ext:
            files = sorted(target.rglob(f"*{ext}"))
        else:
            files = [p for p in sorted(target.rglob("*")) if p.is_file()]
    else:
        files = [target]

    from okuro.db import get_db
    db = get_db()

    written = 0
    skipped = 0
    scanned = 0

    if project:
        from okuro.sense.progress import _ensure_project
        _ensure_project(db, project)

    embedder = None
    to_bytes = None
    if embed:
        try:
            from okuro.embed.client import embed_one as _embed_one
            from okuro.embed.client import to_bytes as _to_bytes
            embedder = _embed_one
            to_bytes = _to_bytes
        except Exception as exc:
            log.warning("transcripts.sweep: embedding disabled (%s)", exc)
            embedder = None

    for fpath in files:
        for msg in adapter.ingest(fpath):
            scanned += 1
            mid = _deterministic_id(source, msg["session_id"], msg["message_uuid"])

            existing = db.fetchone(
                "SELECT 1 FROM transcript_messages WHERE id = ?",
                (mid,),
            )
            if existing:
                skipped += 1
                continue

            content = msg["content"]
            metadata = msg.get("metadata") or {}

            with db.write():
                db.execute(
                    """INSERT INTO transcript_messages
                       (id, source, session_id, message_uuid, role, content,
                        ts, project, transcript_path, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        mid,
                        source,
                        msg["session_id"],
                        msg["message_uuid"],
                        msg["role"],
                        content,
                        msg.get("ts") or None,
                        project,
                        str(fpath),
                        json.dumps(metadata, ensure_ascii=False),
                    ),
                )
                if embedder is not None:
                    try:
                        vec = embedder(content[:_EMBED_CHAR_CAP])
                        db.execute(
                            "INSERT INTO vec_transcript_messages (id, embedding) VALUES (?, ?)",
                            (mid, to_bytes(vec)),
                        )
                    except Exception:
                        pass  # vector failure must not block storage

            written += 1

    return {
        "source": source,
        "files": len(files),
        "scanned": scanned,
        "written": written,
        "skipped": skipped,
        "project": project,
    }


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def transcript_search(query: str, project: str | None = None,
                      source: str | None = None, role: str | None = None,
                      session_id: str | None = None,
                      limit: int = 10) -> list[dict]:
    """Semantic search over transcript_messages.

    Returns ranked rows with content, role, ts, source, session_id, project,
    and a similarity score. Falls back to ORDER BY ts DESC when embeddings
    are unavailable.
    """
    from okuro.db import get_db
    db = get_db()

    base_where = ["1=1"]
    base_params: list = []

    if source:
        base_where.append("source = ?")
        base_params.append(source)
    if role:
        base_where.append("role = ?")
        base_params.append(role)
    if project:
        base_where.append("(project = ? OR project IS NULL)")
        base_params.append(project)
    if session_id:
        base_where.append("session_id = ?")
        base_params.append(session_id)

    where_sql = " AND ".join(base_where)

    # Vector path
    try:
        from okuro.embed.client import embed_query, to_bytes
        vec_bytes = to_bytes(embed_query(query))
        matches = db.vec_search("vec_transcript_messages", vec_bytes, limit=limit * 4)
    except Exception:
        matches = []

    if matches:
        ids = [m["id"] for m in matches]
        distances = {m["id"]: m["distance"] for m in matches}
        placeholders = ",".join("?" * len(ids))
        rows = db.fetchall(
            f"""SELECT id, source, session_id, role, content, ts, project,
                       transcript_path
                FROM transcript_messages
                WHERE id IN ({placeholders}) AND {where_sql}""",
            tuple(ids) + tuple(base_params),
        )
        ranked = sorted(
            (dict(r) for r in rows),
            key=lambda r: distances.get(r["id"], 1.0),
        )[:limit]
        for r in ranked:
            r["similarity"] = 1.0 - distances.get(r["id"], 1.0)
        return ranked

    # Fallback: chronological
    rows = db.fetchall(
        f"""SELECT id, source, session_id, role, content, ts, project,
                   transcript_path
            FROM transcript_messages
            WHERE {where_sql}
            ORDER BY ts DESC
            LIMIT ?""",
        tuple(base_params) + (limit,),
    )
    return [dict(r) for r in rows]


def transcript_session_messages(session_id: str, source: str | None = None,
                                 limit: int = 100) -> list[dict]:
    """Ordered transcript for one session_id (oldest → newest)."""
    from okuro.db import get_db
    db = get_db()
    where = ["session_id = ?"]
    params: list = [session_id]
    if source:
        where.append("source = ?")
        params.append(source)
    rows = db.fetchall(
        f"""SELECT id, source, session_id, role, content, ts, project
            FROM transcript_messages
            WHERE {' AND '.join(where)}
            ORDER BY ts ASC
            LIMIT ?""",
        tuple(params) + (limit,),
    )
    return [dict(r) for r in rows]
