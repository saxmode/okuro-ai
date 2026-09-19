// SPDX-License-Identifier: Apache-2.0
/**
 * THE DERIVED GEOMETRY, as functions rather than numbers.
 *
 * Nothing here runs in the shell — the CSS owns the live layout, and duplicating
 * it in TS would be a second source of truth. What these functions exist for is
 * the OTHER half of the job: a test can assert the arithmetic, and a
 * verification run can state an expectation BEFORE it measures, instead of
 * reading a number off the page and calling it correct.
 *
 * "A still frame hid four separate defects in this build." Every constant below
 * is therefore paired with the CSS expression it mirrors, so a drift between
 * them fails a test rather than shipping.
 *
 * THE TEST THAT MAKES THAT SENTENCE TRUE IS `tests/web/test_shell_geometry_live.py`
 * at the repository root — a Playwright gate, run with
 * `.venv/bin/python -m pytest tests/web/test_shell_geometry_live.py`. Until
 * 2026-09-17 it did not exist — the promise was prose, `sidebarCollapsed`
 * had drifted 24px, and the whole shell suite was green. The guard reads
 * COMPUTED values out of a running browser rather than parsing a stylesheet,
 * because two of the three ways this file can be wrong are invisible to a
 * parse: a kit that re-bases `--sh-s-*`, and a reserved scrollbar gutter that
 * no token declares at all (see `restingStates`).
 *
 * NOTHING IN HERE TOUCHES THE DOM. The functions stay pure arithmetic so the
 * unit tests can state an expectation without a browser; every measured input —
 * the frame width, the reserved gutter — is passed in by the caller.
 */

/**
 * The measured token values, in one place, mirroring `styles/tokens.css`.
 *
 * EVERY ENTRY NAMES THE CSS IT MIRRORS, and the live gate named in the file
 * header reads that CSS out of a running browser and fails when the two
 * disagree. It also fails when an entry is ADDED here without a probe there,
 * so the pairing cannot quietly go missing again: three of these entries used
 * to carry no comment at all, and `sidebarCollapsed` sat 24px wrong with two
 * green tests certifying it.
 *
 * The rem values below resolve at 8px/rem — `globals.css:838` sets the root to
 * `50 %` of the browser's 16px default. That is why `9rem` is 72 and not 144.
 */
export const TOKENS = {
  /** `--sh-icon: 2rem` — tokens.css:395 */
  icon: 16,
  /** `--sh-winctl-glyph: var(--sh-icon)` — tokens.css:505 */
  winctlGlyph: 16,
  /** `--sh-bar-collapsed: 6rem` — tokens.css:591 */
  barCollapsed: 48,
  /** `--sh-sidebar-open: 37.5rem` — tokens.css:583 */
  sidebarOpen: 300,
  /**
   * `--sh-sidebar-collapsed: 9rem` — tokens.css:590.
   *
   * 72, NOT 48. This read 48 until 2026-09-17 because the collapsed BAR is 48
   * and both tokens said "collapsed"; the CSS was corrected from the owner's
   * annotated frame and this mirror was not, so `window.__okuroShell.expected`
   * published a plane 24px too wide on every navigation. The live test is what
   * stops the pair separating again.
   */
  sidebarCollapsed: 72,
  /** the bar icon's own left padding inside a collapsed bar —
   *  `.bar-ico{left:var(--sh-s-16)}`, shell.css:788 */
  iconPad: 16,
  /** the window-control plate's padding toward the frame edge —
   *  `.winctl{padding:var(--sh-s-32) …}`, shell.css:1953 */
  chromeOuterPad: 32,
  /** the window-control plate's padding toward the content —
   *  `.winctl{padding:… var(--sh-s-16)}`, shell.css:1953 */
  chromeInnerPad: 16,
  /** the top line: a 16px row whose top edge sits 32px down —
   *  `.bar-ico{top:var(--sh-s-32)}`, shell.css:788 */
  topLinePad: 32,
  /** the frame's own LEFT margin — `--sh-inset: var(--sh-s-16)`, tokens.css:581.
   *  NOT the right gutter; the two were one token until C5 separated them. */
  frameInset: 16,
  /** `--sh-strip-h: 8rem` — tokens.css. The menu row above the title band. */
  stripH: 64,
  /**
   * THE BAND'S FLOOR, not its height — A-3 #11.
   *
   * `--sh-band-h` is `max(var(--sh-s-72), <measured row> + var(--sh-s-16))`, so
   * 72 is what the band is when its row fits one line and nothing more. It was
   * the band's whole height until 2026-09-18, which is why a mirror that
   * carried one number for it would now be wrong at half the addresses.
   */
  bandFloor: 72,
  /** `.c-band{padding-bottom:var(--sh-s-16)}` — the gap under the row, and the
   *  term that turns a measured ROW height into a BAND height. */
  bandPadBottom: 16,
} as const;

/**
 * THE BAND'S HEIGHT, AND IT HAS TWO STATES RATHER THAN ONE NUMBER — A-3 #11.
 *
 * The owner ruled 2026-09-17 that the title bar takes a second row ONLY when the
 * title and the controls do not fit on one. So the band's height is a function
 * of its row, not a constant, and the oracle has to know the function or it can
 * only certify one of the two states.
 *
 * `rowHeight` IS MEASURED BY THE CALLER, for the same reason every other input
 * here is: TypeScript cannot read the kit, and a row's height depends on the
 * rung (32px display type at XL, 40px at XXL), on the viewport, and on which
 * controls the leaf handed over. Pass what the browser reported for
 * `.c-band-row`; this returns what `--sh-band-h` must resolve to.
 *
 * Mirrors `tokens.css`: `max(var(--sh-s-72), calc(var(--sh-band-row) + var(--sh-s-16)))`.
 */
export function bandHeight(
  rowHeight: number,
  floor: number = TOKENS.bandFloor,
  padBottom: number = TOKENS.bandPadBottom,
): number {
  return Math.max(floor, rowHeight + padBottom);
}

/**
 * THE WHOLE TOP CHROME SURFACE — the strip plus the band.
 *
 * Mirrors `--sh-plate-h: calc(var(--sh-strip-h) + var(--sh-band-h))`, which is
 * read by `.top-plate`'s height AND by `.content`'s padding-top. Those two are
 * in different subtrees and MUST agree — the plate covers exactly what the page
 * reserves — so this is the one function whose drift is visible as a hole under
 * the plate rather than as a wrong number.
 */
export function plateHeight(
  rowHeight: number,
  stripH: number = TOKENS.stripH,
): number {
  return stripH + bandHeight(rowHeight);
}

/**
 * THE RIGHT GUTTER IS AN ALIGNMENT CONSTRAINT, NOT A MARGIN.
 *
 * Mirrors `--sh-gutter` in `tokens.css`.
 *
 *   SYSTEM icon centre     = W - gutter - barCollapsed + iconPad + icon/2
 *   rightmost glyph centre = W - chromeOuterPad - winctlGlyph/2
 *
 * Equate and the frame width drops out entirely:
 *   gutter = chromeOuterPad + iconPad - barCollapsed + (winctlGlyph + icon)/2
 *
 * C5, 2026-09-15 — THE LEADING THREE TERMS USED TO BE CANCELLED AWAY, and that
 * was the bug. `32 + 16 - 48` is 0 under okuro-ds, so the whole group was
 * dropped and this function returned `(g + i) / 2`. But `chromeOuterPad` is
 * `--sh-s-32` and `iconPad` is `--sh-s-16` — both KIT-SCALED — so under
 * `standard` they are 36.8 and 18.4, the group is 7.2 rather than 0, and the
 * constraint missed by exactly that. Keeping the group written out is what makes
 * the dependency visible in both languages (`96ee0e9b`).
 *
 * THE SPACING TERMS ARE PARAMETERS BECAUSE TYPESCRIPT CANNOT READ THE KIT.
 * The defaults are okuro-ds's, which is the reference kit and the one every p1
 * proof was signed off against, so `rightGutter()` still returns 16. A caller
 * measuring under another kit passes that kit's scale and gets that kit's
 * answer, instead of a number that silently describes okuro-ds.
 *
 * Figma's 12px glyph gives (12+16)/2 = 14, which is why its collapsed rail
 * stops at 1352. The mockup corrected the glyph to 16 for the top line but left
 * the gutter literal at 14, so the constraint missed by 2px — measured, SYSTEM
 * icon centre 1328 against last glyph centre 1326.
 */
export function rightGutter(
  winctlGlyph: number = TOKENS.winctlGlyph,
  icon: number = TOKENS.icon,
  chromeOuterPad: number = TOKENS.chromeOuterPad,
  iconPad: number = TOKENS.iconPad,
  barCollapsed: number = TOKENS.barCollapsed,
): number {
  return chromeOuterPad + iconPad - barCollapsed + (winctlGlyph + icon) / 2;
}

/** Where SYSTEM's bar icon centres, when SYSTEM is NOT the active bar. */
export function systemIconCentreX(frameWidth: number, gutter: number = rightGutter()): number {
  return frameWidth - gutter - TOKENS.barCollapsed + TOKENS.iconPad + TOKENS.icon / 2;
}

/** Where the rightmost window-control glyph centres. Constant in all 20 states. */
export function lastGlyphCentreX(frameWidth: number): number {
  return frameWidth - TOKENS.chromeOuterPad - TOKENS.winctlGlyph / 2;
}

/**
 * THE CONTENT WIDTH, and the one length that does not move during a topic
 * switch. Mirrors
 * `calc(100cqw - var(--sh-gutter-safe) - (var(--sh-topics) - 1) * var(--sh-bar-w-safe))`.
 *
 * `planeWidth` IS STILL THE PLANE'S BORDER BOX LESS THE GUTTER, so this
 * function and every caller are unchanged — but WHICH ELEMENT subtracts the
 * gutter moved on 2026-09-18 (D5) and the old wording would now mislead.
 *
 * It used to read: "100cqw resolves against the content box, so the gutter is
 * already excluded." The gutter WAS `.plane`'s `padding-right` and was
 * therefore outside `100cqw` for free. It is now the last bar's `margin-right`
 * and `100cqw` is the plane's border box, so the stylesheet subtracts the
 * gutter explicitly instead. The arithmetic is identical — `planeContentWidth`
 * still returns border minus gutter — which is why the resting geometry is
 * byte-identical and the drift guard still agrees.
 *
 * The reason for the move is in `shell.css` at `.plane`: WebKitGTK stops
 * re-evaluating a container query during a transition, so an eased padding on
 * the container froze this width mid-switch for 436ms at a 14.95px error.
 *
 * During a topic switch NEITHER term moves, which is why the content carries no
 * width transition (Law 2).
 */
export function contentWidth(planeWidth: number, topics: number, barW: number): number {
  return planeWidth - (topics - 1) * Math.max(0, barW);
}

/**
 * The plane's BORDER box — what `getBoundingClientRect()` reports.
 *
 * The left INSET is `.app`'s padding and is STATIC; only the plane's own
 * padding-right (`--sh-gutter-w`) animates. That asymmetry is deliberate and
 * measurable: the plane's border box is identical in `normal` and `max`.
 *
 * C5 — IT IS `--sh-inset`, NOT THE RIGHT GUTTER. These two parameters defaulted
 * to `rightGutter()` because okuro-ds makes both 16; the right gutter is now
 * kit-aware, and a left margin must not move because a right-edge alignment
 * constraint did.
 */
export function planeBorderWidth(
  frameWidth: number,
  sidebar: number,
  leftInset: number = TOKENS.frameInset,
): number {
  return frameWidth - leftInset - sidebar;
}

/**
 * The plane's CONTENT box — what `100cqw` resolves to, and therefore the term
 * the content-width formula actually starts from.
 */
export function planeContentWidth(
  frameWidth: number,
  sidebar: number,
  gutterW: number,
  leftInset: number = TOKENS.frameInset,
): number {
  return planeBorderWidth(frameWidth, sidebar, leftInset) - Math.max(0, gutterW);
}

/**
 * Every element on the upper edge centres on y = 40: a 16px row 32px down.
 * One number, and the only exception in Figma (window controls at 38) is a slip
 * this build corrects rather than reproduces.
 */
export const TOP_LINE_Y = TOKENS.topLinePad + TOKENS.icon / 2;

/**
 * Both chrome plates are 64 tall because they MIRROR each other:
 * 32 toward the frame edge, 16 toward the content.  32+16+16 = 16+16+32 = 64.
 */
export const CHROME_PLATE_H =
  TOKENS.chromeOuterPad + TOKENS.winctlGlyph + TOKENS.chromeInnerPad;

/**
 * The divider hugs its bar's content rather than running full height.
 * 64 is the lead before the swing's own box: 32 pad + 16 icon + 16 of the 24
 * gap that falls before it. Static decoration, never animated.
 */
export function dividerHeight(swingLength: number): number {
  return 64 + swingLength;
}

/** The four resting states, as a declared expectation table. */
export interface RestingState {
  panel: "open" | "collapsed";
  content: "normal" | "max";
  /** border box, comparable with getBoundingClientRect */
  plane: number;
  activeBar: number;
  activeContent: number;
}

/**
 * THE GUTTER HAS TWO ZEROING CONDITIONS, AND THE SECOND ONE IS TOPIC-DEPENDENT.
 *
 * `--sh-gutter-w` drops to 0 in content-max (there are no collapsed bars left to
 * align) AND when SYSTEM is the active topic (its icon has moved to the left
 * edge of its own expanded bar, so nothing remains on the right to align to).
 *
 * That second condition is why a `know -> system` switch is the ONE topic
 * switch during which the content width legitimately moves: measured, it grows
 * by exactly 16px — the gutter — while every other switch measures a spread of
 * 0.000. Modelling it here rather than narrowing the claim is what keeps the
 * expectation table a real oracle instead of one that quietly excuses a miss.
 */
export function gutterIsZero(content: "normal" | "max", activeTopicIsSystem: boolean): boolean {
  return content === "max" || activeTopicIsSystem;
}

/**
 * THE APP IS NARROWER THAN THE VIEWPORT, AND NO TOKEN SAYS SO.
 *
 * `globals.css:829` gives `html{scrollbar-gutter:stable}` and
 * `globals.css:948-949` sets the bar to 2px, so the document reserves that width
 * whether or not it is scrolling. The reservation lands on `body`, which is
 * `.app`'s containing block — MEASURED in Chromium at 1366: `innerWidth` 1366,
 * `documentElement.clientWidth` 1366, `body.clientWidth` **1364**, `.app` 1364.
 *
 * SO `window.innerWidth` IS THE WRONG INPUT and was the oracle's second error,
 * worth 2px on every row and a DIFFERENT class from the token drift: no amount
 * of comparing TS constants against token values can see it, because there is
 * no token to compare against.
 *
 * IT IS A PARAMETER RATHER THAN A CONSTANT because the two engines disagree and
 * both are right. `globals.css:928-933` records the measurement: Chromium
 * honours the reservation at `html` (2px), WebKitGTK reserves nothing there
 * (0px). A caller that passes what it MEASURED gets the right answer on either
 * engine; a caller that hardcoded 2 would be wrong on one of them. `App.tsx`
 * passes `window.innerWidth - document.body.clientWidth`.
 */
export function restingStates(
  frameWidth: number,
  topics: number,
  activeTopicIsSystem = false,
  rootScrollbarGutter = 0,
): RestingState[] {
  const g = rightGutter();
  // What the app actually lays out in, which is the viewport LESS whatever the
  // document reserved for its scrollbar before `.app` ever saw it.
  const frame = frameWidth - Math.max(0, rootScrollbarGutter);
  const out: RestingState[] = [];
  for (const panel of ["open", "collapsed"] as const) {
    for (const content of ["normal", "max"] as const) {
      const sidebar = panel === "open" ? TOKENS.sidebarOpen : TOKENS.sidebarCollapsed;
      // content-max drives BOTH --sh-bar-w and --sh-gutter-w to zero.
      const barW = content === "max" ? 0 : TOKENS.barCollapsed;
      const gutterW = gutterIsZero(content, activeTopicIsSystem) ? 0 : g;
      // The LEFT term is the frame inset, the RIGHT term the gutter. Passing
      // `g` for both was the conflation C5 separated.
      const width = contentWidth(
        planeContentWidth(frame, sidebar, gutterW, TOKENS.frameInset), topics, barW);
      // The active bar and its content are the SAME width in every resting
      // state: the bar grows into exactly the space the formula describes.
      out.push({
        panel,
        content,
        // NO THIRD ARGUMENT. It used to pass `g`, the right gutter, which is
        // the conflation C5 separated — right by coincidence, because okuro-ds
        // makes both 16. The parameter already defaults to `frameInset`, so the
        // correct call is the one that says nothing and lets the default hold.
        plane: planeBorderWidth(frame, sidebar),
        activeBar: width,
        activeContent: width,
      });
    }
  }
  return out;
}
