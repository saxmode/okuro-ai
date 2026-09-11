# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: source
# purpose: Durable store for the weekly model-scan's fit-gated candidates —
#          the LIST backing the /models "Discover" tab. The scan files each
#          pick as a proactive signal (notify); here it also lands as a
#          durable row so it stays browsable after the notification is
#          dismissed. Signal = notify channel; this table = the list.
# index:
#   def upsert_discovery      (scan writes a candidate; status preserved on re-seen)
#   def list_discoveries      (default list for the Discover tab; optional query filter)
#   def set_status            (acknowledge / install / dismiss)
#   def mark_installed        (called when a pull completes)
# AGENT_HEADER_END -->
"""Durable ``model_discoveries`` store (see migration 084).

Kept out of ``suggest.py`` so the scan orchestration stays pure/testable and
the persistence layer has one home. All writes are single-statement DML, which
autocommits under the sqlite backend's ``isolation_level=None``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

_VALID_STATUS = ("new", "acknowledged", "installed", "dismissed")

# Columns returned to the API/UI, in a stable order.
_COLS = (
    "catalog_id", "display_name", "modality", "min_vram_gb", "fit_gpu",
    "score", "rationale", "use_case", "caveats", "source_url", "researched",
    "commercial_status", "commercial_allowed", "license_id",
    "status", "first_seen_at", "last_seen_at",
)


def upsert_discovery(s: Any) -> str:
    """Insert (or refresh) one discovery from a ``ModelSuggestion``.

    New rows land as ``status='new'``. Re-seen rows keep their status
    (an acknowledged/installed/dismissed model must not silently revert to
    new) but refresh score/rationale/last_seen_at so the list reflects the
    latest scan. Returns 'new' on first sight, 'seen' on refresh.
    """
    from okuro.db import get_db

    db = get_db()
    existed = db.fetchone(
        "SELECT 1 FROM model_discoveries WHERE catalog_id = ?",
        (s.catalog_id,),
    )
    db.execute(
        """
        INSERT INTO model_discoveries (
            catalog_id, display_name, modality, min_vram_gb, fit_gpu, score,
            rationale, use_case, caveats, source_url, researched,
            commercial_status, commercial_allowed, license_id, status,
            evidence, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?,
                  datetime('now'), datetime('now'))
        ON CONFLICT(catalog_id) DO UPDATE SET
            display_name = excluded.display_name,
            modality     = excluded.modality,
            min_vram_gb  = excluded.min_vram_gb,
            fit_gpu      = excluded.fit_gpu,
            score        = excluded.score,
            rationale    = excluded.rationale,
            use_case     = excluded.use_case,
            caveats      = excluded.caveats,
            source_url   = excluded.source_url,
            researched   = excluded.researched,
            commercial_status  = excluded.commercial_status,
            commercial_allowed = excluded.commercial_allowed,
            license_id         = excluded.license_id,
            evidence     = excluded.evidence,
            last_seen_at = datetime('now')
        """,
        (
            s.catalog_id,
            s.display_name,
            s.modality,
            s.min_vram_gb,
            s.fit_gpu,
            s.score,
            s.rationale,
            s.use_case,
            s.caveats,
            s.source_url,
            1 if s.researched else 0,
            getattr(s, "commercial_status", "unknown"),
            1 if getattr(s, "commercial_allowed", False) else 0,
            getattr(s, "license_id", None),
            json.dumps(s.to_evidence()),
        ),
    )
    return "seen" if existed else "new"


def list_discoveries(
    *,
    status: Optional[str] = None,
    query: Optional[str] = None,
    include_dismissed: bool = False,
    limit: int = 100,
) -> list[dict]:
    """Discoveries for the Discover tab.

    Default (no status): every row except ``dismissed`` (unless
    ``include_dismissed``), newest-and-highest-signal first — ``new`` models
    on top, then by score, then most-recently-seen. ``query`` substring-matches
    display_name / catalog_id / use_case so the tab's search box can filter the
    durable list without a live network round-trip.
    """
    from okuro.db import get_db

    clauses: list[str] = []
    params: list[Any] = []

    if status is not None:
        if status not in _VALID_STATUS:
            raise ValueError(f"status must be one of {_VALID_STATUS}")
        clauses.append("status = ?")
        params.append(status)
    elif not include_dismissed:
        clauses.append("status != 'dismissed'")

    if query and query.strip():
        like = f"%{query.strip().lower()}%"
        clauses.append(
            "(lower(display_name) LIKE ? OR lower(catalog_id) LIKE ? "
            "OR lower(COALESCE(use_case, '')) LIKE ?)"
        )
        params.extend([like, like, like])

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    rows = get_db().fetchall(
        f"""
        SELECT {", ".join(_COLS)}
        FROM model_discoveries
        {where}
        ORDER BY (status = 'new') DESC, score DESC, last_seen_at DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return [_shape(r) for r in rows]


def set_status(catalog_id: str, status: str) -> bool:
    """Update a discovery's lifecycle status. Returns False if unknown id."""
    if status not in _VALID_STATUS:
        raise ValueError(f"status must be one of {_VALID_STATUS}")
    from okuro.db import get_db

    cur = get_db().execute(
        "UPDATE model_discoveries SET status = ? WHERE catalog_id = ?",
        (status, catalog_id),
    )
    return bool(getattr(cur, "rowcount", 0))


def mark_installed(catalog_id: str) -> bool:
    """Flag a discovery installed — called when a pull completes."""
    return set_status(catalog_id, "installed")


def _shape(row: dict) -> dict:
    """DB row → API dict (bool coercion, no evidence blob in the list)."""
    out = dict(row)
    out["researched"] = bool(out.get("researched"))
    if "commercial_allowed" in out and out["commercial_allowed"] is not None:
        out["commercial_allowed"] = bool(out["commercial_allowed"])
    return out


__all__ = [
    "upsert_discovery",
    "list_discoveries",
    "set_status",
    "mark_installed",
]
