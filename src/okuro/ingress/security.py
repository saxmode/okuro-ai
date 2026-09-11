# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Challenge-response gate for high-stakes ingress dispatches.
#   Loads the per-channel challenge bank from the okuro keyring,
#   tracks pending actions awaiting an answer, enforces the
#   verification grace window + attempt counter + lockout.
# index: imports | constants | challenge bank | pending CRUD |
#   chat state | match | verification | lockout
# AGENT_HEADER_END -->
"""Challenge-response gate.

State machine for a single chat::

                ┌─────────────────────────┐
                │   high-stakes intent    │
                │       arrives           │
                └────────────┬────────────┘
                             │
            ┌────────────────┴────────────────┐
            │ chat verified within 15 min?    │── yes ──▶ dispatch
            └────────────────┬────────────────┘
                             │ no
            ┌────────────────┴────────────────┐
            │ chat in 5-min lockout?          │── yes ──▶ refuse
            └────────────────┬────────────────┘
                             │ no
                             ▼
              create pending_action(chat_id, intent, payload,
                                    challenge_idx, expires_in=120s)
              reply with challenge.question

Next message on that chat::

                ┌─────────────────────────┐
                │   incoming text         │
                └────────────┬────────────┘
                             │
            ┌────────────────┴────────────────┐
            │ pending action exists?          │── no ──▶ normal route
            └────────────────┬────────────────┘
                             │ yes
            ┌────────────────┴────────────────┐
            │ regex matches accepted_patterns?│
            └────────────────┬────────────────┘
                  ┌──────────┴──────────┐
                no                       yes
                  │                       │
       attempt++ (>=3 → lockout)   verified_until = now+15min
       drop pending OR keep        clear pending; dispatch payload
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("okuro.ingress.security")

VERIFY_GRACE_SECONDS = 15 * 60      # 15 minutes
PENDING_TTL_SECONDS = 120           # 2 minutes to answer challenge
ATTEMPT_WINDOW_SECONDS = 60         # rolling failure window
MAX_ATTEMPTS_IN_WINDOW = 3
LOCKOUT_SECONDS = 5 * 60            # 5 minutes

KEYRING_CHALLENGES_PREFIX = "integration/{channel}/challenges"


# ── Challenge bank ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Challenge:
    question: str
    accepted_patterns: list[str]        # regex strings, case-insensitive
    hint: Optional[str] = None          # optional plaintext hint for UI


def load_bank(channel: str, store=None) -> list[Challenge]:
    """Read challenges from keyring. Returns [] if absent/malformed.

    ``store`` is an optional already-unlocked ``KeyringStorage``. In the
    orchestrator API path the caller passes the per-session unlocked
    instance so we don't re-instantiate (which would fail outside a TTY
    when no Secret Service is available).
    """
    if store is None:
        from okuro.keyring.storage import KeyringStorage
        try:
            store = KeyringStorage()
        except Exception:
            log.warning("keyring unavailable for %s challenges", channel, exc_info=True)
            return []
    try:
        raw = store.get_key(KEYRING_CHALLENGES_PREFIX.format(channel=channel))
    except Exception:
        log.warning("keyring read failed for %s challenges", channel, exc_info=True)
        return []
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("malformed challenges JSON for %s", channel)
        return []
    out: list[Challenge] = []
    for it in items if isinstance(items, list) else []:
        q = (it.get("question") or "").strip()
        pats = [p.strip() for p in (it.get("accepted_patterns") or []) if isinstance(p, str) and p.strip()]
        if q and pats:
            out.append(Challenge(question=q, accepted_patterns=pats, hint=it.get("hint")))
    return out


def save_bank(channel: str, items: list[Challenge], store=None) -> None:
    """Persist challenges. Caller passes an unlocked ``KeyringStorage``
    when running outside a TTY (the orchestrator API path)."""
    if store is None:
        from okuro.keyring.storage import KeyringStorage
        store = KeyringStorage()
    payload = json.dumps([
        {"question": c.question, "accepted_patterns": c.accepted_patterns, "hint": c.hint}
        for c in items
    ])
    store.set_key(KEYRING_CHALLENGES_PREFIX.format(channel=channel), payload)


def _match(challenge: Challenge, answer: str) -> bool:
    text = answer.strip().lower()
    for pat in challenge.accepted_patterns:
        try:
            if re.search(pat, text, flags=re.IGNORECASE):
                return True
        except re.error:
            log.warning("invalid regex pattern in challenge: %r", pat)
            continue
    return False


# ── Pending action CRUD ──────────────────────────────────────────────


@dataclass
class PendingAction:
    channel: str
    chat_id: int
    intent: str
    payload: dict
    challenge_idx: Optional[int]
    attempts: int
    expires_at: datetime
    created_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def get_pending(channel: str, chat_id: int) -> Optional[PendingAction]:
    from okuro.db import get_db
    row = get_db().fetchone(
        "SELECT * FROM ingress_pending_actions WHERE channel=? AND chat_id=?",
        (channel, int(chat_id)),
    )
    if not row:
        return None
    expires = _parse_iso(row["expires_at"]) or _now()
    if expires < _now():
        clear_pending(channel, chat_id)
        return None
    return PendingAction(
        channel=row["channel"],
        chat_id=int(row["chat_id"]),
        intent=row["intent"],
        payload=json.loads(row["payload_json"] or "{}"),
        challenge_idx=row["challenge_idx"],
        attempts=int(row["attempts"] or 0),
        expires_at=expires,
        created_at=_parse_iso(row["created_at"]) or _now(),
    )


def set_pending(channel: str, chat_id: int, intent: str, payload: dict,
                challenge_idx: Optional[int]) -> None:
    from okuro.db import get_db
    db = get_db()
    expires = _now() + timedelta(seconds=PENDING_TTL_SECONDS)
    # Overwrite any previous pending — only one slot per chat.
    db.execute(
        "INSERT INTO ingress_pending_actions "
        "(channel, chat_id, intent, payload_json, challenge_idx, attempts, expires_at) "
        "VALUES (?, ?, ?, ?, ?, 0, ?) "
        "ON CONFLICT(channel, chat_id) DO UPDATE SET "
        "  intent=excluded.intent, payload_json=excluded.payload_json, "
        "  challenge_idx=excluded.challenge_idx, attempts=0, "
        "  expires_at=excluded.expires_at, created_at=datetime('now')",
        (channel, int(chat_id), intent, json.dumps(payload),
         challenge_idx, _iso(expires)),
    )


def clear_pending(channel: str, chat_id: int) -> None:
    from okuro.db import get_db
    get_db().execute(
        "DELETE FROM ingress_pending_actions WHERE channel=? AND chat_id=?",
        (channel, int(chat_id)),
    )


def increment_attempts(channel: str, chat_id: int) -> int:
    """Bump attempts on the pending row. Returns the new count."""
    from okuro.db import get_db
    db = get_db()
    db.execute(
        "UPDATE ingress_pending_actions SET attempts = attempts + 1 "
        "WHERE channel=? AND chat_id=?",
        (channel, int(chat_id)),
    )
    row = db.fetchone(
        "SELECT attempts FROM ingress_pending_actions WHERE channel=? AND chat_id=?",
        (channel, int(chat_id)),
    )
    return int((row or {}).get("attempts") or 0)


# ── Chat state (verification + lockout) ─────────────────────────────


@dataclass
class ChatState:
    channel: str
    chat_id: int
    verified_until: Optional[datetime]
    lockout_until: Optional[datetime]
    recent_failures: list[datetime] = field(default_factory=list)


def get_chat_state(channel: str, chat_id: int) -> ChatState:
    from okuro.db import get_db
    row = get_db().fetchone(
        "SELECT * FROM ingress_chat_state WHERE channel=? AND chat_id=?",
        (channel, int(chat_id)),
    )
    if not row:
        return ChatState(channel=channel, chat_id=int(chat_id),
                         verified_until=None, lockout_until=None)
    fails = []
    try:
        fails = [_parse_iso(s) for s in json.loads(row["recent_failures"] or "[]") if s]
        fails = [f for f in fails if f is not None]
    except Exception:
        fails = []
    return ChatState(
        channel=row["channel"],
        chat_id=int(row["chat_id"]),
        verified_until=_parse_iso(row["verified_until"]),
        lockout_until=_parse_iso(row["lockout_until"]),
        recent_failures=fails,
    )


def _save_chat_state(state: ChatState) -> None:
    from okuro.db import get_db
    get_db().execute(
        "INSERT INTO ingress_chat_state "
        "(channel, chat_id, verified_until, lockout_until, recent_failures, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(channel, chat_id) DO UPDATE SET "
        "  verified_until=excluded.verified_until, "
        "  lockout_until=excluded.lockout_until, "
        "  recent_failures=excluded.recent_failures, "
        "  updated_at=datetime('now')",
        (
            state.channel,
            int(state.chat_id),
            _iso(state.verified_until) if state.verified_until else None,
            _iso(state.lockout_until) if state.lockout_until else None,
            json.dumps([_iso(d) for d in state.recent_failures]),
        ),
    )


def is_verified(channel: str, chat_id: int) -> bool:
    st = get_chat_state(channel, chat_id)
    return bool(st.verified_until and st.verified_until > _now())


def is_locked_out(channel: str, chat_id: int) -> bool:
    st = get_chat_state(channel, chat_id)
    return bool(st.lockout_until and st.lockout_until > _now())


def mark_verified(channel: str, chat_id: int) -> None:
    st = get_chat_state(channel, chat_id)
    st.verified_until = _now() + timedelta(seconds=VERIFY_GRACE_SECONDS)
    st.recent_failures = []
    _save_chat_state(st)


def record_failure(channel: str, chat_id: int) -> bool:
    """Append a failure timestamp; flip lockout if threshold exceeded.
    Returns True iff lockout was triggered by this failure."""
    st = get_chat_state(channel, chat_id)
    now = _now()
    cutoff = now - timedelta(seconds=ATTEMPT_WINDOW_SECONDS)
    st.recent_failures = [f for f in st.recent_failures if f > cutoff]
    st.recent_failures.append(now)
    triggered = False
    if len(st.recent_failures) >= MAX_ATTEMPTS_IN_WINDOW:
        st.lockout_until = now + timedelta(seconds=LOCKOUT_SECONDS)
        st.recent_failures = []
        triggered = True
    _save_chat_state(st)
    return triggered


def clear_lockout(channel: str, chat_id: int) -> None:
    st = get_chat_state(channel, chat_id)
    st.lockout_until = None
    st.recent_failures = []
    _save_chat_state(st)
