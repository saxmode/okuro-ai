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
    # migration 152 — what LINE this is and how it relates to what is installed
    "family", "version", "params_total_b", "params_active_b", "quant",
    "variant_tags", "relation", "relation_target", "relation_confidence",
    # migration 153 — which wanted bucket, and is it worth a look
    "category", "interesting", "interesting_why",
)

#: Columns migrations 152 and 153 added that the scan writes on every upsert.
#: Kept as a list so the INSERT, the ON CONFLICT clause and the value tuple
#: cannot drift apart — the shape of bug where a column is written on insert
#: and silently never refreshed afterwards.
_LINEAGE_COLS = (
    "family", "version", "params_total_b", "params_active_b", "quant",
    "variant_tags", "relation", "relation_target", "relation_confidence",
    "category", "interesting", "interesting_why",
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
    lineage_updates = ",\n            ".join(
        f"{c} = excluded.{c}" for c in _LINEAGE_COLS)
    db.execute(
        f"""
        INSERT INTO model_discoveries (
            catalog_id, display_name, modality, min_vram_gb, fit_gpu, score,
            rationale, use_case, caveats, source_url, researched,
            commercial_status, commercial_allowed, license_id, status,
            evidence, first_seen_at, last_seen_at,
            {", ".join(_LINEAGE_COLS)}
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?,
                  datetime('now'), datetime('now'),
                  {", ".join("?" for _ in _LINEAGE_COLS)})
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
            {lineage_updates},
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
            getattr(s, "family", None),
            getattr(s, "version", None),
            getattr(s, "params_total_b", None),
            getattr(s, "params_active_b", None),
            getattr(s, "quant", None),
            json.dumps(list(getattr(s, "variant_tags", None) or [])),
            getattr(s, "relation", None),
            getattr(s, "relation_target", None),
            getattr(s, "relation_confidence", None),
            getattr(s, "category", None),
            _tri(getattr(s, "interesting", None)),
            getattr(s, "interesting_why", None) or None,
        ),
    )
    return "seen" if existed else "new"


def _tri(value) -> Optional[int]:
    """Three-state boolean → column value. NULL means the pass has not judged
    this row, which is a different fact from 0 — the rule migrations 152 and
    153 both set for `relation` and `interesting`."""
    return None if value is None else (1 if value else 0)


def mark_installed_if_new(catalog_id: str) -> bool:
    """Flag a discovery installed, but only from ``new``.

    A candidate the lineage pass finds IDENTICAL to an installed unit is not a
    discovery — it is the thing already on disk, which the exact-catalog_id
    dedup could never see because the release and the file are named
    differently. Only ``new`` is overwritten: ``acknowledged`` and
    ``dismissed`` are the user's own words about this row, and a scan never
    reverts them.
    """
    from okuro.db import get_db

    cur = get_db().execute(
        "UPDATE model_discoveries SET status = 'installed' "
        "WHERE catalog_id = ? AND status = 'new'",
        (catalog_id,),
    )
    return bool(getattr(cur, "rowcount", 0))


def record_category_scan(row: dict) -> None:
    """Record what one wanted category found this run — including nothing.

    An empty Discover list is ambiguous between "nothing was released" and
    "the scanner for this bucket is broken", and those need different actions.
    One row per category, replaced each run.
    """
    from okuro.db import get_db

    get_db().execute(
        """
        INSERT INTO model_category_scans (
            category, source, selector, fetched, passed_gate, published,
            interesting, zero_result, note, scanned_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(category) DO UPDATE SET
            source      = excluded.source,
            selector    = excluded.selector,
            fetched     = excluded.fetched,
            passed_gate = excluded.passed_gate,
            published   = excluded.published,
            interesting = excluded.interesting,
            zero_result = excluded.zero_result,
            note        = excluded.note,
            scanned_at  = datetime('now')
        """,
        (
            str(row.get("category") or ""),
            str(row.get("source") or ""),
            row.get("selector"),
            int(row.get("fetched") or 0),
            int(row.get("passed_gate") or 0),
            int(row.get("published") or 0),
            int(row.get("interesting") or 0),
            1 if row.get("zero_result") else 0,
            row.get("note"),
        ),
    )


def list_category_scans() -> list[dict]:
    """The per-category ledger of the last run, in declared order where known."""
    from okuro.db import get_db

    try:
        rows = get_db().fetchall(
            "SELECT category, source, selector, fetched, passed_gate, published, "
            "interesting, zero_result, note, scanned_at FROM model_category_scans")
    except Exception:
        return []
    out = [dict(r) for r in rows]
    for r in out:
        r["zero_result"] = bool(r.get("zero_result"))
    try:
        from .discovery import categories

        order = {c["name"]: i for i, c in enumerate(categories())}
    except Exception:
        order = {}
    out.sort(key=lambda r: (order.get(r["category"], 99), r["category"]))
    return out


def list_discoveries(
    *,
    status: Optional[str] = None,
    query: Optional[str] = None,
    include_dismissed: bool = False,
    limit: int = 100,
    category: Optional[str] = None,
    variants: str = "hide",
    interesting_only: bool = False,
) -> list[dict]:
    """Discoveries for the Discover tab.

    Default (no status): every row except ``dismissed`` (unless
    ``include_dismissed``), newest-and-highest-signal first — ``new`` models
    on top, then by score, then most-recently-seen. ``query`` substring-matches
    display_name / catalog_id / use_case so the tab's search box can filter the
    durable list without a live network round-trip.

    ``variants='hide'`` (the default) omits rows carrying an identity-variant
    tag — abliterated, uncensored, nsfw, roleplay. Ruling 3: those rows are
    TAGGED and kept, and this is the toggle that decides whether they are on
    screen. ``variants='show'`` returns everything. Nothing is ever deleted, so
    the two answers differ only in what is rendered.
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

    if category:
        clauses.append("category = ?")
        params.append(category)

    if interesting_only:
        clauses.append("interesting = 1")

    if str(variants or "hide").lower() != "show":
        # A row is hidden when its tag array contains one of the identity
        # variants. LIKE over the JSON array is exact enough because the tags
        # are a closed vocabulary written by one writer, quoted, and none is a
        # substring of another. A NULL array (never scanned by the P4 pass) is
        # kept: absence of a tag is not the presence of one.
        from .lineage import IDENTITY_VARIANT_TAGS

        for tag in sorted(IDENTITY_VARIANT_TAGS):
            clauses.append("COALESCE(variant_tags, '[]') NOT LIKE ?")
            params.append(f'%"{tag}"%')

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
    # `interesting` stays three-state: None means the P4 pass has not judged
    # this row, which the UI renders differently from a judged "no".
    if out.get("interesting") is not None:
        out["interesting"] = bool(out["interesting"])
    raw = out.get("variant_tags")
    if isinstance(raw, str):
        try:
            out["variant_tags"] = json.loads(raw)
        except (ValueError, TypeError):
            out["variant_tags"] = []
    elif raw is None:
        out["variant_tags"] = []
    from .lineage import IDENTITY_VARIANT_TAGS

    out["identity_variants"] = [t for t in out["variant_tags"]
                                if t in IDENTITY_VARIANT_TAGS]
    return out


__all__ = [
    "upsert_discovery",
    "list_discoveries",
    "list_category_scans",
    "mark_installed_if_new",
    "record_category_scan",
    "set_status",
    "mark_installed",
]
