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
    // 3000-3099 is the host's convention for web UIs, and 3071 specifically is
    // where the redesigned shell has been reviewed since p1 — it was the sibling
    // app's port, and p2 made the sibling app into this one's frame, so the port
    // came with it. `strictPort` because a silent fallback to 5173 means the
    // reviewer opens the port they were told and sees whatever was there before.
    port: 3071,
    strictPort: true,
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
      // THE ENGINE SHEET, and without it the dev server is not okuro. index.html
      // links `/engine.css` (line 52) and it is deliberately NOT under /api,
      // because a stylesheet fetch cannot attach a bearer — so it needs its own
      // proxy entry rather than riding the one above. In production it is
      // same-origin and served by the daemon. Every colour, type size, radius
      // and motion value in both the shell and the pages resolves through it;
      // unproxied, dev renders the fallback literals and every measurement
      // taken against it is a measurement of the fallbacks.
      "/engine.css": {
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
