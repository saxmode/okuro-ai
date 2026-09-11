/**
 * The one place this page does size arithmetic — mirroring `design_engine/scale.py`.
 *
 * WHY A MIRROR EXISTS AT ALL, when the discipline everywhere else on this page
 * is that the engine decides and the page displays. A builder types a PIXEL,
 * and what a brand must SAVE is the factor:
 *
 *     "The growth UI shows sizes in px because that is what a designer reads,
 *      but what a brand SAVES has to be the factor -- otherwise changing the
 *      base later leaves the saved numbers behind, which is the exact failure
 *      the frozen grid had."
 *
 * Converting on the server would mean a round trip per keystroke, so the
 * conversion happens here. It is two multiplications' worth of arithmetic and
 * it cannot silently diverge — but it CAN diverge if the engine ever makes
 * `factor_of` do more than divide. `tests/design_engine/test_api.py::
 * test_the_scale_definition_the_frontend_mirrors_is_still_a_plain_ratio` pins
 * that, and fails the moment this file needs to change.
 *
 * Nothing else on this page computes a size. The tables, the emitted sheet and
 * every value in the canvas come from the engine.
 */

/**
 * THE SYSTEM BASE — mirroring `scale.BASE`. A constant, never a brand slot:
 *
 *     "Brands do not change their base. Base is always the same. Brands change
 *      their colors and their other settings." (the owner, 2026-08-17)
 *
 * The rail may READ it to show a px beside a factor. It may never offer it as
 * a field — a control here for something the system fixes is the rail lying
 * about what a brand owns. The engine carries the same number in
 * `sizes.base`, and the page suite pins the two together.
 */
export const BASE = 8;

/**
 * THE ROOT — `html { font-size: 50% }`, mirroring `scale.ROOT_PERCENT`.
 *
 * rendered px = user-font-size × 50% × factor. At the 16px default the root is
 * 8px, which is BASE, which is why an emitted rem IS the factor.
 */
export const ROOT_PERCENT = 50;

/** `size(base, factor)` — the whole sizing engine. "base * factor? 2 * 8 = 16?" */
export function size(base: number, factor: number): number {
  return base * factor;
}

/** `factor_of(base, px)` — the inverse, so a UI can show px and store intent. */
export function factorOf(base: number, px: number): number {
  if (base === 0) throw new Error("a brand's base cannot be zero");
  return px / base;
}

/**
 * Round a px readout for display without ever rounding what is STORED.
 *
 * A factor of 0.35 on base 6 is 2.1 px and that is simply the answer — the
 * engine does not snap it and neither does this. Rounding here is a label,
 * applied on the way out and never on the way in.
 */
export function px(value: number): string {
  const rounded = Math.round(value * 100) / 100;
  return `${rounded}`;
}
