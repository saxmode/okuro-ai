# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Append-only typed event log per task (M2). Cross-subtask
#   decision log the compressor reads to produce decision-trace context
#   for the next serial step. Orthogonal to role_handovers (Stream A) —
#   that stays as per-subtask handover; this is per-decision.
# index: imports | Body models | TaskEvent | append_event | list_events |
#   active_decisions | _ConflictError | conflict_detect | _jsonl_mirror |
#   AGENT_HEADER_END -->
"""Append-only typed event log per task (M2).

Storage:
    sqlite ``task_events`` table (migration 045) — authoritative.
    disk mirror at ``~/.okuro/orchestrator/tasks/{task_id}/events.jsonl``
    — newline-delimited JSON, one event per line, ordered by seq.

Append semantics:
    Each append assigns a per-task monotonic ``seq`` (max(seq)+1 under
    the write lock). The JSONL mirror is appended in the same write
    transaction. A failed conflict-detect aborts before either is
    touched.

Conflict-detect-at-append:
    A new ``decision`` event whose body claims a topic already locked by
    an active ADR (task.adrs from M1) OR by a prior unsuperseded
    ``decision`` event for the same task is REJECTED unless the new
    event is itself a ``supersedes`` (event_type='supersedes') referencing
    the prior id. The error message tells the producer how to recover.

Compressor wiring:
    The compressor agent reads ``list_events(task_id)`` + ``task.adrs``,
    emits a 2-5K-token decision-trace, persists it as an artifact, and
    appends a ``compression`` event linking to the artifact. Dispatcher
    fetches the latest ``compression`` for the next dep-rendering and
    injects its body below the M1 "Locked Decisions" block.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Body models — one per event_type
# ---------------------------------------------------------------------------

EventType = Literal[
    "decision", "supersedes", "contract",
    "gap", "open_question", "compression",
    "verdict",  # M3 reviewer-pipeline output — see migration 046
    "role_slice",  # M4 spawn-time projection telemetry
    "role_body_fetched",  # M4 lazy-load telemetry (roles_get from subagent)
    "bootstrap_sizes",  # M5 per-spawn bootstrap section accounting
    "spawn_usage",  # M5+ per-spawn token + cache accounting
    "convergence_telemetry",  # PR 4 — session-loop retry round telemetry
]


class DecisionBody(BaseModel):
    topic: str = Field(min_length=1, max_length=120)
    choice: str = Field(min_length=1, max_length=400)
    rationale: str = Field(min_length=1, max_length=800)
    alternatives: list[str] = Field(default_factory=list, max_length=8)


class SupersedesBody(BaseModel):
    target_event_id: str = Field(min_length=8)
    reason: str = Field(min_length=1, max_length=400)
    new_choice: str = Field(default="", max_length=400)


class ContractBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["api", "schema", "type", "envelope", "event", "config"]
    schema_body: str = Field(min_length=1, max_length=4000)
    notes: str = Field(default="", max_length=400)


class GapBody(BaseModel):
    summary: str = Field(min_length=1, max_length=400)
    affects: list[str] = Field(default_factory=list, max_length=10)


class OpenQuestionBody(BaseModel):
    question: str = Field(min_length=1, max_length=400)
    blocking: bool = False


class CompressionBody(BaseModel):
    artifact_id: str = Field(min_length=8)
    covers_seq_from: int = Field(ge=1)
    covers_seq_to: int = Field(ge=1)
    summary: str = Field(min_length=1, max_length=800)


class VerdictBody(BaseModel):
    """M3 reviewer-pipeline output — one verdict per phase per review pass.

    Emitted by ``okuro.orchestrator.reviewer.pipeline.run_review`` after
    both the Critic and Scorer stages run (or short-circuits with FAIL
    when the deterministic stage finds a load-bearing failure). Append-only
    like every other event_type — a re-review of the same phase appends a
    new event, leaving prior verdicts in place for audit.
    """
    phase_id: int = Field(ge=0)
    # CAP / NEEDS_USER are terminal escalation verdicts the session loop emits
    # (see ConvergenceBody below, which already accepts them). Without them
    # here, append_event(verdict) raised a validation error on exactly the
    # escalation cases, so the verdict never persisted and convergence
    # telemetry went blind on the rounds that matter most.
    verdict: Literal["PASS", "CONDITIONAL", "FAIL", "CAP", "NEEDS_USER"]
    deterministic_failed: int = Field(default=0, ge=0)
    deterministic_load_bearing: int = Field(default=0, ge=0)
    critic_finding_count: int = Field(default=0, ge=0)
    load_bearing_critic_findings: list[dict] = Field(default_factory=list, max_length=50)
    scorer_rubric: dict = Field(default_factory=dict)
    short_circuited: bool = False


class RoleSliceBody(BaseModel):
    """M4 spawn-time projection event — one per `build_role_prompt` call.

    Used to measure metadata-bytes-injected vs body-bytes-injected so the
    M4 success check (5× per-spawn input reduction on pipeline-class tasks)
    is falsifiable. ``card_ids`` lists the metadata cards picked by the
    slice-picker — the list the subagent saw at spawn time, NOT the
    bodies it later pulled via roles_get (see RoleBodyFetchedBody for
    those).
    """
    subtask_role: str = Field(min_length=1, max_length=64)
    card_ids: list[str] = Field(default_factory=list, max_length=20)
    metadata_bytes: int = Field(ge=0)
    body_bytes: int = Field(ge=0)
    legacy_mode: bool = False


class RoleBodyFetchedBody(BaseModel):
    """M4 lazy-load event — one per `roles_get(role_id)` MCP call from a
    spawned subagent. Pairs with RoleSliceBody so the bootstrap report
    can show ``per_spawn_total = metadata_bytes + sum(body_bytes)``."""
    role_id: str = Field(min_length=1, max_length=64)
    level: Literal["micro", "lean", "full"] = "lean"
    body_bytes: int = Field(ge=0)


class SpawnUsageBody(BaseModel):
    """M5+ token + cache accounting for one spawn.

    Cache columns are the load-bearing metric for verifying whether the
    M5 success-check gap is real or a static-measurement shadow:
    `cache_read_input_tokens` are billed at ~10% of the input rate, so
    a spawn whose prompt fits the cache window pays a tenth of the
    naïve byte count. Subtract `cache_read_input_tokens` × 0.9 from the
    static per-spawn input to get the effective billable input.
    """
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    duration_ms: int = Field(default=0, ge=0)
    provider: str = Field(default="unknown", max_length=64)
    # Declared, not assumed. Pydantic drops unknown keys SILENTLY — that is how
    # every stage_timings value published after P0.5 was thrown away at the
    # door (see ConvergenceTelemetryBody). An emitter sending `ts` against an
    # undeclared field would lose it the same way, without an error.
    ts: str = Field(default="", max_length=64)


class ConvergenceTelemetryBody(BaseModel):
    """PR 4 — one row per published review verdict in the session-loop path.

    Emitted by ``dispatch_subtask_streaming`` after every successful
    ``publish_review`` call. Used to observe whether the session-loop
    retry path is actually converging (rounds decreasing, findings_count
    trending to zero) or oscillating. ``attempt`` is 1-indexed and
    monotonically increases per ``(subtask_id, artifact_id)`` family;
    ``findings_count`` is load-bearing for the convergence metric (its
    delta across attempts is the signal).
    """
    subtask_id: str = Field(min_length=1, max_length=128)
    artifact_id: str = Field(min_length=1, max_length=128)
    attempt: int = Field(ge=1)
    verdict: Literal["PASS", "FAIL", "CAP", "NEEDS_USER"]
    findings_count: int = Field(ge=0)
    ts: str = Field(default="", max_length=64)
    # D3a — why the loop stopped, when it stopped for a budget reason rather
    # than a verdict. All optional with defaults, so every pre-existing row
    # stays valid and no migration is needed. ``reason`` is a short stable
    # token plus detail (e.g. "subtask_revision_ceiling (2/2 …)"), and the two
    # counts let review_loop_stats separate "converged with items open" from
    # "ran out of budget with items open" without re-reading the ledger.
    reason: str = Field(default="", max_length=200)
    open_findings: int = Field(default=0, ge=0)
    resolved_findings: int = Field(default=0, ge=0)
    # ROCK-SOLID v5 P0.5 emitted these and P3.9 needs them; the model never
    # declared the field, and pydantic drops unknown keys silently, so every
    # stage timing published since P0.5 was thrown away at the door. Probed
    # before fixing: a body carrying stage_timings came back without it.
    #
    # Free-form on purpose — the stage set is the reviewer's (queue_wait /
    # deterministic / critic / scorer today), and pinning it here would mean
    # a new stage is dropped exactly the same way this one was.
    stage_timings: dict[str, float] = Field(default_factory=dict)


class BootstrapSizesBody(BaseModel):
    """M5 per-spawn bootstrap accounting — one per `bootstrap()` call
    from a spawned subagent.

    Closes the per-spawn input ledger across M4 + M5:
        total_input ≈ role_slice.metadata + Σ role_body_fetched.body
                    + bootstrap_sizes.total

    Per-section bytes let the success-check scorer attribute reductions
    to specific builders (memory / tools / memory_index / tunnels).
    `legacy_mode=True` means OKURO_LEGACY_BOOTSTRAP was honoured for
    this spawn so the row is excluded from M5 success-check averages.
    """
    total_bytes: int = Field(ge=0)
    memory_bytes: int = Field(default=0, ge=0)
    memory_index_bytes: int = Field(default=0, ge=0)
    tools_bytes: int = Field(default=0, ge=0)
    tunnels_bytes: int = Field(default=0, ge=0)
    legacy_mode: bool = False
    provider: str = Field(default="unknown", max_length=64)


_BODY_MODELS: dict[str, type[BaseModel]] = {
    "decision": DecisionBody,
    "supersedes": SupersedesBody,
    "contract": ContractBody,
    "gap": GapBody,
    "open_question": OpenQuestionBody,
    "compression": CompressionBody,
    "verdict": VerdictBody,
    "role_slice": RoleSliceBody,
    "role_body_fetched": RoleBodyFetchedBody,
    "bootstrap_sizes": BootstrapSizesBody,
    "spawn_usage": SpawnUsageBody,
    "convergence_telemetry": ConvergenceTelemetryBody,
}


# ---------------------------------------------------------------------------
# Top-level event envelope
# ---------------------------------------------------------------------------


class TaskEvent(BaseModel):
    """One typed event in a task's append-only log.

    ``body`` shape is determined by ``event_type`` via _BODY_MODELS.
    Validation happens in append_event — we accept dict here and coerce
    in the storage call so the public API stays dict-friendly for MCP
    callers that cannot import Pydantic.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = Field(min_length=1)
    subtask_id: str = Field(min_length=1)
    from_role: str = ""
    event_type: EventType
    body: dict
    supersedes: Optional[str] = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    seq: int = Field(default=0, ge=0)  # assigned at append time
    created_at: str = ""
    created_by: str = ""

    @field_validator("body")
    @classmethod
    def _validate_body_shape(cls, v: Any) -> dict:
        if not isinstance(v, dict):
            raise ValueError("body must be an object")
        return v


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EventConflictError(ValueError):
    """A decision event conflicts with an active ADR or prior decision.

    The error message is shaped for direct surfacing to the producer
    subagent (REJECTED + recovery hint).
    """


# ---------------------------------------------------------------------------
# JSONL mirror
# ---------------------------------------------------------------------------


def _jsonl_path(task_id: str, tasks_dir: Path | None = None) -> Path | None:
    """Resolve the per-task events.jsonl path. None = no on-disk task dir."""
    if tasks_dir is None:
        try:
            from okuro.orchestrator.config import Config
            cfg = Config.load() if hasattr(Config, "load") else Config()
            tasks_dir = getattr(cfg, "tasks_dir", None)
        except Exception:
            return None
    if tasks_dir is None:
        return None
    base = Path(tasks_dir) / task_id
    if not base.exists():
        return None
    return base / "events.jsonl"


def _append_jsonl(path: Path, row: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("task_events: jsonl mirror write failed for %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


def _active_topics(db, task_id: str) -> dict[str, dict]:
    """Map topic -> {event_id, choice, seq} for unsuperseded decisions.

    A decision is "active" when no later 'supersedes' event references
    its id. Linear scan over task_events for the task — tasks have on the
    order of tens to low-hundreds of events, so this stays cheap.
    """
    rows = db.fetchall(
        """SELECT id, body, event_type FROM task_events
           WHERE task_id = ? ORDER BY seq ASC""",
        (task_id,),
    )
    superseded: set[str] = set()
    decisions: dict[str, dict] = {}  # event_id -> parsed body
    order: list[str] = []
    for r in rows:
        eid = r["id"]
        etype = r["event_type"]
        try:
            body = json.loads(r["body"] or "{}")
        except (TypeError, ValueError):
            continue
        if etype == "decision":
            decisions[eid] = body
            order.append(eid)
        elif etype == "supersedes":
            target = body.get("target_event_id")
            if target:
                superseded.add(target)
    out: dict[str, dict] = {}
    for eid in order:
        if eid in superseded:
            continue
        b = decisions[eid]
        topic = (b.get("topic") or "").strip().lower()
        if not topic:
            continue
        out[topic] = {"event_id": eid, "choice": b.get("choice", ""), "body": b}
    return out


def _adr_topics(adrs: list[dict] | None) -> dict[str, dict]:
    """Map topic -> ADR row from M1 task.adrs.

    M1 ADR shape: {gate_id, phase_id, prompt, options, selected_option_id,
    selected_label, selected_rationale, resolved_at}. The 'prompt' string
    is what the gate asked the user — we treat it as the topic key.
    """
    out: dict[str, dict] = {}
    for adr in (adrs or []):
        if not isinstance(adr, dict):
            continue
        prompt = (adr.get("prompt") or "").strip().lower()
        if not prompt:
            continue
        out[prompt] = adr
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append_event(
    *,
    task_id: str,
    subtask_id: str,
    event_type: EventType,
    body: dict,
    from_role: str = "",
    supersedes: Optional[str] = None,
    confidence: float = 0.8,
    created_by: str = "",
    adrs: list[dict] | None = None,
    tasks_dir: Path | None = None,
) -> str:
    """Append one typed event to the task's log.

    Returns the new event id on success. Raises:
        ValueError       — schema validation failed
        EventConflictError — decision conflicts with active ADR / prior decision
                             and event_type is not 'supersedes'

    ``adrs`` is task.adrs from M1 — pass it through so the conflict
    check covers user-locked decisions too. Skipping it means the gate
    layer is invisible to this layer and conflicts will be missed.
    """
    if not task_id or not task_id.strip():
        raise ValueError("task_id is required")
    if not subtask_id or not subtask_id.strip():
        raise ValueError("subtask_id is required")
    if event_type not in _BODY_MODELS:
        raise ValueError(
            f"event_type={event_type!r} must be one of "
            f"{sorted(_BODY_MODELS.keys())}"
        )

    # Validate body shape against the event_type model.
    model_cls = _BODY_MODELS[event_type]
    try:
        validated_body = model_cls.model_validate(body).model_dump()
    except Exception as exc:
        raise ValueError(
            f"body failed validation for event_type={event_type!r}: {exc}"
        )

    from okuro.db import get_db
    db = get_db()

    with db.write():
        # Conflict detection — decision events only. A 'supersedes' event
        # is itself the resolution path so it never conflicts.
        if event_type == "decision":
            existing = _active_topics(db, task_id)
            topic = (validated_body.get("topic") or "").strip().lower()
            if topic and topic in existing:
                prior = existing[topic]
                raise EventConflictError(
                    f"REJECTED: decision on topic {topic!r} conflicts with "
                    f"active event {prior['event_id'][:8]} "
                    f"(prior choice: {prior['choice']!r}). "
                    f"To override, first emit event_type='supersedes' with "
                    f"target_event_id={prior['event_id']!r}, then re-emit "
                    f"this decision. Otherwise reconcile by referencing the "
                    f"prior choice."
                )
            adr_topics = _adr_topics(adrs)
            if topic and topic in adr_topics:
                adr = adr_topics[topic]
                raise EventConflictError(
                    f"REJECTED: decision on topic {topic!r} conflicts with "
                    f"a USER-LOCKED ADR (gate {adr.get('gate_id', '?')}, "
                    f"phase {adr.get('phase_id', '?')}, choice: "
                    f"{adr.get('selected_label', '?')!r}). ADRs cannot be "
                    f"superseded by subagents — surface this as an "
                    f"open_question and let the user reopen the gate."
                )

        # Assign monotonic seq under the lock.
        row = db.fetchone(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM task_events WHERE task_id = ?",
            (task_id,),
        )
        next_seq = int(row["m"] if row else 0) + 1

        ev = TaskEvent(
            task_id=task_id,
            subtask_id=subtask_id,
            from_role=from_role,
            event_type=event_type,
            body=validated_body,
            supersedes=supersedes,
            confidence=confidence,
            seq=next_seq,
            created_at=datetime.utcnow().isoformat(),
            created_by=created_by,
        )

        db.execute(
            """INSERT INTO task_events (
                    id, task_id, subtask_id, from_role, event_type,
                    body, supersedes, confidence, seq, created_at, created_by
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ev.id, ev.task_id, ev.subtask_id, ev.from_role, ev.event_type,
                json.dumps(ev.body), ev.supersedes, ev.confidence, ev.seq,
                ev.created_at, ev.created_by,
            ),
        )

        # Mirror to disk JSONL — best-effort. Storage truth is sqlite;
        # the JSONL is for replay / grep / debugging. A failed write logs
        # but does not unwind the DB insert.
        jsonl = _jsonl_path(task_id, tasks_dir=tasks_dir)
        if jsonl is not None:
            _append_jsonl(jsonl, ev.model_dump())

    return ev.id


def list_events(
    *,
    task_id: str,
    event_type: EventType | None = None,
    since_seq: int = 0,
    limit: int = 500,
) -> list[dict]:
    """Return events for a task in seq order, oldest first."""
    from okuro.db import get_db
    db = get_db()
    sql = "SELECT * FROM task_events WHERE task_id = ? AND seq > ?"
    params: list[Any] = [task_id, since_seq]
    if event_type:
        sql += " AND event_type = ?"
        params.append(event_type)
    sql += " ORDER BY seq ASC LIMIT ?"
    params.append(limit)
    rows = db.fetchall(sql, tuple(params))
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["body"] = json.loads(d.get("body") or "{}")
        except (TypeError, ValueError):
            d["body"] = {}
        out.append(d)
    return out


def attempts_for_artifact(*, task_id: str, artifact_id: str) -> int:
    """PR B — canonical per-artifact attempt count.

    Stage B G6 noted the codebase had two attempt counters: ``subtask.retries``
    (incremented in plan.yaml on each FAIL by the engine) and the implicit
    count of convergence_telemetry rows per artifact_id (one per publish).
    The user contract is **per artifact_id**: max 3 attempts per piece of
    work. The dispatcher's session-loop emits one ``convergence_telemetry``
    row per attempt + artifact, so counting them is the canonical surface.

    Returns 0 when no telemetry exists yet (artifact never published).
    """
    rows = list_events(
        task_id=task_id, event_type="convergence_telemetry", limit=200,
    )
    return sum(
        1 for r in rows
        if str((r.get("body") or {}).get("artifact_id") or "") == str(artifact_id)
    )


def active_decisions(*, task_id: str) -> list[dict]:
    """Return active (unsuperseded) decisions for a task in seq order."""
    from okuro.db import get_db
    db = get_db()
    return list(_active_topics(db, task_id).values())


def latest_compression(*, task_id: str) -> dict | None:
    """Return the most recent compression event (or None)."""
    events = list_events(task_id=task_id, event_type="compression", limit=1)
    # list_events sorts ASC by seq; the last entry is newest.
    rows = events[-1:] if events else []
    return rows[0] if rows else None
