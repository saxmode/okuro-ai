import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import ReconnectingWebSocket from "reconnecting-websocket";
import { api, getToken } from "@/lib/api";
import { usePaneInterval } from "@/lib/pane-active";
import type { PulseActivity } from "@/lib/pulse-engine";

export interface ActivityStreamEntry {
  ts: string;
  tool?: string;
  preview?: string;
}

interface DoctorCheck {
  name: string;
  status: "ok" | "warn" | "fail";
  message: string;
  fix: string | null;
}

interface DoctorResult {
  checks: DoctorCheck[];
  passed: number;
  warned: number;
  failed: number;
}

export interface LiveAgent {
  id: string;
  provider: string;
  host: string;
  pid: number | null;
  transport: string;
  started_at: string;
  last_heartbeat_at: string;
  current_task_hint: string | null;
  current_project: string | null;
  current_session_id: string | null;
  total_sessions: number;
}

const MAX_STREAM_LEN = 24;
const CALL_WINDOW_MS = 60_000;

// Intensity formula constants. See pulse audit spec.
// liveTerm = 1 - exp(-N / K_LIVE)   → 1→0.22, 4→0.63, 10→0.92
// workTerm = 1 - exp(-sumR / K_WORK)→ 3→0.26, 10→0.63, 30→0.95
// intensity = W_LIVE * liveTerm + W_WORK * workTerm
const K_LIVE = 4;
const K_WORK = 10;
const W_LIVE = 0.4;
const W_WORK = 0.6;

function computeIntensity(N: number, sumR: number): number {
  const liveTerm = 1 - Math.exp(-N / K_LIVE);
  const workTerm = 1 - Math.exp(-sumR / K_WORK);
  return W_LIVE * liveTerm + W_WORK * workTerm;
}

export interface UsePulseDataResult {
  activity: PulseActivity | undefined;
  activityStream: ActivityStreamEntry[];
  services: Array<{ active: string }> | undefined;
  activeRoles: string[];
  liveAgents: LiveAgent[];
  isActive: boolean;
  connected: boolean;
}

/**
 * THE THREE POLLS HERE ARE START/NOW'S WHOLE REMAINDER, and they only ran
 * while nobody was looking because a page under the shell could not ask.
 *
 * Law 3 keeps five panes mounted at all times (deb246b8), so four pages are
 * live behind a 48px rail with their `refetchInterval`s firing. Measured at
 * HEAD cd25161db over 16-second windows with the pathname asserted while away:
 * `/start/now` made 15 calls on its route and 15 after switching topic —
 * live-agents x6, active-roles x3, doctor x2, tasks?limit=20, brain. This hook
 * owns nine of those fifteen.
 *
 * `usePaneInterval` (ebe028f8) pauses the INTERVAL, never the query, so the
 * cache survives and a stale query refetches the moment the pane returns — the
 * page shows current data rather than the numbers it had when you left. Its
 * default is `true`, which is the load-bearing half of the contract: outside a
 * provider (`?embed=1`, `/onboarding`, `/q/:token`, a unit test) nothing is
 * off-screen and every consumer behaves exactly as before.
 *
 * THE "NO ALWAYS-MOUNTED CHROME DEPENDS ON THESE" CLAUSE IS GONE, 2026-09-16.
 * It used to read *"verified safe to pause: two consumers, both of them NOW's
 * own files; Blob.tsx deliberately does NOT subscribe"*. The sidebar blob now
 * does, through `shell/components/PulseData.tsx` mounted in the frame, because
 * the engine's SIZE is the activity visualization and fed nothing it sat at its
 * floor forever.
 *
 * WHAT THAT CHANGES, EXACTLY: the frame sits in no pane, so `usePaneInterval`'s
 * default `true` applies to the provider's mount and these three intervals run
 * on every route rather than only while `/start/now` is visible. The pause
 * still works for NOW's own two consumers — this hook is unchanged; only who
 * mounts it is. React Query dedupes on `queryKey`, so the provider and the page
 * share ONE set of requests, and `refetchIntervalInBackground` defaults to
 * `false`, so nothing polls while the tab is hidden.
 *
 * THE WEBSOCKET IS NOT PAUSED, and that is a decision rather than an omission.
 * It is a push channel, so an idle socket costs no requests; closing it would
 * drop `connected`, and the narrative sentence NOW renders from it would read
 * "Connecting to orchestrator…" on every return until the reconnect landed.
 * The 2 s local pruner is likewise left running: it makes no request, and
 * pausing it would let `activity.calls` report a minute that has already
 * passed for the first tick after a return.
 */
export function usePulseData(): UsePulseDataResult {
  const { data: doctor } = useQuery({
    queryKey: ["doctor"],
    queryFn: () => api<DoctorResult>("/api/doctor"),
    refetchInterval: usePaneInterval(8_000),
    staleTime: 4_000,
  });

  const { data: activeRolesData } = useQuery({
    queryKey: ["active-roles"],
    queryFn: () =>
      api<{
        active: boolean;
        roles: Array<{
          task_id: string;
          subtask: string;
          role: string;
          description: string;
        }>;
      }>("/api/active-roles"),
    refetchInterval: usePaneInterval(5_000),
  });

  // Live agents — CLI/orchestrator processes with a recent heartbeat.
  // Drives N in the intensity formula + per-agent satellites + the Now page
  // Live-Agents zone. Orthogonal to work-unit session state (an agent stays
  // live across many bootstrap → session_report cycles), which fixes the
  // pre-agents-layer bug where pulse silently dropped the CLI every time
  // an agent called session_report.
  const { data: liveAgentsData } = useQuery({
    queryKey: ["live-agents"],
    queryFn: () => api<{ agents: LiveAgent[] }>("/api/live-agents"),
    refetchInterval: usePaneInterval(3_000),
    staleTime: 1_500,
  });

  const [stream, setStream] = useState<ActivityStreamEntry[]>([]);
  const [flatCalls, setFlatCalls] = useState<number[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<ReconnectingWebSocket | null>(null);

  // Per-session rolling call timestamps. Kept in a ref so WS messages can
  // mutate freely; a periodic pruner reads it to produce derived state.
  const perSessionRef = useRef<Map<string, number[]>>(new Map());
  const [perSessionRates, setPerSessionRates] = useState<Map<string, number>>(
    new Map(),
  );

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
        type: string;
        event?: {
          type?: string;
          tool?: string;
          preview?: string;
          name?: string;
          text?: string;
          ts?: string;
          session?: string;
          provider?: string;
        };
      };
      try {
        msg = JSON.parse(ev.data as string);
      } catch {
        return;
      }
      if (msg.type !== "agent_activity" || !msg.event) return;

      const e = msg.event;
      const ts = e.ts || new Date().toISOString();
      const tool = e.tool || e.name;
      const preview = e.preview || e.text;

      if (e.type === "tool_use" || e.type === "mcp_tool_call" || e.type === "text") {
        setStream((prev) => {
          const next = [...prev, { ts, tool, preview }];
          return next.length > MAX_STREAM_LEN ? next.slice(-MAX_STREAM_LEN) : next;
        });
        const now = Date.now();
        setFlatCalls((prev) => {
          const cutoff = now - CALL_WINDOW_MS;
          return [...prev.filter((t) => t > cutoff), now];
        });

        // Per-session bucket. Falls back to provider when session missing,
        // then to "__unknown__" so the total still accumulates something.
        const sid = e.session || e.provider || "__unknown__";
        const bucket = perSessionRef.current.get(sid) ?? [];
        const cutoff = now - CALL_WINDOW_MS;
        const trimmed = bucket.filter((t) => t > cutoff);
        trimmed.push(now);
        perSessionRef.current.set(sid, trimmed);
      }
    };

    wsRef.current = ws;
    return () => {
      ws.close();
      wsRef.current = null;
      setConnected(false);
    };
  }, []);

  // Prune window + recompute per-session rates every 2s.
  useEffect(() => {
    const id = setInterval(() => {
      const cutoff = Date.now() - CALL_WINDOW_MS;
      // Prune flat
      setFlatCalls((prev) => prev.filter((t) => t > cutoff));
      // Prune per-session + compute rates
      const nextRates = new Map<string, number>();
      for (const [sid, tsList] of perSessionRef.current.entries()) {
        const kept = tsList.filter((t) => t > cutoff);
        if (kept.length === 0) {
          perSessionRef.current.delete(sid);
          continue;
        }
        perSessionRef.current.set(sid, kept);
        nextRates.set(sid, kept.length / (CALL_WINDOW_MS / 1000));
      }
      setPerSessionRates(nextRates);
    }, 2_000);
    return () => clearInterval(id);
  }, []);

  const services: Array<{ active: string }> | undefined = doctor
    ? doctor.checks.map((c) => ({
        active: c.status === "ok" ? "active" : c.status === "fail" ? "failed" : "degraded",
      }))
    : undefined;

  const activeRoles = (activeRolesData?.roles ?? [])
    .map((r) => r.role)
    .filter((r): r is string => typeof r === "string" && r.length > 0);

  const liveAgents = liveAgentsData?.agents ?? [];
  const N = liveAgents.length;
  let sumR = 0;
  for (const r of perSessionRates.values()) sumR += r;

  // Activity is undefined until at least one real source has responded, so
  // PulseCanvas can render em-dashes for "no data" rather than a fake zero.
  // Consider ourselves "connected" once either the WS or the live-agents
  // poll has returned something. Live-count is ground truth: if N > 0 we have
  // data even without WS traffic.
  const hasData = connected || liveAgentsData !== undefined;
  const activity: PulseActivity | undefined = hasData
    ? {
        calls: flatCalls.length,
        live: N,
        rate: sumR,
        intensity: computeIntensity(N, sumR),
      }
    : undefined;

  const isActive = N > 0 || activeRoles.length > 0 || flatCalls.length > 0;

  return {
    activity,
    activityStream: stream,
    services,
    activeRoles,
    liveAgents,
    isActive,
    connected,
  };
}
