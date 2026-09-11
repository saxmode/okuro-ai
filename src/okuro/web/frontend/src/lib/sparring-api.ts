// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism sparring API client — stateful multi-turn session CRUD +
//   move loop + change-feed tail. Mirrors lib/prism-api.ts (same api() helper,
//   same events-poll shape) for the /sparring page.
// AGENT_HEADER_END -->
import { api } from "./api";

export type TurnKind = "recall" | "challenge" | "assumption" | "decision" | "tripwire" | "note";
export type Verdict = "open" | "held" | "conceded" | "killed" | "falsified";
export type MoveType = "challenge" | "panel" | "assumption" | "decision" | "tripwire" | "note" | "verdict";

/** One turn in the log. `meta` is kind-specific (severity/rebuttal for a
 *  panel-sourced challenge, trigger for a tripwire, items for a recall). */
export interface Turn {
  n: number;
  kind: TurnKind;
  content: string;
  verdict: Verdict;
  source: string;
  meta: Record<string, unknown>;
  ts: string;
}

/** The derived live board — what is still live pressure vs. settled. */
export interface SparringState {
  open_challenges: Turn[];
  standing_assumptions: Turn[];
  falsified_assumptions: Turn[];
  killed: Turn[];
  decisions: Turn[];
  tripwires: Turn[];
  counts: {
    open_challenges: number;
    standing_assumptions: number;
    falsified: number;
    decisions: number;
    tripwires: number;
  };
}

export interface SessionSummary {
  id: string;
  topic: string;
  person_id: string | null;
  brand_id: string | null;
  status: "open" | "closed";
  turn_count: number;
  created_at: string;
  updated_at: string;
}

export interface SessionDetail extends SessionSummary {
  topic_key: string;
  turns: Turn[];
  state: SparringState;
}

/** A move to advance a session. `type` picks the branch; the rest are the
 *  move's fields (content, ref/verdict, trigger, audience_hint). */
export interface Move {
  type: MoveType;
  content?: string;
  source?: string;
  audience_hint?: string;
  trigger?: string;
  ref?: number;
  verdict?: Exclude<Verdict, "open">;
  max_panel?: number;
}

export interface SparringEvent {
  seq: number;
  session_id: string;
  kind: "saved" | "deleted";
  origin: string;
  ts: string;
}

export const sparringApi = {
  list: (topic?: string) =>
    api<{ sessions: SessionSummary[]; seq: number }>(
      `/api/sparring${topic ? `?topic=${encodeURIComponent(topic)}` : ""}`,
    ),
  get: (id: string) => api<SessionDetail>(`/api/sparring/${encodeURIComponent(id)}`),
  start: (topic: string, personId?: string, brandId?: string) =>
    api<SessionDetail>("/api/sparring/start", {
      method: "POST",
      body: JSON.stringify({ topic, person_id: personId, brand_id: brandId }),
    }),
  move: (id: string, move: Move) =>
    api<SessionDetail>(`/api/sparring/${encodeURIComponent(id)}/move`, {
      method: "POST",
      body: JSON.stringify(move),
    }),
  close: (id: string) =>
    api<SessionDetail>(`/api/sparring/${encodeURIComponent(id)}/close`, { method: "POST" }),
  remove: (id: string) =>
    api<{ deleted: boolean }>(`/api/sparring/${encodeURIComponent(id)}`, { method: "DELETE" }),
  events: (since: number) =>
    api<{ events: SparringEvent[]; seq: number }>(`/api/sparring/events?since=${since}`),
};
