// SPDX-License-Identifier: Apache-2.0
/**
 * THE LEAF REGISTRY — one code-split chunk per leaf, and as of p2 every chunk is
 * a REAL OKURO PAGE.
 *
 * WHAT CHANGED IN p2. Until now each line pointed at a read-only sketch of its
 * leaf, written in Phase 4. Those 31 sketches were the failure report's subject
 * (`92ec7f48`): 31 applications replaced by 31 lists of their data, about 3% of
 * the thing being ported, and reported as complete. D2 (`46a64896`) ruled the
 * other direction — "wrap all, rebuild only the bad ones" — so the shell moves
 * INTO the frontend and every line below now names the page that has always
 * served that leaf. The sketches are deleted, not kept beside them: two
 * implementations of one leaf is the divergence nobody would win.
 *
 * PARITY IS BY CONSTRUCTION HERE, WHICH IS THE WHOLE POINT OF D2. Nothing in a
 * page is edited, re-implemented or wrapped in a compatibility layer; the leaf
 * address resolves to the page's own module and the page renders inside
 * `.c-panes` exactly as it renders inside the old `<main>` today.
 *
 * HOW A LEAF BECOMES ITS OWN CHUNK: one line here. The import specifier must be
 * a STATIC STRING — Vite/Rollup resolve chunks at build time, so a computed
 * `import(`@/pages/${leaf}`)` yields either one fat chunk or a glob of
 * everything, never the per-leaf split this exists for.
 *
 * REGISTERING A LEAF IS STILL TWO LINES IN TWO NEIGHBOURING FILES: its module
 * here, its section labels in `sections.ts`. The split is not tidiness — the
 * router has to read a leaf's sections to resolve `?view=`, and the router is in
 * the entry bundle, so a `sections` export on the view module would drag that
 * page (and then all 31) out of its own chunk and back into the entry. Labels
 * are bytes; `pages/settings.tsx` is 4,454 lines. See that file's header.
 *
 * A PAGE TAKES NO PROPS, AND THAT IS NOT AN ACCIDENT OF TYPING. `LeafViewProps`
 * is what the ROUTER resolved — topic, leaf, `?view=` index, detail id — and a
 * page that needs none of it simply ignores all of it, which is what a React
 * component does with props it does not declare. The five leaves that DO need
 * the resolution have a mount in `../mounts/`, one level down, so the choice
 * between two pages at one address is made in a chunk rather than in the entry.
 *
 * EVERY LOADER CARRIES THE STALE-CHUNK GUARD (`lib/lazy-with-retry.ts`). The old
 * route table wrapped all 40 page imports in it; after p2 the route table is
 * gone and these are those imports, so the guard came with them.
 */

import { createElement, lazy, type ComponentType } from "react";
import { retryOnce } from "@/lib/lazy-with-retry";
import type { TopicId } from "../ia";

export interface LeafViewProps {
  topic: TopicId;
  /** the leaf's slug, e.g. `agents` */
  leaf: string;
  /** the detail segment, when the path carries one — `/work/tasks/abc123` */
  id?: string;
  /** which section `?view=` selects; 0 for a bare leaf */
  view: number;
  /**
   * Ask the router to change section. ABSENT for a pane on its way out — a
   * departing view must not follow the address it is leaving.
   */
  onSelectView?: (viewIndex: number) => void;
}

type Loader = () => Promise<{ default: ComponentType<LeafViewProps> }>;

/**
 * `"<topic>/<leaf>"` -> its module. Keyed by the full address rather than the
 * leaf alone because leaf slugs are only unique WITHIN a topic — `assets` and
 * `media` both live under DELIVER today, and nothing stops a future `settings`
 * appearing under two topics.
 *
 * The comment on a line is the live route it answers, so the mapping can be
 * checked against `nav-bar.tsx`'s NAV_TREE without opening a page. Two of them
 * are the traps the inventory found and are spelled out for that reason.
 */
const MODULES: Record<string, Loader> = {
  // START
  "start/inbox": () => import("@/pages/inbox").then((m) => ({ default: m.InboxPage })),
  // live `/` — the index route, not `/now`
  "start/now": () => import("@/pages/home").then((m) => ({ default: m.HomePage })),

  // KNOW
  "know/repos": () => import("../mounts/know-repos"),
  "know/cortex": () => import("@/pages/cortex").then((m) => ({ default: m.CortexPage })),
  "know/notes": () => import("@/pages/notes").then((m) => ({ default: m.NotesPage })),
  "know/knowledge": () =>
    import("@/pages/knowledge").then((m) => ({ default: m.KnowledgePage })),
  "know/brain": () => import("@/pages/brain").then((m) => ({ default: m.BrainPage })),
  "know/lessons": () => import("@/pages/lessons").then((m) => ({ default: m.LessonsPage })),
  "know/corpora": () => import("@/pages/corpora").then((m) => ({ default: m.CorporaPage })),

  // WORK
  "work/bridge": () => import("@/pages/bridge").then((m) => ({ default: m.BridgePage })),
  "work/workflows": () =>
    import("@/pages/workflows").then((m) => ({ default: m.WorkflowsPage })),
  "work/flow": () => import("@/pages/flow").then((m) => ({ default: m.FlowPage })),
  // one leaf, three page files: agents.tsx composes dashboard.tsx + roles.tsx
  "work/agents": () => import("@/pages/agents").then((m) => ({ default: m.AgentsPage })),
  "work/tasks": () => import("../mounts/work-tasks"),
  "work/projects": () =>
    import("@/pages/projects").then((m) => ({ default: m.ProjectsPage })),

  // DELIVER
  "deliver/resonance": () =>
    import("@/pages/resonance").then((m) => ({ default: m.ResonancePage })),
  "deliver/assets": () => import("@/pages/assets").then((m) => ({ default: m.AssetsPage })),
  // NOT a page — the `?view=media` mode of /assets. See the mount.
  "deliver/media": () => import("../mounts/deliver-media"),
  // THE NAME TRAP: live PODCAST is at `/media`, and `/media` is pages/media.tsx.
  // Mapping this from the leaf name would have sent it to the MEDIA grid.
  "deliver/podcast": () => import("@/pages/media").then((m) => ({ default: m.MediaPage })),
  "deliver/studio": () => import("@/pages/studio").then((m) => ({ default: m.StudioPage })),
  "deliver/slides": () => import("../mounts/deliver-slides"),
  "deliver/people": () => import("@/pages/people").then((m) => ({ default: m.PeoplePage })),
  "deliver/prism": () => import("../mounts/deliver-prism"),

  // SYSTEM
  "system/about": () => import("@/pages/about").then((m) => ({ default: m.AboutPage })),
  "system/settings": () => import("../mounts/system-settings"),
  "system/stacks": () => import("@/pages/stack").then((m) => ({ default: m.StackPage })),
  "system/models": () => import("@/pages/models").then((m) => ({ default: m.ModelsPage })),
  "system/scheduled": () =>
    import("@/pages/scheduled").then((m) => ({ default: m.ScheduledPage })),
  "system/health": () => import("@/pages/health").then((m) => ({ default: m.HealthPage })),
  "system/services": () =>
    import("@/pages/services").then((m) => ({ default: m.ServicesPage })),
  // DESIGN is ds-engine-codex, which is what NAV_TREE says (`nav-bar.tsx:100`).
  // `/design-engine` is the OLD authoring page; main deleted it in wave 6 and
  // redirects the path here, so LEGACY does the same. See the p2 report, Q6.
  "system/design": () =>
    import("@/pages/ds-engine-codex").then((m) => ({ default: m.DsEngineCodexPage })),
};

/**
 * THE GAP THAT CANNOT HAPPEN, RENDERED ANYWAY.
 *
 * `sections.test.ts` asserts every IA address has a module, so `viewFor` never
 * reaches this and the sketch view it used to load was dead code — deleted
 * 2026-09-17 along with `SectionPlane`. What is NOT safe is leaving `viewFor`
 * with nothing to return: a future leaf added to the IA without a line above
 * would hand `lazy(undefined)` to React and take the whole shell down, so the
 * "provably unreachable" path would end in a blank window rather than a
 * message. Unreachable BECAUSE A TEST HOLDS IT SO is not the same as
 * impossible, and the difference costs four lines.
 *
 * Deliberately inline and deliberately plain: it must not pull a chunk, must
 * not need the section machinery, and must be impossible to mistake for a
 * finished view.
 */
function MissingView({ topic, leaf }: LeafViewProps) {
  // `.v-prose` rather than a new class: it is the existing body-text block in
  // `views.css`, so this needs no stylesheet of its own.
  return createElement(
    "div",
    { className: "v-prose", role: "status" },
    `No view is registered for ${topic}/${leaf}.`,
  );
}

const cache = new Map<string, ComponentType<LeafViewProps>>();

/**
 * The component for a leaf. Memoised because `lazy()` returns a NEW component
 * type on every call, and a new type remounts the subtree — which would throw
 * away a page's state on every re-render and, worse, restart its entry
 * animation mid-slide.
 */
export function viewFor(topic: TopicId, leaf: string): ComponentType<LeafViewProps> {
  const key = `${topic}/${leaf}`;
  const hit = cache.get(key);
  if (hit) return hit;
  const loader = MODULES[key];
  const view = loader ? lazy(retryOnce(loader)) : MissingView;
  cache.set(key, view);
  return view;
}

/** Which leaves have a mounted page — for reporting, not for routing. */
export function migratedLeaves(): string[] {
  return Object.keys(MODULES);
}
