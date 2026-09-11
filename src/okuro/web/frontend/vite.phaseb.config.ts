import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";
import path from "path";

// PRISM v4 W5 Phase-B PROOF build (not shipped): one self-contained file that mounts
// the real DeckShadowHost with the AUDIENCE-REWRITTEN variant fixture, so the viewer
// renders the rewritten deck for a screenshot. Own out-dir; never collides with the
// SPA / harness / exports.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      "@kit": path.resolve(__dirname, "../../prism/kit/board"),
    },
  },
  build: {
    outDir: "../export-dist/phaseb",
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, "phaseb.html"),
      output: { inlineDynamicImports: true },
    },
  },
});
