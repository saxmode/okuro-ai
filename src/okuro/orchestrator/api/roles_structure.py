# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: The structure-research dispatch — poll the registry, then spawn the
#   researcher against what actually moved.
# index:
#   imports
#   router
#   def _source_rows
#   def structure_sources_endpoint
#   def _role_section
#   def _role_contract
#   def _registry_state
#   def _running_run
#   def _build_description
#   def structure_research_endpoint
#   class LiftRequest / DecisionRequest / TransitionRequest
#   def _authenticated_actor / _decider
#   def structure_actions_endpoint
#   def structure_actions_lift_endpoint
#   def structure_action_detail_endpoint
#   def structure_action_approve_endpoint
#   def structure_action_reject_endpoint
#   def structure_action_transition_endpoint
#   class KnowledgeFetchRequest
#   def knowledge_fetch_endpoint
#   class UpdatePlanRequest / UpdateActorRequest / def _pipeline_call
#   def structure_action_plan_endpoint / critique / dry_run / implement / verify
# AGENT_HEADER_END -->
"""``POST /api/roles/structure-research`` and ``GET /api/roles/structure-sources``.

**Its own module, registered ahead of the roles router, for the reason
``roles_fit.py`` already documents.** Starlette matches routes in registration
order and ``api/roles.py`` registers ``GET /{role_id}`` partway down its file.
A literal ``/structure-sources`` appended after that is unreachable — every
request lands on the detail route and comes back ``Role 'structure-sources' not
found``. Two literals have already been caught by this trap in that file
(``/maintenance``, ``/fit``); a third module is cheaper than a fourth debugging
session.

**Poll first, THEN spawn.** The endpoint does not hand the researcher a list of
sources to go and check. It polls them itself, records a run, and hands over
the run_id plus the ids that actually changed. Three things follow from that
ordering and none of them are available the other way round:

* the bodies are stored before the agent starts, so every quote it makes is
  checkable against something it did not fetch and cannot edit;
* the scope is measured rather than described, so "these two changed" is a
  falsifiable claim the agent can be judged against;
* a dead feed is visible as an alarm in the same breath, instead of arriving
  as the agent reporting "no changes found" for a source that 404s.

**The acceptance criteria are written into the task description, in full.** An
under-specified critic writes its own criteria, and the ones it invents are
reliably the ones the report it is reading happens to satisfy. That is the
recorded failure mode behind three role-refresh fabrications, so the ACs are
spelled out here rather than left to the role body — the body is the agent's
copy, this is the judge's.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("okuro.orchestrator.api.roles_structure")

router = APIRouter(prefix="/api/roles", tags=["roles"])

_SOURCE_COLS = (
    "id, name, url, vendor, kind, check_method, anchor_text, "
    "stale_after_months, last_status, last_hash, last_checked_at, enabled"
)


def _require_localhost(request: Request) -> None:
    """Same loopback guard the other role write endpoints use."""
    from okuro.orchestrator.api.main import _require_loopback

    _require_loopback(request)


# ── GET /api/roles/structure-sources ─────────────────────────────────


def _source_rows() -> list[dict]:
    """The registry plus each source's most recent observation.

    The last observation is a separate read rather than a column on the
    source, because ``role_structure_sources`` holds what is TRUE of the
    source and ``source_fetch_runs`` holds what happened on a RUN. Denormalising
    the alarms onto the source row would make "did it alarm" unanswerable for
    any run but the last one.
    """
    from okuro.db import get_db

    db = get_db()
    sources = [
        dict(r)
        for r in db.fetchall(
            f"SELECT {_SOURCE_COLS} FROM role_structure_sources ORDER BY id"
        )
    ]

    latest = {}
    for row in db.fetchall(
        "SELECT r.source_id, r.run_id, r.observed_at, r.http_status, "
        "       r.changed, r.alarms "
        "FROM source_fetch_runs r "
        "JOIN (SELECT source_id, MAX(observed_at) AS newest "
        "      FROM source_fetch_runs GROUP BY source_id) m "
        "  ON m.source_id = r.source_id AND m.newest = r.observed_at"
    ):
        latest[row["source_id"]] = dict(row)

    out = []
    for source in sources:
        seen = latest.get(source["id"])
        alarms: list[str] = []
        if seen:
            try:
                alarms = json.loads(seen.get("alarms") or "[]")
            except (TypeError, ValueError):
                alarms = []
        out.append(
            {
                **source,
                "enabled": bool(source.get("enabled")),
                "last_run_id": (seen or {}).get("run_id"),
                "last_observed_at": (seen or {}).get("observed_at"),
                "last_changed": bool((seen or {}).get("changed")),
                "alarms": alarms,
                # Never polled is NOT "healthy" and NOT "alarmed". A UI that
                # cannot tell those apart shows nine green rows for a registry
                # nothing has ever fetched.
                "never_polled": seen is None,
            }
        )
    return out


@router.get("/structure-sources")
def structure_sources_endpoint():
    """The registry with its last poll state. Read-only, so ungated — same
    class as ``GET /api/roles`` and ``GET /api/roles/fit``."""
    try:
        sources = _source_rows()
    except Exception as exc:  # noqa: BLE001
        logger.exception("structure-sources read failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "sources": sources,
        "count": len(sources),
        "enabled": sum(1 for s in sources if s["enabled"]),
        "alarmed": [s["id"] for s in sources if s["alarms"]],
        "never_polled": [s["id"] for s in sources if s["never_polled"]],
    }


# ── One run at a time, per registry state ────────────────────────────

#: Task ids for this dispatch. Named so the in-flight scan can find them
#: without reading every task in the store.
TASK_PREFIX = "rolestruct"

#: Written into the spawned task's directory so a later request can tell WHAT
#: a running task is polling, not merely that one is running.
RUN_MARKER = "structure-run.json"

#: The claim key. A SINGLETON — one structure-research run at a time, full
#: stop — and the fact that it is a constant rather than the registry hash is
#: the fix for the duplicate spawn.
#:
#: P3 keyed both guards on the registry state, reasoning that two dispatches
#: against the same state are duplicate work while a dispatch after a source
#: moved is a genuinely different run that must not wait. The first half is
#: true. The second half hands out duplicate researchers, because THE POLL IS
#: WHAT MOVES THE STATE: a run that found a change stamps the new hash, so the
#: second click computes a DIFFERENT key, matches no marker, conflicts with no
#: claim, and spawns a second agent onto overlapping runs. The guard was
#: weakest in exactly the case it existed for — the click that follows a poll
#: that found something.
#:
#: A run after the registry moved now waits. That is a deliberate change to
#: P3's rule: the cost is a few minutes for the second dispatch, and the thing
#: it buys is that two researchers never read the same store at once.
DISPATCH_CLAIM_KEY = "structure-research"

#: Task statuses that mean the run is over. Anything else — including a task
#: whose task.yaml does not exist yet because the engine is still booting —
#: counts as in flight. Treating "not written yet" as finished is how the
#: double-click guard would let the second click through.
_TERMINAL_STATUSES = frozenset(
    {"done", "complete", "completed", "failed", "error", "cancelled", "canceled"}
)

#: The dispatch-window guard is now a ROW, not a set.
#:
#: P3 shipped this as `_IN_FLIGHT: set[str]` and said so in its own report: a
#: Python set is per PROCESS, so two uvicorn workers receiving simultaneous
#: clicks both pass it and two agents get handed the same nine sources. The
#: replacement is `role_structure_dispatch_claims`, whose PRIMARY KEY makes the
#: claim atomic across every process on the host — see `roles.actions`.
#:
#: The task-directory scan below is unchanged and still necessary: the claim
#: covers the window between "request arrives" and "task spawned", which is the
#: whole poll; the scan covers a task that spawned minutes ago and is still
#: going, and it survives a restart of this process.


def _registry_state() -> str:
    """A hash of what the registry currently holds.

    **No longer a guard key — see DISPATCH_CLAIM_KEY.** It is recorded in the
    run marker, where it answers "what did this task actually go and poll",
    which is worth keeping and is a different question from "may this run
    start".
    """
    from okuro.db import get_db

    rows = get_db().fetchall(
        "SELECT id, last_hash, enabled FROM role_structure_sources ORDER BY id"
    )
    payload = "|".join(
        f"{r['id']}:{r['last_hash'] or ''}:{r['enabled']}" for r in rows
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _running_run() -> dict | None:
    """ANY structure-research run still going. One at a time.

    It used to take the registry state and ignore markers that did not carry
    it, which is the duplicate-spawn bug: the poll moves the state, so the
    click right after a productive run matched nothing here.

    Best-effort and deliberately so: it reads task directories, which survive a
    restart of this process, and it cannot see a run that is still inside its
    poll. The claim row covers that window; this covers the rest.
    """
    from okuro.orchestrator.api.main import TASKS_DIR  # type: ignore
    from okuro.orchestrator.api.roles import _read_task_status

    try:
        candidates = sorted(TASKS_DIR.glob(f"task-{TASK_PREFIX}-*"))
    except Exception:  # noqa: BLE001 — no task dir is not an error here
        return None

    for task_dir in candidates:
        marker = task_dir / RUN_MARKER
        if not marker.is_file():
            continue
        try:
            payload = json.loads(marker.read_text())
        except (OSError, ValueError):
            continue
        status = (_read_task_status(task_dir.name) or {}).get("status")
        if status is None or str(status).lower() not in _TERMINAL_STATUSES:
            return {
                "task_id": task_dir.name,
                "run_id": payload.get("run_id"),
                "status": status or "starting",
            }
    return None


def _write_run_marker(task_id: str, run_id: str, state: str) -> None:
    """Record what this task is polling, beside the task itself."""
    from okuro.orchestrator.api.main import TASKS_DIR  # type: ignore

    try:
        (TASKS_DIR / task_id / RUN_MARKER).write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "registry_state": state,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
    except OSError as exc:  # noqa: BLE001 — a missing marker costs a guard, not a run
        logger.warning("could not write the run marker for %s: %s", task_id, exc)


# ── POST /api/roles/structure-research ───────────────────────────────


#: The role whose body IS the contract. Not a default that a caller may
#: override: the ACs the judge reads and the tool grant that enforces AC5 are
#: two halves of the same row, and pointing this at another role would hand
#: the criteria to an agent that can call ``roles_learn``.
RESEARCHER_ROLE_ID = "role-architecture-researcher"

#: The two H2 sections lifted out of that body at dispatch time. They are
#: load-bearing structure in the role text, not formatting — renaming either
#: heading breaks this read, which is why a test asserts they are there.
AC_HEADING = "ACCEPTANCE CRITERIA"
SCHEMA_HEADING = "FINDING SCHEMA"


class ContractMissing(RuntimeError):
    """The role body did not yield the criteria the brief has to carry."""


def _role_section(body: str, heading: str) -> str | None:
    """Return the ``## <heading>`` section of a role body, fence-aware.

    Fence-aware on purpose. ``designer.validate_role_structure`` scans raw
    text and counts a ``## `` line inside a fenced block as a section — ten
    live role bodies carry an output template that trips it. This reader must
    not inherit that bug: the FINDING SCHEMA section is itself a fenced JSON
    block, so a scanner that cannot see fences is one edit away from cutting
    a section in half.
    """
    lines = (body or "").splitlines()
    out: list[str] = []
    in_fence = False
    capturing = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            if capturing:
                out.append(line)
            continue
        if not in_fence and stripped.startswith("## "):
            if stripped[3:].strip().upper() == heading.upper():
                capturing = True
                continue
            if capturing:
                break
        if capturing:
            out.append(line)
    text = "\n".join(out).strip()
    return text or None


def _role_contract() -> dict:
    """The acceptance criteria and finding schema, read from the role row.

    **One home, not four.** These were hand-maintained here and in all three
    grades of the role body — a set that agrees on the day it is written and
    drifts on the first edit to any one of them. The role body is the home;
    this reads it. Editing the role is now what changes the brief the judging
    agent is handed.

    FULL first, LEAN as the fallback. FULL is the on-demand references tier
    and carries the fullest wording; LEAN is what a dispatched subagent
    actually receives, so it carries the same sections and is a truthful
    second choice.

    Raises rather than degrading. A brief that goes out without criteria is
    the exact failure this whole phase exists to remove: an under-specified
    critic writes its own, and the ones it invents are reliably the ones the
    report it is reading happens to satisfy.
    """
    from okuro.db import get_db

    row = get_db().fetchone(
        "SELECT prompt, lean_prompt FROM roles WHERE role_id = ?",
        (RESEARCHER_ROLE_ID,),
    )
    if not row:
        raise ContractMissing(
            f"role '{RESEARCHER_ROLE_ID}' is not in the roles table — "
            f"migration 157 has not been applied to this store"
        )

    for grade in ("prompt", "lean_prompt"):
        body = row[grade] or ""
        criteria = _role_section(body, AC_HEADING)
        schema = _role_section(body, SCHEMA_HEADING)
        if criteria and schema:
            return {"criteria": criteria, "schema": schema, "grade": grade}

    raise ContractMissing(
        f"neither grade of '{RESEARCHER_ROLE_ID}' carries both a "
        f"'## {AC_HEADING}' and a '## {SCHEMA_HEADING}' section — the brief "
        f"would go out with no criteria for the critic to judge against"
    )


def _build_description(summary: dict, contract: dict) -> str:
    """The task description: what moved, what alarmed, and how it is judged.

    ``contract`` comes from the role body via :func:`_role_contract`, so the
    criteria in this brief and the criteria the agent adopts are the same
    text, read from the same row, in the same minute.
    """
    changed = summary.get("changed") or []
    per_source = []
    for row in summary.get("sources", []):
        bits = [f"status={row.get('status')}"]
        bits.append("CHANGED" if row.get("changed") else "unchanged")
        if row.get("anchor_found") is False:
            bits.append("anchor MISSING")
        if row.get("alarms"):
            bits.append("alarms=" + ",".join(row["alarms"]))
        per_source.append(f"  - {row.get('source_id')}: {'; '.join(bits)}")

    alarmed = summary.get("alarmed") or []

    return (
        "Structural role-architecture research run.\n\n"
        "The sources have ALREADY been polled and their bodies stored. Do not "
        "re-poll them and do not widen the scope: a source outside this run "
        "has no stored body, so nothing quoted from it can be verified.\n\n"
        f"run_id: {summary.get('run_id')}\n"
        f"sources polled: {summary.get('polled')}\n"
        f"CHANGED (read these): {', '.join(changed) if changed else 'none'}\n"
        f"ALARMED (report these, claim nothing about them): "
        f"{', '.join(alarmed) if alarmed else 'none'}\n\n"
        "Per-source outcome:\n" + "\n".join(per_source) + "\n\n"
        "Read the STORED body for each changed source rather than re-fetching "
        "it — the stored body is what this run saw. For each material change, "
        "name the ONE okuro element it bears on, quote the sentence that "
        "carries it, and verify that quote before writing it down.\n\n"
        "An alarmed source did NOT report 'no change'. It reported nothing, "
        "and the report must say so in those terms rather than counting it as "
        "quiet.\n\n"
        "A run with zero findings is a COMPLETE run and the correct output "
        "when nothing material moved. Do not manufacture coverage.\n\n"
        "DELIVERABLE: exactly one artifact_write(kind='report', "
        "project='okuro') carrying (a) one outcome line per source in this "
        "run including the ones with nothing to say, (b) the alarm lines, and "
        "(c) the finding rows, each row shaped as the FINDING SCHEMA below.\n\n"
        f"FINDING SCHEMA (read from the {RESEARCHER_ROLE_ID} role body, "
        f"{contract['grade']} grade — it is the same text the role adopts):\n"
        f"{contract['schema']}\n\n"
        "ACCEPTANCE CRITERIA — judge against exactly these, and do not "
        f"substitute your own. Read from the same role body:\n"
        f"{contract['criteria']}\n"
    )


def run_structure_research(gate=None, *, http=None) -> dict:
    """Poll the registry, record the run, then spawn the researcher on it.

    **The one spawn path.** It is a module function rather than only an
    endpoint body because there are now two callers — the owner pressing the
    button, and the weekly ``structure-watch`` clock — and they differ in
    exactly one thing: whether the spawn is conditional. Everything else, the
    cross-process claim, the in-flight scan, the poll, the contract read, the
    description and the run marker, is the same work. Copying it for the
    scheduler would have produced two dispatchers that agree today and drift on
    the first edit to either, which is the class DP10/DP11 names — and the
    duplicate-spawn bug this module already carries a comment about was born
    the last time two guards disagreed about one run.

    ``gate`` is an optional ``(summary) -> (bool, str)`` predicate applied AFTER
    the poll and BEFORE the spawn. The poll still happens: its whole job is to
    store bodies and file ``source_health`` actions, and those are worth doing
    on a week when nothing changed. Returning False skips only the agent.

    The poll runs INLINE rather than in a background task. The task
    description cannot be written until it finishes — deferring it would mean
    spawning an agent with an empty scope and hoping the poll caught up, a
    race whose losing side looks exactly like "there were no changes". The
    poll itself is parallel under a whole-run deadline, so inline costs
    seconds rather than the six minutes nine sequential socket timeouts could.

    Raises ``HTTPException`` with 409 while another run is still going, and
    with 500 when the role body carries no acceptance criteria. A non-HTTP
    caller catches it and reads ``.status_code`` / ``.detail``; refusing to
    spawn a critic that would invent its own criteria matters more than the
    exception type being pretty at one of the two call sites.
    """
    from okuro.db import get_db
    from okuro.orchestrator.api.roles import spawn_role_task
    from okuro.roles.actions import (
        claim_dispatch,
        note_dispatch_task,
        open_dispatch_claim,
        release_dispatch,
    )
    from okuro.roles.source_poll import new_run_id, poll_all

    db = get_db()

    try:
        contract = _role_contract()
    except ContractMissing as exc:
        # Refuse rather than spawn a critic that will invent its own criteria.
        logger.error("structure-research contract unavailable: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    state = _registry_state()

    # CLAIM FIRST, then look for a running task. The other order leaves a
    # window: two workers both see nothing running, both proceed, and only
    # then does either take a claim. The claim is atomic across processes, so
    # taking it first makes the check that follows race-free.
    run_id = new_run_id()
    if not claim_dispatch(db, DISPATCH_CLAIM_KEY, run_id=run_id):
        held = open_dispatch_claim(db, DISPATCH_CLAIM_KEY) or {}
        raise HTTPException(
            status_code=409,
            detail=(
                f"a structure-research run is already being dispatched "
                f"(run {held.get('run_id')}, claimed {held.get('claimed_at')})"
            ),
        )

    try:
        running = _running_run()
        if running:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"a structure-research run is already going: task "
                    f"{running['task_id']} (run {running['run_id']}, status "
                    f"{running['status']}). One at a time — two researchers "
                    f"reading the same store at once is what this prevents."
                ),
            )

        try:
            summary = poll_all(db, run_id=run_id, http=http)
        except Exception as exc:  # noqa: BLE001
            logger.exception("structure poll failed for run %s", run_id)
            raise HTTPException(
                status_code=500, detail=f"source poll failed: {exc}"
            ) from exc

        should_spawn, gate_reason = (True, "unconditional")
        if gate is not None:
            should_spawn, gate_reason = gate(summary)

        task_id = None
        if should_spawn:
            task_id = spawn_role_task(
                _build_description(summary, contract),
                role_scope_label=run_id,
                required_role=RESEARCHER_ROLE_ID,
                task_prefix=TASK_PREFIX,
            )
            if task_id:
                # The marker is written BEFORE the claim is released, so there
                # is no instant where neither guard can see this run.
                _write_run_marker(task_id, run_id, state)
                note_dispatch_task(db, DISPATCH_CLAIM_KEY, task_id)
    finally:
        release_dispatch(db, DISPATCH_CLAIM_KEY)

    if not should_spawn:
        status = "skipped"
    elif task_id:
        status = "running"
    else:
        status = "failed"

    return {
        "run_id": run_id,
        "task_id": task_id,
        "status": status,
        "spawned": bool(task_id),
        "gate_reason": gate_reason,
        "error": None if (task_id or not should_spawn) else "Engine spawn failed",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "contract_grade": contract["grade"],
        "polled": summary.get("polled", 0),
        "changed": summary.get("changed", []),
        "alarmed": summary.get("alarmed", []),
        "health_actions": summary.get("health_actions", []),
        "sources": summary.get("sources", []),
    }


@router.post("/structure-research")
def structure_research_endpoint(request: Request):
    """The button. Localhost-only — it spawns a process and writes to the store.

    Unconditional by design: a human pressing this has already decided the run
    is worth making. The weekly clock is the caller that passes a gate.
    """
    _require_localhost(request)
    return run_structure_research()


# ── /api/roles/structure-actions ─────────────────────────────────────
#
# THE APPROVE ROUTE IS THE OWNER'S SURFACE AND NOTHING ELSE REACHES IT.
#
# `→ approved` is the one transition an agent cannot make, and it is fenced
# three times rather than once, because one fence is one bug away from none:
#
#   1. `roles_actions_transition` does not accept the state (mcp_tools);
#   2. this route is loopback-only behind the API bearer, which a dispatched
#      subagent has neither of;
#   3. `roles.actions.transition` refuses an actor that is not the profile's
#      own human, even when called in-process.
#
# An agent that defeats any one of those has still not defeated the other two.
#
# Route ORDER inside this router matters for the same reason it mattered for
# `/structure-sources`: `/structure-actions/lift` is a literal and
# `/structure-actions/{action_id}` is a pattern that would swallow it. The
# literals are registered first.


class LiftRequest(BaseModel):
    run_id: str = Field(..., description="The poll run the report is about")
    artifact_id: str = Field(..., description="The researcher's report artifact")


class DecisionRequest(BaseModel):
    actor: str | None = Field(
        None,
        description=(
            "OPTIONAL, and it is not how the approver is decided. The actor is "
            "derived from the authenticated identity; supplying one that "
            "disagrees is refused rather than ignored, so a caller asserting an "
            "identity it does not have gets told, not silently corrected."
        ),
    )
    reason: str | None = None
    affected_role_ids: list[str] | None = Field(
        None,
        description="Explicit role ids. Never a count — the write path refuses one.",
    )


class TransitionRequest(BaseModel):
    to_state: str
    actor: str
    reason: str | None = None
    artifact_id: str | None = None
    migration_id: str | None = None
    affected_role_ids: list[str] | None = None
    fit_before: list[dict] | None = None
    fit_after: list[dict] | None = None


def _authenticated_actor() -> str:
    """Who this request is, derived from the identity the API layer knows.

    **The approver is never read from the payload, and that is the fix for the
    hole this route used to have.** `DecisionRequest.actor` defaulted to
    "user", and "user" was unconditionally on the human allowlist, so the whole
    human gate was "did you reach this route" — and reaching it needs loopback
    plus a bearer that is one shared token sitting in the keyring. Any local
    process that could read the keyring could approve a change to okuro's canon
    and have it recorded as a person's decision.

    There is exactly one identity behind that bearer: `_load_api_token` scopes
    it to the local user, so the authenticated principal IS the profile, and
    the profile's handle is the name okuro has for them.

    Raises 403 when the profile names no handle. That is the gate failing
    CLOSED: with no handle there is nobody this request could be, and the
    honest answer is refusal rather than a generic stand-in — a generic
    stand-in is what was there before.
    """
    from okuro.roles.actions import human_actors

    allowed = human_actors()
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail=(
                "this okuro profile names no handle, so no approver can be "
                "derived from the authenticated identity. Approval is the one "
                "human decision gate in this workflow and it fails closed."
            ),
        )
    return sorted(allowed)[0]


def _decider(payload: "DecisionRequest | None") -> str:
    """The actor for a decision route: derived, and refusing a contradiction."""
    actor = _authenticated_actor()
    claimed = (payload.actor or "").strip() if payload else ""
    if claimed and claimed.lower() != actor.lower():
        raise HTTPException(
            status_code=403,
            detail=(
                f"this request cannot decide as {claimed!r}. The approver is "
                f"derived from the authenticated identity, not from the "
                f"payload — omit `actor`, or send {actor!r}."
            ),
        )
    return actor


@router.get("/structure-actions")
def structure_actions_endpoint(state: str | None = None, kind: str | None = None):
    """The action container, newest first. Read-only, so ungated.

    Returns the rows PLUS two per-state counts, because the panel groups by
    state and a grouping computed from a truncated list is wrong exactly when
    there is too much to show.

    **Two counts, both labelled, because one unlabelled count was wrong.** This
    returned a single `by_state` computed over the WHOLE table while the rows
    beside it honoured `state` and `kind` — a header that contradicts the list
    under it, and the reader believes the header. `by_state_filtered` matches
    the rows; `by_state_total` is the registry-wide picture, which is the
    genuinely useful second number when a filter is on.
    """
    from okuro.db import get_db
    from okuro.roles.actions import STATES, ActionRefused, count_actions, list_actions

    db = get_db()
    try:
        rows = list_actions(db, state=state, kind=kind)
        filtered = count_actions(db, state=state, kind=kind)
        total = count_actions(db)
    except ActionRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("structure-actions read failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "actions": rows,
        "count": len(rows),
        "by_state_filtered": filtered,
        "by_state_total": total,
        "filters": {"state": state, "kind": kind},
        "states": list(STATES),
    }


@router.post("/structure-actions/lift")
def structure_actions_lift_endpoint(request: Request, payload: LiftRequest):
    """Read a researcher's report and open one action per finding.

    **This is an ENDPOINT because the orchestrator offers no post-task hook.**
    `engine._finalize_task` drains futures, adopts a declared project path and
    runs the verify gate; there is no registry a module can attach to, and
    inventing one for this single caller would be a change to the engine that
    nothing else asked for. So the lift is explicit: the operator (or a later
    recurring task) names the run and the report.

    The agent's own `quote_verified` is ignored — every quote is re-checked
    against the stored body. Refusals come back in full rather than being
    counted, because "4 findings, 1 action created" is the interesting result
    and a summary that hides it reads like a quiet run.
    """
    _require_localhost(request)

    from okuro.db import get_db
    from okuro.roles.actions import ActionRefused, lift_findings_from_report

    try:
        return lift_findings_from_report(
            get_db(), payload.run_id, payload.artifact_id
        )
    except ActionRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("lifting findings from %s failed", payload.artifact_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/structure-actions/{action_id}")
def structure_action_detail_endpoint(action_id: str):
    """One action with its full event log. Read-only, so ungated."""
    from okuro.db import get_db
    from okuro.roles.actions import get_action

    row = get_action(get_db(), action_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"no action {action_id}")
    return row


@router.post("/structure-actions/{action_id}/approve")
def structure_action_approve_endpoint(
    action_id: str, request: Request, payload: DecisionRequest | None = None
):
    """The human gate. Loopback + bearer, and it creates the todo.

    A 409 rather than a 500 when the state machine refuses: the caller asked
    for something the row's current state does not allow, which is a conflict
    with the row, not a server fault. The detail carries the refusal verbatim
    so the panel can say what happened rather than "request failed".

    **A hash-drift refusal is a 409 too, and it used to be a 200.** That
    refusal does not raise — it MOVES the row back to `researched` and returns
    it — so the route answered 200 with a row that had not been approved, and
    the panel, which reads a 2xx as success, showed the approval going through.
    The one thing a decision surface must never do is report a decision that
    did not happen.
    """
    _require_localhost(request)

    from okuro.db import get_db
    from okuro.roles.actions import ActionRefused, transition

    payload = payload or DecisionRequest()
    actor = _decider(payload)
    try:
        result = transition(
            get_db(),
            action_id,
            "approved",
            actor=actor,
            reason=payload.reason,
            affected_role_ids=payload.affected_role_ids,
        )
    except ActionRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if result.get("state") != "approved":
        raise HTTPException(
            status_code=409,
            detail=(
                result.get("last_refusal")
                or f"the approval did not take; the row is {result.get('state')!r}"
            ),
        )
    return result


@router.post("/structure-actions/{action_id}/reject")
def structure_action_reject_endpoint(
    action_id: str, request: Request, payload: DecisionRequest
):
    """The other half of the human gate. A reason is mandatory.

    The actor is derived here too. Rejection is a decision that stamps
    `decided_by`, and an attribution any local process could choose is not an
    attribution.
    """
    _require_localhost(request)

    from okuro.db import get_db
    from okuro.roles.actions import ActionRefused, transition

    actor = _decider(payload)
    try:
        return transition(
            get_db(),
            action_id,
            "rejected",
            actor=actor,
            reason=payload.reason,
        )
    except ActionRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/structure-actions/{action_id}/transition")
def structure_action_transition_endpoint(
    action_id: str, request: Request, payload: TransitionRequest
):
    """Every move an agent may make. `approved` is refused HERE as well.

    The MCP verb already cannot name it, but this route exists so an operator
    or a script can drive the chain, and a route that accepts any state would
    be the second fence quietly removed.
    """
    _require_localhost(request)

    from okuro.db import get_db
    from okuro.roles.actions import (
        AGENT_REACHABLE_STATES,
        ActionRefused,
        transition,
    )

    if payload.to_state not in AGENT_REACHABLE_STATES:
        raise HTTPException(
            status_code=403,
            detail=(
                f"{payload.to_state!r} is not reachable through this route. "
                f"Approval is the single human decision gate and has its own "
                f"endpoint; the reachable states are "
                f"{', '.join(sorted(AGENT_REACHABLE_STATES))}."
            ),
        )
    try:
        return transition(
            get_db(),
            action_id,
            payload.to_state,
            actor=payload.actor,
            reason=payload.reason,
            artifact_id=payload.artifact_id,
            migration_id=payload.migration_id,
            affected_role_ids=payload.affected_role_ids,
            fit_before=payload.fit_before,
            fit_after=payload.fit_after,
        )
    except ActionRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ── POST /api/roles/knowledge-fetch ──────────────────────────────────
#
# THE DOOR THE DAILY SWEEP NEEDS, AND WHY IT DOES NOT TAKE A BODY.
#
# Amendment A2 says `fetch_verified` may be set only by code that stored the
# fetched body. The structure researcher never writes knowledge at all (AC5 —
# its tool grant excludes `roles_learn`), so without this route the gate would
# sit on an empty road: the one agent that DOES write knowledge, the daily
# role-refresh sweep, had no way to put a body in the store and would score
# `fetch_verified = 0` forever no matter how honestly it worked.
#
# The obvious shape — let the agent POST the body it read — rebuilds the exact
# defect this phase exists to remove. An agent that supplies both the body and
# the quote has certified itself, and `verify_quote` becomes a check that a
# string the agent wrote appears in another string the agent wrote. So this
# route takes a URL and NOTHING ELSE of substance: the server does the HTTP and
# what lands in the store is what the wire returned. The agent is a caller
# here, not a source.
#
# THAT DESIGN BUYS HONESTY AND SELLS AN SSRF PRIMITIVE, so it is paid for.
# The outbound address is now chosen by an agent whose job is reading untrusted
# web pages, and a prompt injected into one of them reaches a fetcher on the
# loopback interface of the machine that owns the keyring, this API and the
# LAN. Validating the scheme — all the first version did — stops none of it:
# `http://127.0.0.1:13333/api/...`, `http://192.168.1.1/` and a public URL that
# 302s to either are all well-formed http. So the fetch goes through
# `roles/safe_fetch.py`, NOT the poller's transport: the host is resolved before
# anything connects, every address it resolves to must be globally routable,
# auto-follow is off and each redirect hop is validated again, ports are an
# allowlist unless the host is a registered source, and the body and the clock
# are both capped.


class KnowledgeFetchRequest(BaseModel):
    url: str = Field(..., description="The page to fetch and store")
    run_id: str = Field(
        ...,
        description=(
            "The maintenance run this fetch belongs to. Cite the SAME id on "
            "the roles_learn call or the entry will not earn fetch_verified."
        ),
    )
    name: str | None = Field(
        None, description="Optional human label for the auto-registered source"
    )


@router.post("/knowledge-fetch")
def knowledge_fetch_endpoint(payload: KnowledgeFetchRequest, request: Request):
    """Fetch a page server-side and store its body against ``run_id``.

    Localhost-only: it makes an outbound request and writes to the store.

    The stored body is what makes a later ``roles_learn`` entry verifiable —
    both through ``fetch_verified`` on the row and through ``verify_quote``,
    which is a substring assert against this exact text.
    """
    _require_localhost(request)

    from okuro.db import get_db
    from okuro.roles.evidence import record_knowledge_fetch
    from okuro.roles.safe_fetch import UnsafeURL, safe_fetch

    db = get_db()
    url = (payload.url or "").strip()

    # The URL came from an agent whose job is reading untrusted web pages, so
    # it is fetched through safe_fetch, never the poller's transport: the host
    # is resolved before anything connects, every address it resolves to has to
    # be globally routable, and each redirect hop is validated again. See
    # roles/safe_fetch.py for what "inward" covers and what it does not.
    try:
        response = safe_fetch(url, db=db)
    except UnsafeURL as exc:
        logger.warning("knowledge-fetch REFUSED %s: %s", url, exc.detail)
        raise HTTPException(
            status_code=400, detail=f"refused ({exc.reason}): {exc.detail}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — a dead URL is a 502, not a 500
        logger.warning("knowledge-fetch of %s failed: %s", url, exc)
        raise HTTPException(
            status_code=502, detail=f"fetch failed: {exc}"
        ) from exc

    if response.status != 200 or not response.body:
        raise HTTPException(
            status_code=502,
            detail=(
                f"fetch returned HTTP {response.status} with "
                f"{len(response.body or '')} bytes — there is nothing to verify "
                f"against, so nothing is stored"
            ),
        )

    stored = record_knowledge_fetch(
        db,
        url=url,
        run_id=payload.run_id,
        body=response.body,
        http_status=response.status,
        name=payload.name,
    )
    return {**stored, "final_url": response.final_url or url}


# ── the update pipeline, behind the same gates ───────────────────────
#
# Five routes mirroring the five MCP verbs, and they sit behind the SAME
# loopback + bearer gate as everything else in this section. `implement` and
# `verify` are the two the panel calls, because they are the two the owner
# presses a button for — and `implement` refuses on any state but `approved`
# inside the pipeline, so the button being visible is not what makes it legal.
#
# Route order still matters: these are literals under `/structure-actions/`
# with a `{action_id}` pattern already registered above, so each one is spelled
# out with its own suffix rather than collected behind a wildcard.


class UpdatePlanRequest(BaseModel):
    actor: str | None = Field(
        None,
        description=(
            "Who is planning. Unlike the decision routes this is NOT derived: "
            "planning is not a decision, and an agent driving the chain has a "
            "name worth recording."
        ),
    )
    instructions: dict | None = Field(
        None,
        description=(
            "{'operations': [...]} in repair_plan's vocabulary. Omit to "
            "re-plan with the operations already stored on the row."
        ),
    )


class UpdateActorRequest(BaseModel):
    actor: str | None = None
    worktree: str | None = Field(
        None,
        description=(
            "Where implement() writes the generated migration. Defaults to "
            "the tree this okuro runs from."
        ),
    )


def _pipeline_call(fn, *args, **kwargs):
    """One refusal shape for all five, matching the routes above.

    409 for a gate, because the caller asked for something the row's state
    does not allow — a conflict with the row, not a server fault. The refusal
    text goes through verbatim: it names which gate said no, and that sentence
    is the most useful thing the panel can show.
    """
    from okuro.roles.actions import ActionRefused
    from okuro.roles.repair_plan import EmitRefused

    try:
        return fn(*args, **kwargs)
    except (ActionRefused, EmitRefused) as exc:
        # EmitRefused is listed even though `implement` already wraps it: the
        # emitter is shared code and a future caller that does not wrap it
        # would otherwise turn a stated refusal into a 500.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("structure update step failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/structure-actions/{action_id}/plan")
def structure_action_plan_endpoint(
    action_id: str, request: Request, payload: UpdatePlanRequest | None = None
):
    """Compute the change and take the baseline. Writes no role."""
    _require_localhost(request)

    from okuro.db import get_db
    from okuro.roles import structural_update as pipeline

    payload = payload or UpdatePlanRequest()
    return _pipeline_call(
        pipeline.plan, get_db(), action_id, payload.actor or "api",
        instructions=payload.instructions,
    )


@router.post("/structure-actions/{action_id}/critique")
def structure_action_critique_endpoint(
    action_id: str, request: Request, payload: UpdateActorRequest | None = None
):
    """Run the uninformed-critic pass over the stored plan."""
    _require_localhost(request)

    from okuro.db import get_db
    from okuro.roles import structural_update as pipeline

    payload = payload or UpdateActorRequest()
    return _pipeline_call(
        pipeline.critique, get_db(), action_id, payload.actor or "api")


@router.post("/structure-actions/{action_id}/dry-run")
def structure_action_dry_run_endpoint(action_id: str, request: Request):
    """The diff. Changes no state and writes no role.

    A POST rather than a GET because it creates an artifact and links it on
    the row — the diff is a document with an id, so it can be the thing
    approved rather than a view that is different each time it is opened.
    """
    _require_localhost(request)

    from okuro.db import get_db
    from okuro.roles import structural_update as pipeline

    return _pipeline_call(pipeline.dry_run, get_db(), action_id)


@router.post("/structure-actions/{action_id}/implement")
def structure_action_implement_endpoint(
    action_id: str, request: Request, payload: UpdateActorRequest | None = None
):
    """Apply an APPROVED plan. Refuses from every other state.

    The actor is DERIVED here, like the decision routes and unlike the other
    pipeline steps. Implementing is not the decision, but it is the act the
    decision authorised, and `decided_by` on the row would otherwise be the
    only name in the trail — the event log should say who carried it out too,
    and a name any local process could type is not a name.
    """
    _require_localhost(request)

    from okuro.db import get_db
    from okuro.roles import structural_update as pipeline

    payload = payload or UpdateActorRequest()
    actor = _authenticated_actor()
    claimed = (payload.actor or "").strip()
    if claimed and claimed.lower() != actor.lower():
        raise HTTPException(
            status_code=403,
            detail=(
                f"this request cannot implement as {claimed!r}; the actor is "
                f"derived from the authenticated identity"
            ),
        )
    return _pipeline_call(
        pipeline.implement, get_db(), action_id, actor,
        worktree=payload.worktree,
    )


@router.post("/structure-actions/{action_id}/verify")
def structure_action_verify_endpoint(
    action_id: str, request: Request, payload: UpdateActorRequest | None = None
):
    """Re-score against the stored baseline. The caller supplies no numbers.

    A refusal to verify comes back 200 with the row and a `refused` field, not
    as an error: the write HAPPENED, the row is legitimately `implemented`,
    and the measurement said the structure did not improve. That is a result,
    and turning it into a 4xx would make the panel render it as a failed
    request rather than as the finding it is.
    """
    _require_localhost(request)

    from okuro.db import get_db
    from okuro.roles import structural_update as pipeline

    payload = payload or UpdateActorRequest()
    return _pipeline_call(
        pipeline.verify, get_db(), action_id, payload.actor or "api")
