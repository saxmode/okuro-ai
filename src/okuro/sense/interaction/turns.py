### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Extract and clean the input-side turns of a trace session; embed them for cross-session clustering.
# index: imports | _NOISE_PATTERNS | clean_turn_text | session_kind | iter_turns | iter_session_turns | embed_turns | turn_search
# AGENT_HEADER_END -->
"""The input side of a session: what the human (or parent agent) actually said.

``agent_events`` stores ``type='user'`` rows, but their ``text`` is not what
anyone typed. The harness staples on context the reader never wrote:
``<system-reminder>`` blocks, ``<ide_opened_file>`` notices, slash-command
expansions, the resume ``Caveat:`` preamble, and hook output. Measured over the
corpus: 457 rows carry a system-reminder, 1084 a slash-command block, 415 the
caveat. Feeding that to a detector produces hits on harness furniture; feeding
it to an embedder blurs every turn toward the same boilerplate centroid.

:func:`clean_turn_text` strips the furniture. Everything downstream reads the
cleaned text; the original stays untouched in ``agent_events``.

Two populations
---------------
About half of traced sessions are subagent runs, where the ``type='user'`` turn
is a parent agent's brief rather than a human message. Both are kept — a badly
briefed subagent is as real a problem as a frustrated human, and it is only
visible here. :func:`session_kind` labels which is which so :mod:`.detect` can
run the right detector family.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Embedding input cap. Long turns are dominated by pasted logs and file dumps;
# the discriminating signal sits at the top. The full text is never lost — this
# caps only what reaches the embedder.
EMBED_CHAR_CAP = 2000

# Turns shorter than this after cleaning carry no analyzable content ("ok",
# "yes", "continue"). They still count toward turn totals but are not embedded.
MIN_EMBED_CHARS = 24

# Harness-injected blocks, stripped before analysis. Ordered loosely by how
# often they fire so the common cases short-circuit first.
_NOISE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<ide_opened_file>.*?</ide_opened_file>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<ide_selection>.*?</ide_selection>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<command-name>.*?</command-name>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<command-message>.*?</command-message>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<command-args>.*?</command-args>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<local-command-stderr>.*?</local-command-stderr>", re.DOTALL | re.IGNORECASE),
    # Session-resume preamble, runs to the end of its paragraph.
    re.compile(
        r"Caveat: The messages below were generated.*?(?:\n\s*\n|\Z)",
        re.DOTALL | re.IGNORECASE,
    ),
    # UserPromptSubmit hook output appended by okuro itself.
    re.compile(
        r"Okuro user profile for this turn:.*?(?:\n\s*\n|\Z)",
        re.DOTALL | re.IGNORECASE,
    ),
)


def clean_turn_text(raw: str | None) -> str:
    """Strip harness-injected context, leaving what the sender actually wrote."""
    if not raw:
        return ""
    text = raw
    for pattern in _NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    # Collapse the whitespace the substitutions leave behind.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def session_kind(native_session_id: str) -> str:
    """``'subagent'`` for spawned agent runs, ``'human-facing'`` otherwise.

    Claude Code names subagent transcripts ``agent-<hash>``; a top-level
    session carries a plain uuid. This is the only reliable in-band signal —
    the events themselves look identical.
    """
    return "subagent" if str(native_session_id).startswith("agent-") else "human-facing"


# Machine-authored turn signatures. A ``type='user'`` event is NOT the same as
# "a human typed this": the harness posts orchestrator briefs, retry blockers,
# and inter-agent messages down the same channel. Measured on the live corpus,
# treating all of them as human produced garbage — "shouted: MECHANISM
# CARDINALITY HARD TEST" scored as user frustration when it was an agent's own
# emphatic brief, and role-assignment templates scored as corrections.
#
# Authorship has to be settled BEFORE detection, not compensated for inside
# each detector; otherwise every new detector re-inherits the same false
# positives.
_MACHINE_AUTHORED: tuple[re.Pattern, ...] = (
    # Inter-agent messages relayed through the user channel.
    re.compile(r"<teammate-message\b", re.IGNORECASE),
    re.compile(r"^\s*Another Claude session sent a message", re.IGNORECASE),
    re.compile(r"<task-notification\b", re.IGNORECASE),
    # Orchestrator role dispatch.
    re.compile(r"\*\*Your assigned role:\*\*", re.IGNORECASE),
    re.compile(r"^\s*##\s*Role Identity", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bcall\s+`roles_get\(", re.IGNORECASE),
    # Orchestrator retry / blocker injections.
    re.compile(r"^\s*#\s*⛔\s*BLOCKER", re.IGNORECASE | re.MULTILINE),
    re.compile(r"your previous turn produced NO okuro-brain artifact", re.IGNORECASE),
    re.compile(r"\bStream-B artifact\b", re.IGNORECASE),
    re.compile(r"^\s*##\s*(?:Subtask|Task) (?:brief|context)\b", re.IGNORECASE | re.MULTILINE),
    # Harness control markers, not prose.
    re.compile(r"^\s*\[Request interrupted by user", re.IGNORECASE),
    re.compile(r"^\s*\[The user (?:sent|has)\b", re.IGNORECASE),
    # okuro's own protocol preamble echoed into a turn.
    re.compile(r"read\s+~/\.okuro/TOOL-PROTOCOL\.md", re.IGNORECASE),
    # okuro's bootstrap packet, echoed back into an input turn.
    re.compile(r"^\s*#\s*Agent Context\s*—\s*Okuro", re.IGNORECASE | re.MULTILINE),
    re.compile(r"##\s*Bootstrap greeting", re.IGNORECASE),
    re.compile(r"\bOKURO 3\.0\b"),
    re.compile(r"\bis bootstrapped\b", re.IGNORECASE),
    re.compile(r"##\s*Behavioral Contract", re.IGNORECASE),
    # okuro's internal role prompts (WORKFORCE-REVIEWER, suggestion writers,
    # judges). Shape: a SHOUTED-SLUG H1 immediately followed by **Purpose:**.
    re.compile(r"^\s*#\s*[A-Z][A-Z0-9-]{4,}\s*\n+\s*\*\*Purpose:\*\*",
               re.MULTILINE),
    re.compile(r"\*\*Constraints:\*\*\s*Output must be", re.IGNORECASE),
    re.compile(r"^\s*##\s*AGENT_HEADER", re.IGNORECASE | re.MULTILINE),
)

# Session locations that are machine-driven by construction. The orchestrator
# runs agents with its own working directory; no human sits in those sessions,
# whatever their turns look like.
_MACHINE_PATH_RE = re.compile(r"/\.okuro/(?:orchestrator|workforce)\b")


def turn_author(text: str, native_session_id: str,
                project_path: str | None = None) -> str:
    """Classify who actually wrote an input-side turn.

    Returns ``'human'``, ``'orchestrator'`` (a brief, blocker, or relayed
    agent message), or ``'harness'`` (a control marker, not prose).

    Every subagent session's opening turn is by definition a brief, so those
    are ``orchestrator`` regardless of shape — that is the population
    :mod:`.detect` audits for brief quality, not for human friction.
    """
    if not text:
        return "harness"

    head = text[:400]
    if re.match(r"^\s*\[(?:Request interrupted|The user)", head, re.IGNORECASE):
        return "harness"

    # Structural, and it outranks every textual signal: a subagent transcript
    # has no human in it at all. Whatever arrives on its input channel was
    # written by the parent that spawned it, however conversational it reads.
    if session_kind(native_session_id) == "subagent":
        return "orchestrator"

    # Same reasoning, by location: the orchestrator runs agents out of its own
    # working directory, so nothing originating there was typed by a human.
    if project_path and _MACHINE_PATH_RE.search(project_path):
        return "orchestrator"

    for pattern in _MACHINE_AUTHORED:
        if pattern.search(text):
            return "orchestrator"

    return "human"


def iter_turns(
    since_days: int | None = None,
    kind: str | None = None,
    session_ids: list[str] | None = None,
    min_chars: int = 1,
) -> list[dict]:
    """Return cleaned input-side turns across the trace store.

    Args:
        since_days: Only turns newer than this. None = all history.
        kind: ``'human-facing'`` or ``'subagent'``. None = both.
        session_ids: Restrict to these native session ids.
        min_chars: Drop turns shorter than this after cleaning.

    Returns:
        Dicts with ``event_uuid``, ``native_session_id``, ``session_kind``,
        ``ord``, ``turn_index``, ``ts``, ``text`` (cleaned), ``raw_len``.
    """
    from okuro.db import get_db

    db = get_db()

    where = ["e.type = 'user'", "e.text IS NOT NULL", "e.text != ''"]
    params: list = []

    if since_days is not None:
        where.append("e.timestamp >= date('now', ?)")
        params.append(f"-{int(since_days)} days")

    if kind == "subagent":
        where.append("e.session_id LIKE 'agent-%'")
    elif kind == "human-facing":
        where.append("e.session_id NOT LIKE 'agent-%'")

    if session_ids:
        placeholders = ",".join("?" * len(session_ids))
        where.append(f"e.session_id IN ({placeholders})")
        params.extend(session_ids)

    rows = db.fetchall(
        f"""
        SELECT e.uuid, e.session_id, e.ord, e.timestamp, e.text,
               a.project_path
        FROM agent_events e
        LEFT JOIN agent_sessions a ON a.session_id = e.session_id
        WHERE {' AND '.join(where)}
        ORDER BY e.session_id, e.ord
        """,
        tuple(params),
    )

    out: list[dict] = []
    turn_index: dict[str, int] = {}
    for r in rows:
        cleaned = clean_turn_text(r["text"])
        if len(cleaned) < min_chars:
            continue
        sid = r["session_id"]
        idx = turn_index.get(sid, 0)
        turn_index[sid] = idx + 1
        out.append(
            {
                "event_uuid": r["uuid"],
                "native_session_id": sid,
                "session_kind": session_kind(sid),
                "author": turn_author(cleaned, sid, r["project_path"]),
                "ord": r["ord"],
                "turn_index": idx,
                "ts": r["timestamp"],
                "text": cleaned,
                "raw_len": len(r["text"] or ""),
            }
        )
    return out


def authorship_report(since_days: int | None = None) -> dict:
    """Count input-side turns by author class and session kind.

    Exists because the split is not obvious and gets forgotten: a large share
    of ``type='user'`` rows were never written by a human, and any analysis
    that assumes otherwise reports agent boilerplate as user behaviour.
    """
    turns = iter_turns(since_days=since_days)
    counts: dict[str, dict[str, int]] = {}
    for t in turns:
        bucket = counts.setdefault(t["session_kind"], {})
        bucket[t["author"]] = bucket.get(t["author"], 0) + 1
    return {"total": len(turns), "by_session_kind": counts}


def iter_session_turns(native_session_id: str) -> list[dict]:
    """Cleaned input-side turns for one session, oldest first."""
    return iter_turns(session_ids=[native_session_id])


def embed_turns(since_days: int | None = None, batch_size: int = 64,
                limit: int | None = None) -> dict:
    """Embed input-side turns into ``vec_interaction_turns``.

    Idempotent: turns already present in the vec table are skipped, so this is
    safe to run on a schedule and safe to resume after an interrupt.

    Only turns at least :data:`MIN_EMBED_CHARS` long are embedded — shorter
    ones ("ok", "go ahead") cluster into noise without carrying a problem.
    """
    from okuro.db import get_db

    db = get_db()

    try:
        from okuro.embed.client import embed as embed_batch
        from okuro.embed.client import to_bytes
    except Exception as exc:  # pragma: no cover - embedding optional
        return {"error": f"embeddings unavailable: {exc}", "embedded": 0}

    existing = {
        r["id"]
        for r in db.fetchall("SELECT id FROM vec_interaction_turns")
    }

    turns = [
        t
        for t in iter_turns(since_days=since_days, min_chars=MIN_EMBED_CHARS)
        if t["event_uuid"] not in existing
    ]
    if limit is not None:
        turns = turns[:limit]

    if not turns:
        return {"embedded": 0, "skipped": len(existing), "pending": 0}

    embedded = 0
    failed = 0
    for i in range(0, len(turns), batch_size):
        batch = turns[i : i + batch_size]
        texts = [t["text"][:EMBED_CHAR_CAP] for t in batch]
        try:
            vectors = embed_batch(texts)
        except Exception as exc:
            log.warning("embed_turns: batch %d failed (%s)", i // batch_size, exc)
            failed += len(batch)
            continue
        with db.write():
            for turn, vec in zip(batch, vectors):
                try:
                    db.execute(
                        "INSERT INTO vec_interaction_turns (id, embedding) VALUES (?, ?)",
                        (turn["event_uuid"], to_bytes(vec)),
                    )
                    embedded += 1
                except Exception:
                    # A vector failure must never block the pipeline; the turn
                    # stays searchable via FTS on agent_events.
                    failed += 1

    return {
        "embedded": embedded,
        "failed": failed,
        "already_present": len(existing),
        "candidates": len(turns),
    }


def turn_search(query: str, kind: str | None = None, limit: int = 20) -> list[dict]:
    """Semantic search over embedded input-side turns.

    This is the capability ``transcript_search`` promised but never had — it
    runs against the real corpus rather than the 40-row test table.
    """
    from okuro.db import get_db

    db = get_db()

    try:
        from okuro.embed.client import embed_query, to_bytes

        matches = db.vec_search(
            "vec_interaction_turns", to_bytes(embed_query(query)), limit=limit * 4
        )
    except Exception as exc:
        log.warning("turn_search: vector path unavailable (%s)", exc)
        return []

    if not matches:
        return []

    distances = {m["id"]: m["distance"] for m in matches}
    ids = list(distances)
    placeholders = ",".join("?" * len(ids))
    rows = db.fetchall(
        f"""
        SELECT e.uuid, e.session_id, e.ord, e.timestamp, e.text,
               a.project_path, a.provider
        FROM agent_events e
        LEFT JOIN agent_sessions a ON a.session_id = e.session_id
        WHERE e.uuid IN ({placeholders})
        """,
        tuple(ids),
    )

    out = []
    for r in rows:
        sid = r["session_id"]
        if kind and session_kind(sid) != kind:
            continue
        out.append(
            {
                "event_uuid": r["uuid"],
                "native_session_id": sid,
                "session_kind": session_kind(sid),
                "ts": r["timestamp"],
                "provider": r["provider"],
                "project_path": r["project_path"],
                "text": clean_turn_text(r["text"])[:600],
                "similarity": 1.0 - distances.get(r["uuid"], 1.0),
            }
        )
    out.sort(key=lambda r: r["similarity"], reverse=True)
    return out[:limit]
