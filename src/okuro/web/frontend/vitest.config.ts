/// <reference types="vitest" />
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  // THE BACKEND'S OWN SOURCE IS A TEST FIXTURE, and Vite refuses to serve it
  // by default: anything outside the project root is a "Denied ID".
  //
  // WHY THAT MATTERS RATHER THAN BEING A NUISANCE. The defect class these
  // tests exist for is a CLIENT ENUM THAT DUPLICATES A SERVER VOCABULARY with
  // nothing asserting the two agree — measured live on 2026-09-14, when
  // `InboxKind` listed eight of the backend's nine kinds and ten of fifty rows
  // rendered a badge with no text. A test that hardcodes the nine names is a
  // third copy of the same list and gates nothing; a test that READS
  // `sense/inbox/scorer.py` fails the moment kind ten is added there.
  //
  // Scoped to `src/okuro/sense` and to the TEST runner only: `vite.config.ts`
  // is untouched, so the dev server's fs allowlist is exactly what it was.
  server: {
    fs: { allow: [path.resolve(__dirname), path.resolve(__dirname, "../../sense")] },
  },
  test: {
    environment: "jsdom",
    globals: false,
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    setupFiles: ["./vitest.setup.ts"],
  },
});
