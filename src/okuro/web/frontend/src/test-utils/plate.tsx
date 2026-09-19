// SPDX-License-Identifier: Apache-2.0
/**
 * THE PLATE, FOR A TEST THAT RENDERS ONE LEAF ON ITS OWN.
 *
 * ===========================================================================
 * WHY A PAGE TEST SUDDENLY NEEDS ONE
 * ===========================================================================
 * A-1 step 3b moved every leaf's search / sort / filter / add row out of
 * `.c-panes` and onto the plate, through `useSectionTitle`. A page rendered
 * bare has no `PageTitleProvider` above it, so it finds the `NOOP` context and
 * publishes into nothing — its controls are then not misplaced, they are
 * ABSENT. Every test that clicked one would fail, and deleting those tests
 * would delete the only assertions that the controls work at all.
 *
 * So the harness supplies the missing half: one provider, one slot in the DOM,
 * one `PlateTitle`. The published node then renders and `screen.getByRole`
 * finds it exactly where it always did.
 *
 * ===========================================================================
 * THE CONSTRAINT THIS FILE EXISTS TO MAKE VISIBLE — READ IT BEFORE PUBLISHING
 * ===========================================================================
 * A published `actions` node is CREATED by the leaf and RENDERED by
 * `PlateTitle`, which lives in `TopicBar`'s React tree. `createPortal` moves a
 * DOM node, not a tree position, and React context flows through the TREE. So
 * **a control published through the channel cannot read any context the leaf
 * itself provides.** Radix compound components — `TabsList`, `TabsTrigger`,
 * `SelectItem`, a form's field context — break silently or throw when handed
 * over. `ui/segmented` takes plain props and is the sanctioned control for
 * exactly that reason (`pages/agents.tsx`, ruling D3).
 *
 * That is a fact about the channel, not about this harness, and it is the one
 * thing a leaf author has to know before writing `actions`.
 */

import type { ReactNode } from "react";
import { PageTitleProvider, PlateTitle, slotId } from "@/shell/components/PageTitle";

/** Any topic id; the harness renders exactly one slot and marks it active. */
export const PLATE_TOPIC = "test";

/**
 * Wrap a leaf so that what it publishes actually lands somewhere.
 *
 * The slot is a SIBLING rendered before `PlateTitle`, because `PlateTitle`
 * looks its slot up by id in a layout effect — React commits the whole tree
 * before any layout effect runs, so the element is certainly there by then.
 */
export function Plate({
  children,
  fallback = "Leaf",
  owner = 0,
}: {
  children: ReactNode;
  /** stands in for the shell's derived leaf title. */
  fallback?: ReactNode;
  /** stands in for the bar's current leaf index. A test that wants to prove a
      published row is DROPPED when the bar moves on re-renders with a
      different one. */
  owner?: string | number;
}) {
  return (
    <PageTitleProvider owner={owner}>
      <div id={slotId(PLATE_TOPIC)} data-on="" />
      <PlateTitle fallback={fallback} topic={PLATE_TOPIC} />
      {children}
    </PageTitleProvider>
  );
}

/** The plate's own row, for a test that wants to scope a query to it. */
export function plateBand(): HTMLElement | null {
  return document.querySelector(`#${slotId(PLATE_TOPIC)} .c-band`);
}

/**
 * Every `<h1>` the LEAF renders — that is, every one that is not the plate's.
 *
 * R1 says the leaf title belongs to the shell, and a test that asserted
 * `querySelectorAll("h1")` was empty could only do so while there was no plate
 * in the render at all. With the harness there IS one, so the honest form of
 * the same rule is "the page adds none to it", which this returns.
 */
export function pageHeadings(): HTMLElement[] {
  const plate = document.getElementById(slotId(PLATE_TOPIC));
  return [...document.querySelectorAll<HTMLElement>("h1")].filter(
    (h) => !plate || !plate.contains(h),
  );
}
