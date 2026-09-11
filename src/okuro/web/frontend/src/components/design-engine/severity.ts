/**
 * The verdict, and the sentence a person reads instead of a machine code.
 *
 * TWO JOBS, and neither of them is deciding anything.
 *
 * 1. RANK. `severityOf` prefers the engine's own `severity` field and falls
 *    back to the table below, which is a MIRROR of `design_engine/colour.py`'s
 *    `SEVERITY` — pinned by `test_api.py`, so the two cannot drift silently.
 *    The fallback exists for one reason: a payload cached before F2 landed
 *    still has to render, and rendering it as `note` is the weakest available
 *    claim rather than a guess that could raise a verdict.
 *
 * 2. TRANSLATE. The engine's messages are the best writing in the product —
 *    "a light ground takes the brand's own black — 20.62:1 here" — but they are
 *    written to a builder reading a payload. `adviceFor` restates the five
 *    findings a user actually meets, in the register's own copy rules:
 *
 *      · Label: 1–2 words, Title Case, naming the TOPIC. Closed vocabulary.
 *      · Sentence 1: what is true, carrying the measurement.
 *      · Sentence 2: the fix, as an imperative, naming the control — and the
 *        number ONLY when the engine computed one.
 *      · No machine code in prose. `data-flag` carries it, for a PR.
 *      · No "please". Ends in a period.
 *      · An action is offered only when the engine computed the remedy.
 *        Otherwise the action navigates, and never guesses.
 *
 * NO NUMBER ON THIS PAGE IS INVENTED. Every measurement below is either read
 * out of the payload (`proposed_shade`) or parsed out of the engine's own
 * message (the contrast ratios). A page that recomputed a ratio would be a
 * second implementation of the one measurement the system takes, diverging
 * exactly at the threshold where it matters.
 */

import type { BrandColourModel, Flag, ResolvedModel, Severity } from "./types";

/* -------------------------------------------------------------- the ranking */

/**
 * The one constant, as the copy on this page says it.
 *
 * Mirror of `design_engine/colour.py::POLARITY_THRESHOLD`, pinned against it by
 * `test_api.py` so the two cannot drift silently. It exists because nine
 * sentences on this page name the number — "a shade that crosses 0.5", "must sit
 * below 0.5" — and when the constant moved from 0.675 to 0.5 and back, every one
 * of those nine had to be found by hand. One place to change is the fix; the
 * tripwire is what keeps it honest.
 *
 * Nothing DECIDES with this value. The engine decides; this is how the page
 * spells what it decided.
 */
export const POLARITY_THRESHOLD = 0.675;

/** The band the engine flags in. Mirror of `colour.py::WARN_BAND`. */
export const WARN_BAND: readonly [number, number] = [0.582, 0.752];

/** Mirror of `design_engine/colour.py::SEVERITY`. Fallback only — see header. */
export const SEVERITY: Record<string, Severity> = {
  "foreground.contrast-short": "must-fix",
  "font.weight-unreadable": "must-fix",
  "brand.adaptation-does-not-separate": "decide",
  "brand.figure-contrast-short": "decide",
  "branded.figure-contrast-short": "decide",
  "font.weights-not-monotone": "decide",
  "brand.possibly-unadjusted": "note",
  "brand.polarity-ambiguous": "note",
  "ground.polarity-ambiguous": "note",
  "shadow.became-border": "note",
};

export function severityOf(flag: Flag): Severity {
  return flag.severity ?? SEVERITY[flag.code] ?? "note";
}

export type Verdict = "pass" | "note" | "decide" | "must-fix";

const RANK: Record<Severity, number> = { note: 1, decide: 2, "must-fix": 3 };

/**
 * The verdict is the WORST severity present. Nothing present is a pass.
 *
 * Not a score and not an average: a brand with nine notes and one WCAG failure
 * has one thing to fix, and a page that averaged them would say it is mostly
 * fine.
 */
export function verdictOf(flags: Flag[]): Verdict {
  let worst: Verdict = "pass";
  for (const flag of flags) {
    const here = severityOf(flag);
    if (worst === "pass" || RANK[here] > RANK[worst as Severity]) worst = here;
  }
  return worst;
}

/** The glyph. Colour is never the only channel — see chrome.css. */
export const GLYPH: Record<Verdict, string> = {
  pass: "✓",
  note: "◆",
  decide: "◆",
  "must-fix": "▲",
};

/* --------------------------------------------------------------- the labels */

/**
 * The topic, in 1–2 words. The vocabulary is CLOSED.
 *
 * No `Warning`, no `Heads Up`, no `Note` — a label that names the tone instead
 * of the subject is a hedge, and the reader already has the tone from the
 * glyph and the colour.
 */
export const LABEL: Record<string, string> = {
  "foreground.contrast-short": "Contrast",
  "brand.figure-contrast-short": "Contrast",
  "branded.figure-contrast-short": "Contrast",
  "brand.adaptation-does-not-separate": "Adaptation",
  "brand.possibly-unadjusted": "Adaptation",
  "brand.polarity-ambiguous": "Ground",
  "ground.polarity-ambiguous": "Ground",
  "font.weight-unreadable": "Typeface",
  "font.weights-not-monotone": "Typeface",
  "shadow.became-border": "Shadow",
};

export function labelOf(flag: Flag): string {
  return LABEL[flag.code] ?? "Note";
}

/* -------------------------------------------------------------- the actions */

/**
 * What a flag's button DOES. Four kinds, and three of them carry a number the
 * engine computed. `show` is what is offered when it did not.
 */
export type AdviceAction =
  | { kind: "set-shade"; label: string; pole: "light" | "dark"; amount: number }
  | { kind: "apply-both"; label: string; light: number; dark: number }
  | { kind: "acknowledge"; label: string }
  | { kind: "explain"; label: string; slot: string }
  | {
      kind: "show";
      label: string;
      scene: "document" | "tokens" | "guests";
      slot?: string;
      /**
       * The root the canvas must be showing for `slot` to be the flagged value.
       *
       * A finding about ONE ground is only visible on that ground: `Show me` on
       * `foreground.contrast-short` used to open `foreground` on whatever ground
       * happened to be on screen, which was the passing light one — a 20.62 : 1
       * answer to a 4.07 : 1 complaint. `rootFor` reads the ground out of the
       * engine's own `where`, and the page places it before it opens.
       */
      root?: string;
    };

export interface Advice {
  label: string;
  /** One or two sentences. The engine's own message when nothing better exists. */
  body: string;
  /**
   * AT MOST ONE, per B2. A second chip beside the first is either a duplicate of
   * a control already on the row (`Set N %` beside `Use N %`) or a duplicate of
   * the label, which is itself the button that opens the inspector (`Why N %?`).
   */
  actions: AdviceAction[];
  /**
   * "I have read this" — not an action, the note's own close.
   *
   * It is rendered as a quiet control rather than a chip precisely so the note
   * carries ONE action: acknowledging changes no value, issues no call, and
   * writes nothing to the brand.
   */
  dismiss?: AdviceAction & { kind: "acknowledge" };
}

/**
 * Which ROOT a finding's `where` is about, as the page's own placement id.
 *
 * The engine's correspondence, restated: `signals.warning` is authored in the
 * rail and is placed on the canvas as the root `signal:warning`. A `where` that
 * names no ground (a font, a radius) answers null and nothing is placed.
 */
export function rootFor(where: string): string | null {
  const head = where.split("/")[0] ?? where;
  if (head.startsWith("signals.")) return `signal:${head.slice(8)}`;
  if (head === "brand.canonical") return "brand";
  if (head === "neutrals.white") return "light";
  if (head === "neutrals.black") return "dark";
  if (head.startsWith("ground:signal:")) return head.slice(7);
  if (head === "ground:root:light") return "light";
  if (head === "ground:root:dark") return "dark";
  return null;
}

const pct = (fraction: number) => `${Math.round(fraction * 100)} %`;

/** The ratio out of the ENGINE's own sentence. Never recomputed here. */
function ratioIn(message: string): string | null {
  const hit = /(\d+\.\d+)\s*:\s*1/.exec(message);
  return hit ? `${hit[1]} : 1` : null;
}

/** `signals.warning` -> `warning`; `ground:brand` -> `brand`. */
function tail(where: string): string {
  const head = where.split("/")[0] ?? where;
  const parts = head.split(/[.:]/);
  return parts[parts.length - 1] ?? head;
}

/** Which pole a `where` names, when it names one. */
function poleOf(where: string): "light" | "dark" | null {
  if (where.includes("on_light") && where.includes("on_dark")) return null;
  if (where.includes("on_light")) return "light";
  if (where.includes("on_dark")) return "dark";
  return null;
}

/**
 * The five findings a user actually meets, written out.
 *
 * Everything else falls through to the engine's own message, which is good
 * prose already — the failure was always the delivery, never the copy.
 */
export function adviceFor(
  flag: Flag,
  colours: BrandColourModel | null,
): Advice {
  const label = labelOf(flag);
  const pole = poleOf(flag.where);
  const proposed = (p: "light" | "dark") => colours?.poles[p].proposed_shade ?? null;

  switch (flag.code) {
    /* 1 — fires on the shipped default. The number is real: it is
       `poles.{pole}.proposed_shade`, already in the payload. */
    case "brand.adaptation-does-not-separate": {
      const p = pole ?? "light";
      const amount = proposed(p);
      const ground = p === "light" ? "light grounds" : "dark grounds";
      if (!amount) {
        return {
          label,
          body: `Your brand sits on the same side of ${POLARITY_THRESHOLD} as ${ground}, so it disappears there. Walk it toward the brand's own ${p === "light" ? "black" : "white"} until it crosses.`,
          actions: [{ kind: "show", label: "Show me", scene: "document" }],
        };
      }
      /* SENTENCE 2 IS THE FIX, AS AN IMPERATIVE, and the single action performs
         exactly it. `Why N %?` is gone because the control's own label is
         already the button that opens the inspector. */
      return {
        label,
        body: `Sits on the ${p} side of ${POLARITY_THRESHOLD} — ${ground} hide it. ${
          p === "light" ? "Darken" : "Lighten"
        } to ${pct(amount)}.`,
        actions: [{ kind: "set-shade", label: `Set ${pct(amount)}`, pole: p, amount }],
      };
    }

    /* 2 — the engine computes no replacement signal colour, so none is offered.
       The live ratio readout under the field is the feedback loop instead. */
    case "foreground.contrast-short": {
      const ratio = ratioIn(flag.message);
      const named = tail(flag.where);
      return {
        label,
        body: `Text on ${named} reads ${ratio ?? "under 4.5 : 1"}, under 4.5 : 1. Darken ${named}.`,
        actions: [
          {
            kind: "show",
            label: "Show me",
            scene: "document",
            slot: "foreground",
            root: rootFor(flag.where) ?? undefined,
          },
        ],
        dismiss: { kind: "acknowledge", label: "Keep it" },
      };
    }

    /* 3 — one finding about TWO fields. `slotsNamedBy()` already handles that
       correctly and is kept. */
    case "brand.possibly-unadjusted": {
      const light = proposed("light");
      const dark = proposed("dark");
      /* A ZERO IS NOT A PROPOSAL. `0 %` presented as "the engine proposes" and
         wired to `[Apply both]` would apply the value the brand already has,
         under a sentence saying it is the remedy. Falsy, not null-checked. */
      if (!light || !dark) {
        return {
          label,
          body: `Both poles hold one colour — usually unfinished. Walk one across ${POLARITY_THRESHOLD}.`,
          actions: [],
          dismiss: { kind: "acknowledge", label: "Keep as is" },
        };
      }
      return {
        label,
        body: `Both poles hold one colour. Apply ${pct(light)} and ${pct(dark)}.`,
        actions: [{ kind: "apply-both", label: "Apply both", light, dark }],
        dismiss: { kind: "acknowledge", label: "Keep as is" },
      };
    }

    /* 4 — the last clause is the engine's own sentence, kept. */
    case "brand.figure-contrast-short":
    case "branded.figure-contrast-short": {
      const ratio = ratioIn(flag.message);
      const p = pole ?? (flag.message.includes("on-dark") ? "dark" : "light");
      const amount = proposed(p);
      const head = `Your brand on its own ${p} ground reads ${ratio ?? "under 3 : 1"}, under 3 : 1.`;
      if (!amount) {
        return {
          label,
          body: `${head} Move the on-${p} shade — that is where a brand fixes this.`,
          actions: [{ kind: "show", label: "Show me", scene: "document" }],
          dismiss: { kind: "acknowledge", label: "Keep it" },
        };
      }
      return {
        label,
        body: `${head} Set the on-${p} shade to ${pct(amount)}.`,
        actions: [{ kind: "set-shade", label: `Set ${pct(amount)}`, pole: p, amount }],
        dismiss: { kind: "acknowledge", label: "Keep it" },
      };
    }

    /* 5 — nothing to fix. The note exists so a builder is not surprised later. */
    case "brand.polarity-ambiguous":
    case "ground.polarity-ambiguous": {
      const lightness = /(\d\.\d{3,})/.exec(flag.message)?.[1];
      return {
        label,
        body: `Lightness ${lightness ?? "here"} is inside the coin-flip band, ${WARN_BAND[0]}–${WARN_BAND[1]}.`,
        actions: [
          { kind: "show", label: "Show it on the scale", scene: "tokens", slot: flag.where },
        ],
      };
    }

    /* The rest keep the engine's own sentence. It is good prose; only the
       delivery was ever the failure. */
    default:
      return { label, body: flag.message, actions: [] };
  }
}

/* --------------------------------------------------------- the verdict copy */

export interface VerdictCopy {
  verdict: Verdict;
  glyph: string;
  text: string;
  action: string | null;
}

/**
 * Where a finding lives, in a reader's words. `signals.warning` -> `warning`.
 *
 * Used by the band and by the ranked list to POINT at a control instead of
 * repeating its advice.
 */
export function whereReads(flag: Flag): string {
  const head = flag.where.split("/")[0] ?? flag.where;
  if (head.startsWith("ground:")) return `the ${tail(head)} ground`;
  if (head.startsWith("signals.")) return `your ${tail(head)} colour`;
  if (head.startsWith("brand.on_")) return `on ${tail(head).replace("_", " ")} grounds`;
  if (head.startsWith("neutrals.")) return `the brand's ${tail(head)}`;
  if (head.startsWith("font")) return "the typeface";
  if (head === "brand.canonical") return "the brand colour";
  return tail(head);
}

/**
 * The band's whole sentence: exactly one of three patterns.
 *
 * IT NAMES THE WORST FINDING; IT DOES NOT QUOTE IT. The spec's pattern reads
 * "{n} thing{s} to fix. {the first must-fix's one-line body}", and quoting the
 * body verbatim would put the same sentence on screen twice — the band and the
 * help slot of the very control the flag is about, which auto-opens. The
 * assertable rule is that no message over 40 characters appears twice in one
 * viewport, and it exists because three findings used to render SIX times. So
 * the band states the topic and the control: one line, the worst finding, and a
 * different string from the advice it points at.
 */
export function verdictCopy(
  flags: Flag[],
  model: ResolvedModel | null,
): VerdictCopy {
  const verdict = verdictOf(flags);
  const glyph = GLYPH[verdict];
  if (verdict === "pass") {
    const derived = model
      ? model.grounds.reduce((total, g) => total + g.derivations.length, 0)
      : 0;
    return {
      verdict,
      glyph,
      text: `This brand is ready. ${derived} values derived, nothing flagged.`,
      action: null,
    };
  }
  if (verdict === "must-fix") {
    const worst = flags.find((f) => severityOf(f) === "must-fix") as Flag;
    const n = flags.filter((f) => severityOf(f) === "must-fix").length;
    return {
      verdict,
      glyph,
      text: `${n} thing${n === 1 ? "" : "s"} to fix. ${labelOf(worst)}, on ${whereReads(worst)}.`,
      action: "Fix it",
    };
  }
  const n = flags.length;
  return {
    verdict,
    glyph,
    text: `This brand works. ${n} thing${n === 1 ? "" : "s"} worth deciding.`,
    action: "Review",
  };
}
