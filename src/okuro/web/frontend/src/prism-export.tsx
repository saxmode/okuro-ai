/**
 * Standalone entry for an exported Prism deck — a single self-contained HTML
 * file (all JS/CSS inlined by vite-plugin-singlefile). Reads the deck payload
 * the backend injected into window.__PRISM_EXPORT__ and mounts the read-only
 * deck. No router, no API, no service worker — the file opens offline anywhere.
 */
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";

import { PrismExportDeck, type ExportPayload } from "./components/prism/prism-export-deck";
import "./globals.css";

const payload = (window as unknown as { __PRISM_EXPORT__?: ExportPayload }).__PRISM_EXPORT__;
const root = document.getElementById("prism-export-root");

if (root) {
  createRoot(root).render(
    payload && payload.doc ? (
      // A router context is required only because some block renderers use
      // react-router hooks for in-prose links; the deck itself never navigates.
      <MemoryRouter>
        <PrismExportDeck payload={payload} />
      </MemoryRouter>
    ) : (
      <div style={{ padding: "2rem", fontFamily: "system-ui", color: "#888" }}>
        This exported deck is empty or failed to load.
      </div>
    ),
  );
}
