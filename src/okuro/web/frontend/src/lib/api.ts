/**
 * Typed API client for the okuro orchestrator.
 *
 * Talks directly to the FastAPI backend (same origin in prod,
 * proxied via Vite in dev).  Bearer token auth for mutations.
 */

import type {
  TaskSummary,
  TaskDetail,
  TaskState,
  LogEntry,
  ArtifactInfo,
  DeliveryInfo,
  ActivityEvent,
  CreateTaskRequest,
  CreateTaskResponse,
  ContinueTaskRequest,
  PhaseSummary,
  SystemStatus,
  RoleInfo,
  RoleDetail,
  RoleKnowledgeEntry,
  RoleMaintenanceSummary,
  MaintenanceJob,
  MaintenanceBulkResponse,
  ToolCatalog,
  FlowSummary,
  FlowDetail,
  RolePlacement,
  RecurringDef,
  TaskKind,
  TaskTier,
  PanelRole,
  PositionSummary,
  DiscussionState,
  AuthorityEntry,
  GraphNode,
  GraphEdge,
  DeliberationStrategy,
  OnboardingState,
  DetectionResult,
  DesignOption,
  QuestionnaireItem,
  InferenceCli,
  CliState,
  CliActionResult,
  ConsumersResponse,
  PreviewState,
  CapabilityGap,
} from "@/types/api";

// -- Auth --

let _token: string | null = null;

export async function getToken(force = false): Promise<string> {
  if (_token && !force) return _token;
  _token = null;
  const res = await fetch("/api/auth/token");
  if (!res.ok) throw new ApiError("Failed to fetch auth token", res.status);
  const data = (await res.json()) as { token: string };
  _token = data.token;
  return _token;
}

/** Synchronously read the cached bearer (warm after the first authed call).
 *  Used to tokenize `<img src>` URLs that can't carry an Authorization header. */
export function cachedToken(): string {
  return _token ?? "";
}

async function authHeaders(): Promise<Record<string, string>> {
  const token = await getToken();
  return { Authorization: `Bearer ${token}` };
}

// -- External URL routing --

/**
 * Open ``url`` in the user's default system browser, working around
 * pywebview.
 *
 * pywebview's GTK / Cocoa / Qt backends silently drop
 * ``window.open(url, "_blank")`` and ``<a target="_blank">`` clicks
 * unless the host wires a backend-specific new-window handler. We
 * route through a Python js_api bridge instead (web_launcher.py
 * ``_WindowApi.open_external``), which uses ``webbrowser.open``.
 *
 * In a real browser ``window.pywebview`` is undefined, so we fall back
 * to plain ``window.open`` — the regular OS browser handles it.
 *
 * Same-origin ``blob:`` URLs are NOT external — pywebview can render
 * them inline, and ``open_in_browser`` would refuse the scheme. Caller
 * keeps using ``window.open`` for those (see ``previewApi.openResult``).
 */
export async function openExternal(url: string): Promise<void> {
  if (window.pywebview?.api?.open_external) {
    try {
      await window.pywebview.api.open_external(url);
      return;
    } catch {
      // Fall through to window.open as a best-effort backup. In normal
      // operation the bridge call returns false (not throws) on refusal,
      // but if the backend is wedged we'd rather try the (likely-dropped)
      // window.open than do nothing.
    }
  }
  window.open(url, "_blank", "noopener,noreferrer");
}

// -- Fetch helpers --

export class ApiError extends Error {
  status: number;
  details?: unknown;
  constructor(message: string, status: number, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

export async function api<T>(
  endpoint: string,
  opts?: RequestInit,
  _retried = false,
): Promise<T> {
  // All /api/* endpoints (except /api/auth/token and /api/health) require
  // bearer auth — see audit(C7).
  const auth = await authHeaders();

  // Don't force Content-Type for FormData — the browser sets it with the
  // multipart boundary. Same for Blob/ArrayBuffer direct uploads.
  const isFormLike =
    typeof FormData !== "undefined" && opts?.body instanceof FormData;
  const res = await fetch(endpoint, {
    ...opts,
    headers: {
      ...(isFormLike ? {} : { "Content-Type": "application/json" }),
      ...auth,
      ...opts?.headers,
    },
  });

  // Auto-recover from token rotation
  if (res.status === 401 && !_retried) {
    await getToken(true);
    return api<T>(endpoint, opts, true);
  }

  if (!res.ok) {
    let detail: unknown;
    try {
      detail = await res.json();
    } catch {
      detail = res.statusText;
    }
    // FastAPI returns string detail for HTTPException, but an ARRAY of
    // {loc, msg, type, input} for Pydantic validation errors. Naively
    // String()'ing the array yields "[object Object]" — useless. Flatten
    // validation errors into a readable "field: msg; field: msg" string.
    let msg = res.statusText;
    if (typeof detail === "object" && detail !== null) {
      const rec = detail as Record<string, unknown>;
      const inner = rec.detail;
      if (typeof inner === "string") {
        msg = inner;
      } else if (Array.isArray(inner)) {
        msg = inner
          .map((item) => {
            if (typeof item === "string") return item;
            if (item && typeof item === "object") {
              const e = item as { loc?: unknown[]; msg?: string };
              const where = Array.isArray(e.loc)
                ? e.loc.slice(1).join(".")
                : "";
              return where ? `${where}: ${e.msg ?? ""}` : (e.msg ?? "");
            }
            return String(item);
          })
          .filter(Boolean)
          .join("; ") || res.statusText;
      } else if (inner !== undefined) {
        msg = JSON.stringify(inner);
      }
    }
    throw new ApiError(msg, res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return res.json();
}

/** A bearer-gated route whose body is TEXT, not JSON — an emitted stylesheet,
    an artifact's raw content. Exported because the settings design tab reads a
    kit's sheet to draw its swatches. */
export async function apiText(endpoint: string): Promise<string> {
  const auth = await authHeaders();
  const res = await fetch(endpoint, { headers: auth });
  if (!res.ok) throw new ApiError(res.statusText, res.status);
  return res.text();
}

function qs(params?: Record<string, string | number | boolean | undefined>): string {
  if (!params) return "";
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined) q.append(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

// -- Task API --

export const taskApi = {
  list: (params?: { status?: string; limit?: number }) =>
    api<{ tasks: TaskSummary[]; total: number }>(`/api/tasks${qs(params)}`),

  get: (id: string) => api<TaskDetail>(`/api/tasks/${id}`),

  getState: (id: string) => api<TaskState>(`/api/tasks/${id}/state`),

  getAwaiting: (id: string) =>
    api<{ task_id: string; status?: string; awaiting: import("@/types/api").AwaitingState | null }>(
      `/api/tasks/${id}/awaiting`,
    ),

  getSnapshot: (id: string) =>
    api<import("@/types/api").TaskSnapshot>(`/api/tasks/${id}/snapshot`),

  getLogs: (id: string, limit = 100) =>
    api<{ task_id: string; logs: LogEntry[] }>(
      `/api/tasks/${id}/logs?limit=${limit}`,
    ),

  getArtifacts: (id: string) =>
    api<{ task_id: string; artifacts: ArtifactInfo[] }>(
      `/api/tasks/${id}/artifacts`,
    ),

  getDeliveries: (id: string) =>
    api<{ task_id: string; deliveries: DeliveryInfo[] }>(
      `/api/tasks/${id}/deliveries`,
    ),

  getDelivery: (deliveryId: string) =>
    api<DeliveryInfo & { body?: string | null }>(
      `/api/deliveries/${encodeURIComponent(deliveryId)}`,
    ),

  getArtifact: (id: string, name: string) =>
    apiText(`/api/tasks/${id}/artifacts/${encodeURIComponent(name)}`),

  /**
   * Fetch a binary artifact as a Blob. The browser won't attach an
   * Authorization header to `<img src>` / `<video src>` / `<iframe src>`
   * / `<a href download>`, so bearer-gated artifacts can't be loaded
   * directly by URL. Fetching here (with bearer) + converting to an
   * object URL lets those tags render normally from a blob: origin.
   * Caller owns the returned URL: `URL.revokeObjectURL` on unmount.
   */
  getArtifactBlob: async (id: string, name: string): Promise<Blob> => {
    const path = `/api/tasks/${id}/artifacts/${encodeURIComponent(name)}`;
    const send = async (token: string) =>
      fetch(path, { headers: { Authorization: `Bearer ${token}` } });
    let token = await getToken();
    let res = await send(token);
    if (res.status === 401) {
      token = await getToken(true);
      res = await send(token);
    }
    if (!res.ok) throw new ApiError(res.statusText, res.status);
    return res.blob();
  },

  getActivity: (id: string, limit = 20000) =>
    api<{ task_id: string; events: ActivityEvent[] }>(
      `/api/tasks/${id}/activity?limit=${limit}`,
    ),

  // Durable, authoritative review history from the task_events store —
  // verdict + load-bearing findings + scorer rubric for every review pass,
  // independent of the best-effort activity feed.
  getReview: (id: string, limit = 50) =>
    api<import("@/types/api").TaskReviewResponse>(
      `/api/tasks/${id}/review?limit=${limit}`,
    ),

  // F3 — post-flow rating. getFeedback 404s when the flow was never rated;
  // callers treat that as "unrated" rather than an error.
  getFeedback: (id: string) =>
    api<import("@/types/api").FlowFeedback>(`/api/tasks/${id}/feedback`),
  submitFeedback: (id: string, body: import("@/types/api").FlowFeedbackSubmit) =>
    api<import("@/types/api").FlowFeedback>(`/api/tasks/${id}/feedback`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  create: (
    data: CreateTaskRequest,
    opts?: {
      files?: File[];
      onProgress?: (loaded: number, total: number) => void;
    },
  ): Promise<CreateTaskResponse> => {
    const files = opts?.files ?? [];
    if (files.length === 0) {
      return api<CreateTaskResponse>("/api/tasks", {
        method: "POST",
        body: JSON.stringify(data),
      });
    }
    // Multipart path — files present. Switch to XHR so we get upload progress.
    return (async () => {
      const form = new FormData();
      form.append("description", data.description);
      if (data.mode) form.append("mode", data.mode);
      form.append("dry_run", String(data.dry_run ?? false));
      form.append("auto_approve", String(data.auto_approve ?? false));
      if (data.preferred_cli) form.append("preferred_cli", data.preferred_cli);
      if (data.intelligence) form.append("intelligence", data.intelligence);
      if (data.review_trigger)
        form.append("review_trigger", data.review_trigger);
      if (data.required_roles?.length)
        form.append("required_roles", data.required_roles.join(","));
      if (data.source_todo_id)
        form.append("source_todo_id", data.source_todo_id);
      if (data.source_thought_id)
        form.append("source_thought_id", data.source_thought_id);
      if (data.skip_intake) form.append("skip_intake", "true");
      // P5.4 — sent only as a complete instruction. A channel with no
      // recipient describes a delivery that will never happen, and the
      // backend drops it; not sending it keeps both sides agreeing on what
      // was asked for.
      if (data.fast_track) form.append("fast_track", "true");
      if (data.audience) {
        form.append("audience", data.audience);
        if (data.delivery_channel)
          form.append("delivery_channel", data.delivery_channel);
        if (data.delivery_brand_id)
          form.append("delivery_brand_id", data.delivery_brand_id);
      }
      if (data.autopilot != null)
        form.append("autopilot", String(data.autopilot));
      files.forEach((f, i) => form.append(`file_${i}`, f, f.name));

      const send = (token: string) =>
        new Promise<{ status: number; body: unknown }>((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open("POST", "/api/tasks");
          xhr.responseType = "json";
          xhr.setRequestHeader("Authorization", `Bearer ${token}`);
          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) opts?.onProgress?.(e.loaded, e.total);
          };
          xhr.onload = () => resolve({ status: xhr.status, body: xhr.response });
          xhr.onerror = () => reject(new ApiError("Network error", 0));
          xhr.onabort = () => reject(new ApiError("Upload aborted", 0));
          xhr.send(form);
        });

      let token = await getToken();
      let { status, body } = await send(token);
      if (status === 401) {
        token = await getToken(true);
        ({ status, body } = await send(token));
      }
      if (status < 200 || status >= 300) {
        const detail =
          body && typeof body === "object" && "detail" in body
            ? String((body as Record<string, unknown>).detail)
            : `HTTP ${status}`;
        throw new ApiError(detail, status, body);
      }
      return body as CreateTaskResponse;
    })();
  },

  preview: (data: { description: string; required_roles?: string[] }) =>
    api<{
      phases: PhaseSummary[];
      required_roles: string[];
      description: string;
    }>("/api/tasks/preview", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  continue: (
    id: string,
    data: ContinueTaskRequest,
    opts?: {
      files?: File[];
      onProgress?: (loaded: number, total: number) => void;
    },
  ): Promise<CreateTaskResponse> => {
    const files = opts?.files ?? [];
    if (files.length === 0) {
      return api<CreateTaskResponse>(`/api/tasks/${id}/continue`, {
        method: "POST",
        body: JSON.stringify(data),
      });
    }
    // Multipart path — files present. Mirrors taskApi.create: XHR for upload
    // progress; backend saves files under tasks/{id}/inputs/ and appends an
    // "Attached files:" block to the continuation prompt.
    return (async () => {
      const form = new FormData();
      form.append("description", data.description);
      form.append("dry_run", String(data.dry_run ?? false));
      form.append("auto_approve", String(data.auto_approve ?? false));
      if (data.preferred_cli) form.append("preferred_cli", data.preferred_cli);
      if (data.intelligence) form.append("intelligence", data.intelligence);
      if (data.review_trigger)
        form.append("review_trigger", data.review_trigger);
      files.forEach((f, i) => form.append(`file_${i}`, f, f.name));

      const send = (token: string) =>
        new Promise<{ status: number; body: unknown }>((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open("POST", `/api/tasks/${id}/continue`);
          xhr.responseType = "json";
          xhr.setRequestHeader("Authorization", `Bearer ${token}`);
          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) opts?.onProgress?.(e.loaded, e.total);
          };
          xhr.onload = () => resolve({ status: xhr.status, body: xhr.response });
          xhr.onerror = () => reject(new ApiError("Network error", 0));
          xhr.onabort = () => reject(new ApiError("Upload aborted", 0));
          xhr.send(form);
        });

      let token = await getToken();
      let { status, body } = await send(token);
      if (status === 401) {
        token = await getToken(true);
        ({ status, body } = await send(token));
      }
      if (status < 200 || status >= 300) {
        const detail =
          body && typeof body === "object" && "detail" in body
            ? String((body as Record<string, unknown>).detail)
            : `HTTP ${status}`;
        throw new ApiError(detail, status, body);
      }
      return body as CreateTaskResponse;
    })();
  },

  resume: (id: string) =>
    api<CreateTaskResponse>(`/api/tasks/${id}/resume`, { method: "POST" }),

  retry: (id: string) =>
    api<CreateTaskResponse & { subtasks_reset: number }>(
      `/api/tasks/${id}/retry`,
      { method: "POST" },
    ),

  cancel: (id: string) =>
    api<{ status: string; task_id: string; killed_pids: number[] }>(
      `/api/tasks/${id}/cancel`,
      { method: "POST" },
    ),

  delete: (id: string, force = false) =>
    api<{ status: string; task_id: string }>(
      `/api/tasks/${id}${force ? "?force=true" : ""}`,
      { method: "DELETE" },
    ),

  rename: (id: string, description: string) =>
    api<{ status: string; task_id: string; description: string }>(
      `/api/tasks/${id}`,
      { method: "PATCH", body: JSON.stringify({ description }) },
    ),

  deleteIntervention: (id: string, interventionId: string) =>
    api<{ status: string; task_id: string; intervention_id: string }>(
      `/api/tasks/${id}/interventions/${interventionId}`,
      { method: "DELETE" },
    ),

  setIntelligence: (id: string, intelligence: string) =>
    api<{ status: string; task_id: string; intelligence: string }>(
      `/api/tasks/${id}/intelligence`,
      { method: "PUT", body: JSON.stringify({ intelligence }) },
    ),

  setProjectPath: (id: string, projectPath: string) =>
    api<{ status: string; task_id: string; project_path: string }>(
      `/api/tasks/${id}/project-path`,
      { method: "PUT", body: JSON.stringify({ project_path: projectPath }) },
    ),

  /**
   * Ranked candidate project_paths for the picker UI. Used when a task
   * has no declared project_path (legacy tasks, or subagents that
   * didn't set brief['project_path']). The user picks one explicitly
   * via setProjectPath — there is NO auto-apply.
   */
  projectPathSuggestions: (id: string, topN = 3) =>
    api<{
      suggestions: Array<{
        path: string;
        hits: number;
        build_markers: string[];
        has_git: boolean;
      }>;
    }>(`/api/tasks/${id}/project-path/suggestions?top_n=${topN}`),

  setSubtaskModel: (id: string, subtaskId: string, model: string) =>
    api<{ status: string; task_id: string; subtask_id: string; model: string }>(
      `/api/tasks/${id}/subtasks/${subtaskId}/model`,
      { method: "PATCH", body: JSON.stringify({ model }) },
    ),

  approve: (
    id: string,
    subtaskId: string,
    action: "approve" | "skip" | "reject",
  ) =>
    api<{ status: string; subtask_id: string; action: string }>(
      `/api/tasks/${id}/approve`,
      { method: "POST", body: JSON.stringify({ subtask_id: subtaskId, action }) },
    ),

  suggest: (id: string) =>
    api<{
      task_id: string;
      suggestion: string;
      suggestions: Array<{
        id: string;
        suggestion_text: string;
        source_role: string;
        category: string;
        effort: string;
        timestamp: string;
        dismissed: boolean;
      }>;
    }>(`/api/tasks/${id}/suggest`, {
      method: "POST",
    }),
};

// -- Deliberation API --

export const deliberationApi = {
  getProposedPanel: (taskId: string) =>
    api<PanelRole[]>(`/api/tasks/${taskId}/proposed-panel`),

  confirmPanel: (
    taskId: string,
    roles: string[],
    strategy: DeliberationStrategy = "parallel",
  ) =>
    api<{
      status: string;
      task_id: string;
      discussion_node: string;
      position_nodes: string[];
      strategy: string;
    }>(`/api/tasks/${taskId}/panel`, {
      method: "POST",
      body: JSON.stringify({ roles, strategy }),
    }),

  getCapabilityGap: async (taskId: string): Promise<CapabilityGap | null> => {
    try {
      return await api<CapabilityGap>(`/api/tasks/${taskId}/capability-gap`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) return null;
      throw err;
    }
  },

  acceptCapabilityGap: (taskId: string, creator_roles?: string[]) =>
    api<{
      status: string;
      task_id: string;
      phase0_node_ids: string[];
      discussion_id: string;
    }>(`/api/tasks/${taskId}/capability-gap/accept`, {
      method: "POST",
      body: JSON.stringify(creator_roles ? { creator_roles } : {}),
    }),

  addRole: (taskId: string, role: string) =>
    api<{
      status: string;
      discussion_node: string;
      position_node: string;
      role: string;
    }>(`/api/tasks/${taskId}/panel/add`, {
      method: "POST",
      body: JSON.stringify({ role }),
    }),

  removeRole: (taskId: string, role: string) =>
    api<{
      status: string;
      discussion_node: string;
      dismissed_node: string;
      role: string;
    }>(`/api/tasks/${taskId}/panel/remove`, {
      method: "POST",
      body: JSON.stringify({ role }),
    }),

  getPositions: (taskId: string, round?: number) =>
    api<PositionSummary[]>(
      `/api/tasks/${taskId}/positions${round ? `?round=${round}` : ""}`,
    ),

  getDiscussion: (taskId: string, nodeId: string) =>
    api<DiscussionState>(`/api/tasks/${taskId}/discussion/${nodeId}`),

  submitAssignments: (
    taskId: string,
    nodeId: string,
    assignments: Array<{
      role: string;
      action: string;
      leads: string;
      reasoning: string;
    }>,
  ) =>
    api<{ status: string; node: string; count: number }>(
      `/api/tasks/${taskId}/discussion/${nodeId}/assign`,
      { method: "POST", body: JSON.stringify({ assignments }) },
    ),

  submitStatement: (taskId: string, nodeId: string, statement: string) =>
    api<{ status: string; node: string }>(
      `/api/tasks/${taskId}/discussion/${nodeId}/statement`,
      { method: "POST", body: JSON.stringify({ statement }) },
    ),

  proceed: (taskId: string, nodeId: string) =>
    api<{ status: string; node: string; assignments: number }>(
      `/api/tasks/${taskId}/discussion/${nodeId}/proceed`,
      { method: "POST" },
    ),

  recouncil: (taskId: string, nodeId: string, roles?: string[]) =>
    api<{
      status: string;
      old_discussion: string;
      new_discussion: string;
      round: number;
      position_nodes: string[];
    }>(`/api/tasks/${taskId}/discussion/${nodeId}/recouncil`, {
      method: "POST",
      body: JSON.stringify({ roles: roles ?? null }),
    }),

  getAuthorityMap: (taskId: string) =>
    api<AuthorityEntry[]>(`/api/tasks/${taskId}/authority-map`),

  getGraph: (taskId: string) =>
    api<{ nodes: GraphNode[]; edges: GraphEdge[] }>(
      `/api/tasks/${taskId}/graph`,
    ),
};

// -- M1 Decision Gates API --

export interface GateOption {
  id: string;
  label: string;
  description: string;
  pros: string;
  cons: string;
  risk: string;
  recommended: boolean;
}

export interface DecisionGateInfo {
  gate_id: string;
  phase_id: number;
  phase_name: string;
  status: "pending" | "resolved" | "skipped";
  prompt: string;
  options: GateOption[];
  selected_option_id: string;
  selected_rationale: string;
  resolved_at: string;
}

export interface GatesListResponse {
  task_id: string;
  gates_enabled: boolean;
  gates: DecisionGateInfo[];
}

export interface GateResolveResponse {
  status: "resolved" | "skipped";
  task_id: string;
  gate_id: string;
  phase_id: number;
  adr: Record<string, unknown>;
}

export const gatesApi = {
  list: (taskId: string) =>
    api<GatesListResponse>(`/api/tasks/${taskId}/gates`),

  resolve: (
    taskId: string,
    gateId: string,
    body: {
      selected_option_id?: string;
      rationale?: string;
      skipped?: boolean;
    },
  ) =>
    api<GateResolveResponse>(
      `/api/tasks/${taskId}/gates/${gateId}/resolve`,
      { method: "POST", body: JSON.stringify(body) },
    ),
};

// -- Other APIs --

export const recurringApi = {
  list: () => api<{ definitions: RecurringDef[] }>("/api/recurring"),
  history: (defId: string) =>
    api<{
      id: string;
      outcomes: Array<{
        run_id: string;
        outcome: string;
        summary: string;
        completed_at: string;
      }>;
    }>(`/api/recurring/${defId}/history`),
  patch: (defId: string, data: Record<string, unknown>) =>
    api<{ status: string; id: string; fields: string[] }>(
      `/api/recurring/${defId}`,
      { method: "PATCH", body: JSON.stringify(data) },
    ),
  create: (data: {
    title: string;
    description: string;
    role?: string;
    roles?: string[];
    schedule: string;
  }) =>
    api<{ status: string; id: string; title: string; schedule: string }>(
      "/api/recurring",
      { method: "POST", body: JSON.stringify(data) },
    ),
  remove: (defId: string) =>
    api<{ status: string; id: string }>(`/api/recurring/${defId}`, {
      method: "DELETE",
    }),
};

// systemd --user .timer units (Layer C of the schedule overview). Timing
// (OnCalendar) lives in the unit file so it is read-only here; enable/disable
// toggles the timer live via `systemctl --user enable/disable --now`.
export interface SystemTimer {
  unit: string;
  description: string;
  active: boolean;
  enabled: boolean;
  state: string;
  schedule: string | null;
  next_run: string | null;
  last_run: string | null;
  activates: string | null;
  okuro_owned: boolean;
}

export const timersApi = {
  list: () => api<{ timers: SystemTimer[] }>("/api/timers"),
  toggle: (unit: string, enabled: boolean) =>
    api<{ ok: boolean; unit: string; enabled: boolean }>(
      `/api/timers/${encodeURIComponent(unit)}/toggle`,
      { method: "POST", body: JSON.stringify({ enabled }) },
    ),
};

// Daemon cron tasks (Health → Schedules). Writes persist an override to
// ~/.okuro/daemon/config.yaml and SIGHUP the daemon so the change applies
// without a restart; `applied=false` means the daemon was unreachable and
// the change takes effect on its next start.
export interface ScheduleTask {
  id: string;
  description: string;
  cron: string;
  handler: string;
  enabled: boolean;
  run_on_startup: boolean;
  timeout_seconds: number;
  last_run: string | null;
  next_run: string | null;
  kind: TaskKind;
  tier: TaskTier;
  embeds: boolean;
}

type ScheduleWriteResult = {
  ok: boolean;
  task: ScheduleTask | null;
  applied: boolean;
  warning: string | null;
};

export const schedulesApi = {
  patch: (id: string, data: { enabled?: boolean; cron?: string }) =>
    api<ScheduleWriteResult>(`/api/schedules/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  reset: (id: string) =>
    api<ScheduleWriteResult>(`/api/schedules/${id}`, { method: "DELETE" }),
};

export const thoughtsApi = {
  setStatus: (id: string, status: "open" | "in_progress" | "resolved" | "dismissed") =>
    api<{ ok: boolean; message: string }>(
      `/api/dashboard/thought/${id}/status`,
      { method: "POST", body: JSON.stringify({ status }) },
    ),
};

export const systemApi = {
  health: () => api<{ status: string }>("/api/health"),
  status: () => api<SystemStatus>("/api/status"),
  tools: () => api<Record<string, unknown>>("/api/tools"),
  activeRoles: () =>
    api<{
      active: boolean;
      roles: Array<{
        task_id: string;
        subtask: string;
        role: string;
        description: string;
      }>;
    }>("/api/active-roles"),
};

export const roleApi = {
  list: async (domain?: string) => {
    const roles = await api<RoleInfo[]>(
      domain ? `/api/roles?domain=${domain}` : "/api/roles",
    );
    return { roles, count: roles.length };
  },
  get: (id: string) => api<RoleDetail>(`/api/roles/${id}`),
  update: (id: string, data: Partial<RoleDetail>) =>
    api<{ status: string }>(`/api/roles/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  create: (data: {
    role_id: string;
    domain: string;
    description: string;
    tier?: string;
    model?: string;
  }) =>
    api<{ status: string }>("/api/roles", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    api<{ status: string }>(`/api/roles/${id}`, { method: "DELETE" }),
  promote: (id: string) =>
    api<{ status: string; role_id: string; from?: string; to?: string }>(
      `/api/roles/${id}/promote`,
      { method: "POST" },
    ),

  // -- Knowledge --
  listKnowledge: (
    id: string,
    opts: { min_confidence?: number; limit?: number; type?: string } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.min_confidence !== undefined)
      params.set("min_confidence", String(opts.min_confidence));
    if (opts.limit !== undefined) params.set("limit", String(opts.limit));
    if (opts.type) params.set("type", opts.type);
    const q = params.toString();
    return api<RoleKnowledgeEntry[]>(
      `/api/roles/${id}/knowledge${q ? `?${q}` : ""}`,
    );
  },
  addKnowledge: (
    id: string,
    data: {
      type: string;
      content: string;
      source_url?: string;
      confidence?: number;
      supersedes?: string;
    },
  ) =>
    api<{ id: string; status: string }>(`/api/roles/${id}/knowledge`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
  deleteKnowledge: (id: string, entryId: string) =>
    api<{ status: string; entry_id: string; soft: boolean }>(
      `/api/roles/${id}/knowledge/${entryId}`,
      { method: "DELETE" },
    ),

  // -- Maintenance --
  maintenanceAll: () =>
    api<RoleMaintenanceSummary[]>("/api/roles/maintenance"),
  mandatePreview: (id: string) =>
    api<{
      instructions?: string;
      research_mandate?: {
        searches?: string[];
        sources_to_check?: string[];
        current_knowledge_summary?: string;
      };
      [key: string]: unknown;
    }>(`/api/roles/${id}/maintenance/mandate`),
  runMaintenance: (id: string) =>
    api<{
      job_id: string;
      status: string;
      started_at: string;
      role_id: string;
      task_id: string | null;
    }>(`/api/roles/${id}/maintenance/run`, { method: "POST" }),
  runAllStale: () =>
    api<MaintenanceBulkResponse>(
      "/api/roles/maintenance/run-all-stale",
      { method: "POST" },
    ),
  maintenanceStatus: (jobId?: string) => {
    if (jobId) {
      return api<MaintenanceJob>(
        `/api/roles/maintenance/status?job_id=${jobId}`,
      );
    }
    return api<{ jobs: MaintenanceJob[]; count: number }>(
      "/api/roles/maintenance/status",
    );
  },
};

export const toolApi = {
  catalog: () => api<ToolCatalog>("/api/tools/catalog"),
};

export const flowApi = {
  list: () => api<{ flows: FlowSummary[] }>("/api/flows"),
  get: (id: string) => api<FlowDetail>(`/api/flows/${id}`),
  create: (data: {
    id?: string;
    name: string;
    description?: string;
    roles: RolePlacement[];
  }) =>
    api<FlowDetail>("/api/flows", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  update: (id: string, data: {
    name: string;
    description?: string;
    roles: RolePlacement[];
  }) =>
    api<FlowDetail>(`/api/flows/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    api<{ ok: boolean }>(`/api/flows/${id}`, { method: "DELETE" }),
};

// -- okuro-flow (generic node-graph canvas; separate from flowApi above) --

export interface FlowDesignerSummary {
  id: string;
  name: string;
  description: string;
  node_count: number;
  edge_count: number;
  rev: number;
  /** owning folder, or null when ungrouped */
  folder_id: string | null;
  created_at: string;
  updated_at: string;
}
export interface FlowFolder {
  id: string;
  name: string;
  parent_id: string | null;
  created_at: string;
  updated_at: string;
}
export interface FlowDesignerGraph {
  nodes: unknown[];
  edges: unknown[];
  viewport?: unknown;
  settings?: unknown;
}
export interface FlowDesignerDetail extends FlowDesignerSummary {
  graph: FlowDesignerGraph;
}
export interface FlowDesignerSavePayload {
  name: string;
  description?: string;
  graph: FlowDesignerGraph;
  origin?: string;
  /** rev the client loaded — server rejects (409) if it moved on (clobber guard). */
  base_rev?: number;
}
export interface FlowDesignerHistoryEntry {
  seq: number;
  flow_id: string;
  node_count: number;
  edge_count: number;
  rev: number;
  origin: string;
  ts: string;
}

export const flowDesignerApi = {
  list: () => api<{ flows: FlowDesignerSummary[] }>("/api/flow-designer"),
  get: (id: string) => api<FlowDesignerDetail>(`/api/flow-designer/${id}`),
  create: (data: FlowDesignerSavePayload) =>
    api<FlowDesignerDetail>("/api/flow-designer", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  upsert: (id: string, data: FlowDesignerSavePayload) =>
    api<FlowDesignerDetail>(`/api/flow-designer/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  remove: (id: string, origin?: string) =>
    api<{ ok: boolean }>(
      `/api/flow-designer/${id}?origin=${encodeURIComponent(origin || "")}`,
      { method: "DELETE" },
    ),
  history: (id: string) =>
    api<{ history: FlowDesignerHistoryEntry[] }>(
      `/api/flow-designer/${id}/history`,
    ),
  restore: (id: string, seq: number, origin?: string) =>
    api<FlowDesignerDetail>(`/api/flow-designer/${id}/restore`, {
      method: "POST",
      body: JSON.stringify({ seq, origin }),
    }),
  /** Chat-to-draw: turn a prompt into a flow graph, STREAMING live inference
   *  activity. Persists server-side (origin "agent-chat") so the open canvas
   *  live-syncs over SSE; resolves with the saved flow summary. */
  chat: async (
    body: { prompt: string; id?: string | null; name?: string; graph?: unknown },
    onActivity?: (msg: string) => void,
  ): Promise<{ flow: FlowDesignerSummary; url: string; node_count: number; edge_count: number }> => {
    const token = await getToken();
    const res = await fetch("/api/flow-designer/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(body),
    });
    if (!res.ok || !res.body) throw new Error(`chat ${res.status}`);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let done: { flow: FlowDesignerSummary; url: string; node_count: number; edge_count: number } | null = null;
    for (;;) {
      const { done: fin, value } = await reader.read();
      if (fin) break;
      buf += dec.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const lineStr = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!lineStr) continue;
        let ev: any;
        try {
          ev = JSON.parse(lineStr);
        } catch {
          continue;
        }
        if (ev.type === "activity") onActivity?.(ev.msg);
        else if (ev.type === "done") done = ev;
        else if (ev.type === "error") throw new Error(ev.error);
      }
    }
    if (!done) throw new Error("chat ended without a result");
    return done;
  },
  // --- folders (gallery organisation) ---
  folders: () => api<{ folders: FlowFolder[] }>("/api/flow-designer/folders"),
  createFolder: (name: string, parent_id?: string | null) =>
    api<FlowFolder>("/api/flow-designer/folders", {
      method: "POST",
      body: JSON.stringify({ name, parent_id: parent_id ?? null }),
    }),
  updateFolder: (id: string, data: { name?: string; parent_id?: string | null }) =>
    api<FlowFolder>(`/api/flow-designer/folders/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  removeFolder: (id: string) =>
    api<{ ok: boolean }>(`/api/flow-designer/folders/${id}`, { method: "DELETE" }),
  move: (id: string, folder_id: string | null, origin?: string) =>
    api<FlowDesignerSummary>(`/api/flow-designer/${id}/move`, {
      method: "POST",
      body: JSON.stringify({ folder_id, origin }),
    }),
};

// -- drawn orchestrator workflows (/workflows) --
//
// The SECOND kind of okuro workflow: a node graph arranged by hand and compiled
// to a plan, no LLM. Same store mechanism as okuro-flow above, separate tables,
// separate surface — /flow stays a standalone visualizer. The one endpoint with
// no flow-designer twin is `compile`.

export interface WorkflowSummary {
  id: string;
  name: string;
  description: string;
  node_count: number;
  edge_count: number;
  rev: number;
  /** owning folder, or null when ungrouped */
  folder_id: string | null;
  created_at: string;
  updated_at: string;
}
export interface WorkflowGraph {
  nodes: unknown[];
  edges: unknown[];
  viewport?: unknown;
  settings?: unknown;
}
export interface WorkflowDetail extends WorkflowSummary {
  graph: WorkflowGraph;
}
export interface WorkflowSavePayload {
  name: string;
  description?: string;
  graph: WorkflowGraph;
  origin?: string;
  /** rev the client loaded — server rejects (409) if it moved on (clobber guard). */
  base_rev?: number;
}
export interface WorkflowHistoryEntry {
  seq: number;
  flow_id: string;
  node_count: number;
  edge_count: number;
  rev: number;
  origin: string;
  ts: string;
}

/** One subtask of a compiled plan — the compiler's output shape. */
export interface CompiledSubtask {
  id: string;
  role: string;
  description: string;
  risk: string;
  complexity: string;
  dependencies?: string[];
  artifact_name?: string;
  outputs?: string[];
  acceptance_criteria?: string[];
  /** provenance: the drawn node this subtask came from */
  _node_id?: string;
}
export interface CompiledPhase {
  id: number;
  name: string;
  serialize: boolean;
  subtasks: CompiledSubtask[];
}
export interface CompiledPlan {
  phases: CompiledPhase[];
}
export interface CompileRequest {
  params?: Record<string, string>;
  phases?: number[];
  fanout?: Record<string, Array<Record<string, unknown>>>;
}
/** Compilation is EXPECTED to fail while a workflow is being authored, so the
 *  failure is a value rather than an exception — showing the compiler's message
 *  to the author is the whole point of the endpoint. */
export type CompileResult =
  | { ok: true; plan: CompiledPlan; subtask_count: number }
  | { ok: false; error: string };

export const workflowApi = {
  list: () => api<{ workflows: WorkflowSummary[] }>("/api/workflows"),
  get: (id: string) => api<WorkflowDetail>(`/api/workflows/${id}`),
  create: (data: WorkflowSavePayload) =>
    api<WorkflowDetail>("/api/workflows", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  upsert: (id: string, data: WorkflowSavePayload) =>
    api<WorkflowDetail>(`/api/workflows/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  remove: (id: string, origin?: string) =>
    api<{ ok: boolean }>(
      `/api/workflows/${id}?origin=${encodeURIComponent(origin || "")}`,
      { method: "DELETE" },
    ),
  history: (id: string) =>
    api<{ history: WorkflowHistoryEntry[] }>(`/api/workflows/${id}/history`),
  restore: (id: string, seq: number, origin?: string) =>
    api<WorkflowDetail>(`/api/workflows/${id}/restore`, {
      method: "POST",
      body: JSON.stringify({ seq, origin }),
    }),
  /** Compile to an orchestrator plan, or return the FlowCompileError message.
   *  A 400 is an authoring mistake, not a transport failure — anything else
   *  (404, 500, offline) still throws. */
  compile: async (id: string, body: CompileRequest = {}): Promise<CompileResult> => {
    try {
      const res = await api<{ plan: CompiledPlan; subtask_count: number }>(
        `/api/workflows/${id}/compile`,
        { method: "POST", body: JSON.stringify(body) },
      );
      return { ok: true, ...res };
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) {
        const detail = e.details as { error?: string } | undefined;
        return { ok: false, error: detail?.error || e.message };
      }
      throw e;
    }
  },
  // --- folders (gallery organisation) ---
  folders: () => api<{ folders: FlowFolder[] }>("/api/workflows/folders"),
  createFolder: (name: string, parent_id?: string | null) =>
    api<FlowFolder>("/api/workflows/folders", {
      method: "POST",
      body: JSON.stringify({ name, parent_id: parent_id ?? null }),
    }),
  updateFolder: (id: string, data: { name?: string; parent_id?: string | null }) =>
    api<FlowFolder>(`/api/workflows/folders/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  removeFolder: (id: string) =>
    api<{ ok: boolean }>(`/api/workflows/folders/${id}`, { method: "DELETE" }),
  move: (id: string, folder_id: string | null, origin?: string) =>
    api<WorkflowSummary>(`/api/workflows/${id}/move`, {
      method: "POST",
      body: JSON.stringify({ folder_id, origin }),
    }),
};

export interface ChatTurn {
  role: "user" | "assistant";
  text: string;
}

export interface ChatPage {
  path: string;
  title: string;
  sections: string[];
  capabilities?: Array<{ name: string; describe: string }>;
}

export interface ChatActionEvent {
  name: string;
  target: string;
}

interface ChatHandlers {
  onActivity?: (msg: string) => void;
  onAction?: (action: ChatActionEvent) => void;
  onForm?: (form: { name: string; context: string }) => void;
}

/** General chat — streams NDJSON activity, resolves with the answer text.
 *  Mirrors flowDesignerApi.chat: `onActivity` fires per status line,
 *  `onAction` fires per UI action the agent emits (scroll/highlight). */
export interface NoteSummary {
  id: string;
  title: string;
  project: string | null;
  /** owning folder, or null when at the vault root */
  folder_id: string | null;
  /** 1 once someone renamed the note; 0 while the title tracks the first body
   *  line. Only `get` returns it — the list query does not select the column. */
  title_explicit?: number;
  pinned: number;
  archived: number;
  created_at: string;
  updated_at: string;
}

export interface NoteFolder {
  id: string;
  name: string;
  parent_id: string | null;
  sort: number;
  created_at?: string;
}

export interface NoteDetail extends NoteSummary {
  body: string;
  frontmatter: Record<string, unknown>;
}

export interface NoteSearchHit {
  id: string;
  title: string;
  project: string | null;
  updated_at: string;
  similarity?: number;
}

export interface NoteBacklink {
  id: string;
  title: string;
  link_text: string;
}

export interface NoteOutgoing {
  target_type: string;
  target_id: string | null;
  link_text: string;
}

export interface NoteSavePayload {
  title?: string;
  body?: string;
  frontmatter?: Record<string, unknown>;
  project?: string | null;
  /** present only when moving the note; omit to leave the folder unchanged */
  folder_id?: string | null;
  origin?: string;
}

export const notesApi = {
  list: (opts?: { project?: string; archived?: boolean }) =>
    api<{ notes: NoteSummary[]; count: number }>(
      `/api/notes?archived=${opts?.archived ? "true" : "false"}` +
        (opts?.project ? `&project=${encodeURIComponent(opts.project)}` : ""),
    ),
  get: (id: string) => api<{ note: NoteDetail }>(`/api/notes/${id}`),
  create: (data: NoteSavePayload) =>
    api<{ note: NoteDetail }>("/api/notes", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  update: (id: string, data: NoteSavePayload) =>
    api<{ note: NoteDetail }>(`/api/notes/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  remove: (id: string) =>
    api<{ success: boolean }>(`/api/notes/${id}`, { method: "DELETE" }),
  search: (q: string, project?: string) =>
    api<{ results: NoteSearchHit[]; count: number }>(
      `/api/notes/search?q=${encodeURIComponent(q)}` +
        (project ? `&project=${encodeURIComponent(project)}` : ""),
    ),
  backlinks: (id: string) =>
    api<{ backlinks: NoteBacklink[]; outgoing: NoteOutgoing[] }>(
      `/api/notes/${id}/backlinks`,
    ),
  graph: () =>
    api<{ nodes: { id: string; title: string }[]; edges: { source: string; target: string }[] }>(
      "/api/notes/graph",
    ),
  // --- folder tree ---
  folders: () => api<{ folders: NoteFolder[] }>("/api/notes/folders"),
  folderCreate: (name: string, parent_id?: string | null) =>
    api<{ folder: NoteFolder }>("/api/notes/folders", {
      method: "POST",
      body: JSON.stringify({ name, parent_id: parent_id ?? null }),
    }),
  folderUpdate: (id: string, data: { name?: string; parent_id?: string | null }) =>
    api<{ success: boolean }>(`/api/notes/folders/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),
  folderDelete: (id: string) =>
    api<{ success: boolean }>(`/api/notes/folders/${id}`, { method: "DELETE" }),
  /** Move a note into a folder (null = root). Cheap — no re-embed server-side. */
  setFolder: (id: string, folder_id: string | null) =>
    api<{ success: boolean }>(`/api/notes/${id}/folder`, {
      method: "PUT",
      body: JSON.stringify({ folder_id }),
    }),
  drawingsList: (noteId: string) =>
    api<{ drawings: { id: string; note_id: string | null; title: string; updated_at: string }[] }>(
      `/api/notes/drawings?note_id=${encodeURIComponent(noteId)}`,
    ),
  drawingGet: (id: string) =>
    api<{ drawing: { id: string; note_id: string | null; title: string; scene: Record<string, unknown> } }>(
      `/api/notes/drawings/${id}`,
    ),
  drawingSave: (data: {
    id?: string;
    note_id?: string | null;
    title?: string;
    scene: Record<string, unknown>;
    png_base64?: string;
  }) =>
    api<{ drawing: { id: string; note_id: string | null; title: string } }>(
      "/api/notes/drawings",
      { method: "POST", body: JSON.stringify(data) },
    ),
  /** Upload a pasted/dropped image; returns its id + embed url. Reference the
   *  url from the note body as ![alt](url) — it renders in preview + editor. */
  imageUpload: (file: File | Blob, noteId?: string | null) => {
    const form = new FormData();
    form.append("file", file, file instanceof File ? file.name : "pasted-image");
    if (noteId) form.append("note_id", noteId);
    return api<{ image: { id: string; url: string; mime: string } }>(
      "/api/notes/images",
      { method: "POST", body: form },
    );
  },
};

export const chatApi = {
  send: async (
    body: {
      prompt: string;
      history?: ChatTurn[];
      provider?: string;
      page?: ChatPage;
      conversation_id?: string;
    },
    handlers?: ChatHandlers,
  ): Promise<{ text: string; provider?: string; model?: string }> => {
    const token = await getToken();
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
    });
    if (!res.ok || !res.body) throw new Error(`chat ${res.status}`);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let answer: { text: string; provider?: string; model?: string } | null = null;
    for (;;) {
      const { done: fin, value } = await reader.read();
      if (fin) break;
      buf += dec.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const lineStr = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!lineStr) continue;
        let ev: {
          type?: string;
          msg?: string;
          text?: string;
          provider?: string;
          model?: string;
          error?: string;
          name?: string;
          target?: string;
          context?: string;
        };
        try {
          ev = JSON.parse(lineStr);
        } catch {
          continue;
        }
        if (ev.type === "activity" && ev.msg) handlers?.onActivity?.(ev.msg);
        else if (ev.type === "action" && ev.name && ev.target)
          handlers?.onAction?.({ name: ev.name, target: ev.target });
        else if (ev.type === "form" && ev.name)
          handlers?.onForm?.({ name: ev.name, context: ev.context ?? "" });
        else if (ev.type === "answer")
          answer = { text: ev.text ?? "", provider: ev.provider, model: ev.model };
        else if (ev.type === "error") throw new Error(ev.error ?? "chat failed");
      }
    }
    if (!answer) throw new Error("chat ended without an answer");
    return answer;
  },
};

export interface SuggestedEpic {
  id: string;
  title: string;
  durationDays: number;
  depends_on: string[];
}

export const ganttApi = {
  /** LLM-decompose a project description -> epic graph (P1), STREAMING the live
   *  inference activity. onActivity fires per event for the UI toast. */
  suggest: async (
    body: { name?: string; description: string; project?: string },
    onActivity?: (msg: string) => void,
  ): Promise<{ epics: SuggestedEpic[] }> => {
    const token = await getToken();
    const res = await fetch("/api/gantt/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(body),
    });
    if (!res.ok || !res.body) throw new Error(`suggest ${res.status}`);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let epics: SuggestedEpic[] = [];
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const lineStr = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!lineStr) continue;
        let ev: any;
        try {
          ev = JSON.parse(lineStr);
        } catch {
          continue;
        }
        if (ev.type === "activity") onActivity?.(ev.msg);
        else if (ev.type === "done") epics = ev.epics || [];
        else if (ev.type === "error") throw new Error(ev.error);
      }
    }
    return { epics };
  },
  /** RAG pre-fill: existing memory relevant to an epic (intake Tier 2). */
  context: (body: { query: string; project?: string }) =>
    api<{ context: string }>("/api/gantt/context", { method: "POST", body: JSON.stringify(body) }),
};

/** Transcribe a recorded audio clip via the backend (Groq Whisper). */
export async function transcribeAudio(blob: Blob): Promise<string> {
  const token = await getToken();
  // Groq sniffs container by extension/mime — derive both from the blob type.
  const ext = ((blob.type.split("/")[1] || "webm").split(";")[0]) || "webm";
  const fd = new FormData();
  fd.append("file", blob, `clip.${ext}`);
  const res = await fetch("/api/stt", { method: "POST", headers: { Authorization: `Bearer ${token}` }, body: fd });
  if (!res.ok) {
    let detail = "";
    try {
      detail = (await res.json()).detail || "";
    } catch {
      /* ignore */
    }
    throw new Error(`STT ${res.status}${detail ? " — " + detail : ""}`);
  }
  return ((await res.json()).text as string) || "";
}

/** Authed EventSource URL for the okuro-flow change-feed (bearer via query). */
export async function flowDesignerEventsUrl(): Promise<string> {
  const token = await getToken();
  return `/api/flow-designer/events?token=${encodeURIComponent(token)}`;
}

export const onboardingApi = {
  state: () => api<OnboardingState>("/api/onboarding/state"),
  detect: () => api<DetectionResult>("/api/onboarding/detect"),
  listClis: () => api<InferenceCli[]>("/api/onboarding/clis"),
  cliState: (toolId: string) =>
    api<CliState>(`/api/onboarding/clis/${toolId}/state`),
  installCli: (toolId: string) =>
    api<CliActionResult>(`/api/onboarding/clis/${toolId}/install`, {
      method: "POST",
    }),
  authenticateCli: (toolId: string) =>
    api<CliActionResult>(`/api/onboarding/clis/${toolId}/authenticate`, {
      method: "POST",
    }),
  /** Per-consumer install / auth / MCP-registration board (Settings). */
  consumers: () => api<ConsumersResponse>("/api/onboarding/consumers"),
  canonTargets: () =>
    api<{
      targets: Array<{ id: string; name: string; detected: boolean }>;
      mcp_servers: string[];
    }>("/api/onboarding/canon/targets"),
  canonDeploy: (targets?: string[]) =>
    api<Record<string, unknown>>("/api/onboarding/canon/deploy", {
      method: "POST",
      body: JSON.stringify(targets ? { targets } : {}),
    }),
  profile: () => api<Record<string, unknown>>("/api/onboarding/profile"),
  patchProfile: (patch: Record<string, unknown>) =>
    api<Record<string, unknown>>("/api/onboarding/profile", {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  complete: () =>
    api<OnboardingState>("/api/onboarding/complete", { method: "POST" }),
  designOptions: () =>
    api<DesignOption[]>("/api/onboarding/design/options"),
  extractDesign: (urls: string[], name?: string) =>
    api<DesignOption>("/api/onboarding/design/extract", {
      method: "POST",
      body: JSON.stringify({ urls, name }),
    }),
  questionnaire: () =>
    api<QuestionnaireItem[]>("/api/onboarding/questionnaire"),
  submitQuestionnaire: (
    answers: Record<string, string>,
    neurodivergence?: { disclosed: boolean; details?: string },
  ) =>
    api<Record<string, unknown>>("/api/onboarding/questionnaire", {
      method: "POST",
      body: JSON.stringify({ answers, neurodivergence }),
    }),
  initKeyring: (password: string) =>
    api<{ status: string; storage?: string; generated?: boolean }>(
      "/api/onboarding/keyring",
      {
        method: "POST",
        body: JSON.stringify({ password }),
      },
    ),
  initKeyringGenerated: () =>
    api<{ status: string; storage?: string; generated?: boolean }>(
      "/api/onboarding/keyring",
      {
        method: "POST",
        body: JSON.stringify({ generate: true }),
      },
    ),
  cognitiveTraits: () =>
    api<Array<{ id: number; question: string; option_a: string; option_b: string; trait: string }>>(
      "/api/onboarding/cognitive-traits",
    ),
  saveCognitiveTraits: (answers: Record<string, string>, neurodivergent = false) =>
    api<{ traits: Record<string, string>; implications: string[]; overrides: Record<string, unknown> }>(
      "/api/onboarding/cognitive-traits",
      {
        method: "POST",
        body: JSON.stringify({ answers, neurodivergent }),
      },
    ),
  principles: () =>
    api<{
      available: Array<{ id: string; name: string; description: string }>;
      selected: string[];
      custom: Array<{ id: string; name: string; description: string }>;
    }>("/api/onboarding/principles"),
  savePrinciples: (selected: string[], custom: Array<{ id: string; name: string; description: string }>) =>
    api<{ selected: string[]; custom: typeof custom }>("/api/onboarding/principles", {
      method: "POST",
      body: JSON.stringify({ selected, custom }),
    }),
  formulatePrinciple: (need: string) =>
    api<{ id: string; name: string; description: string }>("/api/onboarding/principles/formulate", {
      method: "POST",
      body: JSON.stringify({ need }),
    }),
  extractLinkedIn: async (file: File) => {
    const token = await getToken();
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/api/onboarding/sources/linkedin", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    });
    if (!res.ok) throw new ApiError(await res.text(), res.status);
    return res.json() as Promise<Record<string, unknown>>;
  },
  extractGitHub: (handle: string) =>
    api<Record<string, unknown>>("/api/onboarding/sources/github", {
      method: "POST",
      body: JSON.stringify({ handle }),
    }),
  extractUrl: (url: string, sourceType = "website") =>
    api<Record<string, unknown>>("/api/onboarding/sources/url", {
      method: "POST",
      body: JSON.stringify({ url, source_type: sourceType }),
    }),
  mergeSources: (extracts: Record<string, unknown>[]) =>
    api<Record<string, unknown>>("/api/onboarding/sources/merge", {
      method: "POST",
      body: JSON.stringify({ extracts }),
    }),
};

// -- Preview API (see-result button) --

export const previewApi = {
  status: (taskId: string) =>
    api<PreviewState>(`/api/preview/${encodeURIComponent(taskId)}`),

  start: (taskId: string, opts?: { force_rebuild?: boolean }) =>
    api<PreviewState>(`/api/preview/${encodeURIComponent(taskId)}/start`, {
      method: "POST",
      body: JSON.stringify(opts ?? {}),
    }),

  stop: (taskId: string) =>
    api<PreviewState>(`/api/preview/${encodeURIComponent(taskId)}/stop`, {
      method: "POST",
    }),

  /** One-shot tail of the unit's journal — N most recent lines. */
  logs: (taskId: string, lines = 200) =>
    api<{ lines: string[] }>(
      `/api/preview/${encodeURIComponent(taskId)}/logs?tail=${lines}`,
    ),

  /**
   * Open the preview's result in a new tab / system viewer.
   *
   * Two URL shapes flow through `state.url`:
   *   - Absolute `http(s)://…` (kind=web/static/api) — route via
   *     openExternal so pywebview hands off to the system browser.
   *   - Same-origin `/api/preview/{id}/file` (kind=doc/image/download) —
   *     bearer-gated. Two sub-paths:
   *       · Inside pywebview: append `?token=<bearer>` and route via
   *         openExternal so the OS opens the file in its default
   *         viewer (image viewer for PNG, PDF reader for PDF, etc.).
   *         pywebview's BearerAuthMiddleware accepts query-token auth
   *         specifically for this case (see main.py).
   *       · Real browser: fetch as a Blob (carrying the bearer in a
   *         header) and open the resulting `blob:` URL — keeps the
   *         token out of the browser history.
   */
  openResult: async (url: string): Promise<void> => {
    const isApiPath = url.startsWith("/api/");
    if (!isApiPath) {
      await openExternal(url);
      return;
    }
    // Directory-served docs (kind=doc with relative assets) carry their
    // capability token in the PATH — /api/preview/{id}/serve/{token}/… —
    // so the browser preserves it for every relative sub-resource (CSS,
    // JS, iframes). Open the URL directly; the blob path below resolves
    // relative refs against blob: and would render the doc unstyled.
    if (url.includes("/serve/")) {
      await openExternal(`${window.location.origin}${url}`);
      return;
    }
    if (window.pywebview?.api?.open_external) {
      const token = await getToken();
      const sep = url.includes("?") ? "&" : "?";
      const absolute = `${window.location.origin}${url}${sep}token=${encodeURIComponent(token)}`;
      await openExternal(absolute);
      return;
    }
    const send = async (token: string) =>
      fetch(url, { headers: { Authorization: `Bearer ${token}` } });
    let token = await getToken();
    let res = await send(token);
    if (res.status === 401) {
      token = await getToken(true);
      res = await send(token);
    }
    if (!res.ok) throw new ApiError(res.statusText, res.status);
    const blobUrl = URL.createObjectURL(await res.blob());
    window.open(blobUrl, "_blank", "noopener,noreferrer");
    // Browsers hold blob: URLs alive while referenced; revoke after the
    // popup loaded so memory can be reclaimed when the user closes the tab.
    setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
  },
};

// ── Embedding tier API ───────────────────────────────────────────────

export type EmbedGpu = {
  index: number;
  name: string;
  vram_gb: number;
  is_display: boolean;
};

export type EmbedDetection = {
  system: string;
  display_gpu_index: number | null;
  display_gpu_indices: number[];
  gpus: EmbedGpu[];
  cpu_cores: number;
  ram_gb: number;
};

export type EmbedConfigPayload = {
  tier: "low" | "high";
  device: string;
  chosen_at: string;
  chosen_by: "onboarding" | "web" | "cli" | "auto";
  last_detection: EmbedDetection | Record<string, never>;
};

export type EmbedTierOption = {
  tier: "low" | "high";
  model_id: string;
  dim: number;
  description: string;
};

export type EmbedTierOptions = {
  tiers: EmbedTierOption[];
  recommended: "low" | "high";
  recommended_device: string;
};

export type EmbedSwitchEstimate = {
  current_tier: "low" | "high";
  new_tier: "low" | "high";
  current_dim: number;
  new_dim: number;
  dim_changed: boolean;
  files_affected: number;
  eta_seconds: number | null;
  search_disabled_until_done: boolean;
};

export type EmbedSwitchResult = {
  tier: "low" | "high";
  device: string;
  chosen_at: string;
  chosen_by: "onboarding" | "web" | "cli";
  reindex_required: boolean;
  reindex_job_id: string | null;
};

export type EmbedHealth = {
  status: string;
  model?: string;
  tier?: string;
  loaded?: boolean;
  load_time_s?: number;
  dimensions?: number;
  detail?: string;
  url?: string;
};

export const embedApi = {
  getConfig: () => api<EmbedConfigPayload>("/api/embed/config"),
  detect:    () => api<EmbedDetection>("/api/embed/detect"),
  tiers:     () => api<EmbedTierOptions>("/api/embed/tiers"),
  estimate:  (tier: "low" | "high") =>
    api<EmbedSwitchEstimate>(`/api/embed/switch-estimate?tier=${tier}`),
  putConfig: (body: { tier: "low" | "high"; device: string; chosen_by: "onboarding" | "web" | "cli" }) =>
    api<EmbedSwitchResult>("/api/embed/config", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  health:    () => api<EmbedHealth>("/api/embed/health"),
};

// ── Voice / dictation STT tier ────────────────────────────────────────
export type SttTier = "air" | "plus" | "pro";

export type SttTierOption = {
  tier: SttTier;
  label: string;
  stream_model: string;
  batch_model: string;
  needs_gpu: boolean;
  characteristics: string;
  best_for: string;
};

export type SttTiersResponse = {
  tiers: SttTierOption[];
  recommended: SttTier; // highest tier this hardware can run
  recommended_device: string; // "cuda:1" | "cpu" | "mps"
  entitled: SttTier; // subscription ceiling
  effective: SttTier; // what dictation uses right now
  reason: "env-override" | "settings" | "auto";
  selected: SttTier | null; // saved choice, or null (= auto)
  stream_backend: string;
  batch_backend: string;
};

export type SttTierResult = {
  effective: SttTier;
  reason: "env-override" | "settings" | "auto";
  stream_backend: string;
  batch_backend: string;
};

export const sttApi = {
  tiers:   () => api<SttTiersResponse>("/api/voice/stt/tiers"),
  setTier: (tier: SttTier) =>
    api<SttTierResult>("/api/voice/stt/tier", {
      method: "PUT",
      body: JSON.stringify({ tier }),
    }),
};

// ── Voice / delivery TTS (podcast / summary / morning brief voices) ───
// One model, every engine: a standard female voice + a standard male voice, and
// `okuro_gender` saying which of the two okuro itself speaks with. Roles are
// derived server-side (narrator/host_a = okuro, host_b = the other), so there
// are no per-role fields to keep in sync here.
export type TtsGender = "female" | "male";
export type TtsEngine = "qwen" | "kokoro";

export type TtsVoiceOption = {
  id: string;
  label: string;
  gender: TtsGender;
  has_preview: boolean;
};

export type TtsPreviewStatus = {
  ready: boolean;
  pending: boolean;
  missing: string[];
  total: number;
};

export type TtsConfig = {
  sample_text: string;
  engine: TtsEngine;
  edition: string;
  female: string;
  male: string;
  okuro_gender: TtsGender;
  speed: number;
  // okuro's house post-fx chain. One global switch — the chain is applied at the
  // synth seam, so it governs every voice on every engine, not just this tier's.
  fx_enabled: boolean;
  voices: TtsVoiceOption[];
  previews: TtsPreviewStatus;
  speed_min: number;
  speed_max: number;
};

export type TtsConfigUpdate = Partial<
  Pick<TtsConfig, "female" | "male" | "okuro_gender" | "speed" | "fx_enabled">
>;

export const ttsApi = {
  getConfig: () => api<TtsConfig>("/api/voice/tts/config"),
  setConfig: (body: TtsConfigUpdate) =>
    api<TtsConfig>("/api/voice/tts/config", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  // Start the background prerender of this tier's missing previews. Idempotent —
  // Qwen's whole roster rides one model load, which is minutes, so readiness is
  // polled via getConfig().previews rather than awaited here.
  generatePreviews: () =>
    api<TtsPreviewStatus>("/api/voice/tts/previews", { method: "POST" }),
  // Fetch a voice's preview mp3 WITH bearer auth (an <audio src> can't carry the
  // token) and hand back an object URL the caller plays then revokes.
  previewObjectUrl: async (engine: string, voice: string): Promise<string> => {
    const auth = await authHeaders();
    const res = await fetch(
      `/api/voice/tts/preview/${engine}/${encodeURIComponent(voice)}`,
      { headers: { ...auth } },
    );
    if (!res.ok) throw new ApiError(`preview ${engine}/${voice} failed`, res.status);
    return URL.createObjectURL(await res.blob());
  },
};

// ---------------------------------------------------------------------------
// Reviews — contextual feedback on any okuro surface.
//
// `surface_id` is the DECLARED identity of what was reviewed. Route and
// viewport travel as evidence only: this app has already renamed /tasks ->
// /work and /roles -> /agents, so identity derived from location would rot.
// ---------------------------------------------------------------------------
export const reviewApi = {
  submit: (body: import("@/types/api").ReviewSubmit) =>
    api<import("@/types/api").Review>("/api/reviews", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  list: (params: { surface_id?: string; target_type?: string; target_id?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.surface_id) q.set("surface_id", params.surface_id);
    if (params.target_type) q.set("target_type", params.target_type);
    if (params.target_id) q.set("target_id", params.target_id);
    if (params.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return api<{ reviews: import("@/types/api").Review[]; count: number }>(
      `/api/reviews${qs ? `?${qs}` : ""}`,
    );
  },

  surfaces: (includeOrphaned = false) =>
    api<{ surfaces: import("@/types/api").ReviewSurface[]; count: number }>(
      `/api/reviews/surfaces?include_orphaned=${includeOrphaned}`,
    ),

  // Reported once on boot. Surfaces missing from this list are TOMBSTONED
  // server-side, never deleted — a removed surface's complaint may be exactly
  // why it was removed.
  registerSurfaces: (surfaces: import("@/types/api").ReviewSurfaceEntry[]) =>
    api<{ registered: number; orphaned: number; skipped_empty: boolean }>(
      "/api/reviews/surfaces",
      { method: "POST", body: JSON.stringify({ surfaces }) },
    ),
};

// Review sync — forwarding to the maintainer. `status` drives the guidance an
// install that UPDATED into this feature needs: it has no org_label yet, and
// the user will try to give feedback long before reading a changelog.
export const reviewSyncApi = {
  status: () =>
    api<{
      enabled: boolean;
      ready: boolean;
      missing: string[];
      org_label: string;
      install_id: string;
      /** Will send. */
      pending: number;
      /** Gave up after repeated rejection — recoverable via retry(). */
      failed: number;
    }>("/api/reviews/sync"),

  /** The exact bytes that would be transmitted — consent has to be literal. */
  preview: () =>
    api<{ fields: string[]; payload: Record<string, unknown>; note: string }>(
      "/api/reviews/sync/preview",
    ),

  run: () =>
    api<{ sent: number; skipped?: string | null; error?: string }>(
      "/api/reviews/sync",
      { method: "POST" },
    ),

  /** Save the two fields the operator owns; the server drains after saving. */
  save: (body: { sync_enabled?: boolean; org_label?: string }) =>
    api<{
      enabled: boolean; ready: boolean; missing: string[];
      org_label: string; install_id: string; pending: number; failed: number;
    }>("/api/reviews/sync", { method: "PUT", body: JSON.stringify(body) }),

  /** Release reviews that gave up, once the cause is fixed. */
  retry: () =>
    api<{ released: number; sent?: number; error?: string }>(
      "/api/reviews/sync/retry",
      { method: "POST" },
    ),
};
