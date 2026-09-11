import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";
import path from "path";
import { okuroAliases } from "./vite.aliases";

// Build for the deck2 single-file HTML export (PRISM v4 W4 / R33). Renders the
// SAME DeckShadowHost the live viewer uses, so nothing drifts. viteSingleFile
// inlines every asset (incl. the board-kit CSS/JS imported via @kit) so the file
// opens offline anywhere — full L1–L4 ladder + 2D nav + zoom + accuracy, no server.
//
// Out-dir is web/export-dist/deck (its OWN subdir) so the legacy export build's
// emptyOutDir never wipes it and vice-versa.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  resolve: { alias: okuroAliases(__dirname) },
  build: {
    outDir: "../export-dist/deck",
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, "export-deck.html"),
      output: { inlineDynamicImports: true },
    },
  },
});
