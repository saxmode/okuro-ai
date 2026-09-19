// SPDX-License-Identifier: Apache-2.0
/**
 * THE REFLOW HAS TO HAPPEN AFTER COMMIT, WHICH IS WHY THESE ARE LAYOUT EFFECTS.
 *
 * The park-and-release step sets a start value, forces a reflow so the browser
 * treats it as a real start value, and only then releases the element toward its
 * target. If that runs during render the DOM is not committed yet, the reflow
 * reads a stale box, and the browser coalesces both writes into one — the
 * transition is skipped and the pane snaps. `useLayoutEffect` runs after commit
 * and before paint, which is exactly the window this needs.
 *
 * IT IS THE SUB-ITEM AXIS ONLY, AS OF 2026-09-18. The SECTION change used to be
 * driven from here too — a phase machine, then a one-window driver with two
 * accents, speculative pointerdown starts and a withdrawal path. The owner ruled
 * all of it out: *"it's moving containers over a curve and animate attributes.
 * not rocket science."* It is now nine resting CSS rules on one duration and one
 * curve, with no driver at all — see the note where `[data-phase]` used to be in
 * `shell.css`. What is left in this file is the leaf-to-leaf pane switch inside
 * one bar, which was never a section change, and two measured lengths.
 *
 * REACT OWNS STATE, CSS OWNS MOTION. Nothing here animates anything: it sets two
 * values and lets the stylesheet's transition carry them. No rAF loop, no
 * interpolation, and `motion` is not a dependency — a JS animation driving a
 * value CSS already drives is Law 2.
 */

import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { physicalDirection } from "../direction";
import { SHEET_CHANGED_EVENT } from "@/lib/theme";
import { motionDurationMs, park, slidePair } from "../slider";

/**
 * THE PANE WINDOW — at most two mounted, ever.
 *
 * The owner ruled Law 4 as *"just the bars!"*: collapsed BARS go to zero width and
 * are never unmounted, because the slide needs them present. Panes are not
 * covered. So this mounts the active pane, plus — only while it is travelling —
 * the one being animated away. Everything else is built on arrival.
 *
 * That took the shell from 108 mounted panes to two, and it is what makes
 * `React.lazy` per leaf view possible: a lazily-imported component that never
 * renders has nothing for the slide to move, so windowing had to come first.
 *
 * THE WINDOW MUST BE CORRECT AT THE COMMIT WHERE THE INDEX CHANGES, because the
 * outgoing pane has to exist in order to leave. That is why the adjustment
 * happens during render (React's documented "adjusting state when props change"
 * pattern) rather than in an effect — an effect runs after the commit, by which
 * point the outgoing pane would already be gone and there would be nothing to
 * animate out.
 *
 * Direction comes from the ORIGINAL indices, never from position in `mounted`.
 * Law 3 must not start depending on what happens to be rendered.
 */
export interface PaneWindow {
  /** the indices to render — one at rest, two mid-transition */
  mounted: number[];
  /** ref callback so the hook can reach the elements it is moving */
  register: (index: number, el: HTMLElement | null) => void;
}

export function useWindowedPanes(index: number, animate: boolean): PaneWindow {
  const [shown, setShown] = useState(index);
  const [outgoing, setOutgoing] = useState<number | null>(null);
  const els = useRef(new Map<number, HTMLElement>());

  if (index !== shown) {
    setOutgoing(animate ? shown : null);
    setShown(index);
  }

  const travelling = outgoing !== null && outgoing !== index;
  const mounted = travelling ? [outgoing, index].sort((a, b) => a - b) : [index];

  useLayoutEffect(() => {
    if (!travelling) {
      // Resting, or a jump with no animation: make sure nothing carries a
      // leftover transform from an interrupted slide.
      const here = els.current.get(index);
      if (here) park(here);
      return;
    }
    const a = els.current.get(outgoing);
    const b = els.current.get(index);
    if (!a || !b) return;

    slidePair(a, b, physicalDirection(outgoing, index));

    // Retire the outgoing pane once it has finished travelling — this is the
    // unmount, and it is what keeps the window at two.
    const t = window.setTimeout(() => setOutgoing(null), motionDurationMs(b) + 60);
    return () => window.clearTimeout(t);
  }, [travelling, outgoing, index]);

  const register = useCallback((i: number, el: HTMLElement | null) => {
    if (el) els.current.set(i, el);
    else els.current.delete(i);
  }, []);

  return { mounted, register };
}

/**
 * `--sh-divider-h`, the one length CSS cannot compute for itself.
 *
 * The swing hugs its content, so its `offsetWidth` IS the content length along
 * the bar; 64 is the lead before it. `k-icons` and `k-text` share one grid cell
 * and the cell is as wide as the LONGER of the two — the text — so the text is
 * hidden for the read or every divider inherits the ACTIVE length.
 *
 * IT MEASURES RENDERED TEXT, SO IT DEPENDS ON THE TYPEFACE — and as of
 * 2026-09-14 the typeface is the KIT's (D1). That makes this hook the one place
 * in the shell that has to be told about a design-system change, because it
 * reads a value from the layout instead of from the cascade.
 *
 * THREE TRIGGERS, AND EACH ONE FIXES A MEASURED FAILURE:
 *
 * 1. `document.fonts.ready` — the original. Covers a webfont arriving after
 *    first layout. It resolves ONCE per document, which is why it is not enough
 *    on its own.
 *
 * 2. `okuro:engine-sheet-changed` — NEW. `lib/theme.ts::reloadEngineSheet`
 *    swaps the kit at runtime with no reload, and `fonts.ready` has long since
 *    resolved by then. Measured before this listener existed: after a runtime
 *    face change the five inline values stayed at 176/305/277/365/354 while the
 *    recomputed lengths were 176/318/289/363/358 — four dividers wrong by up to
 *    13px, in both directions, with nothing in the console. The event fires on
 *    the new sheet's `load`, not on the href mutation, for the reason recorded
 *    in theme.ts.
 *
 * 3. THE PLANE APPEARING — NEW, AND IT IS A LIVE BUG THIS REPLACES. The effect
 *    was keyed `[rootRef]`, and a ref object is stable, so it ran exactly once.
 *    On any entry that redirects — `/` is one, and it is the default — the first
 *    commit renders `<Navigate>` with no plane, `rootRef.current` is null, the
 *    effect returns early, and it NEVER RUNS AGAIN. Measured on the shell at
 *    12243862a: loading `/` left every bar with no `--sh-divider-h` at all and
 *    `::before` height 0px on all four inactive bars, while loading
 *    `/start/now` directly gave the full 176/305/277/365/354. The dividers were
 *    simply absent on the normal way in. A MutationObserver on the plane's
 *    parent would also work; watching the ref through an interval would not,
 *    and neither would adding the plane to a dep array that has no plane in it.
 *    So the hook now RETRIES until the plane exists, on the animation frame.
 */
export function useDividerHeights(rootRef: React.RefObject<HTMLElement | null>): void {
  useLayoutEffect(() => {
    let live = true;
    let frame = 0;

    const measureInto = (root: HTMLElement) => {
      for (const bar of root.querySelectorAll<HTMLElement>(".bar")) {
        const swing = bar.querySelector<HTMLElement>(".swing");
        const text = bar.querySelector<HTMLElement>(".k-text");
        if (!swing || !text) continue;
        text.style.display = "none";
        const len = swing.offsetWidth;
        text.style.display = "";
        bar.style.setProperty("--sh-divider-h", `${64 + len}px`);
      }
    };

    /** Measure if the plane is mounted; otherwise come back next frame. */
    const measure = () => {
      if (!live) return;
      const root = rootRef.current;
      if (!root || root.querySelector(".bar") === null) {
        frame = requestAnimationFrame(measure);
        return;
      }
      measureInto(root);
    };

    measure();
    void document.fonts?.ready.then(() => measure());
    window.addEventListener(SHEET_CHANGED_EVENT, measure);

    return () => {
      live = false;
      cancelAnimationFrame(frame);
      window.removeEventListener(SHEET_CHANGED_EVENT, measure);
    };
  }, [rootRef]);
}

/**
 * `--sh-v-state-w`, the state column's width, measured instead of pinned.
 *
 * WHY THIS HOOK EXISTS. The column used to be a `136px` literal, measured once
 * on UPPERCASE labels at 10px. Q6 ruled that letter case is the design
 * system's to steer, and the type scale already comes from the kit, so a width
 * measured on one case and one size describes neither kit. Measured:
 *
 *                    transform   size    widest label   against 136px
 *   okuro-ds         none        10px    133.00px       fits, 3px spare
 *   standard         uppercase   12px    155.38px       TRUNCATED by 19.4
 *
 * SAME SHAPE AS `useDividerHeights`, deliberately: a length that depends on
 * rendered text is measured from rendered text, and re-measured whenever the
 * thing it depends on can change. Three triggers, for the same three reasons.
 *
 * THE PROBE WEARS `.v-state` SO IT INHERITS THE REAL DECISION. Font family,
 * size, weight, tracking and `text-transform` all come from that class, which
 * is what makes the measurement case-aware without this file knowing anything
 * about case. The box properties are overridden inline because `.v-state` pins
 * its own width from the token this hook is about to write — measuring it
 * without `flex:none; width:auto` would measure last frame's answer.
 *
 * IT MEASURES ONE STRING, AND THAT IS ON PURPOSE. `LONGEST_STATE` is the
 * longest label okuro's lifecycle emits; the comment in views.css records why
 * the column is sized on the lifecycle set rather than on whatever a given
 * pane happens to render. Several views put arbitrary backend text in this
 * column, and the `text-overflow: ellipsis` there is the net for those. Sizing
 * to the rendered content instead would make the column jump on every filter
 * change, which is the rhythm problem `--sh-v-row-h` was fixed to avoid.
 */
const LONGEST_STATE = "Waiting on decision";

/** The ellipsis margin the sign-off used: 136 for a 133px label. */
const STATE_MARGIN_PX = 3;

export function useStateColumnWidth(): void {
  useLayoutEffect(() => {
    let live = true;

    const probe = document.createElement("span");
    probe.className = "v-state";
    probe.setAttribute("aria-hidden", "true");
    probe.textContent = LONGEST_STATE;
    probe.style.cssText =
      "position:absolute;left:-9999px;top:0;visibility:hidden;pointer-events:none;" +
      "flex:none;width:auto;max-width:none;overflow:visible;white-space:nowrap";
    document.body.appendChild(probe);

    const measure = () => {
      if (!live) return;
      const w = probe.getBoundingClientRect().width;
      if (w <= 0) return;
      document.documentElement.style.setProperty(
        "--sh-v-state-w",
        `${Math.ceil(w) + STATE_MARGIN_PX}px`,
      );
    };

    measure();
    void document.fonts?.ready.then(() => measure());
    window.addEventListener(SHEET_CHANGED_EVENT, measure);

    return () => {
      live = false;
      window.removeEventListener(SHEET_CHANGED_EVENT, measure);
      probe.remove();
      document.documentElement.style.removeProperty("--sh-v-state-w");
    };
  }, []);
}
