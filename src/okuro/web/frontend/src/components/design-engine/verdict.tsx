/**
 * "Is my configuration good?" — the answer, at 20px, above the fold.
 *
 * This is the page's stated purpose and it had NO SURFACE AT ALL. What it had
 * was `3 flags`, at 7px, in grey, in a toolbar, unclickable. Everything here is
 * new.
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
 * ACKNOWLEDGEMENTS LIVE IN THE PAGE, NEVER IN THE BRAND. "Keep as is" drops a
 * finding to a note, greys it, and stops it driving the verdict — and it is
 * stored in localStorage under `brandId:code`. Register rule 12 lists the
 * authored set COMPLETELY and an acknowledgement is not on it. It is a reader's
 * note, not a brand fact, and it must never reach a kit file.
 */

import { useCallback, useEffect, useState } from "react";
import type { Flag, ResolvedModel } from "./types";
import { labelOf, severityOf, verdictCopy, verdictOf, whereReads } from "./severity";

/* ------------------------------------------------------- the acknowledgements */

const STORE = "okuro.design-engine.acknowledged";

function readStore(): Record<string, true> {
  try {
    return JSON.parse(window.localStorage.getItem(STORE) ?? "{}");
  } catch {
    return {};
  }
}

export function acknowledgementKey(brandId: string, code: string): string {
  return `${brandId}:${code}`;
}

/**
 * Which findings this reader has said "keep as is" to.
 *
 * Returns a live set plus a toggle. The brand payload is BYTE-IDENTICAL across
 * an acknowledgement — that is the property that makes this legal.
 */
export function useAcknowledged(brandId: string | undefined) {
  const [store, setStore] = useState<Record<string, true>>({});

  useEffect(() => setStore(readStore()), []);

  const acknowledge = useCallback(
    (code: string) => {
      if (!brandId) return;
      const next = { ...readStore(), [acknowledgementKey(brandId, code)]: true as const };
      window.localStorage.setItem(STORE, JSON.stringify(next));
      setStore(next);
    },
    [brandId],
  );

  const isAcknowledged = useCallback(
    (code: string) => Boolean(brandId && store[acknowledgementKey(brandId, code)]),
    [brandId, store],
  );

  return { acknowledge, isAcknowledged };
}

/**
 * An acknowledged finding drops to `note`.
 *
 * It is not deleted: it is still true, it still marks its control and its
 * component, and it still reads in the inspector. What it stops doing is
 * driving the verdict, because the reader has answered it.
 */
export function withAcknowledgements(
  flags: Flag[],
  isAcknowledged: (code: string) => boolean,
): Flag[] {
  return flags.map((flag) =>
    isAcknowledged(flag.code) ? { ...flag, severity: "note" as const } : flag,
  );
}

/* ------------------------------------------------------------------- the band */

export function VerdictBand({
  flags,
  model,
  onReview,
  busy,
}: {
  flags: Flag[];
  model: ResolvedModel | null;
  onReview: (flag: Flag | null) => void;
  /** The engine has not answered yet. The band says so rather than claiming a
      pass it cannot know — a green tick over an unresolved model is a lie. */
  busy?: boolean;
}) {
  const copy = verdictCopy(flags, model);
  const worst = flags.find((f) => severityOf(f) === verdictOf(flags)) ?? flags[0] ?? null;

  if (busy) {
    return (
      <div
        data-verdict="deriving"
        className="ce-row"
        style={{
          height: 56,
          padding: `0 var(--ce-f6)`,
          borderBottom: "1px solid var(--ce-border)",
          gap: 12,
        }}
      >
        <span aria-hidden className="ce-title" style={{ color: "var(--ce-fg-2)" }}>
          ·
        </span>
        <span className="ce-title" style={{ color: "var(--ce-fg-2)" }}>
          Deriving the system…
        </span>
      </div>
    );
  }

  return (
    <div
      data-verdict={copy.verdict}
      data-severity={copy.verdict === "pass" ? undefined : copy.verdict}
      role="status"
      className="ce-row"
      style={{
        height: 56,
        padding: `0 var(--ce-f6)`,
        borderBottom: "1px solid var(--ce-border)",
        gap: 12,
      }}
    >
      <span
        aria-hidden
        className="ce-title"
        style={{ color: "var(--ce-severity, var(--ce-fg))" }}
      >
        {copy.glyph}
      </span>
      <span className="ce-title" style={{ minWidth: 0 }}>
        {copy.text}
      </span>
      {copy.action && (
        <button
          type="button"
          className="ce-chip"
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
  colours: _colours,
  onPick,
}: {
  flags: Flag[];
  colours?: unknown;
  onPick?: (flag: Flag) => void;
}) {
  if (!flags.length) {
    return (
      <p className="ce-body ce-2">
        Nothing flagged. The engine had no advice about these values.
      </p>
    );
  }
  const ordered = flags
    .slice()
    .sort((a, b) => weight(severityOf(b)) - weight(severityOf(a)));
  return (
    <ul className="ce-stack" data-flag-list>
      {ordered.map((flag, i) => (
        <li key={`${flag.code}-${flag.where}-${i}`}>
          <button
            type="button"
            data-flag={flag.code}
            data-severity={severityOf(flag)}
            className="ce-row"
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
              className="ce-body"
              style={{ color: "var(--ce-severity)", width: 12 }}
            >
              {severityOf(flag) === "must-fix"
                ? "▲"
                : severityOf(flag) === "decide"
                  ? "◆"
                  : "·"}
            </span>
            <span className="ce-body" style={{ fontWeight: 600 }}>
              {labelOf(flag)}
            </span>
            {/* THE ADDRESS, NOT THE ADVICE. The advice is in that control's own
                help slot, once — a list that repeated it would put the same
                sentence on screen twice, which is the defect this page was
                rejected for. */}
            <span className="ce-body ce-2" style={{ minWidth: 0 }}>
              on {whereReads(flag)}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function weight(severity: string): number {
  return severity === "must-fix" ? 3 : severity === "decide" ? 2 : 1;
}
