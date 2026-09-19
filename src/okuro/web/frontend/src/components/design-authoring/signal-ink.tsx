/**
 * ONE SIGNAL'S INK — computed, or authored, with both numbers visible.
 *
 * His ruling of 2026-08-18 (register rule 7): the override is
 * "flagged-not-rejected with the contrast readout shown ... surface WCAG numbers
 * for both candidates next to the override control so the decision is
 * informed". So this is not a colour picker: rule 13 allows exactly two inks —
 * the brand's own black and its own white — and the choice between them is what
 * the control offers, each with the ratio it actually achieves on that signal.
 *
 * `Computed` is a real option rather than a reset link, because "accept what the
 * system says" is the default and a reader should be able to see it selected.
 * The engine, not this file, decides which of the two is the computed one.
 *
 * EVERY NUMBER IS READ, NONE IS COMPUTED. `api.py::_signal_inks` ships
 * `computed`, `alternative`, both contrasts and `aa` per role, precisely so a
 * rail never measures a ratio of its own. A projection that computes is a second
 * authority — and here the two authorities would disagree exactly where the
 * override matters, which is the case a reader is looking at.
 *
 * ── WAVE 2, 2026-09-13 ─────────────────────────────────────────────────────
 * It lived inside `components/design-engine/rail.tsx`, so `/design-engine` was
 * the only surface where a brand could state this exception at all: DESIGN had
 * no control, and register rule 7 is a RULED capability. Moved here whole; the
 * old rail imports it back and its DOM is unchanged, class names included,
 * because the class it paints is now handed in rather than typed.
 */

import type { AuthoringVocabulary } from "./vocabulary";
import type { BrandColourModel, BrandJson } from "@/components/design-engine/types";

/**
 * slot -> role, because the override is keyed by ROLE and the colour by slot.
 *
 * The engine addresses a signal by its ROLE — a role is what the rule is about;
 * a brand authors the same colour under its HUE name, because that is what it
 * picked. The pairs are the engine's own (`schema.Signals.by_role`).
 */
export const SIGNAL_ROLE_OF: Record<string, string> = {
  red: "error",
  green: "success",
  blue: "info",
  pink: "warning",
};

/**
 * The four signals: the AUTHORED slot name, and the role it plays.
 *
 * `as const` rather than a widened annotation, because the slot names ARE the
 * four keys of `BrandJson["signals"]` and a caller reading `brand.signals[slot]`
 * should be told so by the compiler rather than casting.
 */
export const SIGNAL_ROLES = [
  ["red", "Error"],
  ["green", "Success"],
  ["blue", "Info"],
  ["pink", "Warning"],
] as const;

/**
 * The one write for a signal foreground override, on either rail.
 *
 * `null` removes the role's entry, and an EMPTY map becomes `null` rather than
 * `{}` — absent is what "compute it" means in the schema, and a brand storing an
 * empty override map is a brand claiming an exception it did not make.
 */
export function setSignalForeground(
  brand: BrandJson,
  role: string,
  value: string | null,
): BrandJson {
  const next = { ...(brand.signals.foregrounds ?? {}) };
  if (value === null) delete next[role];
  else next[role] = value;
  return {
    ...brand,
    signals: {
      ...brand.signals,
      foregrounds: Object.keys(next).length ? next : null,
    },
  };
}

export function SignalInkChoice({
  role,
  ink,
  vocabulary: v,
  onChange,
}: {
  role: string;
  ink: NonNullable<BrandColourModel["signals"]>[string];
  vocabulary: AuthoringVocabulary;
  onChange: (value: string | null) => void;
}) {
  const options: { value: string | null; label: string; contrast: number }[] = [
    { value: null, label: "Computed", contrast: ink.computed_contrast },
    {
      value: ink.alternative,
      label: ink.alternative.toLowerCase() === ink.computed.toLowerCase()
        ? "Same"
        : "Override",
      contrast: ink.alternative_contrast,
    },
  ];

  return (
    <div
      className={v.row}
      data-signal-foreground={role}
      role="radiogroup"
      aria-label={`${role} foreground`}
      style={{ gap: 8, flexWrap: "wrap" }}
    >
      {options.map((option) => {
        const active =
          option.value === null ? ink.authored === null : ink.authored === option.value;
        const passes = option.contrast >= ink.aa;
        return (
          <button
            key={option.label}
            type="button"
            role="radio"
            className={v.chip}
            aria-checked={active}
            data-ink={option.value ?? "computed"}
            /* THE NUMBER IS THE LABEL, and it is published as an attribute too,
               so a gate can assert it equals what the MODEL says rather than
               parsing a sentence or pinning a literal. */
            data-contrast={option.contrast}
            onClick={() => onChange(option.value)}
          >
            <span
              aria-hidden
              className={v.swatch}
              style={{
                background: ink.background,
                color: option.value ?? ink.computed,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 10,
              }}
            >
              Aa
            </span>
            {option.label} · {option.contrast.toFixed(2)} : 1
            {/* NOT COLOUR ALONE, per the in-line-alert rule: the glyph carries
                pass/fail as well as the number. */}
            <span aria-hidden>{passes ? "✓" : "▲"}</span>
          </button>
        );
      })}
      <span className={`${v.micro} ${v.dim}`} data-signal-aa={ink.aa}>
        AA needs {ink.aa} : 1
      </span>
    </div>
  );
}
