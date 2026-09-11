# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Contradiction detection over the temporal knowledge graph.
# index: imports | def _today | def _between | def _lookup | def kg_assert
# AGENT_HEADER_END -->
"""Contradiction detection over the temporal knowledge graph.

Compares an asserted claim to existing kg_triples and flags conflicts.
Pure read — no new schema. Detection categories (mempalace pattern):

  - **stale**         — matching triple was invalidated before ``as_of``
  - **attribution**   — same predicate+object but a *different* subject
                        was active at ``as_of`` (someone else owns it)
  - **superseded**    — same subject+predicate but a different ``object``
                        is currently active (the role/state moved on)
  - **temporal**      — the claim's ``as_of`` precedes the recorded
                        ``valid_from`` of the matching triple
  - **ok**            — claim agrees with an active triple
  - **unknown**       — KG has no triples for the (s, p) at all

Verdict severity: ``conflict`` > ``warn`` > ``ok`` > ``unknown``.
"""

from __future__ import annotations

from datetime import date


_VERDICT_RANK = {"unknown": 0, "ok": 1, "warn": 2, "conflict": 3}


def _today() -> str:
    return date.today().isoformat()


def _between(d: str | None, lo: str | None, hi: str | None) -> bool:
    """ISO date overlap test — empty/None bounds treated as open."""
    if d is None or d == "":
        return True
    if lo and d < lo:
        return False
    if hi and d > hi:
        return False
    return True


def _lookup(subject: str | None = None, predicate: str | None = None,
            object: str | None = None, project: str | None = None) -> list[dict]:
    """Read triples matching any combination of s/p/o filters."""
    from okuro.db import get_db
    db = get_db()
    where = ["1=1"]
    params: list = []
    if subject is not None:
        where.append("subject = ?")
        params.append(subject)
    if predicate is not None:
        where.append("predicate = ?")
        params.append(predicate)
    if object is not None:
        where.append("object = ?")
        params.append(object)
    if project:
        where.append("(project = ? OR project IS NULL)")
        params.append(project)
    sql = f"""SELECT id, subject, predicate, object, valid_from, valid_to,
                     project, source_memory_id, source_artifact_id,
                     confidence, created_at, invalidated_at
              FROM kg_triples
              WHERE {' AND '.join(where)}"""
    return [dict(r) for r in db.fetchall(sql, tuple(params))]


def _is_active(triple: dict, as_of: str) -> bool:
    """True when ``triple`` was valid on ``as_of``."""
    return _between(as_of, triple.get("valid_from"), triple.get("valid_to"))


def _trim(triple: dict) -> dict:
    """Compact dict for output — drop fields callers rarely need."""
    return {
        "id": triple["id"][:12],
        "subject": triple["subject"],
        "predicate": triple["predicate"],
        "object": triple["object"],
        "valid_from": triple.get("valid_from"),
        "valid_to": triple.get("valid_to"),
        "confidence": triple.get("confidence"),
        "source_memory_id": triple.get("source_memory_id"),
    }


def kg_assert(subject: str, predicate: str, object: str,
              as_of: str | None = None,
              project: str | None = None,
              min_confidence: float = 0.5) -> dict:
    """Compare a claim ``(subject, predicate, object)`` against the KG.

    Args:
        subject, predicate, object: the claim being asserted.
        as_of: ISO date the claim is being asserted as-of (default: today).
        project: scope detection to one project plus system-wide triples.
        min_confidence: skip KG triples below this confidence.

    Returns:
        ``{verdict, conflicts: [...], confirms: [...], stale: [...], notes: [...]}``
        where verdict is one of {ok, warn, conflict, unknown}.
    """
    aof = as_of or _today()

    confirms: list[dict] = []
    conflicts: list[dict] = []
    stale: list[dict] = []
    notes: list[str] = []

    # 1) Direct match: same s+p+o.
    direct = [
        t for t in _lookup(subject=subject, predicate=predicate,
                           object=object, project=project)
        if (t.get("confidence") or 0.0) >= min_confidence
    ]

    for t in direct:
        if _is_active(t, aof):
            confirms.append(_trim(t))
        else:
            vt = t.get("valid_to")
            vf = t.get("valid_from")
            if vt and aof > vt:
                stale.append(_trim(t))
                notes.append(
                    f"matching triple invalidated on {vt} — claim asserted as_of {aof}"
                )
            elif vf and aof < vf:
                # Temporal: claim is earlier than the recorded start.
                conflicts.append({**_trim(t), "_kind": "temporal"})
                notes.append(
                    f"claim as_of {aof} precedes recorded valid_from {vf}"
                )

    # 2) Attribution conflict: same p+o, different active subject.
    same_po = [
        t for t in _lookup(predicate=predicate, object=object, project=project)
        if (t.get("confidence") or 0.0) >= min_confidence
        and t["subject"] != subject
        and _is_active(t, aof)
    ]
    for t in same_po:
        conflicts.append({**_trim(t), "_kind": "attribution"})
        notes.append(
            f"attribution conflict: KG says {t['subject']} -{predicate}-> {object} "
            f"is active at {aof}, not {subject}"
        )

    # 3) Superseded: same s+p, different active object.
    same_sp = [
        t for t in _lookup(subject=subject, predicate=predicate, project=project)
        if (t.get("confidence") or 0.0) >= min_confidence
        and t["object"] != object
        and _is_active(t, aof)
    ]
    for t in same_sp:
        conflicts.append({**_trim(t), "_kind": "superseded"})
        notes.append(
            f"superseded: KG says {subject} -{predicate}-> {t['object']} "
            f"is active at {aof}, not {object}"
        )

    # Decide verdict.
    if conflicts:
        verdict = "conflict"
    elif confirms:
        verdict = "ok"
    elif stale:
        verdict = "warn"
    elif direct or same_po or same_sp:
        # We had matches but none survived activity/confidence filters.
        verdict = "warn"
    else:
        verdict = "unknown"

    return {
        "verdict": verdict,
        "as_of": aof,
        "claim": {"subject": subject, "predicate": predicate, "object": object},
        "confirms": confirms,
        "conflicts": conflicts,
        "stale": stale,
        "notes": notes,
    }
