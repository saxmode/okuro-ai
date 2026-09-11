// <!-- AGENT_HEADER
// role: code
// purpose: PRISM v4 W5 PHASE-B PROOF entry (NOT shipped) — mounts the REAL
//   DeckShadowHost with the AUDIENCE-REWRITTEN variant fixture (w5-phaseb-deck.json,
//   produced by tests/prism/proof/build_w5_phaseb_deck.py) so the viewer renders the
//   rewritten deck for a screenshot. Additive twin of prism-deck-harness.tsx; never
//   touches the committed w3 fixture.
// AGENT_HEADER_END -->
import { createRoot } from "react-dom/client";
import { DeckShadowHost } from "./components/prism/deck2/deck-shadow-host";
import deck from "./components/prism/deck2/fixtures/w5-phaseb-deck.json";
import type { DeckDoc } from "./components/prism/deck2/deck-types";

const root = createRoot(document.getElementById("prism-harness-root")!);
root.render(
  <div style={{ position: "fixed", inset: 0 }}>
    <DeckShadowHost deck={deck as unknown as DeckDoc} canManage={false} />
  </div>,
);
