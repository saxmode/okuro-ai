# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Signals — proactive suggestions awaiting promotion to todos.
# index:
#   def _row_to_dict
#   def signal_add
#   def signal_list
#   def signal_get
#   def signal_promote
#   def signal_discard
#   def signal_ignore
#   def note_id_of
#   def producer_id
#   def signal_sweep_expired
# AGENT_HEADER_END -->
"""Signals — the proactive proposal queue feeding the todos pipeline.

Backed by the ``signals`` table (migration 042). A signal is a
suggestion from automation (sysmon / proactive / orchestrator) or a
human (manual). Promotion creates a ``todos`` row with
``source_signal_id`` set; discard / ignore mark the signal closed.

Distinct from ``proposals`` (older proactive surface) — proposals are
the score-banded scoring/ranking queue. Signals are typed,
severity-stamped events feeding the same human-facing inbox concept
but through a different schema. See migration 042 for the contract.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional


_VALID_SOURCE = {"sysmon", "proactive", "orchestrator", "manual", "notes"}
_VALID_SEVERITY = {"info", "warn", "crit"}
_VALID_STATUS = {"open", "promoted", "discarded", "ignored", "expired"}


def _row_to_dict(row: Any) -> dict:
    """Parse JSON columns into Python dicts for serialisation."""
    out = dict(row)
    raw = out.get("evidence")
    if isinstance(raw, str):
        try:
            out["evidence"] = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            out["evidence"] = {}
    elif raw is None:
        out["evidence"] = {}
    out["auto_promote"] = bool(out.get("auto_promote", 0))
    return out


def signal_add(
    *,
    source: str,
    severity: str,
    summary: str,
    source_ref: Optional[str] = None,
    evidence: Optional[dict] = None,
    suggested_action: Optional[str] = None,
    auto_promote: bool = False,
    expires_at: Optional[str] = None,
) -> dict:
    """Register a new signal in the proposal queue.

    Used by ingest scripts (wave 4c — host monit → okuro) and by
    the orchestrator self-monitoring pipeline. Returns the full row
    including the generated id.

    Raises ValueError when source/severity violate the closed
    vocabularies pinned by CHECK constraints on the table.
    """
    if source not in _VALID_SOURCE:
        raise ValueError(f"source must be one of {sorted(_VALID_SOURCE)}")
    if severity not in _VALID_SEVERITY:
        raise ValueError(f"severity must be one of {sorted(_VALID_SEVERITY)}")
    if not summary or not summary.strip():
        raise ValueError("summary must be non-empty")

    from okuro.db import get_db

    db = get_db()
    signal_id = str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO signals (
            id, source, source_ref, severity, summary,
            evidence, suggested_action, auto_promote, status, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (
            signal_id,
            source,
            source_ref,
            severity,
            summary.strip(),
            json.dumps(evidence or {}),
            suggested_action,
            1 if auto_promote else 0,
            expires_at,
        ),
    )
    return signal_get(signal_id)


def signal_list(
    *,
    status: Optional[str] = "open",
    limit: int = 50,
) -> list[dict]:
    """List signals filtered by status, paginated newest-first.

    Default ``status='open'`` matches the Now-page Signals section
    contract. Pass ``status=None`` to return every status (used by the
    audit UI).
    """
    clauses: list[str] = []
    params: list[Any] = []

    if status is not None:
        if status not in _VALID_STATUS:
            raise ValueError(f"status must be one of {sorted(_VALID_STATUS)}")
        clauses.append("status = ?")
        params.append(status)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))

    from okuro.db import get_db

    db = get_db()
    rows = db.fetchall(
        f"""
        SELECT id, source, source_ref, severity, summary, evidence,
               suggested_action, auto_promote, status, discard_reason,
               created_at, expires_at
          FROM signals
         {where}
         ORDER BY created_at DESC
         LIMIT ?
        """,
        tuple(params),
    )
    return [_row_to_dict(r) for r in rows]


def signal_get(signal_id: str) -> dict:
    """Return one signal by id, or raise KeyError."""
    from okuro.db import get_db

    db = get_db()
    row = db.fetchone(
        """
        SELECT id, source, source_ref, severity, summary, evidence,
               suggested_action, auto_promote, status, discard_reason,
               created_at, expires_at
          FROM signals
         WHERE id = ?
        """,
        (signal_id,),
    )
    if row is None:
        raise KeyError(f"signal {signal_id!r} not found")
    return _row_to_dict(row)


def signal_promote(
    signal_id: str,
    *,
    todo_title: Optional[str] = None,
    todo_priority: int = 3,
    project: Optional[str] = None,
) -> dict:
    """Promote a signal to a todos row.

    The new todo carries ``source_signal_id=signal_id`` so the
    pipeline can backtrace. The signal flips to ``status='promoted'``.

    ``todo_title`` defaults to ``signal.suggested_action`` (when
    present) then to ``signal.summary`` — keeps the user one click
    away from a workable todo.

    Returns ``{signal_id, todo_id, todo}``. Raises KeyError if the
    signal does not exist, ValueError if its status is not 'open'.
    """
    sig = signal_get(signal_id)
    if sig["status"] != "open":
        raise ValueError(
            f"signal {signal_id!r} is {sig['status']!r}, only open signals can be promoted"
        )

    title = (todo_title or sig.get("suggested_action") or sig["summary"]).strip()
    if not title:
        raise ValueError("derived todo title is empty")
    if not 1 <= int(todo_priority) <= 5:
        raise ValueError("todo_priority must be 1..5")

    from okuro.db import get_db
    from okuro.sense import todos as todos_svc

    todo = todos_svc.todo_add(
        title=title,
        detail=sig.get("summary") if title != sig["summary"] else None,
        priority=int(todo_priority),
        project=project,
        source="agent",
        source_event_id=signal_id,
        context={"signal_id": signal_id, "severity": sig["severity"], "source": sig["source"]},
    )

    db = get_db()
    db.execute(
        "UPDATE todos SET source_signal_id = ? WHERE id = ?",
        (signal_id, todo["id"]),
    )
    db.execute(
        "UPDATE signals SET status = 'promoted' WHERE id = ?",
        (signal_id,),
    )

    return {"signal_id": signal_id, "todo_id": todo["id"], "todo": todos_svc.todo_get(todo["id"])}


def signal_discard(signal_id: str, *, reason: Optional[str] = None) -> dict:
    """Mark a signal discarded with an audit reason.

    Raises KeyError if the signal does not exist, ValueError if its
    status is not 'open'.
    """
    sig = signal_get(signal_id)
    if sig["status"] != "open":
        raise ValueError(
            f"signal {signal_id!r} is {sig['status']!r}, only open signals can be discarded"
        )

    from okuro.db import get_db

    db = get_db()
    db.execute(
        "UPDATE signals SET status = 'discarded', discard_reason = ? WHERE id = ?",
        (reason, signal_id),
    )
    return signal_get(signal_id)


def signal_ignore(signal_id: str) -> dict:
    """Mark a signal ignored — will expire on the next sweep.

    Same precondition (status='open') and idempotence as discard.
    """
    sig = signal_get(signal_id)
    if sig["status"] != "open":
        raise ValueError(
            f"signal {signal_id!r} is {sig['status']!r}, only open signals can be ignored"
        )

    from okuro.db import get_db

    db = get_db()
    db.execute(
        "UPDATE signals SET status = 'ignored' WHERE id = ?",
        (signal_id,),
    )
    return signal_get(signal_id)


# ── producer identity ────────────────────────────────────────────────


# Producers that predate the evidence.scanner convention, keyed by the
# source_ref prefix they mint. Without this the closure registries simply
# cannot address them: ai_models.suggest writes catalog_id straight into
# source_ref and never tagged its evidence.
_PRODUCER_BY_REF_PREFIX: dict[str, str] = {
    "huggingface:": "model_suggest",
    "civitai:": "model_suggest",
}


def note_id_of(source_ref: Optional[str], evidence: Optional[dict]) -> Optional[str]:
    """The note a note-derived signal came from, or None.

    ``notes_extract`` mints ``source_ref = "note:<note_id>#<item_key>"`` and
    also tags ``evidence.note_id``; rows written before the tag existed only
    have the ref. One parser, because three callers need it — the inbox
    reducer (age anchor), the revalidation validator (is the note still
    there), and anything that follows.
    """
    if isinstance(evidence, dict):
        for key in ("note_id", "source_id"):
            value = evidence.get(key)
            if isinstance(value, str) and value:
                return value
    ref = source_ref or ""
    if ref.startswith("note:") and "#" in ref:
        return ref[len("note:"):ref.index("#")]
    return None


def producer_id(source_ref: Optional[str], evidence: Optional[dict]) -> Optional[str]:
    """Return the logical producer of a signal, or None if unknown.

    ONE discriminator for every closure registry (``_TTL_DAYS`` here,
    ``_SOURCE_VALIDATORS`` in ``proactive.engine``) so a producer is named in
    exactly one vocabulary. Prefers the ``evidence.scanner`` tag every modern
    producer writes; falls back to the source_ref prefix so rows written
    before that convention are still addressable — retrofitting the tag would
    only reach rows written from now on.
    """
    if isinstance(evidence, dict):
        scanner = evidence.get("scanner")
        if isinstance(scanner, str) and scanner:
            return scanner
    if source_ref:
        for prefix, producer in _PRODUCER_BY_REF_PREFIX.items():
            if source_ref.startswith(prefix):
                return producer
    return None


# ── expiry sweep ─────────────────────────────────────────────────────
#
# Per-producer lifetime, in days, keyed on ``evidence.scanner``. Only for
# producers whose alert is genuinely void with age and that have NO row to
# re-read — for anything with a source row,
# ``proactive.engine._revalidate_sources`` closes by evidence, which is
# always better than closing by clock.
#
# session_failures: the alert is "N failures in the last 24h", and its
# source_ref is date-bucketed, so a given day's spike can never be
# re-observed or retracted. Measured 2026-07-26: 30 open, one per day back
# to at least 2026-07-17, none closable by any existing path.
#
# notes_extract is deliberately ABSENT despite being the largest queue (986
# open in 12 days): its volume is a production problem, not a staleness
# problem, and a TTL would hide the firehose instead of fixing it.
#
# model_suggest: "Run locally? <model>" from the weekly box-fit research. The
# research refreshes every Monday, so a month-old suggestion has been
# superseded by newer scans; and because ai_models.suggest dedupes against
# OPEN signals, a stale one blocks the model from ever being re-suggested.
_TTL_DAYS: dict[str, int] = {
    "session_failures": 7,
    "model_suggest": 30,
}


def signal_sweep_expired() -> dict:
    """Close open signals whose lifetime has lapsed. Returns the counts.

    Two rules, both closing to ``status='expired'`` (the condition lapsed on
    its own — the user's disposition vocabulary stays theirs):

    * explicit — ``expires_at`` is set and in the past. The column has
      existed since migration 042 and was accepted by ``signal_add`` and the
      REST API, but nothing ever read it: it was a write-only lever. Ingest
      paths that know their own shelf life (monit-style events) can now set
      it and have it honoured.
    * policy — the producer is in :data:`_TTL_DAYS` and the row is older
      than its budget. Applied at sweep rather than defaulted at insert
      because a default only reaches rows written from now on, and the rows
      that need this are the ones already stranded.

    Idempotent; only touches ``status='open'``.
    """
    from okuro.db import get_db

    db = get_db()

    cur = db.execute(
        "UPDATE signals SET status = 'expired' "
        "WHERE status = 'open' AND expires_at IS NOT NULL "
        "  AND expires_at <= datetime('now')"
    )
    by_expiry = getattr(cur, "rowcount", 0) or 0

    # Resolved row-wise rather than in SQL: producer identity falls back to a
    # source_ref prefix for producers that never tagged their evidence, which
    # no single json_extract predicate can express.
    rows = db.fetchall(
        "SELECT id, source_ref, evidence, created_at FROM signals "
        "WHERE status = 'open'"
    )
    by_ttl = 0
    for row in rows:
        raw = row.get("evidence")
        evidence: dict = {}
        if isinstance(raw, str) and raw:
            try:
                evidence = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}

        days = _TTL_DAYS.get(producer_id(row.get("source_ref"), evidence) or "")
        if days is None:
            continue
        cur = db.execute(
            "UPDATE signals SET status = 'expired' "
            "WHERE id = ? AND status = 'open' "
            "  AND created_at < datetime('now', ?)",
            (row["id"], f"-{int(days)} days"),
        )
        by_ttl += getattr(cur, "rowcount", 0) or 0

    return {"by_expiry": by_expiry, "by_ttl": by_ttl}
