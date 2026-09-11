# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Commitments — LLM-infer per-session implicit follow-ups from recent provider sessions; store with TTL; surfaced via the inbox as kind='commitment'.
# index:
#   constants
#   def infer_recent
#   def _candidate_sessions
#   def _build_bundle
#   def _resolve_project
#   def _ask_llm
#   def _parse_commitments
#   def _slug
# AGENT_HEADER_END -->
"""Commitments — OpenClaw-style auto-capture of implicit follow-ups.

Where the continuation advisor judges PROJECTS ("X is 90% built"), this
module judges SESSIONS ("you said you'd add a test for the parser and
never did"). It scans recently-active provider sessions
(``agent_sessions``), bundles each session's transcript
(``agent_events``), and asks an LLM (via ``bridge.invoke``) to extract
the concrete implicit follow-ups the user/agent left undone or promised.

Each extracted item becomes one ``commitments`` row (TTL 7 days), which
the inbox reducer projects as ``kind='commitment'``.

CRITICAL data-model note: okuro's ``sessions.session_id`` is a DISJOINT
id space from ``agent_sessions.session_id`` (0 overlap). We scan
``agent_sessions`` directly and read ``agent_events`` for content
(Option A). ``project`` is derived by matching
``agent_sessions.project_path`` against ``projects.path``; unresolvable
→ NULL.

Robustness contract (mirrors the continuation advisor):
- bounded: cap sessions/run (``max_sessions``) + per-call timeout.
- never crashes the daemon: provider/transport failures are caught and
  returned, never raised.
- watermark: every PROCESSED session writes a ``commitment_scans`` row
  (even on provider failure, with ``n_emitted=0``) so the expensive LLM
  pass never re-runs on it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger("okuro.sense.commitments")

_PROVIDER = "claude"
_PER_CALL_TIMEOUT = 180  # seconds — same headroom as the continuation advisor.
_MAX_SESSIONS = 8
_WINDOW_DAYS = 3            # only very recent sessions — volume control
_MAX_PER_SESSION = 2       # keep only the most important follow-ups per session
_TTL_DAYS = 3              # commitments are ephemeral — short shelf life
_MAX_OPEN = 200            # global ceiling — expire oldest open beyond this
_MIN_EVENTS = 6            # skip trivial sessions (status checks, tiny runs)

# A session must be QUIET for this long before it is judged. Nothing in
# agent_sessions says whether a session ended — the table carries only
# first_ts/last_ts (it is populated by an indexer walking provider transcript
# files, which never see an "end"). Silence is the only available proxy.
#
# Without it the scan had a lower bound only ("active within _WINDOW_DAYS"), so
# it judged sessions that were still typing. Measured 2026-07-15: 2519 of 3156
# scans (80%) ran mid-session — the session kept working AFTER okuro decided
# what it had left undone; one continued for 16 more days. Because
# commitment_scans is a once-ever watermark, those verdicts could never be
# revisited. The user saw commitments for work being done as he read them.
#
# 30 min matches the orphan-session threshold used by the inbox surfacing gate
# (_BUSY_FRESHNESS_MINUTES) and session hygiene's close_orphaned_sessions — one
# number for "this agent has stopped", not three.
_QUIET_MINUTES = 30

# Bundle caps — keep the prompt compact and bounded regardless of how
# enormous a transcript is.
_MAX_TEXT_EVENTS = 60       # last N user/assistant text events
_MAX_EVENT_CHARS = 600      # per-event truncation
_MAX_TOOLS = 30             # distinct tool names listed

_SYSTEM_PROMPT = """\
You extract concrete implicit follow-ups from one AI coding session
transcript — the things the user or agent left undone, deferred, or
promised but did not finish in the session.

Output STRICTLY one JSON object, no prose, no markdown fence:
{"commitments":[{"title":"<imperative, specific, <=90 chars>","detail":"<one sentence of context>","evidence":"<short quote or ref from the transcript>"}]}

Rules:
- Each title is a single concrete action the user would otherwise forget
  (e.g. "Add a unit test for the date parser", "Restart the daemon to
  apply migration 047", "Push the branch after the review").
- Imperative voice, specific, deduplicated. No vague items ("improve
  things"), no items already completed in the session.
- Return AT MOST 2 commitments — only the most important things genuinely
  left undone. Most sessions have 0-1. If nothing was left undone, return
  {"commitments":[]}. Prefer an empty list over noise. Do NOT fabricate.
"""

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_BLOCK = re.compile(r"\{.*\"commitments\".*\}", re.DOTALL)


def infer_recent(
    max_sessions: int = _MAX_SESSIONS,
    window_days: int = _WINDOW_DAYS,
    per_call_timeout: int = _PER_CALL_TIMEOUT,
    *,
    provider: str = _PROVIDER,
    quiet_minutes: int = _QUIET_MINUTES,
) -> dict:
    """Infer commitments from FINISHED provider sessions.

    Only sessions quiet for ``quiet_minutes`` are judged — a commitment is
    "what you left undone", which is not answerable while the session is still
    doing it. See _QUIET_MINUTES.

    Returns a summary dict::

        {"scanned": n, "emitted": m, "expired": k,
         "skipped_no_provider": x}

    Never raises on provider failure — a failed session is recorded as a
    watermark with ``n_emitted=0`` and counted under
    ``skipped_no_provider``.
    """
    from okuro.db import get_db

    db = get_db()

    sessions = _candidate_sessions(
        db, window_days=window_days, limit=max_sessions, quiet_minutes=quiet_minutes
    )
    log.info("commitments: %d candidate sessions", len(sessions))

    scanned = 0
    emitted = 0
    skipped_no_provider = 0

    for sess in sessions:
        sid = sess["session_id"]
        project = _resolve_project(db, sess.get("project_path"))

        try:
            bundle = _build_bundle(db, sid)
        except Exception as exc:  # bundle assembly must never crash the run
            log.exception("commitments: bundle failed for %s: %s", sid, exc)
            _write_scan(db, sid, 0)
            scanned += 1
            continue

        n_emitted = 0
        if not bundle.strip():
            # Empty transcript — still watermark so we don't retry forever.
            _write_scan(db, sid, 0)
            scanned += 1
            continue

        try:
            raw = _ask_llm(bundle, provider=provider, timeout=per_call_timeout)
        except Exception as exc:
            # Provider/transport failure — graceful skip + watermark.
            log.warning("commitments: provider failed for %s: %s", sid, exc)
            _write_scan(db, sid, 0)
            scanned += 1
            skipped_no_provider += 1
            continue

        items = _parse_commitments(raw)
        if items:
            n_emitted = _write_commitments(db, sid, project, items)

        _write_scan(db, sid, n_emitted)
        scanned += 1
        emitted += n_emitted

    expired = _sweep_expired(db)

    log.info(
        "commitments: scanned=%d emitted=%d expired=%d skipped_no_provider=%d",
        scanned, emitted, expired, skipped_no_provider,
    )
    return {
        "scanned": scanned,
        "emitted": emitted,
        "expired": expired,
        "skipped_no_provider": skipped_no_provider,
    }


# ── candidate selection ────────────────────────────────────────────


def _candidate_sessions(
    db, *, window_days: int, limit: int, quiet_minutes: int = _QUIET_MINUTES
) -> list[dict]:
    """FINISHED agent_sessions, newest first, not yet scanned, with events.

    A session qualifies when:
      - ``last_ts`` is within ``window_days`` (recent enough to still matter),
      - ``last_ts`` is at least ``quiet_minutes`` old — the session has stopped.
        This is the finished-check: a commitment is "what you left undone", so
        judging a session still in flight produces a verdict about work that is
        actively being done. See _QUIET_MINUTES for the measured damage.
      - it is NOT already in ``commitment_scans`` (watermark),
      - it has at least ``_MIN_EVENTS`` ``agent_events`` rows (trivial
        sessions — status checks, tiny runs — are skipped to control volume).

    The two ts bounds form a window: quiet enough to be over, recent enough to
    be worth reading. A session that stays busy simply waits — it is not lost,
    because the watermark only records sessions that were actually scanned.
    """
    rows = db.fetchall(
        """SELECT s.session_id, s.project_path, s.last_ts
             FROM agent_sessions s
            WHERE s.last_ts > datetime('now', ?)
              AND s.last_ts < datetime('now', ?)
              AND s.session_id NOT IN (SELECT session_id FROM commitment_scans)
              AND (
                    SELECT count(*) FROM agent_events e
                     WHERE e.session_id = s.session_id
                  ) >= ?
         ORDER BY s.last_ts DESC
            LIMIT ?""",
        (
            f"-{int(window_days)} days",
            f"-{int(quiet_minutes)} minutes",
            _MIN_EVENTS,
            limit,
        ),
    )
    return [dict(r) for r in rows]


# ── bundle assembly ────────────────────────────────────────────────


def _build_bundle(db, session_id: str) -> str:
    """Compact transcript bundle for one session.

    Last ``_MAX_TEXT_EVENTS`` user/assistant text events (chronological,
    each truncated) + the distinct tool names used. Bounded by
    construction so the prompt size never blows up on a huge session.
    """
    # Pull the most-recent text-bearing user/assistant events, then
    # re-order chronologically for the prompt.
    events = db.fetchall(
        """SELECT type, role, text
             FROM agent_events
            WHERE session_id = ?
              AND type IN ('user','assistant')
              AND text IS NOT NULL
              AND length(trim(text)) > 0
         ORDER BY ord DESC
            LIMIT ?""",
        (session_id, _MAX_TEXT_EVENTS),
    )
    events = list(reversed(events))

    tools = db.fetchall(
        """SELECT DISTINCT tool_name
             FROM agent_events
            WHERE session_id = ?
              AND tool_name IS NOT NULL
              AND tool_name != ''
            LIMIT ?""",
        (session_id, _MAX_TOOLS),
    )

    parts: list[str] = [f"## Session transcript (id={session_id})"]
    for e in events:
        speaker = (e.get("type") or e.get("role") or "msg").lower()
        text = (e.get("text") or "").strip().replace("\n", " ")
        if len(text) > _MAX_EVENT_CHARS:
            text = text[:_MAX_EVENT_CHARS] + "…"
        parts.append(f"[{speaker}] {text}")

    tool_names = [t["tool_name"] for t in tools if t.get("tool_name")]
    if tool_names:
        parts.append("\n### Tools used")
        parts.append(", ".join(tool_names))

    return "\n".join(parts)


# ── project resolution ─────────────────────────────────────────────


def _resolve_project(db, project_path: str | None) -> str | None:
    """Map agent_sessions.project_path → projects.id, else None."""
    if not project_path:
        return None
    row = db.fetchone(
        "SELECT id FROM projects WHERE path = ? LIMIT 1",
        (project_path,),
    )
    return row["id"] if row else None


# ── LLM call + parse ───────────────────────────────────────────────


def _ask_llm(bundle: str, *, provider: str, timeout: int) -> str:
    """Send the bundle through bridge.invoke. Raises on transport failure."""
    from okuro.bridge import invoke

    result = invoke(
        prompt=bundle,
        system_prompt=_SYSTEM_PROMPT,
        provider=provider,
        timeout=timeout,
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected invoke return type: {type(result)}")
    if not result.get("success"):
        raise RuntimeError(result.get("error") or "invoke returned success=False")
    return str(result.get("output") or "")


def _parse_commitments(raw: str) -> list[dict]:
    """Tolerant parse → list of {title, detail, evidence} dicts.

    Strips any ``<think>…</think>`` reasoning block first, then tries
    fenced JSON, a brace-block regex, and raw json.loads in turn.
    Returns [] on any failure (never raises).
    """
    if not raw or not raw.strip():
        return []
    cleaned = _THINK_RE.sub("", raw).strip()
    if not cleaned:
        return []

    candidates: list[str] = []
    fence = _JSON_FENCE.search(cleaned)
    if fence:
        candidates.append(fence.group(1))
    block = _JSON_BLOCK.search(cleaned)
    if block:
        candidates.append(block.group(0))
    candidates.append(cleaned)

    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        items = data.get("commitments")
        if not isinstance(items, list):
            continue
        out: list[dict] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            title = _clean_str(it.get("title"))
            if not title:
                continue
            out.append(
                {
                    "title": title[:200],
                    "detail": (_clean_str(it.get("detail")) or "")[:600],
                    "evidence": (_clean_str(it.get("evidence")) or "")[:400],
                }
            )
        return out
    return []


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


# ── writes ─────────────────────────────────────────────────────────


def _write_commitments(db, session_id: str, project: str | None, items: list[dict]) -> int:
    """INSERT OR IGNORE commitments for one session (capped, short TTL).

    Only the first ``_MAX_PER_SESSION`` items are kept — the LLM is asked for
    at most 2, but we enforce the cap deterministically. ``expires_at`` is set
    to ``_TTL_DAYS`` (not the table's 7-day default) — commitments are
    ephemeral session follow-ups. Returns rows inserted.
    """
    inserted = 0
    with db.write() as conn:
        for it in items[:_MAX_PER_SESSION]:
            cid = f"commit:{session_id}:{_slug(it['title'])}"
            evidence = json.dumps(
                {"session_id": session_id, "evidence": it.get("evidence", "")},
                ensure_ascii=False,
            )
            cur = conn.execute(
                """INSERT OR IGNORE INTO commitments
                       (id, session_id, project, title, detail, evidence, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now', ?))""",
                (
                    cid,
                    session_id,
                    project,
                    it["title"],
                    it.get("detail") or None,
                    evidence,
                    f"+{_TTL_DAYS} days",
                ),
            )
            inserted += cur.rowcount or 0
    return inserted


def _write_scan(db, session_id: str, n_emitted: int) -> None:
    """Watermark a processed session (idempotent — REPLACE on conflict)."""
    with db.write() as conn:
        conn.execute(
            """INSERT INTO commitment_scans (session_id, scanned_at, n_emitted)
               VALUES (?, datetime('now'), ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   scanned_at = datetime('now'),
                   n_emitted = excluded.n_emitted""",
            (session_id, n_emitted),
        )


def _sweep_expired(db) -> int:
    """Expire past-due commitments AND enforce the global open ceiling.

    Two passes: (1) past-due by ``expires_at``; (2) keep only the newest
    ``_MAX_OPEN`` open commitments, expire the rest (oldest first). The
    inbox surfaces by salience (recency-weighted), so keeping the newest is
    consistent with what the user would see. Returns total expired.
    """
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE commitments SET status='expired' "
            "WHERE status='open' AND expires_at <= datetime('now')"
        )
        n = cur.rowcount or 0
        cur2 = conn.execute(
            "UPDATE commitments SET status='expired' "
            "WHERE status='open' AND id NOT IN ("
            "  SELECT id FROM commitments WHERE status='open' "
            "  ORDER BY created_at DESC LIMIT ?)",
            (_MAX_OPEN,),
        )
        return n + (cur2.rowcount or 0)


def _slug(title: str) -> str:
    """Stable short slug for the commitment id (keeps UNIQUE(session,title) authoritative)."""
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:48] or "x"


__all__ = ["infer_recent"]
