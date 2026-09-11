// <!-- AGENT_HEADER
// role: code
// purpose: okuro·resonance API client — the communication compiler chain
//   (ingest → analyze → interview → answer → render) over /api/resonance/*.
// AGENT_HEADER_END -->
import { api } from "./api";

export interface Claim {
  subject?: string;
  predicate?: string;
  object?: string;
  text?: string;
  source_ref?: string | null;
  confidence: number;
}

export interface IngestManifest {
  artifact_id: string;
  source_ref: string | null;
  claims_added: number;
  claims: Claim[];
}

export type ReqStatus = "covered" | "partial" | "missing";
export type GapRoute = "research" | "interview";

export interface Requirement {
  requirement: string;
  status: ReqStatus;
  confidence: number;
  route: GapRoute;
  question: string;
}

export interface OpenQuestion {
  question: string;
  route: GapRoute;
  requirement: string;
}

export interface GapReport {
  completeness: number;
  requirements: Requirement[];
  open_questions: OpenQuestion[];
  ready: boolean;
  sources?: string[];
  note?: string;
}

export interface InterviewResponse {
  questions: string[];
  completeness: number;
  ready: boolean;
  research_gaps: string[];
}

export interface PcoBeat {
  intent: string;
  depth_hint: string;
  priority: number;
  claims: Claim[];
}

export interface PcoDigest {
  goal: string;
  title: string;
  beat_count: number;
  min_confidence: number;
  beats: PcoBeat[];
  facts: string[];
}

export interface RenderResponse {
  pco: PcoDigest;
  render: {
    media: string;
    audience: Record<string, unknown>;
    result: { id?: string; url?: string; title?: string; facet_count?: number };
  };
}

export interface ResearchResponse {
  researched: { question: string; artifact_id: string; claims_added: number }[];
  new_artifact_ids: string[];
  remaining_research: number;
}

export type Media = "prism" | "slides" | "website";

// --- Durable interview sessions + actuality ---------------------------------
export type SessionStatus = "open" | "ready" | "closed";

export interface SessionTurn {
  kind: "answer" | "doc" | "research";
  question: string | null;
  answer: string | null;
  artifact_id: string | null;
  created_at: string;
}

export interface Session {
  id: string;
  goal: string;
  project: string | null;
  person_id: string | null;
  brand_id: string | null;
  status: SessionStatus;
  artifact_ids: string[];
  completeness: number | null;
  ready: boolean;
  open_questions: string[];
  turns?: SessionTurn[];
  created_at: string;
  updated_at: string;
}

export interface SessionNextResponse {
  session_id: string;
  questions: string[];
  completeness: number;
  ready: boolean;
  research_gaps: string[];
}

export interface SessionAnswerResponse extends SessionNextResponse {
  artifact_id: string;
  claims_added: number;
}

export interface StaleSource {
  artifact_id: string;
  title: string | null;
  source_ref: string | null;
  class: "web" | "doc" | "tacit";
  age_days: number;
  threshold_days: number | null;
}

export interface ActualityReport {
  checked: number;
  stale: StaleSource[];
  fresh: number;
  note: string;
}

export const resonanceApi = {
  ingest: (text: string, title: string, sourceRef?: string, project?: string) =>
    api<IngestManifest>("/api/resonance/ingest", {
      method: "POST",
      body: JSON.stringify({ text, title, source_ref: sourceRef, project }),
    }),
  analyze: (goal: string, sourceArtifactIds: string[], project?: string) =>
    api<GapReport>("/api/resonance/analyze", {
      method: "POST",
      body: JSON.stringify({ goal, source_artifact_ids: sourceArtifactIds, project }),
    }),
  interview: (goal: string, sourceArtifactIds: string[], maxQ = 3, project?: string) =>
    api<InterviewResponse>("/api/resonance/interview", {
      method: "POST",
      body: JSON.stringify({ goal, source_artifact_ids: sourceArtifactIds, max_q: maxQ, project }),
    }),
  answer: (goal: string, question: string, answer: string, project?: string) =>
    api<IngestManifest>("/api/resonance/answer", {
      method: "POST",
      body: JSON.stringify({ goal, question, answer, project }),
    }),
  research: (goal: string, sourceArtifactIds: string[], maxGaps = 3, project?: string) =>
    api<ResearchResponse>("/api/resonance/research", {
      method: "POST",
      body: JSON.stringify({ goal, source_artifact_ids: sourceArtifactIds, max_gaps: maxGaps, project }),
    }),
  render: (
    goal: string,
    sourceArtifactIds: string[],
    audience?: string,
    media: Media = "prism",
    project?: string,
  ) =>
    api<RenderResponse>("/api/resonance/render", {
      method: "POST",
      body: JSON.stringify({ goal, source_artifact_ids: sourceArtifactIds, audience, media, project }),
    }),

  // Durable sessions — thread only a session_id, resume anytime.
  sessionCreate: (goal: string, opts?: { project?: string; personId?: string; brandId?: string; sourceArtifactIds?: string[] }) =>
    api<Session>("/api/resonance/session/create", {
      method: "POST",
      body: JSON.stringify({
        goal, project: opts?.project, person_id: opts?.personId,
        brand_id: opts?.brandId, source_artifact_ids: opts?.sourceArtifactIds,
      }),
    }),
  sessionNext: (sessionId: string, maxQ = 3) =>
    api<SessionNextResponse>("/api/resonance/session/next", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, max_q: maxQ }),
    }),
  sessionAnswer: (sessionId: string, question: string, answer: string) =>
    api<SessionAnswerResponse>("/api/resonance/session/answer", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, question, answer }),
    }),
  sessionGet: (sessionId: string) =>
    api<Session>(`/api/resonance/session/${encodeURIComponent(sessionId)}`),
  sessionList: (status?: SessionStatus, project?: string) => {
    const q = new URLSearchParams();
    if (status) q.set("status", status);
    if (project) q.set("project", project);
    const qs = q.toString();
    return api<{ sessions: Session[] }>(`/api/resonance/sessions${qs ? `?${qs}` : ""}`);
  },
  actualityCheck: (sourceArtifactIds: string[]) =>
    api<ActualityReport>("/api/resonance/actuality/check", {
      method: "POST",
      body: JSON.stringify({ source_artifact_ids: sourceArtifactIds }),
    }),
  actualityRefresh: (sourceArtifactIds: string[], project?: string) =>
    api<{ refreshed: { question: string; old_artifact_id: string; new_artifact_id: string; claims_added: number }[]; new_artifact_ids: string[]; skipped: number }>("/api/resonance/actuality/refresh", {
      method: "POST",
      body: JSON.stringify({ source_artifact_ids: sourceArtifactIds, project }),
    }),
};
