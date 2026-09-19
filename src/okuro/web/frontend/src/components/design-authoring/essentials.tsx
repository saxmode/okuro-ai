/**
 * The essentials gate — the two things a builder actually arrives with.
 *
 * A typeface and a colour. Everything else in the authored set has a defensible
 * default, so this is not a shortcut around authoring: it is the shortest input
 * that produces a COMPLETE and VALID brand, which the rail then refines slot by
 * slot.
 *
 * THIS SURFACE WAS ALREADY THE BEST ONE ON THE PAGE — legible type, real
 * spacing, one promise, one primary button — so what follows is ALIGNMENT, not
 * a rewrite: the same shape, re-expressed in the chrome contract so it stops
 * being the exception and becomes the model.
 *
 * Nothing here computes. The colour's lightness and the proposal it produces
 * come from the engine, so what the builder reads on this screen is what the
 * resolver will do.
 *
 * IT MOVED HERE IN WAVE 5, page-neutral, and paints with the class names its
 * HOST hands in (convention 786f04cd). It named `.ce-*` in nine places, which
 * is `/design-engine`'s chrome; a page's chrome may never be typed inside this
 * folder. What it still publishes unchanged is `data-control`, `data-help`,
 * `data-state` and `data-testid` — those are what a gate reads and what each
 * page's own stylesheet answers, the same mechanism `data-severity` uses.
 */

import { useMemo, useState } from "react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { EssentialsResult, FontFamily } from "@/components/design-engine/types";
import type { AuthoringVocabulary } from "./vocabulary";

const SUGGESTED = ["JetBrains Mono", "Geist Mono", "Inter", "Lato", "DejaVu Sans"];

export function EssentialsGate({
  families,
  busy,
  error,
  onGrow,
  vocabulary,
  seed,
}: {
  families: FontFamily[];
  busy: boolean;
  error: string | null;
  onGrow: (id: string, family: string, colour: string) => void;
  /** The host page's chrome. REQUIRED — a default would be one page's look
      pretending to be neutral, and `tsc` is what makes that unmissable. */
  vocabulary: AuthoringVocabulary;
  /**
   * A prefilled starting point, when the sheet was opened for a REASON.
   *
   * GUESTS on a fresh install has one brand and cannot demonstrate two meeting,
   * so its empty state opens this sheet already holding a contrasting colour —
   * the demonstration needs a guest that goes a different way from the host,
   * and a builder should not have to know that before they can see it.
   */
  seed?: { id: string; colour: string };
}) {
  const v = vocabulary;
  const [colour, setColour] = useState(seed?.colour ?? "#3BA0FF");
  const [family, setFamily] = useState("");
  const [id, setId] = useState(seed?.id ?? "new-brand");

  // Families this machine really has, most useful first — in SUGGESTED's own
  // order, not the alphabetical order the API returns. Filtering by a set
  // preserved the alphabet and put DejaVu Sans at the top, so the default pick
  // was a family that cannot fill four weight slots; the first thing a builder
  // saw was the engine apologising for their typeface.
  const ordered = useMemo(() => {
    const head = SUGGESTED.map((name) =>
      families.find((f) => f.family === name),
    ).filter((f): f is FontFamily => Boolean(f));
    const known = new Set(head.map((f) => f.family));
    return [...head, ...families.filter((f) => !known.has(f.family))];
  }, [families]);

  const chosen = family || ordered[0]?.family || "JetBrains Mono";
  const chosenFamily = families.find((f) => f.family === chosen);
  const quadShort = chosenFamily?.quad?.length === 4;

  return (
    <div className={v.groups} style={{ paddingTop: 16 }}>
      <section className={v.stackTight}>
        <h2 className={v.title}>Start with the essentials.</h2>
        <p className={`${v.body} ${v.dim}`}>
          A typeface and one brand colour. Everything else — grounds,
          foregrounds, both ladders, every border, every state, every size —
          derives from these two, and you will watch it happen.
        </p>
      </section>

      <section className={v.stack}>
        <div data-control="Brand name" className={v.stackTight}>
          <label htmlFor="brand-name" className={v.label}>
            Brand name
          </label>
          <input
            id="brand-name"
            aria-label="brand name"
            value={id}
            className={v.control}
            style={{ fontFamily: "var(--font-mono)", width: "100%" }}
            onChange={(e) =>
              setId(
                e.target.value
                  .toLowerCase()
                  .replace(/[^a-z0-9]+/g, "-")
                  .replace(/^-+|-+$/g, ""),
              )
            }
          />
          <span data-help data-state="calm">
            Lower case, hyphens. It becomes the kit's file name and the prefix on
            every emitted variable.
          </span>
        </div>

        <div data-control="Typeface" className={v.stackTight}>
          <span className={v.label}>Typeface</span>
          {/* A LOADING SELECT SAYS SO. An empty dropdown was the first thing a
              builder met while the font scan was in flight, and an empty
              dropdown reads as a broken page rather than a slow one. */}
          <Select value={chosen} onValueChange={setFamily} disabled={!families.length}>
            <SelectTrigger aria-label="typeface" className={`${v.control} w-full`}>
              <SelectValue
                placeholder={
                  families.length ? "pick a typeface" : "reading this machine's fonts…"
                }
              />
            </SelectTrigger>
            <SelectContent>
              {ordered.map((f) => (
                <SelectItem key={f.family} value={f.family}>
                  {f.family} · {Object.keys(f.faces).length} faces
                  {f.quad.length === 4 ? "" : " (too few for four slots)"}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <span data-help data-state="calm">
            {chosenFamily
              ? quadShort
                ? `The four weight slots would take ${chosenFamily.quad.join(" / ")}.`
                : "This family cannot fill four distinct weight slots on this machine."
              : "Only families installed here are offered — a slot has to name a face that really exists."}
          </span>
        </div>

        <div data-control="Brand colour" className={v.stackTight}>
          <span className={v.label}>Brand colour</span>
          <div className={v.row}>
            <input
              type="color"
              aria-label="brand colour"
              value={colour}
              className={v.control}
              style={{ width: 48, padding: 0, cursor: "pointer" }}
              onChange={(e) => setColour(e.target.value)}
            />
            <input
              aria-label="brand colour hex"
              value={colour}
              className={v.control}
              style={{ flex: 1, minWidth: 0, fontFamily: "var(--font-mono)" }}
              onChange={(e) => {
                const next = e.target.value.trim();
                if (/^#?[0-9a-fA-F]{6}$/.test(next)) {
                  setColour(next.startsWith("#") ? next : `#${next}`);
                }
              }}
            />
          </div>
          <span data-help data-state={error ? "error" : "calm"}>
            {error ??
              "One colour. on-light and on-dark come back as SHADES of it — the rail adjusts them, and a brand that genuinely has two toggles into that mode there."}
          </span>
        </div>
      </section>

      <section>
        <button
          type="button"
          className={v.primary}
          disabled={busy || !id}
          onClick={() => onGrow(id, chosen, colour)}
          data-testid="grow-the-system"
        >
          {busy ? "Deriving…" : "Grow the system"}
        </button>
      </section>
    </div>
  );
}

/**
 * What the funnel decided, and why.
 *
 * It no longer sits in the rail after a growth run. The verdict band states the
 * outcome, and the two adaptations it reports are the resolved values the rail's
 * own two shade controls already show — a second copy of them in a box above
 * the controls was one of the six renderings of three findings.
 */
export function essentialsSummary(result: EssentialsResult): string {
  const parts = [result.on_light, result.on_dark].map(
    (a) =>
      `on-${a.pole} ${a.value}${a.amount > 0 ? ` at ${Math.round(a.amount * 100)} %` : ""}`,
  );
  return `${parts.join(" · ")} · ${result.weight_note}`;
}
