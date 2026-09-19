// SPDX-License-Identifier: Apache-2.0
/**
 * THE ONE MEASURED LENGTH IN THE ADAPTIVE BAND — A-3 #11.
 *
 * ===========================================================================
 * WHAT THIS DOES NOT DO, FIRST, BECAUSE IT IS THE PART THAT MATTERS
 * ===========================================================================
 * It does NOT decide whether the title bar has one row or two. That decision is
 * `flex-wrap` on `.c-band-row` — the engine's own line-breaking against the
 * real type at the real rung — and it is taken without a script, a token or a
 * per-leaf declaration. This hook only REPORTS the height that decision
 * produced, to the one place CSS cannot reach it from.
 *
 * ===========================================================================
 * WHY A MEASUREMENT IS UNAVOIDABLE, AND IT IS STRUCTURAL RATHER THAN LAZY
 * ===========================================================================
 * The brief asked for a CSS-only mechanism and this is the reason there is not
 * one. Three facts, each checkable in the stylesheet:
 *
 *   1. `.content` MUST RESERVE THE PLATE'S HEIGHT and it is not below the
 *      plate. The plate is `position:absolute` on `.app`, OUTSIDE the scroller,
 *      because page content scrolls UNDER it and gives it something to blur.
 *      So `.content`'s `padding-top` and `.top-plate`'s `height` are two boxes
 *      in two subtrees that have to agree, and CSS has no way to make one
 *      element's padding follow another element's rendered height.
 *
 *   2. `anchor-size()` DOES NOT RESCUE IT, and it is supported in both engines
 *      (measured: `CSS.supports('height','anchor-size(--a height)')` is true in
 *      Chromium 145 AND WebKitGTK 2.52.6, which is why it was worth checking).
 *      The spec admits it only for ABSOLUTELY POSITIONED elements. `.content`
 *      is the scroller; it is not one, and making it one to win a padding
 *      value would cost the scroll container.
 *
 *   3. THE TRACK CANNOT BE CONTENT-SIZED EITHER. Its five `.c-slot` children
 *      are `position:absolute; inset:0` — five titles in ONE cell, separated by
 *      `transform`, which is what makes the sliding title travel exactly one
 *      content column. Absolute children contribute nothing to their parent's
 *      height, so a content-sized track measures zero. Putting the active slot
 *      back in flow would re-lay the title-track idiom, and R5 has already
 *      ruled that the plate's stacking is re-thought WITH the sequencer.
 *
 * So exactly one number crosses from the band to the rest of the geometry, and
 * `tokens.css` turns it into the single name (`--sh-band-h`) that the plate,
 * the track, the notch polygon and `.content`'s padding all read. The
 * arithmetic — the 72px floor, the band's bottom padding — stays in CSS in
 * tokens, so this file knows nothing about the kit or the rung.
 *
 * ===========================================================================
 * NO FEEDBACK LOOP, AND IT IS PROVABLE RATHER THAN HOPED
 * ===========================================================================
 * Same fear as `useMeasuredInfoHeight` in `Sidebar.tsx`, same answer. What is
 * measured is `.c-band-row`'s HEIGHT; what is written changes the BAND's,
 * the track's and the plate's height. The row's height is a function of its own
 * width and its content, and its width comes from the track's `left`/`right` —
 * neither of which reads this token. So writing it cannot change what was
 * measured. The write is additionally guarded on the value having changed, so
 * an observation that agrees with the last one touches no style at all.
 *
 * ===========================================================================
 * TWO OBSERVERS, NEITHER OF THEM A CLOCK
 * ===========================================================================
 * A `ResizeObserver` on the five rows catches everything that changes a row's
 * height: a viewport resize, a rung or kit change (the type re-bases), a leaf
 * publishing or withdrawing its controls, a filter expanding. A
 * `MutationObserver` on the track catches the two things a resize cannot see —
 * the `data-on` flip that makes a DIFFERENT slot the active one, and React
 * REPLACING a row node, which happens on every section/detail swap because
 * `PlateTitle` renders `DetailTitle` or `SectionTitle` and they are different
 * components.
 *
 * Neither carries a timer, a debounce or a poll. That distinction is the point:
 * the thing R2 refused and `Blob.tsx` was bitten by is a SECOND CLOCK — a
 * 200ms debounce disagreeing with a 650ms transition. An observer that fires on
 * the layout it observes and writes synchronously has no clock of its own to
 * disagree with.
 */

import { useLayoutEffect } from "react";

/** The track, and the rows inside it. One spelling, two readers below. */
const TRACK = "#top-plate > .c-track";
const ROW = ".c-band-row";
const ACTIVE_ROW = `.c-slot[data-on] ${ROW}`;

/**
 * Publish the ACTIVE band row's height as `--sh-band-row` on the document.
 *
 * `activeTopic` is a dependency only so the effect re-arms across a topic
 * change; the active row is always found by `[data-on]` rather than from this
 * value, because the attribute is what the stylesheet reads too.
 */
export function useBandHeight(activeTopic: string): void {
  useLayoutEffect(() => {
    const root = document.documentElement;
    const track = document.querySelector<HTMLElement>(TRACK);
    if (!track) return;

    let last = "";

    /* THE HEIGHTS COME OUT OF THE OBSERVER, NOT OUT OF A LAYOUT READ.
       =======================================================================
       A `ResizeObserver` entry already CARRIES the box it observed, measured by
       the engine as part of the frame it reports. Calling
       `getBoundingClientRect()` in the callback instead asks for a fresh
       synchronous layout — and this observer fires on nearly every frame of a
       topic switch, because `.c-track` transitions its `right` edge (the notch)
       so the row's WIDTH animates even when its height does not.

       MEASURED on `test_shell_title_track_live.py`, WebKitGTK, same machine:
       a rect read in the callback gave 18 frames for `work->system` against
       main's 41 and a slide that had not arrived (travel -604 against -746);
       reading the entry gives -746.0, main's number exactly. */
    const boxes = new Map<Element, { w: number; h: number }>();

    /* AND THE WRITE WAITS FOR THE MOTION TO STOP, WHICH IS THE OTHER HALF AND
       THE ONE THAT COST THE FRAMES.
       =======================================================================
       `--sh-band-row` is read by `--sh-band-h`, so a write resizes `.c-track`
       AND `.top-plate` — and the plate is the blurred surface, which on
       WebKitGTK is a cloned mirror (`lib/backdrop-glass.ts`). Writing it
       several times during a switch resizes that mirror several times.

       PROVEN BY SUPPRESSING ONLY THE WRITE, observers left running:
         write on every observation   WebKitGTK 33/40 frames, 1-3 of 14 red
         no write at all              WebKitGTK 37/53 frames, 14 of 14 green
         main                         WebKitGTK 41/52 frames, 14 of 14 green
       The reds were never the same three twice — `one_visible_title_at_rest`,
       `slot_lands_on_the_content_column`, `never_stacks_two_titles` — because
       all of them read a state the shell reaches slightly later. A counter-test
       ruled out the obvious suspect: freezing `.content`'s padding-top, so the
       LEAF cannot relayout, changed nothing (21/45). It is the plate, not the
       page.

       AND MORE THAN ONE WRITE PER SWITCH IS NOISE ANYWAY. The ruling is that
       the band's height varies per LEAF, and the leaf changes once, at the
       commit. The extra writes came from the row re-wrapping at the
       intermediate widths the easing notch hands it — widths no leaf ever rests
       at. So waiting is not a compromise, it is the correct answer arrived at
       from the other side.

       `requestAnimationFrame` IS THE PAINT CLOCK, NOT A SECOND ONE. The write
       lands one frame after the last observation, on the same clock the
       transition runs on, and it converges because observations stop when the
       motion does. A timer or a debounce in milliseconds WOULD be a second
       clock — that is the distinction R2 turns on, and the reason
       `Blob.tsx`'s 200ms is a defect while `backdrop-glass.ts`'s `schedule()`
       is not. */
    let seen = 0;
    let frame = 0;
    let armedAt = -1;

    const publish = () => {
      const row = track.querySelector<HTMLElement>(ACTIVE_ROW);
      const box = row ? boxes.get(row) : undefined;
      // No entry yet is a row the observer has not reported, and a zero is a
      // row that has not been laid out. Both leave the token at its floor.
      if (!box || box.h <= 0) return;
      // ROUNDED UP, not to the nearest: the band has to be at least as tall as
      // the row it contains, and `.c-band` clips. Two decimals keeps the
      // sub-pixel honesty the fit gate asserts against.
      const next = `${Math.ceil(box.h * 100) / 100}px`;
      if (next === last) return;
      last = next;
      root.style.setProperty("--sh-band-row", next);
    };

    const settled = () => {
      frame = 0;
      // An observation arrived after this frame was armed, so the layout is
      // still moving. Arm again rather than publishing a width nothing rests at.
      if (seen !== armedAt) {
        arm();
        return;
      }
      publish();
    };

    const arm = () => {
      if (frame) cancelAnimationFrame(frame);
      armedAt = seen;
      frame = requestAnimationFrame(settled);
    };

    if (typeof ResizeObserver === "undefined") {
      // Nothing to observe with: the stylesheet's own `max()` holds the floor,
      // which is exactly today's fixed band. Honest, and never wrong-by-a-frame.
      return () => root.style.removeProperty("--sh-band-row");
    }

    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        // `borderBoxSize` is the box `.c-band` has to hold; `contentRect` is the
        // fallback and equals it here, because `.c-band-row` carries no padding
        // and no border. Both are the engine's own numbers for this frame.
        const box = entry.borderBoxSize?.[0];
        boxes.set(entry.target, {
          w: box ? box.inlineSize : entry.contentRect.width,
          h: box ? box.blockSize : entry.contentRect.height,
        });
      }
      seen += 1;
      arm();
    });

    /* RE-TARGET ONLY WHEN THE ROW SET ACTUALLY CHANGED. `childList: true,
       subtree: true` fires on any node a leaf's header adds or removes — a
       count that ticks, a spinner that mounts. Comparing the nodes is one
       `querySelectorAll` and a loop, and it touches no geometry. */
    let watched: HTMLElement[] = [];
    const same = (next: HTMLElement[]) =>
      next.length === watched.length && next.every((el, i) => el === watched[i]);
    const watch = () => {
      const rows = [...track.querySelectorAll<HTMLElement>(ROW)];
      if (!same(rows)) {
        ro.disconnect();
        boxes.clear();
        for (const row of rows) ro.observe(row);
        watched = rows;
      }
      // The `data-on` flip changes WHICH row is the active one without changing
      // any row's size, so the publish has to be armed on a mutation too.
      arm();
    };
    watch();

    const mo = new MutationObserver(watch);
    mo.observe(track, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["data-on"],
    });

    return () => {
      if (frame) cancelAnimationFrame(frame);
      mo.disconnect();
      ro.disconnect();
      // Hand the token back to the stylesheet rather than freezing the last
      // measured value on the document — the `max()` in `tokens.css` is the
      // designed default and must be what an unmounted shell resolves to.
      root.style.removeProperty("--sh-band-row");
    };
  }, [activeTopic]);
}
