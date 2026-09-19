// SPDX-License-Identifier: Apache-2.0
/**
 * THE INFORMATION ARCHITECTURE, verbatim from Figma r21uoRRsv3ycN3QdNYI1Nk.
 *
 * Extracted 2026-09-13 via the Figma REST API — not from screenshots and not
 * from a prior session's claims. `OKURO-NAV-ELEMENT` exposes exactly
 * `nav-child-1..8`, and DELIVER uses all eight, so this IA sits at its hard
 * ceiling: a ninth item under any topic is a COMPONENT change, not a data edit.
 *
 * FOUR LIVE LEAVES HAD NO FIGMA HOME and the owner placed them 2026-09-14:
 * PROJECTS under WORK (projects are run work), LESSONS and CORPORA under KNOW
 * (both are knowledge), DESIGN under SYSTEM (system setup). That takes SYSTEM
 * to 8/8 — a SECOND topic at the hard ceiling alongside DELIVER. KNOW sits at
 * 7/8 and WORK at 6/8, so the whole IA now has four free slots, none of them
 * under DELIVER or SYSTEM.
 *
 * EACH WAS APPENDED, NOT INSERTED. Order is the direction law and `pathFor`
 * addresses a leaf by INDEX, so inserting would silently repoint every
 * remembered index and every existing bookmark below the insertion point.
 * Appending leaves all existing indices untouched. Position WITHIN a topic was
 * not ruled on; a different left-to-right order is a deliberate edit here plus
 * a redirect for anything that moved.
 */

export type TopicId = "start" | "know" | "work" | "deliver" | "system";

export interface Topic {
  readonly id: TopicId;
  /** The bar label. Rendered at weight 900, tracking .14em. */
  readonly label: string;
  /**
   * The topic TAGLINE — DATA ONLY AS OF R1, NOT RENDERED ANYWHERE.
   *
   * It used to be the content container's title. The owner ruled R1 on
   * 2026-09-14: *"Title should be the leaf title. Right now settings say 'Setup
   * your personal okuro' which stays the same everywhere and just wastes
   * space."* So `.c-title` renders `titles[sub]` below, and this string is read
   * by nothing — `TopicBar` was its only reader, measured before the change
   * (one hit, `TopicBar.tsx:256`), and the whole frontend has no other.
   *
   * KEPT RATHER THAN DELETED because it is the only place the five topics'
   * one-line descriptions are written down, and a later shell-level pattern
   * (a topic overview, a tooltip on the rail, a document title) is the obvious
   * consumer. Deleting it would throw away authored copy to save five strings.
   * If nothing claims it by p5, delete it at the cutover.
   */
  readonly title: string;
  /**
   * Sub-items, in physical left-to-right order. Order IS the direction law.
   *
   * THESE ARE THE RAIL'S LABELS and they are authored UPPERCASE because that
   * is the rail's own typography. They are NOT the document title — see
   * `titles`.
   */
  readonly kids: readonly string[];
  /**
   * THE LEAF TITLES — one per `kids` entry, same order, and the string the
   * content container renders as the document's one `<h1>` (R1, `86b8f1f0`).
   *
   * WHY A SECOND ARRAY RATHER THAN `kids[i]` DIRECTLY. The owner's own example is
   * *"'Scheduled' for example"* — title case, not `SCHEDULED`. Case is steered
   * by the design system and never hardcoded (Q6, `0d37d05e`): the kit's
   * `--type-display-transform` decides whether the rendered glyphs are upper or
   * lower, so the AUTHORED string has to be the neutral one. Under the active
   * `standard` kit that transform is `uppercase` and this paints SCHEDULED;
   * under `okuro-ds` it paints Scheduled. Reusing the rail's uppercase label as
   * the title would have hardcoded the case into the data, where no kit can
   * reach it.
   *
   * AND WHY NOT DERIVED AT RENDER TIME. All 31 leaves happen to be single
   * words, so `kid[0] + kid.slice(1).toLowerCase()` is correct for every one of
   * them today — and silently wrong for the first two-word leaf. Authored, with
   * `sections.test.ts` asserting `titles[i].toUpperCase() === kids[i]` and
   * equal lengths, the pair cannot drift and a two-word leaf is a deliberate
   * edit rather than a rendering accident.
   */
  readonly titles: readonly string[];
}

export const IA: readonly Topic[] = [
  {
    id: "start",
    label: "START",
    title: "Start with okuro now",
    kids: ["INBOX", "NOW"],
    titles: ["Inbox", "Now"],
  },
  {
    id: "know",
    label: "KNOW",
    title: "Everything you and your agents know inside okuro",
    kids: ["REPOS", "CORTEX", "NOTES", "KNOWLEDGE", "BRAIN", "LESSONS", "CORPORA"],
    titles: ["Repos", "Cortex", "Notes", "Knowledge", "Brain", "Lessons", "Corpora"],
  },
  {
    id: "work",
    label: "WORK",
    title: "Run the work",
    kids: ["BRIDGE", "WORKFLOWS", "FLOW", "AGENTS", "TASKS", "PROJECTS"],
    titles: ["Bridge", "Workflows", "Flow", "Agents", "Tasks", "Projects"],
  },
  {
    id: "deliver",
    label: "DELIVER",
    title: "Ship it to the right shape",
    kids: ["RESONANCE", "ASSETS", "MEDIA", "PODCAST", "STUDIO", "SLIDES", "PEOPLE", "PRISM"],
    titles: ["Resonance", "Assets", "Media", "Podcast", "Studio", "Slides", "People", "Prism"],
  },
  {
    id: "system",
    label: "SYSTEM",
    title: "Setup your personal okuro",
    kids: ["ABOUT", "SETTINGS", "STACKS", "MODELS", "SCHEDULED", "HEALTH", "SERVICES", "DESIGN"],
    titles: ["About", "Settings", "Stacks", "Models", "Scheduled", "Health", "Services", "Design"],
  },
] as const;

/** The `--sh-topics` token must agree with the IA or the content formula is wrong. */
export const TOPIC_COUNT = IA.length;

export function topicIndex(id: TopicId): number {
  return IA.findIndex((t) => t.id === id);
}

export function topic(id: TopicId): Topic {
  const t = IA.find((x) => x.id === id);
  if (!t) throw new Error(`unknown topic: ${id}`);
  return t;
}

/**
 * THE ONE `<h1>` A ROUTE PUTS IN THE FRAME — R1.
 *
 * Clamped rather than checked: `resolveEntry` already guarantees a valid leaf
 * index for every address it resolves, so an out-of-range read here would mean
 * the resolver was bypassed. Falling back to the first leaf keeps the heading
 * present in that case, because a document with no `<h1>` is a worse failure
 * than a document with the wrong one.
 */
export function leafTitle(t: Topic, leafIndex: number): string {
  return t.titles[leafIndex] ?? t.titles[0] ?? t.label;
}
