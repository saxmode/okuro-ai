import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { okuroAliases } from "./vite.aliases";
import { readFile, writeFile } from "node:fs/promises";

// Stamp `__BUILD_ID__` in dist/sw.js with a build-time timestamp so the browser
// detects a new service worker on every deploy, installs it, and triggers the
// `controllerchange` handler in index.html → open tabs auto-reload onto the
// new bundle. See sw.js for the other half.
function swBuildStamp(): Plugin {
  return {
    name: "okuro-sw-build-stamp",
    apply: "build",
    async closeBundle() {
      const swPath = path.resolve(__dirname, "../dist/sw.js");
      try {
        const src = await readFile(swPath, "utf8");
        const stamped = src.replace('"__BUILD_ID__"', `"${Date.now()}"`);
        await writeFile(swPath, stamped);
      } catch (err) {
        this.warn(`sw build-stamp skipped: ${(err as Error).message}`);
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), swBuildStamp()],
  resolve: {
    // Shared with the two export configs — see vite.aliases.ts for why.
    alias: okuroAliases(__dirname),
  },
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ["react", "react-dom", "react-router"],
          query: ["@tanstack/react-query"],
          ui: ["clsx", "tailwind-merge", "class-variance-authority", "lucide-react"],
          // Heavy, route-local libs — pinned to their own chunks so they
          // never leak into the entry bundle. markdown is lazy-loaded by
          // markdown-content; flow libs are only used by the /flow route.
          markdown: ["react-markdown", "remark-gfm"],
          flow: ["@xyflow/react", "@dagrejs/dagre"],
        },
      },
    },
  },
  server: {
    fs: {
      // Allow importing the prism board kit assets (?raw) from the sibling
      // package dir for the deck v2 shadow-DOM runtime.
      allow: [path.resolve(__dirname, "."), path.resolve(__dirname, "../../prism/kit")],
    },
    proxy: {
      "/api": {
        target: "http://localhost:13333",
        changeOrigin: true,
      },
      "/health": {
        target: "http://localhost:13333",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://localhost:13333",
        ws: true,
      },
    },
  },
});
