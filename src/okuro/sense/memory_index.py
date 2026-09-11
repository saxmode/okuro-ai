# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: AAAK-style pointer index over agent_memory — compact bootstrap surface.
# index: imports | def extract_entities | def derive_flags | def make_label | def write_pointer | def read_pointers | def backfill_pointers | def render_pointer_lines
# AGENT_HEADER_END -->
"""AAAK-style pointer index over agent_memory.

mempalace closet/AAAK pattern adapted for okuro: each agent_memory row gets
a compact pointer extracted heuristically (no LLM — okuro must run without
local inference). Bootstrap surfaces 25-40 one-line pointers for the same
token cost as 8-10 full memory bodies.

Pointer line format:
    [topic-kind] short-label | entities | flags | →memory_id

Entities: file paths, dotted.module.function names, backtick-quoted tokens,
CamelCase identifiers — extracted via regex, not NLP.

Flags: derived from agent_memory.topic + confidence + supersedes status.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Extraction regexes
# ---------------------------------------------------------------------------

# File paths: absolute or homedir-relative, with at least one separator.
_FILE_PATH_RE = re.compile(r"(?:~/|/(?:home|etc|tmp|usr|var|opt|mnt|media)/|\./)[\w./\-]{3,}")

# `backticked.tokens` — usually function names, file basenames, identifiers.
_BACKTICK_RE = re.compile(r"`([^`\n]{2,80})`")

# Dotted module/function refs: foo.bar, foo.bar.baz — 2-3 segments, alpha+underscore.
_DOTTED_RE = re.compile(r"\b[a-z_][a-z0-9_]+(?:\.[a-z_][a-z0-9_]+){1,3}\b")

# CamelCase / PascalCase identifiers (skip single capitals).
_CAMEL_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")

# Sentence terminator detection for short-label extraction.
_SENT_END_RE = re.compile(r"[.!?]\s+|\n")

# ---------------------------------------------------------------------------
# Stoplists / limits
# ---------------------------------------------------------------------------

_ENTITY_STOPLIST = frozenset({
    "TODO", "FIXME", "XXX", "NOTE", "WARNING", "INFO", "ERROR",
    "True", "False", "None", "Some", "Most", "Many",
})

_LABEL_MAX = 80
_ENTITIES_PER_POINTER = 5
_ENTITY_MAX_LEN = 60
_POINTER_LINE_CAP = 220   # hard cap on a fully-rendered pointer line


# ---------------------------------------------------------------------------
# Extraction primitives
# ---------------------------------------------------------------------------


def make_label(content: str, max_len: int = _LABEL_MAX) -> str:
    """First clause/sentence of content, trimmed and lowercased-friendly."""
    if not content:
        return ""
    text = content.strip()
    # Strip leading bracketed kind tags some agents prepend, e.g. "[gotcha] ..."
    text = re.sub(r"^\[[a-zA-Z_-]+\]\s*", "", text)
    match = _SENT_END_RE.search(text)
    head = text[: match.start()] if match else text
    head = head.strip().replace("\n", " ")
    head = re.sub(r"\s{2,}", " ", head)
    if len(head) > max_len:
        head = head[: max_len - 1].rstrip() + "…"
    return head


def extract_entities(content: str, limit: int = _ENTITIES_PER_POINTER) -> list[str]:
    """Pull file paths, backtick tokens, dotted refs, camelcase identifiers.

    Heuristic, frequency-ranked. Stoplist filters generic words.
    """
    if not content:
        return []
    candidates: list[str] = []
    candidates.extend(_FILE_PATH_RE.findall(content))
    candidates.extend(_BACKTICK_RE.findall(content))
    candidates.extend(_DOTTED_RE.findall(content))
    candidates.extend(_CAMEL_RE.findall(content))

    seen: dict[str, int] = {}
    for raw in candidates:
        token = raw.strip().rstrip(".,;:")
        if not token or token in _ENTITY_STOPLIST or len(token) > _ENTITY_MAX_LEN:
            continue
        # Dedup case-insensitively, keep first-seen casing.
        key = token.lower()
        if key not in seen:
            seen[key] = 0
        seen[key] += 1

    # Re-resolve to original casing by scanning candidates again.
    casing: dict[str, str] = {}
    for raw in candidates:
        token = raw.strip().rstrip(".,;:")
        if token and token.lower() not in casing:
            casing[token.lower()] = token

    ranked = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))
    return [casing[k] for k, _ in ranked[:limit] if k in casing]


def derive_flags(topic: str, confidence: float, supersedes: str | None) -> list[str]:
    """Map topic kind + signals to compact uppercase flag tokens."""
    flags: list[str] = []
    topic_map = {
        "decision": "DECISION",
        "gotcha": "GOTCHA",
        "architecture": "ARCH",
        "convention": "CONVENTION",
        "learning": "LEARNING",
    }
    if topic in topic_map:
        flags.append(topic_map[topic])
    if confidence >= 0.9:
        flags.append("HIGH-CONF")
    if supersedes:
        flags.append("SUPERSEDES")
    return flags


# ---------------------------------------------------------------------------
# Storage / read
# ---------------------------------------------------------------------------


def write_pointer(memory_id: str, topic: str, content: str,
                  project: str | None = None, confidence: float = 0.7,
                  supersedes: str | None = None) -> None:
    """Best-effort pointer write. Failures are swallowed — never block a memory."""
    try:
        from okuro.db import get_db
        db = get_db()
        label = make_label(content)
        entities = ";".join(extract_entities(content))
        flags = ",".join(derive_flags(topic, confidence, supersedes))
        with db.write():
            db.execute(
                """INSERT OR REPLACE INTO memory_pointers
                   (memory_id, topic_label, entities, flags, project, topic_kind, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (memory_id, label, entities, flags, project, topic, confidence),
            )
    except Exception:  # pragma: no cover — defensive
        # Swallowed so a pointer failure never blocks the memory write itself,
        # which is right. But it was also unlogged, and 18 of 2930 memories
        # have no pointer row as a result — absent from the bootstrap pointer
        # surface with nothing recording why.
        logging.getLogger("okuro.sense.memory_index").warning(
            "write_pointer failed for %s — memory stored but missing from the "
            "pointer surface", memory_id, exc_info=True,
        )


def read_pointers(query: str | None = None, project: str | None = None,
                  limit: int = 30, min_confidence: float = 0.3,
                  topic: str | None = None) -> list[dict]:
    """Read pointer rows. Falls back to plain DB query if vec_memory misses.

    When `query` is provided, joins to vec_memory for semantic ranking; rows
    not in vec_memory still surface (LEFT join semantics) but at the bottom
    of the list, scored by confidence.
    """
    from okuro.db import get_db
    db = get_db()

    base_sql = """SELECT memory_id, topic_label, entities, flags, project,
                         topic_kind, confidence
                  FROM memory_pointers
                  WHERE confidence >= ?"""
    params: list = [min_confidence]

    if topic:
        base_sql += " AND topic_kind = ?"
        params.append(topic)

    if project:
        base_sql += " AND (project = ? OR project IS NULL)"
        params.append(project)

    # Semantic re-rank when query given.
    if query:
        try:
            from okuro.embed.client import embed_query, to_bytes
            vec_bytes = to_bytes(embed_query(query))
            matches = db.vec_search("vec_memory", vec_bytes, limit=limit * 3)
            if matches:
                ids = [m["id"] for m in matches]
                distances = {m["id"]: m["distance"] for m in matches}
                placeholders = ",".join("?" * len(ids))
                ranked_sql = base_sql + f" AND memory_id IN ({placeholders})"
                rows = db.fetchall(ranked_sql, tuple(params + ids))
                rows = sorted(rows, key=lambda r: distances.get(r["memory_id"], 1.0))
                return [dict(r) for r in rows[:limit]]
        except Exception:
            pass  # fall through to plain query

    base_sql += " ORDER BY confidence DESC, generated_at DESC LIMIT ?"
    params.append(limit)
    rows = db.fetchall(base_sql, tuple(params))
    return [dict(r) for r in rows]


def render_pointer_lines(rows: list[dict], id_prefix_len: int = 8) -> list[str]:
    """Render pointer rows as one-line strings, capped at _POINTER_LINE_CAP chars."""
    lines: list[str] = []
    for r in rows:
        kind = r.get("topic_kind") or "memory"
        label = r.get("topic_label") or ""
        entities = r.get("entities") or ""
        flags = r.get("flags") or ""
        mid = (r.get("memory_id") or "")[:id_prefix_len]
        scope = r.get("project") or "system"
        # Compact, structured, one line.
        parts = [
            f"[{kind}]",
            label or "(no-label)",
        ]
        if entities:
            parts.append(f"@{entities}")
        if flags:
            parts.append(f"#{flags}")
        parts.append(f"→{mid}")
        parts.append(f"({scope})")
        line = "- " + " | ".join(parts)
        if len(line) > _POINTER_LINE_CAP:
            line = line[: _POINTER_LINE_CAP - 1] + "…"
        lines.append(line)
    return lines


def backfill_pointers(batch_size: int = 500) -> dict:
    """One-shot backfill: generate pointers for memories missing one.

    Idempotent — uses INSERT OR REPLACE. Returns counts. Safe to re-run
    after migration deploy or after extraction-rule changes.
    """
    from okuro.db import get_db
    db = get_db()
    rows = db.fetchall(
        """SELECT m.id, m.topic, m.content, m.project, m.confidence, m.supersedes
           FROM agent_memory m
           LEFT JOIN memory_pointers p ON p.memory_id = m.id
           WHERE p.memory_id IS NULL
           LIMIT ?""",
        (batch_size,),
    )
    written = 0
    for r in rows:
        write_pointer(
            memory_id=r["id"],
            topic=r["topic"],
            content=r["content"] or "",
            project=r["project"],
            confidence=r["confidence"] or 0.7,
            supersedes=r["supersedes"],
        )
        written += 1
    return {"scanned": len(rows), "written": written}
