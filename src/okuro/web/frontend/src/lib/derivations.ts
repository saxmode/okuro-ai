/**
 * READING THE RESOLVED MODEL — two questions, answered once, for any page.
 *
 * Both were inline in `/design-engine`'s own scene files, which meant the only
 * surface that could open an inspector was that page. Neither computes anything:
 * one is a lookup, the other walks the graph the ENGINE published. That is the
 * charter's rule about a projection that computes being a second authority,
 * applied to the two places a page would most easily have re-derived provenance.
 *
 * PROVENANCE IS READ, NEVER RECOMPUTED. A `Derivation` already carries its
 * value, its `because`, its rule id and the inputs that rule read; the page's
 * job is to find the right one and render it.
 */

import type { Derivation, ResolvedGround } from "@/components/design-engine/types";

/** The derivation that produced one slot on one ground, or nothing. */
export function derivationBySlot(
  ground: ResolvedGround | null,
  slot: string | null,
): Derivation | null {
  if (!ground || !slot) return null;
  return ground.derivations.find((d) => d.slot === slot) ?? null;
}

/**
 * "What changes if I move this" — a sentence and a filter, not a picture.
 *
 * The deleted graph's one genuinely unique job, answered from the same
 * `graph.edges` data it was drawn from. No diagram, no nested scroll axes, no
 * truncation, and it works at 320px.
 */
export function readersOf(ground: ResolvedGround | null, slot: string): string[] {
  if (!ground) return [];
  const direct = ground.graph.edges
    .filter(([from]) => from === slot)
    .map(([, to]) => to);
  const seen = new Set(direct);
  const queue = [...direct];
  while (queue.length) {
    const id = queue.shift() as string;
    for (const [from, to] of ground.graph.edges) {
      if (from === id && !seen.has(to)) {
        seen.add(to);
        queue.push(to);
      }
    }
  }
  return [...seen];
}
