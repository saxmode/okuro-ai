/**
 * THE NAV TREE — okuro's five groups and their leaves, as DATA.
 *
 * IT USED TO LIVE IN `components/shell/nav-bar.tsx`, and p2 deleted that file:
 * the redesigned shell's sidebar and five topic bars replace the accordion the
 * tree was written for. But the tree itself was never chrome. Three things read
 * it; two are still live and the third went on 2026-09-17:
 *
 *   ui/command-palette.tsx   the Cmd+K "Go to" and "Create" lists
 *   lib/nav-visibility.ts    the one feature-visibility rule
 *   components/shell/mobile-top-bar.tsx   DELETED — unmounted since p2, and
 *                            its last importer was its own test
 *
 * So it moved rather than going with the component. It is also the source the
 * shell's own LEGACY table was READ FROM rather than inferred
 * (`shell/routes.ts:181`), which is how the PODCAST/MEDIA trap was caught:
 * live PODCAST is at `/media` while MEDIA is at `/assets?view=media`, and a
 * name-shaped guess would have been wrong and would have looked right.
 *
 * THE `to` VALUES ARE LIVE PATHS, NOT SHELL ADDRESSES, and that is deliberate.
 * The palette navigates to `/notes`; the shell's LEGACY table redirects it to
 * `/know/notes`. One table owns the mapping, so this file does not have to be
 * edited every time the IA moves — and the feature switch, whose declarations
 * are also live paths (`features.py:245`), keeps matching.
 */

export interface NavLeaf {
  to: string;
  label: string;
  /** Declares a "new X" entry point. The command palette derives a Create action
   *  that navigates to `${to}?new=1`; the target page honors that intent. One
   *  line here surfaces the create action in Cmd+K — no palette edit needed. */
  create?: { label: string };
}
export interface NavGroup {
  label: string;
  children: NavLeaf[];
}

export const NAV_TREE: NavGroup[] = [
  {
    label: "START",
    children: [
      { to: "/", label: "NOW" },
      { to: "/inbox", label: "INBOX" },
      { to: "/projects", label: "PROJECTS" },
    ],
  },
  {
    label: "KNOW",
    children: [
      { to: "/brain", label: "BRAIN" },
      // Sits in KNOW rather than START: these are mined FROM the sessions
      // BRAIN shows, and they are browsed like the rest of this group. The
      // "you have candidates" nudge is already carried by the bootstrap
      // section and the review reminder, which is where the interrupt belongs.
      { to: "/lessons", label: "LESSONS" },
      { to: "/knowledge", label: "KNOWLEDGE" },
      { to: "/notes", label: "NOTES", create: { label: "New note" } },
      { to: "/cortex", label: "CORTEX" },
      { to: "/repos", label: "REPOS" },
      { to: "/corpora", label: "CORPORA" },
    ],
  },
  {
    label: "WORK",
    children: [
      { to: "/work", label: "TASKS" },
      { to: "/agents", label: "AGENTS" },
      { to: "/flow", label: "FLOW", create: { label: "New flow" } },
      { to: "/workflows", label: "WORKFLOWS", create: { label: "New workflow" } },
      { to: "/bridge", label: "BRIDGE" },
    ],
  },
  {
    label: "DELIVER",
    children: [
      { to: "/prism", label: "PRISM", create: { label: "New prism" } },
      { to: "/slides", label: "SLIDES" },
      { to: "/studio", label: "STUDIO" },
      { to: "/media", label: "PODCAST" },
      { to: "/assets?view=media", label: "MEDIA" },
      { to: "/assets", label: "ASSETS" },
      { to: "/resonance", label: "RESONANCE" },
      { to: "/people", label: "PEOPLE" },
    ],
  },
  {
    label: "SYSTEM",
    children: [
      { to: "/services", label: "SERVICES" },
      { to: "/health", label: "HEALTH" },
      { to: "/scheduled", label: "SCHEDULED" },
      { to: "/models", label: "MODELS" },
      { to: "/stack", label: "STACK" },
      { to: "/ds-engine-codex", label: "DESIGN" },
      { to: "/settings", label: "SETTINGS" },
      { to: "/about", label: "ABOUT" },
    ],
  },
];
