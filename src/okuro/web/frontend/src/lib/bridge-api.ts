/**
 * Bridge API client — /api/bridge/*.
 *
 * Wraps the HTTP router added alongside the frontend /bridge page.
 * status+providers+usage are read-only GETs; invoke is localhost-only
 * and requires bearer auth (handled by api()).
 */

import { api } from "./api";

export type ProviderStatus = {
  id: string;
  type: string; // "cli" | "local-http"
  available: boolean;
  models: Record<string, string>;
  capabilities: string[];
  default_timeout: number;
};

export type BridgeStatus = {
  status: "ok" | "degraded";
  providers: ProviderStatus[];
  total: number;
  available: number;
};

export type RoutingInfo = {
  providers: ProviderStatus[];
  routing: Record<string, string>;
};

export type UsageByProvider = { count: number; total_duration: number };
export type UsageSummary = {
  period: string;
  invocations: number;
  by_provider: Record<string, UsageByProvider>;
};

export type InvokeRequest = {
  prompt: string;
  capability?: string;
  provider?: string;
  timeout?: number;
};

export type InvokeResponse = {
  success: boolean;
  output: string;
  provider: string;
  model: string;
  duration: number;
  latency_ms: number;
  error?: string | null;
};

export const bridgeApi = {
  status: () => api<BridgeStatus>("/api/bridge/status"),
  providers: () => api<RoutingInfo>("/api/bridge/providers"),
  usage: (period = "week") =>
    api<UsageSummary>(`/api/bridge/usage?period=${encodeURIComponent(period)}`),
  invoke: (payload: InvokeRequest) =>
    api<InvokeResponse>("/api/bridge/invoke", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};
