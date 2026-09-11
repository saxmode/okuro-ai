# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Per-artifact-kind review profiles. The reviewer historically judged
#   every deliverable by one code-shaped bar (bash-evidence per AC, all
#   deterministic checks). A `plan`/`report` artifact (architecture proposal,
#   audit — no code, no executable ACs) cannot produce captured stdout, so it
#   FAILed forever and looped to CAP. The profile keyed by artifact kind makes
#   the bar a function of WHAT the deliverable is — one registry, resolved once,
#   read by the three review stages (deterministic / critic / verdict).
# index: ReviewProfile | resolve_profile | resolve_phase_profile
# AGENT_HEADER_END -->
"""Kind-aware review profiles (defect #1 + #2 fix)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

# The full deterministic Check-kind set (deterministic.generate_checks).
_ALL_DET_CHECKS = frozenset({
    "term_consistency", "file_exists", "link_integrity",
    "schema_assert", "secret_name", "ac_evidence_shape",
    # Phase B — recompute a ```numbers ledger deterministically. Harmless when
    # no ledger is present (passes), so it is safe in every profile; bites only
    # when a number-heavy deliverable declares totals that don't reconcile.
    "numeric_consistency",
})


@dataclass(frozen=True)
class ReviewProfile:
    """How a given artifact kind should be reviewed.

    det_checks       — deterministic Check kinds that apply; others dropped.
    critic_ac_mode   — "auto" keeps the legacy declared/none binary (code);
                       "conceptual" judges ACs against the body's reasoning,
                       NOT captured shell evidence (plan/report).
    allow_needs_user — may the verdict be NEEDS_USER when the deliverable
                       carries an unresolved open_question, routing to the
                       user instead of FAIL → CAP.
    """

    det_checks: frozenset
    critic_ac_mode: str
    allow_needs_user: bool


# Code / raw evidence — unchanged from legacy: full bar, executable AC mode.
_PROFILE_CODE = ReviewProfile(_ALL_DET_CHECKS, "auto", False)
# Pre-execution design docs — drop executable-evidence + secret-name checks,
# judge ACs conceptually, allow routing a genuine open question to the user.
_PROFILE_PLAN = ReviewProfile(
    _ALL_DET_CHECKS - {"ac_evidence_shape", "secret_name"}, "conceptual", True,
)
# Narrative synthesis — also drop schema_assert (no typed outputs to shape).
_PROFILE_REPORT = ReviewProfile(
    _ALL_DET_CHECKS - {"ac_evidence_shape", "secret_name", "schema_assert"},
    "conceptual", True,
)

_BY_KIND = {
    "plan": _PROFILE_PLAN,
    "report": _PROFILE_REPORT,
    "evidence": _PROFILE_CODE,
}

# Unknown / None / code — the strict default preserves legacy behaviour exactly.
DEFAULT_PROFILE = _PROFILE_CODE


def resolve_profile(kind: Optional[str]) -> ReviewProfile:
    """Profile for a single artifact kind. None/unknown → strict default."""
    if not kind:
        return DEFAULT_PROFILE
    return _BY_KIND.get(str(kind).lower(), DEFAULT_PROFILE)


def resolve_phase_profile(kinds: Optional[Iterable[str]]) -> ReviewProfile:
    """Strictest profile across a phase's deliverable kinds.

    If ANY deliverable is code/evidence/unknown, the strict default applies — a
    code subtask must never be relaxed because a sibling is a plan. Only when
    EVERY deliverable is plan/report does the relaxed bar apply; among those the
    one with the MOST checks wins (the stricter of plan vs report).
    """
    present = [k for k in (kinds or []) if k]
    if not present:
        return DEFAULT_PROFILE
    if any(str(k).lower() not in ("plan", "report") for k in present):
        return DEFAULT_PROFILE
    profiles = [resolve_profile(k) for k in present]
    return max(profiles, key=lambda p: len(p.det_checks))
