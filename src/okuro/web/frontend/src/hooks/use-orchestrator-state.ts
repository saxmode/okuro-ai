import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useIsMutating, useMutationState } from "@tanstack/react-query";
import ReconnectingWebSocket from "reconnecting-websocket";
import { getToken } from "@/lib/api";

// -- Module-level signal for frontend-initiated mutations that aren't wired
// through React Query's mutation pool (the bulk of okuro's taskApi calls are
// hand-written async functions, not useMutation hooks). Any component that
// kicks off an async action should call `signalThinking(label)` to light up
// the OkuroThinker widget immediately, and `signalIdle()` when done. Both
// success and failure paths should call `signalIdle` so the widget doesn't
// stick. Wrap in a try/finally or use the `withThinker(label, fn)` helper.

interface FrontendSignal {
  active: boolean;
  label: string;
  at: number;
}

let _signal: FrontendSignal = { active: false, label: "", at: 0 };
const _listeners = new Set<() => void>();

function subscribe(fn: () => void) {
  _listeners.add(fn);
  return () => {
    _listeners.delete(fn);
  };
}

function emit() {
  for (const fn of _listeners) fn();
}

export function signalThinking(label: string) {
  _signal = { active: true, label, at: Date.now() };
  emit();
}

export function signalIdle() {
  _signal = { active: false, label: "", at: Date.now() };
  emit();
}

export async function withThinker<T>(label: string, fn: () => Promise<T>): Promise<T> {
  signalThinking(label);
  try {
    return await fn();
  } finally {
    signalIdle();
  }
}

function useFrontendSignal(): FrontendSignal {
  return useSyncExternalStore(subscribe, () => _signal, () => _signal);
}

/**
 * Orchestrator-state hook for the OkuroThinker widget.
 *
 * Sources of truth (merged):
 *   1. Backend: /ws/activity messages with event.type === "orchestrator_state"
 *      — the orchestrator subprocess writing `{type:"orchestrator_state", …}`
 *      to task log.jsonl, forwarded via handle_log_change in api/main.py.
 *   2. Frontend: TanStack Query mutation pool — any pending mutation tagged
 *      with `meta: { okuroLabel: "…" }` bubbles its label up so the widget
 *      animates instantly on click, before the server has had a chance to
 *      emit an orchestrator_state event.
 *
 * When both are active, the backend label wins — the orchestrator knows more
 * about its own state than the frontend could guess.
 */

export type OrchestratorState =
  | "idle"
  | "thinking"
  | "planning"
  | "running"
  | "generating"
  | "waiting"
  | "complete"
  | "error";

export interface OrchestratorSnapshot {
  state: OrchestratorState;
  label: string;
  taskId?: string | null;
  connected: boolean;
}

const IDLE: Omit<OrchestratorSnapshot, "connected"> = {
  state: "idle",
  label: "",
  taskId: null,
};

// "complete" is terminal but should auto-decay to idle after this many ms so
// the widget doesn't get stuck showing "Task complete" forever.
const COMPLETE_DECAY_MS = 4_000;

// Minimum time the widget stays visible once shown. Protects against sub-
// perceptible flashes when the orchestrator's work is faster than a human
// can see (e.g. LLM calls that return in <200 ms from cache). Without this
// the user's complaint "button did nothing" persists even though the system
// did respond — their eye never caught the transition.
const MIN_VISIBLE_MS = 800;

interface UseOrchestratorStateOptions {
  /**
   * When set, the hook only reflects backend events whose task_id matches.
   * Use for the inline thinker variants embedded in a specific task's
   * pipeline view — events from other tasks get filtered out so the
   * widget doesn't jitter when multiple tasks are active.
   *
   * The module-level frontend signal (signalThinking) is always applied
   * regardless of taskId, because a click from the current page is about
   * the current page.
   */
  taskId?: string;
  /**
   * Lifecycle authority. When the caller knows the task's authoritative
   * lifecycle has settled (snapshot.lifecycle terminal — done / failed /
   * blocked / halted / waiting_user), pass true here.
   *
   * The chip trusts WS `/ws/activity` deltas only, and a busy label
   * ("Generating 3 next steps…") clears ONLY on the exact `idle` frame —
   * if a long-lived tab misses that single frame it sticks forever. When
   * `terminalLifecycle` is true the hook reconciles ANY busy backend state
   * (generating/running/thinking/planning), not just `complete`, to idle —
   * and the merge below never surfaces a busy backend label. The engine
   * has stopped; the chip must reflect that no matter which WS frame the
   * tab saw last.
   */
  terminalLifecycle?: boolean;
}

export function useOrchestratorState(
  { taskId, terminalLifecycle = false }: UseOrchestratorStateOptions = {},
): OrchestratorSnapshot {
  const [backend, setBackend] = useState<Omit<OrchestratorSnapshot, "connected">>(IDLE);
  const [connected, setConnected] = useState(false);
  const decayTimer = useRef<number | null>(null);
  const visibleSinceRef = useRef<number | null>(null);
  const minDwellTimer = useRef<number | null>(null);
  // Keep taskId in a ref so the effect doesn't churn the WS every time
  // a parent re-renders with the same taskId literal.
  const taskIdRef = useRef(taskId);
  taskIdRef.current = taskId;
  // Per-task seq high-water mark. orchestrator_state frames carry a
  // monotonic per-task seq; we drop any frame with seq <= the last applied
  // for that task so a replayed/out-of-order frame (e.g. a stale "running"
  // pushed on reconnect after we already saw "idle") can't re-light the
  // pill (Phase 0).
  const lastSeqByTask = useRef<Map<string, number>>(new Map());

  // --- WebSocket listener on /ws/activity, filtered to orchestrator_state.
  useEffect(() => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const urlProvider = async () => {
      const token = await getToken();
      return `${proto}//${location.host}/ws/activity?token=${encodeURIComponent(token)}`;
    };
    const ws = new ReconnectingWebSocket(urlProvider, [], {
      connectionTimeout: 3000,
      maxReconnectionDelay: 10_000,
    });

    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);

    ws.onmessage = (ev) => {
      let msg: {
        type?: string;
        task_id?: string | null;
        seq?: number;
        event?: {
          type?: string;
          state?: OrchestratorState;
          label?: string;
          task_id?: string | null;
          seq?: number;
        };
      };
      try {
        msg = JSON.parse(ev.data as string);
      } catch {
        return;
      }
      if (msg.type !== "agent_activity" || msg.event?.type !== "orchestrator_state") return;

      const state = (msg.event?.state ?? "idle") as OrchestratorState;
      const label = msg.event?.label ?? "";
      const eventTaskId = msg.event?.task_id ?? msg.task_id ?? null;
      const seq = msg.event?.seq ?? msg.seq ?? null;

      // Task-scoped filter — only apply events for our taskId when set.
      const scope = taskIdRef.current;
      if (scope && eventTaskId && eventTaskId !== scope) return;

      // Seq-guard: drop replayed / out-of-order frames per task so a stale
      // "running" can't override a fresher "idle" we already applied.
      if (typeof seq === "number" && eventTaskId) {
        const last = lastSeqByTask.current.get(eventTaskId) ?? 0;
        if (seq <= last) return;
        lastSeqByTask.current.set(eventTaskId, seq);
      }

      // Note: a debug `console.log("[okuro-thinker] ws event", …)` lived
      // here briefly; removed once end-to-end delivery was confirmed.

      // Transitioning into a visible (non-idle) state — record when it
      // started so MIN_VISIBLE_MS can guarantee dwell time if the backend
      // flips to idle faster than a human can perceive.
      const becomingVisible = state !== "idle" && visibleSinceRef.current == null;
      if (becomingVisible) {
        visibleSinceRef.current = Date.now();
      }

      const applyIdle = () => {
        setBackend(IDLE);
        visibleSinceRef.current = null;
      };

      // Clear any pending min-dwell timer from a previous transition.
      if (minDwellTimer.current) {
        window.clearTimeout(minDwellTimer.current);
        minDwellTimer.current = null;
      }

      if (state === "idle" && visibleSinceRef.current != null) {
        const elapsed = Date.now() - visibleSinceRef.current;
        const remaining = MIN_VISIBLE_MS - elapsed;
        if (remaining > 0) {
          // Widget has been visible for less than MIN_VISIBLE_MS — hold
          // the *current* visible state until the dwell budget is spent,
          // then transition to idle.
          minDwellTimer.current = window.setTimeout(applyIdle, remaining);
          return;
        }
        applyIdle();
      } else {
        setBackend({ state, label, taskId: eventTaskId });
      }

      // Auto-decay the terminal "complete" state so the widget doesn't
      // freeze at the end of a task.
      if (decayTimer.current) window.clearTimeout(decayTimer.current);
      if (state === "complete") {
        decayTimer.current = window.setTimeout(() => {
          applyIdle();
        }, COMPLETE_DECAY_MS);
      }
    };

    return () => {
      ws.close();
      setConnected(false);
      if (decayTimer.current) window.clearTimeout(decayTimer.current);
      if (minDwellTimer.current) window.clearTimeout(minDwellTimer.current);
    };
  }, []);

  // --- Stopgap clear-path: reconcile any stuck busy state to idle once the
  // authoritative lifecycle has settled. The WS-only path clears a busy
  // label exclusively on the `idle` frame; a tab that missed that frame
  // would stick on "Generating…" forever. When the caller reports a
  // terminal lifecycle, any non-idle/non-complete backend state is stale —
  // drop it. (`complete` keeps its existing COMPLETE_DECAY_MS decay above so
  // the "Done" pulse still shows briefly.)
  useEffect(() => {
    if (!terminalLifecycle) return;
    if (backend.state === "idle" || backend.state === "complete") return;
    setBackend(IDLE);
    visibleSinceRef.current = null;
  }, [terminalLifecycle, backend.state]);

  // --- Signal sources for instant click feedback.
  // (1) TanStack Query mutation pool — any useMutation() that sets
  //     meta.okuroLabel bubbles its label up. Near-zero wiring for
  //     future mutations.
  // (2) Module-level signal — hand-written async handlers call
  //     signalThinking(label) / signalIdle() or withThinker(). Used by
  //     task-detail.tsx's runMutation() helper today.
  const mutatingCount = useIsMutating();
  const labels = useMutationState<string>({
    filters: { status: "pending" },
    select: (m) => {
      const mutationMeta = (m.options as { meta?: { okuroLabel?: string } } | undefined)?.meta;
      return mutationMeta?.okuroLabel ?? "";
    },
  });
  const queryMutationLabel = labels.find((l) => !!l) ?? "";
  const queryMutationBusy = mutatingCount > 0;

  const moduleSignal = useFrontendSignal();

  // --- Lifecycle authority: a settled task never shows a busy chip.
  // Overrides every busy source (stale backend state that the reconcile
  // effect hasn't flushed yet on this render, plus frontend click signals)
  // so the chip can't animate for an engine that has stopped. The `complete`
  // pulse is allowed through — it's the terminal "Done" affordance, not a
  // busy state — and decays on its own timer.
  if (terminalLifecycle && backend.state !== "complete") {
    return { ...IDLE, connected };
  }

  // --- Merge: backend wins when non-idle; otherwise surface frontend signals.
  if (backend.state !== "idle" && backend.state !== "complete") {
    return { ...backend, connected };
  }
  if (moduleSignal.active) {
    return {
      state: "thinking",
      label: moduleSignal.label || "Working…",
      taskId: null,
      connected,
    };
  }
  if (queryMutationBusy) {
    return {
      state: "thinking",
      label: queryMutationLabel || "Working…",
      taskId: null,
      connected,
    };
  }
  return { ...backend, connected };
}
