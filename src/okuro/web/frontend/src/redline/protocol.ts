/**
 * The postMessage contract between the redline viewer (parent) and the
 * overlay running inside the sandboxed frame.
 *
 * The frame is `sandbox="allow-scripts"` with NO `allow-same-origin`, so its
 * origin is the STRING `"null"` — measured, `{origin: "null", sameSource:
 * true}`. Neither side may ever validate `event.origin`. The only accepted
 * check is identity of the window object:
 *
 *   parent: `if (event.source !== iframeRef.current?.contentWindow) return;`
 *   frame:  `if (event.source !== window.parent) return;`
 *
 * The frame never holds the global bearer and `connect-src 'none'` means it
 * physically cannot fetch. Every write is performed by the parent.
 */

import type { Anchor } from "./anchor";

export type RedlineStatus = "open" | "done";

/** One comment as the frame needs it: a number, a place, and a state. */
export interface RenderComment {
  commentId: string;
  seq: number;
  anchor: Anchor | null;
  status: RedlineStatus;
}

export type FrameToParent =
  | { type: "redline:ready"; docId: string; versionId: string; seq: number }
  | { type: "redline:anchor"; anchor: Anchor }
  | { type: "redline:bubbleClick"; commentId: string }
  /** Echo — the frame left or entered add mode on its own key press. */
  | { type: "redline:mode"; add: boolean }
  /**
   * A shortcut pressed while focus was inside the frame.
   *
   * Design v1.1 §7.5 puts the keyboard in the viewer, but a sandboxed iframe
   * swallows every key event once it has focus, and the page is what the owner
   * is looking at. The frame forwards only the three bound keys and the parent
   * stays the single owner of what they mean.
   */
  | { type: "redline:key"; key: string };

export type ParentToFrame =
  | { type: "redline:render"; comments: RenderComment[] }
  | { type: "redline:scrollTo"; commentId: string }
  | { type: "redline:mode"; add: boolean };

export const REDLINE_KEYS = ["c", "n", "Escape"] as const;

/**
 * The parent's ONLY accepted validation of a message from the frame.
 *
 * Named rather than inlined so there is one place to read it and one place to
 * test it. `event.origin` is deliberately not consulted: the frame is
 * sandboxed without `allow-same-origin`, so its origin is the string `"null"`
 * and comparing it against any expected value either always fails or, worse,
 * passes for every other opaque-origin frame on the page.
 */
export function messageFromFrame(
  event: MessageEvent,
  frame: HTMLIFrameElement | null,
): FrameToParent | null {
  if (!frame?.contentWindow) return null;
  if (event.source !== frame.contentWindow) return null;
  const data = event.data as FrameToParent | undefined;
  if (!data || typeof data !== "object" || typeof data.type !== "string") return null;
  if (!data.type.startsWith("redline:")) return null;
  return data;
}

/** The frame's mirror of the same rule: only the parent window may speak. */
export function messageFromParent(event: MessageEvent): ParentToFrame | null {
  if (event.source !== window.parent) return null;
  const data = event.data as ParentToFrame | undefined;
  if (!data || typeof data !== "object" || typeof data.type !== "string") return null;
  if (!data.type.startsWith("redline:")) return null;
  return data;
}
