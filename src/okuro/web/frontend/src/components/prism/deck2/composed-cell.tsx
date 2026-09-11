// SPDX-License-Identifier: Apache-2.0
// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism deck v2 — ComposedCell. Renders the v4 free-composition
//   SOLVER's real kit HTML (cell.composed, from compose_ladder) in place of the
//   12-archetype downcast. The kit CSS (incl. components-ext + composed grid) is
//   already injected into the deck's shadow root by kit-assets, so this only
//   mounts the fragment — ZoomStage scales the 1600px composed-slide to fit like
//   any kit slide. Byte-identical to the gallery render (no re-render, no drift).
// AGENT_HEADER_END -->
import type { ComposedRender } from "./deck-types";

interface ComposedCellProps {
  composed: ComposedRender;
  /** 0 = primary placement (A), 1 = the solver's B variant (falls back to A). */
  variant?: number;
}

/** Below this share of rendered words tracing to the cell's own claims, the
 *  surface is showing fixture text rather than the document. Mirrors
 *  `to_composed.FIDELITY_FLOOR` — keep the two in step. */
export const FIDELITY_FLOOR = 0.35;

/** Mount the solver-rendered kit fragment. The fragment is trusted server output
 *  (our own Python kit builders emit it from mined claims — never user HTML), so
 *  dangerouslySetInnerHTML is the correct, drift-free injection.
 *
 *  A cell that scored below the fidelity floor still renders — refusing to draw
 *  it would hide the defect rather than expose it — but it is BADGED. Under
 *  `prism.strict` such a cell never reaches the store at all; this path exists
 *  for a deck built with strict off, and a reader must not mistake placeholder
 *  text for the document. */
export function ComposedCell({ composed, variant = 0 }: ComposedCellProps) {
  const html = variant === 1 && composed.altHtml ? composed.altHtml : composed.html;
  const fidelity = composed.fidelity;
  const unfaithful = typeof fidelity === "number" && fidelity < FIDELITY_FLOOR;
  // `.composed-mount` is `display: contents` (kit/gallery/composed.css) so the
  // fragment's own `.composed-slide` is the layout box. Wrapping the html in an
  // extra element would insert a box the grid does not expect — badge and
  // fragment stay SIBLINGS inside the transparent mount.
  return (
    <div className="composed-mount">
      {unfaithful ? (
        <span className="composed-unfaithful" data-testid="cell-unfaithful"
              title={`Only ${Math.round(fidelity * 100)}% of this surface traces to the source document — the rest is placeholder content from the component kit.`}>
          unverified content
        </span>
      ) : null}
      <div className="composed-frag" style={{ display: "contents" }}
           dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}
