// SPDX-License-Identifier: Apache-2.0
/**
 * THE TWO TITLE BARS — one pattern, two variants, both on a blurred plate.
 *
 * The owner sketched them 2026-09-16 and ruled the plate the same day:
 *
 *   SECTION  `[SECTION INTRO]`            right: `[ SEARCH | SORT | FILTER | ADD/DELETE ]`
 *   DETAIL   `← [DETAIL TITLE] [STATUS]`  right: `[Details] [Actions]`
 *
 * They are ONE component in two shapes on purpose: *"These need to be two
 * different components and will be used by detail pages as well for ui
 * consistency."* A leaf and a detail page must not each invent a header.
 *
 * ===========================================================================
 * THIS SUPERSEDES ONE SENTENCE OF R2, AND THE SENTENCE IS NAMED
 * ===========================================================================
 * R2 (2026-09-14) reads: *"the padding ring scrolls WITH the content and the
 * title scrolls away with it."* Both sketches show page content passing UNDER
 * the title on a blurred plate — so the title PINS instead of scrolling away.
 *
 * That is a change of consequence, not a contradiction of R2's subject. R2
 * ruled which element scrolls (`.content`, still true here). What changed is
 * what the title IS: it was a heading, it is now a control surface carrying
 * search, sort, filter, add, back and actions. A control surface that scrolls
 * out of reach is a control surface you cannot use. R2 itself superseded
 * exactly one half of the contract before it; this is the same shape.
 *
 * ===========================================================================
 * WHAT THE PLATE IS MADE OF, ALL RULED RATHER THAN CHOSEN
 * ===========================================================================
 *   blur    `var(--blur-md)` — the engine's `normal` intensity. It exists as a
 *           NAME only since the same day: the engine used to publish blur on
 *           one class at the LIGHT intensity and emitted no `--blur-*` at all.
 *           Reading the name is the whole point, so no literal appears here.
 *   veil    `rgba(0,0,0,0.4)` — his 40% darken, over the blur, under the text.
 *   edge    HARD. Ruled 2026-09-16 over a downward fade: a fade needs a mask
 *           and makes content half-emerge as it passes behind.
 *   height  ITS CONTENT. Never a number — see `.c-band` in shell.css.
 *
 * ===========================================================================
 * THE RIGHT-HAND SLOT IS A SLOT, NOT A TOOLBAR
 * ===========================================================================
 * Both variants take `actions` as children rather than enumerating buttons.
 * The sketch shows find/sort/filter/add on one and edit/remove on the other,
 * and the leaves already own those controls with their own state and handlers.
 * Enumerating them here would mean this file grows a prop per control and every
 * leaf bends to its vocabulary; taking a slot means a leaf hands over what it
 * already has. The BAR owns the geometry and the plate; the leaf owns its
 * actions.
 */

import type { ReactNode } from "react";
import { ArrowLeft } from "lucide-react";

/**
 * SECTION TITLE — the leaf's own header.
 *
 * `title` is normally the leaf title the shell already derives, so a page that
 * renders this must NOT also print its own heading; R1 ruled the title is the
 * shell's and pages drop theirs.
 */
export function SectionTitle({
  title,
  actions,
  owner,
  actionsOwner,
}: {
  title: ReactNode;
  /** search / sort / filter / add — the leaf's own controls. */
  actions?: ReactNode;
  /** The bar's CURRENT leaf — whose name the heading shows. */
  owner?: string | number;
  /** The leaf that PUBLISHED the controls. Deliberately a SECOND value rather
      than the same one on both elements: if both carried `owner` they could
      never disagree, and the assertion that they match would be vacuous. The
      whole defect is a row from one leaf under another leaf's heading, so the
      two stamps have to come from two sources. */
  actionsOwner?: string | number;
}) {
  return (
    <div className="c-band">
      <div className="c-band-row">
        {/* THE DOCUMENT'S ONE `<h1>`, kept — S1 ruled one per document and
            `lib/page-context.ts` derives the chat's page title from it. Moving
            the heading into this component must not quietly create a second or
            drop the only one. */}
        <h1 className="c-title" data-leaf-owner={owner}>{title}</h1>
        {actions ? (
          <div className="c-band-actions" data-leaf-owner={actionsOwner ?? owner}>
            {actions}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/**
 * DETAIL TITLE — one record, inside a leaf.
 *
 * `onBack` is REQUIRED and the arrow is not optional, because that is the whole
 * reason this variant exists: a detail view is reached from a list and must say
 * how to get back. The in-page navigation pattern is its own open todo; this
 * component is where it will land rather than a sixth private back button.
 */
export function DetailTitle({
  title,
  status,
  meta,
  actions,
  onBack,
}: {
  title: ReactNode;
  /** the pill next to the title — `done`, `running`, … */
  status?: ReactNode;
  /** the quiet run of facts after it — `STD · 1/1 · 100% · 18m 30s`. */
  meta?: ReactNode;
  actions?: ReactNode;
  onBack(): void;
}) {
  return (
    <div className="c-band">
      <div className="c-band-row">
        <button type="button" className="c-band-back sh-ctl" aria-label="Back" onClick={onBack}>
          <ArrowLeft aria-hidden="true" />
        </button>
        <h1 className="c-title c-title-detail">{title}</h1>
        {status ? <span className="c-band-status">{status}</span> : null}
        {meta ? <span className="c-band-meta">{meta}</span> : null}
        {actions ? <div className="c-band-actions">{actions}</div> : null}
      </div>
    </div>
  );
}
