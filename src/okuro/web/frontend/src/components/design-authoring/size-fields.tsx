/**
 * SHOW PX, SAVE FACTORS — the two authored size groups, page-neutral.
 *
 *     "The growth UI shows sizes in px because that is what a designer reads,
 *      but what a brand SAVES has to be the factor — otherwise changing the
 *      base later leaves the saved numbers behind, which is the exact failure
 *      the frozen grid had."
 *
 * Register rule 12 puts both groups in the authored set: the four BORDER
 * WEIGHTS (default 1/2/4/8 px, authored per brand) and the EFFECT INTENSITIES
 * (light / normal / strong / x-strong, each a quad of shadow offsets, spread and
 * blur). Every one of them is a FACTOR of the system base, never a pixel.
 *
 * ── WAVE 2, 2026-09-13 ─────────────────────────────────────────────────────
 * Both editors were inline in `components/design-engine/rail.tsx`. DESIGN had
 * border OPACITY and nothing else — no weights, no effects at all — so seven
 * authored numbers and every effect quad were reachable from one page only.
 *
 * THE WRITE IS WHOLE-VALUE, and that is a correctness fix rather than a style
 * choice. The old rail wrote `border.weight_factors.${i}` through a `setPath`
 * that special-cases arrays; DESIGN's `setPath` does not, and would have turned
 * the four-element tuple into an object keyed "0".."3" the first time it was
 * asked to write an index. A component that hands back the whole next array
 * cannot be miswired by either dialect.
 */

import { useEffect, useRef, useState } from "react";
import { Input } from "@/components/ui/input";
import { factorOf, size } from "@/lib/scale";
import type { AuthoringVocabulary } from "./vocabulary";

/**
 * A size field: the reader types PIXELS, the brand stores the FACTOR.
 *
 * The conversion fires on blur, with the same validator every other size field
 * uses. The draft is local so every character appears; the authoritative value
 * overwrites it only when it moves for a reason OTHER than this field — opening
 * another kit, an edit made elsewhere — which is how a debounced round trip
 * stops snapping the input back between keystrokes.
 */
export function FactorField({
  label,
  base,
  factor,
  vocabulary: v,
  onChange,
}: {
  label: string;
  base: number;
  factor: number;
  vocabulary: AuthoringVocabulary;
  onChange: (nextFactor: number) => void;
}) {
  const px = size(base, factor);
  const shown = String(Number(px.toFixed(4)));
  const [draft, setDraft] = useState(shown);
  const [error, setError] = useState<string | null>(null);
  const sent = useRef(shown);

  useEffect(() => {
    if (shown !== sent.current) {
      sent.current = shown;
      setDraft(shown);
      setError(null);
    }
  }, [shown]);

  const commit = () => {
    const next = Number(draft);
    if (!Number.isFinite(next) || next < 0) {
      setError("A size is a number of pixels, never below zero.");
      return;
    }
    setError(null);
    sent.current = String(next);
    setDraft(String(next));
    if (next !== px) onChange(factorOf(base, next));
  };

  return (
    <div
      className={v.stackTight}
      style={{ gap: 4 }}
      data-factor-field={label}
      /* THE STORED VALUE, PUBLISHED. A gate asking "does this field show px and
         save a factor" would otherwise have to parse a label or trust the
         model; both numbers are readable here, from the element itself. */
      data-factor={factor}
      data-px={Number(px.toFixed(4))}
    >
      <span className={v.label}>{label}</span>
      <Input
        type="number"
        aria-label={label}
        aria-invalid={error ? true : undefined}
        value={draft}
        step={base / 8}
        min={0}
        className={`${v.control} w-full`}
        style={{ fontFamily: "var(--font-mono)" }}
        onChange={(e) => {
          setDraft(e.target.value);
          if (error) setError(null);
        }}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          if (e.key === "Escape") {
            setDraft(shown);
            setError(null);
          }
        }}
      />
    </div>
  );
}

/**
 * The four border weights — register rule 12's "border weights (default 1/2/4/8
 * — authored per brand)".
 *
 * They are a LADDER of one decision, not four decisions, which is why they share
 * one section and why the whole array travels back on every edit.
 */
export function BorderWeightFactors({
  factors,
  base,
  vocabulary: v,
  onChange,
}: {
  factors: readonly number[];
  base: number;
  vocabulary: AuthoringVocabulary;
  onChange: (next: number[]) => void;
}) {
  return (
    <div className={v.stackTight} data-section="border-weights">
      {[0, 1, 2, 3].map((i) => (
        <FactorField
          key={i}
          label={`Weight ${i + 1}`}
          base={base}
          factor={factors[i] ?? 0}
          vocabulary={v}
          onChange={(next) => {
            const copy = [0, 1, 2, 3].map((j) => factors[j] ?? 0);
            copy[i] = next;
            onChange(copy);
          }}
        />
      ))}
    </div>
  );
}

/**
 * The effect intensities — register rule 12's "effect intensities (light /
 * normal / strong / x-strong: shadow-x/y/spread + blur)".
 *
 * THE NAMES COME FROM THE BRAND, not from a list typed here. A brand that grows
 * a fifth intensity, or an intensity that grows a fifth field, renders without
 * this file changing — and an editor that enumerated them would silently stop
 * showing whatever the engine learned next.
 */
export function EffectIntensities({
  effects,
  base,
  vocabulary: v,
  onChange,
}: {
  effects: Record<string, Record<string, number>>;
  base: number;
  vocabulary: AuthoringVocabulary;
  onChange: (next: Record<string, Record<string, number>>) => void;
}) {
  return (
    <div className={v.stack} data-section="effects">
      {Object.entries(effects).map(([name, intensity]) => (
        <div key={name} className={v.stackTight} data-effect={name}>
          <span className={v.label}>{name}</span>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
              gap: 16,
            }}
          >
            {Object.entries(intensity).map(([field, factor]) => (
              <FactorField
                key={field}
                label={field.replace(/_/g, " ")}
                base={base}
                factor={factor}
                vocabulary={v}
                onChange={(next) =>
                  onChange({
                    ...effects,
                    [name]: { ...intensity, [field]: next },
                  })
                }
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
