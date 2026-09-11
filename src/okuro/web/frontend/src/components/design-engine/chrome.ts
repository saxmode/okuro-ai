/**
 * The chrome contract, mirrored for the places TypeScript needs a number.
 *
 * `chrome.css` is the authority — every value below restates one of its
 * `--ce-*` tokens. The mirror exists because three things genuinely cannot be
 * expressed in a stylesheet: a media query evaluated in JS (`matchMedia` for
 * the app-chrome collapse), an anchor computation for the inspector, and the
 * width a component reports to a measurement.
 *
 * THE UNIT RULE, restated so it cannot be missed at the one place a number is
 * typed: these are PIXELS. `html { font-size: 50% }` is ratified, so a chrome
 * length written in `rem` renders at half its intended size. The emitted sheet
 * inside the iframe is rem and must stay rem — that is the system's OUTPUT.
 * The page's chrome is px. `scale.ts` is the engine mirror and is a different
 * thing entirely: it converts a builder's pixel into a brand's factor.
 *
 * `px = factor × 8` (BASE), resolved here at author time so the engine's factor
 * vocabulary survives into the chrome without the chrome depending on the root.
 */

/** BASE, from `scale.ts`. Restated as the arithmetic below, never imported —
    a chrome length must not move when a brand's numbers do. */
const F = 8;

export const CE = {
  /* spacing — the ratio is the rule: inter-group = 2 × intra-group */
  f05: 0.5 * F, //  4  icon↔label, swatch↔value
  f1: 1 * F, //  8  inside one control row
  f15: 1.5 * F, // 12  a control ↔ its help slot
  f2: 2 * F, // 16  INTRA-GROUP
  f3: 3 * F, // 24  block padding in the rail
  f4: 4 * F, // 32  INTER-GROUP
  f6: 6 * F, // 48  block padding in a full-width region

  /* type — five sizes, floor 12 */
  title: 20,
  h2: 16,
  body: 14,
  label: 13,
  micro: 12,

  /* geometry — three heights */
  hPrimary: 40,
  hControl: 32,
  hChip: 24,
  rControl: 6,
  rSurface: 8,

  /* structure */
  rail: 320,
  inspector: 400,
  measure: 880,
} as const;

/**
 * Below this width the page collapses the APP's own chrome to a 48px strip.
 *
 * The page is a workbench, not a document. At 1440 the app's 334px shell plus a
 * 320px rail leaves the canvas 31.5 % of the viewport — measured — and the
 * canvas is the thing the page is FOR. The gesture and its Escape restore
 * already exist in `useChrome()`; this only decides when to fire it.
 */
/* The engine is a presentation surface as well as an editor. At ordinary
   desktop widths the product chrome costs the demo more than a quarter of its
   canvas, so it yields by default and remains one gesture away. */
export const CHROME_COLLAPSE_BELOW = 2200;

/** The 48px strip that brings the app chrome back. */
export const CHROME_STRIP = 48;

/**
 * How long a BASE INTERACTION takes, and it is not a brand's to author.
 *
 * A hover, a focus ring, a press: the user is the animator and the transition
 * only stops the change from being a jump-cut. `kit.motion.speeds` are MOTION
 * speeds — entrances, exits, fades, the growth beats — and the default of that
 * scale is `medium`, 650ms, which on a hover reads as a component that has not
 * yet agreed to be hovered. So this number lives with the chrome constants,
 * beside the note that a chrome length must not move when a brand's numbers do.
 *
 * It is the TS mirror of `--interaction-duration` in `globals.css`; the frames
 * need the number because they are separate documents that never see that file.
 * Deliberately not a member of the canonical speed set
 * (150 / 350 / 650 / 850 / 1200 / 1500), so no kit can match it by coincidence.
 */
export const INTERACTION_MS = 120;

/** A `px` string, for a style prop. The one place a number becomes CSS in TS. */
export function ce(value: number): string {
  return `${value}px`;
}
