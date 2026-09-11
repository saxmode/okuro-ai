### SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Draw a stratified calibration sample, render each session as a digest a human can judge in seconds, and stamp the approval that unlocks tier-2.
# index: imports | strata | select_calibration_sample | create_calibration_batch | pending_sessions | session_digest | record_label | approval_status | approve_batch
# AGENT_HEADER_END -->
"""The calibration set: choosing it, showing it, and approving it.

Tier-2 is dormant until a batch holds at least
``distill_config.min_validation_labels`` distinct labelled sessions AND carries
an explicit approval (migration 137). This module is what gets a human from
zero to that state, and its whole design constraint is HIS TIME — the corpus is
8598 facets, and the difference between a workable calibration and an
abandoned one is whether judging one session takes five seconds or five
minutes.

Three things follow from that.

**The sample is drawn, not browsed.** :func:`select_calibration_sample` spans
the live cluster map proportionally, at least one per cluster, across
flagged/routine and main/subagent. Migration 143 then pins it as a roster, so a
label can only name a session that was actually drawn — otherwise the floor is
a row count rather than coverage, and fifty labels of the fifty easiest
sessions clear it exactly as well as a designed fifty.

**The digest is small and it is honest.** :func:`session_digest` is capped at
:data:`DIGEST_MAX_LINES` and reads ``content_json`` message blocks — every
ingester's envelope, resolved by shape in :func:`_content_text`, because the
draw carries no provider filter. The flat ``agent_events.text`` column
concatenates thinking, tool_use arguments and tool_result bodies
(``trace/claude_code.py::_flatten_content``), so a digest built from it would
put the model's private reasoning and the contents of every file it read in
front of the labeller; it is read only where a row has no ``content_json`` at
all, and the digest says so when it does. A session whose bodies the compactor
has already nulled is shown AS a skeleton, with the flag — and a session whose
shape this reader cannot parse says THAT, rather than rendering empty. The one
thing worse than a thin digest is a thin digest that looks like a quiet
session, and a header counting turns over a blank body is exactly that.

**Resume is a position, not a flag.** :func:`pending_sessions` returns the
roster minus the labels, in draw order. A run that dies between two sessions
resumes exactly where it stopped, and a half-finished batch is a prefix rather
than an arbitrary subset — the lesson migrations 135 and 142 were both written
for.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# The plan's figure. The floor that actually gates tier-2 is
# distill_config.min_validation_labels (50), so the draw carries headroom: a
# labeller who abandons a handful of sessions still clears the gate.
DEFAULT_SAMPLE_SIZE = 60

# A digest longer than this stops being a digest. Measured against the job:
# ~60 sessions at a glance each, so the unit that matters is one screen.
DIGEST_MAX_LINES = 20
DIGEST_TURN_CHARS = 400

VERDICTS = ("good", "bad", "mixed", "unusable")


@dataclass
class SampleRow:
    session_id: str
    cluster_id: int | None
    stratum: str


@dataclass
class SampleReport:
    """What the draw actually did — printed, because a sample nobody can audit
    is a sample nobody should trust."""

    rows: list[SampleRow] = field(default_factory=list)
    by_stratum: dict[str, int] = field(default_factory=dict)
    clusters_covered: int = 0
    clusters_total: int = 0
    notes: list[str] = field(default_factory=list)


def _stratum(session_class: str, flagged: int) -> str:
    return f"{session_class}/{'flagged' if flagged else 'routine'}"


def select_calibration_sample(db, target: int = DEFAULT_SAMPLE_SIZE) -> SampleReport:
    """Draw a stratified sample across the live cluster map.

    Proportional to cluster size with a floor of one per cluster, and within a
    cluster the pick alternates strata so flagged and routine, main and
    subagent, all reach the labeller.

    THE FLOOR CAN EXCEED THE TARGET, and when it does the floor wins. The live
    corpus has 61 clusters against a target of 60: dropping a cluster to hit a
    round number would mean the one population nobody looked at is chosen by
    arithmetic. The report says so rather than silently returning 61.

    Deterministic: ordered by cluster, then stratum, then session_id, so two
    draws over an unchanged corpus produce the same sample and a re-draw is a
    re-draw rather than a different experiment.
    """
    report = SampleReport()

    clusters = db.fetchall(
        """
        SELECT cluster_id, COUNT(*) AS n
        FROM distill_facets
        WHERE cluster_id IS NOT NULL AND triage = 'judge' AND stage = 'tier1'
        GROUP BY cluster_id
        ORDER BY n DESC, cluster_id
        """
    )
    report.clusters_total = len(clusters)
    if not clusters:
        report.notes.append(
            "no clustered tier-1 facets — run the pilot or the daemon first"
        )
        return report

    total = sum(c["n"] for c in clusters)
    if target < len(clusters):
        report.notes.append(
            f"target {target} is below the cluster count {len(clusters)}; "
            f"drawing one per cluster instead so no population is dropped by "
            f"arithmetic"
        )

    # Proportional allocation with a floor of 1. Largest-remainder rather than
    # rounding each share independently: independent rounding does not sum to
    # the target and silently over- or under-draws.
    quotas: dict[int, int] = {c["cluster_id"]: 1 for c in clusters}
    remaining = max(0, target - len(clusters))
    if remaining:
        shares = [
            (c["cluster_id"], remaining * c["n"] / total) for c in clusters
        ]
        for cid, share in shares:
            quotas[cid] += int(share)
        allocated = sum(quotas.values()) - len(clusters)
        leftover = remaining - allocated
        for cid, share in sorted(shares, key=lambda s: -(s[1] % 1)):
            if leftover <= 0:
                break
            quotas[cid] += 1
            leftover -= 1

    picked: list[SampleRow] = []
    for cluster in clusters:
        cid = cluster["cluster_id"]
        rows = db.fetchall(
            """
            SELECT session_id, session_class, flagged
            FROM distill_facets
            WHERE cluster_id = ? AND triage = 'judge' AND stage = 'tier1'
            ORDER BY session_id
            """,
            (cid,),
        )
        # Round-robin the strata inside the cluster so a quota of 2 in a
        # cluster that is 90% subagent/routine still has a chance of surfacing
        # its flagged minority.
        buckets: dict[str, list] = {}
        for r in rows:
            buckets.setdefault(
                _stratum(r["session_class"], r["flagged"]), []
            ).append(r["session_id"])
        order = sorted(buckets)
        want = quotas[cid]
        i = 0
        while want > 0 and any(buckets[s] for s in order):
            stratum = order[i % len(order)]
            i += 1
            if not buckets[stratum]:
                continue
            sid = buckets[stratum].pop(0)
            picked.append(SampleRow(sid, cid, stratum))
            report.by_stratum[stratum] = report.by_stratum.get(stratum, 0) + 1
            want -= 1

    report.rows = picked
    report.clusters_covered = len({r.cluster_id for r in picked})
    return report


def create_calibration_batch(db, target: int = DEFAULT_SAMPLE_SIZE,
                             batch_id: str | None = None,
                             labelled_by: str = "owner") -> tuple[str, SampleReport]:
    """Draw a sample and write it as a batch plus its roster.

    ``labelled_by`` is set at creation rather than left NULL: migration 137
    declares the column NOT NULL, and the honest value is whoever is about to
    label — the field records authorship, not completion. What actually gates
    tier-2 is ``approved_at``/``approved_by``, and those stay NULL until
    :func:`approve_batch`. Leaving a batch un-approved is the dormant state;
    there is no second flag that needs to agree with it.
    """
    report = select_calibration_sample(db, target=target)
    if not report.rows:
        raise ValueError("; ".join(report.notes) or "no sessions available to draw")

    bid = batch_id or f"calib-{uuid.uuid4().hex[:12]}"
    from . import RUBRIC_VERSION

    with db.write():
        db.execute(
            "INSERT INTO distill_validation_batches "
            "(batch_id, rubric_version, labelled_by, notes) VALUES (?, ?, ?, ?)",
            (
                bid, RUBRIC_VERSION, labelled_by,
                json.dumps({
                    "target": target,
                    "drawn": len(report.rows),
                    "clusters_covered": report.clusters_covered,
                    "clusters_total": report.clusters_total,
                    "by_stratum": report.by_stratum,
                    "notes": report.notes,
                }, ensure_ascii=False),
            ),
        )
        for position, row in enumerate(report.rows):
            db.execute(
                "INSERT INTO distill_validation_members "
                "(batch_id, session_id, cluster_id, stratum, position) "
                "VALUES (?, ?, ?, ?, ?)",
                (bid, row.session_id, row.cluster_id, row.stratum, position),
            )
    return bid, report


def pending_sessions(db, batch_id: str) -> list[dict]:
    """Roster minus labels, in draw order. The resume POSITION.

    Not a flag on the member row: a flag has to be written after the label and
    a run that dies between the two writes then either re-asks a labelled
    session or skips an unlabelled one. Deriving the answer from the two tables
    cannot drift, because there is nothing to keep in sync.
    """
    return db.fetchall(
        """
        SELECT m.session_id, m.cluster_id, m.stratum, m.position
        FROM distill_validation_members m
        WHERE m.batch_id = ?
          AND NOT EXISTS (SELECT 1 FROM distill_validation_labels l
                          WHERE l.batch_id = m.batch_id
                            AND l.session_id = m.session_id)
        ORDER BY m.position
        """,
        (batch_id,),
    )


def session_digest(db, session_id: str, max_lines: int = DIGEST_MAX_LINES) -> list[str]:
    """A session rendered small enough to judge at a glance.

    Reads the facet row for what was already derived, and ``content_json``
    message blocks for what was actually said — see :func:`_content_text` for
    the envelopes. It does NOT read ``agent_events.text`` where a structured
    column exists: that column is ``_flatten_content``'s concatenation of
    thinking, tool_use arguments and tool_result bodies, so a digest built from
    it would show the labeller the model's private reasoning and the contents
    of every file it read.

    Two lines exist purely so the digest cannot lie by omission: one when the
    turns came from the flat column, one when the header counts turns and
    nothing rendered.
    """
    facet = db.fetchone(
        """
        SELECT session_class, triage, flagged, flag_reasons, tool_calls,
               user_turns, assistant_messages, provider, facet, cluster_id,
               bodies_available, compliance_normalized, markers
        FROM distill_facets WHERE session_id = ?
        """,
        (session_id,),
    )
    lines: list[str] = []
    if facet is None:
        return [f"{session_id}: no facet row — nothing derived for this session"]

    try:
        doc = json.loads(facet["facet"] or "{}")
    except (TypeError, ValueError):
        doc = {}

    head = (
        f"{facet['provider'] or '?'} · {facet['session_class']} · "
        f"cluster {facet['cluster_id']} · {facet['tool_calls']} tool calls · "
        f"{facet['user_turns']}u/{facet['assistant_messages']}a turns"
    )
    lines.append(head)
    if doc:
        lines.append(
            f"facet: {doc.get('task_type', '?')} → {doc.get('outcome', '?')}"
        )
        summary = (doc.get("summary") or "").strip()
        if summary:
            lines.append(f"  {summary[:DIGEST_TURN_CHARS]}")
        if doc.get("failure_mode"):
            lines.append(f"  failure: {doc['failure_mode'][:200]}")

    try:
        reasons = json.loads(facet["flag_reasons"] or "[]")
    except (TypeError, ValueError):
        reasons = []
    if facet["flagged"]:
        lines.append(f"FLAGGED: {', '.join(reasons) or 'unspecified'}")
    if facet["compliance_normalized"] is not None:
        lines.append(f"compliance: {facet['compliance_normalized']:.2f}")

    # A compacted session is a SKELETON and must look like one. Showing it as a
    # short conversation would invite a verdict about content that was deleted
    # rather than absent.
    if not facet["bodies_available"]:
        lines.append(
            "— BODIES COMPACTED: the transcript was nulled by the retention "
            "compactor. What follows is a skeleton, NOT a quiet session; judge "
            "the facet, or skip."
        )

    # Rendered BEFORE the separator is written, so a provenance warning can sit
    # above it instead of being spliced into a list already terminated.
    turns, unstructured = _text_turns(db, session_id, max_lines - len(lines) - 1)
    claimed = (facet["user_turns"] or 0) + (facet["assistant_messages"] or 0)
    if unstructured:
        lines.append(
            "— NO STRUCTURED CONTENT: this provider records no content_json, "
            "so the turns below are the flattened event text, not message "
            "blocks. Weaker provenance — judge accordingly."
        )
    elif not turns and claimed:
        # The failure this guard exists for: a header counting turns over an
        # empty body reads as a quiet session, which is a verdict about content
        # that is present and unread rather than absent. Same reasoning as the
        # BODIES COMPACTED line above, for the shape case rather than the
        # retention case.
        lines.append(
            f"— NO TURNS RENDERED although the header counts "
            f"{facet['user_turns']}u/{facet['assistant_messages']}a. This "
            f"transcript is in a shape the digest cannot read — report it, do "
            f"NOT judge this session as quiet."
        )
    lines.append("—")
    lines.extend(turns)
    return lines[:max_lines]


# Item kinds that carry visible message text, across every ingester. codex uses
# input_text/output_text (trace/codex.py::_flatten_content_items), claude_code
# uses text, gemini emits items with no type key at all.
#
# summary_text is deliberately ABSENT. That is codex's chain of thought, parked
# under payload.summary — the same private reasoning this module refuses to take
# from agent_events.text.
_TEXT_ITEM_KINDS = frozenset({"text", "input_text", "output_text"})


def _content_text(payload: object) -> str:
    """Visible message text from one ``content_json`` envelope, whoever wrote it.

    Shape-driven, not provider-driven, because the draw has no provider filter:
    ``select_calibration_sample`` gates on triage and stage only, so this reader
    meets every envelope in the corpus and will meet the next one too. Keying on
    a provider column would leave that next ingester rendering blank pages until
    somebody noticed.

    Three live shapes, one descent:

    * ``{"content": …}``                       — claude_code, gemini
    * ``{"payload": {"content": …}}``          — codex stores the WHOLE raw
      envelope (``trace/codex.py`` dumps ``raw``), so the message sits one level
      down and there is no top-level ``content`` key at all
    * items as bare strings, or dicts whose ``type`` is a text kind, or dicts
      with no ``type`` at all but a ``text`` string — the gemini case
    """
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""

    content = payload.get("content")
    if content is None:
        inner = payload.get("payload")
        if isinstance(inner, dict):
            content = inner.get("content")

    if isinstance(content, str):
        blocks = [content]
    elif isinstance(content, list):
        blocks = []
        for item in content:
            if isinstance(item, str):
                blocks.append(item)
            elif isinstance(item, dict):
                kind = item.get("type")
                if kind is not None and kind not in _TEXT_ITEM_KINDS:
                    continue
                text = item.get("text")
                if isinstance(text, str):
                    blocks.append(text)
    else:
        return ""
    return "\n".join(b for b in blocks if b and b.strip())


def _text_turns(db, session_id: str, budget: int) -> tuple[list[str], bool]:
    """The opening exchange, cleaned. Returns ``(turns, used_flat_text)``.

    The second element is true when at least one row carried NO ``content_json``
    at all and the flat ``text`` column was read instead. That fallback is keyed
    on the ROW rather than on a provider name so any ingester with the same gap
    is covered, and the caller declares it — ``text`` is the concatenation this
    module normally refuses, and the labeller has to know which source they are
    reading. It fires only where the structured column is absent, so the
    providers that do write it are unaffected.
    """
    if budget <= 0:
        return [], False
    from okuro.sense.interaction.turns import clean_turn_text

    rows = db.fetchall(
        "SELECT type, text, content_json FROM agent_events "
        "WHERE session_id = ? AND type IN ('user', 'assistant') "
        "ORDER BY ord LIMIT 40",
        (session_id,),
    )
    out: list[str] = []
    unstructured = False
    for r in rows:
        if len(out) >= budget:
            break
        raw = r["content_json"]
        if raw:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = {}
            body = _content_text(payload)
        else:
            unstructured = True
            body = str(r["text"] or "")
        if r["type"] == "user":
            body = clean_turn_text(body) or body
        body = " ".join(body.split())
        if not body:
            continue
        who = "user" if r["type"] == "user" else "asst"
        out.append(f"{who}: {body[:DIGEST_TURN_CHARS]}")
    return out, unstructured


def record_label(db, batch_id: str, session_id: str, verdict: str,
                 note: str | None = None, scores: dict | None = None) -> None:
    """Write one label. The roster trigger refuses anything off-sample.

    Dimension scores default from the verdict so the common case is ONE
    keypress. That default is a claim about the labeller's intent, not a
    measurement, so it is coarse on purpose: a 'good' session is recorded as
    broadly good rather than as four precise numbers nobody typed. A caller
    with real per-dimension opinions passes them.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    resolved = dict(_DEFAULT_SCORES.get(verdict) or {})
    resolved.update(scores or {})
    with db.write():
        db.execute(
            """
            INSERT INTO distill_validation_labels
                (batch_id, session_id, verdict, score_protocol_compliance,
                 score_task_outcome, score_communication_contract,
                 score_tool_routing, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id, session_id, verdict,
                resolved.get("protocol_compliance"),
                resolved.get("task_outcome"),
                resolved.get("communication_contract"),
                resolved.get("tool_routing"),
                (note or "").strip()[:500] or None,
            ),
        )


# Verdict -> coarse dimension defaults. NULL for 'unusable' on every axis: an
# unusable session was not assessed on any of them, and 0.0 would mean it was
# assessed and found absent — the distinction migration 136 spells out.
_DEFAULT_SCORES = {
    "good": {"protocol_compliance": 0.8, "task_outcome": 0.8,
             "communication_contract": 0.8, "tool_routing": 0.8},
    "mixed": {"protocol_compliance": 0.5, "task_outcome": 0.5,
              "communication_contract": 0.5, "tool_routing": 0.5},
    "bad": {"protocol_compliance": 0.2, "task_outcome": 0.2,
            "communication_contract": 0.2, "tool_routing": 0.2},
    "unusable": {},
}


def approval_status(db, batch_id: str) -> dict:
    """Where this batch stands against the gate that actually matters."""
    from .judge import label_floor

    floor = label_floor(db)
    roster = db.fetchone(
        "SELECT COUNT(*) AS n FROM distill_validation_members WHERE batch_id = ?",
        (batch_id,),
    )["n"]
    labels = db.fetchone(
        "SELECT COUNT(*) AS n FROM distill_validation_labels WHERE batch_id = ?",
        (batch_id,),
    )["n"]
    batch = db.fetchone(
        "SELECT approved_at, approved_by FROM distill_validation_batches "
        "WHERE batch_id = ?", (batch_id,)
    )
    return {
        "batch_id": batch_id,
        "roster": roster,
        "labels": labels,
        "floor": floor,
        "remaining": max(0, floor - labels),
        "eligible": labels >= floor,
        "approved": bool(batch and batch["approved_at"]),
        "verdicts": {
            r["verdict"]: r["n"]
            for r in db.fetchall(
                "SELECT verdict, COUNT(*) AS n FROM distill_validation_labels "
                "WHERE batch_id = ? GROUP BY verdict", (batch_id,)
            )
        },
    }


def approve_batch(db, batch_id: str, approved_by: str = "owner") -> dict:
    """Stamp the approval that unlocks tier-2. Refuses below the floor.

    The refusal is checked here so the CLI can say WHY, and enforced by
    migration 137's trigger regardless — this function is a courtesy, not the
    boundary. Approving a batch that already clears the floor is what turns
    every later judge verdict from an assertion into a measurement.
    """
    status = approval_status(db, batch_id)
    if status["approved"]:
        return status
    if not status["eligible"]:
        raise ValueError(
            f"batch {batch_id} holds {status['labels']} labels, below the "
            f"distill_config.min_validation_labels floor of {status['floor']} "
            f"— {status['remaining']} more to go"
        )
    with db.write():
        db.execute(
            "UPDATE distill_validation_batches "
            "SET approved_at = datetime('now'), approved_by = ? "
            "WHERE batch_id = ?",
            (approved_by, batch_id),
        )
    return approval_status(db, batch_id)
