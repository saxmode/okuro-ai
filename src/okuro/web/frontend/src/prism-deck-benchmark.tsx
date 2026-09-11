// <!-- AGENT_HEADER
// role: code
// purpose: PRISM v4 W5 PHASE-D BENCHMARK render entry (NOT shipped) — mounts the
//   REAL DeckShadowHost with the multi-perspective benchmark fixture
//   (w5-benchmark-deck.json, produced by tests/prism/proof/build_w5_benchmark_deck.py
//   offline or by the live E2E run) so the viewer renders lens tabs + L1 visuals +
//   provenance + luminance logo for screenshots. Additive twin of the phaseb entry.
// AGENT_HEADER_END -->
import { createRoot } from "react-dom/client";
import { DeckShadowHost } from "./components/prism/deck2/deck-shadow-host";
import deck from "./components/prism/deck2/fixtures/w5-benchmark-deck.json";
import type { DeckDoc } from "./components/prism/deck2/deck-types";

const root = createRoot(document.getElementById("prism-harness-root")!);
root.render(
  <div style={{ position: "fixed", inset: 0 }}>
    <DeckShadowHost deck={deck as unknown as DeckDoc} canManage={false} />
  </div>,
);
