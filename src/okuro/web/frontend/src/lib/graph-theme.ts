// SPDX-License-Identifier: Apache-2.0
/**
 * THE ONE PLACE `@xyflow/react` LEARNS WHICH APPEARANCE IT IS IN.
 *
 * WHY THIS FILE EXISTS. `@xyflow/react/dist/style.css` is a vendor sheet with
 * its own light and dark blocks, selected by a `colorMode` prop that defaults
 * to `light`. Six surfaces in okuro mount `<ReactFlow>` and, before this pass,
 * exactly ONE passed the prop — and it passed the literal `"dark"`, so it was
 * wrong in light appearance rather than wrong in dark. Measured on
 * `/deliver/people` at HEAD b46e297db, kit `standard`:
 *
 *   data-appearance   .react-flow className
 *   dark              "react-flow light"
 *   light             "react-flow light"
 *
 * The theme was PINNED, not merely inverted, which is why the person nodes read
 * as near-invisible circles on the dark ground.
 *
 * WHY IT IS A SHARED MODULE AND NOT A LINE IN `PeopleGraph`. R6 (372ccdb2):
 * *one shared graph module for editors AND read-only viewers*, and the IA
 * ruling behind it (0e3cfeaa item 1) says the graph engine is ONE platform
 * module ported once and mounted many. Fixing `PeopleGraph.tsx` alone is the
 * instance fix DP11 names. `components/graph/` does not exist yet — the module
 * boundary is WORK's pass to draw (FLOW, WORKFLOWS and TASKS?view=gantt share
 * `flow-designer/flow-canvas.tsx`) — so this is the minimal shared piece that
 * pass will absorb: one hook, no layout, no node types, nothing to unpick.
 * That module now exists — `components/graph/` with three entry points — and
 * this hook stays in `lib/` rather than moving into it, because four of the
 * six mounts are read-only viewers that import nothing else from the module.
 *
 * THE ADOPTION LEDGER — CLOSED 2026-09-15 in the p4 WORK pass. All six
 * `@xyflow/react` mounts in okuro pass this hook; there is no seventh:
 *
 *   components/graph/canvas/flow-canvas.tsx      WORK x3           adopted
 *   components/people/PeopleGraph.tsx            DELIVER/PEOPLE    adopted
 *   components/knowledge/unified-graph.tsx       KNOW/KNOWLEDGE    adopted (was the literal "dark")
 *   components/notes/note-graph.tsx              KNOW/NOTES        adopted
 *   components/repos/repo-code-graph.tsx         KNOW/REPOS        adopted
 *   components/prism/graph-block.tsx             DELIVER/PRISM     adopted
 *
 * `flow-canvas.tsx` is the one that matters most and it is now inside the
 * shared module: FLOW, WORKFLOWS and TASKS?view=gantt all mount it, so one
 * adoption covers three leaves. R8 still defers PRISM's own leaf pass — a
 * one-line colorMode prop is not a leaf pass, and leaving the last mount on
 * the vendor default would have kept the class open for no reason.
 *
 * Read it ONCE into a const in the component body. A hook called inline in
 * JSX is one refactor from a conditional-hook violation, which is the trap
 * the p4 DELIVER pane-gate adoption hit out loud.
 *
 * WHY A MutationObserver AND NOT A CONTEXT. The appearance is a DOM attribute
 * written by `lib/theme.ts::setAppearance` on `documentElement`, deliberately:
 * every piece of state the shell exposes to CSS is a data attribute, and the
 * engine sheet keys its ground block on this one. There is no React state to
 * subscribe to, and inventing one would make a second authority for a question
 * the attribute already answers — which is exactly the defect
 * `setAppearance`'s own header describes. So this reads the attribute and
 * watches it.
 */

import { useEffect, useState } from "react";

import { liveAppearance, type ThemeMode } from "./theme";

/** react-flow's own prop type, named here so callers do not retype it. */
export type GraphColorMode = "dark" | "light";

/**
 * The appearance react-flow should render in, tracking the app's.
 *
 * Every `<ReactFlow>` mount passes this as `colorMode`. It is the whole
 * theme bridge: react-flow's vendor sheet then selects its own matching block,
 * and the ~60 `.react-flow__*` overrides a page carries stop fighting a sheet
 * that was in the wrong mode to begin with.
 */
export function useGraphColorMode(): GraphColorMode {
  const [mode, setMode] = useState<ThemeMode>(() => liveAppearance());

  useEffect(() => {
    // Re-read on mount as well as on change: the attribute can have moved
    // between the lazy chunk's first render and this effect.
    setMode(liveAppearance());
    const observer = new MutationObserver(() => setMode(liveAppearance()));
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-appearance"],
    });
    return () => observer.disconnect();
  }, []);

  return mode;
}
