/**
 * Media API client — /api/media/*.
 *
 * Request a podcast (or other delivery channel) for a target person on a
 * topic, with an optional recurring schedule. The backend turns the topic
 * into a briefing artifact, runs the delivery pipeline, and returns a WAV.
 */

import { api, getToken } from "./api";

export type MediaRecurring = {
  enabled: boolean;
  cron?: string;
  time?: string; // "HH:MM"
};

export type MediaRequest = {
  topic: string;
  person_id?: string | null;
  mode?: "summary" | "podcast"; // audio summary (single voice) | podcast (two-host)
  channel?: string; // advanced/back-compat; `mode` overrides
  brand_id?: string | null;
  recurring?: MediaRecurring;
};

export type MediaCreateResult = {
  job_id: string | null;
  status: string; // "generating" | "failed"
  error?: string | null;
};

export type MediaJob = {
  id: string;
  topic: string;
  person_id: string | null;
  channel: string;
  status: "generating" | "done" | "failed";
  stage: string;
  progress: number; // 0..1
  delivery_id: string | null;
  recurring_id: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
};

export type MediaItem = {
  id: string;
  title: string | null;
  person_id: string | null;
  channel: string | null;
  created_at: string | null;
  media_type: string | null;
  success: boolean;
  audio_url: string | null;
};

/**
 * Append the bearer token to an /api/* URL so `<audio src>` (which cannot
 * carry an Authorization header) passes the BearerAuthMiddleware. The
 * middleware accepts `?token=<bearer>` as an equivalent to the header.
 */
export async function withToken(url: string): Promise<string> {
  const token = await getToken();
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

export const mediaApi = {
  list: (limit = 25, channel = "podcast") =>
    api<{ media: MediaItem[] }>(
      `/api/media?limit=${limit}&channel=${encodeURIComponent(channel)}`,
    ),

  create: (payload: MediaRequest) =>
    api<MediaCreateResult>("/api/media", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  jobs: (limit = 20) =>
    api<{ jobs: MediaJob[] }>(`/api/media/jobs?limit=${limit}`),
};
