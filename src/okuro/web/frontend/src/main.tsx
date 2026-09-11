/** Application entry point: initializes React with cached theme overrides before mount to prevent flashing, renders App in StrictMode. */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app";
import {
  applyPulseOutlineOverrideFromCache,
  applyThemeModeFromCache,
} from "./lib/theme";
// Bundled (Tier A) open-licence webfonts — all 15 curated variable families.
// See lib/fonts-bundle.ts (self-hosted by Vite, lazy-loaded, no CDN).
import "./lib/fonts-bundle";
import "./globals.css";

// Apply cached overrides before React mounts so a chosen setting doesn't
// flash through a default during reload.
//
// THE ACCENT IS NO LONGER AMONG THEM. It used to be applied twice here — once
// from localStorage pre-mount, once as an APCA-derived on-accent foreground
// after load — because the sheet the browser fetched carried the KIT's accent
// and the user's pick had to be painted over it. `/engine.css` now resolves the
// user's accent server-side and re-derives every ink from it, so the first
// sheet parsed is already correct and there is no flash to prevent. Re-adding
// a client-side accent paint here re-creates the defect it was removed for:
// the override moves the colour and leaves the computed foregrounds behind.
applyThemeModeFromCache();
applyPulseOutlineOverrideFromCache();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
