# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: scan_once — orchestrate scanners, dedupe, write proactive signals.
# index:
#   def scan_once
#   def _retract_resolved
#   def _revalidate_sources
#   def _load_open_proactive_refs
# AGENT_HEADER_END -->
"""Engine for the proactive suggester.

Single entry point :func:`scan_once`. Iterates over every registered
scanner, deduplicates candidates against open ``source='proactive'``
signals on ``source_ref``, and writes the survivors via
:func:`okuro.sense.signals.signal_add`.

Design notes
------------
* Dedupe pre-loads every open proactive signal's ``source_ref`` into a
  set so per-scanner check is O(1). For the volumes okuro deals with
  (<10k signals) this is fine; pagination would only matter at >100k.
* A failure in one scanner does not kill the rest — exceptions are
  logged and recorded in the returned diagnostics.
* ``max_signals_per_scan`` caps a single scan so a bad heuristic can
  never flood the queue. Per DP09 NO-BLOAT.

Two independent closure passes run after emission, and the difference
between them matters:

* :func:`_retract_resolved` closes by ABSENCE — a scanner that
  enumerates its whole domain and did not re-observe a ref proves the
  condition cleared. Only safe for ``EXHAUSTIVE_SCANNERS``.
* :func:`_revalidate_sources` closes by EVIDENCE — it re-reads the row
  the signal is *about* and asks whether that row still qualifies. It
  works for sampled scanners precisely because it never reasons from a
  scanner's silence.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from okuro.sense.proactive.scanners import (
    EXHAUSTIVE_SCANNERS,
    SCANNERS,
    CandidateSignal,
)

log = logging.getLogger(__name__)

DEFAULT_MAX_SIGNALS_PER_SCAN = 50


def scan_once(max_signals: int = DEFAULT_MAX_SIGNALS_PER_SCAN) -> dict:
    """Run every scanner once, write deduplicated signals, return summary.

    Returns a dict with::

        {
          "written": int,         # total new signals inserted
          "skipped_dedupe": int,  # candidates that already had an open signal
          "retracted": int,       # closed by absence (exhaustive scanners)
          "revalidated": int,     # closed by re-reading the referenced row
          "lapsed": int,          # closed by expires_at / per-producer TTL
          "lapsed_detail": {"by_expiry": int, "by_ttl": int},
          "per_scanner": {name: count_written, ...},
          "errors": {name: "exc message", ...},
          "cap_hit": bool,        # True iff max_signals stopped emission
        }

    The handler in ``okuro.daemon._handlers:proactive_scan`` just
    forwards this dict back to the scheduler log.
    """
    from okuro.db import get_db
    from okuro.sense import signals as signals_svc

    db = get_db()
    open_refs = _load_open_proactive_refs(db)

    per_scanner: dict[str, int] = {}
    errors: dict[str, str] = {}
    written = 0
    skipped_dedupe = 0
    cap_hit = False

    # source_refs each scanner yielded this run — collected BEFORE the dedupe
    # check, so a still-true condition whose signal is already open still
    # counts as "observed". Used only for exhaustive scanners; see
    # _retract_resolved.
    observed: dict[str, set[str]] = {}
    # Scanners that ran to completion. A scanner that raised, or was cut off by
    # the cap, did NOT enumerate its whole domain, so its silence proves
    # nothing and must never retract anything.
    complete: set[str] = set()

    for name, fn in SCANNERS.items():
        if cap_hit:
            break
        per_scanner[name] = 0
        observed[name] = set()
        try:
            for cand in fn(db):
                if not isinstance(cand, CandidateSignal):
                    log.warning("scanner %s yielded non-CandidateSignal %r", name, cand)
                    continue
                if written >= max_signals:
                    cap_hit = True
                    log.warning(
                        "proactive scan hit cap %d — skipping remaining candidates",
                        max_signals,
                    )
                    break
                observed[name].add(cand.source_ref)
                if cand.source_ref in open_refs:
                    skipped_dedupe += 1
                    continue

                try:
                    signals_svc.signal_add(
                        source="proactive",
                        source_ref=cand.source_ref,
                        severity=cand.severity,
                        summary=cand.summary,
                        evidence=cand.evidence,
                        suggested_action=cand.suggested_action,
                    )
                except Exception as exc:
                    log.exception(
                        "signal_add failed for %s ref=%s: %s",
                        name, cand.source_ref, exc,
                    )
                    errors.setdefault(name, str(exc))
                    continue

                open_refs.add(cand.source_ref)
                per_scanner[name] += 1
                written += 1
        except Exception as exc:
            log.exception("scanner %s failed: %s", name, exc)
            errors[name] = str(exc)
        else:
            # Only a clean, uncut run proves the domain was fully enumerated.
            if not cap_hit:
                complete.add(name)

    retracted = _retract_resolved(db, observed, complete)
    revalidated = _revalidate_sources(db)
    # Hourly scan is the natural host for the store-level expiry sweep; the
    # sweep itself lives in sense.signals, which owns the status vocabulary.
    swept = signals_svc.signal_sweep_expired()
    lapsed = swept["by_expiry"] + swept["by_ttl"]

    log.info(
        "proactive_scan: written=%d skipped_dedupe=%d retracted=%d revalidated=%d "
        "lapsed=%d cap_hit=%s per_scanner=%s errors=%s",
        written, skipped_dedupe, retracted, revalidated, lapsed,
        cap_hit, per_scanner, errors,
    )

    return {
        "written": written,
        "skipped_dedupe": skipped_dedupe,
        "retracted": retracted,
        "revalidated": revalidated,
        "lapsed": lapsed,
        "lapsed_detail": swept,
        "per_scanner": per_scanner,
        "errors": errors,
        "cap_hit": cap_hit,
    }


def _retract_resolved(db, observed: dict[str, set[str]], complete: set[str]) -> int:
    """Expire open signals whose condition no longer holds. Returns the count.

    A monitoring scanner emits while a condition is true and simply stops
    emitting when it clears — nothing ever closed the signal it left behind.
    Measured 2026-07-15 on the live DB: six open disk signals, up to 57 days
    old, every one about a condition that had already resolved. The worst was a
    `crit` for "/mnt/win11iso at 100% — 0.0 GB free" on a path that is no
    longer even a mountpoint; meanwhile no mount was above 85%. The user's
    only crit-severity alert was pure fiction.

    Only EXHAUSTIVE_SCANNERS are eligible, and only on a run where they
    completed cleanly: their yield is then the complete set of conditions that
    currently hold, so an open signal they did not re-observe has resolved.
    For sampled scanners (LIMIT-capped) an absent ref may just be past the cap
    — retracting those would silently close real alerts.

    'expired' rather than 'discarded': the condition lapsed on its own, the
    user did not reject it. Their disposition vocabulary stays theirs.
    """
    retracted = 0
    for scanner in EXHAUSTIVE_SCANNERS:
        if scanner not in complete:
            continue
        seen = observed.get(scanner, set())

        rows = db.fetchall(
            """SELECT id, source_ref, summary FROM signals
                WHERE source = 'proactive'
                  AND status = 'open'
                  AND CASE WHEN json_valid(evidence)
                           THEN json_extract(evidence, '$.scanner') END = ?""",
            (scanner,),
        )
        for r in rows:
            if r["source_ref"] in seen:
                continue
            db.execute(
                "UPDATE signals SET status = 'expired' WHERE id = ? AND status = 'open'",
                (r["id"],),
            )
            retracted += 1
            log.info(
                "proactive: retracted resolved %s signal — %s",
                scanner, (r["summary"] or "")[:80],
            )
    return retracted


# ── revalidation: close by evidence, not by absence ───────────────────
#
# Every validator below answers ONE question about the row a signal is
# about: does that row still qualify for the alert? It never reasons from
# a scanner's silence, so — unlike _retract_resolved — it is safe for the
# LIMIT-capped scanners, which are exactly the ones that leak.
#
# Measured on the live DB 2026-07-26, before this pass existed:
#   stuck_todo      24 open — 15 referenced a todo row that no longer exists
#   aging_thought   22 open — 14 referenced thoughts already 'dismissed'
#   stale_progress  21 open —  5 had progress logged AFTER the signal fired
# None of them could ever close: no TTL, no re-read, and the only automatic
# closer (_retract_resolved) excludes all three by design.
#
# Retraction is cheap precisely because these producers re-emit: stale
# progress and stuck todos re-fire hourly, continuations daily at 06:30. A
# wrongly-closed signal comes back on the next tick with a fresh reading;
# a never-closed one stays wrong forever.


def _todo_resolved(db, sig: dict) -> Optional[str]:
    """stuck_todo: the alert is void once the todo is closed or deleted."""
    todo_id = sig["evidence"].get("todo_id") or sig["evidence"].get("source_id")
    if not todo_id:
        return None
    row = db.fetchone("SELECT status FROM todos WHERE id = ?", (str(todo_id),))
    if row is None:
        # Deletion of the referenced row is direct evidence about THIS entity
        # (a deliberate act on that todo), not a scanner failing to mention it.
        return "referenced todo no longer exists"
    if row["status"] in ("done", "dropped"):
        return f"referenced todo is {row['status']}"
    return None


def _thought_resolved(db, sig: dict) -> Optional[str]:
    """aging_thought: the scanner only fires on status='open' thoughts."""
    thought_id = sig["evidence"].get("thought_id") or sig["evidence"].get("source_id")
    if not thought_id:
        return None
    row = db.fetchone("SELECT status FROM thoughts WHERE id = ?", (str(thought_id),))
    if row is None:
        return "referenced thought no longer exists"
    if row["status"] != "open":
        return f"referenced thought is {row['status']}"
    return None


def _progress_moved(db, sig: dict) -> Optional[str]:
    """stale_progress / continuation_advisor: work resumed after the signal.

    Positive evidence only — a project with NO progress row is left alone
    rather than treated as resolved. Absence of a row is the same kind of
    silence _retract_resolved refuses to act on for sampled scanners.
    """
    project = sig["evidence"].get("project") or sig["evidence"].get("source_id")
    if not project or not sig.get("created_at"):
        return None
    row = db.fetchone(
        "SELECT MAX(updated_at) AS last FROM progress WHERE project = ?",
        (str(project),),
    )
    last = row["last"] if row else None
    if last and last > sig["created_at"]:
        return f"progress logged {last} — after this signal fired"
    return None


def _note_still_live(db, sig: dict) -> Optional[str]:
    """notes_extract: the item is void once its source note is gone or filed.

    Deleting or archiving a note is a deliberate act on that specific note,
    not a scanner failing to mention it — the same reasoning that lets
    :func:`_todo_resolved` act on a deleted todo. Archived counts because
    ``notes_extract._pending_notes`` excludes archived notes outright: an
    archived note is out of scope for extraction, so an item still claiming
    its authority has none.

    Deliberately NOT age: measured 2026-07-26, the oldest open note signal was
    12 days old. Nothing in that queue is stale yet, and closing on the clock
    would throw away items the user has simply not reached.
    """
    from okuro.sense.signals import note_id_of

    note_id = note_id_of(sig.get("source_ref"), sig["evidence"])
    if not note_id:
        return None
    row = db.fetchone("SELECT archived FROM notes WHERE id = ?", (str(note_id),))
    if row is None:
        return "source note was deleted"
    if row["archived"]:
        return "source note was archived"
    return None


def _model_suggestion_resolved(db, sig: dict) -> Optional[str]:
    """model_suggest: "Run locally? X" is answered once X is installed.

    The durable list (``model_discoveries``) is upserted by every weekly scan
    and survives dismissal, so its ``status`` is the authoritative answer to
    the question the signal asked. A suggestion with no list row is left alone
    — that is absence, and the TTL handles it.
    """
    catalog_id = (sig["evidence"].get("catalog_id") or sig.get("source_ref"))
    if not catalog_id:
        return None
    row = db.fetchone(
        "SELECT status FROM model_discoveries WHERE catalog_id = ?",
        (str(catalog_id),),
    )
    if row is None:
        return None
    if row["status"] in ("installed", "dismissed"):
        return f"model is {row['status']}"
    return None


# producer -> validator. Adding a producer is one line of data (DP10). Keyed
# on signals.producer_id, the same discriminator the TTL registry uses.
# A producer absent here is simply never revalidated — session_failures stays
# out on purpose: its source_ref is a date bucket with no row to re-read, so
# it needs a TTL, not a validator.
_SOURCE_VALIDATORS: dict[str, Callable[[Any, dict], Optional[str]]] = {
    "stuck_todo": _todo_resolved,
    "aging_thought": _thought_resolved,
    "stale_progress": _progress_moved,
    "continuation_advisor": _progress_moved,
    "model_suggest": _model_suggestion_resolved,
    "notes_extract": _note_still_live,
}


def _revalidate_sources(db) -> int:
    """Expire open signals whose referenced source row no longer qualifies.

    Runs independently of the scan: it re-reads producer rows directly, so a
    scanner that crashed, hit the cap, or was capped by its LIMIT does not
    affect it. Returns the number of signals expired.

    'expired' rather than 'discarded' — the condition lapsed on its own; the
    user's disposition vocabulary stays theirs (same rule as
    :func:`_retract_resolved`).
    """
    from okuro.sense.signals import producer_id

    # Every open row, filtered in Python: producer identity falls back to a
    # source_ref prefix for producers that never tagged their evidence, and no
    # single json_extract predicate can express that.
    rows = db.fetchall(
        """SELECT id, source_ref, summary, evidence, created_at
             FROM signals
            WHERE status = 'open'"""
    )

    expired = 0
    for r in rows:
        sig = dict(r)
        raw = sig.get("evidence")
        if isinstance(raw, str):
            try:
                sig["evidence"] = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, TypeError):
                continue
        elif raw is None:
            sig["evidence"] = {}
        if not isinstance(sig["evidence"], dict):
            continue

        validator = _SOURCE_VALIDATORS.get(
            producer_id(sig.get("source_ref"), sig["evidence"]) or ""
        )
        if validator is None:
            continue
        try:
            reason = validator(db, sig)
        except Exception as exc:  # a broken validator must never close a signal
            log.warning("revalidate: validator failed for %s: %s", sig["id"], exc)
            continue
        if not reason:
            continue

        db.execute(
            "UPDATE signals SET status = 'expired' WHERE id = ? AND status = 'open'",
            (sig["id"],),
        )
        expired += 1
        log.info(
            "proactive: revalidated away %s — %s (%s)",
            producer_id(sig.get("source_ref"), sig["evidence"]),
            (sig["summary"] or "")[:80],
            reason,
        )
    return expired


def _load_open_proactive_refs(db) -> set[str]:
    """Pre-load source_refs for every open proactive signal.

    Used to skip candidates whose previous signal is still in the
    user's queue — promotes one signal per heuristic event until the
    user acts on it.
    """
    rows = db.fetchall(
        """SELECT source_ref FROM signals
           WHERE source = 'proactive'
             AND status = 'open'
             AND source_ref IS NOT NULL""",
    )
    return {r["source_ref"] for r in rows if r["source_ref"]}


__all__ = ["scan_once", "DEFAULT_MAX_SIGNALS_PER_SCAN", "_SOURCE_VALIDATORS"]
