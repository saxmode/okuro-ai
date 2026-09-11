// <!-- AGENT_HEADER
// role: code
// purpose: okuro·studio API client — outcome-based image generation over
//   /api/studio/*. Model-free: the UI talks in styles + plain prompts. Wraps
//   presets, readiness, setup (Gap A) + generate (Gap C) as SSE-progress jobs,
//   and the result library. SSE uses EventSource with ?token= (works in both
//   Chromium and pywebview's WebKit, where fetch-stream buffers).
// AGENT_HEADER_END -->
import { api, getToken } from "./api";
import type { MediaAsset } from "./assets-api";
import { withToken } from "./media-api";

export interface StudioPreset {
  id: string;
  label: string;
  description: string;
  icon: string;
  modality: string;
  tiers: string[];
  default_tier: string;
  /** true → every way of rendering this style needs local inference. */
  needs_gpu: boolean;
}

export type ReadinessState = "ready" | "setup_needed";

export interface StudioReadiness {
  preset: string;
  family: string;
  comfy_installed: boolean;
  has_model: boolean;
  runtime_ready: boolean;
  state: ReadinessState;
  message: string;
  /** Which engine renders this style — "comfy" (local) | "bridge" (cloud). */
  engine?: string;
  /** false → okuro CANNOT fix this itself; the user installs/signs in to a CLI. */
  self_service?: boolean;
  /** Cloud only: the resolved provider, and why it isn't usable yet. */
  provider?: string | null;
  provider_state?: "ready" | "signed_out" | "not_installed" | "unsupported" | string;
}

/** One library tile — the full media-bucket row, so the shared media inspector
 *  can open it with no extra fetch. `url` resolves BY ID through the asset store
 *  (the only resolver that knows both byte homes); `name` is the library
 *  filename the reference picker submits back. */
export type StudioImage = MediaAsset & {
  name: string;
  url: string;
};

/** How the prompt was shaped for the model that ran (feature C transparency). */
export interface PromptPlan {
  syntax: string | null; // booru_tags | natural_language | chat_template
  prompt: string | null; // the optimized prompt actually sent
  negative: string | null;
  notes: string[];
}

export interface GenerateResult {
  preset: string;
  tier: string;
  seed: number | null; // cloud providers expose no seed
  prompt_used: string;
  count: number;
  images: string[]; // /api/studio/image/<name> paths
  model?: { id: string | null; family: string | null };
  prompt_plan?: PromptPlan;
  engine?: string;
}

/** One curated model option for a style, with runtime availability resolved. */
export interface StudioModelChoice {
  family: string;
  label: string;
  rationale: string;
  recommended: boolean;
  installed: boolean;
  model_id: string | null;
  vram_gb: number | null;
  source: "curated" | "installed" | "cloud";
  /** Which engine renders this choice — "comfy" (local) | "bridge" (cloud). */
  engine: string;
}

export interface StudioResolvedModel {
  model_id: string;
  label: string;
  family: string;
  installed: boolean;
  /** Engine that will actually run — a style may offer both. */
  engine: string;
}

export interface StudioModels {
  preset: string;
  resolved: StudioResolvedModel | null; // default model this style will run
  choices: StudioModelChoice[];
}

export interface StartedJob {
  job_id: string;
  state: string;
}

/** One progress frame from a setup or generation job (shared contract). */
export interface ProgressEvent {
  phase: string; // engine | model | link | workflow | generate | ready | error
  message: string;
  pct?: number;
}

export interface GenerateBody {
  preset: string;
  prompt: string;
  tier?: string | null;
  model_id?: string | null;
  ckpt_name?: string | null;
  seed?: number | null;
  org_revenue_usd?: number | null;
  /** Cloud styles only: absolute paths to edit from ("make the jacket red").
   * Ignored by the local engine. */
  reference_images?: string[] | null;
  /** Cloud styles only: pin the text the image may render. A word/phrase allows
   * exactly that; "" forces a text-free image; null leaves the model free — and
   * it WILL invent captions, dates, venues and real-looking credits. */
  literal_text?: string | null;
}

export interface SetupBody {
  preset?: string;
  family?: string;
  consent: boolean;
  org_revenue_usd?: number | null;
}

export interface JobHandlers<R> {
  onProgress?: (ev: ProgressEvent) => void;
  onDone?: (result: R) => void;
  onError?: (message: string) => void;
  onCancelled?: () => void;
}

/** Stream a job's SSE progress (named events: progress / done / error).
 * Returns a canceller. Bearer goes in the query string — EventSource is
 * GET-only and can't set headers; the API middleware accepts ?token=. */
async function streamJob<R>(path: string, h: JobHandlers<R>): Promise<() => void> {
  const token = await getToken();
  const sep = path.includes("?") ? "&" : "?";
  const es = new EventSource(`${path}${sep}token=${encodeURIComponent(token)}`);
  let finished = false;
  const close = () => {
    if (finished) return;
    finished = true;
    es.close();
  };

  es.addEventListener("progress", (e) => {
    try {
      h.onProgress?.(JSON.parse((e as MessageEvent).data));
    } catch {
      /* ignore malformed frame */
    }
  });
  const terminal = (ok: boolean) => (e: Event) => {
    let payload: { result?: R; error?: string } = {};
    try {
      payload = JSON.parse((e as MessageEvent).data);
    } catch {
      /* ignore */
    }
    if (ok) h.onDone?.(payload.result as R);
    else h.onError?.(payload.error ?? "job failed");
    close();
  };
  es.addEventListener("done", terminal(true));
  es.addEventListener("error", terminal(false));
  es.addEventListener("cancelled", () => {
    h.onCancelled?.();
    close();
  });
  // Connection drop before a terminal frame.
  es.onerror = () => {
    if (!finished) {
      h.onError?.("connection lost");
      close();
    }
  };
  return close;
}

/** One installed LLM bundle you can prompt (Studio text mode). */
export interface TextModel {
  bundle_id: string;
  display_name: string;
  parameters: string | null;
  engine: string | null;
  warm: boolean;
  commercial_status?: "commercial" | "conditional" | "non_commercial" | "unknown" | string;
  commercial_allowed?: boolean;
  license_id?: string | null;
}

export interface TextGenerateBody {
  bundle_id: string;
  prompt: string;
  system_prompt?: string | null;
  temperature?: number | null;
}

/** A text engine currently loaded on a GPU (the "what's running" overview). */
export interface TextEngine {
  model_id: string;
  endpoint: string | null;
  gpu_index: number | null;
  tier: string | null;
  uptime_s: number | null;
}

export interface TextGenerateResult {
  success: boolean;
  text: string;
  error?: string | null;
  latency_s: number;
  endpoint: string;
  warm_start: boolean;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface TextChatBody {
  bundle_id: string;
  messages: ChatMessage[];
  temperature?: number | null;
}

export const studioApi = {
  presets: (modality = "image") =>
    api<{ presets: StudioPreset[] }>(`/api/studio/presets?modality=${modality}`),

  readiness: (preset: string) =>
    api<StudioReadiness>(`/api/studio/readiness?preset=${encodeURIComponent(preset)}`),

  /** Curated model picker + default resolved model for a style (feature C). */
  models: (preset: string) =>
    api<StudioModels>(`/api/studio/models?preset=${encodeURIComponent(preset)}`),

  library: (limit = 60, q = "") =>
    api<{ images: StudioImage[] }>(
      `/api/studio/library?limit=${limit}${q ? `&q=${encodeURIComponent(q)}` : ""}`,
    ),

  /** Tokenized <img src> for a generated file (path can't carry a header). */
  imageSrc: (name: string) => withToken(`/api/studio/image/${encodeURIComponent(name)}`),

  /** Start first-time setup; stream progress from streamSetup(job_id). */
  startSetup: (body: SetupBody) =>
    api<StartedJob>(`/api/studio/setup`, { method: "POST", body: JSON.stringify(body) }),

  streamSetup: (jobId: string, h: JobHandlers<{ family: string; model_id: string }>) =>
    streamJob(`/api/studio/setup/progress/${jobId}`, h),

  /** Start a generation as a job with live progress. */
  startGenerate: (body: GenerateBody) =>
    api<StartedJob>(`/api/studio/generate/stream`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  streamGenerate: (jobId: string, h: JobHandlers<GenerateResult>) =>
    streamJob(`/api/studio/generate/progress/${jobId}`, h),

  // --- Text mode: prompt an installed local LLM ---------------------------
  /** Installed LLM bundles you can test (warm = engine already running). */
  textModels: () => api<{ models: TextModel[] }>(`/api/studio/text/models`),

  /** Text engines currently loaded on a GPU (live status overview). */
  textEngines: () => api<{ engines: TextEngine[] }>(`/api/studio/text/engines`),

  /** Unload a running text engine — frees its GPU VRAM. */
  textUnload: (bundle_id: string) =>
    api<{ ok: boolean; bundle_id: string }>(`/api/studio/text/unload`, {
      method: "POST",
      body: JSON.stringify({ bundle_id }),
    }),

  /** Start a text generation job; stream it via streamText(job_id). */
  startTextGenerate: (body: TextGenerateBody) =>
    api<StartedJob>(`/api/studio/text/generate`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Text jobs share the generation progress SSE (loading → generating → done). */
  streamText: (jobId: string, h: JobHandlers<TextGenerateResult>) =>
    streamJob(`/api/studio/generate/progress/${jobId}`, h),

  /** Start a streaming chat turn; tokens arrive as {phase:"token"} progress
   * events on the shared SSE, terminal `done` carries the full reply. */
  startTextChat: (body: TextChatBody) =>
    api<StartedJob>(`/api/studio/text/chat`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Cancel a running image generation (interrupts ComfyUI). */
  cancelGenerate: (jobId: string) =>
    api<{ cancelled: boolean }>(`/api/studio/generate/cancel/${jobId}`, {
      method: "POST",
    }),
};
