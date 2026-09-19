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

/**
 * THE REACT KEY FOR ONE MODEL ROW — key on what identifies the FILE, not the
 * model. `/api/models` is the ONE list endpoint in okuro whose `id` is not
 * unique, and this is the only place that rule has to be written down.
 *
 * WHY `id` ALONE IS NOT ENOUGH, measured against the live payload 2026-09-15:
 *
 *     rows returned by GET /api/models   48
 *     distinct id                        43   <- 5 short
 *     distinct id + source               43   <- the composite buys NOTHING
 *     distinct path                      40   <- 8 short
 *     distinct id + path                 48   <- unique
 *
 * A multi-part GGUF reports one `id` per FILE, so three shards of
 * `nvidia-nemotron-3-super-120b-a12b-ud` arrive as three rows with one id;
 * `qwen2-5-7b` likewise ×3 and `qwen3-32b` ×2 (two copies on different
 * disks). All three collisions sit inside ONE source (`local-gguf`), which is
 * why `id + source` — the fix memory 245d4942 recorded — concatenates to the
 * identical string for every colliding row and fixes nothing. React's own
 * warning says children "may be duplicated and/or omitted", so the list could
 * silently drop a row.
 *
 * AND WHY `path` ALONE IS NOT ENOUGH EITHER, which corrects the p3 spec
 * (`f39fa488` §7) and the brief it produced: both say "key on `path`". Nine of
 * the 48 rows are SUBSCRIPTION models with `path: null` — they are a bridge
 * config mapping, not a file — so `path` collapses them to one empty key and
 * trades five collisions for eight. Measured, not reasoned: 40 distinct.
 *
 * So the key is the pair. `id` disambiguates the rows that have no file, `path`
 * disambiguates the files that share an id, and neither is redundant.
 */
export function modelRowKey(m: Pick<ModelEntry, "id" | "path">): string {
  return `${m.id}|${m.path ?? ""}`;
}

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
  /** P4 — which wanted bucket this candidate came from. */
  category?: string | null;
  /** P4 — every variant tag the one parser found on the name. */
  variant_tags?: string[];
  /** P4 — the subset the `variants` toggle hides: abliterated, uncensored,
   *  nsfw, roleplay. Tagged and kept, never dropped (ruling 3). */
  identity_variants?: string[];
  /** P4 — lineage relation to an installed unit. `null` means the pass has
   *  not judged this row, which is NOT the same as "unrelated". */
  relation?: string | null;
  relation_target?: string | null;
  relation_confidence?: number | null;
  /** P4 — runnable when idle AND (related, or its category is empty). */
  interesting?: boolean | null;
  interesting_why?: string | null;
  /** P3 — the best placement on THIS host, joined by the discoveries route.
   *  `null` means no placement fits when idle, which is a real answer. */
  best_fit?: BestFit | null;
};

/** What one wanted category found in the last scan — including nothing. */
export type CategoryScan = {
  category: string;
  source: string;
  selector: string | null;
  fetched: number;
  passed_gate: number;
  published: number;
  interesting: number;
  zero_result: boolean;
  note: string | null;
  scanned_at: string;
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
  job_id?: string;
  catalog_id?: string;
  display_name?: string;
  state: PullState;
  error?: string | null;
  bundle_id?: string | null;
  /** "store" = a declared model store (P6), "bundle" = okuro's own store. */
  into?: "store" | "bundle";
  destination?: string | null;
  headroom?: Headroom | null;
  unit_ids?: string[];
  results?: ResultsLink[];
  downloaded_bytes?: number;
  total_bytes?: number;
  progress?: number; // 0..1
  started_at?: string;
  updated_at?: string;
};

/** Free space on the destination store against what a pull adds.
 *
 *  `warn` is NEVER a refusal — ruling 8, 2026-09-15: there is no headroom
 *  floor. The page shows the numbers and keeps the pull button live beside
 *  them. `size_known: false` means the source publishes no size, which is a
 *  different fact from "it fits". */
export type Headroom = {
  store: string;
  free_gb: number | null;
  total_gb: number | null;
  size_gb: number | null;
  after_gb: number | null;
  margin_gb: number;
  warn: boolean;
  size_known: boolean;
  text: string;
};

/** Where a person goes to SEE what a model produces. */
export type ResultsLink = { label: string; url: string; via: string };

export type PullPlan = {
  ok: boolean;
  dry_run: boolean;
  reason?: string;
  catalog_id?: string;
  display_name?: string;
  destination?: {
    store: string;
    tier: string;
    path: string;
    rel_path: string;
    why: string;
  };
  files?: {
    files: { name: string; size_bytes: number | null }[];
    total_gb: number | null;
    why: string;
  };
  headroom?: Headroom;
  results?: ResultsLink[];
};


/* ── P1/P2/P3/P5 — the store-aware inventory, the consumer map, swap plans ──
 *
 * Every one of these endpoints answers "not configured" as its own shape
 * (`configured: false` with a reason) and HTTP 200. A host that declares no
 * model stores is a supported install, and rendering that as "zero models
 * found" is the confusion these types exist to prevent.
 */

/** The best stored placement for a subject — P3's placement space, ranked. */
export type BestFit = {
  mode: string;
  gpu: string | null;
  est_vram_gb: number;
  est_ram_gb: number;
  speed_class: "fast" | "usable" | "slow" | string;
  offloaded_pct: number | null;
  fits_idle: boolean;
  fits_now: boolean | null;
  basis: string;
};

/** One model unit on one configured store. */
export type ModelUnit = {
  unit_id: string;
  name: string;
  store: string;
  rel_path: string;
  size_bytes: number;
  size_gb: number;
  format: string;
  layout: string;
  status: "ok" | "broken" | "missing" | string;
  note: string | null;
  identity: string | null;
  family?: string | null;
  version?: string | null;
  quant?: string | null;
  variant_tags?: string[] | null;
  /** From P2's join — an EMPTY list means no DECLARED consumer names it, */
  /** never that nothing uses it. A loader can build a path at runtime. */
  consumers?: string[];
  tier?: "PROTECTED" | "ACTIVE" | "ARCHIVE" | null;
  reachable?: boolean;
  best_fit?: BestFit | null;
  /** P6 — where to SEE what this model produces. Empty when the host has
   *  declared no results_urls for this unit's consumers or modality. */
  results?: ResultsLink[];
  last_seen?: string;
};

export type StoreSummary = {
  store: string;
  units: number;
  size_bytes: number;
  size_gb: number;
  broken: number;
  missing: number;
  identified: number;
  last_scanned: string | null;
};

export type StoreInfo = {
  name: string;
  path: string;
  tier: "hot" | "cold" | string;
  exists: boolean;
  /** Measured on the request, not cached with the unit table — the same
   *  number the swap plan's headroom warning reads. `null` when the path
   *  cannot be stat'd, which is not the same as zero. */
  free_gb: number | null;
  total_gb: number | null;
  used_pct: number | null;
};

export type InventoryResponse = {
  configured: boolean;
  reason?: string;
  stores: StoreInfo[];
  summary: StoreSummary[];
  units: ModelUnit[];
  totals: { units: number; size_bytes: number; size_gb: number };
  twins?: { identity: unknown[]; size: unknown[] };
  scan?: Record<string, unknown>;
};

/** One reference, proven by a config file:line. */
export type ConsumerRef = {
  consumer: string;
  run_mode: string;
  config_path: string;
  line: number;
  model_ref: string;
  unit_id: string | null;
  match_kind: string | null;
  tier: "PROTECTED" | "ACTIVE" | "ARCHIVE" | string;
  state: string;
  reachable: boolean;
  note: string | null;
  location: string;
};

export type ConsumerSummary = {
  consumer: string;
  run_mode: string;
  tier: string;
  state: string;
  refs: number;
  units: number;
  dead: number;
  unreachable: number;
  last_seen: string | null;
};

export type ConsumersResponse = {
  configured: boolean;
  reason?: string;
  roots: { name: string; path: string; kind: string; exists: boolean }[];
  protected: string[];
  consumers: ConsumerSummary[];
  rows: ConsumerRef[];
  unreferenced?: { unit_id: string; rel_path: string; store: string; size_gb: number }[];
  totals: { rows: number; consumers: number; dead: number; unreachable: number };
};

/** One checklist item on a swap plan's gate. */
export type SwapChecklistItem = { item: string; done: boolean; done_at: string | null };

/** severity "warn" NEVER refuses an apply — ruling 8 removed the floor. */
export type SwapBlocker = { code: string; severity: "block" | "warn" | string; text: string; resolved: boolean };

/** One config LINE a swap would change. */
export type SwapRow = {
  id: number;
  config_path: string;
  line: number;
  old_ref: string;
  new_ref_written: string | null;
  match_kind: string | null;
  rewritable: boolean;
  rewrite_note: string | null;
  diff: string | null;
  location: string;
};

export type SwapPlan = {
  plan_group: string;
  consumer: string;
  tier: "PROTECTED" | "ACTIVE" | "ARCHIVE" | string;
  unit_old: string;
  new_kind: "unit" | "release" | string;
  new_ref: string;
  relation: string | null;
  relation_why: string | null;
  mode: "replace" | "side-by-side" | string;
  state: "proposed" | "testing" | "applied" | "rejected" | string;
  note: string | null;
  checklist: SwapChecklistItem[];
  blockers: SwapBlocker[];
  rows: SwapRow[];
  created_at: string;
  updated_at: string;
  totals: {
    rows: number;
    rewritable: number;
    unrewritable: number;
    files: number;
    checklist_done: number;
    checklist_total: number;
    blocking: number;
    warnings: number;
  };
};

export type SwapListResponse = {
  configured: boolean;
  reason?: string;
  plans: SwapPlan[];
  checklists?: { default_active: string[]; config_key: string };
  totals: { plans: number; by_state: Record<string, number> };
};

/** A mutation's answer. `ok: false` with a plan is a REFUSAL, not an error. */
export type SwapActionResponse = {
  ok?: boolean;
  reason?: string;
  plan?: SwapPlan;
  apply_by_hand?: string;
  undone?: string[];
  blockers?: SwapBlocker[];
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
  discoveries: (params?: {
    query?: string;
    status?: string;
    limit?: number;
    category?: string;
    /** "hide" (default) omits rows tagged abliterated/uncensored/nsfw/roleplay.
     *  They are never deleted — this only decides what is rendered. */
    variants?: "hide" | "show";
    interesting?: boolean;
  }) => {
    const qs = new URLSearchParams();
    if (params?.query) qs.set("query", params.query);
    if (params?.status) qs.set("status", params.status);
    if (params?.category) qs.set("category", params.category);
    if (params?.variants) qs.set("variants", params.variants);
    if (params?.interesting) qs.set("interesting", "true");
    qs.set("limit", String(params?.limit ?? 100));
    return api<{ discoveries: Discovery[]; categories: CategoryScan[] }>(
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

  /** Plan a pull without downloading: destination, headroom, file list.
   *
   *  The page calls this BEFORE `pull`, so a headroom shortfall is on screen
   *  before any byte moves. It never refuses — see the Headroom type. */
  pullDryRun: (catalog_id: string, opts?: { store?: string; file?: string }) =>
    api<PullPlan>(`/api/models/pull/dry-run`, {
      method: "POST",
      body: JSON.stringify({ catalog_id, ...(opts ?? {}) }),
    }),

  /** Start a background pull. Into a declared model store when this host has
   *  one, otherwise into okuro's bundle store. */
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
  /** Unit inventory over the configured stores. `refresh: "stat"` is the live
   *  dir-stat walk — ruling 5's page-load path for the cold store. It never
   *  opens a model file. */
  inventory: (params?: {
    store?: string;
    format?: string;
    status?: string;
    refresh?: "stat" | "identity";
    twins?: boolean;
    limit?: number;
  }) => {
    const qs = new URLSearchParams();
    if (params?.store) qs.set("store", params.store);
    if (params?.format) qs.set("format", params.format);
    if (params?.status) qs.set("status", params.status);
    if (params?.refresh) qs.set("refresh", params.refresh);
    if (params?.twins) qs.set("twins", "true");
    qs.set("limit", String(params?.limit ?? 1000));
    return api<InventoryResponse>(`/api/models/inventory?${qs.toString()}`);
  },

  /** The code-derived consumer map — every row proven by a config file:line. */
  consumers: (params?: {
    unit?: string;
    consumer?: string;
    tier?: string;
    dead?: boolean;
    unreachable?: boolean;
    unreferenced?: boolean;
    refresh?: boolean;
    limit?: number;
  }) => {
    const qs = new URLSearchParams();
    if (params?.unit) qs.set("unit", params.unit);
    if (params?.consumer) qs.set("consumer", params.consumer);
    if (params?.tier) qs.set("tier", params.tier);
    if (params?.dead) qs.set("dead", "true");
    if (params?.unreachable) qs.set("unreachable", "true");
    if (params?.unreferenced) qs.set("unreferenced", "true");
    if (params?.refresh) qs.set("refresh", "true");
    qs.set("limit", String(params?.limit ?? 1000));
    return api<ConsumersResponse>(`/api/models/consumers?${qs.toString()}`);
  },

  /** Swap plans — proposed line edits that nothing executes. */
  swaps: (params?: { consumer?: string; state?: string; unit?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    if (params?.consumer) qs.set("consumer", params.consumer);
    if (params?.state) qs.set("state", params.state);
    if (params?.unit) qs.set("unit", params.unit);
    qs.set("limit", String(params?.limit ?? 50));
    return api<SwapListResponse>(`/api/models/swaps?${qs.toString()}`);
  },

  /** Tick one checklist item (1-based). The first tick opens `testing`. */
  swapTick: (plan: string, item: number) =>
    api<SwapActionResponse>(`/api/models/swaps/${encodeURIComponent(plan)}/tick`, {
      method: "POST",
      body: JSON.stringify({ item }),
    }),

  /** Mark a plan applied and get the patch back. okuro edits NO consumer file. */
  swapApply: (plan: string) =>
    api<SwapActionResponse>(`/api/models/swaps/${encodeURIComponent(plan)}/apply`, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  /** Close a plan without applying it. The reason is the point. */
  swapReject: (plan: string, why: string) =>
    api<SwapActionResponse>(`/api/models/swaps/${encodeURIComponent(plan)}/reject`, {
      method: "POST",
      body: JSON.stringify({ why }),
    }),
};
