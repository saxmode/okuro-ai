// SPDX-License-Identifier: Apache-2.0
/**
 * THE COLLAPSED-PLATE DISCLOSURE — R9's mechanism, extracted so the two plates
 * SHARE it instead of each owning a copy.
 *
 * ===========================================================================
 * WHY A HOOK AND NOT A SECOND COPY OF THE SAME SIX LINES
 * ===========================================================================
 * R9 (`8546865f`) is literally *"window controls collapse into a single icon,
 * **like the bottom-right action container (.genact)**"* — the ruling names the
 * corner container as the pattern the window controls should copy. C2 then makes
 * `.genact` actually behave that way. Two plates, one behaviour: if it lives
 * twice, the next change to the focus handling fixes one plate and leaves the
 * other, and nothing errors.
 *
 * ===========================================================================
 * THE FOUR HANDLERS, AND WHY EACH ONE IS NOT OPTIONAL
 * ===========================================================================
 * Every one of these was paid for in the R9 pass and the comments are what stop
 * them being simplified back:
 *
 *   onPointerEnter   fires from a DESCENDANT, because React synthesises
 *                    enter/leave out of `pointerover`/`pointerout`, which
 *                    BUBBLE. That is what lets the closed trigger open a plate
 *                    whose body is `pointer-events:none` while closed.
 *   onPointerLeave   the plate takes `pointer-events:auto` while open, so the
 *                    pointer crossing the padding BETWEEN two glyphs stays
 *                    inside the plate and does not close it.
 *   onFocusCapture   focus and blur do not bubble, so capture is the only phase
 *                    that sees a glyph inside gaining focus.
 *   onBlurCapture    `relatedTarget` is what stops a Tab from glyph 2 to glyph 3
 *                    closing the plate: the blurring element is leaving, but
 *                    focus is still inside the plate.
 *
 * ===========================================================================
 * WHY THE STATE IS IN REACT AT ALL, given `:hover` + `:focus-within` would do it
 * ===========================================================================
 * `aria-expanded` needs something to read. In pure CSS a screen reader is told
 * about a button that opens nothing. So React owns ONE boolean and writes ONE
 * attribute (`data-open`); every width, opacity, margin and visibility change
 * stays a CSS transition (`cd04fca9` — React owns state, CSS owns motion).
 */

import { useCallback, useState, type FocusEvent } from "react";

export interface Disclosure {
  open: boolean;
  /**
   * Spread onto the PLATE element. Carries `data-open` and all four handlers,
   * so a plate cannot accidentally wire three of the four.
   */
  /** Flip the plate. The corner's arrow is the only caller. */
  toggle: () => void;
  plate: {
    onPointerEnter: () => void;
    onPointerLeave: () => void;
    onFocusCapture: () => void;
    onBlurCapture: (e: FocusEvent<HTMLElement>) => void;
    "data-open"?: "";
  };
  /**
   * The TRIGGER's click handler. It only ever OPENS — never toggles.
   *
   * The pointer path has already opened the plate by the time a click lands, so
   * a toggle would shut it under the cursor. This is the touch and
   * click-to-pin path, where there was no hover to open it first.
   */
  pin: () => void;
}

/* BOTH PLATES START OPEN — a 2026-09-19 ruling that REVERSES R9.
 *
 * R9 and the walkthrough of 2026-09-15 asked for the opposite: *"bottom-right
 * action container COLLAPSED by default, opens on hover like the window
 * controls"*. The reason it was asked for is recorded in `Corner.tsx` and was
 * real — a floating feedback button sat on top of the plate's third glyph on
 * every leaf, so the corner had to collapse before anything else about it could
 * be right. That collision is gone.
 *
 * The owner, 2026-09-19: *"the icons on both edges right (bottom and top) should
 * be open by default."* Figma's window frame agrees — `okuro-window-controls`
 * and `okuro-general-actions` are both drawn expanded.
 *
 * SO THE PLATES NO LONGER CLOSE THEMSELVES. The handlers stay wired because
 * `pin` and `aria-expanded` still need one boolean to read, and because a
 * future ruling that wants hover-collapse back should find the mechanism here
 * rather than have to rebuild it. They only ever open now. */
export function useDisclosure(defaultOpen = true): Disclosure {
  const [open, setOpen] = useState(defaultOpen);

  const onBlurCapture = useCallback((_e: FocusEvent<HTMLElement>) => {
    // Was: close when focus left the plate. The plate does not close now.
  }, []);

  return {
    open,
    plate: {
      ...(open ? { "data-open": "" as const } : {}),
      /* HOVER NO LONGER OPENS ANYTHING. The owner, 2026-09-19: "bottom icon
         stack opens on hover - should open on click." The plate is open by
         default and its arrow is a deliberate toggle, so a pointer crossing
         the corner on its way somewhere else must not reopen what was just
         collapsed. */
      onPointerEnter: useCallback(() => {}, []),
      // Hover no longer closes a plate. The bottom corner collapses on its
      // ARROW, which is a deliberate click — the owner, 2026-09-19 — and the top
      // cluster does not collapse at all.
      onPointerLeave: useCallback(() => {}, []),
      /* Focus still opens: a keyboard user tabbing into a collapsed plate has
         no other way to reach its glyphs, and that is not an accidental
         reopen the way a passing cursor is. */
      onFocusCapture: useCallback(() => setOpen(true), []),
      onBlurCapture,
    },
    pin: useCallback(() => setOpen(true), []),
    toggle: useCallback(() => setOpen((v) => !v), []),
  };
}
