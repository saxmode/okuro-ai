/**
 * WHAT COLOUR, GIVEN THIS DECK — the one place that answers it.
 *
 * R4 splits this leaf in two and the split is not the file boundary the p3 spec
 * drew. Deck CONTENT renders in the RECIPIENT's brand, and that is the product.
 * The editor's own marks — the selection outline, the resize handle, the text
 * caret, the alignment guide — are okuro CHROME, and they are drawn ON TOP of
 * that content. So they can be neither a brand colour nor a kit colour:
 *
 *   - a KIT colour can vanish, because the kit's accent resolves to `#9c9c9c`
 *     in dark and `#080808` in light (measured under `standard`), and a deck
 *     is free to be exactly that grey or that black;
 *   - a BRAND colour is the recipient's, and marking up their deck in their own
 *     accent is indistinguishable from content;
 *   - a FIXED literal is what the code had — okuro's retired green `#8ff0a4`,
 *     which is a third brand appearing in someone else's deliverable.
 *
 * Only the GROUND knows which case it is, which is the same conclusion the p4
 * WORK pass reached for graph node ink after shipping a 1.01:1 contrast. So the
 * mark asks the deck's own background, through `lib/color.ts::pickForeground`
 * — the APCA helper that already exists for exactly this question. Reused
 * rather than re-derived (DP10).
 *
 * THE FALLBACKS ARE THE OTHER HALF, and the p3 spec named only one of the four
 * places they live (`brandToTheme`). A deck that carries no background, or an
 * element that carries no colour, used to fall back to okuro's green-on-black:
 * `?? "#0b0f0c"`, `?? "#eafff0"`, `?? "#e8ffe8"`, `?? "#39463d"`. That is the
 * same defect as D2 with a different spelling, in five files. The honest
 * fallback is the ACTIVE KIT, because that is what the user is looking at and
 * what every other okuro surface resolves to.
 *
 * Concrete values, not `var()` references: `export.ts` emits a standalone
 * document for PDF / PPTX / PNG that cannot reach `/engine.css`, so a deck
 * colour has to be a real colour by the time it is written.
 */

import { pickForeground, withAlpha } from "@/lib/color";
import type { Deck, SlideElement } from "./scene";

/** Reached only with NO document at all — the export worker, a unit test.
 *  Deliberately achromatic so a missing stylesheet can never be mistaken for a
 *  brand decision. D1's documented-local case: the engine has no name to offer
 *  when its own sheet is absent. */
const NO_SHEET = {
  background: "#ffffff",
  ink: "#111111",
  accent: "#666666",
} as const;

function kitToken(name: string, lastResort: string): string {
  if (typeof document === "undefined") return lastResort;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || lastResort;
}

/** The deck's canvas ground: its own, else the active kit's. */
export function deckBackground(deck: Pick<Deck, "background">): string {
  return deck.background ?? kitToken("--color-background-base", NO_SHEET.background);
}

/** An element's ink: its own, else the active kit's primary foreground. */
export function elementInk(el: Pick<SlideElement, "color">): string {
  return el.color ?? kitToken("--color-foreground-primary", NO_SHEET.ink);
}

/** The deck's accent for CONTENT that asks for one (a KPI arrow, a rule, an
 *  `accent: true` element). The kit's, never okuro's retired green. */
export function deckAccent(): string {
  return kitToken("--color-accent", NO_SHEET.accent);
}

/**
 * THE EDITOR'S MARK COLOUR — guaranteed to be visible on THIS deck, whatever
 * brand it carries, because it is measured against the deck's own ground.
 *
 * `pickForeground` needs a 6-digit hex and a deck background may legitimately
 * be `rgb()`, a gradient, or absent. When it cannot be read, the kit's primary
 * foreground is the fallback — which is correct for the common case of a deck
 * that has not been branded yet, since then the canvas IS the kit's ground.
 */
export function editorMark(deck: Pick<Deck, "background">): string {
  const bg = deckBackground(deck);
  if (/^#[0-9a-fA-F]{6}$/.test(bg)) return pickForeground(bg);
  return kitToken("--color-foreground-primary", NO_SHEET.ink);
}

/** The same mark, dimmed — the hint outline every unselected element wears.
 *  One mechanism so the hint can never drift from the selection it previews. */
export function editorMarkHint(deck: Pick<Deck, "background">): string {
  const m = editorMark(deck);
  return /^#[0-9a-fA-F]{6}$/.test(m) ? withAlpha(m, 0.25) : m;
}

/**
 * The alignment-guide colour. A kit NAME, and deliberately NOT the mark: a
 * guide says "you are aligned with something else", which is a different
 * statement from "this is selected", so it must not share the selection's
 * colour. `--color-status-error` is a hue rather than a polarity — measured
 * `#f92f77` in BOTH appearances — so it stays visible on a light deck and a
 * dark one without asking the ground.
 */
export function editorGuide(): string {
  return kitToken("--color-status-error", "#f92f77");
}
