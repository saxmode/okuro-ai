/**
 * WebSocket event types for okuro orchestrator.
 * Server: ws://host:13333/ws and ws://host:13333/ws/{task_id}
 */

import type { ActivityEvent, PreviewState } from "./api";

export interface WSConnectedEvent {
  type: "connected";
  timestamp: string;
}

export interface WSSubscribedEvent {
  type: "subscribed";
  task_id: string;
  channels: string[];
}

export interface WSLogEvent {
  type: "log";
  task_id: string;
  entry: Record<string, unknown>;
  timestamp: string;
}

export interface WSStateChangeEvent {
  type: "state_change";
  task_id: string;
  event_type: string;
  timestamp: string;
}

export interface WSAgentActivityEvent {
  type: "agent_activity";
  // null when the event is global (pulse only) — e.g. telemetry tail or
  // broadcast_agent_event from bridge/sense. /ws clients ignore those.
  task_id: string | null;
  event: ActivityEvent;
}

export interface WSPongEvent {
  type: "pong";
  timestamp: string;
}

/**
 * Pushed by the preview launcher on every lifecycle transition
 * (building → starting → ready/failed). Lets the SPA skip the 1.5 s
 * status poll while a build is in flight — the button flips the
 * moment the launcher writes the new state.
 */
export interface WSPreviewStateChangedEvent {
  type: "preview_state_changed";
  task_id: string;
  preview: PreviewState;
}

export interface WSSnapshotEvent {
  type: "snapshot";
  task_id: string;
  snapshot: import("./api").TaskSnapshot;
  trigger?: string;
  timestamp?: string;
}

export type WSEvent =
  | WSConnectedEvent
  | WSSubscribedEvent
  | WSLogEvent
  | WSStateChangeEvent
  | WSAgentActivityEvent
  | WSPongEvent
  | WSPreviewStateChangedEvent
  | WSSnapshotEvent;
