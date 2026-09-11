/**
 * Advice, placed by PROXIMITY rather than by architecture.
 *
 * The root cause this file exists to end: help, provenance and errors used to
 * live in a panel addressed by a TAB, so the good sentences hung off 1px hover
 * targets and the errors appeared up to 800px from their cause. Nothing
 * explanatory lives in a tab any more. Help goes in a reserved slot under the
 * control; an error swaps into that same slot; the "why" opens where it was
 * clicked.
 *
 * THE ONE-SLOT CONTRACT. Every control owns exactly ONE reserved line beneath
 * it, and the line occupies its height whether or not it currently holds text:
 *
 *     calm    what this setting does — one sentence
 *     advice  the flag body + its action
 *     error   how to fix it
 *
 * The description and the error SHARE the slot and carry the same essential
 * information, so nothing is lost when one replaces the other and the layout
 * never jumps under the reader's eye at the moment they need to read it.
 */

import type { ReactNode } from "react";
import type { BrandColourModel, Flag } from "./types";
import { adviceFor, severityOf, type AdviceAction } from "./severity";

export type HelpState = "calm" | "advice" | "error";

/* ------------------------------------------------------------------- a note */

/**
 * One finding, once, beside the value it is about.
 *
 * ONE NOTE PER CONCEPT. Three notes stacked on one control means the layout is
 * wrong, not the copy — so this renders a single flag and the caller picks
 * which. `HelpSlot` picks the worst.
 */
export function Note({
  flag,
  colours,
  onAction,
}: {
  flag: Flag;
  colours: BrandColourModel | null;
  onAction?: (action: AdviceAction, flag: Flag) => void;
}) {
  const advice = adviceFor(flag, colours);
  const severity = severityOf(flag);
  return (
    <span
      data-note={flag.code}
      /* THE MACHINE CODE LIVES HERE AND NOWHERE A PERSON READS. It is what a
         builder copies into a PR; it is not a sentence. */
      data-flag={flag.code}
      data-severity={severity}
      className="ce-body flex gap-[8px]"
      style={{ color: "var(--ce-fg-2)" }}
    >
      <span
        aria-hidden
        className="ce-body shrink-0"
        style={{ color: "var(--ce-severity)", width: 12 }}
      >
        {severity === "must-fix" ? "▲" : severity === "decide" ? "◆" : "·"}
      </span>
      <span className="min-w-0">
        <b style={{ color: "var(--ce-fg)", fontWeight: 600 }}>{advice.label}</b>
        {" · "}
        {advice.body}
        {/* ONE ACTION, and — separately — the note's own close.
            A second CHIP beside the first always turned out to be a duplicate:
            of a control already on the row, or of the label, which is itself
            the button that opens the inspector. What is not a duplicate is
            "I have read this": it performs no edit, issues no call and writes
            nothing to the brand, so it is a quiet control rather than a chip. */}
        {(advice.actions.length > 0 || advice.dismiss) && (
          <span className="ce-row" style={{ marginTop: 8, gap: 8 }}>
            {advice.actions.slice(0, 1).map((action) => (
              <button
                key={action.label}
                type="button"
                className="ce-chip"
                data-action={action.kind}
                /* ONE BUTTON, BOTH NAMES. `[Use 32 %]` on the shade row and
                   `[Set 32 %]` in the note 130px below were two controls
                   performing one edit, in one group — "which do I press". They
                   are the same control now: when a note offers the shade, the
                   note's action IS the proposal control and the row does not
                   draw a second one. */
                data-use-proposal={
                  action.kind === "set-shade" ? action.pole : undefined
                }
                onClick={() => onAction?.(action, flag)}
              >
                {action.label}
              </button>
            ))}
            {advice.dismiss && (
              <button
                type="button"
                className="ce-quiet"
                data-action={advice.dismiss.kind}
                data-dismiss
                onClick={() => onAction?.(advice.dismiss as AdviceAction, flag)}
              >
                {advice.dismiss.label}
              </button>
            )}
          </span>
        )}
      </span>
    </span>
  );
}

/* -------------------------------------------------------------- the one slot */

/**
 * The reserved line under a control. Its height does not change with its state.
 *
 * `calm` is not filler. It is what this setting does, in one sentence, at 14px
 * in secondary — permanently visible, because anything a person needs in order
 * to JUDGE the configuration may not be hidden behind a hover.
 */
export function HelpSlot({
  calm,
  flags,
  error,
  colours,
  onAction,
}: {
  calm?: ReactNode;
  flags?: Flag[];
  error?: string | null;
  colours?: BrandColourModel | null;
  onAction?: (action: AdviceAction, flag: Flag) => void;
}) {
  /* THE WORST ONE, and only the worst one. A control carrying three findings is
     a layout problem; stacking them here would hide that. */
  const worst = (flags ?? [])
    .slice()
    .sort((a, b) => rank(severityOf(b)) - rank(severityOf(a)))[0];

  if (error) {
    /* THE RESERVATION IS THE TALLEST OCCUPANT, not two lines.
       `min-height: 2 lines` in the stylesheet holds the no-jump contract only
       while every occupant fits in two, and an advice note does not: it carries
       a sentence plus an action row. So when this control has a finding, the
       note is laid in the same grid cell as the error, hidden and inert, purely
       to keep the cell the height it already had. Measured on the shipped kit:
       the Brand colour slot is 92px carrying its advice and was collapsing to
       40px the moment a bad hex replaced it — a 52px jump under the reader's
       eye at exactly the moment the contract names. */
    return (
      <span data-help data-state="error" role="alert" data-reserved={worst ? "" : undefined}>
        {worst && (
          <span data-help-reserve aria-hidden="true">
            <Note flag={worst} colours={colours ?? null} />
          </span>
        )}
        <span data-help-live>{error}</span>
      </span>
    );
  }
  if (worst) {
    return (
      <span data-help data-state="advice">
        <Note flag={worst} colours={colours ?? null} onAction={onAction} />
      </span>
    );
  }
  return (
    <span data-help data-state="calm">
      {calm}
    </span>
  );
}

function rank(severity: string): number {
  return severity === "must-fix" ? 3 : severity === "decide" ? 2 : 1;
}

/* ---------------------------------------------------------- the info popover */

/**
 * The only tooltip shape this page allows, and it has THREE lines.
 *
 * An icon survives only when all three hold: the sentence states a constraint,
 * limit or consequence rather than a definition; it is NOT needed to judge the
 * configuration; and the control's own label does not already say it. Fewer
 * than three lines means the icon did not survive the audit.
 *
 * The trigger is a 16px glyph inside a 24×24 hit box. A 1×16px trigger is a
 * defect, not a small target — two of the most valuable sentences in the
 * product used to hang off exactly that.
 */
export function InfoLines({
  what,
  changes,
  good,
}: {
  what: string;
  changes: string;
  good: string;
}) {
  return (
    <span className="ce-stack-tight" style={{ maxWidth: 320 }}>
      {(
        [
          ["What it is", what],
          ["What changes it", changes],
          ["What good looks like", good],
        ] as const
      ).map(([title, line]) => (
        <span key={title} className="ce-body">
          <b style={{ fontWeight: 600 }}>{title}</b> — {line}
        </span>
      ))}
    </span>
  );
}
