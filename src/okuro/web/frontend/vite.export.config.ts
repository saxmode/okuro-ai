import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";
import path from "path";
import { okuroAliases } from "./vite.aliases";

// Build for the Prism single-file HTML export. Separate from the main SPA build
// so the app stays code-split while each exported deck is ONE self-contained
// file: viteSingleFile inlines every asset, and `inlineDynamicImports` folds the
// lazy renderers (mermaid / graph / flow) into the single bundle so nothing is
// fetched at runtime — the file opens offline anywhere.
//
// Out-dir is web/export-dist (NOT web/dist) because the main build runs with
// `emptyOutDir: true` and would otherwise wipe this template on every rebuild.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  resolve: { alias: okuroAliases(__dirname) },
  build: {
    outDir: "../export-dist",
    emptyOutDir: true,
    // Service worker / PWA is meaningless in an offline file; skip it.
    rollupOptions: {
      input: path.resolve(__dirname, "export.html"),
      output: { inlineDynamicImports: true },
    },
  },
});
