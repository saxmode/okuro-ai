/**
 * ONE FLAG MODEL, for every page that renders the engine's findings.
 *
 * THE CLASS THIS FILE ENDS. Two authoring pages each answered "which field does
 * this flag's `where` name" in their own code — `rail.tsx::slotsNamedBy` split
 * the address, `configuration-panel.tsx::addressesOf` + `WHERE_ALIASES`
 * translated the engine's ROLE name to the brand's HUE name — so a flag could
 * reach a control on one page and nothing on the other. Both are below, merged,
 * once. The severity rank, the topic label and the acknowledgement store lived
 * in three and two places respectively; they are here too.
 *
 * FOUR JOBS, and none of them is deciding anything.
 *
 * 1. ADDRESS. `addressesOf` answers every authored field a `where` names. A
 *    `where` may name SEVERAL (`brand.on_light/on_dark` is ONE finding about TWO
 *    fields) and it may name a field under the engine's own vocabulary rather
 *    than the brand's (`signals.error` is authored as `signals.red`).
 *    `placeFlags` puts each flag at its fields and keeps the ones it could not
 *    place, because a finding that exists in the payload and reaches no pixel is
 *    the whole class.
 *
 * 2. RANK. `severityOf` reads the ENGINE's `severity` and nothing else. The
 *    frontend mirror of `colour.py::SEVERITY` that used to stand here as a
 *    fallback is DELETED: `api.py::_flag` writes `flag.severity` on every flag
 *    it serialises, `colour.Flag.severity` is a property that answers
 *    `severity_of(code)` with `note` for an unranked code, and
 *    `test_api.py::test_every_flag_the_api_ships_carries_its_severity` asserts
 *    the field against `c.severity_of` for every flag on the wire. A second
 *    table could only disagree with that one.
 *
 * 3. TRANSLATE. The engine's messages are the best writing in the product —
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
 *    NEVER ERROR -> HINT, his rule. A flag comes back ALONGSIDE a fully resolved
 *    model, never instead of one, so `must-fix` says how bad a finding is and
 *    not that a value was refused. Nothing in this file uses the word "error"
 *    about a finding, and nothing here rejects a value.
 *
 * 4. REMEMBER. `useAcknowledged` is the reader's "keep as is", stored per KIT
 *    under a page-neutral key — see `STORE` below. Register rule 12 lists the
 *    authored set COMPLETELY and an acknowledgement is not on it: it is a
 *    reader's note, not a brand fact, and it must never reach a kit file.
 *
 * NO NUMBER ON THIS PAGE IS INVENTED. Every measurement below is either read
 * out of the payload (`proposed_shade`) or parsed out of the engine's own
 * message (the contrast ratios). A page that recomputed a ratio would be a
 * second implementation of the one measurement the system takes, diverging
 * exactly at the threshold where it matters.
 */

import { useCallback, useEffect, useState } from "react";

import type {
  BrandColourModel,
  Flag,
  ResolvedModel,
  Severity,
} from "@/components/design-engine/types";

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

/**
 * How bad a finding is — the ENGINE's answer, never a second one.
 *
 * TYPED AGAINST WHAT IT READS, not against `Flag`. The preview frame carries a
 * narrower flag shape of its own (`FrameModel.brandFlags`, no `where` on a
 * per-ground flag) and it is the third place that used to keep its own copy of
 * this table. A function that demanded the whole `Flag` would have forced that
 * copy to stay.
 *
 * MEASURED, 2026-09-13, which is why the fallback table is gone.
 * `colour.Flag.severity` is a `@property` returning `severity_of(self.code)`,
 * and `severity_of` is `SEVERITY.get(code, "note")` — it cannot return nothing.
 * `api.py::_flag` puts that value on every flag it serialises, brand-level and
 * per-ground alike. A frontend table keyed by code could therefore only ever
 * disagree with the engine, which is the "second authority" the charter names.
 *
 * The `?? "note"` is the TYPE's optionality, not a ranking: `Flag.severity` is
 * declared optional so a payload that predates the field still parses, and
 * unranked advice is the weakest claim available — the same rule the engine
 * states for a code nobody ranked.
 */
export function severityOf(flag: { severity?: Severity | string }): Severity {
  return (flag.severity as Severity | undefined) ?? "note";
}

/** The one ordering of the three severities. Four copies of this existed. */
export function rankOf(severity: string): number {
  return severity === "must-fix" ? 3 : severity === "decide" ? 2 : 1;
}

/** The worst finding in a list, or `undefined` when there is none. */
export function worstOf<T extends { severity?: Severity | string }>(
  flags: T[],
): T | undefined {
  return flags
    .slice()
    .sort((a, b) => rankOf(severityOf(b)) - rankOf(severityOf(a)))[0];
}

export type Verdict = "pass" | "note" | "decide" | "must-fix";

/**
 * The verdict is the WORST severity present. Nothing present is a pass.
 *
 * Not a score and not an average: a brand with nine notes and one WCAG failure
 * has one thing to fix, and a page that averaged them would say it is mostly
 * fine.
 */
export function verdictOf(flags: { severity?: Severity | string }[]): Verdict {
  let worst: Verdict = "pass";
  for (const flag of flags) {
    const here = severityOf(flag);
    if (worst === "pass" || rankOf(here) > rankOf(worst)) worst = here;
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

export function labelOf(flag: { code: string }): string {
  return LABEL[flag.code] ?? "Note";
}

/* -------------------------------------------------------------- the actions */

/**
 * A NAMED PLACE A CANVAS CAN BE SENT TO, and not every host has all three.
 *
 * DESIGN has anchored SECTIONS rather than scenes: it owns `document` (the live
 * system) and `guests` (the nesting proof) and has no token list at all. The
 * three names outlive the page that had all three — `/design-engine` answered
 * every one with `?scene=` and was deleted in wave 6 — because `tokens` is a
 * place a canvas COULD be sent to, and a host that cannot reach one must say so
 * rather than draw a chip that goes nowhere. Which is why this is a published
 * set rather than an assumption — see `adviceFor`'s `host` argument.
 */
export type AdviceScene = "document" | "tokens" | "guests";

/** Every scene there is. A host that can reach them all passes nothing. */
export const EVERY_SCENE: readonly AdviceScene[] = ["document", "tokens", "guests"];

/**
 * What the host mounting the advice can actually reach.
 *
 * THE CLASS THIS CLOSES: a control whose precondition lives one layer down
 * cannot know it is inert. `adviceFor` decided which chip to draw and the HOST
 * decided what the click did; when the two disagreed the chip rendered, took
 * the click and did nothing (DESIGN's `show`, todo d049a0fe; "Keep as is"
 * before it, fixed in 5b9bda21). The charter's remedy is "either lift the
 * condition to the control, or delete the control" — this is the condition
 * lifted: a host says which scenes it has and a chip it cannot honour is never
 * drawn.
 */
export interface AdviceHost {
  /** Omit for a host that can reach every scene — that is the old page. */
  scenes?: readonly AdviceScene[];
}

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
      scene: AdviceScene;
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
  host?: AdviceHost,
): Advice {
  const advice = writtenAdvice(flag, colours);
  if (!host?.scenes) return advice;
  /* A CHIP THE HOST CANNOT HONOUR IS NOT DRAWN. The note keeps its sentence and
     its own close — those cost the host nothing — and loses only the navigation
     it has nowhere to perform. */
  const reachable = new Set(host.scenes);
  const actions = advice.actions.filter(
    (action) => action.kind !== "show" || reachable.has(action.scene),
  );
  return actions.length === advice.actions.length ? advice : { ...advice, actions };
}

function writtenAdvice(
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
export function whereReads(flag: { where: string }): string {
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

/* ------------------------------------------------------------- the address */

/**
 * THE ENGINE'S NAME FOR A SIGNAL, AND THE BRAND'S — the only place they meet.
 *
 * The engine addresses a signal by its ROLE (`signals.error`) because a role is
 * what the rule is about; a brand authors the same colour under its HUE name
 * (`signals.red`) because that is what it picked. The pairs are the engine's own
 * (`schema.Signals.by_role`). This map used to live in
 * `ds-engine-codex/configuration-panel.tsx`, where only one of the two pages
 * could see it — so a `signals.error` finding reached DESIGN's Error field and
 * nothing at all on a page that looked the address up literally.
 */
export const WHERE_ALIASES: Record<string, string> = {
  "signals.error": "signals.red",
  "signals.success": "signals.green",
  "signals.info": "signals.blue",
  "signals.warning": "signals.pink",
};

/**
 * Every authored slot a `where` names, before aliasing.
 *
 * A `where` may name SEVERAL slots at once — `brand.on_light/on_dark` is ONE
 * finding about TWO fields, because "these two hold the same colour" is not a
 * statement about either one alone. Exact-matching the whole string sent that
 * flag to neither field: invisible in the rail while sitting in the payload.
 * The group prefix is carried from the first name onto the rest.
 */
export function slotsNamedBy(where: string): string[] {
  const parts = where.split("/");
  const head = parts[0] ?? "";
  const dot = head.lastIndexOf(".");
  const group = dot > 0 ? head.slice(0, dot) : "";
  return parts.map((part, i) =>
    i === 0 || !group || part.includes(".") ? part : `${group}.${part}`,
  );
}

/**
 * Every authored field path a flag reaches — the split AND the translation.
 *
 * The two pages each had half of this. The rail split `a/b` and never
 * translated, so it only ever matched on a PREFIX (`flagsUnder("signals")`) and
 * would have missed an exact `signals.red` lookup. The panel translated and
 * split, but its split dropped the group prefix handling into a narrower form.
 * One function answers for both now, and `signals.error` and `signals.red` are
 * the same address from either direction.
 */
export function addressesOf(flag: Flag): string[] {
  return slotsNamedBy(flag.where).map((slot) => WHERE_ALIASES[slot] ?? slot);
}

export interface PlacedFlags {
  /** Field path -> the findings that belong under it. */
  placed: Map<string, Flag[]>;
  /**
   * Findings whose `where` names no field the caller declared.
   *
   * NOT AN ERROR AND NEVER DROPPED. A page renders these in a list with their
   * address in front. The failure this exists to prevent is the one that made
   * the whole class: information that exists in the payload and reaches no
   * pixel, the day the engine learns an address a page has no control for.
   */
  unplaced: Flag[];
}

/**
 * Put every finding at the fields it names, and keep the ones that name none.
 *
 * `fields` is DECLARED by the caller rather than inferred, because "is there a
 * control for this address" is the question that decides whether a finding is
 * placed or listed — and a lookup that guessed would put a flag under a control
 * that does not write that value. A flag naming two fields is placed at both:
 * it is one finding about two values and hiding it under one of them is how
 * `brand.possibly-unadjusted` reached neither.
 */
export function placeFlags(flags: Flag[], fields: ReadonlySet<string>): PlacedFlags {
  const placed = new Map<string, Flag[]>();
  const unplaced: Flag[] = [];
  for (const flag of flags) {
    const addresses = addressesOf(flag).filter((address) => fields.has(address));
    if (!addresses.length) {
      unplaced.push(flag);
      continue;
    }
    for (const address of addresses) {
      placed.set(address, [...(placed.get(address) ?? []), flag]);
    }
  }
  return { placed, unplaced };
}

/* ------------------------------------------------------- the acknowledgements */

/**
 * THE STORE IS NAMED FOR THE KIT, NOT FOR THE PAGE.
 *
 * It was `okuro.design-engine.acknowledged`, which named the surface a reader
 * happened to be standing on. An acknowledgement is a statement about a FINDING
 * ON A KIT — "I have read this and I am keeping the value" — and it is just as
 * true on the other page. Keyed by the page, answering the same question twice
 * produced two answers.
 *
 * THE OLD KEY IS STILL READ, and that is deliberate rather than tidy: a reader
 * who dismissed a finding before this wave has an entry under the old name, and
 * silently forgetting it would re-raise a verdict they had already answered.
 * `readStore` merges both and every write goes to the new one, so the legacy key
 * drains without ever being written again.
 */
const STORE = "okuro.design-kit.acknowledged";

/** What `/design-engine` wrote before wave 3. Read, never written. */
const LEGACY_STORE = "okuro.design-engine.acknowledged";

function readOne(key: string): Record<string, true> {
  try {
    const raw = window.localStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function readStore(): Record<string, true> {
  return { ...readOne(LEGACY_STORE), ...readOne(STORE) };
}

export function acknowledgementKey(brandId: string, code: string): string {
  return `${brandId}:${code}`;
}

/**
 * Which findings this reader has said "keep as is" to, for this kit.
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

/* ------------------------------------------------------------ the jump */

/**
 * `Review` / `Fix it` — move to the control the finding is about.
 *
 * IT ADDRESSES WHAT A COMPONENT PUBLISHES, never a class name or a label, so it
 * works on either page: the note carries `data-flag`, and the field around it
 * carries `data-control` or is the `<label>` the control sits in. A page that
 * publishes neither still gets the scroll, which is the honest half.
 *
 * Returns whether it found anything, so a gate can tell "nothing to jump to"
 * apart from "jumped".
 */
export function focusFlaggedField(
  code: string,
  root: ParentNode = document,
): boolean {
  const note = root.querySelector(`[data-flag="${code}"]`);
  if (!note) return false;
  /* A CLOSED DISCLOSURE IS NOT A MISSING CONTROL, and it is the one thing that
     made this jump land nowhere. Both rails group their advanced fields in a
     collapsible section; a note inside a shut `<details>` is in the DOM, so the
     lookup finds it, and is not rendered, so neither the scroll nor the focus
     does anything — the action reads as broken while doing exactly what it was
     told. Every shut ancestor is opened first. */
  for (
    let box = note.parentElement?.closest("details");
    box;
    box = box.parentElement?.closest("details") ?? null
  ) {
    box.open = true;
  }
  note.scrollIntoView({ block: "center" });
  const field = note.closest("[data-control]") ?? note.closest("label");
  const control = field?.querySelector(
    "input,select,button,[role='slider']",
  ) as HTMLElement | undefined;
  control?.focus();
  return true;
}
