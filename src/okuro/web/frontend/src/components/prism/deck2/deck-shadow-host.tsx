// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism deck v2 — shadow-DOM host. Renders the deck inside a
//   Shadow root so the kit stylesheet (which redefines --accent/--border/body)
//   cannot leak into the SPA and the SPA cannot leak into the deck — the kit
//   renders exactly as its specimen gallery. React events don't cross a shadow
//   boundary reliably, so the deck runs in its OWN React root created ON the
//   shadow root (listeners attach inside the boundary). Brand = data-theme on the
//   host (:host([data-theme=…]) in the kit theme). Deep-link nav is via the URL
//   hash, so the nested root needs no router context.
// AGENT_HEADER_END -->
import { useEffect, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { BrandId, DeckDoc, DeckLevel } from "./deck-types";
import { DeckApp } from "./deck-app";
import { KIT_SHADOW_CSS, ensureKitGlobals } from "./kit-assets";
import chromeCss from "./deck-chrome.css?raw";

export interface DeckShadowHostProps {
  deck: DeckDoc;
  onPickAlt?: (row: "hero" | DeckLevel, col: number, pick: number) => void;
  onBrandSwitch?: (brand: BrandId) => void;
  onRetailor?: () => void;
  onExport?: () => void;
  onDelete?: () => void;
  canManage?: boolean;
  className?: string;
}

const STYLE = KIT_SHADOW_CSS + "\n/* chrome */\n:host { display: block; height: 100%; }\n" + chromeCss;

export function DeckShadowHost(props: DeckShadowHostProps) {
  const { deck, className } = props;
  const hostRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<Root | null>(null);
  // latest props for the imperative nested render
  const propsRef = useRef(props);
  propsRef.current = props;

  // create shadow + nested React root once (StrictMode-safe: fresh mount node)
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    ensureKitGlobals();
    const shadow = host.shadowRoot ?? host.attachShadow({ mode: "open" });
    if (!shadow.querySelector("style[data-prism-kit-style]")) {
      const style = document.createElement("style");
      style.dataset.prismKitStyle = "1";
      style.textContent = STYLE;
      shadow.appendChild(style);
    }
    // fresh mount node each setup so createRoot never double-binds a node
    shadow.querySelector(".deck-mount")?.remove();
    const mount = document.createElement("div");
    mount.className = "deck-mount";
    mount.style.height = "100%";
    shadow.appendChild(mount);
    const root = createRoot(mount);
    rootRef.current = root;
    const p = propsRef.current;
    host.dataset.theme = p.deck.brand;
    root.render(<DeckApp {...p} />);
    return () => {
      rootRef.current = null;
      root.unmount();
      shadow.querySelector(".deck-mount")?.remove();
    };
  }, []);

  // re-render the nested root when any prop changes
  useEffect(() => {
    const host = hostRef.current;
    if (host) host.dataset.theme = deck.brand;
    rootRef.current?.render(<DeckApp {...props} />);
  });

  return <div ref={hostRef} className={className} style={{ height: "100%" }} data-testid="deck-host" />;
}
