/**
 * redline API client — /api/redline.
 *
 * Mirrors okuro.sense.redline. Two things about this surface are load-bearing
 * and neither is obvious from the shapes:
 *
 * 1. **A comment belongs to a VERSION, not to a document.** Switching version
 *    loads that version's own list; nothing carries across, because 0 of 5
 *    anchors survived the real regeneration that produced the rule.
 * 2. **The parent holds the bearer.** The framed document is served under a
 *    per-document capability token in its own URL and `connect-src 'none'`.
 *    Every call in this file is made by the SPA, never by the frame.
 *
 * The serve token dies with the API process (it is an in-process dict, 8-hour
 * TTL). A 401 on the frame is therefore "call open again", not an error —
 * `openDocument` is the way back.
 */

import { api } from "./api";
import type { Anchor } from "@/redline/anchor";

export type RedlineKind = "file" | "artifact" | "prism";
export type RedlineStatus = "open" | "done";
/** `unresolved` means the version's bytes were never read, not "gone". */
export type AnchorState = "exact" | "orphan" | "unresolved";

export interface RedlineDocument {
  id: string;
  kind: RedlineKind;
  ref: string;
  title: string;
  project: string | null;
  tombstoned_at?: string | null;
  version_seq?: number | null;
  version_id?: string | null;
}

export interface RedlineOpened {
  document_id: string;
  version_id: string;
  version_seq: number;
  content_hash: string;
  created_version: boolean;
  /** The document's serve URL. Null for a deck, and for a hash-only version. */
  url: string | null;
  /**
   * A prism version's own deck JSON, under the same capability token. A
   * DeckDoc is not html, so it is the SPA's deck runtime that renders it and
   * `url` stays null.
   */
  deck_url: string | null;
  /** Whether THIS version's own bytes still exist and can be shown. */
  renderable: boolean;
  /** Why not, when `renderable` is false. */
  note: string | null;
  web: string;
  token_expires_at: string;
  open_comments: number;
  carried_over_from: { version_seq: number; open_comments: number } | null;
  shots: {
    made: number;
    skipped: number;
    reason: string | null;
    paths: string[];
  } | null;
}

export interface RedlineVersion {
  id: string;
  seq: number;
  content_hash: string;
  label: string | null;
  captured_at: string;
  is_current: boolean;
  /**
   * Whether this version can be RENDERED, which is not the same as being
   * current. An artifact and a prism version keep their own bytes, so a past
   * one is readable; a past file version kept only its hash.
   */
  renderable: boolean;
  open: number;
  done: number;
}

export interface RedlineReply {
  id: string;
  body: string;
  author: string;
  created_at: string;
}

export interface RedlineComment {
  id: string;
  seq: number;
  status: RedlineStatus;
  body: string;
  author: string;
  created_at: string;
  version_seq: number;
  content_hash: string;
  anchor_state: AnchorState;
  /**
   * The stored anchor v2. Present on the HTTP shape only — the overlay
   * re-resolves it against the live DOM to place the bubble. `redline_list`
   * over MCP omits it: an agent acts on `element` and `file:line:col`.
   */
  anchor: Anchor | null;
  path: string | null;
  element: string | null;
  text: string | null;
  /** WHY this pointer can be trusted — a label, never a score. */
  identified_by: string;
  /**
   * What the ANCHOR carries, whatever the resolution concluded. The two
   * diverge exactly when resolution failed: a bubble whose title repeats is an
   * orphan with verdict "position" while still holding that title, and the
   * title is what a reader looking for it by hand uses.
   */
  anchor_identity: string;
  file: string | null;
  line: number | null;
  col: number | null;
  /**
   * The shadow-host chain the element lives inside. Empty for an ordinary
   * document; for a prism deck it is the only thing that says WHERE the
   * element is, because no document-level selector reaches into a shadow root.
   */
  hosts: string[];
  screenshot: string | null;
  box: [number, number, number, number] | null;
  replies: RedlineReply[];
  done_at: string | null;
  done_by: string | null;
  done_note: string | null;
  after_excerpt: string | null;
  reanchored_to: { version_seq: number; path: string | null } | null;
}

export interface RedlineListing {
  document: Pick<RedlineDocument, "id" | "kind" | "ref" | "title" | "project">;
  version: {
    id: string;
    seq: number;
    content_hash: string;
    captured_at: string;
    root_path: string | null;
    renderable: boolean;
  };
  counts: { open: number; done: number };
  comments: RedlineComment[];
  /** The §10 paste block. Non-null only when format="markdown" was asked for. */
  markdown: string | null;
}

export const redlineApi = {
  documents: () =>
    api<{ documents: RedlineDocument[] }>("/api/redline/documents"),

  /**
   * Mint a version + a fresh serve token. Also the recovery path from a 401.
   *
   * `versionSeq` re-opens an EXISTING version read-only instead of pinning the
   * current bytes — the version switcher's call. It answers
   * `renderable: false` with a reason for a past file version, whose content
   * was never stored.
   */
  open: (kind: RedlineKind, ref: string, versionSeq?: number | null, reload = false) =>
    api<RedlineOpened>("/api/redline/open", {
      method: "POST",
      body: JSON.stringify({
        kind,
        ref,
        reload,
        version_seq: versionSeq ?? null,
      }),
    }),

  versions: (documentId: string) =>
    api<{ document: RedlineDocument; versions: RedlineVersion[] }>(
      `/api/redline/documents/${documentId}/versions`,
    ),

  comments: (documentId: string, versionId: string | null, status: "open" | "done" | "all") => {
    const params = new URLSearchParams({ document_id: documentId, status });
    if (versionId) params.set("version_id", versionId);
    return api<RedlineListing>(`/api/redline/comments?${params.toString()}`);
  },

  create: (versionId: string, body: string, anchor: Anchor | null) =>
    api<{ id: string }>("/api/redline/comments", {
      method: "POST",
      body: JSON.stringify({ version_id: versionId, body, anchor }),
    }),

  /** Marking done REQUIRES a note — the schema refuses an empty one too. */
  resolve: (commentId: string, note: string, afterExcerpt?: string) =>
    api<{ comment_id: string; warning: string | null }>(
      `/api/redline/comments/${commentId}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          status: "done",
          note,
          after_excerpt: afterExcerpt ?? null,
          done_by: "owner",
        }),
      },
    ),

  reopen: (commentId: string) =>
    api<Record<string, unknown>>(`/api/redline/comments/${commentId}`, {
      method: "PATCH",
      body: JSON.stringify({ status: "open" }),
    }),

  reply: (commentId: string, text: string) =>
    api<Record<string, unknown>>(`/api/redline/comments/${commentId}`, {
      method: "PATCH",
      body: JSON.stringify({ reply: text }),
    }),
};
