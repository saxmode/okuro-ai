# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The fleet update pipeline — an APPROVED structure action becomes a
#   snapshot, a generated migration for the roles a migration carries, in-store
#   writes for the rest, and a verification measured against a baseline the
#   caller never supplied.
# index:
#   CARRIER / budgets / def _row / def _affected
#   def plan
#   def critique
#   def dry_run
#   def implement  (snapshot -> migration half -> database half)
#   def verify
#   def restore
# AGENT_HEADER_END -->
"""What happens after the owner says yes.

P4 built the backlog: a finding becomes a row, the row has a state, and every
arrow names what must already exist before it moves. Nothing in it writes a
``roles`` row — by design, because the container had to be trustworthy before
anything was allowed to act on it.

This module is the acting. It reads an approved row and produces a change, and
every step below exists because of a specific way this kind of work has gone
wrong before:

=============  =========================================================
step           what it refuses to do
=============  =========================================================
``plan``       compute a change without recording the reading it will
               later be judged against — the baseline is taken HERE,
               before any write, and stored on the row
``critique``   send the whole fleet to one reviewer — the plan is
               chunked, because a subject over the size limit comes
               back as a timeout, and a timeout reads as "no findings"
``dry_run``    summarise. Explicit ids grouped by carrier, five FULL
               before/after bodies, budgets, gate audit and a guard
               scan per body. A count is not a diff.
``implement``  write before it has a way back. The store is snapshotted
               first, under a name naming this action.
``verify``     compare against a number the caller chose
``restore``    guess which snapshot. It takes the newest one recorded
               for THIS action and copies back only the affected rows.
=============  =========================================================

THE SPLIT, AND WHY IT IS COMPUTED TWICE FROM THE SAME TWO FACTS.

The owner ruled Q2: the fleet update splits by origin. A role some migration
already carries keeps being carried by a migration, so its repaired body ships
as SQL in the repo and a fresh install gets it. A role that exists only in this
store is written in this store.

But origin may not be READ: E3 ruled the column out and migration 162 dropped
it, so anything that outlives one migration has to compute the answer —
there is no column left to fall back on. ``repair_plan.carrier_for``
does, from two facts — membership in the role-id set the migration files
INSERT, and whether the CONTENT guard finds a private token in this particular
body. Both have to allow it. A role whose text names a real customer is
written in the database however it got there, because the migration file is
read by the release-by-export pipeline and the ruling on names is not a
preference.

WHAT THIS MODULE DOES NOT DO. It does not approve. ``→ approved`` stays where
P4 put it: behind a loopback endpoint, an actor derived from the profile, and
a state the MCP verb cannot name. ``implement`` refuses from any state but
``approved``, which makes this module the FOURTH fence rather than a way past
the other three.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from okuro.roles.actions import ActionRefused, _hydrate, _row, transition
from okuro.roles.repair_plan import (
    CARRIER_DATABASE,
    CARRIER_MIGRATION,
    Plan,
    PlanRefused,
    carrier_for,
    guard_hits,
    guard_is_armed,
    migration_carried_ids,
    plan_for_roles,
)

logger = logging.getLogger("okuro.roles.structural_update")


class UpdateRefused(ActionRefused):
    """A pipeline gate said no. Nothing was written.

    Subclasses ``ActionRefused`` so every surface P4 already built — the MCP
    verb's ``{"refused": …}`` shape, the API's 409 — handles it without a
    second except clause. A new exception family here would be a second way to
    fail that only some callers know about.
    """


#: How large a subject the uninformed critic can actually read. Measured, not
#: chosen: over roughly this the bridge call times out, and a timed-out
#: critique returns no findings — which reads identically to a clean one. The
#: plan is chunked under it rather than truncated, because a truncated plan is
#: a review of the roles that happened to sort first.
CRITIQUE_CHARS = 7_500

#: How many full before/after bodies the dry-run diff carries. The owner's
#: number. Five is enough to see what the change looks like in practice and
#: few enough that the artifact stays readable; the remaining roles are listed
#: by id with their character deltas, never rolled into an average.
DIFF_SAMPLES = 5

#: The states each verb may be called from. Written as data so the refusal
#: message can name the state the row is actually in.
ENTRY_STATE: dict[str, tuple[str, ...]] = {
    "plan": ("researched",),
    "critique": ("planned",),
    "dry_run": ("planned", "critiqued", "approved", "implemented", "verified"),
    "implement": ("approved",),
    "verify": ("implemented",),
}


# ── small helpers ────────────────────────────────────────────────────


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _short(action_id: str) -> str:
    """The action id, short enough for a filename and still unique in practice."""
    return str(action_id).replace("-", "")[:8]


def _require_state(row: dict, verb: str) -> None:
    allowed = ENTRY_STATE[verb]
    if row["state"] not in allowed:
        raise UpdateRefused(
            f"{verb}() runs on an action in {' or '.join(allowed)}; this one is "
            f"{row['state']!r}. "
            + (
                "Approval is the owner's and it is not reachable from here — the "
                "row has to pass the human gate first."
                if verb == "implement" else
                "The state machine is what orders these steps; calling them out "
                "of order would produce an artifact describing work that has "
                "not happened."
            )
        )


def _affected(row: dict) -> list[str]:
    ids = json.loads(row.get("affected_role_ids") or "[]")
    if not ids:
        raise UpdateRefused(
            "this action names no affected roles. 'Some roles need this' is "
            "not a change that can be planned, diffed or reversed — the "
            "explicit list is what the whole container exists to carry."
        )
    return ids


def _role_rows(db, role_ids: list[str]) -> dict[str, dict]:
    marks = ", ".join("?" for _ in role_ids)
    rows = db.fetchall(
        f"SELECT role_id, domain, tier, maintenance_schedule, prompt, "
        f"lean_prompt, micro_prompt FROM roles WHERE role_id IN ({marks})",
        tuple(role_ids),
    )
    return {dict(r)["role_id"]: dict(r) for r in rows}


def _fit_snapshot(db, role_ids: list[str]) -> list[dict]:
    """One whole ``compute_fit`` result per role, each carrying its rubric.

    The WHOLE result rather than the structure score alone, because A5's
    refusal is stated over the snapshot's own ``rubric_version`` and a
    snapshot reduced to a number cannot be refused for being incomparable — it
    would just compare.
    """
    from okuro.roles.fit import compute_fit

    rows = _role_rows(db, role_ids)
    marks = ", ".join("?" for _ in role_ids)
    knowledge: dict[str, list[dict]] = {rid: [] for rid in role_ids}
    for k in db.fetchall(
        f"SELECT role_id, content, source_url, created_at, confidence "
        f"FROM role_knowledge WHERE role_id IN ({marks})",
        tuple(role_ids),
    ):
        knowledge.setdefault(dict(k)["role_id"], []).append(dict(k))

    out = []
    for rid in role_ids:
        row = rows.get(rid)
        if not row:
            continue
        out.append(compute_fit(row, knowledge.get(rid, [])))
    return out


def _instructions_of(row: dict):
    stored = row.get("plan_json")
    if not stored:
        return None
    try:
        return json.loads(stored).get("instructions")
    except ValueError:
        return None


def _stored_plan(row: dict) -> dict:
    if not row.get("plan_json"):
        raise UpdateRefused(
            f"action {row['id']} carries no computed plan. Run plan() first — "
            f"the diff, the write and the verification all read that one "
            f"computation, which is what makes the approved document and the "
            f"applied change the same thing."
        )
    return json.loads(row["plan_json"])


def _write_artifact(**kwargs) -> str:
    from okuro.sense.artifacts import artifact_write

    got = artifact_write(project="okuro", **kwargs)
    if isinstance(got, str) and got.startswith("REJECTED"):
        raise UpdateRefused(got)
    return got


# ── plan ─────────────────────────────────────────────────────────────


def plan(db, action_id: str, actor: str, *, instructions=None,
         invoke=None) -> dict:
    """Compute the change, take the baseline, write the plan artifact.

    ``instructions`` is the structural change this action describes, in
    ``repair_plan``'s operations vocabulary. It may be omitted on a RE-plan:
    the operations already stored are reused, which is what makes re-planning
    after a re-anchor a repeat of the same instruction rather than a second
    chance to write a different one.

    THE BASELINE IS TAKEN HERE AND NOWHERE ELSE. Migration 158's own header
    named the hole this closes: ``→ verified`` compared against a
    caller-supplied ``fit_before``, so the party asking to be verified chose
    the number it was measured against. Taken at plan time, before a single
    body is written, the reading is of the fleet as it actually is.

    REFUSES ON A RUBRIC THAT MOVED, and refuses HERE rather than at
    verification. A5 forbids comparing scores across rubrics, so an action
    proposed under an older rubric can never be verified — and discovering
    that after the human has approved wastes the one decision in this workflow
    that costs a person's attention. The exit is the re-anchor the state
    machine already has.
    """
    from okuro.roles.fit import rubric_version

    row = _row(db, action_id)
    _require_state(row, "plan")
    role_ids = _affected(row)

    ops = instructions if instructions is not None else _instructions_of(row)
    if ops is None:
        raise UpdateRefused(
            "plan() needs the operations this action performs. A row that "
            "says WHAT changed outside okuro does not yet say what okuro "
            "would do about it, and the planner will not invent the answer."
        )

    current_rubric = rubric_version()
    if row.get("rubric_version") and row["rubric_version"] != current_rubric:
        raise UpdateRefused(
            f"this action was proposed under rubric "
            f"{row['rubric_version']!r} and the scorer now reads "
            f"{current_rubric!r}. A baseline taken now could never be compared "
            f"against it (amendment A5), so verification would refuse after "
            f"the approval rather than before it. Re-anchor the action "
            f"(researched → researched with a fresh run) to bring it under the "
            f"current rubric."
        )

    try:
        computed: Plan = plan_for_roles(
            db, role_ids, instructions=ops, invoke=invoke)
    except PlanRefused as exc:
        raise UpdateRefused(str(exc)) from exc

    if not any(rp.changed for rp in computed.roles.values()):
        raise UpdateRefused(
            "the plan changes nothing on any of the named roles. That is a "
            "real answer — the fleet may already say what the source now says "
            "— but it is a rejection, not a plan: "
            + "; ".join(
                f"{rid}: {rp.refused or 'no change'}"
                for rid, rp in sorted(computed.roles.items())
            )[:600]
        )

    baseline = _fit_snapshot(db, role_ids)
    payload = {
        "instructions": ops if isinstance(ops, dict) else {"operations": ops},
        "summary": computed.summary(),
        "roles": {rid: rp.to_emit_entry() for rid, rp in computed.roles.items()},
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "rubric_version": current_rubric,
    }

    artifact_id = _write_artifact(
        kind="plan",
        title=f"Structure action {_short(action_id)} — plan for "
              f"{len(role_ids)} role(s)",
        summary=(
            f"{computed.summary()['changed']} of {len(role_ids)} roles change; "
            f"{len(computed.by_carrier()[CARRIER_MIGRATION])} carried by a "
            f"migration, "
            f"{len(computed.by_carrier()[CARRIER_DATABASE])} written in the store."
        ),
        body=_render_plan(row, computed),
        created_by=actor,
    )

    # Written BEFORE the transition on purpose. A crash between them leaves a
    # `researched` row with a plan stored, which is harmless; the other order
    # leaves a `planned` row with no plan, which is the state claiming work
    # that does not exist.
    db.execute(
        "UPDATE role_structure_actions SET plan_json = ?, fit_baseline = ?, "
        "updated_at = ? WHERE id = ?",
        (json.dumps(payload), json.dumps(baseline),
         datetime.now(timezone.utc).isoformat(), action_id),
    )
    return transition(db, action_id, "planned", actor=actor,
                      artifact_id=artifact_id,
                      reason=f"plan computed over {len(role_ids)} role(s)")


def _render_plan(row: dict, computed: Plan) -> str:
    split = computed.by_carrier()
    lines = [
        f"# Structure action {row['id']} — plan",
        "",
        f"**{row['title']}**",
        "",
        f"- okuro element: `{row.get('okuro_element') or '—'}`",
        f"- affected roles: {len(computed.roles)} "
        f"({', '.join(sorted(computed.roles)) or 'none'})",
        f"- guard armed on this host: {computed.guard_armed}",
        "",
        "## Operations",
        "",
    ]
    lines += [f"{i}. {op.describe()}" for i, op in enumerate(computed.operations, 1)]
    lines += ["", "## Split by carrier", "",
              "| carrier | roles |", "|---|---|",
              f"| migration | {', '.join(split[CARRIER_MIGRATION]) or '—'} |",
              f"| database | {', '.join(split[CARRIER_DATABASE]) or '—'} |",
              "", "## Per role", "",
              "| role | changed | carrier | full | lean | micro | why |",
              "|---|---|---|---|---|---|---|"]
    for rid, rp in sorted(computed.roles.items()):
        why = rp.refused or "; ".join(rp.reasons) or "no change"
        lines.append(
            f"| {rid} | {'yes' if rp.changed else 'no'} | {rp.carrier} | "
            f"{rp.before['chars']['full']}→{rp.after['chars']['full']} | "
            f"{rp.before['chars']['lean']}→{rp.after['chars']['lean']} | "
            f"{rp.before['chars']['micro']}→{rp.after['chars']['micro']} | "
            f"{why[:180]} |"
        )
    if computed.unknown_role_ids:
        lines += ["", "## Named but not in the store", "",
                  "These ids are on the action and the store does not have "
                  "them. That is a finding about the action.", ""]
        lines += [f"- `{r}`" for r in computed.unknown_role_ids]
    return "\n".join(lines) + "\n"


# ── critique ─────────────────────────────────────────────────────────


def changed_sections(before: str, after: str) -> list[tuple[str, str, str]]:
    """``[(heading, before_body, after_body), ...]`` for what actually moved.

    Per SECTION rather than per body, because a role brief is thousands of
    characters and one operation usually touches one section of it. Sending
    the whole body to a reviewer with a size limit spends the budget on text
    nobody changed; sending the changed sections spends it on the change.

    A section present on one side only comes back with ``""`` for the other,
    which is how an addition and a removal read differently in the rendering.
    """
    from okuro.roles.repair_plan import split_h2

    _, before_secs = split_h2(before or "")
    _, after_secs = split_h2(after or "")
    b = {h.strip().upper(): body for h, body in before_secs}
    a = {h.strip().upper(): body for h, body in after_secs}
    out: list[tuple[str, str, str]] = []
    for heading in list(a) + [h for h in b if h not in a]:
        if b.get(heading, "").strip() != a.get(heading, "").strip():
            out.append((heading, b.get(heading, ""), a.get(heading, "")))
    return out


def _excerpt(text: str, limit: int) -> str:
    """``text`` cut to ``limit``, saying so when it cuts."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text or "(empty)"
    return text[:limit].rstrip() + f"\n… [{len(text) - limit} more characters]"


def _role_subject(rid: str, entry: dict, budget: int) -> str:
    """One role's block for the critic: the counters AND the text.

    THIS USED TO BE METADATA ONLY — character counts, a reasons list, a gate
    boolean — and a reviewer handed that is reviewing a summary of a change,
    not the change. It cannot see a section that says the opposite of what it
    should, a body that repeats itself, or authored prose that reads like a
    template. Every finding such a reviewer can produce is a finding about the
    numbers, which is the one part nobody needed a judgement on.

    The budget is split across the changed sections so one long section cannot
    crowd the others out entirely.
    """
    body = entry.get("_body") or {}
    before = entry.get("_body_before") or {}
    head = (
        f"\n--- {rid} ---\n"
        f"carrier: {'migration' if entry['ships_in_migration'] else 'database'}\n"
        f"reasons: {'; '.join(entry['reasons']) or '(none)'}\n"
        f"sizes: full {entry['before']['chars']['full']}->"
        f"{entry['after']['chars']['full']}, "
        f"lean {entry['before']['chars']['lean']}->"
        f"{entry['after']['chars']['lean']}, "
        f"micro {entry['before']['chars']['micro']}->"
        f"{entry['after']['chars']['micro']}\n"
    )
    blocks: list[str] = []
    for grade in ("full", "lean", "micro"):
        for heading, was, now in changed_sections(
                before.get(grade, ""), body.get(grade, "")):
            blocks.append((grade, heading, was, now))
    if not blocks:
        return head + "(no section-level difference; see the reasons above)\n"

    share = max(300, (budget - len(head)) // (2 * len(blocks)))
    parts = [head]
    for grade, heading, was, now in blocks:
        parts.append(
            f"\n[{grade}] {heading}\nBEFORE:\n{_excerpt(was, share)}\n"
            f"AFTER:\n{_excerpt(now, share)}\n"
        )
    return "".join(parts)


def _chunks(computed: dict, limit: int = CRITIQUE_CHARS) -> list[str]:
    """The plan, split into subjects the reviewer can actually read.

    Chunked BY ROLE and never mid-role: half a role's before/after is a
    subject the reviewer will critique as if the missing half were absent.
    A single role whose rendering is over the limit still goes alone — its
    text is excerpted to fit, and being that large is itself worth a finding.
    """
    head = json.dumps(computed.get("summary", {}), indent=2)
    preamble = f"PLAN SUMMARY\n{head}\n"
    out: list[str] = []
    current = [preamble]
    size = len(preamble)
    for rid, entry in sorted((computed.get("roles") or {}).items()):
        if not entry.get("changed"):
            continue
        block = _role_subject(rid, entry, limit - len(preamble))
        if size + len(block) > limit and len(current) > 1:
            out.append("".join(current))
            current, size = [preamble], len(preamble)
        current.append(block)
        size += len(block)
    out.append("".join(current))
    return out


def critique(db, action_id: str, actor: str, *, critic=None) -> dict:
    """Hand the plan to a model that owns none of it, in readable pieces.

    The findings are stored WHOLE, including the reviewer's verdict on whether
    the plan fixes the instance or the class. That verdict is the reason this
    step exists at all: a fleet-wide structural change that turns out to be an
    instance fix is the failure DP11 names, and it is not one the author of
    the plan is well placed to notice.
    """
    row = _row(db, action_id)
    _require_state(row, "critique")
    stored = _stored_plan(row)

    if critic is None:  # pragma: no cover - the real bridge
        from okuro.critic import critique as critic

    pieces = _chunks(stored)
    results = []
    for i, subject in enumerate(pieces, 1):
        results.append(critic(
            subject=subject,
            kind="plan",
            context=(
                "This is a plan to change okuro role BODIES across a fleet of "
                "about 100 roles. Every role keeps three grades (full, lean, "
                "micro) that must stay consistent; a body may not name a real "
                "customer, person or host; roles carried by a database "
                "migration ship as SQL in the repo and the rest are written in "
                "the store. Judge whether the plan fixes the CLASS or one "
                "instance of it."
            ),
        ))

    failed = [r for r in results if not r.get("ok")]
    if failed and len(failed) == len(results):
        raise UpdateRefused(
            "every critique chunk failed: "
            + "; ".join(str(r.get("error")) for r in failed)[:400]
            + ". A plan with no critique behind it must not be presented for "
              "approval as if it had one."
        )

    artifact_id = _write_artifact(
        kind="report",
        title=f"Structure action {_short(action_id)} — uninformed critique",
        summary=(
            f"{sum(len(r.get('findings') or []) for r in results)} finding(s) "
            f"across {len(pieces)} chunk(s); "
            f"{len(failed)} chunk(s) failed."
        ),
        body=_render_critique(row, pieces, results),
        created_by=actor,
    )
    return transition(db, action_id, "critiqued", actor=actor,
                      artifact_id=artifact_id,
                      reason=f"critique over {len(pieces)} chunk(s), "
                             f"{len(failed)} failed")


def _render_critique(row: dict, pieces: list[str], results: list[dict]) -> str:
    lines = [f"# Structure action {row['id']} — critique", "",
             f"**{row['title']}**", "",
             f"The plan was reviewed in {len(pieces)} chunk(s), each under the "
             f"{CRITIQUE_CHARS}-character limit a single bridge call reliably "
             f"returns from. A chunk that failed is reported as a failure and "
             f"never as a clean pass.", ""]
    for i, res in enumerate(results, 1):
        lines += [f"## Chunk {i}", ""]
        if not res.get("ok"):
            lines += [f"FAILED: {res.get('error')}", ""]
            continue
        lines += [
            f"- scope: **{res.get('scope')}** — {res.get('scope_reason')}",
            f"- the class: {res.get('the_class')}",
            f"- verdict: **{res.get('verdict')}**",
            "",
        ]
        # THE KEYS ARE `claim` / `evidence` / `fix`, AND THEY ALWAYS WERE.
        #
        # This read `title` and `detail`, which `okuro.critic.critique` has
        # never emitted — so every finding rendered as "- **blocker**  — "
        # and the critique artifact, the document a person reads before
        # approving a fleet-wide body change, carried six severities and not
        # one word of what they were about. Found by running the pipeline on
        # a store copy: the severities looked right, which is why nobody
        # noticed the substance was gone.
        for f in res.get("findings") or []:
            if not isinstance(f, dict):
                lines.append(f"- {f}")
                continue
            parts = [f"- **{f.get('severity', '?')}** {f.get('claim', '')}"]
            if f.get("evidence"):
                # The marker stays visible in the prose: a claim the critic
                # could not check must not read like one it could.
                parts.append(f"  - evidence: {f['evidence']}")
            if f.get("fix"):
                parts.append(f"  - fix: {f['fix']}")
            lines.extend(parts)
        lines.append("")
    return "\n".join(lines) + "\n"


# ── dry run ──────────────────────────────────────────────────────────


def dry_run(db, action_id: str, *, samples: int = DIFF_SAMPLES) -> dict:
    """The diff the owner approves. Explicit, whole-bodied and never averaged.

    A summary is what this refuses to be. "34 roles affected, structure up 12
    points" is the shape of claim that started this workstream: it cannot be
    checked, and every part of it could be wrong without anybody noticing. So
    the artifact carries the explicit ids grouped by carrier, five complete
    before AND after bodies, the budget and gate readings on both sides, and
    the guard's answer for every single body — including the ones that are
    clean, because "the guard found nothing" and "the guard was not armed" are
    different facts and only one of them is safe.

    AFTER APPROVAL THE STORED DIFF ID IS NOT REPLACED. `diff_artifact_id` is
    the document the decision was about, so re-running the dry run on an
    approved row produces a new artifact and returns it WITHOUT overwriting
    the old one. Overwriting it would re-label what was approved — the row
    would point at a document written after the decision, and the panel would
    present it as the thing that was reviewed.
    """
    row = _row(db, action_id)
    _require_state(row, "dry_run")
    stored = _stored_plan(row)
    body = _render_diff(row, stored, samples=samples)

    artifact_id = _write_artifact(
        kind="evidence",
        title=f"Structure action {_short(action_id)} — dry-run diff",
        summary=(
            f"{stored['summary']['changed']} of {stored['summary']['roles']} "
            f"roles change. Migration half: "
            f"{len(stored['summary']['migration_carried'])}; database half: "
            f"{len(stored['summary']['database_only'])}."
        ),
        body=body,
        created_by="structural-update",
    )

    frozen = row["state"] not in ("planned", "critiqued")
    if not frozen:
        db.execute(
            "UPDATE role_structure_actions SET diff_artifact_id = ?, "
            "updated_at = ? WHERE id = ?",
            (artifact_id, datetime.now(timezone.utc).isoformat(), action_id),
        )
    out = _hydrate(_row(db, action_id))
    return {
        **out,
        "diff_artifact_id": out.get("diff_artifact_id") or artifact_id,
        # Named separately when the row kept its own: the caller asked for a
        # diff and got one, and it is not the approved one.
        "rendered_artifact_id": artifact_id,
        "diff_frozen": frozen,
    }


def _grade_diff_block(grade: str, before: str, after: str) -> list[str]:
    """One grade's changed sections, before beside after.

    THE DIFF USED TO SHOW ONLY THE `AFTER` BODIES. A reviewer reading the
    proposed text with nothing to compare it against is reading a draft, not a
    diff — the word in the artifact's title was doing work the document did
    not do, and the one question approval turns on ("what is different") had
    to be answered from memory of a role brief nobody memorises.
    """
    sections = changed_sections(before, after)
    if not sections:
        return []
    lines = [f"#### {grade}", ""]
    for heading, was, now in sections:
        if not was.strip():
            lines += [f"**{heading}** — added", "", "```markdown",
                      now.rstrip(), "```", ""]
        elif not now.strip():
            lines += [f"**{heading}** — removed", "", "```markdown",
                      was.rstrip(), "```", ""]
        else:
            lines += [f"**{heading}** — before", "", "```markdown",
                      was.rstrip(), "```", "",
                      f"**{heading}** — after", "", "```markdown",
                      now.rstrip(), "```", ""]
    return lines


def _render_diff(row: dict, stored: dict, *, samples: int) -> str:
    roles = stored.get("roles") or {}
    summary = stored.get("summary") or {}
    changed = sorted(r for r, e in roles.items() if e["changed"])
    armed = guard_is_armed()

    lines = [
        f"# Structure action {row['id']} — dry run",
        "",
        f"**{row['title']}**",
        "",
        "Nothing below has been written. This is what `implement` would do.",
        "",
        "## The split, by carrier",
        "",
        "A role goes into the migration only when a migration already carries "
        "it AND the CONTENT guard finds nothing private in its text. Either "
        "fact alone sends it to the in-store half. Both are recomputed at "
        "implement time on the host that writes, and a disagreement with what "
        "is below is a refusal rather than a silent correction.",
        "",
        "| carrier | count | role ids |",
        "|---|---|---|",
        f"| migration | {len(summary.get('migration_carried') or [])} | "
        f"{', '.join(summary.get('migration_carried') or []) or '—'} |",
        f"| database | {len(summary.get('database_only') or [])} | "
        f"{', '.join(summary.get('database_only') or []) or '—'} |",
        "",
        "## Guard scan",
        "",
        f"Guard armed on this host: **{armed}**."
        + ("" if armed else
           "  \n**The guard loaded no tokens, so it cannot judge.** Nothing "
           "may enter the migration half under that condition — `implement` "
           "refuses rather than shipping bodies nobody checked."),
        "",
        "| role | private tokens found |",
        "|---|---|",
    ]
    for rid in sorted(roles):
        n = len(roles[rid].get("private_tokens") or [])
        # The COUNT, never the tokens. A note about private data is not exempt
        # from the rule it is explaining.
        lines.append(f"| {rid} | {n} |")

    lines += ["", "## Budgets and gates, before and after", "",
              "| role | full | lean | micro | structure ok | gate |",
              "|---|---|---|---|---|---|"]
    for rid in sorted(roles):
        e = roles[rid]
        lines.append(
            f"| {rid} | {e['before']['chars']['full']}→{e['after']['chars']['full']} "
            f"| {e['before']['chars']['lean']}→{e['after']['chars']['lean']} "
            f"| {e['before']['chars']['micro']}→{e['after']['chars']['micro']} "
            f"| {e['before']['ok']}→{e['after']['ok']} "
            f"| {'ok' if e['after'].get('gate_ok') else e['after'].get('gate_error')} |"
        )

    shown = changed[:samples]
    lines += ["", f"## {len(shown)} role(s) in full, before beside after", "",
              "Whole sections, not excerpts. An excerpt of a role brief is "
              "the part the author chose to show.", ""]
    for rid in shown:
        e = roles[rid]
        lines += [f"### {rid}", "",
                  "REASONS: " + ("; ".join(e["reasons"]) or "—"), ""]
        before = e.get("_body_before") or {}
        for grade in ("full", "lean", "micro"):
            block = _grade_diff_block(
                grade, before.get(grade, ""), e["_body"][grade])
            if block:
                lines += block
            elif before.get(grade, "") != e["_body"][grade]:
                # Changed with no section boundary moving — a preamble edit or
                # a sweep. Show both whole bodies rather than claim no change.
                lines += [f"#### {grade} — whole body", "",
                          "**before**", "", "```markdown",
                          (before.get(grade) or "").rstrip(), "```", "",
                          "**after**", "", "```markdown",
                          e["_body"][grade].rstrip(), "```", ""]
    if len(changed) > samples:
        lines += [f"_The other {len(changed) - samples} changed role(s) are "
                  f"listed by id in the tables above; their bodies are in the "
                  f"plan artifact._", ""]
    return "\n".join(lines) + "\n"


# ── implement ────────────────────────────────────────────────────────


def _snapshot(db, action_id: str) -> Path:
    """Copy the whole store before the first write, the runner's way.

    ``sqlite3``'s own backup API, WAL-consistent and needing no checkpoint —
    the same call ``_snapshot_before_migrate`` makes before applying a
    migration, for the same reason and into the same directory. A copy taken
    with ``cp`` while the daemon holds a write lock is a file that opens and
    is missing the last transaction.

    NOT best-effort here, unlike at boot. The runner proceeds without a backup
    rather than blocking a service start; this function's entire job is to be
    the way back, so a snapshot that fails is a refusal.
    """
    src = Path(db._path)
    if str(src) == ":memory:":
        raise UpdateRefused(
            "this store is in memory, so there is nothing to snapshot and "
            "therefore no way back from a write"
        )
    bdir = src.parent / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    dest_path = bdir / f"okuro-prewrite-structure-{_short(action_id)}-{_now_stamp()}.db"
    try:
        dest = sqlite3.connect(str(dest_path))
        try:
            with dest:
                db.conn.backup(dest)
        finally:
            dest.close()
    except Exception as exc:  # noqa: BLE001
        raise UpdateRefused(
            f"the pre-write snapshot failed ({exc}); refusing to write role "
            f"bodies with no way back"
        ) from exc
    logger.info("pre-write snapshot for action %s: %s", action_id, dest_path)
    return dest_path


def snapshots_for(db, action_id: str) -> list[Path]:
    """Every snapshot taken for this action, oldest first (names sort)."""
    bdir = Path(db._path).parent / "backups"
    if not bdir.is_dir():
        return []
    return sorted(bdir.glob(f"okuro-prewrite-structure-{_short(action_id)}-*.db"))


_GENERATED_HEADER = """-- <!-- AGENT_HEADER
-- role: code
-- purpose: {purpose}
-- index: content
-- AGENT_HEADER_END -->
--
-- GENERATED. Do not edit by hand.
--
-- Produced by `okuro.roles.structural_update.implement` from structure action
-- {action_id}, which reached `approved` on {decided_at} by {decided_by}.
--
--   title    : {title}
--   element  : {element}
--   evidence : {evidence}
--
-- WHY THESE ROLES AND NOT THE OTHERS. This file carries the roles a migration
-- already carries AND whose text the CONTENT guard found nothing private in.
-- The rest of the affected roles were written directly in the database by the
-- same run: they exist only in that store, or their own text names something
-- that may not enter this repo. Both halves come from ONE computed plan, so
-- the bodies are the same bodies either way.
--
-- The plan is artifact {plan_artifact}; the reviewed diff is artifact
-- {diff_artifact}.
"""


def _recompute_split(changed: dict) -> tuple[dict, list[str]]:
    """Ask the carrier question again, HERE, on the host that is writing.

    THE CLASS THIS FIXES: A WRITE-SIDE DECISION TAKEN SOMEWHERE ELSE.

    ``ships_in_migration`` was computed by ``plan()``, which may have run on a
    different machine, at a different commit, minutes or days earlier. Two of
    its inputs can move in that window and both move in the dangerous
    direction:

    * the CONTENT guard's token list is per host. A plan computed where the
      guard was unarmed marks every body clean, and implementing that plan
      here would put real names into a file the release-by-export pipeline
      reads — the exact outcome ruling E1 exists to prevent;
    * the set of role ids a migration carries is parsed from the migration
      FILES, so it changes with the checkout.

    So the split is recomputed from the bodies that are about to be written,
    and disagreement with the plan is a REFUSAL rather than a silent
    correction. Correcting it silently would implement a split nobody
    reviewed — the dry-run diff the owner approved names which roles go where.

    Returns ``(recomputed, disagreements)``.
    """
    carried = migration_carried_ids()
    recomputed: dict[str, str] = {}
    disagreements: list[str] = []
    for rid, entry in sorted(changed.items()):
        body = entry["_body"]
        tokens = sorted(
            guard_hits(body["full"]) | guard_hits(body["lean"])
            | guard_hits(body["micro"]))
        now = carrier_for(rid in carried, tokens)
        recomputed[rid] = now
        planned = (CARRIER_MIGRATION if entry["ships_in_migration"]
                   else CARRIER_DATABASE)
        if now != planned:
            # The COUNT of tokens, never the tokens. A refusal explaining that
            # a body carries private names must not quote them.
            disagreements.append(
                f"{rid}: planned {planned}, recomputed {now} "
                f"(carried={rid in carried}, private tokens={len(tokens)})"
            )
    return recomputed, disagreements


def outbox_dir() -> Path:
    """Where a generated migration is LEFT for review. Never a checkout.

    ``~/.okuro/orchestrator/outbox/migrations/``, created on demand.
    """
    from okuro.db.engine import okuro_home

    return okuro_home() / "orchestrator" / "outbox" / "migrations"


def _running_tree() -> Path:
    """The checkout this module was imported from — src/okuro/roles → root."""
    return Path(__file__).resolve().parents[3]


def _resolve_write_target(worktree) -> tuple[Path, str]:
    """Where the generated migration goes. Returns ``(directory, kind)``.

    NOTHING IS EVER WRITTEN INTO THE TREE THIS PROCESS IS RUNNING FROM.

    The default used to be exactly that — ``worktree or _repo_root()`` — which
    put a generated migration into the live ``src/okuro/db/migrations`` of the
    checkout the daemon runs from. The next restart migrates, the runner finds
    a numbered file it has never applied, and applies it. SQL nobody read
    would reach the store by way of a service restart, and the review this
    pipeline is built around ("left for the orchestrator to commit and merge")
    would be skipped by the runner rather than by anybody's decision.

    So the default is an outbox outside every checkout. An explicit worktree
    is still accepted, because the orchestrator legitimately has one, but only
    when it is a BOUND worktree: it carries ``.okuro-worktree`` and it is not
    the tree this code was imported from. An unbound directory is somebody's
    guess at a path, and the running tree is the accident above.
    """
    from okuro.cortex.worktrees import MARKER

    if not worktree:
        target = outbox_dir()
        target.mkdir(parents=True, exist_ok=True)
        return target, "outbox"

    tree = Path(worktree).expanduser().resolve()
    running = _running_tree()
    if tree == running or running in tree.parents or tree in running.parents:
        raise UpdateRefused(
            f"{tree} is (or contains) the checkout this okuro is running "
            f"from. A generated migration written there is applied by the "
            f"next service restart, before anyone has read it. Leave it in "
            f"the outbox ({outbox_dir()}) or name a bound worktree."
        )
    if not (tree / MARKER).is_file():
        raise UpdateRefused(
            f"{tree} carries no {MARKER}, so it is not a bound worktree. "
            f"`okuro wt add <topic>` creates one; without the marker there is "
            f"nothing saying which branch a commit there would land on."
        )
    mdir = tree / "src" / "okuro" / "db" / "migrations"
    if not mdir.is_dir():
        raise UpdateRefused(f"no migrations directory at {mdir}")
    return mdir, "worktree"


def _record_refusal(db, action_id: str, refusal: str) -> None:
    """Put a refusal on the row without moving it.

    A refusal the caller sees and the row does not is a refusal that is gone
    the moment the panel is refreshed — and the panel is where somebody would
    look to find out why an approved action has not been written.
    """
    db.execute(
        "UPDATE role_structure_actions SET last_refusal = ?, updated_at = ? "
        "WHERE id = ?",
        (refusal, datetime.now(timezone.utc).isoformat(), action_id),
    )


def implement(db, action_id: str, actor: str, *, worktree=None) -> dict:
    """Write the change. Recompute, emit, snapshot, write, then move.

    Only from ``approved``, which is the point: this is the fourth fence in
    front of the human gate, not a way around the other three. An agent that
    reaches this function on a row the owner has not approved gets a refusal
    naming the state the row is in.

    THE ORDER OF THE FIRST THREE STEPS IS ITSELF A FIX. The emitter can refuse
    — an unarmed guard, or generated SQL assigning a runtime-owned column —
    and it used to be asked AFTER the store had been snapshotted. A refusal
    then left a multi-gigabyte copy on disk for work that never happened and,
    while the refusal was still a ``SystemExit``, left it without answering
    the caller at all. So the SQL is produced in memory FIRST and the snapshot
    is taken only once there is something to write.

    The generated migration is left in the outbox for the orchestrator to
    review, commit and merge. It is never written into the running checkout —
    see :func:`_resolve_write_target`.
    """
    from okuro.roles.repair_plan import EmitRefused, emit_migration_sql
    from okuro.roles.write import update_role_fields

    row = _row(db, action_id)
    _require_state(row, "implement")
    stored = _stored_plan(row)
    roles = stored.get("roles") or {}
    changed = {rid: e for rid, e in roles.items() if e["changed"]}
    if not changed:
        raise UpdateRefused(
            "the stored plan changes nothing; there is nothing to implement"
        )

    # ── the split, recomputed HERE ───────────────────────────────────
    recomputed, disagreements = _recompute_split(changed)
    if disagreements:
        refusal = (
            "the carrier split computed on this host disagrees with the "
            "approved plan's, so implementing it would apply a split nobody "
            "reviewed. Re-plan here and have the new diff approved. "
            + "; ".join(disagreements)
        )
        _record_refusal(db, action_id, refusal)
        raise UpdateRefused(refusal)

    migration_half = {
        rid: e for rid, e in changed.items()
        if recomputed[rid] == CARRIER_MIGRATION}
    database_half = {
        rid: e for rid, e in changed.items()
        if recomputed[rid] == CARRIER_DATABASE}

    # THE GUARD MUST BE ABLE TO JUDGE ON THIS HOST, and that is a different
    # question from whether it found anything: a host with no token files
    # reads every body as clean. Asked only when something would enter a
    # migration — a database-only change needs no such judgement.
    if migration_half and not guard_is_armed():
        refusal = (
            "the CONTENT guard loaded no tokens on this host, so which bodies "
            "may enter the repo cannot be computed. Run implement on the "
            "machine whose ~/.okuro/guard token files define what is private, "
            "or the migration half would ship unchecked."
        )
        _record_refusal(db, action_id, refusal)
        raise UpdateRefused(refusal)

    # ── produce the SQL before anything is copied or written ─────────
    migration_name = None
    migration_sql = None
    target_dir = None
    target_kind = None
    if migration_half:
        target_dir, target_kind = _resolve_write_target(worktree)
        # NO NUMBER. Whoever reviews and commits the file numbers it —
        # numbering it here would mean guessing the next free number in a
        # checkout this process is deliberately not looking at. The STEM is
        # stable, and `verify` matches the migration ledger on it.
        migration_name = f"structure_action_{_short(action_id)}.sql"
        header = _GENERATED_HEADER.format(
            purpose=(
                f"Structural role update from action {_short(action_id)} — "
                f"{row['title']}"),
            action_id=action_id,
            decided_at=row.get("decided_at") or "—",
            decided_by=row.get("decided_by") or "—",
            title=row["title"],
            element=row.get("okuro_element") or "—",
            evidence=row.get("evidence_url") or "—",
            plan_artifact=row.get("plan_artifact_id") or "—",
            diff_artifact=row.get("diff_artifact_id") or "—",
        )
        try:
            migration_sql = emit_migration_sql(migration_half, header)
        except EmitRefused as exc:
            # A normal answer to a normal question. The row keeps its state,
            # the refusal is on it, and no snapshot was taken for work that
            # did not happen.
            _record_refusal(db, action_id, str(exc))
            raise UpdateRefused(str(exc)) from exc

    # ── the way back, before the first write ─────────────────────────
    snapshot = _snapshot(db, action_id)

    before_bodies = {
        rid: dict(_role_rows(db, [rid]).get(rid) or {}) for rid in changed}
    evidence_id = _write_artifact(
        kind="evidence",
        title=f"Structure action {_short(action_id)} — bodies before the write",
        summary=(
            f"The {len(changed)} affected role bodies as they were, plus the "
            f"snapshot path. This is the artifact the `implemented` state is "
            f"allowed to exist on."
        ),
        body=_render_pre_change(row, before_bodies, snapshot),
        created_by=actor,
    )

    # ── the migration half: a file to review, not a file to apply ────
    outbox_path = None
    if migration_sql is not None:
        written_file = target_dir / migration_name
        written_file.write_text(migration_sql, encoding="utf-8")
        outbox_path = str(written_file)
        # The diff goes NEXT TO IT. Whoever opens that directory is the person
        # deciding whether this SQL should be committed, and sending them to
        # an artifact id in another system to find out what it changes is how
        # a generated file gets committed unread.
        (target_dir / f"structure_action_{_short(action_id)}.diff.md").write_text(
            _render_diff(row, stored, samples=DIFF_SAMPLES), encoding="utf-8")
        logger.info("action %s left %s in the %s (%d role(s))",
                    action_id, written_file, target_kind, len(migration_half))

    # ── the database half ────────────────────────────────────────────
    written: list[str] = []
    written_columns: dict[str, list[str]] = {}
    for rid, entry in sorted(database_half.items()):
        fields = {
            "prompt": entry["_body"]["full"],
            "lean_prompt": entry["_body"]["lean"],
            "micro_prompt": entry["_body"]["micro"],
        }
        fields.update(entry["after"].get("fields") or {})
        if entry["after"].get("tier"):
            fields["tier"] = entry["after"]["tier"]
        update_role_fields(rid, fields, actor=actor)
        written.append(rid)
        # WHAT WAS WRITTEN, not what is usually written. `restore` reverted a
        # hard-coded three bodies and the tier, which was right until an
        # operation touched `model` or `maintenance_schedule` — and then wrong
        # silently, leaving the row half reversed.
        written_columns[rid] = sorted(fields)

    db.execute(
        "UPDATE role_structure_actions SET written_columns = ?, "
        "migration_outbox_path = ?, updated_at = ? WHERE id = ?",
        (json.dumps(written_columns), outbox_path,
         datetime.now(timezone.utc).isoformat(), action_id),
    )

    result = transition(
        db, action_id, "implemented", actor=actor,
        migration_id=migration_name,
        artifact_id=evidence_id,
        reason=(
            f"snapshot {snapshot.name}; "
            f"migration half {len(migration_half)} "
            f"({migration_name or 'none'}"
            + (f" in the {target_kind}" if migration_name else "")
            + f"); database half {len(written)}"
        ),
    )
    return {
        **result,
        "snapshot": str(snapshot),
        "migration": migration_name,
        "migration_outbox_path": outbox_path,
        "database_writes": written,
        "written_columns": written_columns,
        "evidence_artifact_id": evidence_id,
    }



def _render_pre_change(row: dict, bodies: dict, snapshot: Path) -> str:
    lines = [f"# Structure action {row['id']} — bodies before the write", "",
             f"**{row['title']}**", "",
             f"Store snapshot taken before the first write: `{snapshot}`",
             "",
             "`okuro roles restore --action " + row["id"] + "` copies these "
             "rows back from that file.", ""]
    for rid, b in sorted(bodies.items()):
        lines += [f"## {rid}", ""]
        for col, grade in (("prompt", "full"), ("lean_prompt", "lean"),
                           ("micro_prompt", "micro")):
            lines += [f"### {grade}", "", "```markdown",
                      (b.get(col) or "").rstrip(), "```", ""]
    return "\n".join(lines) + "\n"


# ── verify ───────────────────────────────────────────────────────────


def migration_applied(db, migration_id: str | None) -> str | None:
    """The ledger name of the applied migration carrying this action, or None.

    Matched on the STEM rather than on the whole filename, because the file
    this pipeline leaves in the outbox is unnumbered on purpose and whoever
    commits it gives it its number. ``structure_action_4f2a9c1b`` survives
    that; ``structure_action_4f2a9c1b.sql`` does not.
    """
    if not migration_id:
        return None
    stem = str(migration_id).rsplit("/", 1)[-1]
    if stem.endswith(".sql"):
        stem = stem[:-4]
    row = db.fetchone(
        "SELECT name FROM _migrations WHERE name LIKE ? "
        "ORDER BY applied_at DESC LIMIT 1",
        (f"%{stem}%",),
    )
    return (row or {}).get("name")


def verify(db, action_id: str, actor: str) -> dict:
    """Re-score the affected roles and compare against the STORED baseline.

    The caller supplies nothing. That is the whole change from what P4 could
    do: ``_check_verified`` still performs the comparison and still refuses
    across rubrics, but both readings now come from this module — the baseline
    from ``plan``, before any write, and the fresh one from the store as it is
    now.

    IT ALSO REFUSES WHILE THE MIGRATION HALF IS STILL A FILE. `implement`
    writes the database half into the store and leaves the migration half in
    the outbox for review, so the roles that half carries are UNCHANGED here
    until somebody commits, merges and applies it. Re-scoring them in that
    window compares a body against itself: "structure did not drop" is
    trivially true, and the verification would certify a change that has not
    happened anywhere. So the ledger is consulted first, and a pending
    migration is a refusal naming what is missing.

    A refusal leaves the row in ``implemented`` with the reason recorded,
    rather than raising into the caller's lap. An implemented change that does
    not verify is a real state and the row should say so; the write happened,
    and pretending the call never occurred would lose that.
    """
    row = _row(db, action_id)
    _require_state(row, "verify")
    role_ids = _affected(row)

    if not row.get("fit_baseline"):
        raise UpdateRefused(
            "this action has no stored fit baseline, so there is nothing to "
            "compare against. It was implemented outside this pipeline — the "
            "baseline is taken at plan time precisely so the party asking to "
            "be verified does not choose it."
        )

    # ── is there anything still waiting in the outbox ────────────────
    migration_id = row.get("migration_id")
    if migration_id:
        applied = migration_applied(db, migration_id)
        if not applied:
            refusal = (
                f"migration pending: {migration_id} has not been applied to "
                f"this store, so the roles it carries still hold their old "
                f"bodies. Verifying now would compare them against "
                f"themselves and pass on a change that has not happened. "
                f"Commit and merge the file"
                + (f" ({row['migration_outbox_path']})"
                   if row.get("migration_outbox_path") else "")
                + ", let the migration apply, then verify."
            )
            _record_refusal(db, action_id, refusal)
            logger.info("action %s not verified: %s", action_id, refusal)
            return {**_hydrate(_row(db, action_id)), "refused": refusal,
                    "verified_roles": [], "migration_applied": None}

    after = _fit_snapshot(db, role_ids)
    baseline = json.loads(row["fit_baseline"])

    try:
        result = transition(db, action_id, "verified", actor=actor,
                            fit_before=baseline, fit_after=after)
    except ActionRefused as exc:
        _record_refusal(db, action_id, str(exc))
        logger.warning("action %s did not verify: %s", action_id, exc)
        return {**_hydrate(_row(db, action_id)), "refused": str(exc),
                "verified_roles": []}

    # WHICH ROLES WERE ACTUALLY RE-SCORED, named rather than counted. A caller
    # reading "verified" over an action whose roles it cannot list is reading
    # the same unfalsifiable claim this workstream removed everywhere else.
    return {
        **result,
        "verified_roles": [f.get("role_id") for f in after],
        "migration_applied": migration_applied(db, migration_id),
    }


# ── restore ──────────────────────────────────────────────────────────


def _restore_columns(row: dict, role_ids: list[str]) -> dict[str, list[str]]:
    """Which columns to put back, per role.

    Read from ``written_columns``, which ``implement`` recorded. The fallback
    is the three bodies plus the tier, and it is a fallback for rows written
    before that column existed — NOT the rule. As the rule it was wrong the
    moment an operation touched ``model`` or ``maintenance_schedule``: those
    columns kept their new values while the bodies went back, leaving a row in
    neither state and nothing saying so.
    """
    try:
        recorded = json.loads(row.get("written_columns") or "{}")
    except ValueError:
        recorded = {}
    default = ["prompt", "lean_prompt", "micro_prompt", "tier"]
    return {rid: list(recorded.get(rid) or default) for rid in role_ids}


def restore(db, action_id: str, *, actor: str = "restore",
            snapshot: str | None = None) -> dict:
    """Put the affected roles back, from this action's own snapshot.

    ONLY the affected rows, and never the whole file. Restoring the whole
    store would undo everything else that happened since the write — other
    sessions' memories, todos, progress — to reverse a change to a dozen role
    bodies. The snapshot is a whole-store copy because that is the only
    consistent thing to take; what comes BACK out of it is scoped to the ids
    this action named and to the columns it actually wrote.

    Goes through ``update_role_fields`` rather than an UPDATE written here, so
    the restored rows get the same tag derivation, re-embedding and
    ``updated_at`` every other write path gives them. A row restored by raw
    SQL keeps a vector describing text it no longer holds.

    ONE EXCEPTION, AND IT IS DELIBERATE: the vocabulary gate. A snapshot can
    hold a tier outside ``CANONICAL_TIERS`` — that is exactly what migration
    156's tier normalisation was repairing — and the write funnel refuses one.
    Refusing here would mean a rollback that cannot roll back the rows it most
    needs to, and would abort mid-loop leaving some roles restored and some
    not. So the tier goes around the gate, alone, with a warning in the log:
    putting a row back as it was is not the same act as writing a new value,
    and the gate exists for the second.

    The row then moves to ``restored``, which is terminal. A reversal used to
    leave it reading ``implemented`` or ``verified``, so every later reader saw
    a change that was in force when it had been taken out.
    """
    from okuro.roles.write import RoleWriteRefused, update_role_fields

    row = _row(db, action_id)
    role_ids = _affected(row)
    wanted = _restore_columns(row, role_ids)

    if snapshot:
        path = Path(snapshot)
    else:
        found = snapshots_for(db, action_id)
        if not found:
            raise UpdateRefused(
                f"no pre-write snapshot for action {action_id}. Either it was "
                f"never implemented through this pipeline, or the snapshot was "
                f"pruned — there is nothing to restore FROM, and restoring "
                f"from a different action's snapshot would write bodies this "
                f"action never touched."
            )
        path = found[-1]
    if not path.exists():
        raise UpdateRefused(f"no snapshot file at {path}")

    columns = sorted({c for cols in wanted.values() for c in cols})
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        marks = ", ".join("?" for _ in role_ids)
        select = ", ".join(["role_id", *columns])
        old = {
            r["role_id"]: dict(r)
            for r in con.execute(
                f"SELECT {select} FROM roles WHERE role_id IN ({marks})",
                tuple(role_ids))
        }
    finally:
        con.close()

    missing = [r for r in role_ids if r not in old]
    restored: list[str] = []
    bypassed: list[str] = []
    for rid, before in sorted(old.items()):
        fields = {c: before[c] for c in wanted[rid] if c in before}
        if not fields:
            continue
        try:
            update_role_fields(rid, fields, actor=actor)
        except (RoleWriteRefused, ValueError) as exc:
            # The vocabulary gate, almost always on `tier`. Put back
            # everything the funnel accepts, then the refused column alone,
            # loudly. An abort here would leave the fleet half reversed.
            refused_cols = _gate_refused_columns(rid, fields, exc)
            keep = {k: v for k, v in fields.items() if k not in refused_cols}
            if keep:
                update_role_fields(rid, keep, actor=actor)
            for column in refused_cols:
                db.execute(
                    f"UPDATE roles SET {column} = ?, updated_at = ? "
                    f"WHERE role_id = ?",
                    (fields[column], datetime.now(timezone.utc).isoformat(), rid),
                )
            bypassed.append(f"{rid}:{','.join(sorted(refused_cols))}")
            logger.warning(
                "restore of %s wrote %s around the vocabulary gate — the "
                "snapshot holds a value the gate refuses (%s). A rollback "
                "restores what WAS, which is not the same act as writing a "
                "new value.", rid, sorted(refused_cols), exc)
        restored.append(rid)

    reason = (
        f"restored {len(restored)} role(s) from {path.name}"
        + (f"; {len(missing)} not present in the snapshot" if missing else "")
        + (f"; gate bypassed for {', '.join(bypassed)}" if bypassed else "")
    )
    result = transition(db, action_id, "restored", actor=actor, reason=reason)
    logger.info("action %s restored %d role(s) from %s",
                action_id, len(restored), path)
    return {
        **result,
        "action_id": action_id,
        "snapshot": str(path),
        "restored": restored,
        "restored_columns": wanted,
        "not_in_snapshot": missing,
        "gate_bypassed": bypassed,
    }


def _gate_refused_columns(role_id: str, fields: dict, exc: Exception) -> set[str]:
    """Which column the write funnel objected to.

    Read off the message rather than guessed, and falling back to the two the
    funnel validates. ``update_role_fields`` raises one exception for the
    whole payload, so a restore that dropped the entire payload on a tier
    complaint would leave the bodies unreversed — which is the failure the
    bypass exists to avoid, reintroduced one level up.
    """
    text = str(exc).lower()
    hit = {c for c in ("tier", "tools") if c in text and c in fields}
    return hit or ({"tier"} if "tier" in fields else set())
