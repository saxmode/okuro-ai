/**
 * Unified inline-session event types — mirrors the SSE envelope shape
 * defined in docs/research/cli-streaming-contract.md §2.1.
 *
 * Every server event carries ``type``, ``session_id``, ``seq`` (monotonic
 * int) and ``ts`` (ISO-8601). The JSON ``type`` discriminator is the
 * wire-level union tag; the SSE ``event:`` channel is fixed to
 * ``"okuro"`` so we ignore it on the client.
 *
 * Keep this file free of React imports — the hook + reducer is the only
 * consumer of these types; widening into other modules would couple
 * unrelated layers to the streaming contract.
 */
export type SessionState =
  | "running"
  | "paused"
  | "done"
  | "cancelled"
  | "error";

export type ToolTier =
  | "read"
  | "write"
  | "shell-safe"
  | "shell-write"
  | "secrets"
  | "destructive";

export type ToolOutcome =
  | "auto"
  | "approved"
  | "denied"
  | "edited"
  | "timeout"
  | "error";

export type ApprovalDecision = "approve" | "deny" | "edit";

interface BaseEvent {
  session_id: string;
  seq: number;
  ts: string;
}

export interface StatusEvent extends BaseEvent {
  type: "status";
  state: SessionState;
  detail?: string;
}

export interface MessageStartEvent extends BaseEvent {
  type: "message_start";
  message_id: string;
  role: "assistant" | "user";
}

export interface TokenEvent extends BaseEvent {
  type: "token";
  message_id: string;
  delta: string;
}

export interface MessageStopEvent extends BaseEvent {
  type: "message_stop";
  message_id: string;
  text: string;
  stop_reason?: string;
}

export interface UserEchoEvent extends BaseEvent {
  type: "user_echo";
  message_id: string;
  text: string;
}

export interface ToolCallStartEvent extends BaseEvent {
  type: "tool_call_start";
  call_id: string;
  tool_name: string;
  args: Record<string, unknown>;
  tier: ToolTier;
  mcp_server?: string;
}

export interface ToolApprovalRequiredEvent extends BaseEvent {
  type: "tool_approval_required";
  call_id: string;
  tool_name: string;
  args: Record<string, unknown>;
  tier: ToolTier;
  expires_at?: string;
  default_action?: "deny" | "approve";
  diff?: string;
}

export interface ToolCallDoneEvent extends BaseEvent {
  type: "tool_call_done";
  call_id: string;
  outcome: ToolOutcome;
  result?: unknown;
  error?: string;
  duration_ms?: number;
}

export interface UsageEvent extends BaseEvent {
  type: "usage";
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number;
}

export interface ErrorEvent extends BaseEvent {
  type: "error";
  code: string;
  message: string;
  recoverable?: boolean;
}

export type InlineEvent =
  | StatusEvent
  | MessageStartEvent
  | TokenEvent
  | MessageStopEvent
  | UserEchoEvent
  | ToolCallStartEvent
  | ToolApprovalRequiredEvent
  | ToolCallDoneEvent
  | UsageEvent
  | ErrorEvent;

// ---------------------------------------------------------------------------
// Reduced UI state — what components render.
// ---------------------------------------------------------------------------

export interface TranscriptMessage {
  message_id: string;
  role: "assistant" | "user";
  text: string;
  /** True while the assistant is still streaming tokens into ``text``. */
  streaming: boolean;
  /** Tool calls anchored to this message, in arrival order. */
  tool_call_ids: string[];
  ts: string;
}

export type ToolCardState =
  | "pending-approval"
  | "running"
  | "done"
  | "denied"
  | "error";

export interface ToolCard {
  call_id: string;
  tool_name: string;
  tier: ToolTier;
  args: Record<string, unknown>;
  state: ToolCardState;
  /** Set once ``tool_call_done`` arrives — drives card colour. */
  outcome?: ToolOutcome;
  result?: unknown;
  error?: string;
  duration_ms?: number;
  /** Only set on tool_approval_required — drives approve/deny UI. */
  approval_required: boolean;
  approval_expires_at?: string;
  started_at: string;
}

export interface SessionUsage {
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}

export interface InlineSessionView {
  /** Last seq we've seen — passed back as ``since_seq`` on reconnect. */
  last_seq: number;
  state: SessionState;
  /** Free-text from the last ``status`` event ``detail`` field. */
  state_detail?: string;
  messages: TranscriptMessage[];
  /** Tool cards keyed by call_id, in arrival order. */
  tool_cards: ToolCard[];
  usage: SessionUsage;
  /** Last error event (non-fatal — for the banner). */
  last_error?: { code: string; message: string };
  /** True while the EventSource is open and reading. */
  connected: boolean;
}
