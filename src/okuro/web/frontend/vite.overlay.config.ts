import { defineConfig } from "vite";
import path from "path";

// Build for the redline in-frame overlay — the SECOND build target this
// module needs, and deliberately its own config file rather than a rollup
// input on the SPA build.
//
// Why separate: the bundle is served INTO a document that is framed with
// `sandbox="allow-scripts"` and no `allow-same-origin`, under REDLINE_CSP.
// That policy carries `connect-src 'none'` and no `https:` anywhere, so the
// bundle must be ONE self-contained file with no imports and no dynamic
// chunk fetches — a code-split ES-module entry would 404 its own chunks
// inside the frame with nothing to show for it. `formats: ["iife"]` is what
// makes that a build-time guarantee instead of a review note.
//
// Out-dir is web/dist-overlay (a sibling of web/dist and web/export-dist, NOT
// a subdirectory of either) because the SPA build runs with `emptyOutDir:
// true` and would otherwise wipe the bundle on every rebuild — the same
// reason vite.export.config.ts sits in export-dist.
export default defineConfig({
  // The SPA's public/ (PWA manifest, service worker, icons) is meaningless
  // here and would land beside the bundle as clutter the serve route would
  // then be asked about. One file out, nothing else.
  publicDir: false,
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  build: {
    outDir: "../dist-overlay",
    emptyOutDir: true,
    target: "es2020",
    lib: {
      entry: path.resolve(__dirname, "src/redline/overlay.ts"),
      name: "RedlineOverlay",
      formats: ["iife"],
      fileName: () => "overlay.js",
    },
  },
});
