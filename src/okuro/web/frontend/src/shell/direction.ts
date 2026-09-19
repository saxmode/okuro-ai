// SPDX-License-Identifier: Apache-2.0
/**
 * LAW 3 — DIRECTION FOLLOWS PHYSICAL POSITION, NOT HISTORY.
 *
 * The owner, 2026-09-13: *"Tab means physical position."* It holds at all three
 * levels — topic bars, sub-item panes, section tabs — for one reason, which is
 * Law 4: everything is side by side, so the direction of every animation is
 * simply the direction the layout is actually moving.
 *
 * Click a topic to the RIGHT and the outgoing bar shrinks, so the whole rail
 * shifts LEFT: the outgoing content exits LEFT while the incoming arrives from
 * the RIGHT. Clicking moves the viewport TOWARD the thing rather than shoving
 * the thing at you.
 *
 * The first build had both inverted (always in-from-left, out-to-right). This
 * module exists so there is exactly one place that can be inverted again.
 *
 * IT IS THE SUB-ITEM AXIS ALONE AS OF 2026-09-18. The SECTION change used to
 * call in here too, through a driver that computed a sign per switch and parked
 * an inline transform from it. The law is unchanged and it is still stated in
 * exactly one place — that place is now a CSS expression rather than a function:
 * `clamp(-100%, (--sh-i - --sh-active-i) * 100%, 100%)` on `.content` and
 * `.c-slot`, which is the same sentence with the sign implicit in the
 * subtraction. `directionAttr` left with the driver that published it.
 */

/** +1 = the target lies to the right of the current item; -1 = to the left. */
export type Direction = 1 | -1;

export function physicalDirection(fromIndex: number, toIndex: number): Direction {
  return toIndex > fromIndex ? 1 : -1;
}

/** Where the INCOMING pane is parked before it is released. */
export function incomingOffset(dir: Direction): string {
  return `translateX(${100 * dir}%)`;
}

/** Where the OUTGOING pane travels: the direction its own bar is moving. */
export function outgoingOffset(dir: Direction): string {
  return `translateX(${-100 * dir}%)`;
}
