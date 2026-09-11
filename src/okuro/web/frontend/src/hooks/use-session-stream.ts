/**
 * EventSource consumer for ``/api/sessions/{id}/events``.
 *
 * The hook owns ONE EventSource per (sessionId, token) pair, normalises
 * frames into a typed ``InlineEvent``, and folds them through the
 * pure reducer in ``lib/inline-reducer``. Reconnect logic:
 *
 *   - EventSource auto-reconnects with the standard ``Last-Event-ID``
 *     header. The backend (Starlette) does not key its replay on that
 *     header — it uses the ``since_seq`` query param. So when we
 *     observe ``onerror``+readyState=CLOSED on a non-terminal session,
 *     we manually rebuild the URL with the latest ``last_seq`` and
 *     open a new EventSource.
 *   - On terminal status (``done | cancelled | error``) we close
 *     deliberately and do NOT reconnect.
 *
 * Heartbeats arrive as SSE comments (``: ping``) which EventSource
 * silently drops — no client work required.
 *
 * The hook intentionally does not own POST mutations; those live on
 * the component side via ``sendInput`` / ``approveCall`` so React Query
 * mutation lifecycles stay co-located with the buttons that fire them.
 */
import { useEffect, useReducer, useRef } from "react";
import type { InlineEvent, InlineSessionView } from "@/types/inline";
import {
  applyEvent,
  initialSessionView,
  optimisticUserMessage,
} from "@/lib/inline-reducer";

type StreamAction =
  | { kind: "event"; event: InlineEvent }
  | { kind: "connected"; connected: boolean }
  | {
      kind: "optimistic_user";
      message_id: string;
      text: string;
    }
  | { kind: "reset" };

function reducer(state: InlineSessionView, action: StreamAction): InlineSessionView {
  switch (action.kind) {
    case "event":
      return applyEvent(state, action.event);
    case "connected":
      return state.connected === action.connected
        ? state
        : { ...state, connected: action.connected };
    case "optimistic_user":
      return optimisticUserMessage(state, action.message_id, action.text);
    case "reset":
      return initialSessionView;
    default:
      return state;
  }
}

const TERMINAL_STATES = new Set(["done", "cancelled", "error"]);

interface UseSessionStreamOpts {
  /** Disable opening the EventSource — used by tests + storybook stubs. */
  enabled?: boolean;
  /**
   * EventSource factory injection point — lets tests stub the network
   * without monkey-patching globals. Defaults to ``window.EventSource``.
   */
  eventSourceFactory?: (url: string) => EventSource;
}

interface UseSessionStreamResult {
  view: InlineSessionView;
  /** Local push for optimistic user bubbles before the server echo lands. */
  pushOptimisticUser: (message_id: string, text: string) => void;
  /** Force a reset — used after a manual reconnect or session swap. */
  reset: () => void;
}

export function useSessionStream(
  sessionId: string | undefined,
  token: string | undefined,
  opts: UseSessionStreamOpts = {},
): UseSessionStreamResult {
  const [view, dispatch] = useReducer(reducer, initialSessionView);
  // Mirror last_seq into a ref so reconnect can read the latest value
  // without re-running the effect on every event.
  const lastSeqRef = useRef(0);
  lastSeqRef.current = view.last_seq;

  const stateRef = useRef(view.state);
  stateRef.current = view.state;

  const { enabled = true, eventSourceFactory } = opts;

  useEffect(() => {
    if (!enabled) return;
    if (!sessionId || !token) return;

    let es: EventSource | null = null;
    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let backoffMs = 500;

    const open = () => {
      if (cancelled) return;
      const since = lastSeqRef.current;
      const qs = new URLSearchParams({
        since_seq: String(since),
        t: token,
      });
      const url = `/api/sessions/${encodeURIComponent(sessionId)}/events?${qs.toString()}`;
      const factory =
        eventSourceFactory ?? ((u: string) => new EventSource(u));
      es = factory(url);

      es.onopen = () => {
        if (cancelled) return;
        backoffMs = 500;
        dispatch({ kind: "connected", connected: true });
      };

      const onMessage = (raw: MessageEvent) => {
        if (cancelled) return;
        if (typeof raw.data !== "string" || !raw.data) return;
        let parsed: unknown;
        try {
          parsed = JSON.parse(raw.data);
        } catch {
          // Heartbeats are comments and never reach onmessage; any
          // unparseable payload is a server bug — drop quietly.
          return;
        }
        if (!parsed || typeof parsed !== "object") return;
        const candidate = parsed as { type?: unknown };
        if (typeof candidate.type !== "string") return;
        dispatch({ kind: "event", event: parsed as InlineEvent });
      };

      // The backend uses ``event: <type>`` for every frame (e.g.
      // ``event: okuro``, ``event: end``, ``event: status``). We
      // listen to the generic ``message`` channel for everything that
      // carries a JSON payload, plus the explicit ``end`` channel for
      // the close marker so we know to stop reconnecting.
      es.onmessage = onMessage;
      // Backend session_sse formats frames as `event: <evt.type>` —
      // listen to every contract event type explicitly so they don't
      // fall through into 'message'.
      for (const t of [
        "okuro",
        "status",
        "message_start",
        "token",
        "message_stop",
        "user_echo",
        "tool_call_start",
        "tool_approval_required",
        "tool_call_done",
        "usage",
        "error",
      ]) {
        es.addEventListener(t, onMessage as EventListener);
      }
      es.addEventListener("end", () => {
        // Server closed cleanly — terminal status already applied.
        cancelled = true;
        es?.close();
        dispatch({ kind: "connected", connected: false });
      });

      es.onerror = () => {
        if (cancelled) return;
        dispatch({ kind: "connected", connected: false });
        // EventSource keeps trying on its own, but the backend's
        // replay-from-zero on a fresh connection will re-emit history
        // (already deduped by seq in the reducer). Close + manually
        // reopen with ``since_seq`` set so the replay window is small.
        es?.close();
        es = null;
        if (TERMINAL_STATES.has(stateRef.current)) {
          cancelled = true;
          return;
        }
        // Exponential backoff capped at 8s.
        reconnectTimer = setTimeout(() => {
          backoffMs = Math.min(backoffMs * 2, 8_000);
          open();
        }, backoffMs);
      };
    };

    open();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      es?.close();
      es = null;
      dispatch({ kind: "connected", connected: false });
    };
    // eventSourceFactory is intentionally read once per (id,token) pair
    // — re-opening on factory identity churn would thrash the connection.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, token, enabled]);

  return {
    view,
    pushOptimisticUser: (message_id, text) =>
      dispatch({ kind: "optimistic_user", message_id, text }),
    reset: () => dispatch({ kind: "reset" }),
  };
}
