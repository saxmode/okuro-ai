# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Pre-write deterministic self-check. Lets a subagent run the
#   reviewer's own text checks against a DRAFT body before artifact_write,
#   so a mechanically-detectable defect costs a tool call instead of a full
#   review round.
# index:
#   imports
#   def _load_subtask_context
#   def precheck_deliverable
# AGENT_HEADER_END -->
"""Pre-write deterministic self-check (L4).

Measured motivation: 55.9% of round-1 review verdicts are FAIL, and a round
costs a mean 162s of wall clock (see ``review_loop_stats``). A large share of
round-1 load-bearing findings are mechanically detectable from the deliverable
text alone — missing acceptance-criteria evidence, superseded terminology, a
numbers ledger that does not reconcile, a secret name outside the active set.
Every one of those currently costs a full round: write, review, publish, wake,
patch, re-review.

This module runs those checks BEFORE the write, against text the author still
has in hand.

**It reuses the reviewer's own generators and runners — it does not
reimplement them.** That is the load-bearing design property: a private copy
of the rules would drift from the reviewer and give the author false comfort,
which is worse than no check at all. The draft body is passed through the same
``{locus, text, subtask_id}`` inline-source channel the reviewer already uses
for brain-only deliverables, and the acceptance-criteria scan calls the same
pure shape function the ``ac_evidence_shape`` runner calls.

Only checks that can be answered from TEXT are run. ``file_exists``,
``link_integrity`` and ``schema_assert`` are disk-bound and meaningless before
the artifact exists — their absence is reported in ``checks_skipped`` rather
than silently implied to have passed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Checks this pre-flight cannot answer from a draft body. Reported, never
# silently omitted — an author must not read "no findings" as "will pass".
_DISK_BOUND_CHECKS = ("file_exists", "link_integrity", "schema_assert")

# Every check this pre-flight can answer from text alone. The accounting for
# checks_run / checks_skipped is derived from THIS set rather than from what
# the generators emitted, so a check can never fall out of both lists.
_TEXT_ANSWERABLE = (
    "term_consistency", "numeric_consistency", "secret_name",
    "ac_evidence_shape",
)

_DRAFT_LOCUS = "draft://pending-artifact_write"


def _load_subtask_context(
    task_id: str,
    subtask_id: str,
    tasks_dir: Optional[Path] = None,
) -> dict:
    """Best-effort lookup of the plan context the checks need.

    Returns ``{"acceptance_criteria": [...], "adrs": [...], "found": bool}``.
    Degrades to empties on any failure: a pre-flight that raises because the
    plan is mid-write is worse than one that runs the checks it can.
    """
    out: dict[str, Any] = {"acceptance_criteria": [], "adrs": [], "found": False}
    if not task_id or not subtask_id:
        return out
    try:
        from okuro.orchestrator.reviewer.deterministic import _resolve_tasks_dir
        from okuro.orchestrator.state import load_task

        task = load_task(task_id, _resolve_tasks_dir(tasks_dir))
    except Exception as exc:  # noqa: BLE001
        log.info("precheck: could not load task %s (%r)", task_id, exc)
        return out

    out["adrs"] = list(getattr(task, "adrs", None) or [])
    for phase in getattr(task, "phases", None) or []:
        for st in getattr(phase, "subtasks", None) or []:
            if str(getattr(st, "id", "")) == str(subtask_id):
                out["acceptance_criteria"] = list(
                    getattr(st, "acceptance_criteria", None) or []
                )
                out["found"] = True
                return out
    return out


def precheck_deliverable(
    *,
    task_id: str,
    subtask_id: str,
    body: str,
    kind: Optional[str] = None,
    acceptance_criteria: Optional[list[str]] = None,
    tasks_dir: Optional[Path] = None,
) -> dict:
    """Run the reviewer's text checks against a draft body.

    ``kind`` is the artifact kind about to be written (report / plan /
    evidence). It resolves the SAME ``ReviewProfile`` the reviewer resolves,
    which drops ``ac_evidence_shape`` and ``secret_name`` for plan/report —
    those deliverables carry no executable evidence and no secret usage, so
    the reviewer never applies them. Without this the pre-check would raise
    findings the reviewer would never raise, and could report
    ``would_fail_review`` on a report the reviewer would pass. Omitting
    ``kind`` keeps the strict code-shaped default, matching the reviewer.

    ``acceptance_criteria`` overrides what the plan declares — useful for a
    caller checking a deliverable whose ACs are not (yet) in plan.yaml. When
    omitted the plan is consulted.

    Returns::

        {"ok": bool,                  # no load-bearing findings
         "would_fail_review": bool,   # a load-bearing finding would
                                      # short-circuit the LLM stages
         "findings": [...],           # reviewer-shaped findings
         "checks_run": [...],
         "checks_skipped": [...],     # with the reason, never silent
         "acceptance_criteria_seen": int}
    """
    from okuro.orchestrator.reviewer.deterministic import (
        _check_ac_evidence_shape,
        _gen_numeric_consistency,
        _gen_secret_name,
        _gen_term_consistency,
        _known_secret_names,
        _superseded_terms_from_adrs,
        _superseded_terms_from_events,
        run_checks,
    )

    body = body or ""
    ctx = _load_subtask_context(task_id, subtask_id, tasks_dir)
    acs = list(
        acceptance_criteria
        if acceptance_criteria is not None
        else ctx["acceptance_criteria"]
    )

    events: list[dict] = []
    try:
        from okuro.sense.task_events import list_events

        events = list_events(task_id=task_id, limit=500)
    except Exception as exc:  # noqa: BLE001
        log.info("precheck: event log unavailable for %s (%r)", task_id, exc)

    adrs = ctx["adrs"]
    # The draft, presented through the SAME inline-source channel the reviewer
    # uses for brain-only deliverables — so the runners cannot tell the
    # difference between this and a written artifact.
    sources = [{
        "locus": _DRAFT_LOCUS,
        "text": body,
        "subtask_id": subtask_id,
    }]

    # Resolve the SAME profile the reviewer will, and filter identically to
    # generate_checks. A pre-check stricter than the reviewer is a false
    # blocker; a pre-check looser than it is false comfort.
    from okuro.orchestrator.reviewer.profiles import resolve_profile

    profile = resolve_profile(kind)
    allowed = profile.det_checks

    sup_pairs = (
        _superseded_terms_from_events(events) + _superseded_terms_from_adrs(adrs)
    )
    checks = []
    checks.extend(_gen_term_consistency(sup_pairs, [], sources))
    checks.extend(_gen_numeric_consistency([], sources))
    checks.extend(_gen_secret_name([], _known_secret_names(adrs, events), sources))
    # Derived from what this pre-check is CAPABLE of, not from what the
    # generators happened to emit. Deriving it from the emitted checks hid a
    # kind twice over: secret_name with no known-secret set generates nothing,
    # so on a `report` (which drops it by profile anyway) it appeared in
    # neither checks_run nor checks_skipped and vanished silently.
    dropped_by_profile = sorted(k for k in _TEXT_ANSWERABLE if k not in allowed)
    checks = [c for c in checks if c.kind in allowed]
    ac_applies = "ac_evidence_shape" in allowed

    # A check the profile ALLOWS but whose generator produced nothing did not
    # run, and silence would read as "ran and found nothing". term_consistency
    # emits nothing without a superseded term; secret_name emits nothing
    # without a known secret set — and both of those inputs come from the task
    # ADRs + event log, which are exactly what is thin or absent early in a
    # task. That is when an author most needs to know the check was not a
    # verdict.
    _NO_INPUT_REASONS = {
        "term_consistency": (
            "no superseded terms declared for this task yet (no supersedes "
            "events, no ADR replacements) — nothing to drift from"
        ),
        "secret_name": (
            "no known secret names declared for this task yet (none named in "
            "an ADR or decision/contract event) — cannot tell a typo from a "
            "new secret"
        ),
    }
    generated = {c.kind for c in checks}
    no_input = sorted(
        k for k in _NO_INPUT_REASONS if k in allowed and k not in generated
    )

    findings: list[dict] = []
    for result in run_checks(checks):
        if result.passed:
            continue
        for f in result.findings:
            f = dict(f)
            f.setdefault("severity", result.check.severity)
            f.setdefault("check_kind", result.check.kind)
            f.setdefault("origin", result.check.origin)
            findings.append(f)

    # ac_evidence_shape: the runner reads the brain, so call the pure shape
    # function it delegates to. Severity mirrors the reviewer's own — this
    # check is 'infra_error' there, deliberately excluded from load-bearing
    # failures, and misreporting it as blocking here would train authors to
    # distrust the tool.
    if acs and ac_applies:
        for f in _check_ac_evidence_shape(body, acs):
            f = dict(f)
            f.setdefault("severity", "infra_error")
            f.setdefault("check_kind", "ac_evidence_shape")
            f.setdefault("subtask_id", subtask_id)
            findings.append(f)

    load_bearing = [
        f for f in findings
        if str(f.get("severity", "")).lower() == "load_bearing"
    ]

    skipped = [
        {"kind": k, "reason": "needs the artifact on disk — run after write"}
        for k in _DISK_BOUND_CHECKS
    ]
    for k in dropped_by_profile:
        skipped.append({
            "kind": k,
            "reason": (
                f"the {kind!r} review profile does not apply this check — "
                "the reviewer will not raise it either"
            ),
        })
    for k in no_input:
        skipped.append({"kind": k, "reason": _NO_INPUT_REASONS[k]})
    if not acs and ac_applies:
        skipped.append({
            "kind": "ac_evidence_shape",
            "reason": (
                "no acceptance_criteria found for this subtask — pass them "
                "explicitly if the plan does not declare them"
            ),
        })
    if not ctx["found"]:
        skipped.append({
            "kind": "_plan_context",
            "reason": (
                f"subtask {subtask_id} not found in plan for {task_id} — "
                "ADR-derived term and secret checks may be incomplete"
            ),
        })

    return {
        "ok": not load_bearing,
        "would_fail_review": bool(load_bearing),
        "findings": findings,
        "load_bearing_count": len(load_bearing),
        "checks_run": sorted(
            {c.kind for c in checks}
            | ({"ac_evidence_shape"} if (acs and ac_applies) else set())
        ),
        "checks_skipped": skipped,
        "acceptance_criteria_seen": len(acs),
        "profile_kind": kind,
    }
