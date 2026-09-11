### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Deterministic friction/accuracy detectors over input-side turns; writes interaction_markers.
# index: imports | DETECTOR_VERSION | class Detector | turn detectors | session detectors | DETECTORS | scan_session | scan_sessions | marker_summary
# AGENT_HEADER_END -->
"""Deterministic detectors for interaction problems.

Detection is split deliberately from interpretation. This module answers only
*"did this observable thing occur, and where"* — cheap, reproducible, no model
call, re-runnable over the whole corpus. :mod:`.analyze` then reasons over the
markers to name patterns worth acting on.

Keeping the split means a detector change never silently rewrites history: bump
:data:`DETECTOR_VERSION` and rescan, and every marker is reproducible from
``agent_events`` alone.

Two families
------------
``human-facing`` sessions are scanned for signs the human is fighting the
agent. ``subagent`` sessions are scanned for signs the agent was briefed badly
or returned badly. A detector declares which population it applies to; running
a frustration detector over a parent agent's brief would be noise.

Authorship first
----------------
A ``type='user'`` event is not the same as "a human typed this". The harness
posts orchestrator briefs, retry blockers, and relayed inter-agent messages
down the same channel, inside otherwise human-facing sessions. Every detector
declares which authorship classes it reads (:attr:`Detector.authors`), and
:func:`okuro.sense.interaction.turns.turn_author` settles the question once,
before detection. Compensating per-detector instead would make every future
detector re-inherit the same false positives.

Evidence discipline
-------------------
Every marker stores the triggering span *with surrounding context*, so a
reader can judge the hit. A marker with no quotable evidence is not a finding,
it is an assertion — and the analysis stage is instructed to ignore any
pattern it cannot ground in one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Bump when detector semantics change; `interaction_scanned` compares against
# it to decide what needs a rescan.
DETECTOR_VERSION = 3

_EVIDENCE_CAP = 240


@dataclass(frozen=True)
class Detector:
    """One observable signal.

    Attributes:
        id: snake_case marker id, stored in ``interaction_markers.marker``.
        applies_to: ``'human-facing'``, ``'subagent'``, or ``'both'``.
        scope: ``'turn'`` (per input turn) or ``'session'`` (whole session).
        weight: Relative severity; the analysis stage ranks on the sum.
        describe: One line explaining what the marker means, fed to the LLM
            so it interprets markers rather than guessing from the name.
        authors: Which turn-authorship classes this detector may read. A
            ``type='user'`` event is not necessarily human — the harness posts
            orchestrator briefs and relayed agent messages down the same
            channel — so every detector must declare what it is reading.
        fn: For turn scope ``fn(turn, ctx) -> evidence|None``. For session
            scope ``fn(turns, ctx) -> evidence|None``.
    """

    id: str
    applies_to: str
    scope: str
    weight: float
    describe: str
    fn: Callable
    authors: tuple[str, ...] = ("human",)


def _ev(text: str) -> str:
    """Normalize a matched span into storable evidence."""
    return re.sub(r"\s+", " ", text).strip()[:_EVIDENCE_CAP]


def _ev_around(text: str, match: re.Match, before: int = 60, after: int = 160) -> str:
    """Evidence with surrounding context, so a reader can judge the hit.

    A bare span is unverifiable — an early version stored ``". no"`` and
    ``"AGAIN,"`` as evidence, which is indistinguishable between a genuine
    correction and the word appearing mid-sentence. The analysis stage is told
    to discard patterns it cannot ground in a quote; a quote that short cannot
    ground anything.
    """
    start = max(0, match.start() - before)
    end = min(len(text), match.end() + after)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return _ev(snippet)


# ---------------------------------------------------------------------------
# Turn detectors — human-facing
# ---------------------------------------------------------------------------

# Explicit contradiction of what the agent just did.
#
# A bare "no" was tried and removed: this user writes constraints in that shape
# constantly ("no push", "no assumptions", "No google spying allowed"), and
# those are instructions, not corrections. Measured, the bare alternative
# produced the large majority of this detector's false positives. A standalone
# turn-initial "no," survives, because opening a turn that way IS a correction.
#
# Two tiers. "no"/"nope" must open the turn, because mid-sentence they are
# almost always constraints. The rest are unambiguous enough to match anywhere
# — nobody writes "incorrect" or "you misunderstood" as an instruction.
_CORRECTION_RE = re.compile(
    r"(?:^\s*(?:no|nope)[,.!]"
    r"|\b(?:that'?s not\b|thats not\b|that'?s wrong\b|that is wrong\b"
    r"|is incorrect\b|are incorrect\b|incorrect\b"
    r"|not what i (?:asked|wanted|meant)\b|i didn'?t ask\b"
    r"|you misunderstood\b|you got (?:it|that) wrong\b"
    r"|don'?t do that\b))",
    re.IGNORECASE,
)

# The user restating an instruction they already gave. This is the expensive
# one — it means the protocol layer failed to carry a stated preference.
_RESTATED_RE = re.compile(
    r"\b(i (?:already |just )?(?:said|told you|asked)\b|"
    r"as i (?:said|mentioned|explained)\b|"
    r"again[,:]|"
    r"like i said\b|"
    r"i repeat\b|"
    r"for the (?:second|third|last) time\b)",
    re.IGNORECASE,
)

# Rejection of a delivered artifact rather than of a statement.
#
# "revert" alone matched agent status output quoting git revert anchors, so it
# now requires an object — a rejection names what is being thrown away.
_REJECTION_RE = re.compile(
    r"\b(redo (?:it|this|that)\b|do (?:it|this) again\b|start over\b|"
    r"revert (?:it|this|that|the (?:change|commit|fix))\b|"
    r"undo (?:it|this|that)\b|scrap (?:it|this|that)\b|"
    r"throw (?:it|this|that) away\b|"
    r"this is (?:not )?(?:broken|useless)\b)",
    re.IGNORECASE,
)

# The user supplying context okuro was supposed to have served. Named directly
# in the charter — "kill re-explanation" — and never measured until now.
_REEXPLAIN_RE = re.compile(
    r"\b(as i (?:told|explained to) you (?:before|earlier|last time)\b|"
    r"i (?:already )?explained (?:this|that)\b|"
    r"you (?:should|already) know (?:this|that)\b|"
    r"we (?:discussed|talked about) this\b|"
    r"remember[,:]? (?:i|we|the)\b|"
    r"(?:again|once more)[,:]? (?:the|my|our) (?:setup|config|convention|rule)\b)",
    re.IGNORECASE,
)


def _detect_correction(turn: dict, ctx: dict) -> Optional[str]:
    m = _CORRECTION_RE.search(turn["text"])
    return _ev_around(turn["text"], m) if m else None


def _detect_restated_directive(turn: dict, ctx: dict) -> Optional[str]:
    m = _RESTATED_RE.search(turn["text"])
    return _ev_around(turn["text"], m) if m else None


def _detect_rejection(turn: dict, ctx: dict) -> Optional[str]:
    m = _REJECTION_RE.search(turn["text"])
    return _ev_around(turn["text"], m) if m else None


def _detect_reexplanation(turn: dict, ctx: dict) -> Optional[str]:
    m = _REEXPLAIN_RE.search(turn["text"])
    return _ev_around(turn["text"], m) if m else None


def _detect_frustration(turn: dict, ctx: dict) -> Optional[str]:
    """Frustration per the user's own profile definition.

    The profile names the trigger explicitly: *caps lock, exclamation marks,
    frustrated tone (NOT swearing)*. Encoding that definition rather than a
    generic sentiment heuristic keeps the detector aligned with how this user
    actually signals frustration — and keeps swearing, which they use
    conversationally, from producing false hits.
    """
    text = turn["text"]

    # Shouted words: 4+ consecutive capitals, excluding known acronyms and
    # identifiers that are legitimately uppercase.
    shouted = [
        w
        for w in re.findall(r"\b[A-Z]{4,}\b", text)
        if w not in _CAPS_ALLOWLIST
    ]
    if len(shouted) >= 2:
        first = re.search(r"\b" + re.escape(shouted[0]) + r"\b", text)
        context = _ev_around(text, first) if first else _ev(text[:200])
        return _ev(f"shouted {len(shouted)} words ({' '.join(shouted[:4])}) in: {context}")

    # Emphatic punctuation.
    bangs = text.count("!")
    if bangs >= 3:
        return _ev(f"{bangs} exclamation marks")
    if re.search(r"[!?]{2,}", text):
        m = re.search(r".{0,60}[!?]{2,}", text)
        return _ev(m.group(0) if m else "repeated terminal punctuation")

    return None


# Uppercase tokens that are vocabulary, not shouting. Without this the detector
# fires on every infrastructure conversation this user has.
_CAPS_ALLOWLIST = {
    "JSON", "HTTP", "HTTPS", "HTML", "YAML", "TOML", "SQL", "SQLITE", "REST",
    "GRPC", "MCP", "LLM", "LLMS", "GPU", "CPU", "VRAM", "RAID", "SSD", "NVME",
    "LAN", "WLAN", "DNS", "TLS", "SSL", "API", "APIS", "CLI", "UUID", "CRUD",
    "OKURO", "CLAUDE", "CODEX", "GEMINI", "CURSOR", "DOCKER", "LINUX", "BASH",
    "TODO", "FIXME", "NOTE", "WARN", "INFO", "DEBUG", "ERROR", "NULL", "TRUE",
    "FALSE", "NEVER", "ALWAYS", "MUST", "SHOULD", "IMPORTANT", "CRITICAL",
    "READ", "WRITE", "ONLY", "PLEASE", "OKAY", "DONE", "PASS", "FAIL",
}


# ---------------------------------------------------------------------------
# Turn detectors — subagent
# ---------------------------------------------------------------------------

# ORCH-BRIEF-COMPLETE names what every subagent brief must carry: target repo
# absolute path, branch strategy, commit convention, close-out tools. A brief
# missing them under-specifies the job, and the principle exists because
# under-briefing measurably produced drive-by refactors.
_BRIEF_REQUIREMENTS = (
    ("repo_path", re.compile(r"(/home/\S+|absolute path|repo(?:sitory)? path)", re.I)),
    ("branch", re.compile(r"\b(branch|worktree|git checkout|feat/|fix/)\b", re.I)),
    ("closeout", re.compile(r"\b(write_memory|log_progress|session_report)\b", re.I)),
)


def _detect_brief_underspecified(turn: dict, ctx: dict) -> Optional[str]:
    # Only the opening brief is a brief; later turns are follow-ups.
    if turn["turn_index"] != 0:
        return None
    # Very short opening turns are dispatch stubs, not briefs.
    if len(turn["text"]) < 200:
        return None
    missing = [name for name, rx in _BRIEF_REQUIREMENTS if not rx.search(turn["text"])]
    if not missing:
        return None
    return _ev("brief omits: " + ", ".join(missing))


# ---------------------------------------------------------------------------
# Session detectors
# ---------------------------------------------------------------------------


def _detect_abandonment(turns: list[dict], ctx: dict) -> Optional[str]:
    """Session ends on a human turn that never drew a substantive response.

    ``ctx['last_ord']`` is the final event in the session. If the last input
    turn sits at or near the end, nothing followed it — the human asked and
    the session died.
    """
    if not turns:
        return None
    last_turn = turns[-1]
    last_ord = ctx.get("last_ord")
    if last_ord is None:
        return None
    trailing = last_ord - last_turn["ord"]
    if trailing > 2:
        return None
    # A closing pleasantry is not an abandonment.
    if len(last_turn["text"]) < 40:
        return None
    return _ev(f"session ended {trailing} events after: {last_turn['text'][:160]}")


def _detect_escalating_friction(turns: list[dict], ctx: dict) -> Optional[str]:
    """Three or more input turns in a session, each shorter than the last.

    A human whose turns keep contracting is losing patience — the first ask is
    a paragraph, the third is "no", the fourth is "just do it". Length decay
    catches that shape without needing sentiment.
    """
    if len(turns) < 3:
        return None
    lengths = [len(t["text"]) for t in turns]
    if lengths[0] < 120:
        return None
    decreasing = all(b < a for a, b in zip(lengths, lengths[1:]))
    if not decreasing:
        return None
    return _ev(f"turn lengths decayed {lengths[0]} → {lengths[-1]} over {len(lengths)} turns")


def _detect_no_closeout(turns: list[dict], ctx: dict) -> Optional[str]:
    """Subagent session that never called any close-out tool.

    The operating rules make write_memory / log_progress / session_report
    mandatory before finishing. A subagent that skips all three produced work
    nothing downstream can find.
    """
    tools = ctx.get("tool_names") or set()
    closeout = {"write_memory", "log_progress", "session_report", "write_role_handover",
                "artifact_write"}
    if tools & closeout:
        return None
    if len(tools) < 3:
        # Barely did anything — a stub run, not a close-out failure.
        return None
    return _ev(f"{len(tools)} tools called, none of: " + ", ".join(sorted(closeout)))


DETECTORS: tuple[Detector, ...] = (
    Detector(
        "correction", "human-facing", "turn", 1.0,
        "The human explicitly contradicted what the agent just did or said.",
        _detect_correction,
    ),
    Detector(
        "restated_directive", "human-facing", "turn", 2.0,
        "The human repeated an instruction they had already given — the "
        "protocol layer failed to carry a stated preference.",
        _detect_restated_directive,
    ),
    Detector(
        "rejection", "human-facing", "turn", 2.0,
        "The human rejected a delivered artifact and asked for a redo.",
        _detect_rejection,
    ),
    Detector(
        "re_explanation", "human-facing", "turn", 2.5,
        "The human re-supplied context okuro was supposed to have served. "
        "This is the failure the charter exists to eliminate.",
        _detect_reexplanation,
    ),
    Detector(
        "frustration", "human-facing", "turn", 1.5,
        "Shouting or emphatic punctuation, per the user's own stated "
        "frustration trigger (caps and exclamation marks, not swearing).",
        _detect_frustration,
    ),
    Detector(
        "abandonment", "human-facing", "session", 2.0,
        "The session ended on a substantive human turn that drew no response.",
        _detect_abandonment,
    ),
    Detector(
        "escalating_friction", "human-facing", "session", 1.5,
        "Successive human turns got steadily shorter — a patience-decay shape.",
        _detect_escalating_friction,
    ),
    Detector(
        "brief_underspecified", "subagent", "turn", 2.0,
        "The spawning brief omitted elements ORCH-BRIEF-COMPLETE requires "
        "(repo path, branch strategy, or close-out tools).",
        _detect_brief_underspecified,
        authors=("orchestrator",),
    ),
    Detector(
        "no_closeout", "subagent", "session", 1.5,
        "The subagent worked but called no close-out tool, so its output is "
        "unfindable downstream.",
        _detect_no_closeout,
        authors=("human", "orchestrator", "harness"),
    ),
)


def _applicable(detector: Detector, kind: str) -> bool:
    return detector.applies_to in ("both", kind)


def scan_session(native_session_id: str, turns: list[dict] | None = None) -> list[dict]:
    """Run every applicable detector over one session. Returns marker dicts."""
    from okuro.db import get_db
    from okuro.sense.interaction.turns import iter_session_turns, session_kind

    if turns is None:
        turns = iter_session_turns(native_session_id)
    if not turns:
        return []

    db = get_db()
    kind = session_kind(native_session_id)

    ctx_row = db.fetchone(
        "SELECT MAX(ord) AS last_ord FROM agent_events WHERE session_id = ?",
        (native_session_id,),
    )
    tool_rows = db.fetchall(
        "SELECT DISTINCT tool_name FROM agent_events "
        "WHERE session_id = ? AND tool_name IS NOT NULL",
        (native_session_id,),
    )
    ctx = {
        "last_ord": (ctx_row or {}).get("last_ord"),
        "tool_names": {r["tool_name"] for r in tool_rows},
        "session_kind": kind,
    }

    markers: list[dict] = []
    for detector in DETECTORS:
        if not _applicable(detector, kind):
            continue
        # Authorship gate. Without it, orchestrator briefs and relayed agent
        # messages score as human behaviour — measured on the live corpus,
        # that was the dominant source of false positives.
        eligible = [t for t in turns if t.get("author", "human") in detector.authors]
        if not eligible:
            continue
        if detector.scope == "turn":
            for turn in eligible:
                evidence = detector.fn(turn, ctx)
                if evidence:
                    markers.append(
                        {
                            "event_uuid": turn["event_uuid"],
                            "native_session_id": native_session_id,
                            "marker": detector.id,
                            "weight": detector.weight,
                            "evidence": evidence,
                            "turn_index": turn["turn_index"],
                            "ts": turn["ts"],
                        }
                    )
        else:
            evidence = detector.fn(eligible, ctx)
            if evidence:
                last = eligible[-1]
                markers.append(
                    {
                        "event_uuid": last["event_uuid"],
                        "native_session_id": native_session_id,
                        "marker": detector.id,
                        "weight": detector.weight,
                        "evidence": evidence,
                        "turn_index": last["turn_index"],
                        "ts": last["ts"],
                    }
                )
    return markers


def scan_sessions(since_days: int | None = None, limit: int | None = None,
                  rescan: bool = False) -> dict:
    """Scan unscanned sessions and persist their markers.

    Idempotent via ``interaction_scanned``. Marker absence cannot distinguish
    "clean" from "not yet looked at", so the scanned set is tracked explicitly.
    """
    from okuro.db import get_db
    from okuro.sense.interaction.turns import iter_turns

    db = get_db()

    where = ["1=1"]
    params: list = []
    if since_days is not None:
        where.append("a.last_ts >= date('now', ?)")
        params.append(f"-{int(since_days)} days")
    if not rescan:
        where.append(
            "NOT EXISTS (SELECT 1 FROM interaction_scanned s "
            " WHERE s.native_session_id = a.session_id "
            "   AND s.detector_version = ?)"
        )
        params.append(DETECTOR_VERSION)

    sql = f"""
        SELECT a.session_id
        FROM agent_sessions a
        WHERE {' AND '.join(where)}
        ORDER BY a.last_ts DESC
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    candidates = [r["session_id"] for r in db.fetchall(sql, tuple(params))]
    if not candidates:
        return {"sessions_scanned": 0, "markers_written": 0}

    # One bulk turn read beats N per-session queries over a 943k-row table.
    all_turns = iter_turns(session_ids=candidates)
    by_session: dict[str, list[dict]] = {}
    for turn in all_turns:
        by_session.setdefault(turn["native_session_id"], []).append(turn)

    written = 0
    scanned = 0
    for sid in candidates:
        turns = by_session.get(sid, [])
        markers = scan_session(sid, turns=turns) if turns else []
        with db.write():
            for m in markers:
                db.execute(
                    """
                    INSERT INTO interaction_markers
                        (event_uuid, native_session_id, marker, weight,
                         evidence, turn_index, ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_uuid, marker) DO UPDATE SET
                        weight   = excluded.weight,
                        evidence = excluded.evidence
                    """,
                    (
                        m["event_uuid"], m["native_session_id"], m["marker"],
                        m["weight"], m["evidence"], m["turn_index"], m["ts"],
                    ),
                )
                written += 1
            db.execute(
                """
                INSERT INTO interaction_scanned
                    (native_session_id, turns_scanned, markers_found,
                     detector_version, scanned_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(native_session_id) DO UPDATE SET
                    turns_scanned    = excluded.turns_scanned,
                    markers_found    = excluded.markers_found,
                    detector_version = excluded.detector_version,
                    scanned_at       = excluded.scanned_at
                """,
                (sid, len(turns), len(markers), DETECTOR_VERSION),
            )
        scanned += 1

    return {
        "sessions_scanned": scanned,
        "markers_written": written,
        "detector_version": DETECTOR_VERSION,
    }


def marker_summary(since_days: int | None = 30, kind: str | None = None) -> list[dict]:
    """Marker counts with session spread, most frequent first."""
    from okuro.db import get_db

    db = get_db()
    where = ["1=1"]
    params: list = []
    if since_days is not None:
        where.append("m.ts >= date('now', ?)")
        params.append(f"-{int(since_days)} days")
    if kind == "subagent":
        where.append("m.native_session_id LIKE 'agent-%'")
    elif kind == "human-facing":
        where.append("m.native_session_id NOT LIKE 'agent-%'")

    rows = db.fetchall(
        f"""
        SELECT m.marker,
               COUNT(*) AS hits,
               COUNT(DISTINCT m.native_session_id) AS sessions,
               SUM(m.weight) AS weight
        FROM interaction_markers m
        WHERE {' AND '.join(where)}
        GROUP BY m.marker
        ORDER BY weight DESC
        """,
        tuple(params),
    )
    return [dict(r) for r in rows]
