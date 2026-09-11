// <!-- AGENT_HEADER
// role: code
// purpose: Frontend client for /api/integrations — Settings → Integrations panel.
// index: types | api
// AGENT_HEADER_END -->

import { api } from "./api";

export interface AdapterHealth {
  name: string;
  state: "running" | "starting" | "stopped" | "error";
  last_poll_at: string | null;
  last_message_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  offset?: number;
  allowed_count?: number;
  pending_chat_id?: number | null;
}

export interface Integration {
  channel: string;
  enabled: boolean;
  status: "stopped" | "starting" | "running" | "error";
  has_token: boolean;
  allowed_chat_ids: number[];
  pending_chat_id: number | null;
  last_seen_at: string | null;
  last_message_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  adapter_health: AdapterHealth | null;
}

export const integrationsApi = {
  list: () => api<Integration[]>("/api/integrations"),
  get: (channel: string) => api<Integration>(`/api/integrations/${channel}`),

  setEnabled: (channel: string, enabled: boolean) =>
    api<Integration>(`/api/integrations/${channel}/enabled`, {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),

  approvePending: (channel: string) =>
    api<Integration>(`/api/integrations/${channel}/approve_pending`, {
      method: "POST",
    }),

  rejectPending: (channel: string) =>
    api<Integration>(`/api/integrations/${channel}/reject_pending`, {
      method: "POST",
    }),

  revoke: (channel: string, chatId: number) =>
    api<Integration>(`/api/integrations/${channel}/revoke`, {
      method: "POST",
      body: JSON.stringify({ chat_id: chatId }),
    }),

  setToken: (channel: string, token: string, keyringSession: string) =>
    api<Integration>(`/api/integrations/${channel}/token`, {
      method: "POST",
      headers: { "X-Keyring-Session": keyringSession },
      body: JSON.stringify({ token }),
    }),

  deleteToken: (channel: string, keyringSession: string) =>
    api<Integration>(`/api/integrations/${channel}/token`, {
      method: "DELETE",
      headers: { "X-Keyring-Session": keyringSession },
    }),

  listChallenges: (channel: string, keyringSession: string) =>
    api<Challenge[]>(`/api/integrations/${channel}/challenges`, {
      headers: { "X-Keyring-Session": keyringSession },
    }),

  addChallenge: (
    channel: string,
    keyringSession: string,
    body: { question: string; accepted_patterns: string[]; hint?: string | null },
  ) =>
    api<Challenge[]>(`/api/integrations/${channel}/challenges`, {
      method: "POST",
      headers: { "X-Keyring-Session": keyringSession },
      body: JSON.stringify(body),
    }),

  deleteChallenge: (channel: string, keyringSession: string, idx: number) =>
    api<Challenge[]>(`/api/integrations/${channel}/challenges/${idx}`, {
      method: "DELETE",
      headers: { "X-Keyring-Session": keyringSession },
    }),
};

export interface Challenge {
  question: string;
  accepted_patterns: string[];
  hint: string | null;
}
