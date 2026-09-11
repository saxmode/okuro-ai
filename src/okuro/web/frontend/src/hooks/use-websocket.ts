import { useEffect, useRef, useCallback, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import ReconnectingWebSocket from "reconnecting-websocket";
import { getToken } from "@/lib/api";
import { pickFresherSnapshot, snapshotSeq } from "@/lib/snapshot-seq";
import { activityContradictsSnapshot } from "@/lib/snapshot-witness";
import type { TaskSnapshot } from "@/types/api";
import type {
  WSEvent,
  WSLogEvent,
  WSAgentActivityEvent,
  WSPreviewStateChangedEvent,
} from "@/types/ws";
import type { LogEntry, ActivityEvent } from "@/types/api";

/** Collapse a burst of contradicting activity rows into one resync. */
const WITNESS_DEBOUNCE_MS = 400;
/** Floor between witness-driven resyncs, so a lagging BE is not hammered. */
const WITNESS_COOLDOWN_MS = 5_000;

interface UseWebSocketOptions {
  taskId?: string;
  onLog?: (entry: LogEntry) => void;
  onActivity?: (event: ActivityEvent) => void;
}

/**
 * Every task-scoped cache the task page reads (ROCK-SOLID v5 P3.2).
 *
 * ONE list, consumed by BOTH the `state_change` handler and the reconnect
 * handler. They were hand-maintained separately and had already drifted in
 * both directions: `proposedPanel` / `capabilityGap` were invalidated only on
 * reconnect, `gates` / `positions` / `discussion` only on state_change. A
 * cache in the first group could not refresh on a HEALTHY socket at all —
 * measured at the source, `proposedPanel` carries no refetchInterval, so its
 * only refresh was a reconnect. The plan's phrase for it, "NEVER refreshes on
 * a healthy socket", was literal.
 *
 * Invalidation refetches only MOUNTED queries, so a task page pays for the
 * panels it is actually showing, not for all twelve.
 */
const TASK_QUERY_KEYS = [
  "task",
  "taskState",
  "taskSnapshot",
  "activity",
  "graph",
  "positions",
  "discussion",
  // ["gates"] was a poll-only island: use-gates.ts polls every 3 s and nothing
  // invalidated it, so option/state text inside an OPEN GatePanel lagged up to
  // 3 s however healthy the socket was. This does NOT govern when the panel
  // APPEARS — GatePanel mounts only once a decision_gate blocker is visible,
  // and that blocker comes from the snapshot push.
  "gates",
  // P3.2 additions. Poll intervals measured at the source: review 5 s,
  // capabilityGap 5 s, authorityMap 10 s, proposedPanel none at all.
  "review",
  "proposedPanel",
  "capabilityGap",
  "authorityMap",
  // P3.3 — Stream C. Paired with the engine's `delivery_sent` event;
  // without this the deliveries tab is a mount-only read again.
  "deliveries",
  // P3.9 — the review timeline. Push-only (no refetchInterval), so this
  // invalidation IS its freshness.
  "reviewRounds",
] as const;

/** Caches not scoped to one task — any task's change can move them. */
const GLOBAL_QUERY_KEYS = ["tasks", "systemStatus"] as const;

/**
 * WebSocket hook for real-time task events.
 *
 * Connects to ws://host:port/ws/{taskId} when taskId is provided.
 * Invalidates TanStack Query caches on state_change events.
 * Forwards log and agent_activity events via callbacks.
 */
export function useWebSocket({ taskId, onLog, onActivity }: UseWebSocketOptions = {}) {
  const queryClient = useQueryClient();
  const wsRef = useRef<ReconnectingWebSocket | null>(null);
  const [connected, setConnected] = useState(false);

  // Stable refs for callbacks
  const onLogRef = useRef(onLog);
  onLogRef.current = onLog;
  const onActivityRef = useRef(onActivity);
  onActivityRef.current = onActivity;

  // Cross-channel staleness witness — see lib/snapshot-witness.ts. Debounced
  // so a burst of activity rows costs one refetch, and rate-limited so a
  // genuinely-behind backend cannot be hammered while it catches up.
  const witnessTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastResyncAtRef = useRef(0);

  const connect = useCallback(() => {
    if (!taskId) return;

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    // URL provider re-runs on every reconnect so a rotated bearer works.
    const urlProvider = async () => {
      const token = await getToken();
      return `${proto}//${location.host}/ws?token=${encodeURIComponent(token)}`;
    };

    const ws = new ReconnectingWebSocket(urlProvider, [], {
      maxRetries: 5,
      connectionTimeout: 3000,
      maxReconnectionDelay: 10000,
    });

    ws.onopen = () => {
      setConnected(true);
      // Subscribe to task events. Carry the highest snapshot seq we've
      // already applied so the server replays only fresher state on
      // reconnect (replay-from-cursor) — no redundant or backwards push.
      const cachedSnap = queryClient.getQueryData<TaskSnapshot>([
        "taskSnapshot",
        taskId,
      ]);
      ws.send(
        JSON.stringify({
          type: "subscribe",
          task_id: taskId,
          last_seq: snapshotSeq(cachedSnap),
        }),
      );
      // Every reconnect = a window in which state_change / log /
      // agent_activity messages may have fired and been dropped (server
      // restart, network blip, laptop sleep). Invalidate the polled
      // caches so the UI catches up to current truth; we deliberately
      // do this AFTER subscribe so any race between the resubscribe
      // and the refetch resolves toward the fresher data.
      // Same list as state_change — a reconnect is the moment the client is
      // MOST likely to be stale, so it cannot afford a narrower set than the
      // steady-state path. It previously had one.
      for (const key of TASK_QUERY_KEYS) {
        queryClient.invalidateQueries({ queryKey: [key, taskId] });
      }
      for (const key of GLOBAL_QUERY_KEYS) {
        queryClient.invalidateQueries({ queryKey: [key] });
      }
    };
    ws.onclose = () => setConnected(false);

    ws.onmessage = (ev) => {
      let event: WSEvent;
      try {
        event = JSON.parse(ev.data as string) as WSEvent;
      } catch (err) {
        // Phase 5 — a malformed frame previously vanished silently, so a
        // server-side serialization bug looked like "no events". Surface it.
        console.warn(
          "[ws] dropped unparseable frame",
          { taskId, raw: String(ev.data).slice(0, 200) },
          err,
        );
        return;
      }

      // Safety: even though the backend splits pools (state vs activity),
      // drop any task-scoped event whose task_id doesn't match ours. Prevents
      // cross-task contamination if a backend regression reintroduces the leak.
      const eventTaskId = (event as { task_id?: string }).task_id;

      switch (event.type) {
        case "log": {
          const logEvent = event as WSLogEvent;
          if (logEvent.task_id && logEvent.task_id !== taskId) break;
          onLogRef.current?.({
            timestamp: logEvent.timestamp,
            type: logEvent.entry.type as string ?? "unknown",
            data: logEvent.entry,
          });
          break;
        }

        case "state_change":
          // Invalidate task queries so UI refetches. State_change is cross-task
          // by design (sidebar needs it); only invalidate the current task's
          // detail caches when the event is for this task.
          if (!eventTaskId || eventTaskId === taskId) {
            for (const key of TASK_QUERY_KEYS) {
              queryClient.invalidateQueries({ queryKey: [key, taskId] });
            }
          }
          for (const key of GLOBAL_QUERY_KEYS) {
            queryClient.invalidateQueries({ queryKey: [key] });
          }
          break;

        case "snapshot": {
          // Step 5 — BE pushes the freshly-rendered snapshot whenever any
          // allowlisted event lands. FE writes it straight into the cache
          // so the UI updates without a /snapshot round-trip. Polling at
          // 8s stays as a safety net for first paint + reconnect resync.
          const evt = event as { task_id?: string; snapshot?: TaskSnapshot };
          if (evt.task_id !== taskId) break;
          if (!evt.snapshot) break;
          // Seq-guard: keep whichever of cache vs push is fresher so an
          // out-of-order push (or a replayed older snapshot on reconnect)
          // can't regress the UI (Phase 0).
          queryClient.setQueryData<TaskSnapshot>(
            ["taskSnapshot", taskId],
            (prev) => pickFresherSnapshot(prev, evt.snapshot) as TaskSnapshot,
          );
          break;
        }

        case "agent_activity": {
          const actEvent = event as WSAgentActivityEvent;
          if (actEvent.task_id && actEvent.task_id !== taskId) break;
          onActivityRef.current?.(actEvent.event);

          // Staleness witness. Activity for a subtask the cached snapshot
          // still calls pending PROVES a snapshot push went missing — the
          // one failure the push-only path has no recovery from, because
          // the socket never dropped so nothing resyncs and the poll is
          // paused. Resync once instead of re-enabling polling.
          const cached = queryClient.getQueryData<TaskSnapshot>([
            "taskSnapshot",
            taskId,
          ]);
          if (activityContradictsSnapshot(cached, actEvent.event)) {
            if (witnessTimerRef.current === null) {
              witnessTimerRef.current = setTimeout(() => {
                witnessTimerRef.current = null;
                const now = Date.now();
                if (now - lastResyncAtRef.current < WITNESS_COOLDOWN_MS) return;
                lastResyncAtRef.current = now;
                queryClient.invalidateQueries({
                  queryKey: ["taskSnapshot", taskId],
                });
              }, WITNESS_DEBOUNCE_MS);
            }
          }
          break;
        }

        case "preview_state_changed": {
          // Push the launcher's new state straight into the cache so the
          // PreviewButton flips without waiting for the next /status
          // poll. Background polling stays as a safety net for clients
          // that disconnect briefly.
          const previewEvent = event as WSPreviewStateChangedEvent;
          if (previewEvent.task_id !== taskId) break;
          queryClient.setQueryData(
            ["preview", taskId, "status"],
            previewEvent.preview,
          );
          break;
        }

        // Protocol envelopes that need no client action.
        case "connected":
        case "subscribed":
        case "pong":
          break;

        default:
          // Phase 5 — forward-compat. A newer engine event type previously
          // fell through this switch and disappeared with no trace, so a
          // shipped-but-unhandled type read as "the UI is broken / stale".
          // Log it (visible signal that the FE needs a handler) instead of
          // silently dropping it.
          console.warn(
            "[ws] unhandled envelope type",
            (event as { type?: string }).type,
            { taskId },
          );
          break;
      }
    };

    wsRef.current = ws;
  }, [taskId, queryClient]);

  useEffect(() => {
    connect();
    return () => {
      wsRef.current?.close();
      wsRef.current = null;
      if (witnessTimerRef.current !== null) {
        clearTimeout(witnessTimerRef.current);
        witnessTimerRef.current = null;
      }
      setConnected(false);
    };
  }, [connect]);

  return { connected };
}
