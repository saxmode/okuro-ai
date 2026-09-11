/**
 * Solve API client — /api/todos/{id}/solve + /api/sessions/{id}/reissue-bearer.
 *
 * Two paths share the solve endpoint:
 *   - session       → mints sessions_inline row + scoped bearer
 *   - orchestrator  → spawns the engine with --source-todo-id
 *
 * The session response carries the plaintext bearer; the caller passes
 * it directly to the inline panel as a prop so the token never lands
 * in browser history.
 *
 * Wave 5 adds bearer reissue: when a todo card already has a
 * ``claimed_session_id`` (returned from /api/todos), the button mints a
 * fresh bearer via ``reissueBearer`` instead of opening the Solve
 * dialog. This survives page reload — the previous bearer was kept in
 * component state only and dies with the tab.
 */

import { api } from "./api";

export type SolveMode = "session" | "orchestrator";
export type SolveProvider = "claude" | "gemini" | "codex";

export interface SessionSolveResult {
  mode: "session";
  session_id: string;
  bearer: string;
  inline_url: string;
  provider: string;
}

export interface OrchestratorSolveResult {
  mode: "orchestrator";
  orchestrator_id: string;
  status_url: string;
  pid: number;
}

export type SolveResult = SessionSolveResult | OrchestratorSolveResult;

export interface SolveRequest {
  mode: SolveMode;
  provider?: SolveProvider;
  model?: string;
  intelligence?: string;
}

export interface ReissueBearerResult {
  bearer: string;
  inline_url: string;
}

export const solveApi = {
  solve: (todoId: string, body: SolveRequest) =>
    api<SolveResult>(`/api/todos/${encodeURIComponent(todoId)}/solve`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /**
   * Mint a fresh per-session bearer for an in-flight inline session.
   * Auth'd by the global API token (the caller has lost the per-session
   * bearer it's trying to recover). 404 if the session doesn't exist;
   * 409 if it has reached a terminal state. The caller falls back to
   * the Solve dialog on either error.
   */
  reissueBearer: (sessionId: string) =>
    api<ReissueBearerResult>(
      `/api/sessions/${encodeURIComponent(sessionId)}/reissue-bearer`,
      { method: "POST" },
    ),
};
