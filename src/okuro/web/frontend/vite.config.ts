import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
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
    alias: {
      "@": path.resolve(__dirname, "./src"),
      // The prism board kit (styling authority) lives outside the frontend src.
      // The deck v2 runtime imports its CSS/JS as ?raw to inject into a shadow
      // root — single source of truth, never copied (no drift). fs.allow below
      // grants the dev server read access to that sibling package dir.
      "@kit": path.resolve(__dirname, "../../prism/kit/board"),
      // The composed-slide grid CSS (.composed-slide/-row/-cell) lives in the kit
      // gallery, not board — the deck's ComposedCell injects it so the solver's
      // grid placement renders in the viewer exactly as in the gallery.
      "@kitgallery": path.resolve(__dirname, "../../prism/kit/gallery"),
    },
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
