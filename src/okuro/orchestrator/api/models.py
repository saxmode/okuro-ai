# SPDX-License-Identifier: Apache-2.0
# <!-- AGENT_HEADER
# role: code
# purpose: Pydantic models for the Okuro Orchestrator API.
# index:
#   imports
#   class TaskStatus
#   class RiskLevel
#   class SubtaskSummary
#   class PhaseSummary
#   class TaskSummary
#   class TaskDetail
#   class LogEntry
#   class ArtifactInfo
#   class TaskState
#   class SystemStatus
#   class WSEvent
#   class WSTaskEvent
#   class WSLogEvent
#   class WSStateChange
#   class WSTaskCreated
# AGENT_HEADER_END -->
"""Pydantic models for the Okuro Orchestrator API."""

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    HALTED = "halted"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MED = "MED"
    HIGH = "HIGH"


# -- Response Models --

class SubtaskSummary(BaseModel):
    id: str
    role: str
    description: str
    risk: str
    complexity: str
    status: str
    duration: float = 0.0
    error: str = ""
    artifact: str = ""  # primary artifact (backward compat)
    artifacts: List[str] = []  # all artifact filenames for this subtask
    cli_used: str = ""
    model_used: str = ""
    planned_model: str = ""  # resolved from complexity tier (or model_override)
    model_override: str = ""  # user-set override (bypasses tier resolution)
    started_at: str = ""  # ISO timestamp when subtask started running
    retries: int = 0  # number of retry attempts already consumed
    # When the subtask's phase sits at-or-after an unlocked decision gate,
    # the engine refuses to dispatch it. Surface that explicitly so the UI
    # can distinguish "waiting for ADR" from "waiting for a worker slot" —
    # both render as status=pending without this hint.
    blocked_by_gate: bool = False
    blocking_gate_id: str = ""
    blocking_gate_phase: int = 0
    # Step 2 — BE-supplied rendering metadata. FE switches on color_class
    # (closed enum: neutral|info|warning|error|success); label is rendered
    # raw. Adding a new BE status = one row in state_reader maps, zero FE
    # code change. See docs/audit-2026-05-26/02-frontend-state-audit.md §6.
    color_class: str = ""
    label: str = ""
    # Step 8 — set when the M3 reviewer is currently judging this subtask's
    # phase. FE renders an inline "REVIEWING" badge so the reviewer activity
    # is co-located with the work, not just in the global thinker.
    review_in_progress: bool = False
    # Per-subtask review summary — lets the tile show "reviewed Nx, PASS"
    # without opening the activity feed. Derived durably in state_reader
    # from ``retries`` (each reviewer FAIL = one prior round) + the
    # subtask's terminal status, NOT from the TTL-expiring review_queue
    # table. ``review_attempt`` is the attempt number reached
    # (retries + 1); ``review_max_attempts`` is the configured ceiling
    # (default 5); ``review_verdict`` is the latest verdict
    # (PASS | CONDITIONAL | FAIL) or "" when no verdict yet / never
    # reviewed. All zero/empty when the subtask saw no review activity so
    # untouched tiles stay clean.
    review_attempt: int = 0
    review_max_attempts: int = 0
    review_verdict: str = ""


class PhaseSummary(BaseModel):
    id: int
    name: str
    status: str
    subtasks: List[SubtaskSummary] = []
    # Step 2 — same contract as SubtaskSummary. `state` is the aggregate
    # phase state computed from subtasks + persisted status (handles
    # blocked_review which is_phase_complete cannot represent).
    state: str = ""
    color_class: str = ""
    label: str = ""
    review_in_progress: bool = False


class TaskSummary(BaseModel):
    id: str
    title: str = ""
    description: str
    status: str
    created_at: str
    current_phase: int = 1
    phases_total: int = 0
    subtasks_done: int = 0
    subtasks_total: int = 0
    task_type: str = ""
    recurring_def_id: str = ""
    intelligence: str = ""
    # Step 6 — BE-supplied lifecycle rendering metadata. Lets the
    # task list / sidebar / home page render the badge color + label
    # without ever switching on raw status strings. Same source as the
    # snapshot endpoint (`_LIFECYCLE_COLOR` + `_LIFECYCLE_LABEL`).
    color_class: str = ""
    label: str = ""


class TaskDetail(BaseModel):
    id: str
    title: str = ""
    description: str
    status: str
    created_at: str
    current_phase: int = 1
    phases: List[PhaseSummary] = []
    progress_percent: int = 0
    intelligence: str = ""
    # Filesystem path to the project this task realizes work in (e.g. a
    # webapp under ~/workspace/...). Used by the preview
    # auto-detector to find a runnable artifact instead of falling back
    # to scanning the orchestrator's bookkeeping dir. Empty when unknown.
    project_path: str = ""


class LogEntry(BaseModel):
    timestamp: str
    type: str
    data: Dict = {}


class Intervention(BaseModel):
    """User prompt that shaped the task — initial description or continuation."""
    id: str
    ts: str
    kind: str  # "initial" | "continuation"
    text: str
    before_phase_id: int
    # "initial" | "user_typed" | "suggestion_accepted" | "unknown"
    source: str = "user_typed"


class ArtifactInfo(BaseModel):
    name: str
    path: str
    size_bytes: int
    title: str = ""
    modified_at: str = ""  # ISO 8601 UTC timestamp
    # Subtask that produced this artifact, derived from the filename's
    # leading numeric prefix (e.g. "1.1-findings.md" -> "1.1"). None when
    # the filename has no recognisable prefix (e.g. screenshots).
    subtask_id: Optional[str] = None
    # Storage layer this artifact lives in:
    #   "brain" — row in the artifacts SQLite table (Stream B reports +
    #             plans + evidence). Use ``id`` to fetch via artifact_get
    #             or the /artifacts/{id} content endpoint.
    #   "disk"  — file under tasks/{id}/artifacts/. Use ``name`` (same as
    #             the legacy listing). Reserved for non-document outputs
    #             that the dispatcher does not route through artifact_write
    #             (binaries, screenshots, .stdout.md fallbacks).
    source: str = "disk"
    # Brain artifact id (UUID) when ``source == "brain"``; None otherwise.
    id: Optional[str] = None
    # Artifact kind (report / evidence / plan) when ``source == "brain"``.
    kind: Optional[str] = None
    # MIME type — "text/markdown" for most brain reports.
    media_type: Optional[str] = None
    # Supersede chain (brain artifacts only). When a subtask is reviewed N
    # times it emits N artifacts with the same title; losers land at
    # ``confidence=0.1`` and the active one keeps ~0.8/0.9. ``supersedes``
    # points an active artifact at the prior-loop id it replaced. The UI
    # uses these to collapse the list to the active artifact per subtask +
    # show a "N versions" badge instead of listing every review round.
    # None for disk artifacts (no review loop).
    confidence: Optional[float] = None
    supersedes: Optional[str] = None
    # Communication stream (brain artifacts). "user" = human deliverable
    # (Stream B, default), "process" = QA/reviewer/M-pipeline meta output,
    # "agent" = agent-to-agent context (compressor decision-traces). The panel
    # defaults to "user" and offers a toggle for the rest. None for disk.
    audience: Optional[str] = None
    created_by: Optional[str] = None


class TaskState(BaseModel):
    """Real-time task state for UI display."""
    task_id: str
    title: str = ""
    description: str = ""
    status: str
    current_phase: int = 1
    progress_percent: int = 0
    phases: List[PhaseSummary] = []
    recent_logs: List[LogEntry] = []
    artifacts: List[ArtifactInfo] = []
    links: List[Dict] = []
    warnings: List[str] = []  # State validation warnings (empty = valid)
    continuation_suggestion: str = ""  # legacy single string
    continuation_suggestions: List[Dict] = []  # structured: [{id, suggestion_text, category, effort, ...}]
    interventions: List[Intervention] = []
    intelligence: str = ""
    project_path: str = ""
    # Resolved project slug (projects.id) for project_path, or "" if the path
    # maps to no registered project. Additive — lets the FE scope a
    # project-filtered inbox strip on the task-detail page without re-deriving
    # the slug client-side.
    project: str = ""
    created_at: str = ""
    # Execution mode: "auto-execute" | "plan" | "deliberate". The task-detail
    # SPA gates its DeliberationPanel on `state.mode === "deliberate"` —
    # missing this field meant deliberation tasks rendered an empty pipeline
    # because the panel branch was never entered.
    mode: str = ""
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    # Single-source-of-truth surface for "engine wants user input". Set by the
    # engine before exit-on-wait; cleared by API handlers when input arrives.
    # When non-null the UI renders a foreground card with the call-to-action
    # described in the payload.
    awaiting: Optional["AwaitingState"] = None
    # Step 4 — ids of phases currently running concurrently. FE displays a
    # "parallel" indicator on each so users see that phase 2 is actively
    # working even when phase 1 is blocked_review awaiting their decision.
    parallel_phases: List[int] = Field(default_factory=list)


class AwaitingState(BaseModel):
    kind: str                       # panel_confirmation | capability_gap | discussion_proceed | decision_gate | blocked_review | timeout_cap
    message: str
    endpoint: str
    method: str = "POST"
    payload: Dict = Field(default_factory=dict)
    since: str = ""


class SystemStatus(BaseModel):
    status: str = "healthy"
    okuro_root: str
    tasks_count: int = 0
    active_tasks: int = 0
    tools_count: int = 0
    uptime_seconds: float = 0.0


# -- WebSocket Event Models --

class WSEvent(BaseModel):
    type: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class WSTaskEvent(WSEvent):
    task_id: str


class WSLogEvent(WSTaskEvent):
    type: str = "log"
    entry: Dict = {}


class WSStateChange(WSTaskEvent):
    type: str = "state_change"
    field: str
    old_value: Optional[str] = None
    new_value: str


class WSTaskCreated(WSEvent):
    type: str = "task_created"
    task_id: str
    description: str
