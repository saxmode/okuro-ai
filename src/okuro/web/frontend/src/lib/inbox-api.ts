/**
 * Inbox API client — /api/inbox.
 *
 * Mirrors okuro.sense.inbox (Phase 1/2): the unified, ranked, deduped
 * overlay across todos / signals / reminders / continuations / research.
 * Rows are projected from the `inbox` table and ordered by salience DESC.
 * Disposition (act / defer / dismiss) writes back via /dispose.
 */

import { api } from "./api";

export type InboxKind =
  | "continue"
  | "note"
  | "task"
  | "saved"
  | "signal"
  | "research"
  | "reminder"
  | "forgotten";

export type InboxState =
  | "new"
  | "surfaced"
  | "staging"
  | "active"
  | "all"
  | "disposed";

export type InboxAction = "act" | "defer" | "dismiss";

/**
 * The explain-yourself block a producer can attach to a row.
 *
 * A title plus a salience score cannot answer "what is this about, where did
 * it come from, and why is it on my list?" — this carries the answer. Written
 * by producers into their own JSON column (todos.context / signals.evidence)
 * and lifted back out server-side by inbox._tag_block, which is key-driven:
 * any producer emitting these keys renders, with no allow-list to maintain.
 *
 * Today only notes-extract populates it, so every field is optional and the
 * UI branches on presence.
 */
export interface TagBlock {
  /** Kebab-case subject, e.g. "meridian-strategy". Shown inline on the row. */
  topic?: string;
  /** People / projects / systems this item involves. */
  entities?: string[];
  /** Where in the source this came from — a quote or section name. */
  context?: string;
  /** Why this earned a slot on the list — justification, not restatement. */
  rationale?: string;
  /** Title of the originating note. */
  note_title?: string;
  note_id?: string;
  /** Extractor's 0..1 self-reported certainty. */
  confidence?: number;
}

export interface InboxItem {
  /** Composite ref "ref_table:ref_id". */
  id: string;
  kind: InboxKind;
  ref_table: string;
  ref_id: string;
  project: string | null;
  title: string;
  /** Ranked salience score (~0..0.1), DESC ordering. */
  salience: number;
  importance: number;
  type_weight: number;
  source_trust: number;
  gravity: number;
  age_anchor_at: string | null;
  state: string;
  dedup_key: string | null;
  /** >1 means this row collapsed N duplicates. */
  dup_count: number;
  snoozed_until: string | null;
  surfaced_at: string | null;
  created_at: string;
  updated_at: string;
  /** True when the source row has an expandable markdown body. */
  has_detail?: boolean;
  /** Present when this row's producer explained itself. See TagBlock. */
  tags?: TagBlock | null;
}

export const inboxApi = {
  list: (params?: {
    kind?: InboxKind;
    /** Partition over the overlay; defaults to the gated "surfaced" view. */
    state?: InboxState;
    project?: string;
    limit?: number;
  }) => {
    const qs = new URLSearchParams();
    if (params?.kind) qs.set("kind", params.kind);
    qs.set("state", params?.state ?? "surfaced");
    if (params?.project) qs.set("project", params.project);
    if (params?.limit !== undefined) qs.set("limit", String(params.limit));
    const q = qs.toString();
    return api<{ inbox: InboxItem[] }>(`/api/inbox${q ? `?${q}` : ""}`);
  },

  dispose: (id: string, action: InboxAction, snooze_until?: string) =>
    api<InboxItem>(`/api/inbox/${id}/dispose`, {
      method: "POST",
      body: JSON.stringify({ action, snooze_until }),
    }),

  /**
   * Fetch the readable body of any inbox item's source row → markdown, so a
   * row of any kind can expand inline. The server resolves ref_table
   * (todos / thoughts / commitments / signals / reminder_suggestions) into a
   * single normalized body; unknown tables resolve to null.
   */
  detail: (refTable: string, refId: string) =>
    api<InboxDetail>(
      `/api/inbox/detail?ref_table=${encodeURIComponent(refTable)}&ref_id=${encodeURIComponent(refId)}`,
    ),
};

export interface InboxDetail {
  body?: string | null;
  /** Same block the list payload carries — see TagBlock. */
  tags?: TagBlock | null;
}
