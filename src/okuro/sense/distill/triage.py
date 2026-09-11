### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Tier-0 — free deterministic triage. Decides what reaches the judge, and writes the only ledger row counting alone can justify.
# index: imports | thresholds | Tier0Result | load_signals | classify | triage_session | run_tier0
# AGENT_HEADER_END -->
"""Tier-0: what can be decided without reading the session.

Free, reproducible, no model call. It answers two questions and refuses the
third:

**Where does this session go?** ``unusable`` or ``judge``. Nothing else.

**What do we already know about it?** Compliance score, friction markers,
error runs, how it ended — signals other okuro subsystems already computed,
denormalised onto one row so the corpus map is a single read.

**Is it a good session?** Not answerable here, and tier-0 does not try. See
below.

The one trap this tier exists to avoid
--------------------------------------
The obvious cheap filter is "no tool calls, skip it". That is exactly
backwards for this corpus. A session with zero tool calls is usually a
CONVERSATION — the user thinking out loud, arguing with a plan, correcting a
misunderstanding — and those are the richest material the distillation has,
because they are the only place the behavioural contract is visibly kept or
broken. So ``unusable`` requires zero tool calls **and** almost nothing said:
an opened-and-abandoned stub, not a quiet session.

Why ``unusable`` is the only verdict that reaches the ledger
------------------------------------------------------------
A ``session_distillations`` row is the retention gate's precondition for
DELETING a transcript (``gate.py``, migration 136). If tier-0 wrote a row per
session, the day the owner flips ``deletion_enabled`` the entire corpus would be
deletable on the strength of a row count — with nothing having read a word of
it. Counting supports exactly one verdict: a session with no tool calls and
under 400 characters of content has nothing in it to lose. Everything else
gets a ``distill_facets`` row, which licenses nothing, and waits for the
judge.

Migration 137's trigger enforces the same rule from the schema side, so a
future caller cannot widen it by accident.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from . import RUBRIC_VERSION
from .facets import EXTRACTION_VERSION, read_session

log = logging.getLogger(__name__)

# `unusable` needs BOTH: no tool calls AND essentially nothing said. See the
# module docstring — a quiet session is not an empty one.
UNUSABLE_MAX_USER_TURNS = 1
UNUSABLE_MAX_CHARS = 400

# Sampling policy: 100% of flagged sessions reach the judge.
LOW_COMPLIANCE = 0.5
ERROR_CASCADE_RUN = 3
# `ghost` is a session that stopped reporting rather than ending — the same
# class of failure as a timeout from the analysis side, and 548 of them exist
# (live store 2026-08-13, alongside 606 timeouts).
ABNORMAL_END_REASONS = frozenset({"timeout", "ghost"})

# Detector ids that mean the interaction went wrong, as opposed to the two
# high-volume subagent-hygiene markers (`brief_underspecified`, `no_closeout`)
# which fire on 4785 and 3930 sessions and would flag most of the corpus.
FRICTION_MARKERS = frozenset({
    "frustration",
    "escalating_friction",
    "correction",
    "restated_directive",
    "rejection",
    "re_explanation",
    "abandonment",
})


def is_unusable(read) -> bool:
    """The `unusable` predicate. ONE definition, called by everything.

    The pilot's ``--dry-run`` predicts how many sessions will be binned, and a
    prediction computed from a second copy of this rule disagrees with the run
    it is predicting the moment either copy changes. That is the same drift
    ``embed/repair.py`` avoids by delegating its text function to the module
    that owns it.

    BOTH conditions are required. Zero tool calls alone is a conversation, and
    conversations are the richest material in the corpus.
    """
    return (
        read.tool_calls == 0
        and read.user_turns <= UNUSABLE_MAX_USER_TURNS
        and read.total_chars < UNUSABLE_MAX_CHARS
    )


@dataclass
class Tier0Result:
    session_id: str
    triage: str                       # 'unusable' | 'judge'
    session_class: str                # 'main' | 'subagent'
    flagged: bool = False
    flag_reasons: list[str] = field(default_factory=list)
    ledger_written: bool = False


@dataclass
class Tier0Run:
    considered: int = 0
    unusable: int = 0
    to_judge: int = 0
    flagged: int = 0
    ledger_rows: int = 0
    skipped_by_reason: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped_by_reason[reason] = self.skipped_by_reason.get(reason, 0) + 1


def classify(session_id: str) -> str:
    """``'subagent'`` or ``'main'``, from the session id's shape.

    Delegates to :func:`okuro.sense.interaction.turns.session_kind` rather than
    re-deriving the rule, so the store has ONE definition of what a subagent
    session is. That function answers ``'human-facing'``; this table's
    vocabulary is ``'main'``, because half of these sessions have no human in
    them at all and calling them human-facing would be wrong twice.
    """
    from okuro.sense.interaction.turns import session_kind

    return "subagent" if session_kind(session_id) == "subagent" else "main"


def load_signals(db, session_ids) -> dict[str, dict]:
    """Batch-load the signals other subsystems already computed.

    Three queries for N sessions rather than 3N. On a 500-session pilot that
    is the difference between a few hundred milliseconds and a few minutes,
    and the full backlog is 10 000.

    Everything here is OPTIONAL by construction. ``compliance_normalized`` is
    NULL for the ~90% of sessions with no ``session_bridge`` link, and NULL
    means *unknown* — never 0. A tier-0 row that flagged unknown compliance as
    low would flag nearly the whole corpus.
    """
    sids = list(session_ids)
    out: dict[str, dict] = {
        sid: {"compliance": None, "end_reason": None, "markers": [], "compacted": False}
        for sid in sids
    }
    if not sids:
        return out

    chunk = 400
    for i in range(0, len(sids), chunk):
        batch = sids[i : i + chunk]
        marks = ",".join("?" * len(batch))

        for row in db.fetchall(
            f"""
            SELECT b.native_session_id AS sid,
                   s.compliance_normalized AS compliance,
                   s.end_reason AS end_reason
            FROM session_bridge b
            JOIN sessions s ON s.session_id = b.telemetry_session_id
            WHERE b.native_session_id IN ({marks})
            ORDER BY b.confidence ASC
            """,
            tuple(batch),
        ):
            # Ascending confidence, so the strongest link is written last and
            # wins. Many telemetry sessions legitimately map to one native id
            # (a resumed transcript), so this is a real many-to-one.
            entry = out[row["sid"]]
            entry["compliance"] = row["compliance"]
            entry["end_reason"] = row["end_reason"]

        for row in db.fetchall(
            f"""
            SELECT native_session_id AS sid, marker
            FROM interaction_markers
            WHERE native_session_id IN ({marks})
            GROUP BY native_session_id, marker
            """,
            tuple(batch),
        ):
            out[row["sid"]]["markers"].append(row["marker"])

        for row in db.fetchall(
            f"""
            SELECT native_session_id AS sid
            FROM trace_lifecycle
            WHERE native_session_id IN ({marks})
              AND compacted_at IS NOT NULL
            """,
            tuple(batch),
        ):
            out[row["sid"]]["compacted"] = True

    return out


def _flags(read, signals: dict) -> list[str]:
    """Why this session must reach the judge regardless of sampling."""
    reasons: list[str] = []
    compliance = signals.get("compliance")
    if compliance is not None and compliance < LOW_COMPLIANCE:
        reasons.append("low_compliance")
    if FRICTION_MARKERS.intersection(signals.get("markers") or ()):
        reasons.append("friction_markers")
    if read.max_error_run >= ERROR_CASCADE_RUN:
        reasons.append("error_cascade")
    if (signals.get("end_reason") or "") in ABNORMAL_END_REASONS:
        reasons.append(f"end_{signals['end_reason']}")
    return reasons


def triage_session(db, session_id: str, signals: dict | None = None,
                   rows=None) -> tuple[Tier0Result, object]:
    """Triage one session and persist its ``distill_facets`` row.

    Returns the result and the :class:`~.facets.SessionRead` behind it, so
    tier-1 can reuse the read instead of walking ``agent_events`` twice.
    """
    if signals is None:
        signals = load_signals(db, [session_id])[session_id]

    read = read_session(db, session_id, rows=rows)
    session_class = classify(session_id)

    triage = "unusable" if is_unusable(read) else "judge"

    flag_reasons = _flags(read, signals)
    result = Tier0Result(
        session_id=session_id,
        triage=triage,
        session_class=session_class,
        flagged=bool(flag_reasons),
        flag_reasons=flag_reasons,
    )

    provider_row = db.fetchone(
        "SELECT provider FROM agent_sessions WHERE session_id = ?", (session_id,)
    )
    provider = provider_row["provider"] if provider_row else None

    markers = sorted(set(signals.get("markers") or ()))
    with db.write():
        db.execute(
            """
            INSERT INTO distill_facets (
                session_id, extraction_version, scanned_through_ord, stage,
                session_class, triage, tool_calls, user_turns,
                assistant_messages, tokens_out_total, context_peak,
                tokens_in_total, token_shape,
                distinct_tools, provider, compliance_normalized, markers,
                marker_count, flagged, flag_reasons, bodies_available,
                last_event_ts, updated_at
            ) VALUES (?, ?, ?, 'tier0', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(session_id) DO UPDATE SET
                extraction_version    = excluded.extraction_version,
                scanned_through_ord   = excluded.scanned_through_ord,
                session_class         = excluded.session_class,
                triage                = excluded.triage,
                tool_calls            = excluded.tool_calls,
                user_turns            = excluded.user_turns,
                assistant_messages    = excluded.assistant_messages,
                tokens_out_total      = excluded.tokens_out_total,
                context_peak          = excluded.context_peak,
                tokens_in_total       = excluded.tokens_in_total,
                token_shape           = excluded.token_shape,
                distinct_tools        = excluded.distinct_tools,
                provider              = excluded.provider,
                compliance_normalized = excluded.compliance_normalized,
                markers               = excluded.markers,
                marker_count          = excluded.marker_count,
                flagged               = excluded.flagged,
                flag_reasons          = excluded.flag_reasons,
                bodies_available      = excluded.bodies_available,
                last_event_ts         = excluded.last_event_ts,
                updated_at            = datetime('now')
            """,
            (
                session_id, EXTRACTION_VERSION, read.scanned_through_ord,
                session_class, triage, read.tool_calls, read.user_turns,
                read.assistant_messages, read.tokens_out_total,
                read.context_peak, read.tokens_in_total, read.token_shape,
                json.dumps(read.distinct_tools, ensure_ascii=False),
                provider, signals.get("compliance"),
                json.dumps(markers, ensure_ascii=False), len(markers),
                1 if result.flagged else 0,
                json.dumps(flag_reasons, ensure_ascii=False),
                # A session the compactor already nulled yields a skeleton, and
                # any facet from it is a weaker claim. Either signal is enough
                # to know the bodies are gone.
                0 if (signals.get("compacted") or not read.bodies_available) else 1,
                read.last_event_ts,
            ),
        )

        # The ONLY ledger row tier-0 may write. Everything about this statement
        # is shaped by migration 137's trigger: verdict 'unusable', no judge
        # model, no dimension scores. A row with any of those needs a
        # human-labelled validation batch, which is the point.
        if triage == "unusable":
            cur = db.execute(
                """
                INSERT INTO session_distillations
                    (session_id, rubric_version, verdict)
                VALUES (?, ?, 'unusable')
                ON CONFLICT(session_id) DO NOTHING
                """,
                (session_id, RUBRIC_VERSION),
            )
            result.ledger_written = bool(getattr(cur, "rowcount", 0))

    return result, read


def run_tier0(session_ids, db=None) -> Tier0Run:
    """Triage a batch. Counts by outcome, and skips are counted by reason."""
    if db is None:
        from okuro.db import get_db

        db = get_db()

    sids = list(session_ids)
    signals = load_signals(db, sids)
    run = Tier0Run()
    for sid in sids:
        try:
            result, _ = triage_session(db, sid, signals=signals.get(sid))
        except Exception as exc:  # noqa: BLE001 — one bad session must not
            # abort a 10 000-session pass; the reason is counted and visible.
            log.warning("tier-0 failed for %s: %s", sid, exc)
            run.skip(type(exc).__name__)
            continue
        run.considered += 1
        if result.triage == "unusable":
            run.unusable += 1
        else:
            run.to_judge += 1
        if result.flagged:
            run.flagged += 1
        if result.ledger_written:
            run.ledger_rows += 1
    return run
