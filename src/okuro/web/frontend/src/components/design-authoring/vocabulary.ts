/**
 * THE HOST PAGE'S CHROME, DECLARED AS A TYPE — logic is shared, chrome is not.
 *
 * Convention 786f04cd, in the form the compiler can hold: a component in this
 * folder paints with the class names its HOST hands it and publishes `data-*`
 * for anything a gate reads. Nothing here names a class, a colour or a size —
 * this file is the CONTRACT between a shared component and the page mounting it,
 * and a default value would be one page's chrome pretending to be neutral.
 *
 * WHY A TYPE AND NOT A CSS VARIABLE. `nesting.tsx` used to read `--ce-fg`,
 * `--ce-border`, `--ce-measure` straight out of the cascade — a requirement that
 * a stylesheet somewhere would please define these names, unenforceable and
 * silent when unmet. Wave 4 paid for exactly that failure in its CLASS form: the
 * rules were *permitted* on `/ds-engine-codex` and never *loaded*, and a scope
 * that matches nothing is indistinguishable from a stylesheet that is absent
 * (gotcha 6a6ad27d). A requirement published as a prop cannot be silently unmet:
 * `tsc` refuses the call.
 *
 * WHAT BELONGS IN HERE vs WHAT DOES NOT. Engine names — `.ds-n7`, `.ds-headings`,
 * `--color-background-base` — are the SYSTEM's own vocabulary, emitted into
 * `/engine.css`, which every surface in this app consumes. A shared component may
 * name those directly. What it may never name is one page's chrome: `.ce-*` and
 * `--ce-*` belong to `/design-engine`, `.dsc-*` and `--dsc-*` belong to DESIGN,
 * and a component that types either has picked a side.
 *
 * DIRECTION, his rule of 2026-09-13 (memory df6e21cb, convention 786f04cd):
 * "no old parts overwrite new parts in the ds-engine-codex". Logic travels from
 * the old page to this folder and DESIGN consumes it; the old page's LOOK never
 * travels with it.
 */

/**
 * Every role a shared authoring component paints, named by what it MEANS rather
 * than by what either page calls it. A host supplies one of these; the strings
 * are that page's own class names and its own already-resolved token references.
 */
export interface AuthoringVocabulary {
  /* ── structure ─────────────────────────────────────────────────────────── */
  /** The outermost block of a scene. */
  scene: string;
  /** A scene's introductory header. */
  sceneIntro: string;
  /** A vertical stack at the page's ordinary rhythm. */
  stack: string;
  /** A vertical stack at the tighter, intra-group rhythm. */
  stackTight: string;
  /**
   * A column of GROUPS — the inter-group rhythm, with whatever boundary the
   * page draws between them.
   *
   * Wave 5. `stack` is the rhythm INSIDE one group and `groups` is the rhythm
   * BETWEEN them, and the two are a documented 2:1 ratio on `/design-engine`.
   * The essentials sheet is authored against the wider one, so a component that
   * substituted `stack` would halve every boundary on the page it moved off.
   */
  groups: string;
  /** A horizontal row: baseline-aligned, wrapping, gapped. */
  row: string;
  /** A titled sub-section's heading block (kicker + heading, side by side). */
  sectionHeading: string;
  /** A raised panel that can hold content of its own. */
  surface: string;
  /** The two-column stage the nesting proof is authored for. */
  stage: string;
  /** A horizontally scrollable wrapper for a table. */
  tableWrap: string;
  /** A data table. */
  table: string;
  /** A block that scrolls rather than growing past its container. */
  scroller: string;

  /* ── type ──────────────────────────────────────────────────────────────── */
  /** The eyebrow above a heading. */
  kicker: string;
  /** The largest heading a scene owns. */
  display: string;
  /** A sub-section heading. */
  title: string;
  /** A heading below `title`. */
  h2: string;
  /** Running prose. */
  body: string;
  /** A field or group label. */
  label: string;
  /** The smallest legible size the page uses. */
  micro: string;
  /** A read-back value: what the engine answered, usually monospaced. */
  value: string;
  /** Applied ALONGSIDE another type class to step the colour back. */
  dim: string;

  /* ── controls ──────────────────────────────────────────────────────────── */
  /** A small toggle. Carries `aria-pressed`; the component never styles it. */
  chip: string;
  /** A form control — an input or a select. */
  control: string;
  /** The one action a scene leads with. */
  primary: string;
  /**
   * A low-emphasis button: a remove ×, a reset link. Wave 2 — the additional
   * colours editor and the shade controls both need one.
   */
  quiet: string;
  /**
   * A small square showing a resolved colour. Wave 2 — every colour a shared
   * authored field reads back is shown as one, and no page may be asked to
   * guess the size of it.
   */
  swatch: string;

  /* ── values a stylesheet cannot be asked for ───────────────────────────── */
  /**
   * The comfortable reading width, as a CSS length. A page hands in its own
   * name (`var(--dsc-measure)`) or a number; the component only ever sets it.
   */
  measure: string;
  /** Full-strength foreground, as a CSS colour. */
  strongForeground: string;
  /** Stepped-back foreground, as a CSS colour. */
  mutedForeground: string;
  /** The hairline colour, as a CSS colour. */
  border: string;
  /** The corner a control wears, as a CSS length. */
  controlRadius: string;
  /** The ground a quiet block sits on, as a CSS colour. */
  mutedSurface: string;
  /**
   * The colour of the severity currently in scope, as a CSS colour.
   *
   * Wave 3. A shared judgement block paints a glyph and a dot in the tone of
   * the finding beside it, and it must NEVER decide that tone itself — which
   * severity is in scope is published as `data-severity` on the block, and each
   * page's own stylesheet answers with its own token. So this string is a
   * `var(--…)` with a fallback, resolved by the host's rule for
   * `[data-severity="must-fix"]` and friends.
   *
   * Colour is never the only channel: the glyph beside it comes from
   * `verdict.tsx::GLYPH_FOR` and is not the host's to change.
   */
  severityColour: string;
}
