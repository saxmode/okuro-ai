/**
 * DESIGN'S OWN CHROME, handed to the shared authoring components.
 *
 * His rule of 2026-09-13, verbatim: "i want to make sure, that no old parts
 * overwrite new parts in the ds-engine-codex" (memory df6e21cb). Wave 4 moved the
 * live inheritance proof into `components/design-authoring/` still written in
 * `/design-engine`'s `.ce-*`, and made this page import that page's `chrome.css`
 * so the rules would arrive. The logic was right to move; the look was not.
 *
 * This is the answer in DESIGN's own two vocabularies, and they are two on
 * purpose:
 *
 *   `.dsc-*`   this page's chrome — structure, surfaces, chips. Authored in
 *              `ds-engine-codex.css`, in px, like every other `.dsc-*` rule.
 *   `.ds-*`    the ENGINE's classes, emitted into `/engine.css`. Sizes and type
 *              bands are the system's own answer and this page already writes
 *              every heading it owns that way (`ds-h3 ds-headings`,
 *              `ds-n5 ds-paragraphs`). A size typed here would be a second
 *              authority for a number the engine already publishes.
 *
 * NO COLOUR IS TYPED BELOW. Every `--dsc-*` name resolves, at the top of
 * `ds-engine-codex.css`, to an engine token — `--color-foreground-primary` and
 * friends. `tests/design_engine/test_drift.py` walks this page's folder asking
 * the other direction's question: does the app read a value the engine never
 * emitted.
 */

import type { AuthoringVocabulary } from "@/components/design-authoring/vocabulary";

export const CODEX_VOCABULARY: AuthoringVocabulary = {
  scene: "dsc-nest-scene",
  sceneIntro: "dsc-nest-intro",
  stack: "dsc-nest-stack",
  stackTight: "dsc-nest-stack-tight",
  groups: "dsc-groups",
  row: "dsc-nest-row",
  sectionHeading: "dsc-section-heading",
  surface: "dsc-nest-surface",
  stage: "dsc-nest-stage",
  tableWrap: "dsc-table-wrap",
  table: "dsc-nest-table",
  scroller: "dsc-nest-scroller",

  /* SIZE COMES FROM THE ENGINE, band class beside size class — the form the
     emitter documents and the rest of this page already uses. */
  kicker: "ds-n8 ds-leads",
  display: "ds-h5 ds-headings",
  title: "ds-n4 ds-headings",
  h2: "ds-n6 ds-headings",
  body: "ds-n7 ds-paragraphs",
  label: "ds-n8 ds-leads",
  micro: "ds-n8 ds-paragraphs",
  value: "ds-n7 ds-leads",
  dim: "dsc-dim",

  /* The ground chip this page already authored, and already hands to
     `RootPlacementChips` in the toolbar above. One chip, one look. */
  chip: "dsc-ground-chip ds-n8 ds-leads",
  control: "dsc-nest-control",
  primary: "dsc-nest-primary ds-n7 ds-leads",
  /* Wave 2. The authored fields moved here need a low-emphasis button and a
     resolved-colour square; both are authored in `ds-engine-codex.css` beside
     this page's other `.dsc-*` rules, in px, and neither types a colour. */
  quiet: "dsc-quiet ds-n8 ds-leads",
  swatch: "dsc-swatch",

  measure: "var(--dsc-measure)",
  strongForeground: "var(--dsc-fg)",
  mutedForeground: "var(--dsc-muted)",
  border: "var(--dsc-rule)",
  controlRadius: "var(--dsc-r-control)",
  mutedSurface: "var(--dsc-surface-2)",
  /* Wave 3. Resolved by `ds-engine-codex.css`'s `[data-severity]` rules, which
     answer with the ENGINE's own status tokens — the same three this page's
     field-flag dots already wear. Nothing is typed here either. */
  severityColour: "var(--dsc-severity, var(--dsc-fg))",
};
