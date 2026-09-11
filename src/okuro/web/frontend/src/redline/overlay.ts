/**
 * The redline overlay ENTRY for a served document — the half that runs INSIDE
 * the sandboxed frame.
 *
 * Built as a standalone IIFE (`vite.overlay.config.ts`) and served at
 * `…/serve/{token}/__redline__/overlay.js`, inside the same token scope as the
 * document, under `REDLINE_CSP`. It has no imports at runtime, no network of
 * its own (`connect-src 'none'` makes that physical rather than a promise),
 * and no access to the global bearer. Everything it learns arrives by
 * postMessage from the parent; everything it produces leaves the same way.
 *
 * This file is only the wiring for that situation: read the configuration off
 * its own script tag, build the overlay against `document`, and connect the
 * postMessage contract in both directions. The mechanism — bubbles, ghosts,
 * the hover outline, the breadcrumb, the ancestor walk, committing at the
 * walked level, and the `[data-redline-ignore]` trap that stops a comment ever
 * anchoring to a bubble — lives in `./overlay-core`, because a prism deck
 * needs exactly the same behaviour against a shadow root and two copies of it
 * would drift.
 *
 * The keyboard IS bound here. A frame sandboxed without `allow-same-origin`
 * swallows every key event once it has focus, and the page is what the owner
 * is looking at — so the frame handles the walk locally and forwards only the
 * three bound keys, leaving the parent the single owner of what they mean.
 */

import { createOverlay } from "./overlay-core";
import { humanPath } from "./anchor";
import { messageFromParent, type FrameToParent } from "./protocol";

// ── configuration, read off our own script tag ───────────────────────

const scriptEl =
  (document.currentScript as HTMLScriptElement | null) ??
  document.querySelector<HTMLScriptElement>("script[data-redline-doc]");

const DOC_ID = scriptEl?.getAttribute("data-redline-doc") ?? "";
const VERSION_ID = scriptEl?.getAttribute("data-redline-version") ?? "";
const SEQ = Number(scriptEl?.getAttribute("data-redline-seq") ?? "0");

// ── messaging ────────────────────────────────────────────────────────

function send(message: FrameToParent): void {
  // The parent's origin is not knowable from an opaque origin, and the frame
  // holds nothing worth protecting: no bearer, no token beyond the one already
  // in its own URL. "*" is the honest target.
  window.parent.postMessage(message, "*");
}

const overlay = createOverlay({ root: document, emit: send, bindKeys: true });

window.addEventListener("message", (event: MessageEvent) => {
  const data = messageFromParent(event);
  if (!data) return;
  switch (data.type) {
    case "redline:render":
      overlay.render(Array.isArray(data.comments) ? data.comments : []);
      break;
    case "redline:scrollTo":
      overlay.scrollToComment(data.commentId);
      break;
    case "redline:mode":
      overlay.setAddMode(Boolean(data.add));
      break;
    default:
      break;
  }
});

// ── boot ─────────────────────────────────────────────────────────────

send({ type: "redline:ready", docId: DOC_ID, versionId: VERSION_ID, seq: SEQ });

export function setAddMode(next: boolean): void {
  overlay.setAddMode(next);
}

/** Test seam — the pieces a unit test drives without a served document. */
export const __redline = {
  render: overlay.render,
  setAddMode: overlay.setAddMode,
  walk: overlay.walk,
  commit: overlay.commit,
  chainFrom: overlay.chainFrom,
  defaultLevel: overlay.defaultLevel,
  humanPath,
  host: overlay.host,
  shadow: overlay.shadow,
  state: overlay.state,
  setHover: overlay.setHover,
};
