/**
 * Models API client — /api/models*.
 *
 * Lists AI models the okuro registry knows about — local GGUF files
 * scanned from the configured model-store dirs plus subscription models
 * (claude/sonnet, gemini/2.5-pro, codex/o3, ...) mapped in the
 * bridge config.
 */

import { api } from "./api";

export type ModelEntry = {
  id: string;
  name: string;
  alias: string;
  source: "local-gguf" | "subscription" | string;
  provider?: string | null;
  tier?: "fast" | "standard" | "quality" | string | null;
  path?: string | null;
  size_gb: number;
  parameters?: string | null;
  quantization?: string | null;
  capabilities: string[];
  prompt_ready?: boolean;
  prompt_syntax?: string | null;
};

export type ModelListResponse = {
  models: ModelEntry[];
  by_source: Record<string, number>;
  by_provider: Record<string, number>;
  total: number;
};

/** One ranked result from the OSS catalog (HuggingFace + Civitai). */
export type CatalogEntry = {
  catalog_id: string;
  source: "huggingface" | "civitai" | string;
  display_name: string;
  modality: string;
  task: string;
  min_vram_gb: number;
  purpose_summary?: string | null;
  base_model?: string | null;
  family?: string | null;
  quality_signals?: { downloads?: number; likes?: number } | null;
  gated?: boolean;
  nsfw?: boolean;
  credential_required?: boolean;
  acquired?: boolean;
};

/** One durable row from the weekly model-scan (model_discoveries table). */
export type Discovery = {
  catalog_id: string;
  display_name: string;
  modality: string;
  min_vram_gb: number | null;
  fit_gpu: string | null;
  score: number | null;
  rationale: string | null;
  use_case: string | null;
  caveats: string | null;
  source_url: string | null;
  researched: boolean;
  commercial_status?: "commercial" | "conditional" | "non_commercial" | "unknown" | string;
  commercial_allowed?: boolean;
  license_id?: string | null;
  status: "new" | "acknowledged" | "installed" | "dismissed" | string;
  first_seen_at: string;
  last_seen_at: string;
};

/** Deployment edition + whether local inference is offered here. */
export type EditionInfo = {
  edition: "air" | "advanced" | "pro" | string;
  local_inference: boolean;
  vram_ceiling_gb: number | null;
  usable_vram_gb: number;
};

/** A non-okuro process holding VRAM, with how it can be reclaimed. */
export type GpuHolder = {
  tenant: string;
  pid: number;
  vram_mb: number;
  reclaim: "graceful_api" | "process_stop" | "none";
  endpoint?: string | null;
  models: string[];
};

export type GpuInfo = {
  gpu_index: number;
  total_gb: number;
  smi_free_gb: number | null;
  okuro_leases: { model_id: string; tier: string; vram_gb: number }[];
  external: GpuHolder[];
};

export type PullState = "idle" | "downloading" | "done" | "error";

export type PullStatus = {
  catalog_id?: string;
  display_name?: string;
  state: PullState;
  error?: string | null;
  bundle_id?: string | null;
  downloaded_bytes?: number;
  total_bytes?: number;
  progress?: number; // 0..1
  started_at?: string;
  updated_at?: string;
};

export const modelsApi = {
  list: (params?: { source?: string; provider?: string }) => {
    const qs = new URLSearchParams();
    if (params?.source) qs.set("source", params.source);
    if (params?.provider) qs.set("provider", params.provider);
    const q = qs.toString();
    return api<ModelListResponse>(`/api/models${q ? `?${q}` : ""}`);
  },
  get: (id: string) => api<ModelEntry>(`/api/models/${encodeURIComponent(id)}`),

  /** This deployment's edition + local-inference availability. */
  edition: () => api<EditionInfo>(`/api/models/edition`),

  /** Per-GPU tenants (okuro leases + external holders + reclaim methods). */
  gpu: () => api<{ gpus: GpuInfo[] }>(`/api/models/gpu`),

  /** Identify an unknown VRAM holder by pid (read-only). */
  identify: (pid: number, name = "") =>
    api<{ label: string; is_inference: boolean; reclaim_hint: string; source: string }>(
      `/api/models/identify`,
      { method: "POST", body: JSON.stringify({ pid, name }) },
    ),

  /** Execute one reclaim. process_stop is destructive → pass confirm=true. */
  reclaim: (holder: GpuHolder, confirm = false) =>
    api<{ ok: boolean; reason?: string; action?: string }>(`/api/models/reclaim`, {
      method: "POST",
      body: JSON.stringify({ holder, confirm }),
    }),

  /** The durable discoveries list — the Discover tab's default content. */
  discoveries: (params?: { query?: string; status?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.query) qs.set("query", params.query);
    if (params?.status) qs.set("status", params.status);
    qs.set("limit", String(params?.limit ?? 100));
    return api<{ discoveries: Discovery[] }>(
      `/api/models/discoveries?${qs.toString()}`,
    );
  },

  /** Advance a discovery's lifecycle (acknowledged | installed | dismissed). */
  setDiscoveryStatus: (catalog_id: string, status: string) =>
    api<{ ok: boolean; catalog_id: string; status: string }>(
      `/api/models/discoveries/status`,
      { method: "POST", body: JSON.stringify({ catalog_id, status }) },
    ),

  /** Search the OSS catalog, ranked for this box. */
  search: (params: { query?: string; modality?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params.query) qs.set("query", params.query);
    qs.set("modality", params.modality ?? "text");
    qs.set("limit", String(params.limit ?? 20));
    return api<CatalogEntry[]>(`/api/models/search?${qs.toString()}`);
  },

  /** Start a background pull of a catalog entry into the bundle store. */
  pull: (catalog_id: string, force = false, display_name?: string) =>
    api<PullStatus>(`/api/models/pull`, {
      method: "POST",
      body: JSON.stringify({ catalog_id, force, display_name }),
    }),

  /** Poll a pull's state. */
  pullStatus: (catalog_id: string) =>
    api<PullStatus>(
      `/api/models/pull/status?catalog_id=${encodeURIComponent(catalog_id)}`,
    ),

  /** All tracked pull jobs — rehydrates the downloads tray after a refresh. */
  pullActive: () => api<{ jobs: PullStatus[] }>(`/api/models/pull/active`),

  /** Drop a finished job from the tray (downloading jobs are kept). */
  pullDismiss: (catalog_id: string) =>
    api<{ ok: boolean }>(`/api/models/pull/dismiss`, {
      method: "POST",
      body: JSON.stringify({ catalog_id }),
    }),
};
