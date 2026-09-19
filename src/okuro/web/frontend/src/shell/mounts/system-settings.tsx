// SPDX-License-Identifier: Apache-2.0
/**
 * SYSTEM/SETTINGS — four live routes behind one leaf, all four ruled.
 *
 *   /system/settings              pages/settings.tsx        the 15-tab page
 *   /system/settings?view=embed   pages/settings-embed.tsx  ruled, routes.ts:276
 *   /system/settings?view=stt     pages/settings-stt.tsx    ruled, routes.ts:277
 *   /system/settings?view=tts     pages/settings-tts.tsx    ruled, routes.ts:278
 *
 * THE SECTION SLUGS ARE LOAD-BEARING and `views/sections.ts:175-178` already
 * says so: `LEGACY_VIEWS` maps the three live paths onto `?view=` with exactly
 * these spellings, so a rename there sends three live bookmarks back to GENERAL.
 * Resolved here by slug lookup for the same reason — an index pinned as `1`
 * would keep type-checking and start rendering the wrong page the moment a
 * section is inserted above it.
 *
 * `pages/settings.tsx` CARRIES FIFTEEN MORE SUB-VIEWS OF ITS OWN, on `?tab=`
 * via `hooks/use-url-tab.ts`. They keep working untouched: the shell's grammar
 * claims `?view=` and nothing else, so `?tab=identity` passes straight through
 * to the page. Whether those fifteen become `?view=` sections is the SETTINGS
 * leaf pass in p3, not a p2 edit.
 */

import { lazy } from "react";
import { retryOnce } from "@/lib/lazy-with-retry";
import type { LeafViewProps } from "../views/registry";
import { sectionSlugs } from "../views/sections";

const SettingsPage = lazy(
  retryOnce(() => import("@/pages/settings").then((m) => ({ default: m.SettingsPage }))),
);
const SettingsEmbedPage = lazy(
  retryOnce(() =>
    import("@/pages/settings-embed").then((m) => ({ default: m.SettingsEmbedPage })),
  ),
);
const SettingsSttPage = lazy(
  retryOnce(() => import("@/pages/settings-stt").then((m) => ({ default: m.SettingsSttPage }))),
);
const SettingsTtsPage = lazy(
  retryOnce(() => import("@/pages/settings-tts").then((m) => ({ default: m.SettingsTtsPage }))),
);

const SLUGS = sectionSlugs("system", "settings");
const EMBED = SLUGS.indexOf("embed");
const STT = SLUGS.indexOf("stt");
const TTS = SLUGS.indexOf("tts");

export default function SystemSettings({ view }: LeafViewProps) {
  if (EMBED >= 0 && view === EMBED) return <SettingsEmbedPage />;
  if (STT >= 0 && view === STT) return <SettingsSttPage />;
  if (TTS >= 0 && view === TTS) return <SettingsTtsPage />;
  return <SettingsPage />;
}
