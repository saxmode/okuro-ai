import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";
import path from "path";

// PRISM v4 W4 PROOF harness build (not shipped): one self-contained file that
// mounts the real DeckShadowHost with the W3 fixture, so Playwright can drive the
// full viewer offline. Own out-dir so it never collides with the SPA / exports.
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      "@kit": path.resolve(__dirname, "../../prism/kit/board"),
    },
  },
  build: {
    outDir: "../export-dist/harness",
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, "harness.html"),
      output: { inlineDynamicImports: true },
    },
  },
});
