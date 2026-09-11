/**
 * Okuro API response types.
 * Source of truth: okuro/orchestrator/api/models.py + api/main.py
 */

// -- Status Enums --

export type TaskStatus =
  | "pending"
  | "planning"
  | "active"
  | "done"
  // Ran to the end of what was reachable, but a subtask failed permanently or
  // was stranded behind one. Distinct from "done" so a hollow run is visible.
  | "completed_partial"
  | "failed"
  | "halted"
  | "blocked"
  | "deliberating"
  | "awaiting_decision"
  | "waiting_user";

export type AwaitingKind =
  | "panel_confirmation"
  | "capability_gap"
  | "discussion_proceed"
  | "decision_gate"
  | "blocked_review";

export interface AwaitingState {
  kind: AwaitingKind;
  message: string;
  endpoint: string;
  method: string;
  payload: Record<string, unknown>;
  since: string;
}

/**
 * Structured plain-language gate copy produced by the backend
 * (okuro.orchestrator.gate_messages.humanize_gate). Lives under
 * `payload.presentation`. `headline`/`explanation`/`action` are always
 * jargon-free; `technical_details` holds raw diagnostics rendered behind a
 * "Show details" disclosure. Optional — older payloads only have `message`.
 */
export interface GatePresentation {
  headline: string;
  explanation: string;
  action: string;
  options?: string[];
  /** ROCK-SOLID v5 P2.1 — which entry in `options` is the safe default. */
  recommended?: string;
  technical_details?: string;
}

export type SubtaskStatus =
  | "pending"
  | "running"
  | "done"
  | "failed"
  | "skipped"
  | "waiting_approval"
  | "approved";

export type RiskLevel = "LOW" | "MED" | "HIGH";
export type Complexity = "fast" | "standard" | "strategic";
export type TaskMode = "auto-execute" | "deliberate";

// -- Core Data --

export interface SubtaskSummary {
  id: string;
  role: string;
  description: string;
  risk: string;
  complexity: string;
  status: SubtaskStatus;
  duration: number;
  error: string;
  artifact?: string;
  artifacts?: string[];
  cli_used?: string;
  model_used?: string;
  planned_model?: string;
  model_override?: string;
  started_at?: string;
  dependencies?: string[];
  retries?: number;
  // Snapshot-supplied — present when consumed from /snapshot endpoint.
  color_class?: ColorClass;
  label?: string;
  blocked_by_gate?: boolean;
  blocking_gate_phase?: number;
  review_in_progress?: boolean;
  // Per-subtask review summary (BE-derived). Lets the tile show
  // "reviewed Nx · PASS" without opening the activity feed. All
  // zero/empty when the subtask saw no review activity.
  review_attempt?: number;
  review_max_attempts?: number;
  review_verdict?: "PASS" | "CONDITIONAL" | "FAIL" | "";
}

export interface PhaseSummary {
  id: number;
  name: string;
  status: string;
  subtasks: SubtaskSummary[];
  // Snapshot-supplied — present when consumed from /snapshot endpoint.
  state?: string;
  color_class?: ColorClass;
  label?: string;
  review_in_progress?: boolean;
}

// -- Canonical snapshot — single source of truth for the FE. -----------
// Step 1+2 of the canonical-state migration. The five color_class values
// are FE-fixed; every status carries its color + label decided by the BE
// so the FE has zero derivation in component code.

export type ColorClass = "neutral" | "info" | "warning" | "error" | "success";

export interface SnapshotLifecycle {
  state: string;
  label: string;
  color_class: ColorClass;
  progress_percent: number;
  current_phase: number;
  /** Why this task was halted, in the halter's own words (task.yaml
   *  `halt_reason`). Empty for every non-halted task. Optional so an older
   *  snapshot still typechecks. */
  halt_reason?: string;
}

export interface SnapshotBlocker {
  kind: string;
  label: string;
  color_class: ColorClass;
  summary: string;
  endpoint: string;
  method: string;
  payload: Record<string, unknown>;
  since: string;
  /** BE-derived: this gate has been open >48h and looks forgotten rather
   *  than under consideration (P2.2). Informational — nothing is broken, and
   *  the recurring tasks that cannot afford to wait never park at all.
   *  Optional so an older snapshot still typechecks. */
  stale?: boolean;
}

export interface SnapshotSubtask {
  id: string;
  role: string;
  description: string;
  status: string;
  color_class: ColorClass;
  label: string;
  duration: number;
  error: string;
  started_at: string;
  retries: number;
  model: string;
  risk: string;
  artifacts: string[];
  artifact: string;
  blocked_by_gate: boolean;
  blocking_gate_phase: number;
}

export interface SnapshotPhase {
  id: number;
  name: string;
  status: string;
  state: string;
  color_class: ColorClass;
  label: string;
  progress: {
    running: number;
    done: number;
    failed: number;
    pending: number;
    total: number;
  };
  subtasks: SnapshotSubtask[];
  decision_gate: {
    id: string;
    status: string;
    prompt: string;
  } | null;
}

export interface TaskSnapshot {
  task_id: string;
  title: string;
  description: string;
  mode: string;
  intelligence: string;
  project_path: string;
  created_at: string;
  current_phase: number;
  progress_percent: number;
  phases_total: number;
  lifecycle: SnapshotLifecycle;
  blocker: SnapshotBlocker | null;
  // Engine liveness as a first-class fact (Phase 1). "idle" = no engine
  // expected (parked/terminal); "live" = engine actively owns the task;
  // "recovering" = engine gone but within the reconciler respawn window;
  // "wedged" = should be running but isn't (the wedged-looks-alive class).
  engine_state?: "live" | "recovering" | "wedged" | "idle";
  phases: SnapshotPhase[];
  parallel_phases: number[];
  interventions: unknown[];
  warnings: string[];
  continuation_suggestions: unknown[];
  last_updated: string;
  // ORDERING cursor (last_updated epoch-ms). Orders GENERATIONS of persisted
  // state and kills the poll-regresses-a-fresher-push flicker. NOT a content
  // cursor — read-time fields (engine_state, blocker.stale) move without it.
  // Optional for back-compat with snapshots from before the field existed.
  seq?: number;
  // Digest of the whole snapshot (P3.5). Breaks a same-seq tie so a
  // re-derived read-time field still applies. Absent on older backends.
  content_hash?: string;
  /** P4.6 — set only when the engine that owns this task is running an older
   *  commit than the API. Null in the normal case, so nothing renders. */
  engine_staleness?: {
    engine_sha: string;
    api_sha: string;
    color_class: ColorClass;
    label: string;
  } | null;
}

// -- Task shapes --

export interface TaskSummary {
  id: string;
  title: string;
  description: string;
  status: TaskStatus;
  created_at: string;
  current_phase: number;
  phases_total: number;
  subtasks_done: number;
  subtasks_total: number;
  task_type?: string;
  recurring_def_id?: string;
  intelligence?: string;
  /** "" | "per_artifact" | "closeout" — per-task review trigger. */
  review_trigger?: string;
  mode?: TaskMode;
  /** Step 6 — BE-supplied lifecycle metadata. FE list views render
   *  badge color + label without ever switching on raw status strings. */
  color_class?: ColorClass;
  label?: string;
}

export interface TaskDetail {
  id: string;
  title?: string;
  description: string;
  status: TaskStatus;
  created_at: string;
  current_phase: number;
  phases: PhaseSummary[];
  progress_percent: number;
  intelligence?: string;
  /** "" | "per_artifact" | "closeout" — per-task review trigger. */
  review_trigger?: string;
  /**
   * Filesystem path to the project this task realizes work in. The
   * preview "See result" feature scans this dir to auto-detect a
   * runnable webapp. Empty string when unknown.
   */
  project_path?: string;
}

export interface ContinuationSuggestion {
  id: string;
  suggestion_text: string;
  category: "followup" | "research" | "build" | "validate" | "business";
  effort: "small" | "medium" | "large";
  source_role?: string;
  timestamp?: string;
  dismissed?: boolean;
}

/**
 * User prompt that shaped this task — the original task description
 * (`kind: "initial"`) plus every Continue/Retry instruction (`kind:
 * "continuation"`). Rendered as inline cards in the pipeline view,
 * anchored to the phase they produced. Persisted server-side the
 * moment the API receives them so they survive planner crashes.
 */
export interface Intervention {
  id: string;
  ts: string;
  kind: "initial" | "continuation";
  text: string;
  before_phase_id: number;
  /**
   * Distinguishes free-text user prompts from agent-suggested
   * continuations the user merely clicked-to-accept. The flow chart
   * surfaces only `initial` and `user_typed` so the cards reflect
   * the user's voice, not the orchestrator's suggestion stream.
   */
  source: "initial" | "user_typed" | "suggestion_accepted" | "unknown";
}

export interface TaskState {
  task_id: string;
  title?: string;
  description: string;
  status: TaskStatus;
  current_phase: number;
  progress_percent: number;
  phases: PhaseSummary[];
  recent_logs: LogEntry[];
  artifacts: ArtifactInfo[];
  created_at: string;
  last_updated: string;
  intelligence?: string;
  /** "" | "per_artifact" | "closeout" — per-task review trigger. */
  review_trigger?: string;
  project_path?: string;
  /** Resolved project slug (projects.id) for project_path; "" if unresolved. */
  project?: string;
  continuation_suggestion?: string;
  continuation_suggestions?: ContinuationSuggestion[];
  interventions?: Intervention[];
  warnings?: string[];
  mode?: TaskMode;
  awaiting?: AwaitingState | null;
  parallel_phases?: number[];
}

// -- Logs & Artifacts --

export interface LogEntry {
  timestamp: string;
  type: string;
  data: Record<string, unknown>;
}

export interface ArtifactInfo {
  name: string;
  path: string;
  size_bytes: number;
  title: string;
  modified_at?: string;
  /**
   * Subtask that produced this artifact, derived from the filename's
   * leading numeric prefix (e.g. "1.1-findings.md" -> "1.1"). Null when
   * the filename has no recognisable prefix.
   */
  subtask_id?: string | null;
  /**
   * Storage layer the artifact lives in:
   *   "brain" — row in the artifacts SQLite table (Stream B reports).
   *             ``id`` is set; fetch via /api/tasks/{id}/artifacts/{id}.
   *   "disk"  — file under tasks/{id}/artifacts/. ``name`` is the filename;
   *             fetch via /api/tasks/{id}/artifacts/{name}.
   * Default "disk" for backward-compat when the API is older than 035.
   */
  source?: "brain" | "disk";
  /** Brain artifact id (UUID) when source === "brain". */
  id?: string | null;
  /** Artifact kind — "report" | "evidence" | "plan" — when source === "brain". */
  kind?: string | null;
  /** MIME type — "text/markdown" for most reports. */
  media_type?: string | null;
  /**
   * Supersede chain (brain artifacts only). A subtask reviewed N times
   * emits N artifacts with the same title; losers land at confidence≈0.1
   * and the active one keeps ~0.8/0.9. ``supersedes`` points an active
   * artifact at the prior-loop id it replaced. The viewer uses these to
   * collapse the list to the active artifact + show a "N versions" badge.
   * null for disk artifacts (no review loop).
   */
  confidence?: number | null;
  supersedes?: string | null;
  /**
   * Who the artifact is for (migration 085ce122a):
   *   "user"    — human deliverable (Stream B); the default panel view.
   *   "process" — QA/reviewer/M-pipeline meta (verdicts, autofix).
   *   "agent"   — agent-to-agent context (compressor decision traces).
   * Absent on older rows / disk artifacts → treated as "user".
   */
  audience?: "user" | "process" | "agent" | null;
  /** Producer identifier (agent/role/user) — brain artifacts only. */
  created_by?: string | null;
}

/**
 * Stream C delivery — an audience-adapted render of an artifact for one
 * recipient on one channel. See migration 036.
 */
export interface DeliveryInfo {
  id: string;
  artifact_id: string;
  person_id?: string | null;
  channel: "markdown" | "marp" | "microsite" | "tts" | "podcast";
  brand_id?: string | null;
  title?: string | null;
  body_path?: string | null;
  media_type?: string | null;
  duration_ms?: number | null;
  cost_usd?: number | null;
  provider?: string | null;
  model?: string | null;
  success: boolean;
  error?: string | null;
  created_by?: string | null;
  created_at?: string | null;
  /** Outline JSON; audience_metadata sits under it. */
  outline?: Record<string, unknown> | null;
}

// -- Activity Feed --

export interface ActivityEvent {
  ts: string;
  type:
    | "tool_use"
    | "thinking"
    | "text"
    | "result"
    | "session_start"
    | "subtask_start"
    | "review_starting"
    | "review_complete"
    // Smart-gate auto-route into deliberation. Global broadcast from
    // main.py broadcast_agent_event("smart_gate_deliberation", ...).
    | "smart_gate_deliberation"
    // Reviewer reset a subtask after a FAIL verdict. Per-task activity row
    // from reviewer/pipeline.py (~L728).
    | "subtask_retry"
    // Retry cap hit — phase flipped to blocked_review. Per-task activity row
    // from engine.py (~L1369). Verdict carried for the FE branch.
    | "verdict"
    // One row per Critic finding, streamed from reviewer/pipeline.py during a
    // review pass (synthetic subtask_id `reviewer:phase{N}:critic`). Carries the
    // finding detail (severity/summary/file/line) so the feed can render WHAT
    // was flagged and WHY — for every verdict, not just blocked_review.
    | "critic_finding"
    | "phase_blocked_by_review_cap";
  name?: string;
  preview?: string;
  text?: string;
  success?: boolean;
  duration_ms?: number;
  cost_usd?: number;
  // Token accounting from the streaming result envelope. Surfaced
  // by the decomposer activity panel (and any future cost view).
  input_tokens?: number;
  output_tokens?: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
  tools_count?: number;
  role?: string;
  cmd?: string;
  subtask_id?: string;
  // Reviewer marker fields — present only on review_starting / review_complete
  // rows the engine appends to .activity.jsonl around each M3 gate. They let
  // the FE feed interleave reviewer activity into each subtask's panel
  // without leaning on a separate channel.
  review_kind?: string;
  phase_id?: number;
  // Reviewer terminal verdict. PASS/CONDITIONAL/FAIL are the M3 critic
  // outcomes; NEEDS_USER is emitted when the reviewer escalates a decision
  // to the user (seen on review_complete rows in live data); CAP is the
  // retry-budget-exhausted state surfaced via phase_blocked_by_review_cap.
  verdict?: "PASS" | "CONDITIONAL" | "FAIL" | "NEEDS_USER" | "CAP";
  load_bearing_findings?: number;
  message?: string;
  // smart_gate_deliberation — clarity confidence + the deliberation band
  // [threshold, ceiling) that triggered the auto-route, plus a human reason.
  confidence?: number;
  band?: [number, number];
  reason?: string;
  // subtask_retry — which subtask, attempt counter, and how much prior work
  // was superseded on the reset.
  subtask?: string;
  retries?: number;
  max_retries?: number;
  superseded_artifacts?: number;
  superseded_handovers?: number;
  // phase_blocked_by_review_cap — the subtasks the reviewer capped after the
  // retry budget ran out, and the load-bearing finding count.
  capped_subtasks?: string[];
  critic_findings?: number;
  // critic_finding — one streamed Critic finding. `severity` is "load_bearing"
  // (must-fix) or "cosmetic"; `finding_summary` is the human-readable issue;
  // file/line locate it. Emitted by reviewer/pipeline.py:519.
  severity?: string;
  finding_summary?: string;
  file?: string;
  line?: string | number;
  // result rows from the scorer stage carry the scorer's own verdict and the
  // override reason when the Critic's load-bearing finding forced FAIL.
  scorer_verdict?: string;
  override_reason?: string;
}

// -- Structured review history (GET /api/tasks/{id}/review) --
// The durable verdict record from the task_events store — authoritative,
// unlike the best-effort activity feed. One entry per review pass.

export interface ReviewVerdictFinding {
  file?: string | null;
  line?: string | number | null;
  summary?: string;
}

export interface ReviewVerdictBody {
  phase_id: number;
  verdict: "PASS" | "CONDITIONAL" | "FAIL" | "CAP" | "NEEDS_USER";
  deterministic_failed?: number;
  deterministic_load_bearing?: number;
  critic_finding_count?: number;
  load_bearing_critic_findings?: ReviewVerdictFinding[];
  scorer_rubric?: Record<string, unknown>;
  short_circuited?: boolean;
}

export interface ReviewVerdictEvent {
  id: string;
  seq: number;
  task_id: string;
  subtask_id?: string;
  event_type: string;
  body: ReviewVerdictBody;
  created_at?: string;
}

export interface TaskReviewResponse {
  task_id: string;
  verdicts: ReviewVerdictEvent[];
}

// -- Request bodies --

export interface CreateTaskRequest {
  description: string;
  mode?: TaskMode;
  dry_run?: boolean;
  auto_approve?: boolean;
  preferred_cli?: string;
  intelligence?: string;
  /** "" | "per_artifact" | "closeout" — per-task review trigger. */
  review_trigger?: string;
  required_roles?: string[];
  recurring?: boolean;
  recurring_title?: string;
  recurring_role?: string;
  recurring_schedule?: string;
  /** Set when the task was spawned from a registered todo (Now-page click).
   *  Engine marks the todo done when the task reaches a terminal state. */
  source_todo_id?: string;
  /** Set when the task was spawned from an action-item embedded in a
   *  thought (Now-page Action Items click). Engine marks the parent
   *  thought resolved when the task reaches a terminal state. */
  source_thought_id?: string;
  /** Force past the clarity intake gate. Set when the user chooses to proceed
   *  despite an intake_required response (main.py intake_gate, ~L2384). */
  skip_intake?: boolean;
  /** ROCK-SOLID v5 P5.4 — Stream C recipient (a person_id). When set, each
   *  Stream B artifact a subtask produces is delivered to that person as the
   *  subtask finalizes. Unset means no delivery, which is the default. */
  /** P6.7 — fast-track review profile. An all-LOW-risk step whose
   *  deterministic checks pass clean skips the critic + scorer LLM stages. */
  fast_track?: boolean;
  audience?: string;
  /** markdown | marp | microsite | tts | podcast. Defaults to markdown when
   *  a recipient is set; meaningless without one. */
  delivery_channel?: string;
  /** Brand override for the delivery theme. Meaningless without a recipient. */
  delivery_brand_id?: string;
  /** F2 autopilot — auto-answer EVERY human gate (incl. high-risk approvals +
   *  capability-gap) from the profile instead of parking. Omit to inherit the
   *  global orchestrator.autopilot default. The confidence/abstain fallback is
   *  the only safeguard, so enabling means a fully-unattended run. */
  autopilot?: boolean;
}

export interface ContinueTaskRequest {
  description: string;
  dry_run?: boolean;
  auto_approve?: boolean;
  preferred_cli?: string;
  intelligence?: string;
  /** "" | "per_artifact" | "closeout" — per-task review trigger. */
  review_trigger?: string;
}

export interface CreateTaskResponse {
  status: string;
  /** Absent when status === "intake_required" — the gate blocked before a
   *  task was spawned, so there is nothing to navigate to. */
  task_id?: string;
  pid?: number;
  description?: string;
  dry_run?: boolean;
  /** Present only when status === "intake_required". The clarity gate
   *  (main.py intake_gate) returns these instead of spawning a task when
   *  confidence < CONFIDENCE_THRESHOLD. */
  confidence?: number;
  missing?: string[];
  questions?: string[];
  hint?: string;
}

// -- System --

export interface SystemStatus {
  status: string;
  okuro_root: string;
  tasks_count: number;
  active_tasks: number;
  tools_count: number;
  uptime_seconds: number;
}

// -- Roles --

export interface RoleInfo {
  id: string;
  domain: string;
  description: string;
  tier: string;
  model: string;
  maturity: string;
  sessions: number;
  learnings: number;
  // Maintenance fields (added with migration 011 — role self-maintenance loop).
  maintenance_schedule?: string | null;
  last_maintained?: string | null;
  stale?: boolean;
  knowledge_count?: number;
}

export interface RoleKnowledgeStats {
  role_id: string;
  total_entries: number;
  by_type: Record<string, number>;
  last_updated: string | null;
  maturity: string | null;
  sessions: number;
  learnings: number;
}

export interface RoleMaintenanceMandate {
  role: string;
  domain: string;
  stale: boolean;
  stale_since: string | null;
  schedule: string;
  research_mandate: {
    searches: string[];
    sources_to_check: string[];
    current_knowledge_summary: string;
  };
  instructions: string;
}

export interface RoleMaintenanceBlock {
  stale: boolean;
  stats: RoleKnowledgeStats;
  mandate: RoleMaintenanceMandate | null;
}

export interface RoleDetail extends RoleInfo {
  tools: string[] | null;
  prompt: string | null;
  lean_prompt: string | null;
  micro_prompt: string | null;
  knowledge: string | null;
  last_used: string | null;
  created_at: string | null;
  maintenance?: RoleMaintenanceBlock;
}

export interface RoleKnowledgeEntry {
  id: string;
  role_id: string;
  type: string;
  content: string;
  source_url: string | null;
  session_id: string | null;
  confidence: number | null;
  created_at: string | null;
  last_accessed: string | null;
  supersedes: string | null;
}

export interface RoleMaintenanceSummary {
  role_id: string;
  domain: string;
  stale: boolean;
  schedule: string;
  last_maintained: string | null;
  next_due: string | null;
  mandate_summary: string;
  knowledge_count: number;
}

export interface MaintenanceJob {
  job_id: string;
  role_id?: string;            // single-role jobs
  role_ids?: string[];         // bulk jobs (also populated for single)
  role_count?: number;         // bulk jobs
  status: "running" | "done" | "failed";
  started_at: string;
  finished_at: string | null;
  error: string | null;
  task_id?: string | null;     // spawned orchestrator task_id
  learned?: number;            // rows written during the task window
  orchestrator_phase?: string; // e.g. planning | active
  mandate?: unknown;           // legacy preview shape (not returned by run endpoint anymore)
}

// Shape returned by POST /api/roles/maintenance/run-all-stale
export interface MaintenanceBulkResponse {
  job_id: string;
  role_count: number;
  role_ids: string[];
  task_id: string | null;
  started_at: string;
}

// -- Tools (MCP catalog) --

export interface ToolInfo {
  name: string;
  namespace: string;
  description: string;
  input_schema: Record<string, unknown>;
  roles: string[];
}

export interface ToolCatalog {
  tools: ToolInfo[];
  namespaces: Record<string, string>;
  tool_roles: Record<string, string[]>;
  count: number;
}

// -- Flows (role-team templates) --

export interface RolePlacement {
  role_id: string;
  x: number;
  y: number;
}

export interface OrchestratorPlacement {
  x: number;
  y: number;
}

export interface FlowSummary {
  id: string;
  name: string;
  description: string;
  role_count: number;
  role_ids: string[];
  has_orchestrator?: boolean;
  created_at: string;
  updated_at: string;
}

export interface FlowDetail {
  id: string;
  name: string;
  description: string;
  orchestrator: OrchestratorPlacement;
  roles: RolePlacement[];
  created_at: string;
  updated_at: string;
}

// -- Recurring --

// What a scheduled thing DOES when it fires. Shared across both panels so a
// daemon job and a recurring def are directly comparable.
export type TaskKind = "script" | "llm" | "mixed" | "orchestrator";

// Model tier, in okuro.bridge's vocabulary. The orchestrator's config spells
// the top tier `strategic`; the API normalizes it to `quality` so one word
// means opus in both panels. "" = makes no direct model call.
export type TaskTier = "fast" | "standard" | "quality" | "";

export interface RecurringDef {
  id: string;
  title: string;
  role: string;
  roles: string[];
  schedule: string;
  status: string;
  last_run_at: string;
  next_run_at: string;
  next_run_seconds: number | null;
  run_count: number;
  adaptive: boolean;
  description_template: string;
  kind: TaskKind;
  tier: TaskTier;
}

export interface OutcomeEntry {
  run_id: string;
  outcome: "useful" | "empty" | "failed" | "unknown";
  summary: string;
  completed_at: string;
}

// -- Deliberation --

export type DeliberationStrategy = "parallel" | "sequential" | "debate";

export interface PanelRole {
  role_id: string;
  domain: string;
  description: string;
  why: string;
  similarity?: number;
  /** "matched" when similarity >= threshold; "fallback" otherwise.
   *  When EVERY panel item is "fallback" the engine has written a
   *  capability_gap.json — render the gap card instead of confirming. */
  match_type?: "matched" | "fallback";
}

export interface CapabilityGapPhase0Step {
  id: string;
  role: string;
  description: string;
  risk: string;
  complexity: string;
  artifact_name: string;
  dependencies: string[];
}

export interface CapabilityGap {
  task_id: string;
  kind: "missing_role" | "missing_tool" | "missing_data" | "missing_migration";
  summary: string;
  creator_roles: string[];
  slot_descriptions: string[];
  payload: {
    closest?: Array<{ id: string; domain?: string; similarity: number }>;
    task_description?: string;
  };
  phase0: CapabilityGapPhase0Step[];
  /** "pending_approval" → "accepted" → "phase0_complete" */
  status: "pending_approval" | "accepted" | "phase0_complete";
  created_at: string;
}

export interface PositionSummary {
  node_id: string;
  role: string;
  status: string;
  source: string;
  round: number;
  artifact: string;
  claim: string;
  reasoning?: string;
  risks?: string;
  recommendation?: string;
}

export interface DiscussionState {
  node_id: string;
  status: string;
  round: number;
  positions: PositionSummary[];
  assignments: AssignmentEntry[];
  user_statement: string;
  inherits: string;
}

export interface AssignmentEntry {
  role: string;
  action: "assign" | "acknowledge" | "dismiss";
  leads: string;
  reasoning: string;
}

export interface AuthorityEntry extends AssignmentEntry {
  round: number;
}

export interface GraphNode {
  id: string;
  type: "position" | "discussion" | "execution";
  role: string;
  status: string;
  round: number;
}

export interface GraphEdge {
  from: string;
  to: string;
}

// -- Onboarding --

export interface OnboardingStep {
  key: string;
  label: string;
  done: boolean;
  detail?: string | null;
  locked: boolean;
  skippable: boolean;
  enhanced_by: string[];
  requires_explicit_skip_consent?: boolean;
}

export interface OnboardingState {
  steps: OnboardingStep[];
  completed: boolean;
  completed_at?: string | null;
  current_step?: string | null;
  services_install?: Record<string, string> | null;
  services_install_error?: string | null;
  install_report_path?: string | null;
  install_report_error?: string | null;
}

export interface DetectionResult {
  os: Record<string, unknown>;
  shell: string;
  timezone: string;
  python: Record<string, unknown>;
  gpus: Array<Record<string, unknown>>;
  providers: Record<
    string,
    { installed: boolean; path?: string | null; logged_in?: boolean | null }
  >;
  tools: Record<string, boolean>;
}

/** One MCP consumer's merged install / auth / registration state.
 * Backend: GET /api/onboarding/consumers (ConsumerStatus). */
export interface ConsumerStatus {
  id: string;
  tool_id: string;
  name: string;
  kind: string;
  provider_id: string;
  installed: boolean | null;
  logged_in: boolean | null;
  /** Raw cli_probe state — lets the UI show "ineligible" distinct from "unknown". */
  auth_state: string | null;
  mcp_registered: boolean;
  transport: string | null;
  preferred_transport: string | null;
  transports: string[];
  stale: boolean;
  legacy_found: string[];
  config_exists: boolean;
  config_path: string | null;
  supported: boolean;
  /** Detected but not registered, or registered stale — the "register me" case. */
  actionable: boolean;
}

/** okuro entry found in a file canon does NOT write (shadow / disabled). */
export interface UnmanagedRegistration {
  path: string;
  scope: string;
  server: string;
  transport: string | null;
  note: string;
}

export interface ConsumersResponse {
  consumers: ConsumerStatus[];
  unmanaged: UnmanagedRegistration[];
  transport_expected: string;
  notes: string[];
}

export interface InferenceCli {
  tool_id: string;
  name: string;
  description: string;
  provider_id: string;
  recommended: boolean;
  docs_url?: string | null;
  binary: string;
  installed: boolean;
  path?: string | null;
  authenticated: boolean | null;
  install_preview: { linux?: string | null; darwin?: string | null };
  authenticate: {
    command?: string;
    instructions?: string;
    non_interactive_alt?: string;
  };
}

export interface CliActionResult {
  status:
    | "spawned"
    | "no_terminal"
    | "unsupported_os"
    | "unknown_tool"
    | "no_auth_command"
    | "no_command_for_os"
    | "missing_prereq";
  terminal?: string;
  command?: string;
  error?: string;
  /** When status="missing_prereq", the missing tool (e.g. "brew", "npm"). */
  prereq?: string;
  /** When status="missing_prereq", a human-readable install hint. */
  message?: string;
  install_command_hint?: string;
  alt_command_hint?: string;
}

export interface CliState {
  tool_id: string;
  installed: boolean;
  path?: string | null;
  authenticated: boolean | null;
  error?: string;
}

export interface DesignOption {
  id: string;
  name: string;
  description: string;
  source: string;
}

export interface QuestionnaireItem {
  id: number;
  question: string;
  option_a: string;
  option_b: string;
}

// -- UI Helpers --
//
// C10 \u2014 `STATUS_CONFIG` (TaskStatus \u2192 label + color) was deleted as part
// of the FE consumer-routing migration. The closed 5-token
// `ColorClass` enum supplied on snapshot.lifecycle / TaskSummary /
// phase / subtask is now the only FE-side switch. Adding a BE state no
// longer touches FE code. See docs/audit-2026-05-27/07-c10-fe-design.md.

export const SUBTASK_STATUS_CONFIG: Record<SubtaskStatus, { icon: string }> = {
  pending: { icon: "\u00B7" },
  running: { icon: "\u25B6" },
  done: { icon: "\u2713" },
  failed: { icon: "\u2717" },
  skipped: { icon: "\u2014" },
  waiting_approval: { icon: "\u26A1" },
  approved: { icon: "\u25B6" },
};

// -- Stack registry --

export type StackLifecycle = "trial" | "approved" | "deprecated" | "banned";
export type StackCardinality = "single" | "multi";
export type StackProfileStatus = "active" | "draft" | "archived";
export type StackProposalKind =
  | "new"
  | "promote"
  | "deprecate"
  | "ban"
  | "reinstate";
export type StackProposalOutcome = "pending" | "accepted" | "rejected";

export interface StackLayer {
  id: string;
  name: string;
  description: string;
  category: string;
  cardinality: StackCardinality;
  sort_order: number;
}

export interface StackEntryAlternative {
  id: string;
  name: string | null;
  reason_rejected: string | null;
}

export interface StackEntry {
  id: string;
  layer: string;
  name: string;
  version: string | null;
  status: StackLifecycle;
  rationale: string;
  use_when: string[];
  avoid_when: string[];
  docs_url: string | null;
  owner: string | null;
  replaces: string | null;
  last_reviewed: string | null;
  depends_on: string[];
  alternatives_considered: StackEntryAlternative[];
  created_at: string;
  updated_at: string;
}

export type StackProfileScope =
  | "frontend"
  | "backend"
  | "fullstack"
  | "agent"
  | "other";

export interface StackProfileSummary {
  name: string;
  label: string;
  description: string;
  scope: StackProfileScope;
  status: StackProfileStatus;
  entry_count: number;
  created_at: string;
  updated_at: string;
}

export interface StackProfileEntry {
  id: string;
  layer: string;
  name: string;
  version: string | null;
  status: StackLifecycle;
  role: "primary" | "secondary";
}

export interface StackResolvedEntry {
  id: string;
  name: string;
  version: string | null;
  status: StackLifecycle;
  layer: string;
  layer_name: string;
  role: "primary" | "secondary" | "transitive";
}

export interface StackResolvedProfile {
  name: string;
  label: string;
  description: string;
  status: StackProfileStatus;
  by_category: Record<string, StackResolvedEntry[]>;
  transitive: StackResolvedEntry[];
  projects: string[];
}

export interface StackValidation {
  ok: boolean;
  errors: string[];
  warnings: string[];
  stats: Record<string, number>;
}

export interface StackProposal {
  id: string;
  entry_id: string;
  kind: StackProposalKind;
  proposed_by: string | null;
  rationale: string;
  payload: Record<string, unknown>;
  outcome: StackProposalOutcome;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string;
}

export const STACK_STATUS_CONFIG: Record<
  StackLifecycle,
  { label: string; tone: string }
> = {
  trial:      { label: "TRIAL",      tone: "text-warning" },
  approved:   { label: "APPROVED",   tone: "text-success" },
  deprecated: { label: "DEPRECATED", tone: "text-tertiary" },
  banned:     { label: "BANNED",     tone: "text-error" },
};


// ── Brands + slot kinds ───────────────────────────────────────────────────

export type SlotKindRegistry =
  | "design_profile"
  | "stack_profile"
  | "principle_set";

export interface SlotKind {
  kind: string;
  registry: SlotKindRegistry;
  required: boolean;
  cardinality: "single" | "multi";
  scope_filter: StackProfileScope | null;
  sort_order: number;
  description: string;
  created_at: string;
}

export type BrandStatus = "active" | "draft" | "archived";

/** A single slot assignment — for `single` cardinality slots this is a scalar
 *  ref_id; for `multi` slots it's an array. */
export type BrandSlotAssignment = string | string[];

export interface Brand {
  id: string;
  name: string;
  description: string;
  status: BrandStatus;
  /** Default corner for the sticky logo module on a deck (tl/tr/bl/br). */
  logo_corner?: "tl" | "tr" | "bl" | "br";
  slots: Record<string, BrandSlotAssignment>;
  projects: string[];
  slot_count?: number;
  project_count?: number;
  created_at: string;
  updated_at: string;
}

export interface BrandSummary {
  id: string;
  name: string;
  description: string;
  status: BrandStatus;
  slot_count: number;
  project_count: number;
  created_at: string;
  updated_at: string;
}

/** A resolved slot — either the expanded data, or a {missing: true} marker
 *  if the ref couldn't be resolved. */
export interface ResolvedSlotItem<TData = unknown> {
  ref_id: string;
  data?: TData;
  missing?: boolean;
}

export interface BrandResolved {
  id: string;
  name: string;
  description: string;
  status: BrandStatus;
  /** Keyed by slot_kind. Single-cardinality → ResolvedSlotItem; multi → array. */
  slots: Record<
    string,
    ResolvedSlotItem | ResolvedSlotItem[] | { raw: unknown; unknown_kind: true }
  >;
  projects: string[];
}


// ── Principle sets ────────────────────────────────────────────────────────

export interface Principle {
  id: string;
  title: string;
  description: string;
  source: string;
  priority: number;
  active: boolean;
  examples: string[];
}

export interface PrincipleSetSummary {
  id: string;
  name: string;
  description: string;
  status: BrandStatus;
  member_count: number;
  created_at: string;
  updated_at: string;
}

export interface PrincipleSet {
  id: string;
  name: string;
  description: string;
  status: BrandStatus;
  principles: Principle[];
  created_at: string;
  updated_at: string;
}

// -- Preview (see-result button) --

export type PreviewLifecycle =
  | "idle"
  | "building"
  | "starting"
  | "ready"
  | "failed"
  | "stopped";

export interface PreviewState {
  state: PreviewLifecycle;
  slug: string;
  unit: string | null;
  port: number | null;
  url: string | null;
  started_at: string | null;
  recipe_present: boolean;
  last_error: string | null;
}

// -- Flow feedback (F3) — post-flow rating + forward note --

export type OutcomeClass =
  | "success"
  | "partial"
  | "failed"
  | "off_track"
  | "needs_rework";

export interface FlowFeedbackSubmit {
  usability: number; // 1..5
  outcome_class: OutcomeClass;
  comment?: string | null;
}

export interface FlowFeedback {
  task_id: string;
  usability: number;
  outcome_class: OutcomeClass;
  comment: string | null;
  resolved_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// -- Reviews ----------------------------------------------------------------

export type ReviewSeverity = "blocker" | "annoyance" | "idea" | "praise";

export type ReviewTargetType =
  | "task" | "subtask" | "note" | "person" | "project" | "thought"
  | "artifact" | "role" | "deck" | "kg" | "unresolved";

export interface ReviewSubmit {
  /** DECLARED identity of what was reviewed — never the URL. */
  surface_id: string;
  comment?: string | null;
  rating?: number | null;
  severity?: ReviewSeverity;
  target_type?: ReviewTargetType;
  target_id?: string | null;
  /** Context / evidence. Recorded, never used as identity. */
  route?: string | null;
  route_params?: Record<string, string> | null;
  viewport?: string | null;
  app_version?: string | null;
  /** Option D overlay evidence — a hint, not identity. */
  dom_hint?: string | null;
  screenshot_ref?: string | null;
  project?: string | null;
  /** Overrides the severity->todo gate in either direction. */
  make_todo?: boolean | null;
}

export interface Review extends ReviewSubmit {
  id: string;
  severity: ReviewSeverity;
  target_type: ReviewTargetType;
  todo_id: string | null;
  created_at: string;
}

export interface ReviewSurfaceEntry {
  surface_id: string;
  label?: string | null;
  route?: string | null;
}

export interface ReviewSurface extends ReviewSurfaceEntry {
  orphaned_at: string | null;
  review_count: number;
  last_review_at: string | null;
  avg_rating: number | null;
  blockers: number;
}
