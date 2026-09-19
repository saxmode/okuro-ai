// SPDX-License-Identifier: Apache-2.0
/**
 * CHROME STATE — and ONLY chrome state.
 *
 * This file used to own `topic` and `sub` as well. It does not any more: those
 * are LOCATION, and location lives in the URL. Keeping a copy here is the
 * failure the address layer exists to prevent — two stores that drift, where
 * the route says one thing and this says another.
 *
 * What is left is the two axes that are genuinely preference rather than place:
 *
 *   panel    open | collapsed    is the side panel showing
 *   content  normal | max        is the active section maximised
 *
 * They stay OUT of the path deliberately. A link you paste should take someone
 * to the content, not impose your panel state on them — and `/work/agents` and
 * `/work/agents/max` being two URLs for one place would break bookmarking for
 * no gain. They persist per-viewer instead, so a reload keeps your layout.
 *
 * The axes remain INDEPENDENT of each other, as Figma has them: maximising a
 * section does not touch the panel, and collapsing the panel does not
 * un-maximise.
 */

export type PanelState = "open" | "collapsed";
export type ContentState = "normal" | "max";

export interface ChromeState {
  panel: PanelState;
  content: ContentState;
}

export const CHROME_DEFAULT: ChromeState = { panel: "open", content: "normal" };

export function togglePanel(s: PanelState): PanelState {
  return s === "open" ? "collapsed" : "open";
}

export function toggleContent(s: ContentState): ContentState {
  return s === "normal" ? "max" : "normal";
}

const KEY = "okuro.shell.chrome";

/**
 * Read persisted chrome. Storage can throw or return nonsense — a private
 * window, cleared site data, a hand-edited value — so anything unrecognised
 * falls back to the default rather than reaching the DOM as an attribute no
 * stylesheet matches.
 */
export function loadChrome(): ChromeState {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return CHROME_DEFAULT;
    const parsed = JSON.parse(raw) as Partial<ChromeState>;
    return {
      panel: parsed.panel === "collapsed" ? "collapsed" : "open",
      content: parsed.content === "max" ? "max" : "normal",
    };
  } catch {
    return CHROME_DEFAULT;
  }
}

export function saveChrome(s: ChromeState): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    // A viewer who blocks storage still gets a working shell, just not a
    // remembered layout. Never let a preference failure break navigation.
  }
}

/**
 * THE PER-TOPIC SUB-ITEM MEMORY — a routing INPUT, not a store.
 *
 * Switching away from WORK and back should return you to the leaf you were on.
 * Under a URL regime that looks like a second source of truth, and it is not:
 * it is consulted exactly once, to turn a bare `/work` into `/work/agents`, and
 * after that redirect the path is complete and authoritative. Nothing reads it
 * again, so it cannot disagree with the URL.
 */
const LAST_KEY = "okuro.shell.lastLeaf";

export function loadRemembered(): Record<string, number> {
  try {
    const raw = localStorage.getItem(LAST_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    const out: Record<string, number> = {};
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v === "number" && Number.isInteger(v) && v >= 0) out[k] = v;
    }
    return out;
  } catch {
    return {};
  }
}

export function rememberLeaf(topic: string, leafIndex: number): void {
  try {
    const all = loadRemembered();
    all[topic] = leafIndex;
    localStorage.setItem(LAST_KEY, JSON.stringify(all));
  } catch {
    // see saveChrome
  }
}

/**
 * THE LAST LOCATION — what `/` resolves against.
 *
 * Stored WITH the query, so a returning visitor gets their section back and not
 * just their leaf. This is the only input to the router's one deliberately
 * non-deterministic decision; everything else resolves identically for everyone.
 */
const LAST_LOCATION_KEY = "okuro.shell.lastLocation";

export function loadLastLocation(): string | null {
  try {
    return localStorage.getItem(LAST_LOCATION_KEY);
  } catch {
    return null;
  }
}

export function rememberLocation(location: string): void {
  try {
    localStorage.setItem(LAST_LOCATION_KEY, location);
  } catch {
    // A viewer who blocks storage lands on /start/now every time, which is the
    // correct degradation: a fixed destination, never a broken one.
  }
}
