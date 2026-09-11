### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: ACE lesson lifecycle — closed target vocabulary, idempotent evidence counters, hysteretic status transitions, and emission onto the EXISTING improvement lifecycle.
# index: imports | LESSON_TARGETS | validate_target_ref | VEC_TABLE | store_candidate | embed_lesson | observe_evidence | recount | transition | emit_lesson_improvements | run_lesson_maintenance | lesson_report
# AGENT_HEADER_END -->
"""What happens to a lesson after it is mined.

:mod:`.mining` produces candidates. This module decides which of them okuro
actually believes, which it stops believing, and how a believed one reaches
the owner — and the third of those is the one with an architectural constraint
attached, so it comes first.

There is no new injection channel, and there will not be one
------------------------------------------------------------
The binding decision (the owner, 2026-07-26) is that okuro's self-improvement
mechanism stays GENERAL while the data flowing through it is PERSONAL. A
lesson-specific pipeline into the bootstrap packet would be a second,
user-shaped path into the most load-bearing config in the system.

So an active lesson does exactly one thing: it writes an
``interaction_improvements`` row and inherits that table's EXISTING lifecycle —
``proposed -> injected/promoted -> verified -> outcome``, baseline stamped at
proposal time and re-measured against the same marker rate
(:func:`okuro.sense.interaction.improve.verify_improvements` picks the row up
with no knowledge that a lesson produced it). Three consequences worth stating
because each was a live temptation:

* **No flow into todos.** ``signal_promote`` creates ``todos`` rows; this
  module never calls it. Self-improvement has its own stream and is measured by
  a marker RATE, not by a checkbox somebody ticks.
* **No eighth surface.** ``target_surface`` is the same seven values
  ``interaction_improvements`` already routes.
* **No free-text target.** See :data:`LESSON_TARGETS` below.

Why the counters are derived and not incremented
------------------------------------------------
``helpful_count`` / ``harmful_count`` decide retirement, and a retirement
decision has to rest on what the corpus showed, not on how many times the
daemon happened to run. Both are RECOMPUTED from ``distill_lesson_evidence``,
whose ``PRIMARY KEY (lesson_id, session_id)`` makes a second observation of the
same session a no-op. Run maintenance five times and the numbers do not move.

What ``harmful_count`` honestly means
-------------------------------------
A post-activation session in the lesson's cluster that STILL exhibits the
lesson's markers. That is evidence the lesson did not work. It is NOT evidence
the lesson caused harm — attributing a regression to one playbook entry is not
something this data can support, and the column name (fixed by migration 136)
overstates it. Retirement therefore reads as "stopped earning its place",
which is the claim the evidence actually carries.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

VEC_TABLE = "vec_distill_lessons"

# ---------------------------------------------------------------------------
# The closed target vocabulary
# ---------------------------------------------------------------------------
# Every okuro-internal location a lesson may address. Mirrored EXACTLY by the
# trigger in migration 138 — the tuple below is what callers validate against
# and what the prompt enumerates, the trigger is what makes it true for writers
# that never import this module (a future agent, the sqlite3 shell).
#
# The vocabulary is closed because the alternative was measured. The sibling
# column `interaction_improvements.target_ref` is bare TEXT filled by a model,
# and USER PROJECT NAMES leaked into it — personal data resting in the general
# mechanism, from a column whose type allowed anything at all. A lesson is
# produced from an even cheaper source than a reviewed proposal, so it gets the
# constraint that column never had.
LESSON_TARGETS: tuple[str, ...] = (
    "bootstrap.failure_modes",
    "bootstrap.conventions",
    "bootstrap.behavioral_contract",
    "bootstrap.composition",
    "tool_protocol.routing",
    "tool_protocol.deliverables",
    "tool_protocol.system_state",
    "subagent_brief.template",
    "principle_set",
    "mcp_middleware.gate",
    "memory.lifecycle",
)

_ROLE_PITFALL_PREFIX = "role.pitfall:"

# ---------------------------------------------------------------------------
# Where a lesson's fix has to land
# ---------------------------------------------------------------------------
# Decision b87d1a8e (the owner, 2026-07-26): DATA IS PERSONAL, MECHANISM IS
# GENERAL. Approving a lesson about a role pitfall or a profile rule edits rows
# in THIS user's database and changes okuro for him alone. Approving a lesson
# about the mcp gate, the brief template, bootstrap composition or a principle
# names a defect in okuro itself — if it fails for one user it fails for all,
# and no amount of local data fixes it. A reviewer deciding in seconds needs to
# know which of the two he is holding, because they have different costs and
# only one of them is finished when he clicks approve.
#
# The map is DETERMINISTIC on the surface, with no model anywhere in it, for
# the reason the decision gives: on the measured batch the surface alone was a
# perfect predictor (4/4 personal, 8/8 general), and run-to-run drift on the
# question of what counts as personal is the exact failure that put CLAUDE.md
# into a proposal twice in one session.
#
# memory_hygiene sits on the CODE side and it is the one worth explaining. The
# decision calls memory CONTENT personal — and it is — but this surface is the
# memory LIFECYCLE (its target_ref is `memory.lifecycle`, the decay rule), and
# the decision lists memory decay among the eight mechanism-surface proposals.
# The rule that keeps them apart: what a memory SAYS is data, when a memory
# EXPIRES is code.
ROUTING_LOCAL = "local"
ROUTING_CODE_SURFACE = "code_surface"

_LOCAL_SURFACES = frozenset({"role", "user_profile"})

# interaction_improvements' seven surfaces, mirrored from migration 138's
# vocabulary trigger. Carried so `known_surface` can tell "classified by the
# map" from "classified by the fail-closed default" — a lesson landing on the
# default is a signal the vocabulary moved and this map did not.
_KNOWN_SURFACES = frozenset({
    "subagent_brief", "enforcement_hook", "role", "user_profile",
    "principle", "bootstrap", "memory_hygiene",
})

_ROUTING_LABELS = {
    ROUTING_LOCAL: "local — applies to your data",
    ROUTING_CODE_SURFACE: "code surface — needs okuro update",
}


def classify_lesson_routing(target_surface: str | None,
                            target_ref: str | None = None) -> dict:
    """Whether approving this lesson edits the user's data or okuro's code.

    Fail-closed on the CODE side. An unrecognised surface is one this function
    predates, and the two mistakes are not symmetric: calling a mechanism
    defect "local" tells the reviewer the work is done when it has not started,
    while calling a data fix "code surface" only over-warns. The same direction
    migrations 136 and 137 chose for their own unknowns.
    """
    surface = str(target_surface or "").strip()
    ref = str(target_ref or "").strip()

    # A role pitfall is personal whatever the surface column says. Belt and
    # braces: the two columns are written by the same miner and could disagree.
    if ref.startswith(_ROLE_PITFALL_PREFIX) or surface in _LOCAL_SURFACES:
        routing = ROUTING_LOCAL
    else:
        routing = ROUTING_CODE_SURFACE

    return {
        "routing": routing,
        "label": _ROUTING_LABELS[routing],
        "known_surface": surface in _KNOWN_SURFACES,
    }

# ---------------------------------------------------------------------------
# Lifecycle constants — the hysteresis band
# ---------------------------------------------------------------------------
# Activation and retirement are deliberately NOT two sides of one threshold. A
# single cut-off makes a lesson oscillate: it crosses, activates, the next
# session pushes it back, it retires, the one after re-activates it, and the
# playbook churns while nothing in the corpus actually changed. Three separate
# brakes:
#
#   1. A BAND. Activation needs corroboration; retirement needs a different
#      quantity entirely (contrary evidence outweighing supporting evidence by
#      RETIRE_RATIO), so no single observation can sit on both boundaries.
#   2. A DWELL CLOCK. No status change within MIN_DWELL_DAYS of the last one,
#      whatever the counters say.
#   3. TERMINAL RETIREMENT. Nothing re-activates a retired lesson
#      automatically. Re-learning a retired lesson is mining's job, and it
#      arrives as a new row with its own evidence.
MIN_DWELL_DAYS = 14
RETIRE_MIN_EVIDENCE = 5
RETIRE_RATIO = 2.0
STALE_DAYS = 90

# Handed to interaction_improvements so the existing verifier judges a lesson
# on the same clock as every other proposal. Imported rather than restated —
# two definitions of the verification window would drift.


def validate_target_ref(target_ref: str | None) -> bool:
    """True when ``target_ref`` names an okuro-internal location.

    ``None`` is valid — a lesson may address a surface as a whole. Everything
    else must be a member of :data:`LESSON_TARGETS`, or ``role.pitfall:<id>``
    for a role that EXISTS. The suffix is checked against the store rather than
    pattern-matched: a prefix-only check would leave the one parameterised
    member accepting arbitrary text, which is the hole the vocabulary exists to
    close.
    """
    if target_ref is None:
        return True
    ref = str(target_ref).strip()
    if not ref:
        return False
    if ref in LESSON_TARGETS:
        return True
    if not ref.startswith(_ROLE_PITFALL_PREFIX):
        return False

    role_id = ref[len(_ROLE_PITFALL_PREFIX):].strip()
    if not role_id:
        return False
    from okuro.db import get_db

    row = get_db().fetchone(
        "SELECT 1 AS ok FROM roles WHERE role_id = ?", (role_id,)
    )
    return bool(row)


def store_candidate(db, *, lesson_text: str, lesson_class: str,
                    target_surface: str, target_ref: str | None,
                    markers: list, cluster_id: int,
                    evidence_session_ids: list, model: str | None = None) -> int:
    """Write one mined lesson as a CANDIDATE and embed it. Returns its id.

    Status is always ``candidate`` here regardless of how well corroborated the
    finding was. Activation is :func:`transition`'s decision and is subject to
    the dwell clock; letting mining write ``active`` directly would let a
    single expensive model call put advice straight in front of the owner.
    """
    from . import RUBRIC_VERSION

    sessions = sorted({str(s) for s in evidence_session_ids})
    with db.write():
        cur = db.execute(
            """
            INSERT INTO distill_lessons
                (lesson_text, lesson_class, status, evidence_session_ids,
                 cluster_id, rubric_version, target_surface, target_ref,
                 markers, corroboration_count, status_changed_at,
                 last_evidence_at)
            VALUES (?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?,
                    datetime('now'), datetime('now'))
            """,
            (
                lesson_text, lesson_class,
                json.dumps(sessions, ensure_ascii=False),
                int(cluster_id), RUBRIC_VERSION, target_surface, target_ref,
                json.dumps(sorted(set(markers)), ensure_ascii=False),
                len(sessions),
            ),
        )
        lesson_id = int(cur.lastrowid)

        # The sessions that asserted the defect, recorded as evidence rows so
        # the counters and the provenance live in one place.
        for sid in sessions:
            db.execute(
                """
                INSERT INTO distill_lesson_evidence
                    (lesson_id, session_id, verdict)
                VALUES (?, ?, 'evidence')
                ON CONFLICT(lesson_id, session_id) DO NOTHING
                """,
                (lesson_id, sid),
            )

    embed_lesson(db, lesson_id, lesson_text)
    if model:
        log.info("mining: lesson #%s stored from cluster %s (%s)",
                 lesson_id, cluster_id, model)
    return lesson_id


def embed_lesson(db, lesson_id: int, lesson_text: str) -> bool:
    """Embed a lesson into ``vec_distill_lessons``. Failure is not fatal.

    UNPARTITIONED, permanently. Lessons number in the hundreds; the only column
    anyone would reach for as a partition key is the lesson id itself, and a
    partition value per lesson preallocates 4 MiB PER LESSON (the measurement
    behind ``embed/repair.py``'s NO_PARTITION). Writes go through
    ``sense.retrieval.vec_write``, the sanctioned chokepoint, which resolves
    the partition question against the live table rather than assuming it.
    """
    from okuro.embed.client import embed_one, to_bytes
    from okuro.sense.retrieval import vec_write

    text = (lesson_text or "").strip()
    if not text:
        return False
    try:
        blob = to_bytes(embed_one(text))
    except Exception as exc:  # noqa: BLE001 — embed service down is a counted
        # refusal, never a lost lesson: the row is already stored.
        log.warning("lessons: embed failed for #%s (%s)", lesson_id, exc)
        return False
    try:
        with db.write():
            db.execute(f"DELETE FROM {VEC_TABLE} WHERE id = ?", (str(lesson_id),))
            vec_write(db, VEC_TABLE, str(lesson_id), blob)
        with db.write():
            db.execute(
                "UPDATE distill_lessons SET embedding_id = ? WHERE id = ?",
                (str(lesson_id), lesson_id),
            )
    except Exception as exc:  # noqa: BLE001 — table absent until ensure_vec_dims
        log.warning("lessons: vec write failed for #%s (%s)", lesson_id, exc)
        return False
    return True


def observe_evidence(db, lesson: dict) -> dict:
    """Record how post-activation sessions in this lesson's cluster behaved.

    A session counts as evidence when it RAN after the lesson last changed
    status and sits in the lesson's cluster. Sessions that predate activation
    say nothing about whether the lesson worked — they are the population it
    was derived from.

    TWO TIMESTAMP FORMATS, and mixing them silently returns the wrong set.
    ``agent_sessions.first_ts`` is ISO-8601 with ``T`` and ``Z``;
    ``status_changed_at`` is ``datetime('now')``'s space-separated form. The
    comparison therefore converts the latter with ``strftime`` rather than
    comparing the two spellings, which would order ``'2026-08-13 09:00:00'``
    after ``'2026-08-13T09:00:00Z'`` on every row (space sorts below ``T``) and
    quietly select the whole corpus. Same trap ``pipeline.py`` documents.
    """
    markers = set(_json_list(lesson["markers"]))
    cluster_id = lesson["cluster_id"]
    if cluster_id is None or not markers:
        return {"considered": 0, "for": 0, "against": 0}

    rows = db.fetchall(
        """
        SELECT f.session_id, f.markers
        FROM distill_facets f
        JOIN agent_sessions s ON s.session_id = f.session_id
        LEFT JOIN distill_lesson_evidence e
               ON e.lesson_id = ? AND e.session_id = f.session_id
        WHERE f.cluster_id = ?
          AND e.session_id IS NULL
          AND s.first_ts IS NOT NULL
          AND s.first_ts > strftime('%Y-%m-%dT%H:%M:%SZ', ?)
        ORDER BY f.session_id
        """,
        (lesson["id"], cluster_id, lesson["status_changed_at"]),
    )

    counts = {"considered": 0, "for": 0, "against": 0}
    if not rows:
        return counts

    with db.write():
        for row in rows:
            counts["considered"] += 1
            recurred = bool(markers & set(_json_list(row["markers"])))
            verdict = "against" if recurred else "for"
            counts[verdict] += 1
            db.execute(
                """
                INSERT INTO distill_lesson_evidence
                    (lesson_id, session_id, verdict)
                VALUES (?, ?, ?)
                ON CONFLICT(lesson_id, session_id) DO NOTHING
                """,
                (lesson["id"], row["session_id"], verdict),
            )
    return counts


def recount(db, lesson_id: int) -> tuple[int, int]:
    """Recompute (helpful, harmful) from the evidence rows. Idempotent.

    Derived rather than incremented: see the module docstring. Running this
    repeatedly cannot move the numbers, which is what makes a retirement
    decision a property of the corpus instead of the scheduler.
    """
    row = db.fetchone(
        """
        SELECT
            SUM(CASE WHEN verdict = 'for'     THEN 1 ELSE 0 END) AS helpful,
            SUM(CASE WHEN verdict = 'against' THEN 1 ELSE 0 END) AS harmful
        FROM distill_lesson_evidence
        WHERE lesson_id = ?
        """,
        (lesson_id,),
    ) or {}
    helpful = int(row.get("helpful") or 0)
    harmful = int(row.get("harmful") or 0)
    with db.write():
        db.execute(
            """
            UPDATE distill_lessons
               SET helpful_count = ?, harmful_count = ?,
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (helpful, harmful, lesson_id),
        )
    return helpful, harmful


def _dwell_elapsed(db, lesson: dict) -> bool:
    """True when the lesson has held its current status long enough to move."""
    if not lesson["status_changed_at"]:
        return True
    row = db.fetchone(
        "SELECT (julianday('now') - julianday(?)) AS days",
        (lesson["status_changed_at"],),
    ) or {}
    days = row.get("days")
    return days is None or float(days) >= MIN_DWELL_DAYS


def transition(db, lesson: dict, *, threshold: int) -> str | None:
    """Decide this lesson's next status, or None to leave it alone.

    Retirement is TERMINAL — nothing here moves a lesson out of ``retired``.
    A defect that recurs after retirement is re-learned by mining as a new row
    with its own evidence, which is both honest about provenance and the reason
    a lesson cannot oscillate.
    """
    status = lesson["status"]
    if status == "retired":
        return None

    helpful = int(lesson["helpful_count"] or 0)
    harmful = int(lesson["harmful_count"] or 0)

    if status == "candidate":
        if int(lesson["corroboration_count"] or 0) < threshold:
            return None
        # Corroboration is NECESSARY AND NOT SUFFICIENT (the owner, 2026-08-15).
        # A mined lesson becomes a rule he has to live under, so the cluster
        # having proved the defect is real does not by itself make the proposed
        # remedy right. Approval is the second key, and the schema refuses an
        # active row without it (migration 143) — this check exists so the
        # maintenance pass declines quietly rather than driving every unapproved
        # candidate into a trigger abort on every weekly run.
        # Bracket, not .get(): a caller passing a row without this column would
        # otherwise get None here and see NO LESSON EVER ACTIVATE, silently and
        # forever. The direction is safe but the diagnosis is expensive, so an
        # incomplete row raises KeyError at the boundary instead. Every other
        # field on this dict is read the same way.
        if not lesson["approved_at"]:
            return None
        return "active"

    # status == 'active'
    if not _dwell_elapsed(db, lesson):
        return None
    if harmful >= RETIRE_MIN_EVIDENCE and harmful >= helpful * RETIRE_RATIO:
        return "retired"

    stale = db.fetchone(
        "SELECT (julianday('now') - julianday(?)) AS days",
        (lesson["last_evidence_at"] or lesson["status_changed_at"],),
    ) or {}
    days = stale.get("days")
    if days is not None and float(days) >= STALE_DAYS and helpful == 0:
        return "retired"
    return None


def _apply_status(db, lesson_id: int, new_status: str,
                  reason: str | None = None) -> None:
    with db.write():
        db.execute(
            """
            UPDATE distill_lessons
               SET status = ?, status_changed_at = datetime('now'),
                   retired_reason = COALESCE(?, retired_reason),
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (new_status, reason, lesson_id),
        )


# ---------------------------------------------------------------------------
# The review surface
# ---------------------------------------------------------------------------
# Bounded on purpose. A reviewer deciding whether a lesson should become a rule
# needs the claim, what it targets, how well corroborated it is, and enough
# evidence to believe it — not the transcripts. Unbounded evidence here would
# rebuild the thing the whole pipeline exists to delete.
_REVIEW_EVIDENCE_SESSIONS = 3
_REVIEW_SNIPPET_CHARS = 240


def lessons_for_review(db=None, limit: int = 20, status: str = "candidate") -> list[dict]:
    """Lessons awaiting a decision, best-corroborated first.

    Evidence is summarised from the tier-1 facets of the sessions that asserted
    the defect — the same rows mining counted — rather than from raw events.
    That keeps the review surface readable after the compactor has been through
    the corpus, and it is the reason this reads ``distill_facets`` and never
    ``agent_events``.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()

    rows = db.fetchall(
        """
        SELECT id, lesson_text, lesson_class, target_surface, target_ref,
               cluster_id, corroboration_count, markers, evidence_session_ids,
               status, created_at, approved_by, approved_at, retired_reason
        FROM distill_lessons
        WHERE status = ?
        ORDER BY corroboration_count DESC, id
        LIMIT ?
        """,
        (status, int(limit)),
    )

    out: list[dict] = []
    for row in rows:
        item = dict(row)
        item["markers"] = _json_list(row["markers"])
        # Attached HERE rather than in each front-end, so the CLI, the MCP
        # tools and the web page cannot end up telling the owner three different
        # things about where the same lesson's fix lands.
        item["routing"] = classify_lesson_routing(
            row["target_surface"], row["target_ref"]
        )
        sessions = _json_list(row["evidence_session_ids"])
        item["evidence_session_count"] = len(sessions)

        snippets: list[dict] = []
        for sid in sessions[:_REVIEW_EVIDENCE_SESSIONS]:
            facet_row = db.fetchone(
                "SELECT facet FROM distill_facets WHERE session_id = ?", (sid,)
            )
            summary = ""
            if facet_row and facet_row["facet"]:
                try:
                    facet = json.loads(facet_row["facet"])
                except (TypeError, ValueError):
                    facet = {}
                if isinstance(facet, dict):
                    summary = str(
                        facet.get("failure_mode") or facet.get("summary") or ""
                    )
            snippets.append({
                "session_id": sid,
                "snippet": summary[:_REVIEW_SNIPPET_CHARS],
            })
        item["evidence"] = snippets
        item.pop("evidence_session_ids", None)
        out.append(item)
    return out


def approve_lesson(lesson_id: int, approved_by: str, db=None) -> dict:
    """Approve a candidate. HUMAN CALL — nothing scheduled invokes this.

    Approval does NOT set the status. It records the decision, and the next
    maintenance pass promotes the lesson if it ALSO clears corroboration and the
    dwell clock. Keeping :func:`transition` the single place status changes
    means the two conditions cannot drift apart, and it preserves the property
    the owner asked for: approving an under-corroborated lesson does not activate
    it, because approval is the second key rather than an override.

    ``approved_by`` must name a person. The automation vocabulary is shared with
    :func:`okuro.sense.distill.rubric.set_min_deletable_rubric_version` rather
    than restated — the two switches answer the same question about who is
    allowed to decide, and two copies of that list would drift.

    What this honestly cannot do: verify a human typed the name. Migration 143
    says the same of its own guard. What it does is make every cheap forgery
    impossible, leaving only a path nobody walks by accident.
    """
    from .rubric import _NON_HUMAN_APPROVERS

    if db is None:
        from okuro.db import get_db

        db = get_db()

    approver = str(approved_by or "").strip()
    if approver.lower() in _NON_HUMAN_APPROVERS:
        raise ValueError(
            f"approving a lesson needs a human approver, got {approved_by!r}. "
            f"A mined lesson becomes a rule somebody lives under; no daemon may "
            f"make that call."
        )

    row = db.fetchone(
        "SELECT id, status FROM distill_lessons WHERE id = ?", (int(lesson_id),)
    )
    if row is None:
        raise ValueError(f"no lesson {lesson_id}")
    if row["status"] == "retired":
        raise ValueError(
            f"lesson {lesson_id} is retired. Retirement is terminal — a defect "
            f"that recurs is re-learned by mining as a new row with its own "
            f"evidence, which keeps provenance honest and stops lessons "
            f"oscillating."
        )

    with db.write():
        db.execute(
            "UPDATE distill_lessons SET approved_by = ?, "
            "approved_at = datetime('now'), updated_at = datetime('now') "
            "WHERE id = ?",
            (approver, int(lesson_id)),
        )
    log.warning("distill: lesson #%s approved by %s", lesson_id, approver)
    return {"lesson_id": int(lesson_id), "approved_by": approver,
            "status": row["status"],
            "note": "recorded; the next maintenance pass activates it if "
                    "corroboration and the dwell clock also allow"}


def reject_lesson(lesson_id: int, reason: str, rejected_by: str = "",
                  db=None) -> dict:
    """Reject a lesson outright — retires it, with the reason on the row.

    Deliberately needs NO approval machinery and no thresholds. Rejecting is the
    SAFE direction: it removes a candidate rule from consideration, so making it
    as cheap as possible is correct. The same asymmetry migration 143 documents
    for automatic retirement.

    A reason is required. A retired row with no reason is indistinguishable
    from one the lifecycle retired on evidence, and the next person looking at
    the playbook cannot tell whether a human decided or a counter did.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()

    why = str(reason or "").strip()
    if not why:
        raise ValueError(
            "rejecting a lesson needs a reason — without one the row is "
            "indistinguishable from an evidence-retired lesson"
        )

    row = db.fetchone(
        "SELECT id, status FROM distill_lessons WHERE id = ?", (int(lesson_id),)
    )
    if row is None:
        raise ValueError(f"no lesson {lesson_id}")

    who = str(rejected_by or "").strip()
    stamped = f"rejected by {who}: {why}" if who else f"rejected: {why}"
    with db.write():
        db.execute(
            "UPDATE distill_lessons SET status = 'retired', retired_reason = ?, "
            "status_changed_at = datetime('now'), updated_at = datetime('now') "
            "WHERE id = ?",
            (stamped, int(lesson_id)),
        )
    log.warning("distill: lesson #%s rejected — %s", lesson_id, stamped)
    return {"lesson_id": int(lesson_id), "status": "retired",
            "retired_reason": stamped}


# The stable key that makes this reminder updatable rather than duplicable.
# ``create_reminder`` has no upsert, so identity lives in the context JSON —
# a structured field, not a substring of the human text, because the text
# carries a COUNT that changes every week and matching on it would find
# nothing and stack a new row each time. That is the exact failure this
# constant exists to prevent.
REMINDER_MARKER = "distill-lesson-review"

# Live states. A reminder the owner dismissed or completed must NOT be revived by
# the next mining run — that would make dismissal meaningless and turn the nudge
# into nagging. Once he has closed it, a genuinely new batch creates a fresh
# one.
_LIVE_REMINDER_STATES = ("pending", "active", "snoozed")


def notify_pending_candidates(db=None) -> dict:
    """Create or UPDATE the single 'candidates awaiting review' reminder.

    The second push channel. Bootstrap surfaces the queue to an agent starting
    a session; this reaches the owner when he is not in one. Both ride existing
    okuro mechanisms — the architecture decision forbids a new channel, and a
    reminder is what okuro already uses to say "this needs you".

    Exactly one reminder exists at a time. Weekly mining runs would otherwise
    stack a near-identical row every Sunday until the reminder list is useless,
    which is worse than not notifying at all: a channel that cries wolf weekly
    is one that gets muted, taking the signal with it.
    """
    if db is None:
        from okuro.db import get_db

        db = get_db()

    pending = int((db.fetchone(
        "SELECT COUNT(*) AS n FROM distill_lessons WHERE status = 'candidate'"
    ) or {}).get("n") or 0)
    if not pending:
        return {"pending": 0, "action": "none",
                "reason": "no candidates awaiting review"}

    what = (f"{pending} lesson candidate{'s' if pending != 1 else ''} awaiting "
            f"review — okuro distill lessons")

    existing = db.fetchone(
        f"""
        SELECT id FROM reminders
        WHERE json_extract(context, '$.marker') = ?
          AND status IN ({','.join('?' * len(_LIVE_REMINDER_STATES))})
        ORDER BY created_at DESC LIMIT 1
        """,
        (REMINDER_MARKER, *_LIVE_REMINDER_STATES),
    )

    if existing:
        # Refresh the count and leave everything else — including when_due —
        # alone. Pushing the due date out every week would let a reminder he
        # keeps not-acting-on drift forever without ever becoming overdue,
        # which is how a nudge stops being one.
        with db.write():
            db.execute(
                "UPDATE reminders SET what = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (what, existing["id"]),
            )
        return {"pending": pending, "action": "updated",
                "reminder_id": existing["id"]}

    from datetime import datetime, timedelta, timezone

    from okuro.sense.reminders.engine import create_reminder

    created = create_reminder(
        what=what,
        when_due=datetime.now(timezone.utc) + timedelta(days=1),
        urgency=2,
        project="okuro",
        context={"marker": REMINDER_MARKER, "pending": pending},
        source="system",
    )
    return {"pending": pending, "action": "created",
            "reminder_id": created.get("id") if isinstance(created, dict) else None}


def emit_lesson_improvements(db=None, limit: int = 20) -> dict:
    """Give every active, unproposed lesson a row on the EXISTING lifecycle.

    Deliberately thin. It stamps a baseline from the same
    :func:`~okuro.sense.interaction.improve._marker_rate` every other proposal
    uses, registers the same ``source='proactive'`` signal, and writes an
    ``interaction_improvements`` row in ``proposed``. From that moment the
    lesson is invisible to the improvement machinery — ``verify_improvements``
    re-measures it on its own clock and writes the outcome, with no idea a
    lesson produced it.

    ``signal_promote`` is NOT called, here or anywhere in this package: it
    creates ``todos`` rows, and self-improvement must not flow into the user's
    task list.

    The ``approved_at IS NOT NULL`` clause is REDUNDANT and deliberate.
    Migration 143 makes an unapproved active row unrepresentable, so
    ``status = 'active'`` already implies approval and this selects the same
    set either way. It is spelled anyway because the invariant then reads
    locally: somebody auditing what reaches the owner sees the requirement here,
    rather than having to know a trigger two files away is what makes the
    query safe. Same reasoning as lifecycle.py's explicit ``type != 'user'``
    guard next to a tuple that already excludes it.
    """
    import uuid

    from okuro.sense.interaction.improve import (
        BASELINE_WINDOW_DAYS,
        VERIFY_AFTER_DAYS,
        _marker_rate,
    )
    from okuro.sense.signals import signal_add

    if db is None:
        from okuro.db import get_db

        db = get_db()

    rows = db.fetchall(
        """
        SELECT id, lesson_text, lesson_class, target_surface, target_ref,
               markers, cluster_id, corroboration_count, evidence_session_ids
        FROM distill_lessons
        WHERE status = 'active'
          AND approved_at IS NOT NULL
          AND improvement_id IS NULL
          AND target_surface IS NOT NULL
        ORDER BY corroboration_count DESC, id
        LIMIT ?
        """,
        (int(limit),),
    )

    emitted: list[dict] = []
    skipped: dict[str, str] = {}
    for row in rows:
        markers = _json_list(row["markers"])
        if not markers:
            skipped[str(row["id"])] = "no markers — unverifiable"
            continue

        baseline = _marker_rate(markers, BASELINE_WINDOW_DAYS)
        imp_id = str(uuid.uuid4())
        title = f"[lesson] {str(row['lesson_text'])[:160]}"
        rationale = (
            f"Mined from distill cluster {row['cluster_id']}, corroborated by "
            f"{row['corroboration_count']} distinct sessions "
            f"({row['lesson_class']})."
        )

        signal_id = None
        try:
            sig = signal_add(
                source="proactive",
                severity="info",
                summary=f"[{row['target_surface']}] {str(row['lesson_text'])[:120]}",
                source_ref=f"distill_lesson:{row['id']}",
                evidence={
                    "surface": row["target_surface"],
                    "target_ref": row["target_ref"],
                    "markers": markers,
                    "cluster_id": row["cluster_id"],
                    "corroboration": row["corroboration_count"],
                    "baseline_rate": baseline,
                    "lesson_class": row["lesson_class"],
                },
                suggested_action=str(row["lesson_text"]),
            )
            signal_id = sig.get("id") if isinstance(sig, dict) else None
        except Exception as exc:  # noqa: BLE001 — a missing signal must not
            # lose the improvement row; the join is nullable by design.
            log.warning("lessons: signal_add failed for #%s (%s)", row["id"], exc)

        with db.write():
            db.execute(
                """
                INSERT INTO interaction_improvements
                    (id, signal_id, surface, target_ref, title, proposal,
                     rationale, markers, baseline_value, baseline_window,
                     verify_after, status, model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        date('now', ?), 'proposed', ?)
                """,
                (
                    imp_id, signal_id, row["target_surface"], row["target_ref"],
                    title[:200], str(row["lesson_text"]), rationale,
                    json.dumps(markers, ensure_ascii=False), baseline,
                    f"{BASELINE_WINDOW_DAYS}d", f"+{VERIFY_AFTER_DAYS} days",
                    "distill-mining",
                ),
            )
            db.execute(
                "UPDATE distill_lessons SET improvement_id = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (imp_id, row["id"]),
            )

        emitted.append({
            "lesson_id": row["id"],
            "improvement_id": imp_id,
            "signal_id": signal_id,
            "surface": row["target_surface"],
            "target_ref": row["target_ref"],
            "baseline_rate": baseline,
        })

    return {"emitted": emitted, "skipped": skipped, "considered": len(rows)}


def run_lesson_maintenance(db=None, threshold: int | None = None) -> dict:
    """Observe, recount, transition, emit. The daemon's lesson half.

    Order matters and is not arbitrary: evidence must be observed before the
    counters are recomputed, and the counters must be current before a
    transition reads them. Emission runs last so a lesson activated in this
    pass reaches the improvement lifecycle in the same pass rather than waiting
    a week for the next one.
    """
    from .mining import min_corroboration

    if db is None:
        from okuro.db import get_db

        db = get_db()

    limit = threshold if threshold is not None else min_corroboration()

    lessons = db.fetchall(
        """
        SELECT id, status, markers, cluster_id, corroboration_count,
               helpful_count, harmful_count, status_changed_at, last_evidence_at,
               approved_at
        FROM distill_lessons
        WHERE status IN ('candidate', 'active')
        ORDER BY id
        """
    )

    observed = {"considered": 0, "for": 0, "against": 0}
    transitions: dict[str, int] = {}

    for lesson in lessons:
        row = dict(lesson)
        if row["status"] == "active":
            counts = observe_evidence(db, row)
            for key in observed:
                observed[key] += counts.get(key, 0)
            if counts["considered"]:
                with db.write():
                    db.execute(
                        "UPDATE distill_lessons SET last_evidence_at = "
                        "datetime('now') WHERE id = ?",
                        (row["id"],),
                    )
            recount(db, row["id"])
            # Re-read rather than patching the in-memory row: observe_evidence,
            # the last_evidence_at bump and recount have all written since it
            # was fetched, and `transition` reads every one of those columns.
            row = dict(db.fetchone(
                "SELECT id, status, markers, cluster_id, corroboration_count, "
                "helpful_count, harmful_count, status_changed_at, "
                "last_evidence_at, approved_at FROM distill_lessons WHERE id = ?",
                (row["id"],),
            ))

        new_status = transition(db, row, threshold=limit)
        if new_status:
            reason = None
            if new_status == "retired":
                reason = (
                    f"contrary evidence {row['harmful_count']} vs supporting "
                    f"{row['helpful_count']}"
                    if int(row["harmful_count"] or 0) >= RETIRE_MIN_EVIDENCE
                    else f"no supporting evidence in {STALE_DAYS} days"
                )
            _apply_status(db, row["id"], new_status, reason)
            key = f"{row['status']}->{new_status}"
            transitions[key] = transitions.get(key, 0) + 1

    emitted = emit_lesson_improvements(db)

    log.info("distill lessons: %d maintained, transitions %s, %d emitted",
             len(lessons), transitions, len(emitted["emitted"]))
    return {
        "maintained": len(lessons),
        "observed": observed,
        "transitions": transitions,
        "emitted": emitted,
    }


def lesson_report(limit: int = 50) -> list[dict]:
    """Lessons with their class, status, counters and measured effect."""
    from okuro.db import get_db

    rows = get_db().fetchall(
        """
        SELECT l.id, l.lesson_class, l.status, l.cluster_id, l.target_surface,
               l.target_ref, l.corroboration_count, l.helpful_count,
               l.harmful_count, l.rubric_version, l.retired_reason,
               l.created_at, i.outcome AS improvement_outcome
        FROM distill_lessons l
        LEFT JOIN interaction_improvements i ON i.id = l.improvement_id
        ORDER BY l.created_at DESC
        LIMIT ?
        """,
        (int(limit),),
    )
    return [dict(r) for r in rows]


def _json_list(blob) -> list:
    try:
        parsed = json.loads(blob or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
