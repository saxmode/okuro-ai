// <!-- AGENT_HEADER
// role: code
// purpose: okuro·handover API client — the Content IR + the two REST calls
//   (targets/send) the shared HandoverDialog uses. Mirrors lib/prism-api.ts.
// AGENT_HEADER_END -->
import { api } from "./api";

/** Richest shape a selection preserves on the way into a handover. */
export type ContentKind = "text" | "subgraph" | "facet" | "asset" | "snapshot";

export interface HandoverSource {
  tool: string;
  id?: string;
  label?: string;
}

/** The ONE normalized payload every source (notes selection, flow subgraph,
 *  prism facet) emits — mirrors okuro.handover.ir.ContentIR.public(). */
export interface ContentIR {
  kind: ContentKind;
  title: string;
  body_md: string;
  structured?: {
    nodes?: unknown[];
    edges?: unknown[];
    facets?: Record<string, unknown>;
    assets?: Array<{ id: string; kind?: string; mime?: string | null; url: string; title?: string | null }>;
    /** snapshot: a PNG data URL of the captured view. */
    screenshot?: string;
    /** snapshot: page context — {url, route, params, title, visibleText, entity?}. */
    context?: Record<string, unknown>;
  };
  project?: string | null;
  source?: HandoverSource;
}

export type HandoverFieldType = "text" | "recipient" | "brand" | "select";
export type HandoverFieldMode = "ask" | "auto" | "optional";

export interface HandoverField {
  key: string;
  label: string;
  type: HandoverFieldType;
  mode: HandoverFieldMode;
  help?: string;
  /** For type "select": the allowed values (label = the option string). */
  options?: string[];
}

export interface HandoverTarget {
  id: string;
  label: string;
  icon: string;
  accepts: ContentKind[];
  fields: HandoverField[];
}

export interface HandoverTargetsResponse {
  kind: ContentKind;
  targets: HandoverTarget[];
}

/** Value shape for a `recipient`-typed field. Either a person OR a target
 *  group — never both. */
export interface RecipientValue {
  kind: "person" | "group";
  id: string;
}

export interface HandoverSendResult {
  success: boolean;
  url?: string;
  id?: string;
  label?: string;
  target?: string;
}

/** A target group as listed for the recipient picker's "group" mode. */
export interface TargetGroupSummary {
  id: string;
  name: string;
  kind?: string;
  company_id?: string | null;
  role_class?: string | null;
  /** Resolved membership (affiliation-derived ∪ explicit), server-side. Lets
   *  the People graph place an existing audience on the canvas without
   *  re-resolving who belongs to it. */
  member_ids: string[];
  member_count: number;
}

export const targetGroupsApi = {
  list: () => api<{ groups: TargetGroupSummary[] }>("/api/target-groups"),
};

export const handoverApi = {
  targets: (content: ContentIR) =>
    api<HandoverTargetsResponse>("/api/handover/targets", {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  send: (content: ContentIR, target: string, inputs: Record<string, unknown>) =>
    api<HandoverSendResult>("/api/handover/send", {
      method: "POST",
      body: JSON.stringify({ content, target, inputs }),
    }),
};
