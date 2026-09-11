# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: okuro·prism sparring SESSIONS — the stateful multi-turn adversarial
#   loop. Everything else in prism is one-shot; this is the partner that
#   REMEMBERS. Turns accumulate within a session (challenge → rebut/concede,
#   assumption → hold/falsify, option killed, decision recorded, tripwire set);
#   durable facts flow to the temporal KG so a LATER session on the same topic
#   opens by recalling what was assumed, decided, killed — the moat no
#   single-model, stateless tool can hold.
# index:
#   imports
#   constants
#   class SparringSession
#   def start_session
#   def advance
#   def session_state
#   storage (get/list/save/delete/events)
#   kg wiring (_recall_turn / _assert_from_turn)
# AGENT_HEADER_END -->
"""Stateful multi-turn sparring — the prism sparring partner's memory.

A ``SparringSession`` is a topic/decision contested over many turns and stored
as one JSON blob (``turns[]`` + meta), mirroring ``okuro.prism.storage``:
SQLite-backed, last-write-wins, with an append-only change-feed
(``sparring_events``) so an open view live-updates.

Two layers sit on top of storage:

* **Turn loop** (:func:`advance`) — a *move* (challenge, run the panel, surface
  an assumption, concede/rebut/falsify/kill, record a decision, set a tripwire)
  appends one or more turns and, where the move resolves a durable fact, writes
  it to the temporal KG.
* **KG wiring** — the cross-session memory. On start, prior KG triples for this
  topic surface as an opening *recall* turn; on decision/assumption/tripwire
  moves, the fact is asserted (and invalidated when an assumption is falsified),
  so the timeline of "assumed X → didn't hold" survives across sessions.

Pure-ish: the turn model is deterministic given its inputs; the only side
effects are storage writes, the optional bridge call behind a ``panel`` move,
and KG mutations. Degrades gracefully — a KG or bridge fault never sinks a turn.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from okuro.db import get_db
from okuro.prism.storage import slugify

logger = logging.getLogger("okuro.prism.sparring")

_EVENTS_KEEP = 500

# A turn's kind — what happened on this turn.
TURN_KINDS = ("recall", "challenge", "assumption", "decision", "tripwire", "note")

# A turn's verdict — its standing in the argument. Challenges resolve held
# (rebutted, your position stands) or conceded (the objection won); assumptions
# resolve held (stood up) or falsified (didn't hold); an option/challenge can be
# killed. ``open`` = unresolved (the default, and the only state that "counts"
# as live pressure in the derived session state).
VERDICTS = ("open", "held", "conceded", "killed", "falsified")

# Moves an actor can make. ``panel`` fans out to the multi-model red-team;
# ``verdict`` resolves a prior turn (and may write to the KG).
MOVE_TYPES = ("challenge", "panel", "assumption", "decision", "tripwire", "note", "verdict")


def _now() -> str:
    """UTC timestamp in SQLite ``datetime('now')`` format (turns are stamped in
    code, not by SQL, since they live inside the JSON blob)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class SparringSession:
    """One stateful sparring session. ``turns`` is the append-only turn log."""

    id: str
    topic: str
    topic_key: str = ""
    person_id: str | None = None
    brand_id: str | None = None
    status: str = "open"
    turns: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "topic": self.topic,
            "person_id": self.person_id,
            "brand_id": self.brand_id,
            "status": self.status,
            "turn_count": len(self.turns),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_detail(self) -> dict:
        return {**self.to_summary(), "topic_key": self.topic_key, "turns": self.turns}

    def to_state(self) -> dict:
        """Summary + the derived live board (open pressure, standing facts)."""
        return {**self.to_summary(), "state": session_state(self.turns)}


# ---------------------------------------------------------------------------
# Turn loop
# ---------------------------------------------------------------------------


def _append_turn(
    turns: list[dict[str, Any]],
    kind: str,
    content: str,
    *,
    verdict: str = "open",
    source: str = "user",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and append one turn; return it. ``n`` is 1-based and monotonic."""
    turn = {
        "n": len(turns) + 1,
        "kind": kind if kind in TURN_KINDS else "note",
        "content": (content or "").strip(),
        "verdict": verdict if verdict in VERDICTS else "open",
        "source": source or "user",
        "meta": meta or {},
        "ts": _now(),
    }
    turns.append(turn)
    return turn


def start_session(
    topic: str,
    *,
    person_id: str | None = None,
    brand_id: str | None = None,
    origin: str = "agent",
) -> SparringSession:
    """Open a new session on ``topic``. If the temporal KG already holds facts
    for this topic (from prior sessions), the session opens with a ``recall``
    turn surfacing them — the cross-session memory moat."""
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic required")
    topic_key = slugify(topic)

    turns: list[dict[str, Any]] = []
    recall = _recall_turn(topic_key)
    if recall is not None:
        turns.append(recall)

    session = SparringSession(
        id="",  # storage assigns a unique slug
        topic=topic,
        topic_key=topic_key,
        person_id=person_id or None,
        brand_id=brand_id or None,
        status="open",
        turns=turns,
    )
    return _save_session(session, origin=origin)


def advance(session_id: str, move: dict[str, Any], *, origin: str = "agent") -> SparringSession:
    """Apply one ``move`` to a session, persist, and return the updated session.

    Moves (``move["type"]``):
      * ``challenge``   — ``{content, source?}`` add an objection (verdict open).
      * ``panel``       — ``{audience_hint?, max_panel?}`` run the multi-model
                          red-team; each objection lands as a challenge turn.
      * ``assumption``  — ``{content}`` surface a load-bearing assumption; also
                          asserted to the KG so a later session can recall it.
      * ``decision``    — ``{content}`` record a decision (asserted to the KG).
      * ``tripwire``    — ``{content, trigger?}`` set a condition to revisit
                          (asserted to the KG).
      * ``note``        — ``{content}`` free annotation.
      * ``verdict``     — ``{ref, verdict}`` resolve turn #``ref`` (held /
                          conceded / killed / falsified). Falsifying an
                          assumption invalidates its KG triple.
    """
    session = get_session(session_id)
    if session is None:
        raise ValueError(f"sparring session '{session_id}' not found")
    if session.status != "open":
        raise ValueError(f"session '{session_id}' is closed")

    mtype = (move or {}).get("type")
    if mtype not in MOVE_TYPES:
        raise ValueError(f"unknown move type '{mtype}' (expected one of {MOVE_TYPES})")

    content = str(move.get("content") or "").strip()

    if mtype == "challenge":
        if not content:
            raise ValueError("challenge move requires content")
        _append_turn(session.turns, "challenge", content, source=str(move.get("source") or "user"))

    elif mtype == "panel":
        _apply_panel(session, move)

    elif mtype == "assumption":
        if not content:
            raise ValueError("assumption move requires content")
        turn = _append_turn(session.turns, "assumption", content, source="user")
        _assert_from_turn(session, turn)

    elif mtype == "decision":
        if not content:
            raise ValueError("decision move requires content")
        turn = _append_turn(session.turns, "decision", content, verdict="held", source="user")
        _assert_from_turn(session, turn)

    elif mtype == "tripwire":
        if not content:
            raise ValueError("tripwire move requires content")
        meta = {"trigger": str(move.get("trigger") or "").strip()} if move.get("trigger") else {}
        turn = _append_turn(session.turns, "tripwire", content, source="user", meta=meta)
        _assert_from_turn(session, turn)

    elif mtype == "note":
        if not content:
            raise ValueError("note move requires content")
        _append_turn(session.turns, "note", content, source=str(move.get("source") or "user"))

    elif mtype == "verdict":
        _apply_verdict(session, move)

    return _save_session(session, origin=origin)


def _apply_panel(session: SparringSession, move: dict[str, Any]) -> None:
    """Run the multi-model red-team over the session's live position; land each
    objection as its own challenge turn tagged with the model that raised it."""
    from okuro.prism.redteam import red_team

    position = _position_text(session)
    hint = str(move.get("audience_hint") or "").strip()
    if not hint and session.person_id:
        hint = f"the recipient {session.person_id}"

    block = red_team(
        position,
        audience_hint=hint,
        max_panel=int(move.get("max_panel") or 3),
    )
    challenges = block.get("challenges") or []
    if not challenges:
        _append_turn(
            session.turns, "note",
            "Ran the adversary panel — no objections returned (no provider reachable).",
            source="panel",
        )
        return
    for ch in challenges:
        _append_turn(
            session.turns, "challenge", ch.get("objection") or "",
            source=ch.get("who") or "panel",
            meta={
                "severity": ch.get("severity") or "med",
                "rebuttal": ch.get("rebuttal"),
                "via": "panel",
            },
        )


def _apply_verdict(session: SparringSession, move: dict[str, Any]) -> None:
    """Resolve a prior turn. On falsifying an assumption, invalidate its KG
    triple so the cross-session timeline shows it rose and fell."""
    try:
        ref = int(move.get("ref"))
    except (TypeError, ValueError):
        raise ValueError("verdict move requires an integer 'ref' (the turn number)")
    verdict = str(move.get("verdict") or "").strip()
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")

    turn = next((t for t in session.turns if t.get("n") == ref), None)
    if turn is None:
        raise ValueError(f"no turn #{ref} in this session")
    turn["verdict"] = verdict
    turn.setdefault("meta", {})["resolved_ts"] = _now()

    if turn.get("kind") == "assumption" and verdict == "falsified":
        _invalidate_from_turn(session, turn)


def _position_text(session: SparringSession) -> str:
    """The live position the panel attacks: the topic plus every standing
    (open/held) claim, so the adversary contests the current state, not just the
    opening statement."""
    lines = [f"TOPIC: {session.topic}"]
    for t in session.turns:
        if t.get("kind") in ("assumption", "decision") and t.get("verdict") in ("open", "held"):
            lines.append(f"- {t['kind']}: {t.get('content', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Derived state — the live sparring board
# ---------------------------------------------------------------------------


def session_state(turns: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold the turn log into the current board: what is still live pressure vs.
    what has been settled. Pure — derives entirely from ``turns``."""
    open_challenges: list[dict] = []
    standing_assumptions: list[dict] = []
    falsified: list[dict] = []
    killed: list[dict] = []
    decisions: list[dict] = []
    tripwires: list[dict] = []

    for t in turns:
        kind, verdict = t.get("kind"), t.get("verdict")
        if kind == "challenge" and verdict == "open":
            open_challenges.append(t)
        elif kind == "assumption":
            if verdict in ("open", "held"):
                standing_assumptions.append(t)
            elif verdict == "falsified":
                falsified.append(t)
        if verdict == "killed":
            killed.append(t)
        if kind == "decision":
            decisions.append(t)
        if kind == "tripwire":
            tripwires.append(t)

    return {
        "open_challenges": open_challenges,
        "standing_assumptions": standing_assumptions,
        "falsified_assumptions": falsified,
        "killed": killed,
        "decisions": decisions,
        "tripwires": tripwires,
        "counts": {
            "open_challenges": len(open_challenges),
            "standing_assumptions": len(standing_assumptions),
            "falsified": len(falsified),
            "decisions": len(decisions),
            "tripwires": len(tripwires),
        },
    }


# ---------------------------------------------------------------------------
# KG wiring — the cross-session memory moat
# ---------------------------------------------------------------------------

_KG_PRED = {"assumption": "assumed", "decision": "decided", "tripwire": "tripwire"}


def _entity(topic_key: str) -> str:
    return f"sparring/{topic_key}"


def _assert_from_turn(session: SparringSession, turn: dict[str, Any]) -> None:
    """Persist a durable turn (assumption/decision/tripwire) to the temporal KG,
    keyed on the topic entity so future sessions on the same topic recall it."""
    pred = _KG_PRED.get(turn.get("kind", ""))
    if not pred or not turn.get("content"):
        return
    try:
        from okuro.sense.kg import kg_add

        kg_add(
            subject=_entity(session.topic_key),
            predicate=pred,
            object=turn["content"],
            subject_type="topic",
            object_type="claim",
            project="okuro",
            confidence=0.6,
        )
    except Exception as exc:  # noqa: BLE001 — memory is best-effort; never sink a turn
        logger.info("kg assert skipped (%s): %s", turn.get("kind"), exc)


def _invalidate_from_turn(session: SparringSession, turn: dict[str, Any]) -> None:
    """Close an assumption's KG triple — the timeline now shows it stopped being
    true, so a later session reads 'assumed X … did not hold'."""
    if not turn.get("content"):
        return
    try:
        from okuro.sense.kg import kg_invalidate

        kg_invalidate(
            subject=_entity(session.topic_key),
            predicate="assumed",
            object=turn["content"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("kg invalidate skipped: %s", exc)


def _recall_turn(topic_key: str) -> dict[str, Any] | None:
    """Query the KG for prior facts on this topic; build the opening recall turn.

    Returns ``None`` when the topic is fresh (no prior sessions left a trace).
    Falsified assumptions are surfaced explicitly — recalling a claim that
    *didn't hold* is the sharpest thing a sparring partner can open with.
    """
    try:
        from okuro.sense.kg import kg_query

        rows = kg_query(_entity(topic_key), direction="outgoing", project="okuro", limit=50)
    except Exception as exc:  # noqa: BLE001
        logger.info("kg recall skipped: %s", exc)
        return None
    if not rows:
        return None

    items: list[dict[str, Any]] = []
    for r in rows:
        pred, obj = r.get("predicate"), r.get("object")
        closed = bool(r.get("valid_to"))
        if pred == "assumed":
            label = "assumed (did NOT hold)" if closed else "assumed"
        elif pred == "decided":
            label = "decided"
        elif pred == "tripwire":
            label = "tripwire"
        else:
            continue
        items.append({"kind": pred, "label": label, "content": obj, "closed": closed})

    if not items:
        return None

    def _line(it: dict) -> str:
        return f"• {it['label']}: {it['content']}"

    body = "Recalled from prior sessions on this topic:\n" + "\n".join(_line(it) for it in items)
    return {
        "n": 1,
        "kind": "recall",
        "content": body,
        "verdict": "open",
        "source": "memory",
        "meta": {"items": items},
        "ts": _now(),
    }


# ---------------------------------------------------------------------------
# Storage — SQLite CRUD + change-feed (mirrors okuro.prism.storage)
# ---------------------------------------------------------------------------


def _row_to_session(row: dict) -> SparringSession:
    try:
        state = json.loads(row["state"]) if row.get("state") else {}
    except (ValueError, TypeError):
        state = {}
    turns = state.get("turns") if isinstance(state.get("turns"), list) else []
    return SparringSession(
        id=row["id"],
        topic=row["topic"],
        topic_key=row.get("topic_key") or "",
        person_id=row.get("person_id"),
        brand_id=row.get("brand_id"),
        status=row.get("status") or "open",
        turns=turns,
        created_at=row.get("created_at") or "",
        updated_at=row.get("updated_at") or "",
    )


def _unique_id(db, base: str) -> str:
    candidate = base
    n = 1
    while db.fetchone("SELECT id FROM sparring_sessions WHERE id = ?", (candidate,)) is not None:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def get_session(session_id: str) -> SparringSession | None:
    db = get_db()
    row = db.fetchone("SELECT * FROM sparring_sessions WHERE id = ?", (session_id,))
    return _row_to_session(row) if row else None


def list_sessions(topic_key: str | None = None) -> list[SparringSession]:
    db = get_db()
    if topic_key:
        rows = db.fetchall(
            "SELECT id, topic, topic_key, person_id, brand_id, '' AS state, "
            "turn_count, status, created_at, updated_at FROM sparring_sessions "
            "WHERE topic_key = ? ORDER BY updated_at DESC",
            (topic_key,),
        )
    else:
        rows = db.fetchall(
            "SELECT id, topic, topic_key, person_id, brand_id, '' AS state, "
            "turn_count, status, created_at, updated_at FROM sparring_sessions "
            "ORDER BY updated_at DESC"
        )
    return [_row_to_session(r) for r in rows]


def _emit(conn, session_id: str, kind: str, origin: str) -> None:
    conn.execute(
        "INSERT INTO sparring_events (session_id, kind, origin) VALUES (?, ?, ?)",
        (session_id, kind, origin or ""),
    )
    conn.execute(
        "DELETE FROM sparring_events WHERE seq <= (SELECT MAX(seq) FROM sparring_events) - ?",
        (_EVENTS_KEEP,),
    )


def _save_session(session: SparringSession, *, origin: str = "") -> SparringSession:
    """Upsert a session (assigning a unique slug id on first save). Emits a
    ``saved`` change-feed event. Returns the reloaded session."""
    topic = (session.topic or "").strip()
    if not topic:
        raise ValueError("topic required")
    turns = session.turns if isinstance(session.turns, list) else []
    state_json = json.dumps({"turns": turns}, separators=(",", ":"))

    db = get_db()
    with db.write() as conn:
        existing_id = (session.id or "").strip() or None
        existing = None
        if existing_id:
            existing = conn.execute(
                "SELECT id FROM sparring_sessions WHERE id = ?", (existing_id,)
            ).fetchone()

        if existing_id and existing:
            session_id = existing_id
            conn.execute(
                "UPDATE sparring_sessions SET topic = ?, topic_key = ?, person_id = ?, "
                "brand_id = ?, state = ?, turn_count = ?, status = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (topic, session.topic_key, session.person_id, session.brand_id,
                 state_json, len(turns), session.status, session_id),
            )
        else:
            session_id = _unique_id(db, slugify(existing_id or topic))
            conn.execute(
                "INSERT INTO sparring_sessions "
                "(id, topic, topic_key, person_id, brand_id, state, turn_count, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, topic, session.topic_key, session.person_id, session.brand_id,
                 state_json, len(turns), session.status),
            )
        _emit(conn, session_id, "saved", origin)

    saved = get_session(session_id)
    assert saved is not None
    return saved


def close_session(session_id: str, *, origin: str = "agent") -> SparringSession | None:
    session = get_session(session_id)
    if session is None:
        return None
    session.status = "closed"
    return _save_session(session, origin=origin)


def delete_session(session_id: str, *, origin: str = "") -> bool:
    db = get_db()
    with db.write() as conn:
        if conn.execute("SELECT id FROM sparring_sessions WHERE id = ?", (session_id,)).fetchone() is None:
            return False
        conn.execute("DELETE FROM sparring_sessions WHERE id = ?", (session_id,))
        _emit(conn, session_id, "deleted", origin)
    return True


def events_since(seq: int) -> list[dict]:
    db = get_db()
    return db.fetchall(
        "SELECT seq, session_id, kind, origin, ts FROM sparring_events "
        "WHERE seq > ? ORDER BY seq ASC",
        (int(seq),),
    )


def latest_seq() -> int:
    db = get_db()
    row = db.fetchone("SELECT MAX(seq) AS s FROM sparring_events")
    return int(row["s"]) if row and row.get("s") is not None else 0


__all__ = [
    "SparringSession",
    "start_session",
    "advance",
    "session_state",
    "get_session",
    "list_sessions",
    "close_session",
    "delete_session",
    "events_since",
    "latest_seq",
    "TURN_KINDS",
    "VERDICTS",
    "MOVE_TYPES",
]
