/**
 * People API client — /api/people/*.
 *
 * Wraps the HTTP router added alongside the frontend /people page.
 * Shape mirrors the on-disk schema: communication/cognitive/contact/tags
 * are already parsed into JSON by the server.
 */

import { api, getToken } from "./api";

export type OfflineImportResult = {
  ok: boolean;
  person_id: string;
  display_name: string;
  created: boolean;
  fields_updated: string[];
  measured_axes: string[];
  over_claim_flags: number;
};

export type PersonSummary = {
  id: string;
  display_name: string;
  organization?: string | null;
  role?: string | null;
  relation_to_user?: string | null;
  relation_type?: string | null;
  tags?: string[];
  active?: number;
  updated_at?: string;
  similarity?: number;
};

export type PersonCommunication = {
  style?: string;
  language?: string;
  formality?: string;
  response_length?: string;
  decision_style?: string;
  format_preferences?: string[];
  avoid?: string[];
  [key: string]: unknown;
};

export type SliderProvenance = {
  value?: number;
  confidence?: number;
  source?: string;
  updated_at?: string;
};

export type TopicInterest = { topic: string; weight?: number; detail?: string };
export type KnowledgeArea = {
  area: string;
  depth?: number;
  jargon?: number;
  recency?: string;
  detail?: string;
};

export type PersonCognitive = {
  accessibility?: string[];
  learning_style?: string;
  attention_span?: string;
  expertise_level?: Record<string, string>;
  pet_peeves?: string[];
  sliders?: Record<string, number>;
  // Per-axis provenance written by PUT /sliders (1.0 = user-set). Absent for
  // role-seeded previews and legacy pre-provenance rows.
  slider_provenance?: Record<string, SliderProvenance>;
  // CV / survey-derived knowledge map (see okuro.peer.cognitive_profile).
  profession?: string;
  function?: string;
  seniority?: string;
  topic_interests?: TopicInterest[];
  knowledge_areas?: KnowledgeArea[];
  [key: string]: unknown;
};

export type SourceGroup = {
  source_type: string;
  source_ref: string | null;
  field_count: number;
  latest_at: string;
  confidence: number;
  fields: string[];
};

export type PersonContact = {
  email?: string;
  phone?: string;
  [key: string]: unknown;
};

export type PersonRecord = PersonSummary & {
  notes?: string | null;
  communication?: PersonCommunication;
  cognitive?: PersonCognitive;
  contact?: PersonContact;
  created_at?: string;
};

export type PersonUpsert = Partial<
  Omit<PersonRecord, "id" | "created_at" | "updated_at" | "similarity">
>;

// --- Graph types ---

export type GraphMe = {
  id: "me";
  display_name: string;
  role?: string;
  communication?: Record<string, unknown>;
  cognitive?: Record<string, unknown>;
};

export type GraphNode = {
  id: string;
  display_name: string;
  organization?: string | null;
  role?: string | null;
  relation_to_user?: string | null;
  relation_type?: string | null;
  communication?: PersonCommunication;
  cognitive?: PersonCognitive;
  tags?: string[];
};

export type GraphEdge = {
  source: "me";
  target: string;
  translation_count: number;
  last_translated_at: string | null;
  /**
   * Short first-person phrase describing how the agent will reshape the
   * sender's message for this recipient (e.g. "I'll cut the wall of text
   * + shape it as an exec brief"). Derived server-side from the profile
   * diff — deterministic, no LLM per paint.
   */
  intent: string;
};

export type PeopleGraph = {
  me: GraphMe;
  nodes: GraphNode[];
  edges: GraphEdge[];
};

// --- Graph layout (server-side arrangement) ---

/**
 * The stored arrangement. Mirrors the client types in
 * components/people/graph-storage.ts field for field.
 *
 * `saved_at` is the hydration discriminator: null means the server has
 * never been written, so the browser's localStorage is the better source
 * and gets pushed up. Non-null means the server wins — including when a
 * collection inside is empty, which is a deliberate wipe, not an absence.
 */
export type GraphLayout = {
  positions: Record<string, { x: number; y: number }>;
  groups: Array<{
    id: string;
    name: string;
    memberIds: string[];
    center?: { x: number; y: number };
    radius?: number;
    geomVersion?: number;
  }>;
  settings: { connectionStyle?: string };
  saved_at: string | null;
};

export type GraphLayoutIn = Omit<GraphLayout, "saved_at">;

// --- Translate + presets ---

export type RolePresetSummary = {
  role_id: string;
  label: string;
  domain?: string;
  description?: string;
};

export type RolePresetFull = RolePresetSummary & {
  person_preset: {
    communication?: PersonCommunication;
    cognitive?: PersonCognitive;
    questionnaire_hints?: string[];
  };
};

export type TranslateResult = {
  success: boolean;
  translated: string | null;
  log_id?: string;
  provider?: string | null;
  model?: string | null;
  duration_ms?: number;
  error?: string | null;
  person?: { id: string; display_name: string };
};

/** Authed EventSource URL for the people change-feed (bearer via query, since
 *  EventSource can't set headers — mirrors flowDesignerEventsUrl). */
export async function peopleEventsUrl(): Promise<string> {
  const token = await getToken();
  return `/api/people/events?token=${encodeURIComponent(token)}`;
}

export const peopleApi = {
  list: (includeInactive = false) =>
    api<{ people: PersonSummary[] }>(
      `/api/people${includeInactive ? "?include_inactive=true" : ""}`,
    ),

  get: (id: string) => api<PersonRecord>(`/api/people/${encodeURIComponent(id)}`),

  create: (payload: PersonUpsert & { display_name: string }) =>
    api<PersonRecord>("/api/people", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  update: (id: string, payload: PersonUpsert) =>
    api<PersonRecord>(`/api/people/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  delete: (id: string, hard = false) =>
    api<void>(`/api/people/${encodeURIComponent(id)}${hard ? "?hard=true" : ""}`, {
      method: "DELETE",
    }),

  match: (query: string, limit = 5) =>
    api<{ matches: PersonSummary[] }>("/api/people/match", {
      method: "POST",
      body: JSON.stringify({ query, limit }),
    }),

  lens: (id: string, context?: string) =>
    api<{ person_id: string; markdown: string }>(
      `/api/people/${encodeURIComponent(id)}/lens`,
      {
        method: "POST",
        body: JSON.stringify({ context }),
      },
    ),

  graph: () => api<PeopleGraph>("/api/people/graph"),

  getLayout: () => api<GraphLayout>("/api/people/layout"),

  putLayout: (layout: GraphLayoutIn) =>
    api<{ ok: boolean; positions: number; groups: number; saved_at: string }>(
      "/api/people/layout",
      { method: "PUT", body: JSON.stringify(layout) },
    ),

  translate: (id: string, source: string, context?: string, provider?: string) =>
    api<TranslateResult>(`/api/people/${encodeURIComponent(id)}/translate`, {
      method: "POST",
      body: JSON.stringify({ source, context, provider }),
    }),

  listPresets: () =>
    api<{ presets: RolePresetSummary[] }>("/api/people/presets"),

  getPreset: (roleId: string) =>
    api<RolePresetFull>(`/api/people/presets/${encodeURIComponent(roleId)}`),

  applyPreset: (id: string, roleQuery: string) =>
    api<{
      sources_written: number;
      fields_changed: string[];
      role_id: string | null;
      note?: string;
    }>(`/api/people/${encodeURIComponent(id)}/apply-preset`, {
      method: "POST",
      body: JSON.stringify({ role_query: roleQuery }),
    }),

  ingestSourceText: (
    id: string,
    text: string,
    source_type: string = "notes",
    source_ref?: string,
  ) =>
    api<{
      sources_written: number;
      fields_changed: string[];
      source_type: string;
      source_ref?: string;
      evidence?: string;
      note?: string;
    }>(`/api/people/${encodeURIComponent(id)}/sources`, {
      method: "POST",
      body: JSON.stringify({ text, source_type, source_ref }),
    }),

  ingestSourceFile: async (id: string, file: File) => {
    // Uploads through the same /sources endpoint but as multipart — the
    // server dispatches by filename extension (.pdf/.eml/.txt/.md).
    const form = new FormData();
    form.append("file", file);
    return api<{
      sources_written: number;
      fields_changed: string[];
      source_type: string;
      source_ref?: string;
      evidence?: string;
      note?: string;
    }>(`/api/people/${encodeURIComponent(id)}/sources`, {
      method: "POST",
      body: form,
    });
  },

  listSources: (id: string) =>
    api<{
      sources: Array<{
        id: string;
        source_type: string;
        source_ref: string | null;
        field_path: string;
        extracted_value: unknown;
        confidence: number;
        applied: number;
        created_at: string;
      }>;
    }>(`/api/people/${encodeURIComponent(id)}/sources`),

  // Grouped provenance — one entry per uploaded file / preset / survey.
  listSourceGroups: (id: string) =>
    api<{ groups: SourceGroup[] }>(
      `/api/people/${encodeURIComponent(id)}/source-groups`,
    ),

  // Remove a source group and revert the fields it set.
  removeSource: (id: string, source_type: string, source_ref: string | null) =>
    api<{ removed_rows: number; fields_reverted: string[] }>(
      `/api/people/${encodeURIComponent(id)}/sources`,
      { method: "DELETE", body: JSON.stringify({ source_type, source_ref }) },
    ),

  // ── Mode B: sliders ─────────────────────────────────────────────
  // Fixed canonical 8-axis slider vector. Server is the source of truth
  // for axis order + labels (GET /slider-meta) so renaming or reordering
  // never requires a frontend redeploy.

  getSliderMeta: () =>
    api<{
      sliders: Array<{ key: string; left_label: string; right_label: string }>;
    }>("/api/people/slider-meta"),

  getRoleDefaults: (role?: string) =>
    api<{ role: string; sliders: Record<string, number> }>(
      `/api/people/role-defaults${role ? `?role=${encodeURIComponent(role)}` : ""}`,
    ),

  putSliders: (id: string, sliders: Record<string, number>) =>
    api<{ person_id: string; cognitive: { sliders: Record<string, number> } }>(
      `/api/people/${encodeURIComponent(id)}/sliders`,
      { method: "PUT", body: JSON.stringify(sliders) },
    ),

  mintQuestionnaire: (id: string, roleHint?: string, expiresHours: number = 72) =>
    api<{
      id: string;
      token: string;
      share_url: string;
      expires_at: string;
      question_count: number;
      preset_key: string | null;
    }>(`/api/people/${encodeURIComponent(id)}/questionnaires`, {
      method: "POST",
      body: JSON.stringify({ role_hint: roleHint, expires_hours: expiresHours }),
    }),

  // Which languages the survey can be served in, and which have been
  // reviewed. `reviewed` is here so the picker can warn before the download
  // rather than the user meeting a 409 after choosing.
  surveyLanguages: () =>
    api<{ languages: SurveyLanguage[] }>("/api/people/survey/languages"),

  // Offline survey — download the BLANK self-identifying file (auth'd fetch
  // then blob-download; the endpoint is bearer-gated so window.open can't be used).
  downloadBlankSurvey: async (
    lang = "en",
    allowUnreviewed = false,
  ): Promise<void> => {
    const token = await getToken();
    const qs = new URLSearchParams({ lang });
    if (allowUnreviewed) qs.set("allow_unreviewed", "true");
    const res = await fetch(`/api/people/offline/blank.html?${qs}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) throw new Error("Could not download the survey");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `okuro-survey-${lang}.html`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },

  // Import a returned file. Self-identified ({identity,survey}) creates the
  // person; prefilled ({person_id,survey}) re-profiles an existing one.
  importFilledSurvey: (file: {
    identity?: Record<string, unknown>;
    survey: Record<string, unknown>;
    person_id?: string;
  }): Promise<OfflineImportResult> => {
    if (file.person_id) {
      return api<OfflineImportResult>(
        `/api/people/${encodeURIComponent(file.person_id)}/survey-import`,
        { method: "POST", body: JSON.stringify({ survey: file.survey }) },
      );
    }
    return api<OfflineImportResult>("/api/people/offline/import", {
      method: "POST",
      body: JSON.stringify({ identity: file.identity, survey: file.survey }),
    });
  },
};

// Public (bearerless) questionnaire API — used by the /q/:token form.
// Recipients loading this page aren't authenticated users; we must not
// try to fetch /api/auth/token (which is loopback-only). Use raw fetch.
async function publicFetch<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts?.headers || {}) },
  });
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = await res.json();
    } catch {
      detail = res.statusText;
    }
    const msg =
      typeof detail === "object" && detail !== null && "detail" in detail
        ? String((detail as Record<string, unknown>).detail)
        : res.statusText;
    throw new Error(msg);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// Structured survey v2 (deterministic, no-LLM) — shared shape with the scorer.
export type SurveySlider = {
  id: string;
  statement: string;
  low: string;
  high: string;
  scale: [number, number];
  // The tappable options, translated. Present from the i18n change onward.
  cards: SurveyCard[];
};

export type SurveyForm = {
  identity: {
    function_options: string[];
    /** Stable keys the scorer reads — NOT display text. */
    seniority_options: string[];
    seniority_labels?: Record<string, string>;
  };
  knowledge: {
    starter_domains: string[];
    depth_scale: [number, number];
    depth_anchors: string[];
    depth_helpers: string[];
    detail_prompt: string;
    // Keys stay English (the scorer reads them); labels are translated.
    recency_options: string[];
    recency_labels: Record<string, string>;
    confidence_scale: [number, number];
  };
  cognitive: SurveySlider[];
  angle: SurveySlider[];
  // Step order, display name, tier and measured axes — the single definition
  // both recipient transports render from (peer/survey.py::_STEPS). Optional
  // so a client talking to a pre-P3.1 server still renders.
  steps?: SurveyStep[];
  modules?: SurveyModule[];
  // Resolved language — also drives the document's <html lang> attribute.
  // Review status is NOT here: it governs delivery, which peer/survey_guard
  // enforces before a survey is ever handed out.
  lang?: string;
  // Buttons, placeholders and other chrome, from the same catalogue.
  ui?: Record<string, string>;
};

export type SurveyStep = {
  id: string;
  name: string;
  tier: "core" | "module";
  module?: string;
  axes: string[];
  // Recipient-facing copy for this step, from the language catalogue.
  heading?: string;
  hint?: string;
  [k: string]: unknown;
};

export type SurveyModule = { key: string; name: string; blurb: string };

export type SurveyLanguage = {
  code: string;
  /** Endonym — "Deutsch", not "German". */
  label: string;
  reviewed: boolean;
  note: string;
};

// `value` is the datum the scorer reads and is identical in every language;
// label and helper are the translated part.
export type SurveyCard = { value: number; label: string; helper: string };

export type StructuredSurveyResponse = {
  identity?: {
    display_name?: string;
    profession?: string;
    function?: string;
    seniority?: string;
  };
  knowledge: {
    areas: Array<{ area: string; depth: number; detail?: string; recency?: string }>;
    weaknesses: Array<{ area: string; mode: "explain" | "skip" }>;
    confidence?: number;
  };
  cognitive: Record<string, number>;
  angle: Record<string, number>;
};

export const questionnaireApi = {
  get: (token: string) =>
    publicFetch<{
      person_display_name: string;
      person_role: string | null;
      questions: Array<{ id: string; prompt: string }>;
      survey_form?: SurveyForm;
      structured?: boolean;
      status: string;
      already_answered: boolean;
      expires_at: string | null;
    }>(`/api/q/${encodeURIComponent(token)}`),

  submit: (token: string, responses: Record<string, string>) =>
    publicFetch<{ ok: boolean; fields_updated: string[]; evidence?: string }>(
      `/api/q/${encodeURIComponent(token)}`,
      { method: "POST", body: JSON.stringify({ responses }) },
    ),

  submitSurvey: (token: string, survey: StructuredSurveyResponse) =>
    publicFetch<{
      ok: boolean;
      fields_updated: string[];
      measured_axes: string[];
      over_claim_flags: number;
    }>(`/api/q/${encodeURIComponent(token)}`, {
      method: "POST",
      body: JSON.stringify({ survey }),
    }),
};
