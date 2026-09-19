/**
 * THE ADDITIONAL COLOURS: add, remove, recolour, label.
 *
 * His ruling of 2026-08-17: "NEW AUTHORED SECTION: BRAND ADDITIONAL COLOURS —
 * up to 10, add/remove. Each gets the SAME foreground calculation as everything
 * else. Freely usable in the interface; intended uses incl. presentation slides,
 * charts." Register rule 12 carries it as part of the authored set.
 *
 * The swatch renders the PAIR — the colour with the engine's own ink on it —
 * because the pair is what a consumer receives. Beside it sits the EMITTED name,
 * permanently, so nothing implies that renaming the label repoints a `var()`:
 * the emitted names are positional and a label is for the eye.
 *
 * NOTHING HERE COMPUTES A FOREGROUND. `resolved[i].foreground` is the engine's
 * answer for that colour, by the same threshold everything else uses. A page
 * that picked its own ink would be a second implementation of the one
 * measurement the system takes.
 *
 * ── WAVE 2, 2026-09-13 ─────────────────────────────────────────────────────
 * It lived inside `components/design-engine/rail.tsx` and DESIGN had no editor
 * at all, so ten authored slots of the register's rule 12 were reachable from
 * one page only. Moved here whole; the old rail imports it back and its DOM is
 * unchanged, because the classes it paints are handed in rather than typed.
 */

import type { AuthoringVocabulary } from "./vocabulary";
import type { AdditionalColour } from "@/components/design-engine/types";

export function AdditionalColours({
  entries,
  resolved,
  max,
  vocabulary: v,
  onChange,
}: {
  entries: { value: string; label: string }[];
  /** The engine's answer per entry: the emitted name and the computed ink. */
  resolved: AdditionalColour[];
  max: number;
  vocabulary: AuthoringVocabulary;
  onChange: (next: { value: string; label: string }[]) => void;
}) {
  const patch = (index: number, part: Partial<{ value: string; label: string }>) =>
    onChange(entries.map((entry, i) => (i === index ? { ...entry, ...part } : entry)));

  return (
    <div className={v.stack} data-section="additional">
      {entries.length === 0 && (
        <p className={`${v.body} ${v.dim}`}>
          None yet. Add one and it joins the emitted vocabulary as
          {" --additional-1 "}
          with its own computed foreground — free for slides, charts, anything.
        </p>
      )}

      {entries.map((entry, index) => {
        const answer = resolved[index];
        const emitted = answer?.name ?? `additional-${index + 1}`;
        return (
          <div key={index} data-additional={index + 1} className={v.stackTight}>
            <div className={v.row}>
              <span
                aria-hidden
                data-swatch={emitted}
                className={v.swatch}
                style={{
                  background: entry.value,
                  color: answer?.foreground ?? undefined,
                  display: "inline-flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 10,
                }}
              >
                Aa
              </span>
              <input
                type="color"
                aria-label={`additional ${index + 1} colour`}
                value={entry.value}
                onChange={(e) => patch(index, { value: e.target.value })}
                className={v.control}
                style={{ width: 40, padding: 0, cursor: "pointer" }}
              />
              <input
                aria-label={`additional ${index + 1} label`}
                placeholder={`additional-${index + 1}`}
                value={entry.label}
                onChange={(e) => patch(index, { label: e.target.value })}
                className={v.control}
                style={{ flex: 1, minWidth: 0, fontFamily: "var(--font-mono)" }}
              />
              <button
                type="button"
                className={v.quiet}
                aria-label={`remove additional ${index + 1}`}
                onClick={() => onChange(entries.filter((_, i) => i !== index))}
              >
                ×
              </button>
            </div>
            <span className={`${v.micro} ${v.dim}`} style={{ fontFamily: "var(--font-mono)" }}>
              {entry.value} · emitted as --{emitted}
              {answer ? ` · ink ${answer.foreground}` : ""}
            </span>
          </div>
        );
      })}

      <div className={v.row}>
        <button
          type="button"
          className={v.chip}
          data-testid="add-additional-colour"
          disabled={entries.length >= max}
          onClick={() => onChange([...entries, { value: "#888888", label: "" }])}
        >
          Add a colour
        </button>
        <span className={`${v.micro} ${v.dim}`}>
          {entries.length} of {max}
        </span>
      </div>
    </div>
  );
}
