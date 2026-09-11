/**
 * ASK A COMPONENT WHAT IT IS, NOT WHAT IT IS MADE OF.
 *
 * WHY THIS EXISTS. Eight tests across three files went red on 2026-09-06 with
 * no defect anywhere in the product. Every one of them asserted a MECHANISM
 * that had legitimately changed underneath:
 *
 *   - `blocker-recommended` decided "is this the filled button" by looking for
 *     the Tailwind literals `border-input` and `bg-transparent`. The `outline`
 *     variant stopped emitting either when secondary buttons became border
 *     buttons (the owner, 2026-08-19). Result: every button read as primary, so
 *     "exactly one filled button" saw three — while the card was highlighting
 *     precisely the right one all along.
 *
 *   - `inbox` and `solve-button` looked their controls up as
 *     `getByRole("button")`. Those controls became `Segmented`, which renders
 *     `role="radiogroup"` with `role="radio"` children and `aria-checked` —
 *     strictly better semantics. The queries stopped matching.
 *
 * Its own comment said the intent out loud — "asserting on 'is this the
 * primary' rather than on a literal class keeps the test about hierarchy, not
 * about Tailwind" — and then asserted on two literal classes. That gap is what
 * this module closes: the components already PUBLISH the answers, so the tests
 * read the published answer instead of inferring it.
 *
 *   which action is primary   ->  `data-variant` on the Button
 *   which segment is chosen   ->  `aria-checked` on the radio
 *
 * Both are contracts a component states about itself, so a restyle cannot move
 * them and a genuine regression still turns these red.
 */

import { screen, within } from "@testing-library/react";

/**
 * The filled/primary button, by accessible name.
 *
 * `data-variant` is set by `components/ui/button.tsx` on every render, for
 * exactly this kind of question — a variant is a claim the button makes, and
 * the class list is one rendering of that claim.
 */
export function isPrimaryAction(name: RegExp | string): boolean {
  return screen.getByRole("button", { name }).dataset.variant === "default";
}

/** Every filled button currently on screen. The "exactly one" assertion. */
export function primaryActions(): HTMLElement[] {
  return screen
    .getAllByRole("button")
    .filter((b) => (b as HTMLElement).dataset.variant === "default");
}

/**
 * One option of a segmented control, by accessible name.
 *
 * NOT `getByRole("button")`: `Segmented` is a radiogroup, which is what a
 * "choose one of these" control is. Tests that reached for a button were
 * describing the old implementation.
 */
export function segment(name: RegExp | string): HTMLElement {
  return screen.getByRole("radio", { name });
}

/** Whether that option is the chosen one. */
export function segmentChecked(name: RegExp | string): boolean {
  return segment(name).getAttribute("aria-checked") === "true";
}

/** The chosen option inside one named group — for a screen with several. */
export function chosenIn(groupName: RegExp | string): HTMLElement | undefined {
  const group = screen.getByRole("radiogroup", { name: groupName });
  return within(group)
    .getAllByRole("radio")
    .find((r) => r.getAttribute("aria-checked") === "true");
}
