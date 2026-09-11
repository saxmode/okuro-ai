# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Pure salience scorer for the unified inbox — no DB, no I/O.
# index: constants | def salience | normalizers | def source_trust | def age_hours
# AGENT_HEADER_END -->
"""Pure salience scorer for the unified inbox.

No DB access, no I/O — every function here is a pure transform so the
ranking logic is unit-testable in isolation and identical between the
daemon reducer and any future caller.

Salience model::

    salience = importance * TYPE_WEIGHT[kind] * source_trust
               / (age_hours + AGE_SCALE_HOURS) ** GRAVITY[kind]

- ``importance`` ∈ [0, 1]: normalized producer importance (priority,
  urgency, severity, readiness, or a thought heuristic).
- ``TYPE_WEIGHT``: how much the kind matters intrinsically.
- ``source_trust`` ∈ [0, 1]: trust in the producer of the row.
- ``GRAVITY``: per-kind age-decay exponent. Higher gravity = decays
  faster, so transient kinds (signals) and noisy kinds (research,
  forgotten) sink quickly while durable kinds (continue, task) persist.
- ``AGE_SCALE_HOURS``: the timescale the ranking thinks in. GRAVITY sets the
  kinds' decay RATIOS; this sets their UNIT. Half-life for a kind is
  ``AGE_SCALE_HOURS * (2**(1/GRAVITY[kind]) - 1)``.
"""

from __future__ import annotations

from datetime import datetime, timezone

# How much each kind matters intrinsically (multiplicative).
TYPE_WEIGHT: dict[str, float] = {
    "continue": 1.0,
    # 'note' — a commitment the user wrote in their own words, usually naming a
    # person and a date. The highest-intent producer in the system: it is the
    # user talking, not an agent inferring. Weighted above every derived kind
    # and below only 'continue' (near-finished work already in flight).
    "note": 0.95,
    "reminder": 0.95,
    "commitment": 0.9,
    "task": 0.8,
    "saved": 0.8,
    "signal": 0.6,
    "forgotten": 0.4,
    "research": 0.3,
}

# Per-kind age-decay exponent. Larger = faster decay with age.
GRAVITY: dict[str, float] = {
    "continue": 0.6,
    # Slow decay: a commitment to a colleague does not stop mattering because
    # it is a week old — if anything it matters more. Matches 'saved'/'continue'
    # rather than the fast-sinking derived kinds.
    "note": 0.6,
    "commitment": 0.9,
    "task": 1.0,
    "saved": 0.6,
    "reminder": 1.0,
    "signal": 1.2,
    "research": 1.8,
    "forgotten": 1.8,
}

# Severity → importance lookup (signals).
_SEVERITY_IMPORTANCE: dict[str, float] = {
    "info": 0.3,
    "warn": 0.6,
    "crit": 1.0,
}

# Source/kind → trust lookup. Resolved by source_trust() with fallbacks.
_SOURCE_TRUST: dict[str, float] = {
    "user": 1.0,
    "agent": 0.7,
    "advisor": 0.8,
    "continuation": 0.8,
    "commitment": 0.8,
    "proactive": 0.6,
    "thought": 0.5,
    "ingress": 0.4,
    "scraper": 0.4,
    # User-curated saves (e.g. TikToks the user actively saved, surfaced by a
    # scanner). Higher than raw ingress: the save IS a signal of intent.
    "saved": 0.7,
    # Lifted from the user's own note. Trusted just below a hand-typed todo
    # ('user', 1.0): the words are his, but an extractor chose what to lift —
    # so it carries the extractor's error rate, not his.
    "note": 0.9,
    # Same provenance, different spelling. notes_extract routes todos under the
    # kind 'note' but writes signals with source='notes' (signals.py's CHECK
    # vocabulary), so the plural missed this table and 986 open signals resolved
    # to the 0.5 default — a 1.8x under-trust on the user's own words.
    "notes": 0.9,
}

_DEFAULT_TYPE_WEIGHT = 0.3
_DEFAULT_GRAVITY = 1.0
_DEFAULT_SOURCE_TRUST = 0.5

# The age softening constant — and, in practice, the DECAY TIMESCALE of the
# whole inbox. It is not a divide-by-zero guard; it sets the unit of time the
# ranking thinks in.
#
# Half-life for a kind is `AGE_SCALE_HOURS * (2**(1/gravity) - 1)`, so the
# constant scales every half-life linearly. At the original 2.0 the inbox
# thought in HOURS:
#
#     research/forgotten 56 min · signal 1.6h · task/reminder 2.0h ·
#     commitment 2.3h · continue/note/saved 4.3h
#
# i.e. a commitment to a colleague was worth half as much after lunch. That is
# a news feed's timescale, not a work list's, and it is why age buried
# importance: over three days the age term moves ~76x while the widest
# importance spread (info 0.3 → crit 1.0) moves 3.3x. Importance could never
# win. Measured 2026-07-15: a fresh trivial `info` signal outranked a
# three-day-old `crit` by 23x, and 42-day incumbents scored 0.000052 — roughly
# 400 half-lives down, which is also why 39% of the visible inbox rendered a
# 0% salience bar.
#
# At 48.0 the same gravities read in DAYS — research 22.5h, signal 1.6d,
# task 2.0d, commitment 2.3d, note 4.3d — and the three-day-old crit correctly
# beats the fresh info. Every GRAVITY relationship is preserved (research still
# decays fastest, notes slowest); only the unit changes.
#
# Tuning note: raising this flattens the ranking toward pure importance;
# lowering it flattens toward pure recency. It is the single strongest lever in
# the scorer — change it deliberately, and re-read the half-life table above.
AGE_SCALE_HOURS = 48.0


# ── salience ──────────────────────────────────────────────────────────


def salience(
    importance: float,
    kind: str,
    source_trust: float,
    age_hours: float,
) -> float:
    """Compute salience for one row.

    Strictly decreasing in ``age_hours`` (gravity > 0). Guards a negative
    age (clock skew) to 0 so the denominator stays ≥ AGE_SCALE_HOURS**gravity.

    ``AGE_SCALE_HOURS`` sets how fast the whole inbox forgets — see its
    definition for the half-life table it implies.
    """
    tw = TYPE_WEIGHT.get(kind, _DEFAULT_TYPE_WEIGHT)
    g = GRAVITY.get(kind, _DEFAULT_GRAVITY)
    age = age_hours if age_hours and age_hours > 0 else 0.0
    denom = (age + AGE_SCALE_HOURS) ** g
    return importance * tw * source_trust / denom


# ── importance normalizers ────────────────────────────────────────────


def priority_to_importance(p: int | float | None) -> float:
    """Todo priority (1-5) → importance. Missing → 0."""
    if p is None:
        return 0.0
    return float(p) / 5.0


def urgency_to_importance(u: int | float | None) -> float:
    """Reminder / suggestion urgency (1-5) → importance. Missing → 0."""
    if u is None:
        return 0.0
    return float(u) / 5.0


def severity_to_importance(severity: str | None) -> float:
    """Signal severity (info/warn/crit) → importance. Unknown → 0.3."""
    if not severity:
        return _SEVERITY_IMPORTANCE["info"]
    return _SEVERITY_IMPORTANCE.get(severity.lower(), _SEVERITY_IMPORTANCE["info"])


def readiness_to_importance(pct: int | float | None) -> float:
    """Continuation readiness_percent (0-100) → importance. Missing → 0."""
    if pct is None:
        return 0.0
    return float(pct) / 100.0


def thought_importance(has_action_items: bool) -> float:
    """Thought importance heuristic: 0.5 with action items, else 0.35."""
    return 0.5 if has_action_items else 0.35


# ── source trust ──────────────────────────────────────────────────────


def source_trust(source: str | None, kind: str | None = None) -> float:
    """Resolve trust for a producer.

    Resolution order:
      1. exact ``source`` match in the trust table,
      2. exact ``kind`` match (e.g. kind='continue' maps via
         'continuation' synonym below),
      3. default 0.5.
    """
    if source:
        s = source.lower()
        if s in _SOURCE_TRUST:
            return _SOURCE_TRUST[s]
        # ingress sub-sources (e.g. tiktok scraper) are still ingress-trust.
        if s.startswith("ingress") or "scraper" in s:
            return _SOURCE_TRUST["ingress"]
    if kind:
        k = kind.lower()
        if k == "continue":
            return _SOURCE_TRUST["continuation"]
        if k in _SOURCE_TRUST:
            return _SOURCE_TRUST[k]
    return _DEFAULT_SOURCE_TRUST


# ── age ───────────────────────────────────────────────────────────────


def age_hours(anchor: str | None, now: datetime | None = None) -> float:
    """Hours between an ISO/sqlite-datetime TEXT anchor and ``now`` (UTC).

    Parses the common sqlite ``'YYYY-MM-DD HH:MM:SS'`` and ISO ``'T'``
    forms (trailing ``Z`` tolerated). Any NULL / unparseable / future
    anchor returns 0.0 so a bad timestamp never crashes or inflates the
    denominator unfairly.
    """
    if not anchor:
        return 0.0
    # Stored anchors are naive UTC ('datetime(now)'); compare in UTC.
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    text = str(anchor).strip().replace("T", " ").replace("Z", "").strip()
    # Drop fractional seconds / timezone offset tail if present.
    if "." in text:
        text = text.split(".", 1)[0]
    if "+" in text:
        text = text.split("+", 1)[0].strip()
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if parsed is None:
        return 0.0
    delta = (now - parsed).total_seconds() / 3600.0
    return delta if delta > 0 else 0.0
