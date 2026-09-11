// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism deck v2 — ACCURACY RENDERING (PRISM v4 §3d, W3 fields).
//   Consumes the DeckDoc.accuracy units for the focused level and surfaces every
//   truth the pipeline computed, never a silent thin slide: deficit affordances
//   ("N of M — all M in L4", click → drill to the deepest level), synthesized /
//   fallback badges, count-truth mismatches, and content-gaps. Empty when the
//   cell is fully accurate.
// AGENT_HEADER_END -->
import type { AccuracyUnit } from "./deck-types";

interface AccuracyStripProps {
  units: AccuracyUnit[];
  /** Drill to the deepest level (L4) where the complete set lives. */
  onDrillToL4: () => void;
}

export function AccuracyStrip({ units, onDrillToL4 }: AccuracyStripProps) {
  const deficits = units.filter((u) => u.affordance);
  const synth = units.some((u) => u.synthesized);
  const fallback = units.some((u) => u.fallback);
  const countMiss = units.filter((u) => u.count_mismatch);
  const gaps = units.filter((u) => u.content_incomplete);

  if (!deficits.length && !synth && !fallback && !countMiss.length && !gaps.length) {
    return null;
  }

  return (
    <div className="deck-accuracy" data-testid="deck-accuracy">
      {deficits.map((u, i) => (
        <button
          key={`d${i}`}
          type="button"
          className="acc-chip acc-deficit"
          data-testid="acc-deficit"
          onClick={onDrillToL4}
          title="Only some items fit at this depth — click to see the complete set in L4"
        >
          {u.affordance} <span aria-hidden>↗</span>
        </button>
      ))}
      {synth ? (
        <span className="acc-chip acc-synth" data-testid="acc-synth"
          title="Fail-soft: this cell was code-synthesized, not authored from the source.">
          synthesized
        </span>
      ) : null}
      {fallback ? (
        <span className="acc-chip acc-fallback" data-testid="acc-fallback"
          title="Rendered with a lower-fit fallback component.">
          fallback
        </span>
      ) : null}
      {countMiss.map((u, i) => (
        <span key={`c${i}`} className="acc-chip acc-count" data-testid="acc-count"
          title={`Count-truth: the title asserts a number that differs from the ${u.rendered} items shown.`}>
          count check
        </span>
      ))}
      {gaps.map((u, i) => (
        <span key={`g${i}`} className="acc-chip acc-gap" data-testid="acc-gap"
          title={`Content gap: ${u.content_available} of ${u.rendered} items carry real source content — the rest are thin.`}>
          content gap
        </span>
      ))}
    </div>
  );
}
