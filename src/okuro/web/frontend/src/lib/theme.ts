/**
 * The app's dark/light switch, and the one attribute the engine reads.
 *
 * `/engine.css` is the app's only stylesheet. It emits BOTH grounds and
 * switches on `:root[data-appearance="dark"]`; `index.html` carries that
 * attribute on <html> from the first byte so the page never flashes.
 */

import { isHex6, normalizeHex } from "./color";

const ENGINE_LINK_ID = "okuro-design-tokens";
const THEME_MODE_LS_KEY = "okuro.theme-mode";

/**
 * THE ATTRIBUTE THE ENGINE ACTUALLY READS.
 *
 * This was `data-theme`, which is what v0's `/tokens.css` keyed its palette
 * blocks on. That sheet was unlinked on 2026-09-03 and deleted in p10, and the
 * engine has never emitted a single `[data-theme]` rule — measured on the live
 * sheet: 0 blocks for `data-theme`, 1 for `data-appearance`.
 *
 * So from the unlink onward the toggle wrote an attribute nothing read. It
 * flipped its own icon and `color-scheme` (native scrollbars did change), which
 * is exactly enough feedback to look alive while the page stayed dark.
 *
 * NAMED ONCE, HERE. The selector lives in the emitter and the initial value in
 * index.html; a third hand-typed copy is how those two drift apart again.
 */
const APPEARANCE_ATTR = "data-appearance";

export type ThemeMode = "dark" | "light";

/**
 * AN APPEARANCE IS TWO KEYS, AND WRITING ONE OF THEM IS THE DEFECT.
 *
 * `data-appearance` is what the ENGINE reads; `color-scheme` is what CHROMIUM
 * reads, and it owns everything the page's stylesheets cannot reach — the
 * `<select>` popup, scrollbars, spin buttons, date pickers. A writer that sets
 * only the first leaves the second saying whatever the last writer said.
 *
 * MEASURED 2026-09-11 on a codex page showing LIGHT appearance:
 *
 *   documentElement.getAttribute('data-appearance')  ->  "light"
 *   documentElement.style.cssText                    ->  "color-scheme: dark;"
 *   getComputedStyle(panelSelect).colorScheme        ->  "dark"
 *
 * The closed control was correct — white ground, near-black text — and its
 * OPTION LIST was black, which is why he could not find where it came from: no
 * page stylesheet reaches a native popup.
 *
 * THE SHAPE OF THE FIX IS THE SAME ONE `APPEARANCE_ATTR` ABOVE ALREADY IS. The
 * attribute is named once because a second hand-typed copy is how the
 * `data-theme` outage survived three days. A second PARTIAL writer is that same
 * defect with one of the two keys, so the pair is written once, here, and every
 * writer goes through this function.
 *
 * Returns an undo for the pair, because a page that borrows the appearance for
 * the length of a route has to give BOTH keys back — restoring only the
 * attribute is how the two came apart in the first place.
 */
export function setAppearance(mode: ThemeMode): () => void {
  if (typeof document === "undefined") return () => {};
  const root = document.documentElement;
  const previousAttr = root.getAttribute(APPEARANCE_ATTR);
  const previousScheme = root.style.colorScheme;
  root.setAttribute(APPEARANCE_ATTR, mode);
  root.style.colorScheme = mode;
  return () => {
    if (previousAttr === null) root.removeAttribute(APPEARANCE_ATTR);
    else root.setAttribute(APPEARANCE_ATTR, previousAttr);
    root.style.colorScheme = previousScheme;
  };
}

/**
 * Apply a dark/light mode: sets `data-appearance` on <html> (so the engine's
 * matching ground block wins) and `color-scheme` (so native controls and
 * scrollbars match). Persisted globally — the choice is remembered across
 * brands per the theming decision.
 */
export function applyThemeMode(mode: ThemeMode, persist = true): void {
  if (typeof document === "undefined") return;
  setAppearance(mode);
  if (persist) {
    try { localStorage.setItem(THEME_MODE_LS_KEY, mode); } catch { /* private mode */ }
  }
}

/** The persisted mode, or null if the user has never chosen one. */
export function getStoredThemeMode(): ThemeMode | null {
  try {
    const v = localStorage.getItem(THEME_MODE_LS_KEY);
    return v === "dark" || v === "light" ? v : null;
  } catch {
    return null;
  }
}

/** The mode currently in effect: the stored choice, else inferred from the
 *  active background so the toggle shows the right initial state. */
export function currentThemeMode(): ThemeMode {
  const stored = getStoredThemeMode();
  if (stored) return stored;
  return inferModeFromBackground();
}

/** Read the active --color-background-base and classify it dark/light. */
function inferModeFromBackground(): ThemeMode {
  if (typeof document === "undefined") return "dark";
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue("--color-background-base")
    .trim();
  const hex = normalizeHex(raw);
  if (!isHex6(hex)) return "dark";
  // Rec. 601 luma; >= 0.5 → light.
  const r = parseInt(hex.slice(1, 3), 16) / 255;
  const g = parseInt(hex.slice(3, 5), 16) / 255;
  const b = parseInt(hex.slice(5, 7), 16) / 255;
  return 0.299 * r + 0.587 * g + 0.114 * b >= 0.5 ? "light" : "dark";
}

/**
 * On boot: if the user has a stored mode, apply it. Otherwise leave the
 * attribute index.html shipped (the engine's own default ground shows) but sync
 * color-scheme to that ground so controls/scrollbars don't mismatch. Pre-mount
 * safe.
 */
export function applyThemeModeFromCache(): void {
  if (typeof document === "undefined") return;
  const stored = getStoredThemeMode();
  if (stored) {
    applyThemeMode(stored, false);
  } else {
    document.documentElement.style.colorScheme = inferModeFromBackground();
  }
}
const PULSE_OUTLINE_STYLE_ID = "okuro-pulse-outline-override";
const PULSE_OUTLINE_LS_KEY = "okuro.pulse-outline";

export const PULSE_OUTLINE_DEFAULTS = {
  outlinesOnly: false,
  strength: 1.5,
} as const;

export type PulseOutlineOverride = {
  outlinesOnly: boolean;
  strength: number;
};

/**
 * The <link> element that carries the app's design sheet.
 *
 * THIS USED TO CREATE A `/tokens.css` LINK WHEN IT FOUND NONE, and that became
 * a live defect the moment index.html stopped linking the v1 dump (2026-09-03,
 * step one of the approved unlink-then-delete retirement). A created link is
 * APPENDED to <head>, so it would have landed AFTER `/engine.css` — and since
 * both sheets declare the same consumer names at `:root` at equal specificity,
 * the later one wins. The frozen dump would have silently won the cascade and
 * reverted the whole re-theme, on the Settings path only, with every file still
 * looking correct.
 *
 * So this now RESOLVES the engine sheet and never fabricates a sheet of any
 * kind. If the link is absent the caller gets null and does nothing, which is
 * the honest outcome: a document with no engine sheet is a document the app
 * cannot re-theme, and inventing a v1 one to fill the hole is what caused the
 * defect above.
 */
function getEngineLink(): HTMLLinkElement | null {
  let link = document.getElementById(ENGINE_LINK_ID) as HTMLLinkElement | null;
  if (!link) {
    link = document.querySelector(
      'link[href="/engine.css"], link[href^="/engine.css?"]',
    ) as HTMLLinkElement | null;
    if (link) link.id = ENGINE_LINK_ID;
  }
  return link;
}

/**
 * Re-fetch the app's design sheet so a server-side authoring change shows
 * without a reload.
 *
 * `/engine.css` is generated from the saved kit on EVERY request and answers
 * `Cache-Control: no-store`, so this is the whole mechanism: change what the
 * server resolves, re-fetch, and every value the engine computes — including
 * the inks it derives at the 0.675 threshold — moves together. That is the
 * property the accent picker now rides on instead of overriding names at
 * `:root` and leaving the computed foregrounds pinned to the old brand.
 *
 * The cache-buster is on the query string only; the route itself takes no
 * parameters on purpose (see design_engine/api.py::engine_css — a kit id here
 * would invite pointing the whole SPA at a guest brand).
 */
/**
 * Fired on `window` once a re-fetched engine sheet has actually LOADED.
 *
 * Anything that reads a token OUTSIDE the cascade — the pulse canvas paints
 * into a `<canvas>`, so CSS cannot reach it — has to be told when to re-read.
 * Watching the `<link>`'s `href` attribute is the tempting signal and it is the
 * WRONG MOMENT: a MutationObserver fires the instant the attribute is assigned,
 * which is before the new sheet has been fetched or applied. Measured
 * 2026-09-06 — after a kit switch the blob stayed on the old brand's colour
 * while `getComputedStyle` already reported the new one, because the canvas
 * re-read during that gap, got the old value, and marked itself clean.
 *
 * `load` is the moment the values are really there.
 */
export const SHEET_CHANGED_EVENT = "okuro:engine-sheet-changed";

export function reloadEngineSheet(): void {
  const link = getEngineLink();
  if (!link) return;
  link.addEventListener(
    "load",
    () => window.dispatchEvent(new CustomEvent(SHEET_CHANGED_EVENT)),
    { once: true },
  );
  link.href = `/engine.css?t=${Date.now()}`;
}

/**
 * RETIRED, and the retirement is the fix.
 *
 * `syncAccentForeground` used to read the computed `--color-accent` and inject
 * an APCA-picked foreground into a late `<style>` at `:root`, and
 * `applyAccentOverride` injected the accent triple the same way. Between them
 * they made THREE authorities for one question — the engine's emitted sheet,
 * the picker's override block, and the APCA block — and the app's appearance
 * was whichever won the cascade at that instant.
 *
 * That is the defect the owner ruled on: the picker's `:root` block is (0,1,0)
 * and the engine's `:root[data-appearance="dark"]` is (0,1,1), so his saved
 * accent applied in light and silently stopped applying in dark. A specificity
 * bump was the tempting fix and the wrong one: the block moved the accent while
 * leaving every ink the engine derives from it pinned to the old brand, and the
 * 0.675 threshold sits exactly on WCAG 3:1-against-white, so that combination
 * fails AA on every hue rather than on unlucky ones.
 *
 * So the accent is now an INPUT to the engine instead of an override of its
 * output: Settings writes it to the profile, `/engine.css` re-derives the whole
 * brand around it server-side, and the app re-fetches. One value descends the
 * tree. There is no pre-mount cache to apply any more either — the sheet the
 * browser fetches already carries the user's accent, so the flash the cache
 * existed to prevent cannot happen.
 *
 * See design_engine/api.py::_with_accent for the re-derivation, and note it
 * saves nothing: okuro-ds is a shipped kit and `store.save` refuses it.
 */

/** Resolve and tag the engine sheet link on page load. Creates nothing. */
export function initTheme(): void {
  getEngineLink();
}

/**
 * NOTE FOR THE NEXT READER, because two functions used to live here.
 *
 * `applyAccentOverrideFromCache` (pre-mount, from localStorage) and
 * `applyAccentOverrideFromProfile` (post-fetch, from design.overrides) both
 * existed to re-apply the picked accent over the sheet after every boot. Both
 * are gone: `/engine.css` now resolves that same profile key server-side, so
 * the very first sheet the browser parses already carries the user's accent.
 * There is nothing to re-apply and no window in which the wrong colour is
 * painted — which is what the localStorage cache was for.
 *
 * A picked accent therefore survives a reload through the PROFILE, not through
 * the browser. It also now survives across devices, which the cache never did.
 */

// ── Pulse outline override ──────────────────────────────
//
// The pulse blob in the sidebar is drawn on canvas by pulse-engine.ts.
// pulse-engine reads its style from two CSS variables on :root:
//   --pulse-outlines-only:     "1" / "0"
//   --pulse-outline-strength:  px number (e.g. "1.5")
// We inject a tiny <style> block on :root for instant runtime effect and
// mirror the values into design.overrides for cross-session persistence.

/**
 * Inject (or replace) a :root style block that overrides the pulse blob
 * rendering. Wins over /tokens.css. pulse-engine's MutationObserver picks
 * up the change on the next animation frame.
 */
export function applyPulseOutlineOverride(
  override: PulseOutlineOverride,
  persist = true,
): void {
  if (typeof document === "undefined") return;
  const strength = Number.isFinite(override.strength) && override.strength > 0
    ? override.strength
    : PULSE_OUTLINE_DEFAULTS.strength;
  let style = document.getElementById(
    PULSE_OUTLINE_STYLE_ID,
  ) as HTMLStyleElement | null;
  if (!style) {
    style = document.createElement("style");
    style.id = PULSE_OUTLINE_STYLE_ID;
    document.head.appendChild(style);
  }
  style.textContent =
    `:root{` +
    `--pulse-outlines-only:${override.outlinesOnly ? "1" : "0"};` +
    `--pulse-outline-strength:${strength};` +
    `}`;

  if (persist) {
    try {
      localStorage.setItem(
        PULSE_OUTLINE_LS_KEY,
        JSON.stringify({ outlinesOnly: !!override.outlinesOnly, strength }),
      );
    } catch { /* private mode */ }
  }
}

/** Remove the pulse outline override so defaults reassert. */
export function clearPulseOutlineOverride(): void {
  if (typeof document === "undefined") return;
  const style = document.getElementById(PULSE_OUTLINE_STYLE_ID);
  if (style) style.remove();
  try { localStorage.removeItem(PULSE_OUTLINE_LS_KEY); } catch { /* private mode */ }
}

/**
 * Apply any cached pulse-outline override from localStorage. Safe to call
 * before React mounts — eliminates a frame of wrong-style render after
 * reload.
 */
export function applyPulseOutlineOverrideFromCache(): void {
  if (typeof document === "undefined") return;
  try {
    const cached = localStorage.getItem(PULSE_OUTLINE_LS_KEY);
    if (!cached) return;
    const parsed = JSON.parse(cached) as Partial<PulseOutlineOverride>;
    applyPulseOutlineOverride(
      {
        outlinesOnly: !!parsed.outlinesOnly,
        strength: typeof parsed.strength === "number"
          ? parsed.strength
          : PULSE_OUTLINE_DEFAULTS.strength,
      },
      false,
    );
  } catch { /* private mode or corrupt JSON */ }
}

/**
 * Read pulse-outline overrides from a profile's design.overrides bag and
 * apply them. Called on app boot after profile fetch.
 */
export function applyPulseOutlineOverrideFromProfile(
  profile: Record<string, unknown> | null | undefined,
): void {
  if (!profile) return;
  const design = profile.design as { overrides?: Record<string, string> } | undefined;
  const rawOnly = design?.overrides?.["--pulse-outlines-only"];
  const rawStrength = design?.overrides?.["--pulse-outline-strength"];
  if (rawOnly === undefined && rawStrength === undefined) return;
  const outlinesOnly = rawOnly === "1" || rawOnly === "true";
  const strengthNum = rawStrength !== undefined ? parseFloat(rawStrength) : NaN;
  applyPulseOutlineOverride({
    outlinesOnly,
    strength: Number.isFinite(strengthNum) && strengthNum > 0
      ? strengthNum
      : PULSE_OUTLINE_DEFAULTS.strength,
  });
}
