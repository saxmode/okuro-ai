// SPDX-License-Identifier: Apache-2.0
/**
 * THE SIDE-BY-SIDE PANE SWITCH — one mechanism, every consumer.
 *
 * The sub-item panes inside a section and the section-plane tabs are the same
 * movement under Law 3, so they are the same code. Written once here, so the
 * direction can only be inverted in one place.
 *
 * IT TAKES A PAIR, NOT AN ARRAY. It used to receive every pane and two indices,
 * which only worked because every pane was mounted. Under the windowing ruling
 * — *"just the bars!"* — at most two panes exist at a time, so the caller hands
 * over exactly the two elements that are moving. Direction still comes from the
 * ORIGINAL indices, never from their position in the DOM, or Law 3 would start
 * depending on what happens to be mounted.
 *
 * ===========================================================================
 * IT IS THE SUB-ITEM AXIS ALONE AS OF 2026-09-18
 * ===========================================================================
 * `accentMs`, `switchWindowMs` and `PHASE_UNITS` lived here and mirrored the
 * stylesheet's window arithmetic for the section-change driver's one timer.
 * There is no driver and no timer: the section change is nine resting CSS
 * rules on one duration and one curve. What is left is the leaf-to-leaf pane
 * switch inside a single bar, which was never a section change.
 *
 * `parkAt` and `releaseTo` are the whole mechanism and `slidePair` is one call
 * of each. Neither has an opinion about opacity — `slidePair` states its own
 * cross-fade, which is why the primitives stay reusable.
 */

import type { Direction } from "./direction";
import { incomingOffset, outgoingOffset } from "./direction";

/**
 * The motion duration, READ from the token rather than repeated as a literal.
 *
 * A hardcoded `700` was how the mockup cleaned up after itself, and it silently
 * disagrees with the token the moment slow-mo is on or a user has asked for
 * reduced motion. Reading it keeps the vocabulary at three values.
 */
export function motionDurationMs(el: Element): number {
  const raw = getComputedStyle(el).getPropertyValue("--sh-speed-medium").trim();
  const ms = raw.endsWith("ms")
    ? parseFloat(raw)
    : raw.endsWith("s")
      ? parseFloat(raw) * 1000
      : NaN;
  return Number.isFinite(ms) ? ms : 650;
}

/**
 * Reset a pane to its resting, untransformed state.
 *
 * IT STILL CLEARS `opacity`, AND THAT IS NOT A LEFTOVER. Nothing writes an
 * inline opacity any more, but an INTERRUPTED sequence from before this file
 * changed — a page reloaded onto a shell that had one in flight — has no other
 * way back, and clearing a property that is already empty costs nothing. What
 * it must never do is SET one; that is the phase map's job.
 */
export function park(pane: HTMLElement): void {
  pane.classList.remove("anim");
  pane.style.transform = "";
  pane.style.opacity = "";
  pane.style.removeProperty("--sh-grow");
}

/**
 * Write a start value the engine will interpolate FROM, and nothing else.
 *
 * The reflow read between the two writes is what makes the parked value a real
 * start value instead of being coalesced away with whatever is written next —
 * and under React this must run in a LAYOUT effect, after commit, or the reflow
 * reads a stale box and the transition is skipped entirely.
 *
 * `write` is a callback rather than a transform string because the sequencer
 * parks three different kinds of value: a `transform` on the two travelling
 * surfaces, and `--sh-grow` on the two bars whose width is about to change.
 * One mechanism, three consumers, no third argument describing which.
 */
export function parkAt(el: HTMLElement, write: (style: CSSStyleDeclaration) => void): void {
  const previous = el.style.transition;
  el.style.transition = "none";
  write(el.style);
  void el.offsetWidth;
  el.style.transition = previous;
}

/**
 * Release a parked element toward its target, on whatever transition the
 * stylesheet has selected for the phase that is running.
 *
 * It deliberately does NOT read or set a duration. The section change selects
 * two beats through `[data-phase="move"]`; a sub-item slide selects a full
 * medium through `.pane.anim`. Which clock applies is a cascade decision, and a
 * JS animation driving a value CSS already drives is Law 2.
 */
export function releaseTo(el: HTMLElement, write: (style: CSSStyleDeclaration) => void): void {
  write(el.style);
}

/**
 * Slide `incoming` in and `outgoing` out, both travelling the way their physical
 * positions dictate.
 *
 * THE SUB-ITEM AXIS, AND ONLY THAT. A leaf-to-leaf switch inside one bar is not
 * a section change: it has no bars to grow, no label to turn and no plate to
 * move, so it has no phases and keeps its full medium through `.pane.anim`.
 * D1 merged the two idioms, not the two clocks.
 *
 * ITS CROSS-FADE STAYS, AND IT IS STATED HERE RATHER THAN BUILT INTO THE
 * PRIMITIVE. That is the whole of what D1 changes about this function. Every
 * mounted pane carries `data-on` (`TopicBar.tsx`), so `.pane[data-on]{opacity:1}`
 * cannot fade the one that is leaving and an inline value is the only way to say
 * it. Deleting these two writes would silently end the sub-item cross-fade,
 * which nobody ruled and which is not the section change's business — so the
 * caller keeps the opinion and `parkAt`/`releaseTo` keep none.
 */
export function slidePair(
  outgoing: HTMLElement,
  incoming: HTMLElement,
  dir: Direction,
): void {
  incoming.classList.remove("anim");
  parkAt(incoming, (s) => {
    s.transform = incomingOffset(dir);
    s.opacity = "0";
  });

  outgoing.classList.add("anim");
  incoming.classList.add("anim");
  releaseTo(outgoing, (s) => {
    s.transform = outgoingOffset(dir);
    s.opacity = "0";
  });
  releaseTo(incoming, (s) => {
    s.transform = "translateX(0)";
    s.opacity = "1";
  });
}
