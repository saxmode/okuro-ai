/**
 * ONE DECISION, ONE WRITE — putting a face on a type band.
 *
 * The owner, 2026-09-06: *"for one font it will be ExtraBlack for Title and for
 * the other Font it will be Heavy for the Title."* That is ONE decision. The
 * model needs TWO writes for it — `font.slot_N` to say which face a slot holds,
 * then `weights.titles` to point the band at that slot — and a builder asked to
 * make both is being asked to know the indirection exists.
 *
 * SO THE INDIRECTION STAYS AND STOPS BEING THE BUILDER'S PROBLEM. It is not an
 * accident to be refactored away: the slot ORDINAL is the Figma contract
 * (`1_0 brand-set/weight_1..4`), and Figma cannot express a numeric font
 * weight. Collapsing the model to "a face name per band" would read cleaner and
 * break the round-trip. This module does the bookkeeping instead.
 *
 * THE INVARIANT THAT MAKES IT NON-TRIVIAL. `schema.lint` raises
 * `font.weights-not-monotone` unless slots 1..4 weigh descending — "slot 1 is
 * meant to be the heaviest fatness range the family offers and slot 4 the
 * lightest". So a naive "write the face into whichever slot is free" flags the
 * brand. Every assignment therefore RE-SORTS: the bands' distinct faces are
 * laid into slots heaviest-first, the leftover slots are filled with lighter
 * faces the family really ships, and every band is re-pointed at wherever its
 * face landed.
 *
 * WHICH MEANS SLOT NUMBERS MOVE, and that is correct rather than surprising: a
 * slot is a fatness RANGE, not an identity. What a builder chose — this band
 * wears this face — is preserved exactly; only the ordinal under it is
 * renumbered, the same way `setFamily` already rewrites all four when the
 * typeface changes.
 *
 * IT CANNOT RUN OUT OF SLOTS. Four bands over four slots: at most four distinct
 * faces can be asked for, so the sort always fits.
 *
 * NOTHING HERE READS A WEIGHT IT WAS NOT GIVEN. `faces` is the family's own
 * measured descriptor -> usWeightClass table, from `installed_families()`. A
 * descriptor the table does not spell weighs nothing this module can order by,
 * so it is placed after everything it can order — never guessed at from its
 * name, because "Black" is 900 in one family and 950 in another.
 */

import type { BrandJson } from "./types";

/** The four bands a brand authors against. Mirrors `schema.TYPE_BANDS`. */
export const TYPE_BANDS = [
  "titles",
  "headings",
  "leads",
  "paragraphs",
] as const;

export type TypeBand = (typeof TYPE_BANDS)[number];

/** A family's measured faces: descriptor -> OS/2 usWeightClass. */
export type Faces = Record<string, number>;

type Slots = [string, string, string, string];
type Assignment = Record<TypeBand, number>;

/**
 * Order descriptors heaviest first.
 *
 * AN UNKNOWN DESCRIPTOR SORTS LAST rather than being dropped or guessed. A kit
 * can name a face this machine cannot measure — the scanner and a hand-authored
 * kit both allow it — and losing it on the next edit would silently rewrite
 * somebody's brand.
 */
function byWeightDesc(faces: Faces): (a: string, b: string) => number {
  return (a, b) => {
    const wa = faces[a];
    const wb = faces[b];
    if (wa === undefined && wb === undefined) return a.localeCompare(b);
    if (wa === undefined) return 1;
    if (wb === undefined) return -1;
    if (wb !== wa) return wb - wa;
    return a.localeCompare(b);
  };
}

function slotsOf(font: BrandJson["font"]): Slots {
  return [font.slot_1, font.slot_2, font.slot_3, font.slot_4];
}

/** Which face each band currently wears. */
export function facesOfBands(
  font: BrandJson["font"],
  weights: Assignment,
): Record<TypeBand, string> {
  const slots = slotsOf(font);
  const out = {} as Record<TypeBand, string>;
  for (const band of TYPE_BANDS) {
    // A stored ordinal is 1..4 by schema, but a hand-edited file is not a
    // promise — clamp rather than index off the end and render `undefined`.
    const ordinal = Math.min(4, Math.max(1, weights[band] ?? 1));
    out[band] = slots[ordinal - 1]!;
  }
  return out;
}

/**
 * Lay four slots out from the faces the bands want, heaviest first.
 *
 * The leftover slots take the heaviest UNUSED faces that are no heavier than
 * the last placed one, so the ladder stays descending. When the family has
 * nothing lighter left, the last face repeats — a duplicate is one fatness
 * range spelled twice and lint accepts it, where an ascending step would flag.
 */
function layOut(wanted: string[], faces: Faces): Slots {
  const order = byWeightDesc(faces);
  const placed = Array.from(new Set(wanted)).sort(order);

  const floor = placed.length
    ? faces[placed[placed.length - 1]!]
    : undefined;
  const spare = Object.keys(faces)
    .filter((d) => !placed.includes(d))
    .filter((d) => floor === undefined || faces[d]! <= floor)
    .sort(order);

  const out = [...placed];
  while (out.length < 4) {
    out.push(spare.shift() ?? out[out.length - 1] ?? "Regular");
  }
  return out.slice(0, 4) as Slots;
}

export interface BandAssignment {
  font: BrandJson["font"];
  weights: Assignment;
}

/**
 * Put `descriptor` on `band`, and keep every other band on the face it already
 * wears. Returns the whole `{font, weights}` pair — the caller writes both.
 *
 * Idempotent: assigning the face a band already wears returns an equivalent
 * pair, so a re-render or a double click cannot walk the slots.
 */
export function assignFaceToBand(
  font: BrandJson["font"],
  weights: Assignment,
  band: TypeBand,
  descriptor: string,
  faces: Faces | null,
): BandAssignment {
  const table = faces ?? {};
  const current = facesOfBands(font, weights);
  const wanted: Record<TypeBand, string> = { ...current, [band]: descriptor };

  const slots = layOut(TYPE_BANDS.map((b) => wanted[b]), table);

  const nextWeights = {} as Assignment;
  for (const b of TYPE_BANDS) {
    const at = slots.indexOf(wanted[b]);
    // `indexOf` cannot miss: every wanted face is in `placed`, which opens the
    // layout. The fallback exists so a future change to layOut degrades to the
    // band's old slot rather than to slot 0, which is not an ordinal.
    nextWeights[b] = at >= 0 ? at + 1 : weights[b];
  }

  return {
    font: {
      ...font,
      slot_1: slots[0],
      slot_2: slots[1],
      slot_3: slots[2],
      slot_4: slots[3],
    },
    weights: nextWeights,
  };
}
