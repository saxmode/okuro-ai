// <!-- AGENT_HEADER
// role: code
// purpose: Standalone entry for an exported deck2 deck (PRISM v4 W4 / R33) — a
//   single self-contained HTML file (all JS/CSS inlined by vite-plugin-singlefile,
//   kit CSS/JS bundled). Reads the DeckDoc the backend injected into
//   window.__PRISM_DECK__ and mounts the SAME DeckShadowHost the live viewer uses,
//   read-only (canManage=false → no brand/export/delete/retailor). Full L1–L4
//   ladder + 2D nav + zoom + peek + grid + accuracy work offline; no router, no
//   API, no server. The deck's brand is baked in (data-theme on the shadow host).
// AGENT_HEADER_END -->
import { createRoot } from "react-dom/client";
import { DeckShadowHost } from "./components/prism/deck2/deck-shadow-host";
import type { DeckDoc } from "./components/prism/deck2/deck-types";

const payload = (window as unknown as { __PRISM_DECK__?: { deck: DeckDoc } }).__PRISM_DECK__;
const root = document.getElementById("prism-deck-export-root");

if (root) {
  createRoot(root).render(
    payload?.deck ? (
      <div style={{ position: "fixed", inset: 0 }}>
        <DeckShadowHost deck={payload.deck} canManage={false} />
      </div>
    ) : (
      <div style={{ padding: "2rem", fontFamily: "system-ui", color: "#888" }}>
        This exported deck is empty or failed to load.
      </div>
    ),
  );
}
