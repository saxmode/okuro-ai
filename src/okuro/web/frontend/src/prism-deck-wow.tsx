// <!-- AGENT_HEADER
// role: code
// purpose: PRISM v4 W5 WOW render entry (NOT shipped) — mounts the REAL
//   DeckShadowHost with the Robin-Vogt WOW fixture (w5-wow-deck.json, produced by
//   tests/prism/proof/build_w5_wow_vision.py) so the viewer renders the vision-
//   forward single-audience deck (L1 visuals + provenance + luminance logo) for
//   screenshots. Additive twin of the benchmark entry.
// AGENT_HEADER_END -->
import { createRoot } from "react-dom/client";
import { DeckShadowHost } from "./components/prism/deck2/deck-shadow-host";
import deck from "./components/prism/deck2/fixtures/w5-wow-deck.json";
import type { DeckDoc } from "./components/prism/deck2/deck-types";

const root = createRoot(document.getElementById("prism-harness-root")!);
root.render(
  <div style={{ position: "fixed", inset: 0 }}>
    <DeckShadowHost deck={deck as unknown as DeckDoc} canManage={false} />
  </div>,
);
