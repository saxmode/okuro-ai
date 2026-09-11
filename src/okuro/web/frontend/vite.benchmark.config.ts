import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";
import path from "path";

// PRISM v4 W5 Phase-D BENCHMARK build (not shipped): one self-contained file that
// mounts the real DeckShadowHost with the multi-perspective benchmark fixture
// (w5-benchmark-deck.json), so the viewer renders lens tabs + L1 visuals +
// provenance + luminance logo for screenshots. Own out-dir; never collides with the
// SPA / harness / exports / phaseb.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      "@kit": path.resolve(__dirname, "../../prism/kit/board"),
    },
  },
  build: {
    outDir: "../export-dist/benchmark",
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, "benchmark.html"),
      output: { inlineDynamicImports: true },
    },
  },
});
