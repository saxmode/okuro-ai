### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Tier-2 — the rubric, the sampling policy, and the refusal that keeps the judge dormant until a human has labelled a calibration set.
# index: imports | DIMENSIONS | Refusal | RUBRIC | guards_present | validated_batches | sample_for_judging | build_rubric_prompt | parse_verdict | write_verdict | judge_session
# AGENT_HEADER_END -->
"""Tier-2: the judge. Built, wired, and deliberately not running.

Everything here works except the one thing it refuses to do: write a verdict.
:func:`write_verdict` fails closed unless ``validation_batch_id`` names a
``distill_validation_batches`` row that is APPROVED (``approved_at`` and
``approved_by`` both set) and holds at least
``distill_config.min_validation_labels`` distinct labelled sessions. Migration
137 enforces the same predicate from inside the schema, so a future caller — a
daemon task, a CLI, a raw sqlite3 shell — cannot route around it.

The bar used to be "the batch holds at least one label", and that was not a
bar at all: a reviewer landed a fully judged verdict in three statements by
creating a batch, inserting ONE model-authored label for an unrelated session,
and writing the verdict. Every check passed individually and the thing they
were meant to compose into — a measured human calibration — never happened.
:func:`batch_status` is now the single predicate, resolving WHICH condition
failed so a pipeline can count "unapproved" apart from "too small".

**That refusal is the design, not a stub.** An LLM judge whose agreement with
the human has never been measured produces numbers with no denominator, and
those numbers would then license deleting the transcripts they were derived
from. The calibration set is what converts "the judge said 0.4" into "the
judge agrees with the owner on 34 of 40 sessions". Until that set exists, a
tier-2 verdict is an assertion, and assertions must not enter a ledger the
retention gate reads.

What the rubric may ask
-----------------------
Objective criteria only, and each one has to be answerable from the transcript
by pointing at something:

``protocol_compliance``
    Did the session follow okuro's stated tool protocol — bootstrap, memory
    before acting, cortex before hunting, the close-out calls?

``task_outcome``
    Is there evidence in the transcript that the stated task was completed —
    a passing test, a written file, a confirmed result — as opposed to a
    claim that it was?

``communication_contract``
    Did replies honour the user's stated communication rules: answer first,
    tables over prose, no restating the question?

``tool_routing``
    Were the right tools used for the job, or was a gated//preferred route
    bypassed?

Deliberately absent: anything about tone, helpfulness, or whether the agent
"did well". Those are the dimensions an unvalidated judge scores confidently
and wrongly.

Sampling policy
---------------
:func:`sample_for_judging` encodes it: **100% of flagged sessions** (low
compliance, friction markers, error cascades, abnormal endings) plus a
**stratified sample per cluster** for the routine remainder. Flagged sessions
are where the lessons are; the cluster sample is what keeps the routine
majority represented without paying for all of it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from . import RUBRIC_VERSION

log = logging.getLogger(__name__)

# Column stem -> what the judge is being asked. The stems match
# session_distillations' score_* columns exactly, so a rubric change that adds
# a dimension without a migration fails loudly at write time rather than
# silently dropping a score.
DIMENSIONS = (
    "protocol_compliance",
    "task_outcome",
    "communication_contract",
    "tool_routing",
)

VERDICTS = ("good", "bad", "mixed", "unusable")

# Total bytes of the evidence_quotes JSON array. session_distillations CHECKs
# 4096; this stays under it so a verbose judge is trimmed here rather than
# aborting the INSERT. The bound is the point of the column: an unbounded
# quote field becomes a second copy of the transcript and defeats deleting
# the first one.
_CAP_EVIDENCE_JSON = 3500
_CAP_QUOTE = 400
_MAX_QUOTES = 6

# The two schema guards this module's refusal depends on. DROP TRIGGER fires
# nothing and cannot be refused in SQL, so "the schema enforces this" is a
# claim with a runtime lifetime — the same hole gate.py answers the same way.
_REQUIRED_TRIGGERS = (
    "session_distillations_require_validated_batch",
    "session_distillations_require_validated_batch_upd",
)


class Refusal:
    """Why a verdict was not written. Counter keys and log text."""

    NO_VALIDATION_BATCH = "no_validation_batch"
    BATCH_UNLABELLED = "batch_unlabelled"
    BATCH_UNAPPROVED = "batch_unapproved"
    BATCH_TOO_SMALL = "batch_below_label_floor"
    GUARD_MISSING = "guard_missing"
    NO_FACET = "no_facet"
    INVOKE_FAILED = "invoke_failed"
    UNPARSEABLE = "unparseable_verdict"


@dataclass
class JudgeResult:
    session_id: str
    written: bool = False
    reason: str | None = None
    detail: str | None = None
    verdict: str | None = None
    scores: dict[str, float | None] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    model: str | None = None


_SYSTEM_PROMPT = (
    "You grade agent session transcripts against a fixed rubric. Every score "
    "must be defensible from a quote in the transcript. Reply with one JSON "
    "object and nothing else."
)

RUBRIC = """\
Grade this agent session against four objective criteria. Score each from 0.0 \
to 1.0, or null if the transcript does not contain enough evidence to assess \
it — null is the correct answer far more often than 0.0, which means "assessed \
and found absent".

protocol_compliance
  Did the session follow the stated tool protocol: bootstrap first, memory \
consulted before acting on a subsystem, semantic search used to locate code \
rather than shell hunting, close-out calls made?

task_outcome
  Is there EVIDENCE the stated task was done — a test run and its output, a \
file written, a result confirmed — rather than a claim that it was done?

communication_contract
  Did the replies lead with the answer, prefer tables and bullets over long \
prose, and avoid restating the request back to the user?

tool_routing
  Was the right tool used for each job, or was a preferred route bypassed in \
favour of a generic one?

Reply with exactly this JSON object:

{{
  "verdict": "<one of: good | bad | mixed | unusable>",
  "scores": {{
    "protocol_compliance": <0.0-1.0 or null>,
    "task_outcome": <0.0-1.0 or null>,
    "communication_contract": <0.0-1.0 or null>,
    "tool_routing": <0.0-1.0 or null>
  }},
  "evidence": ["<short quote from the transcript supporting a score>", ...]
}}

At most {max_quotes} quotes, each under {cap_quote} characters. A score with no \
quote behind it is an assertion; prefer null.

TRANSCRIPT:
{transcript}
"""


def guards_present(db) -> list[str]:
    """Names of migration 137's triggers missing from the schema.

    Checked before the judge runs, so it never writes under the belief it is
    constrained when it is not.
    """
    present = {
        r["name"]
        for r in db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'session_distillations'"
        )
    }
    return sorted(set(_REQUIRED_TRIGGERS) - present)


def label_floor(db) -> int:
    """``distill_config.min_validation_labels``, failing closed when absent.

    A store whose config row did not migrate must refuse every batch, so the
    fallback is unreachably high rather than 0. Mirrors the ``COALESCE`` the
    trigger uses, and for the same reason.
    """
    row = db.fetchone("SELECT min_validation_labels FROM distill_config WHERE id = 1")
    if row is None or row["min_validation_labels"] is None:
        return 1_000_000_000
    return int(row["min_validation_labels"])


def batch_status(db, batch_id: str) -> tuple[bool, str | None, str | None]:
    """``(ok, refusal_reason, detail)`` for one calibration batch.

    Mirrors the trigger's predicate, but resolves WHICH condition failed. The
    trigger raises one ``IntegrityError`` for every cause; a pipeline needs to
    count "nobody approved it" apart from "it is too small" apart from "there
    is no such batch".
    """
    if not batch_id:
        return False, Refusal.NO_VALIDATION_BATCH, (
            "tier-2 is dormant: no validation_batch_id given"
        )
    batch = db.fetchone(
        "SELECT approved_at, approved_by FROM distill_validation_batches "
        "WHERE batch_id = ?",
        (batch_id,),
    )
    if batch is None:
        return False, Refusal.NO_VALIDATION_BATCH, (
            f"no distill_validation_batches row named {batch_id!r}"
        )

    labels = db.fetchone(
        "SELECT COUNT(*) AS n FROM distill_validation_labels WHERE batch_id = ?",
        (batch_id,),
    )["n"]
    if not labels:
        return False, Refusal.BATCH_UNLABELLED, (
            f"batch {batch_id!r} holds no labels — an empty batch is not a "
            f"calibration set"
        )
    if batch["approved_at"] is None or batch["approved_by"] is None:
        return False, Refusal.BATCH_UNAPPROVED, (
            f"batch {batch_id!r} has {labels} labels but is not approved — "
            f"approved_at and approved_by must both be set, and approving is a "
            f"deliberate second act, not a side effect of labelling"
        )
    floor = label_floor(db)
    if labels < floor:
        return False, Refusal.BATCH_TOO_SMALL, (
            f"batch {batch_id!r} holds {labels} labelled sessions, below the "
            f"distill_config.min_validation_labels floor of {floor}"
        )
    return True, None, None


def validated_batches(db) -> list[str]:
    """Batch ids that satisfy the full predicate. The judge's licence to write.

    Approved, and holding at least the configured floor of DISTINCT labelled
    sessions — the labels PK is ``(batch_id, session_id)``, so the count is
    distinct sessions by construction and fifty rows cannot be one session
    labelled fifty times.

    Counted against the label rows themselves, never a counter column on the
    batch: a row claiming fifty labels with none behind it is exactly the
    failure this exists to prevent, and a counter cannot rule it out.
    """
    return [
        r["batch_id"]
        for r in db.fetchall(
            """
            SELECT b.batch_id
            FROM distill_validation_batches b
            WHERE b.approved_at IS NOT NULL
              AND b.approved_by IS NOT NULL
              AND (SELECT COUNT(*) FROM distill_validation_labels l
                   WHERE l.batch_id = b.batch_id)
                  >= COALESCE((SELECT min_validation_labels FROM distill_config
                               WHERE id = 1), 1000000000)
            ORDER BY b.created_at DESC
            """
        )
    ]


def sample_for_judging(db, per_cluster: int = 5, include_unusable: bool = False
                       ) -> dict[str, list[str]]:
    """Which sessions tier-2 should read, and why each was picked.

    Returns ``{"flagged": [...], "stratified": [...]}`` — kept apart because
    the two populations answer different questions and a caller that merges
    them loses the ability to say which budget went where.

    ``flagged`` is every flagged session, no sampling: those are where the
    lessons are, and sampling them would mean deciding in advance which
    failures matter.

    ``stratified`` takes ``per_cluster`` routine sessions from each cluster,
    ordered by session_id so the pick is deterministic and a re-run judges the
    same sessions rather than slowly judging everything.
    """
    where_triage = "" if include_unusable else "AND triage = 'judge'"

    flagged = [
        r["session_id"]
        for r in db.fetchall(
            f"SELECT session_id FROM distill_facets "
            f"WHERE flagged = 1 {where_triage} ORDER BY session_id"
        )
    ]

    stratified: list[str] = []
    clusters = db.fetchall(
        f"SELECT DISTINCT cluster_id FROM distill_facets "
        f"WHERE cluster_id IS NOT NULL {where_triage} ORDER BY cluster_id"
    )
    for row in clusters:
        stratified.extend(
            r["session_id"]
            for r in db.fetchall(
                f"SELECT session_id FROM distill_facets "
                f"WHERE cluster_id = ? AND flagged = 0 {where_triage} "
                f"ORDER BY session_id LIMIT ?",
                (row["cluster_id"], per_cluster),
            )
        )
    return {"flagged": flagged, "stratified": stratified}


def build_rubric_prompt(transcript: str) -> str:
    """The exact text a judge call would send. Pure, so tests can read it."""
    return RUBRIC.format(
        max_quotes=_MAX_QUOTES, cap_quote=_CAP_QUOTE, transcript=transcript
    )


def _score(raw) -> float | None:
    """Coerce one dimension score. Anything unusable becomes NULL, not 0.0.

    The distinction is in the schema's own comment (migration 136): NULL means
    the judge could not assess the dimension, 0.0 means it assessed and found
    nothing. Coercing a malformed score to 0.0 would manufacture the harshest
    possible assessment out of a parse failure.
    """
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val != val or val < 0.0 or val > 1.0:  # NaN or out of range
        return None
    return val


def parse_verdict(raw: str) -> dict | None:
    """Coerce a judge reply into the ledger's shape, or None."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        try:
            from json_repair import repair_json

            parsed = json.loads(repair_json(text))
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(parsed, dict):
        return None

    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        return None

    raw_scores = parsed.get("scores")
    raw_scores = raw_scores if isinstance(raw_scores, dict) else {}
    scores = {dim: _score(raw_scores.get(dim)) for dim in DIMENSIONS}

    quotes: list[str] = []
    for q in (parsed.get("evidence") or [])[:_MAX_QUOTES]:
        quote = str(q).strip()[:_CAP_QUOTE]
        if quote:
            quotes.append(quote)
    while quotes and len(json.dumps(quotes, ensure_ascii=False)) > _CAP_EVIDENCE_JSON:
        quotes.pop()

    return {"verdict": verdict, "scores": scores, "evidence": quotes}


def write_verdict(db, session_id: str, verdict: dict, model: str,
                  validation_batch_id: str | None) -> JudgeResult:
    """Write a tier-2 ledger row, or refuse with a counted reason.

    Refusals before anything is written: the schema guards must be installed,
    and the named batch must pass :func:`batch_status` — it exists, holds
    labels, is approved, and clears the configured floor.

    The schema refuses all of those on its own — that is the point of
    migration 137 — but a Python-side refusal returns a reason a counter can
    aggregate, whereas the trigger returns one ``IntegrityError`` that reads
    identically whether the batch was unapproved, too small, or absent.
    """
    result = JudgeResult(
        session_id=session_id,
        verdict=verdict.get("verdict"),
        scores=verdict.get("scores") or {},
        evidence=verdict.get("evidence") or [],
        model=model,
    )

    missing = guards_present(db)
    if missing:
        result.reason = Refusal.GUARD_MISSING
        result.detail = (
            f"schema guards absent from session_distillations: "
            f"{', '.join(missing)} — refusing to write a judged verdict "
            f"without the protection this path assumes"
        )
        return result

    ok, reason, detail = batch_status(db, validation_batch_id)
    if not ok:
        result.reason = reason
        result.detail = detail
        return result

    scores = result.scores
    with db.write():
        db.execute(
            """
            INSERT INTO session_distillations (
                session_id, rubric_version, verdict,
                score_protocol_compliance, score_task_outcome,
                score_communication_contract, score_tool_routing,
                evidence_quotes, judge_model, validation_batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                rubric_version               = excluded.rubric_version,
                verdict                      = excluded.verdict,
                score_protocol_compliance    = excluded.score_protocol_compliance,
                score_task_outcome           = excluded.score_task_outcome,
                score_communication_contract = excluded.score_communication_contract,
                score_tool_routing           = excluded.score_tool_routing,
                evidence_quotes              = excluded.evidence_quotes,
                judge_model                  = excluded.judge_model,
                validation_batch_id          = excluded.validation_batch_id
            """,
            (
                session_id, RUBRIC_VERSION, result.verdict,
                scores.get("protocol_compliance"), scores.get("task_outcome"),
                scores.get("communication_contract"), scores.get("tool_routing"),
                json.dumps(result.evidence, ensure_ascii=False),
                model, validation_batch_id,
            ),
        )
    result.written = True
    return result


def judge_session(db, session_id: str, validation_batch_id: str | None = None,
                  capability: str = "standard") -> JudgeResult:
    """Judge one session end to end. Refuses before spending anything.

    The batch check runs FIRST, before the model call. A dormant tier-2 that
    paid for inference and then refused to store the answer would be the
    expensive way to do nothing.
    """
    missing = guards_present(db)
    if missing:
        return JudgeResult(
            session_id, reason=Refusal.GUARD_MISSING,
            detail=f"missing triggers: {', '.join(missing)}",
        )
    ok, reason, detail = batch_status(db, validation_batch_id)
    if not ok:
        return JudgeResult(session_id, reason=reason, detail=detail)

    from okuro.bridge.invoke import invoke

    from .facets import read_session, render_transcript

    read = read_session(db, session_id)
    if not read.turns:
        return JudgeResult(session_id, reason=Refusal.NO_FACET,
                           detail="no transcript to judge")

    reply = invoke(
        prompt=build_rubric_prompt(render_transcript(read)),
        capability=capability,
        system_prompt=_SYSTEM_PROMPT,
        tool=True,
        isolated=True,
    )
    if not reply.get("success"):
        return JudgeResult(session_id, reason=Refusal.INVOKE_FAILED,
                           detail=str(reply.get("error"))[:200])
    parsed = parse_verdict(reply.get("output") or "")
    if parsed is None:
        return JudgeResult(session_id, reason=Refusal.UNPARSEABLE,
                           detail="judge reply did not parse into a verdict")

    return write_verdict(
        db, session_id, parsed, reply.get("model") or "", validation_batch_id
    )
