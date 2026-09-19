/**
 * HOW LONG A BASE INTERACTION TAKES, AND IT IS NOT A BRAND'S TO AUTHOR.
 *
 * A hover, a focus ring, a press: the user is the animator and the transition
 * only stops the change from being a jump-cut. `kit.motion.speeds` are MOTION
 * speeds — entrances, exits, fades, the growth beats — and the default of that
 * scale is `medium`, 650ms, which on a hover reads as a component that has not
 * yet agreed to be hovered.
 *
 * Deliberately NOT a member of the canonical speed set
 * (150 / 350 / 650 / 850 / 1200 / 1500), so no kit can match it by coincidence.
 * `tests/design_engine/test_showcase.py` asserts exactly that against every kit
 * in the live store, not only the shipped one.
 *
 * IT IS THE APP'S NUMBER, WHICH IS WHY IT LEFT THE PAGE FOLDER. It lived in
 * `components/design-engine/chrome.ts` beside the lengths of one page's chrome,
 * and it is the TS mirror of `--interaction-duration` in `globals.css` — which
 * Tailwind reads as `--default-transition-duration`, so this number times every
 * `transition-*` utility in all 33 vendored primitives, on every page. Behind a
 * page that is scheduled for deletion, the oracle for an app-wide constant was
 * one `rm` away from being deleted with the page it was visiting.
 *
 * `chrome.ts` re-exports it so the frames that publish it into their own
 * documents are unchanged: a preview frame is a separate document and never
 * sees `globals.css`, so it has to write the number itself.
 */

export const INTERACTION_MS = 120;
