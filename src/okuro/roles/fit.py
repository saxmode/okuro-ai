# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Role fit analytics — five measured segments per role, plus fleet rollup.
# index:
#   imports
#   constants
#   def _gate_is_fence_aware
#   def rubric_version
#   def _grade_bodies
#   def _score_structure
#   def _score_tiers
#   def _score_size
#   def _is_filler
#   def _is_sourced
#   def _parse_ts
#   def _recency_factor
#   def _score_knowledge
#   def _score_hygiene
#   def compute_fit
#   def fit_summary
#   def fleet_fit
#   def knowledge_sourced_ratio_by_month
# AGENT_HEADER_END -->
"""Role fit analytics — what "how well tailored is this role" means, measured.

Five named segments, never one composite number. The composite hides which of
five different repairs a role needs; the five say it without opening the role.
Each segment is 0-100 and carries the list of concrete defects behind it, so
the UI can name a missing section rather than render a bar and stop.

Deliberately NOT inputs (measured uniform or too sparse to rank anything, see
the design plan's §3.7): ``last_maintained`` (0 NULL, the sweep resets the
clock with a no-change row, so green means "the cron ran"), ``sessions``
(17 of 103 non-zero), ``effort`` (103 of 103 = "medium"), ``learnings``
(counts the filler).

Two rulings bind this module and are enforced here, not assumed upstream:

**Q1** — the four persona sections stay GATING. ``designer.FULL_REQUIRED`` is
the structure rubric unchanged; this module never re-states or relaxes it, it
imports it.

**Q6** — FULL is an on-demand references tier. It is never inlined, so its
length is not a defect. The size segment scores lean and micro only and reports
FULL as ``references_chars``, a number with no verdict attached.

Everything here is pure: ``compute_fit`` takes the role row and its knowledge
rows and does no I/O, so it is testable without a store and cheap to run over
the whole fleet in one pass.

**Two invariants that are easy to break by improving this file.**

*A score must not move unless the measurement moved, and it must not stay
still when it did.* Every constant the scorer reads is hashed into
``rubric_version`` — the rubrics, the budgets, the filler marker, the legacy
strings, the canonical tiers, the recency curve, the suppression threshold and
the sourced definition. Adding a constant and not adding it there is the bug
this module is most likely to grow.

*A segment with nothing to measure reports ``None``, never a pass.* A role with
no lean and no micro is not a role with perfect sizing, and a role with no body
at all is not a role with perfect structure. ``None`` renders as N/A and is left
out of every mean.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone

from okuro.roles._time import parse_ts
from okuro.roles.designer import (
    FULL_REQUIRED,
    LEAN_REQUIRED,
    MICRO_REQUIRED_KEYS,
    REQUIRED_LABELS,
    validate_role_structure,
)

# ── Constants ────────────────────────────────────────────────────────

#: Per-grade character budgets. FULL carries one too, but it is reported and
#: never scored — see Q6 in the module docstring.
SIZE_BUDGETS: dict[str, int] = {"full": 17_300, "lean": 7_500, "micro": 2_500}

#: The grades whose length is a defect when exceeded. FULL is absent on purpose.
SCORED_GRADES: tuple[str, ...] = ("lean", "micro")

#: Substring that marks a knowledge row as sweep filler rather than a finding.
#: Matched case-insensitively. 386 of 844 live rows carried it before migration
#: 159 moved them; monthly counts ran 2/24/47/109/113/91 from April to
#: September 2026.
#:
#: **Must equal** ``okuro.roles.checks.NO_CHANGE_MARKER``, which is the routing
#: rule's home. It is restated rather than imported because this module is pure
#: — it takes rows and returns numbers, and ``checks`` opens the database at
#: import. A test asserts the two are identical, so the copy cannot drift
#: silently, and the value is hashed into ``rubric_version`` so a change to it
#: cannot re-baseline the fleet under an unchanged version string.
FILLER_MARKER = "no significant change"

#: How a knowledge row earns its place in the numerator. An EXPLICIT mode, not
#: a key sniffed off whatever the caller's SELECT happened to include.
#:
#: - ``url_claimed`` — the row carries a non-empty ``source_url``. That is a
#:   CLAIM the writing agent made, not proof anything was fetched. It is the
#:   only thing the store can answer today, so it is the default and the
#:   buckets are named ``claimed`` rather than ``verified``.
#: - ``fetch_verified`` — the row's ``fetch_verified`` column is true. Amendment
#:   A2's column, set only by code that stored the fetched body.
#:
#: **Switching the mode is not a config change, it is a migration.** Every one
#: of the 844 live rows predates the fetch store, so under ``fetch_verified``
#: they all read false and every role's knowledge segment drops to zero
#: overnight. Whoever turns it on owes a backfill decision first: either
#: backfill ``fetch_verified`` for rows whose URL can still be re-fetched and
#: matched, or scope the segment to rows created after the store existed. The
#: mode is part of ``rubric_version``, so the two eras never compare as if they
#: were the same measurement.
SOURCED_DEFINITIONS: tuple[str, ...] = ("url_claimed", "fetch_verified")
DEFAULT_SOURCED_DEFINITION = "url_claimed"

#: Bucket label per mode — the API ships this so the UI never has to guess
#: whether the number in front of it is a claim or a proof.
SOURCED_LABEL: dict[str, str] = {
    "url_claimed": "sourced (claimed)",
    "fetch_verified": "verified",
}

#: Strings that only appear in a role body because nobody rewrote it after the
#: thing they name was removed. ``knowledge/roles/`` is a filesystem path dead
#: since migration 155; the rest name retired tools, hosts and models.
LEGACY_STRINGS: tuple[str, ...] = (
    "knowledge/roles/",
    "tm-nightbird",
    "TM-Cortex",
    "start.md",
    "pingpong",
    "claude-sonnet-4",
)

#: Total legacy-string occurrences at which the hygiene legacy component
#: scores zero. Six is the number of tracked strings: a body carrying one
#: occurrence of each is as stale as this check can tell. Proportional rather
#: than all-or-nothing, so removing four of five hits is visible progress.
LEGACY_HITS_AT_ZERO = 6

#: What the hygiene segment SCORES. `tier` is deliberately absent — it is
#: reported as advisory. See `_score_hygiene`.
HYGIENE_SCORED_CHECKS: tuple[str, ...] = ("schedule", "legacy")

#: Recency curve for the knowledge segment: full credit inside
#: RECENCY_FULL_DAYS, decaying linearly to RECENCY_FLOOR at
#: RECENCY_FLOOR_DAYS and never below it.
RECENCY_FULL_DAYS = 30
RECENCY_FLOOR_DAYS = 180
RECENCY_FLOOR = 0.5

#: The five segments, in render order. Named here so the API, the UI and the
#: rollup cannot drift apart on spelling.
SEGMENTS: tuple[str, ...] = ("structure", "tiers", "size", "knowledge", "hygiene")

#: Required-label sets per grade, IMPORTED from designer.py rather than rebuilt
#: here. Used to tell a real missing-section label apart from
#: validate_role_structure's sentinel messages ("grade 'x' is empty", "micro is
#: not parseable YAML …"), which mean the grade failed wholesale rather than by
#: one section. Reconstructing the micro label format in this file was a
#: hand-copied string contract that nothing enforced: reword it in designer.py
#: and the copy here would match nothing, silently.
_REQUIRED_LABELS = REQUIRED_LABELS


def _canonical(value):
    """A JSON-ready shape whose serialisation does not depend on the process.

    ``repr`` was the previous encoding and it is not stable: a set or a
    frozenset reprs in hash order, which moves with ``PYTHONHASHSEED``, so two
    processes could hash the same rubric to two different versions. Sequences
    keep their declared order (it is meaningful), sets are sorted, mappings are
    emitted as sorted key/value pairs, and regex patterns reduce to their
    pattern string.
    """
    if isinstance(value, (set, frozenset)):
        return ["__set__", sorted(_canonical(v) for v in value)]
    if isinstance(value, dict):
        return ["__map__", [[str(k), _canonical(value[k])] for k in sorted(value)]]
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


#: A body whose ONLY persona marker sits inside a fenced output template —
#: the shape ``role-researcher`` had, and the shape that used to pass the
#: gate. Both halves of the probe are the same text; only the fence differs.
_FENCE_PROBE_FENCED = "## IDENTITY\n\nX\n\n```markdown\n**Archetype:** X\n```\n"
_FENCE_PROBE_PLAIN = "## IDENTITY\n\nX\n\n**Archetype:** X\n"


def _gate_is_fence_aware() -> bool:
    """Ask the gate whether a fenced marker still counts. Do not assume.

    Fence-awareness means the fenced body misses STRICTLY MORE required
    sections than the identical unfenced one. Measuring it this way needs no
    label hard-coded here and survives a rename of either.
    """
    fenced = validate_role_structure(
        {"full": _FENCE_PROBE_FENCED}).get("missing", {}).get("full") or []
    plain = validate_role_structure(
        {"full": _FENCE_PROBE_PLAIN}).get("missing", {}).get("full") or []
    return len(fenced) > len(plain)


def rubric_version(sourced_definition: str = DEFAULT_SOURCED_DEFINITION) -> str:
    """Short hash of EVERY constant the scorer reads.

    Amendment A5: a score may only be compared against another score taken
    under the same rubric. The first version of this hashed the structure and
    size constants only, which left a hole big enough to re-baseline the whole
    fleet under an unchanged version string — ``FILLER_MARKER``,
    ``LEGACY_STRINGS``, ``CANONICAL_TIERS``, the recency curve, the
    definition of "sourced" and the suppression threshold could all move and
    the version would not. A version that does not change when the measurement
    changes is worse than no version: it asserts comparability that is false.

    So the payload is every input, canonically encoded. Adding a constant to
    the scorer without adding it here is the failure this docstring exists to
    prevent.
    """
    from okuro.orchestrator.config import CANONICAL_TIERS

    payload = json.dumps(
        _canonical(
            {
                "full_required": FULL_REQUIRED,
                "lean_required": LEAN_REQUIRED,
                "micro_required_keys": MICRO_REQUIRED_KEYS,
                "required_labels": _REQUIRED_LABELS,
                "size_budgets": SIZE_BUDGETS,
                "scored_grades": SCORED_GRADES,
                "segments": SEGMENTS,
                "filler_marker": FILLER_MARKER,
                "legacy_strings": LEGACY_STRINGS,
                "legacy_hits_at_zero": LEGACY_HITS_AT_ZERO,
                "canonical_tiers": CANONICAL_TIERS,
                "hygiene_scored_checks": HYGIENE_SCORED_CHECKS,
                "recency_full_days": RECENCY_FULL_DAYS,
                "recency_floor_days": RECENCY_FLOOR_DAYS,
                "recency_floor": RECENCY_FLOOR,
                "suppressed_at_confidence": SUPPRESSED_AT_CONFIDENCE,
                "sourced_definition": sourced_definition,
                # The segment now reads role_knowledge_checks. That changes
                # what the number means — a role with 40 checks and no
                # findings now carries a defect it did not carry before — so
                # the version has to move, and these two keys are what moves
                # it. `checks_in_ratio` is pinned False rather than omitted:
                # if a later session puts checks into the denominator, the
                # version must change again, and a key that is already here
                # makes that automatic instead of remembered.
                "checks_read": True,
                "checks_in_ratio": False,
                # THE GATE NOW IGNORES MARKERS INSIDE A FENCED CODE BLOCK,
                # AND THIS KEY IS MEASURED RATHER THAN DECLARED.
                #
                # It belongs in the hash because it changes what the
                # structure segment MEASURES for the ten roles that carry an
                # output template: a body whose only `**Archetype:**` sits in
                # a fence used to score as if it had the section. Two
                # readings of the same role under one version string would be
                # two different measurements, which is what A5 refuses.
                #
                # A literal `True` here would have been a claim about another
                # module, and claims drift. `_gate_is_fence_aware()` asks the
                # gate, so reverting `validate_role_structure` to a raw
                # `re.search` moves the version by itself rather than by
                # somebody remembering to move it.
                "fence_aware": _gate_is_fence_aware(),
            }
        ),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _grade_bodies(role_row: dict) -> dict[str, str]:
    """Normalise a roles row (or a get_role result) to the three grade bodies."""
    return {
        "full": role_row.get("prompt") or role_row.get("full") or "",
        "lean": role_row.get("lean_prompt") or role_row.get("lean") or "",
        "micro": role_row.get("micro_prompt") or role_row.get("micro") or "",
    }


# ── Segment: structure ───────────────────────────────────────────────


def _score_structure(content: dict[str, str]) -> dict:
    """Required sections present ÷ required, for the grades that EXIST.

    Worst grade wins among those, because the grades are not interchangeable:
    a perfect FULL does not compensate for a micro that fails to parse, since
    micro is what every interactive session actually receives.

    An empty grade is skipped rather than scored 0. It used to be scored 0
    here AND counted by the tiers segment, so one condition — a NULL column —
    drove two of five segments and about a third of the overall mean. Tiers
    owns emptiness; structure only judges bodies that exist. A role with no
    body at all scores ``None`` here, not 0: there is nothing to judge.
    """
    verdict = validate_role_structure(content)
    missing = verdict["missing"]

    per_grade: dict[str, int] = {}
    defects: list[str] = []
    skipped: list[str] = []

    for grade, labels in _REQUIRED_LABELS.items():
        total = len(labels)
        text = (content.get(grade) or "").strip()
        miss = list(missing.get(grade, []))

        if not text:
            # Emptiness belongs to the tiers segment. Double-counting it here
            # let one NULL column move two bars.
            skipped.append(grade)
            continue

        # A message that is not one of the grade's section labels is a
        # wholesale failure (unparseable YAML, prose instead of a mapping).
        sentinel = [m for m in miss if m not in labels]
        if sentinel:
            per_grade[grade] = 0
            defects.extend(f"{grade}: {m}" for m in sentinel)
            continue

        per_grade[grade] = round(100 * (total - len(miss)) / total)
        defects.extend(f"{grade}: {m}" for m in miss)

    return {
        "score": min(per_grade.values()) if per_grade else None,
        "per_grade": per_grade,
        "missing": missing,
        "advisory": verdict["advisory"],
        "defects": defects,
        "notes": [
            f"{g} grade is empty — counted by Tiers, not scored here"
            for g in skipped
        ],
        "ok": verdict["ok"],
    }


# ── Segment: tiers ───────────────────────────────────────────────────


def _score_tiers(content: dict[str, str]) -> dict:
    """Non-empty grades ÷ 3. Names the empty ones.

    A role claiming the three-tier model while two columns are NULL is the gap
    between what okuro says roles are and what 25 of them are.
    """
    present = [g for g in ("full", "lean", "micro") if (content.get(g) or "").strip()]
    empty = [g for g in ("full", "lean", "micro") if g not in present]
    return {
        "score": round(100 * len(present) / 3),
        "present": present,
        "empty": empty,
        "defects": [f"{g} grade is empty" for g in empty],
    }


# ── Segment: size ────────────────────────────────────────────────────


def _score_size(content: dict[str, str]) -> dict:
    """Lean and micro against their budgets. FULL is reported, never scored.

    Q6: FULL is the on-demand references tier — served only by an explicit
    ``roles_get(level="full")``, never inlined into a dispatch — so its length
    costs nothing at dispatch time and is not a defect. It is returned as
    ``references_chars`` so the UI can show the number without a verdict.

    Over-budget scoring is the budget/length ratio, so twice the budget scores
    50 rather than falling off a cliff at 101%.
    """
    chars = {g: len(content.get(g) or "") for g in ("full", "lean", "micro")}
    per_grade: dict[str, int] = {}
    defects: list[str] = []

    for grade in SCORED_GRADES:
        length = chars[grade]
        budget = SIZE_BUDGETS[grade]
        if length == 0:
            # Emptiness is the tiers segment's defect, not size's. Saying 100
            # here would read as "size is fine"; the note keeps it honest.
            continue
        if length <= budget:
            per_grade[grade] = 100
            continue
        per_grade[grade] = max(0, round(100 * budget / length))
        defects.append(
            f"{grade} is {length:,} chars against a {budget:,} budget "
            f"({length - budget:,} over)"
        )

    notes = [
        f"{g} grade is empty — nothing to size"
        for g in SCORED_GRADES
        if chars[g] == 0
    ]

    return {
        # NOTHING TO SIZE IS NOT A PASS. With lean and micro both empty this
        # used to return 100, so the flattest roles in the fleet showed a full
        # green block on the one segment that had measured nothing at all.
        # None renders as N/A and is left out of the mean.
        "score": min(per_grade.values()) if per_grade else None,
        "per_grade": per_grade,
        "chars": chars,
        "budgets": dict(SIZE_BUDGETS),
        "references_chars": chars["full"],
        "full_scored": False,
        "defects": defects,
        "notes": notes,
    }


# ── Segment: knowledge ───────────────────────────────────────────────


#: Rows at or below this confidence are soft-deleted / superseded. They are
#: excluded from scoring but NOT from ``total``: the caller used to apply this
#: as a SQL filter, which meant the fit view's row count silently disagreed
#: with the row count in the knowledge table and nothing said why.
SUPPRESSED_AT_CONFIDENCE = 0.0


def _is_filler(row: dict) -> bool:
    return FILLER_MARKER in (row.get("content") or "").lower()


def _is_suppressed(row: dict) -> bool:
    """A soft-deleted or superseded row. Counted, named, never scored."""
    if "confidence" not in row:
        return False
    confidence = row.get("confidence")
    if confidence is None:
        return False
    try:
        return float(confidence) <= SUPPRESSED_AT_CONFIDENCE
    except (TypeError, ValueError):
        return False


def _is_sourced(row: dict, mode: str = DEFAULT_SOURCED_DEFINITION) -> bool:
    """Whether a knowledge row lands in the numerator, under an EXPLICIT mode.

    This used to sniff for a ``fetch_verified`` key and prefer it when present.
    Two things were wrong with that. The key presence depended on what the
    caller's SELECT happened to include, so the same row scored differently
    from two call sites — and no call site selected it, so the branch was dead.
    And it made the switch look free: flip a column into existence and every
    legacy row silently reads false. See ``SOURCED_DEFINITIONS``.
    """
    if mode == "fetch_verified":
        return bool(row.get("fetch_verified"))
    return bool((row.get("source_url") or "").strip())


#: One parser for both roles modules — see okuro.roles._time for why this is
#: not a local function any more. Kept as a module-level alias rather than
#: rewriting 3 call sites, so `_parse_ts` still reads the same here.
_parse_ts = parse_ts


def _recency_factor(newest: datetime | None, now: datetime) -> float:
    """Full credit inside RECENCY_FULL_DAYS, decaying to RECENCY_FLOOR.

    A floor rather than zero: an old sourced finding is still a finding. The
    decay exists so a role whose last real finding was in April does not read
    the same as one refreshed this month. The three parameters are named
    constants because they are scorer inputs and therefore part of
    ``rubric_version`` — as literals they could move a whole fleet's scores
    under an unchanged version string.
    """
    if newest is None:
        return 0.0
    age_days = max(0.0, (now - newest).total_seconds() / 86_400)
    if age_days <= RECENCY_FULL_DAYS:
        return 1.0
    if age_days >= RECENCY_FLOOR_DAYS:
        return RECENCY_FLOOR
    span = RECENCY_FLOOR_DAYS - RECENCY_FULL_DAYS
    return 1.0 - (1.0 - RECENCY_FLOOR) * (age_days - RECENCY_FULL_DAYS) / span


def _score_knowledge(
    rows: list[dict],
    now: datetime,
    mode: str = DEFAULT_SOURCED_DEFINITION,
    check_rows: list[dict] | None = None,
) -> dict:
    """Sourced rows ÷ NON-FILLER rows, times a recency factor.

    The denominator excludes filler deliberately. It used to be every row,
    which made the score depend on something nobody intends to keep: Q4 moves
    the 386 no-change rows into ``role_knowledge_checks``, and under the old
    formula that migration would have raised every role's knowledge score
    overnight without a single finding being written. A metric that improves
    when you delete rows measures the rows, not the knowledge.

    Filler is still counted and still named in the defects — it is the whole
    evidence for Q4 — it just no longer sits in the denominator of a ratio
    about sourcing. After migration 159 the ``filler`` bucket reads 0 on a
    migrated store and ``checks`` carries the same signal from its own table;
    both are reported because a store mid-upgrade has some of each.

    Buckets:

    - **filler** — a legacy in-table "no significant change" row. Reported,
      not in the ratio. Zero once 159 has run and the code gate holds.
    - **claimed** — not filler and sourced under ``mode``. The numerator.
    - **unverified** — not filler, not sourced. In the denominator, so it
      dilutes: it is never counted AS sourced, but a role whose findings cite
      nothing is not scoring the same as one whose findings cite something.
    - **checks** — rows in ``role_knowledge_checks``. Never in either half of
      the ratio.

    **Checks are read for RECENCY and never for score, and that asymmetry is
    the point.** A1 asks the segment to see knowledge ∪ checks, because when
    159 moved the filler out, the evidence that a role produces nothing but
    receipts moved with it — a reader of this segment alone would see the
    defect vanish and read it as repair. So the checks come back as a count and
    a date, and they answer "when was this role last looked at". What they must
    NOT do is multiply the score: ``recency_factor`` is what decays an old
    finding, and letting a no-change report reset it would rebuild exactly the
    thing Q4 removed — a number that rises when a sweep writes nothing.
    """
    check_rows = list(check_rows or [])
    total = len(rows)
    suppressed = [r for r in rows if _is_suppressed(r)]
    active = [r for r in rows if not _is_suppressed(r)]
    filler = [r for r in active if _is_filler(r)]
    non_filler = [r for r in active if not _is_filler(r)]
    claimed = [r for r in non_filler if _is_sourced(r, mode)]
    unverified = [r for r in non_filler if not _is_sourced(r, mode)]
    label = SOURCED_LABEL.get(mode, mode)

    timestamps = [_parse_ts(r.get("created_at")) for r in claimed]
    newest = max((t for t in timestamps if t is not None), default=None)
    factor = _recency_factor(newest, now)

    # The newest FINDING of any kind, sourced or not. Distinct from `newest`
    # above, which is the newest SOURCED row and is what decays the score.
    #
    # The "checked but not learning" defect must compare against this one. The
    # first version compared against `newest`, so a role that wrote three
    # unsourced findings last week still read as "last finding: never" — the
    # defect fired on roles that HAD been learning and only failed to cite,
    # which is a different complaint that the `unverified` bucket already
    # makes. Two defects saying different things must not share a clock.
    finding_times = [_parse_ts(r.get("created_at")) for r in non_filler]
    newest_finding = max((t for t in finding_times if t is not None), default=None)

    check_times = [_parse_ts(c.get("checked_at")) for c in check_rows]
    newest_check = max((t for t in check_times if t is not None), default=None)
    check_age = (
        int((now - newest_check).total_seconds() // 86_400)
        if newest_check is not None
        else None
    )

    ratio = (len(claimed) / len(non_filler)) if non_filler else 0.0
    score = round(100 * ratio * factor)

    defects: list[str] = []
    if total == 0 and not check_rows:
        defects.append("no knowledge rows")
    else:
        if total == 0:
            defects.append(
                f"no knowledge rows — {len(check_rows)} checks and nothing learned"
            )
        if suppressed:
            defects.append(
                f"{len(suppressed)} of {total} rows are suppressed "
                f"(confidence 0 — superseded or soft-deleted) and are not scored"
            )
        if filler:
            defects.append(
                f"{len(filler)} of {total} rows are '{FILLER_MARKER}' filler"
            )
        if total and not non_filler:
            defects.append("every row is filler — no findings at all")
        elif non_filler and not claimed:
            defects.append(f"no {label} rows among {len(non_filler)} findings")
        elif factor < 1.0 and newest is not None:
            age = int((now - newest).total_seconds() // 86_400)
            defects.append(f"newest {label} row is {age} days old")
        # The replacement for the filler defect, now that the rows live
        # elsewhere: a role being checked steadily while producing nothing is
        # the exact shape the sweep degenerated into, and it is invisible if
        # only the knowledge table is read.
        if check_rows and newest_check is not None and (
            newest_finding is None
            or (newest_check - newest_finding).total_seconds()
            > RECENCY_FULL_DAYS * 86_400
        ):
            finding_age = (
                f"{int((now - newest_finding).total_seconds() // 86_400)} days ago"
                if newest_finding is not None
                else "never"
            )
            defects.append(
                f"checked {check_age} days ago but last finding of any kind: "
                f"{finding_age} ({len(check_rows)} checks on record)"
            )

    return {
        "score": score,
        "total": total,
        # The numerator's honest name. Under url_claimed this counts rows that
        # CLAIM a source_url; nothing has checked that the URL was fetched.
        "claimed": len(claimed),
        "filler": len(filler),
        "unverified": len(unverified),
        "suppressed": len(suppressed),
        "scored_rows": len(non_filler),
        # Its own count, beside the knowledge buckets and never inside them.
        "checks": len(check_rows),
        "sourced_definition": mode,
        "sourced_label": label,
        "ratio": round(ratio, 4),
        "recency_factor": round(factor, 4),
        "newest_claimed_at": newest.isoformat() if newest else None,
        "newest_finding_at": newest_finding.isoformat() if newest_finding else None,
        "newest_check_at": newest_check.isoformat() if newest_check else None,
        "check_age_days": check_age,
        "defects": defects,
    }


# ── Segment: hygiene ─────────────────────────────────────────────────


def _score_hygiene(role_row: dict, content: dict[str, str]) -> dict:
    """Two scored checks — a maintenance schedule, and no dead strings.

    **Tier is reported, not scored.** The previous version scored it and told
    the reader that a non-canonical tier means "dispatch silently downgrades it
    to standard". That is false, and it was worth a third of this segment.
    Traced this session: ``resolve_unit_tier`` (orchestrator/config.py:51) reads
    ``unit.complexity`` — the subtask's or DAG node's own field — and both call
    sites (dispatcher.py:347, dispatcher_streaming.py:610) pass the unit, never
    a role row. The ``roles.tier`` column does not reach model routing at all.
    What it does reach is listings and the roles UI's tier selector, which
    offers three values and cannot render a fourth. Real, but cosmetic, and a
    cosmetic defect must not cost a third of a quality bar.

    **The legacy check is proportional.** All-or-nothing meant removing four of
    five dead strings scored identically to removing none, which is precisely
    the wrong incentive for a repair that is done a string at a time.
    """
    from okuro.orchestrator.config import CANONICAL_TIERS

    defects: list[str] = []
    advisory: list[str] = []

    tier = (role_row.get("tier") or "").strip()
    tier_ok = tier in CANONICAL_TIERS
    if not tier_ok:
        advisory.append(
            f"tier '{tier or '(unset)'}' is outside {', '.join(CANONICAL_TIERS)} "
            f"— listings and the tier selector cannot represent it. It does NOT "
            f"affect dispatch: model routing reads the subtask's complexity, "
            f"never this column."
        )

    schedule = (role_row.get("maintenance_schedule") or "").strip()
    schedule_ok = bool(schedule)
    if not schedule_ok:
        defects.append("maintenance_schedule is unset")

    legacy_hits: list[dict] = []
    for grade, text in content.items():
        if not text:
            continue
        lowered = text.lower()
        for needle in LEGACY_STRINGS:
            count = lowered.count(needle.lower())
            if count:
                legacy_hits.append({"grade": grade, "string": needle, "count": count})
    total_hits = sum(h["count"] for h in legacy_hits)
    legacy_ok = not legacy_hits
    legacy_factor = max(0.0, 1.0 - total_hits / LEGACY_HITS_AT_ZERO)
    defects.extend(
        f"{h['grade']} body contains '{h['string']}' ×{h['count']}" for h in legacy_hits
    )

    components = (1.0 if schedule_ok else 0.0, legacy_factor)
    return {
        "score": round(100 * sum(components) / len(HYGIENE_SCORED_CHECKS)),
        "tier_ok": tier_ok,
        "tier_scored": False,
        "schedule_ok": schedule_ok,
        "legacy_ok": legacy_ok,
        "legacy_hits": legacy_hits,
        "legacy_hit_total": total_hits,
        "legacy_factor": round(legacy_factor, 4),
        "defects": defects,
        "advisory": advisory,
    }


# ── Public API ───────────────────────────────────────────────────────


def compute_fit(
    role_row: dict,
    knowledge_rows: list[dict] | None = None,
    now: datetime | None = None,
    sourced_definition: str = DEFAULT_SOURCED_DEFINITION,
    check_rows: list[dict] | None = None,
) -> dict:
    """Five segments for one role. Pure — no I/O, no store access.

    Args:
        role_row: a ``roles`` row or ``get_role`` result. Reads ``role_id``/
            ``id``, ``prompt``/``lean_prompt``/``micro_prompt`` (or
            ``full``/``lean``/``micro``), ``tier`` and ``maintenance_schedule``.
        knowledge_rows: this role's ``role_knowledge`` rows, UNFILTERED. Each
            needs ``content``, ``created_at``, ``confidence`` and the column
            named by ``sourced_definition``. Pass every row: suppression is
            counted here, not applied in SQL, so the reported total matches
            the table.
        now: injected for deterministic tests. Defaults to UTC now.
        sourced_definition: what puts a row in the knowledge numerator. See
            ``SOURCED_DEFINITIONS`` — switching it is a migration, not a
            setting.
        check_rows: this role's ``role_knowledge_checks`` rows. Counted and
            dated, never scored — see ``_score_knowledge``. Omitting them is
            safe for a caller that has none; it costs the "checked but not
            learning" defect, not a wrong number.

    Returns a dict with ``segments`` (the five, each with ``score`` and
    ``defects``), the ``worst`` segment name, an ``overall`` mean and the
    ``rubric_version`` the scores were taken under. A segment score is ``None``
    when that segment has nothing to measure — never 100.
    """
    if sourced_definition not in SOURCED_DEFINITIONS:
        raise ValueError(
            f"sourced_definition must be one of {SOURCED_DEFINITIONS}, "
            f"got {sourced_definition!r}"
        )
    rows = list(knowledge_rows or [])
    now = now or datetime.now(timezone.utc)
    content = _grade_bodies(role_row)

    segments = {
        "structure": _score_structure(content),
        "tiers": _score_tiers(content),
        "size": _score_size(content),
        "knowledge": _score_knowledge(
            rows, now, sourced_definition, list(check_rows or [])
        ),
        "hygiene": _score_hygiene(role_row, content),
    }

    scores: dict[str, int | None] = {
        name: segments[name]["score"] for name in SEGMENTS
    }
    scorable = [name for name in SEGMENTS if scores[name] is not None]
    worst = min(scorable, key=lambda name: scores[name]) if scorable else None

    return {
        "role_id": role_row.get("role_id") or role_row.get("id"),
        "rubric_version": rubric_version(sourced_definition),
        "sourced_definition": sourced_definition,
        "computed_at": now.isoformat(),
        "segments": segments,
        "scores": scores,
        # The mean is for a fleet average, never for sorting a row — the row
        # sorts by its worst segment, which is the one that names a repair.
        # Segments with nothing to measure are left out of both rather than
        # counted as a pass.
        "overall": (
            round(sum(scores[n] for n in scorable) / len(scorable))
            if scorable
            else None
        ),
        "worst": worst,
        "worst_score": scores[worst] if worst else None,
    }


def fit_summary(fit: dict) -> dict:
    """The compact shape for the listing endpoint — scores without the prose.

    ``GET /api/roles`` renders 103 rows and each row needs five numbers and a
    worst-segment name. Shipping every defect string there would put the Fit
    tab's entire payload in a list nobody reads it from; the detail endpoint
    carries the defects.
    """
    return {
        "rubric_version": fit["rubric_version"],
        "sourced_definition": fit["sourced_definition"],
        "scores": dict(fit["scores"]),
        "overall": fit["overall"],
        "worst": fit["worst"],
        "worst_score": fit["worst_score"],
        "defect_counts": {
            name: len(fit["segments"][name].get("defects", [])) for name in SEGMENTS
        },
        # Kept because the row's knowledge chip renders the grey unverified
        # band next to the count, and refetching per row to get it is absurd.
        "knowledge": {
            k: fit["segments"]["knowledge"][k]
            for k in (
                "total",
                "claimed",
                "filler",
                "unverified",
                "suppressed",
                "scored_rows",
                "checks",
                "sourced_label",
            )
        },
        "references_chars": fit["segments"]["size"]["references_chars"],
    }


def fleet_fit(fits: list[dict], worst_n: int = 10) -> dict:
    """Roll up per-role fits: per-segment mean, worst-N roles, counts.

    ``perfect`` and ``zero`` are counted per segment because a mean of 62 says
    nothing about whether that is 103 mediocre roles or 46 clean ones and 57
    broken ones — which is the actual shape of this fleet.
    """
    n = len(fits)
    per_segment: dict[str, dict] = {}
    for name in SEGMENTS:
        # A segment that measured nothing is absent from its own mean, not
        # folded in as 100 or as 0. `not_applicable` is how many roles that was.
        scores = [f["scores"][name] for f in fits if f["scores"][name] is not None]
        k = len(scores)
        per_segment[name] = {
            "mean": round(sum(scores) / k, 1) if k else None,
            "min": min(scores) if k else None,
            "max": max(scores) if k else None,
            "perfect": sum(1 for s in scores if s == 100),
            "zero": sum(1 for s in scores if s == 0),
            "scored": k,
            "not_applicable": n - k,
        }

    worst_counts: dict[str, int] = defaultdict(int)
    for f in fits:
        if f["worst"]:
            worst_counts[f["worst"]] += 1

    ranked = sorted(
        fits,
        key=lambda f: (
            f["worst_score"] if f["worst_score"] is not None else 101,
            f["overall"] if f["overall"] is not None else 101,
        ),
    )[:worst_n]

    overall_scores = [f["overall"] for f in fits if f["overall"] is not None]
    return {
        "roles": n,
        "rubric_version": rubric_version(
            fits[0]["sourced_definition"] if fits else DEFAULT_SOURCED_DEFINITION
        ),
        "overall_mean": (
            round(sum(overall_scores) / len(overall_scores), 1)
            if overall_scores
            else None
        ),
        "segments": per_segment,
        "worst_segment_counts": dict(worst_counts),
        "worst_roles": [
            {
                "role_id": f["role_id"],
                "worst": f["worst"],
                "worst_score": f["worst_score"],
                "overall": f["overall"],
                "scores": dict(f["scores"]),
            }
            for f in ranked
        ],
    }


def knowledge_sourced_ratio_by_month(
    rows: list[dict],
    sourced_definition: str = DEFAULT_SOURCED_DEFINITION,
) -> list[dict]:
    """Sourced-row ratio per calendar month, oldest first.

    Amendment A1 calls this the standing metric: it is the chart that would
    have shown the June 2026 collapse (concrete findings 129 → 42) in the week
    it happened rather than three months later in an audit.

    ``ratio`` uses the same denominator as the knowledge segment — non-filler,
    non-suppressed rows — so the header number and the bar cannot tell
    different stories about the same month.
    """
    buckets: dict[str, dict] = {}
    for row in rows:
        ts = _parse_ts(row.get("created_at"))
        if ts is None:
            continue
        key = f"{ts.year:04d}-{ts.month:02d}"
        b = buckets.setdefault(
            key,
            {
                "month": key,
                "total": 0,
                "claimed": 0,
                "filler": 0,
                "unverified": 0,
                "suppressed": 0,
            },
        )
        b["total"] += 1
        if _is_suppressed(row):
            b["suppressed"] += 1
        elif _is_filler(row):
            b["filler"] += 1
        elif _is_sourced(row, sourced_definition):
            b["claimed"] += 1
        else:
            b["unverified"] += 1

    out = []
    for key in sorted(buckets):
        b = buckets[key]
        scored = b["claimed"] + b["unverified"]
        active = scored + b["filler"]
        b["scored_rows"] = scored
        b["ratio"] = round(b["claimed"] / scored, 4) if scored else 0.0
        # TWO ratios, because taking filler out of the first one's denominator
        # took the signal out with it. `ratio` now answers "of the findings
        # written, how many cited a source" and reads 0.7–1.0 every month —
        # the July collapse is invisible in it. `findings_ratio` is where the
        # collapse lives: 0.97 in April against 0.22 in July. A1 asked for a
        # metric that would have shown that in the week it happened, so the
        # header shows this one next to it rather than either alone.
        b["findings_ratio"] = round(scored / active, 4) if active else 0.0
        b["active_rows"] = active
        b["sourced_definition"] = sourced_definition
        b["sourced_label"] = SOURCED_LABEL.get(sourced_definition, sourced_definition)
        out.append(b)
    return out
