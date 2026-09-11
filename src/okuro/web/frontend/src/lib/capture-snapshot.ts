// <!-- AGENT_HEADER
// role: code
// purpose: captureSnapshot() — the client-side "what is the user looking at"
//   grab. Renders #main-content (the shell scroll container, excludes nav/pulse)
//   to a PNG and builds the page context, then returns a snapshot Content IR the
//   shared HandoverDialog can send to a task / note / asset.
// AGENT_HEADER_END -->
import { domToPng } from "modern-screenshot";

import type { ContentIR } from "@/lib/handover-api";
import { getSnapshotContext } from "@/lib/handover-context";

const _clean = (n: Element | Node | null): string =>
  (n?.textContent || "").replace(/\s+/g, " ").trim();

/** Extract labeled structure from the view so an L3 page (no registered L2
 *  record) still hands over more than a flat text blob: the heading outline and
 *  any label→value pairs (definition lists, aria-labelled controls). Bounded so
 *  a huge page can't bloat the IR. */
function extractStructure(el: HTMLElement): {
  headings: { level: number; text: string }[];
  fields: { label: string; value: string }[];
} {
  const headings = Array.from(el.querySelectorAll("h1,h2,h3,h4,h5,h6"))
    .map((h) => ({ level: Number(h.tagName[1]), text: _clean(h) }))
    .filter((h) => h.text)
    .slice(0, 60);

  const fields: { label: string; value: string }[] = [];
  // Definition lists (dt → dd) — okuro's record surfaces render as these.
  el.querySelectorAll("dt").forEach((dt) => {
    const dd = dt.nextElementSibling;
    if (dd && dd.tagName === "DD") {
      const label = _clean(dt);
      const value = _clean(dd).slice(0, 500);
      if (label && value) fields.push({ label, value });
    }
  });
  // aria-labelled elements that carry their own visible value.
  el.querySelectorAll("[aria-label]").forEach((n) => {
    const label = (n.getAttribute("aria-label") || "").trim();
    const value = _clean(n).slice(0, 500);
    if (label && value && value !== label) fields.push({ label, value });
  });

  return { headings, fields: fields.slice(0, 80) };
}

/** Build the L3 baseline context + fold in the page's L2 rich context (entity /
 *  record) when a page registered one via registerSnapshotContext. */
function buildContext(el: HTMLElement): Record<string, unknown> {
  const loc = window.location;
  const base: Record<string, unknown> = {
    url: loc.href,
    route: loc.pathname + loc.search + loc.hash,
    params: Object.fromEntries(new URLSearchParams(loc.search)),
    title: document.title,
    // L3 structured signal — heading outline + labeled fields, so a page with
    // no registered L2 record still yields workable structure, not just prose.
    structure: extractStructure(el),
    // L3 baseline signal — trimmed + capped so a long page can't bloat the IR.
    visibleText: (el.innerText || "").trim().slice(0, 8000),
  };
  // L2 (entity, data…) overrides/extends the generic baseline where present.
  return { ...base, ...getSnapshotContext() };
}

/**
 * Capture the current view as a snapshot Content IR. Returns null when there is
 * no #main-content to capture (should not happen inside the shell). Never throws:
 * a screenshot failure still yields an IR with an empty screenshot + the context,
 * so the handover degrades to data-only rather than breaking.
 */
export async function captureSnapshot(): Promise<ContentIR | null> {
  const el = document.getElementById("main-content");
  if (!el) return null;

  let screenshot = "";
  try {
    // Viewport scope for MVP (the visible container), scale 1 to keep the data
    // URL light. WebGL surfaces (the pulse orb) live outside #main-content, so
    // nothing here needs the WebGL caveat.
    screenshot = await domToPng(el, { scale: 1 });
  } catch {
    screenshot = "";
  }

  const context = buildContext(el);
  const title = (context.title as string) || "Snapshot";
  const route = (context.route as string) || (context.url as string) || "";

  return {
    kind: "snapshot",
    title,
    // Backend derives the inline embed from structured.screenshot — sending an
    // empty body keeps ONE copy of the (large) base64 on the wire.
    body_md: "",
    structured: { screenshot, context },
    project: null,
    source: { tool: "snapshot", label: route },
  };
}
