/**
 * Bundled webfont @font-face declarations (Tier A, self-hosted by Vite).
 *
 * Side-effect imports only — importing a Fontsource `wght.css` declares that
 * family's variable @font-face. Vite fingerprints + self-hosts the WOFF2 (no
 * CDN, offline/LAN-ok). Browsers lazy-load a face only when a design profile
 * actually references the family, so importing all of them upfront is free.
 *
 * MUST stay in sync with BUNDLED_FONTS in ./fonts.ts (one import per entry) —
 * enforced by fonts.test.ts.
 */

// Sans
import "@fontsource-variable/inter/wght.css";
import "@fontsource-variable/roboto/wght.css";
import "@fontsource-variable/geist/wght.css";
import "@fontsource-variable/open-sans/wght.css";
import "@fontsource-variable/noto-sans/wght.css";
// Static exception (no variable version) — a brand font. Per-weight
// faces; browsers lazy-load only the weights actually used.
import "@fontsource/be-vietnam-pro/100.css";
import "@fontsource/be-vietnam-pro/200.css";
import "@fontsource/be-vietnam-pro/300.css";
import "@fontsource/be-vietnam-pro/400.css";
import "@fontsource/be-vietnam-pro/500.css";
import "@fontsource/be-vietnam-pro/600.css";
import "@fontsource/be-vietnam-pro/700.css";
import "@fontsource/be-vietnam-pro/800.css";
import "@fontsource/be-vietnam-pro/900.css";
// Mono
import "@fontsource-variable/roboto-mono/wght.css";
import "@fontsource-variable/geist-mono/wght.css";
import "@fontsource-variable/jetbrains-mono/wght.css";
import "@fontsource-variable/spline-sans-mono/wght.css";
import "@fontsource-variable/noto-sans-mono/wght.css";
// Serif
import "@fontsource-variable/noto-serif/wght.css";
import "@fontsource-variable/faustina/wght.css";
import "@fontsource-variable/lora/wght.css";
import "@fontsource-variable/newsreader/wght.css";
import "@fontsource-variable/literata/wght.css";
