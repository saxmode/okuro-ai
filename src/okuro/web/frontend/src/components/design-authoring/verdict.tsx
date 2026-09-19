/**
 * "Is my configuration good?" — the answer, above the fold, on either page.
 *
 * This is an authoring page's stated purpose, and both pages had a COUNT where
 * the answer belongs: `3 flags` in a toolbar on one, `2 findings` beside a dot
 * on the other. A number is not a judgement — it does not say how bad, about
 * what, or what to do.
 *
 * THREE SEVERITIES, ONE SENTENCE, ONE ACTION. The band states the COUNT and
 * NAMES the worst finding — its topic and the control it is about — and never
 * the body of any flag. The full advice lives once, in the help slot of that
 * control. Measured before: three findings rendered SIX times across three
 * surfaces, and the canvas `title` concatenated the duplicates.
 *
 * GLYPH AND COLOUR, ALWAYS. Severity is never signalled by colour alone: the
 * icon exists to help a reader with a colour vision deficiency discern the
 * message tone.
 *
 * HINTS, NEVER ERRORS — his rule, and the engine already agrees. A flag comes
 * back ALONGSIDE a fully resolved model, never instead of one. `must-fix` says
 * how bad a finding is, not that a value was refused, and nothing in this file
 * calls a finding an error or blocks anything.
 *
 * PAGE-NEUTRAL SINCE WAVE 3. It paints with the class names its HOST hands it
 * (`vocabulary`) and publishes `data-verdict`, `data-severity`, `data-flag` and
 * `data-verdict-action` for anything that reads it. The acknowledgement store
 * and the copy live in `lib/flags.ts`, which both pages already read.
 */

import type { Flag, ResolvedModel } from "@/components/design-engine/types";
import {
  labelOf,
  rankOf,
  severityOf,
  verdictCopy,
  verdictOf,
  whereReads,
} from "@/lib/flags";
import type { AuthoringVocabulary } from "./vocabulary";

/* ------------------------------------------------------------------- the band */

export function VerdictBand({
  flags,
  model,
  onReview,
  busy,
  vocabulary,
  className,
}: {
  flags: Flag[];
  model: ResolvedModel | null;
  onReview: (flag: Flag | null) => void;
  /** The engine has not answered yet. The band says so rather than claiming a
      pass it cannot know — a green tick over an unresolved model is a lie. */
  busy?: boolean;
  vocabulary: AuthoringVocabulary;
  /**
   * The host's own box for the band: its height, its padding, its rule.
   *
   * WHERE A BLOCK SITS IS THE HOST'S CALL — the seam rule wave 2 established.
   * One page gives this a 56px strip under a full-width toolbar; the other
   * gives it the head of a 384px rail. A shared component that fixed either
   * would be replacing a page's layout, which is the whole subject of the rule.
   */
  className?: string;
}) {
  const copy = verdictCopy(flags, model);
  const worst = flags.find((f) => severityOf(f) === verdictOf(flags)) ?? flags[0] ?? null;

  if (busy) {
    return (
      <div data-verdict="deriving" className={[className, vocabulary.row].filter(Boolean).join(" ")}>
        <span aria-hidden className={`${vocabulary.title} ${vocabulary.dim}`}>
          ·
        </span>
        <span className={`${vocabulary.title} ${vocabulary.dim}`}>Deriving the system…</span>
      </div>
    );
  }

  return (
    <div
      data-verdict={copy.verdict}
      data-severity={copy.verdict === "pass" ? undefined : copy.verdict}
      role="status"
      className={[className, vocabulary.row].filter(Boolean).join(" ")}
    >
      <span
        aria-hidden
        className={vocabulary.title}
        style={{ color: vocabulary.severityColour }}
      >
        {copy.glyph}
      </span>
      <span className={vocabulary.title} style={{ minWidth: 0 }}>
        {copy.text}
      </span>
      {copy.action && (
        <button
          type="button"
          className={vocabulary.chip}
          data-verdict-action
          style={{ marginLeft: "auto" }}
          onClick={() => onReview(worst)}
        >
          {copy.action}
        </button>
      )}
    </div>
  );
}

/* ------------------------------------------------------- the ranked findings */

/**
 * Every finding, ranked, as a list — the `Review` destination.
 *
 * It lives here rather than in a panel because a LIST of findings is the
 * verdict's own detail, not a fifth surface. Each row is one line and a
 * jump: the full advice is in the control's help slot, once.
 */
export function FlagList({
  flags,
  onPick,
  vocabulary,
}: {
  flags: Flag[];
  onPick?: (flag: Flag) => void;
  vocabulary: AuthoringVocabulary;
}) {
  if (!flags.length) {
    return (
      <p className={`${vocabulary.body} ${vocabulary.dim}`}>
        Nothing flagged. The engine had no advice about these values.
      </p>
    );
  }
  const ordered = flags
    .slice()
    .sort((a, b) => rankOf(severityOf(b)) - rankOf(severityOf(a)));
  return (
    <ul className={vocabulary.stack} data-flag-list>
      {ordered.map((flag, i) => (
        <li key={`${flag.code}-${flag.where}-${i}`}>
          <button
            type="button"
            data-flag={flag.code}
            data-severity={severityOf(flag)}
            className={vocabulary.row}
            style={{
              width: "100%",
              textAlign: "left",
              minHeight: 24,
              background: "transparent",
              border: 0,
              color: "inherit",
              cursor: onPick ? "pointer" : "default",
            }}
            onClick={() => onPick?.(flag)}
          >
            <span
              aria-hidden
              className={vocabulary.body}
              style={{ color: vocabulary.severityColour, width: 12 }}
            >
              {GLYPH_FOR[severityOf(flag)]}
            </span>
            <span className={vocabulary.body} style={{ fontWeight: 600 }}>
              {labelOf(flag)}
            </span>
            {/* THE ADDRESS, NOT THE ADVICE. The advice is in that control's own
                help slot, once — a list that repeated it would put the same
                sentence on screen twice, which is the defect this page was
                rejected for. */}
            <span
              className={`${vocabulary.body} ${vocabulary.dim}`}
              style={{ minWidth: 0 }}
            >
              on {whereReads(flag)}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

/** The per-severity mark, so colour is never the only channel. */
export const GLYPH_FOR: Record<string, string> = {
  "must-fix": "▲",
  decide: "◆",
  note: "·",
};
