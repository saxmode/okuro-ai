// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism deck v2 — kit asset bridge. Imports the board kit CSS/JS
//   (src/okuro/prism/kit/board) as raw text via the @kit vite alias (single
//   source of truth — never copied), rewrites the `:root`/`body` selectors to
//   `:host` so the token/theme cascade works inside a Shadow root, and exposes
//   the kit's own behaviour globals (window.PrismZoomStage / window.PrismReveal)
//   by injecting the kit scripts once. The deck renders inside a shadow root so
//   the kit CSS cannot leak into the SPA (it redefines --accent/--border/body)
//   and the SPA cannot leak into the deck. The kit stays the styling authority.
// AGENT_HEADER_END -->
import tokensCss from "@kit/tokens.css?raw";
import themeCss from "@kit/theme.css?raw";
import componentsCss from "@kit/components.css?raw";
// Ported + high-cardinality components (chart, evidence, matrix, dense-table, …)
// and the composed-slide grid — needed so the v4 solver's composed cells render.
import componentsExtCss from "@kit/components-ext.css?raw";
import composedCss from "@kitgallery/composed.css?raw";
import zoomStageCss from "@kit/zoom-stage.css?raw";
// Side-effect imports: the kit's zoom-stage + reveal IIFEs attach
// window.PrismZoomStage / window.PrismReveal when executed. Importing them as
// bundled modules (not raw text injected as an inline <script>) keeps them
// under script-src 'self' — the live CSP forbids inline scripts, which is why
// injecting raw text left the deck stage blank in production. The kit files
// stay valid classic scripts for the standalone gallery.
import "@kit/zoom-stage.js";
import "@kit/reveal.js";

/** The kit's zoom-stage API (board/zoom-stage.js). */
export interface PrismZoomStageApi {
  apply: (stage: HTMLElement) => void;
  fitScale: (stage: HTMLElement) => number;
}
/** The kit's scroll-reveal API (board/reveal.js). */
export interface PrismRevealApi {
  scan: (root?: ParentNode) => void;
  revealAll: (root?: ParentNode) => void;
}
declare global {
  interface Window {
    PrismZoomStage?: PrismZoomStageApi;
    PrismReveal?: PrismRevealApi;
  }
}

/**
 * Rewrite `:root` / `body` / `html` selectors to `:host` so the kit's token and
 * theme cascade (authored for the light DOM document root) applies inside a
 * shadow root. Deterministic, load-time only — the kit files on disk are never
 * edited. `:root[data-theme="X"]` must become the functional `:host(...)` form.
 */
function rootToHost(css: string): string {
  return css
    .replace(/:root\[([^\]]+)\]/g, ":host($1)")
    .replace(/:root\b/g, ":host")
    .replace(/^\s*body\s*\{/gm, ":host {")
    .replace(/^\s*html\s*\{/gm, ":host {");
}

/** The full kit stylesheet for a deck shadow root, in gallery load order:
 *  structural tokens → brand theme → components → zoom-stage. Token/theme files
 *  are host-scoped; class-scoped files pass through unchanged. */
export const KIT_SHADOW_CSS: string = [
  rootToHost(tokensCss),
  rootToHost(themeCss),
  componentsCss,
  componentsExtCss,
  composedCss,
  zoomStageCss,
].join("\n");

/**
 * No-op retained for call-site compatibility. window.PrismZoomStage /
 * window.PrismReveal are now set by the side-effect module imports above
 * (bundled under script-src 'self'), so no runtime script injection is needed.
 * The prior inline-<script> injection was blocked by the production CSP.
 */
export function ensureKitGlobals(): void {
  /* globals are installed at module load by the side-effect imports */
}

export function zoomStageApi(): PrismZoomStageApi | undefined {
  return typeof window !== "undefined" ? window.PrismZoomStage : undefined;
}
export function revealApi(): PrismRevealApi | undefined {
  return typeof window !== "undefined" ? window.PrismReveal : undefined;
}
