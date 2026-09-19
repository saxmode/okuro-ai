// SPDX-License-Identifier: Apache-2.0
/**
 * THE FRAME — the redesigned shell plus the global furniture that used to live
 * in `components/shell/app-shell.tsx`.
 *
 * WHY THIS FILE EXISTS AND `App.tsx` DID NOT ABSORB IT. `App.tsx` is the shell:
 * geometry, motion, the three laws, the address layer, measured against Figma
 * over ~20 rounds. Everything below is okuro's, not the shell's — a command
 * palette, a create dialog, a feedback button, a handover host. Keeping them out
 * of `App.tsx` keeps the signed-off file free of anything a future session could
 * mistake for part of the design.
 *
 * WHAT `AppShell` COMPOSED AND WHERE EACH PIECE WENT — the W1 table, in code:
 *   NavBar, Sidebar, WindowChrome, MobileTopBar    replaced by the shell;
 *                                                  MobileTopBar and the
 *                                                  HealthDot it alone imported
 *                                                  were DELETED 2026-09-17
 *   OkuroThinker                                   DELETED 2026-09-17 — it had
 *                                                  no importer at all
 *   SidebarPanels                                  untouched, unmounted, with
 *                                                  the owner (p2 question 4)
 *   PulseCanvas                                    C3 — the engine is back, as
 *                                                  the sidebar's blob
 *   ChatCapabilities, CommandPalette, CreateDialog,
 *   HandoverHost, ChromeProvider                   rehomed here, unchanged
 *   FeedbackButton                                 C2 — moved INTO the shell's
 *                                                  bottom-right container
 *   the skip link                                  rehomed here
 *
 * CHROMEPROVIDER IS NOT DEAD WITH THE OLD CHROME, which is the trap in dropping
 * it. `useChrome()` no-ops outside its provider, so two live features would have
 * gone quiet with no error: `pages/ds-engine-codex.tsx:75` and
 * `components/flow-designer/flow-designer.tsx:235` both call `hide()`/`show()`
 * as a real fullscreen gesture. The provider stays mounted; what the old chrome
 * did with `hidden` is now the shell's own content-max axis.
 */

import { useState } from "react";
import { useLocation } from "react-router";
import { ChromeProvider } from "@/lib/chrome-context";
import { ChatCapabilities } from "@/components/shell/chat-capabilities";
import { CommandPalette } from "@/components/ui/command-palette";
import { CreateDialog } from "@/components/task/create-dialog";
import { HandoverHost } from "@/components/handover/handover-host";
import { App as Shell } from "./App";
import { CornerActionsProvider } from "./components/Corner";
import { LeafView } from "./components/LeafView";
import { PulseDataProvider } from "./components/PulseData";
import { PinsProvider } from "./components/Pins";
import { leafSlugs, resolveEntry } from "./routes";
import { loadLastLocation, loadRemembered } from "./state";

/**
 * `?embed=1` — the chrome-less mode, ported rather than dropped.
 *
 * It exists so a live okuro·flow chart can be embedded inside a slide
 * (`app-shell.tsx:107-121`), and it renders the addressed page and nothing else:
 * no sidebar, no bars, no pulse. The palette and the handover host still mount,
 * because the view has to be reachable from everywhere and `#main-content` is
 * what `captureSnapshot` targets — the same three the old branch kept, for the
 * same stated reasons.
 *
 * IT READS THE ADDRESS ITSELF rather than borrowing the shell's resolution,
 * because the shell is not rendered in this mode at all. `resolveEntry` is a
 * pure function of the path and the IA, so there is no second source of truth
 * here — just the same function called from a second place.
 */
function EmbeddedLeaf() {
  const { pathname, search } = useLocation();
  const [createOpen, setCreateOpen] = useState(false);
  const r = resolveEntry(pathname, loadRemembered(), search, loadLastLocation());
  const slug = leafSlugs(r.topic)[r.leafIndex] ?? leafSlugs(r.topic)[0] ?? "";

  return (
    <div id="main-content" className="h-screen w-full overflow-hidden bg-surface">
      <LeafView topic={r.topic} leaf={slug} id={r.id} view={r.viewIndex} />
      <CommandPalette onCreateTask={() => setCreateOpen(true)} />
      <CreateDialog open={createOpen} onOpenChange={setCreateOpen} />
      <HandoverHost />
    </div>
  );
}

export function Frame() {
  const [createOpen, setCreateOpen] = useState(false);
  const { search } = useLocation();

  if (new URLSearchParams(search).get("embed") === "1") return <EmbeddedLeaf />;

  return (
    <ChromeProvider>
      {/* The skip link targets the ACTIVE bar's content slot — see the id's note
          in TopicBar. It stays first in the tree so it is the first tab stop. */}
      <a href="#main-content" className="skip-link">
        Skip to content
      </a>
      <ChatCapabilities />
      {/* C2 — THE CORNER'S ACTIONS ARE PROVIDED HERE AND CONSUMED INSIDE `.app`.
          `createOpen` already lived here because the command palette's "create
          new task" entry drives it; the corner's add-task glyph is a SECOND
          caller of the same state, not a second dialog. A context rather than a
          prop through `App.tsx`, so the signed-off shell file learns nothing
          about tasks. */}
      {/* THE BLOB AND THE PANEL'S BAR METER BOTH READ AGENT ACTIVITY, so one
          subscription is provided here and consumed inside `.app` — the same
          boundary reason as `CornerActionsProvider` above. `Blob.tsx`'s old
          "no activity is fed in" subtraction is what this reverses; see
          `PulseData.tsx` for the cost this moves onto every route and the three
          things that keep it affordable. */}
      <PulseDataProvider>
        {/* The panel's five slots are a PROFILE preference, so their store sits
            beside the other frame-level providers rather than inside the shell —
            same boundary reason as the two above. */}
        <PinsProvider>
          <CornerActionsProvider value={{ onAddTask: () => setCreateOpen(true) }}>
            <Shell />
          </CornerActionsProvider>
        </PinsProvider>
      </PulseDataProvider>
      <CommandPalette onCreateTask={() => setCreateOpen(true)} />
      {/* THE ONE `CreateDialog` IN THE FRAME. p3 START S6 found it mounted
          twice — here and again inside `pages/home.tsx`, each with its own
          state, so NOW had a private copy of the dialog behind its own floating
          bubble. C2 removed that bubble and its dialog; this is the survivor,
          and both the palette and the corner's glyph open it. */}
      <CreateDialog open={createOpen} onOpenChange={setCreateOpen} />
      <HandoverHost />
      {/* THE FEEDBACK BUTTON IS NO LONGER FLOATING HERE. The owner ruled it INTO
          the bottom-right action container (todo `9a2e62aa`), and measurement
          said why: its 46x46 fixed button sat at 1295.61,959.61 — inside the
          `.genact` plate's own box (1183.22..1360 x 952.81..1024), on top of the
          plate's third glyph, on all 31 leaves. It now renders as one glyph of
          that container, from `Corner.tsx`. */}
    </ChromeProvider>
  );
}
