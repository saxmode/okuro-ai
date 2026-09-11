// <!-- AGENT_HEADER
// role: code
// purpose: PRISM v4 W4 PROOF harness (NOT shipped in the SPA) — mounts the REAL
//   DeckShadowHost with the committed W3 E2E fixture baked in, so the full viewer
//   (nav, orientation, transitions, accuracy, controls) can be driven offline by
//   Playwright with no backend. `?manage=1` turns on the management controls;
//   control clicks + brand switches are recorded to window.__harnessCalls and the
//   brand restyle is applied locally (mirrors PrismDeckPage). Built to a single
//   self-contained file via vite.harness.config.ts.
// AGENT_HEADER_END -->
import { createRoot } from "react-dom/client";
import { DeckShadowHost } from "./components/prism/deck2/deck-shadow-host";
import w3 from "./components/prism/deck2/fixtures/w3-e2e-deck.json";
import type { BrandId, DeckDoc } from "./components/prism/deck2/deck-types";

declare global { interface Window { __harnessCalls?: unknown[] } }

const manage = new URLSearchParams(location.search).get("manage") === "1";
const calls: unknown[] = (window.__harnessCalls = []);
const rec = (name: string) => (...args: unknown[]) => calls.push([name, ...args]);

let deck = w3 as unknown as DeckDoc;
const root = createRoot(document.getElementById("prism-harness-root")!);

function render() {
  root.render(
    <div style={{ position: "fixed", inset: 0 }}>
      <DeckShadowHost
        deck={deck}
        canManage={manage}
        onBrandSwitch={(b: BrandId) => { deck = { ...deck, brand: b }; rec("brand")(b); render(); }}
        onExport={rec("export")}
        onDelete={rec("delete")}
        onRetailor={rec("retailor")}
        onPickAlt={rec("pick")}
      />
    </div>,
  );
}
render();
