# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Resonance ACTUALITY — is the grounding evidence still current? Flags
#   stale evidence artifacts by age × source volatility, and re-researches the
#   stale WEB sources so a deck never renders on rotten facts. The freshness
#   counterpart to the gap engine (which checks COVERAGE, not currency).
# index:
#   _source_ref / _classify / _age_days
#   def check_actuality
#   def refresh_stale
# AGENT_HEADER_END -->
"""Actuality check — keeps the Resonance grounding current.

The gap engine asks "is anything MISSING". This asks "is anything STALE". Every
ingested evidence artifact carries a ``SOURCE:`` header + a ``created_at``; a
source's staleness is age measured against its VOLATILITY class:

- ``web``   — web-research findings / URLs. Facts churn — default stale > 30 days.
- ``doc``   — an uploaded document. Ages slowly — default stale > 180 days.
- ``tacit`` — an interview answer. Intent/priorities don't decay by the clock —
  never stale by age (a re-interview is a human decision, not a timer).

``refresh_stale`` re-researches the stale WEB sources (their question is recoverable
from the ``web-research:<question>`` source_ref) and ingests fresh findings.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("okuro.resonance.actuality")

# Volatility class → staleness threshold in days. None = never stale by age.
_DEFAULT_MAX_AGE: dict[str, Optional[int]] = {"web": 30, "doc": 180, "tacit": None}
_WEB_PREFIX = "web-research:"


def _source_ref(body: str) -> Optional[str]:
    """Pull the ``SOURCE: <ref>`` header that ingest_document writes."""
    if not body:
        return None
    m = re.match(r"\s*SOURCE:\s*(.+)", body)
    return m.group(1).strip() if m else None


def _classify(source_ref: Optional[str]) -> str:
    """Volatility class from the source_ref."""
    if not source_ref:
        return "doc"
    s = source_ref.strip()
    if s.startswith(_WEB_PREFIX) or re.match(r"https?://", s):
        return "web"
    if s == "interview":
        return "tacit"
    return "doc"


def _age_days(created_at: Optional[str]) -> Optional[float]:
    """Whole-ish days since ``created_at`` (sqlite 'YYYY-MM-DD HH:MM:SS' UTC)."""
    if not created_at:
        return None
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def check_actuality(
    source_artifact_ids: list[str],
    *,
    max_age: Optional[dict[str, Optional[int]]] = None,
) -> dict[str, Any]:
    """Flag stale grounding evidence. Returns
    ``{checked, stale:[{artifact_id, title, source_ref, class, age_days,
    threshold_days}], fresh, note}``. ``max_age`` overrides per-class thresholds."""
    from okuro.sense.artifacts import artifact_get

    thresholds = {**_DEFAULT_MAX_AGE, **(max_age or {})}
    stale: list[dict[str, Any]] = []
    fresh = 0
    checked = 0
    for aid in source_artifact_ids or []:
        art = artifact_get(aid, include_body=True)
        if not art:
            continue
        checked += 1
        src = _source_ref(art.get("body") or "")
        cls = _classify(src)
        limit = thresholds.get(cls)
        age = _age_days(art.get("created_at"))
        if limit is None or age is None or age <= limit:
            fresh += 1
            continue
        stale.append({
            "artifact_id": aid,
            "title": art.get("title"),
            "source_ref": src,
            "class": cls,
            "age_days": round(age, 1),
            "threshold_days": limit,
        })
    note = ("all current" if not stale else
            f"{len(stale)} stale source(s) — refresh before rendering")
    return {"checked": checked, "stale": stale, "fresh": fresh, "note": note}


def refresh_stale(
    source_artifact_ids: list[str],
    *,
    project: Optional[str] = None,
    provider: Optional[str] = None,
    max_age: Optional[dict[str, Optional[int]]] = None,
    web_timeout: int = 240,
) -> dict[str, Any]:
    """Re-research the stale WEB sources and ingest fresh findings.

    Only ``web`` sources are auto-refreshable (their question is recoverable from
    the ``web-research:<question>`` source_ref). ``doc``/``tacit`` staleness is
    surfaced by :func:`check_actuality` but needs a human (re-upload / re-interview).
    Returns ``{refreshed:[{question, old_artifact_id, new_artifact_id,
    claims_added}], new_artifact_ids, skipped}``."""
    from .research import research_question

    report = check_actuality(source_artifact_ids, max_age=max_age)
    refreshed: list[dict[str, Any]] = []
    new_ids: list[str] = []
    skipped = 0
    for s in report["stale"]:
        src = s.get("source_ref") or ""
        if s["class"] != "web" or not src.startswith(_WEB_PREFIX):
            skipped += 1  # doc / bare-URL → can't reconstruct a query to re-run
            continue
        question = src[len(_WEB_PREFIX):].strip()
        man = research_question(question, project=project, provider=provider,
                                web_timeout=web_timeout)
        if not man:
            skipped += 1
            continue
        refreshed.append({"question": question, "old_artifact_id": s["artifact_id"],
                          "new_artifact_id": man["artifact_id"],
                          "claims_added": man.get("claims_added", 0)})
        new_ids.append(man["artifact_id"])
    return {"refreshed": refreshed, "new_artifact_ids": new_ids, "skipped": skipped}
