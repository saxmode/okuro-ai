import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";
import path from "path";

// PRISM v4 W5 WOW build (not shipped): one self-contained file that mounts the real
// DeckShadowHost with the Robin-Vogt WOW fixture (w5-wow-deck.json), for screenshots.
// Own out-dir; never collides with the SPA / harness / exports / phaseb / benchmark.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      "@kit": path.resolve(__dirname, "../../prism/kit/board"),
    },
  },
  build: {
    outDir: "../export-dist/wow",
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, "wow.html"),
      output: { inlineDynamicImports: true },
    },
  },
});
