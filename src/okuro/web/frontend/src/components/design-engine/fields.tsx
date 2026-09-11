/**
 * The controls, and the one rule that makes them controls rather than inputs.
 *
 * EVERY FIELD HAS THE SAME FOUR PARTS, in this order and no other:
 *
 *     label            13/18, w500, SENTENCE CASE — not uppercase
 *     the control      32px tall, 6px radius, one of three heights on the page
 *     its resolved value   14/20 w500, full strength — what the engine answered
 *     one help slot    reserved height, calm / advice / error
 *
 * The value is on the row because anything a person needs in order to JUDGE the
 * configuration is permanently visible. A rail that showed only the controls
 * makes a reader open each one to find out what it currently says.
 *
 * VALIDATE ON BLUR, AND SHOW EVERY CHARACTER WHILE TYPING.
 *
 * Measured before: a bad hex was discarded by a regex guard inside `onChange`,
 * so a keystroke produced NOTHING — no character, no message, no state. The
 * field was uneditable and said nothing about why. `radius base = 0` went the
 * other way and was accepted silently, taking S/M/L to zero and printing hints
 * that read `x1 of 0`.
 *
 * So a text field here is UNCONTROLLED WHILE IT IS BEING TYPED IN. Every
 * character appears. On blur the value is validated: valid commits, invalid
 * raises a ring, a halo and a sentence 8px under the field — never a border
 * swap, because a swap changes the box and the layout jumps at the exact moment
 * a reader needs to read. Escape reverts.
 *
 * NO ENGINE CALL IS MADE FOR A REJECTED VALUE. `onChange` fires only after the
 * validator passes, which is what makes "the value does not enter the brand" a
 * property of this file rather than a hope about the resolver.
 */

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Segmented } from "@/components/ui/segmented";
import { HelpSlot } from "./advice";
import { factorOf, size } from "./scale";
import type { AdviceAction } from "./severity";
import type { BrandColourModel, Flag } from "./types";

/* --------------------------------------------------------------- the shell */

export interface FieldCommon {
  label: string;
  /** What this setting does, in one sentence. The help slot's calm state. */
  calm?: ReactNode;
  flags?: Flag[];
  colours?: BrandColourModel | null;
  onAction?: (action: AdviceAction, flag: Flag) => void;
  /** Open the inspector on this slot — "why is this value what it is". */
  onReveal?: () => void;
}

/**
 * One control row. The label, the control, its value and exactly one help slot.
 *
 * `data-control` is what the gate counts, and the invariant it checks is that
 * every one of them has exactly one `[data-help]` child whose height does not
 * change between states.
 */
export function Field({
  label,
  calm,
  flags,
  colours,
  error,
  onAction,
  onReveal,
  value,
  children,
}: FieldCommon & {
  error?: string | null;
  /** The resolved value, as a reader reads it. */
  value?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div data-control={label} className="ce-stack-tight">
      <div className="ce-row" style={{ gap: 8 }}>
        {onReveal ? (
          <button
            type="button"
            className="ce-label"
            style={{
              background: "transparent",
              border: 0,
              padding: 0,
              minHeight: 24,
              color: "var(--ce-fg)",
              cursor: "pointer",
              textAlign: "left",
            }}
            onClick={onReveal}
          >
            {label}
          </button>
        ) : (
          <span className="ce-label">{label}</span>
        )}
        {value != null && (
          <span className="ce-value" style={{ marginLeft: "auto" }} data-value>
            {value}
          </span>
        )}
      </div>
      {children}
      <HelpSlot
        calm={calm}
        flags={flags}
        error={error}
        colours={colours}
        onAction={onAction}
      />
    </div>
  );
}

/* ------------------------------------------------------- blur-validated text */

/**
 * A text field that shows what you typed and judges it when you leave.
 *
 * The draft is local. The authoritative value only overwrites it when it moves
 * for a reason OTHER than this field — opening another kit, an `[Apply both]`
 * elsewhere — which is the same discipline the shade slider needed, and for the
 * same reason: the answer arrives back through a debounced round trip.
 */
function useDraft(value: string) {
  const [draft, setDraft] = useState(value);
  const [error, setError] = useState<string | null>(null);
  const sent = useRef(value);

  useEffect(() => {
    if (value !== sent.current) {
      sent.current = value;
      setDraft(value);
      setError(null);
    }
  }, [value]);

  return { draft, setDraft, error, setError, sent };
}

const HEX = /^#?[0-9a-fA-F]{6}$/;

export function ColourField({
  label,
  value,
  calm,
  flags,
  colours,
  onAction,
  onChange,
  onReveal,
  beside,
}: FieldCommon & {
  value: string;
  /** Part of the SAME decision, on the control's own row. See the mode switch. */
  beside?: ReactNode;
  onChange: (next: string) => void;
}) {
  const { draft, setDraft, error, setError, sent } = useDraft(value);

  const commit = () => {
    const next = draft.trim();
    if (!HEX.test(next)) {
      // NAMES THE FIX, NOT THE FAULT, and shows the shape by example.
      setError("Six hex digits, like #f03541.");
      return;
    }
    setError(null);
    const normalised = next.startsWith("#") ? next : `#${next}`;
    sent.current = normalised;
    setDraft(normalised);
    if (normalised !== value) onChange(normalised);
  };

  return (
    <Field
      label={label}
      calm={calm}
      flags={flags}
      colours={colours}
      error={error}
      onAction={onAction}
      onReveal={onReveal}
      value={<span style={{ fontFamily: "var(--font-mono)" }}>{value}</span>}
    >
      <div className="ce-row">
        <Input
          type="color"
          aria-label={`${label} colour`}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="ce-control shrink-0"
          style={{ width: 40, padding: 0, cursor: "pointer" }}
        />
        <Input
          type="text"
          aria-label={label}
          aria-invalid={error ? true : undefined}
          value={draft}
          spellCheck={false}
          /* `min-w-0` so the hex shrinks rather than pushing whatever shares its
             row past the 271px column — a flex child's default `min-width:auto`
             is its content, which is how a 40px swatch, a hex and a switch came
             to measure 281. */
          className="ce-control min-w-0 flex-1"
          style={{ fontFamily: "var(--font-mono)" }}
          onChange={(e) => {
            // EVERY CHARACTER APPEARS. No guard here — the guard was the defect.
            setDraft(e.target.value);
            if (error) setError(null);
          }}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            if (e.key === "Escape") {
              setDraft(value);
              setError(null);
            }
          }}
        />
        {beside}
      </div>
    </Field>
  );
}

/* ---------------------------------------------------------------- a number */

export function NumberField({
  label,
  value,
  step = 1,
  min,
  max,
  unit,
  calm,
  flags,
  colours,
  invalidMessage,
  onAction,
  onChange,
  onReveal,
  beside,
}: FieldCommon & {
  value: number;
  step?: number;
  min?: number;
  max?: number;
  unit?: string;
  /** What to say when the value is refused. Names the fix, per the copy rules. */
  invalidMessage?: string;
  /** Fields that belong to the SAME decision, on the same row. See `Radius`. */
  beside?: ReactNode;
  onChange: (next: number) => void;
}) {
  const { draft, setDraft, error, setError, sent } = useDraft(String(value));

  const commit = () => {
    const next = Number(draft);
    const bad =
      !Number.isFinite(next) ||
      (min != null && next < min) ||
      (max != null && next > max);
    if (bad) {
      setError(
        invalidMessage ??
          `A number${min != null ? `, at least ${min}` : ""}${
            max != null ? `, at most ${max}` : ""
          }.`,
      );
      return;
    }
    setError(null);
    sent.current = String(next);
    setDraft(String(next));
    if (next !== value) onChange(next);
  };

  return (
    <Field
      label={label}
      calm={calm}
      flags={flags}
      colours={colours}
      error={error}
      onAction={onAction}
      onReveal={onReveal}
      value={
        <span style={{ fontFamily: "var(--font-mono)" }}>
          {value}
          {unit ? ` ${unit}` : ""}
        </span>
      }
    >
      <div className="ce-row" style={{ gap: 16 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <Input
            type="number"
            aria-label={label}
            aria-invalid={error ? true : undefined}
            value={draft}
            step={step}
            className="ce-control w-full"
            style={{ fontFamily: "var(--font-mono)" }}
            onChange={(e) => {
              setDraft(e.target.value);
              if (error) setError(null);
            }}
            onBlur={commit}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
              if (e.key === "Escape") {
                setDraft(String(value));
                setError(null);
              }
            }}
          />
        </div>
        {beside}
      </div>
    </Field>
  );
}

/* ------------------------------------------------------- a size, px in / factor out */

/**
 * A size, typed in PIXELS and saved as a FACTOR — behaviour unchanged.
 *
 *     "The growth UI shows sizes in px because that is what a designer reads,
 *      but what a brand SAVES has to be the factor — otherwise changing the
 *      base later leaves the saved numbers behind, which is the exact failure
 *      the frozen grid had."
 *
 * It is a COMPONENT rather than a convention precisely so no size field on this
 * rail can quietly skip it. The only thing this redesign changed is when the
 * conversion fires: on blur, with the same validator as every other field.
 */
export function PxField({
  label,
  base,
  factor,
  calm,
  flags,
  colours,
  onAction,
  onChange,
  onReveal,
  bare,
  inline,
}: FieldCommon & {
  base: number;
  factor: number;
  onChange: (nextFactor: number) => void;
  /**
   * Label BESIDE the input rather than above it.
   *
   * For the members of a one-row decision: `S 4` reads as one thing at 32px,
   * where a stacked label costs 54 and turns the radius ladder into a second
   * row of its own.
   */
  inline?: boolean;
  /**
   * One field of a MULTI-FIELD decision, not a decision of its own.
   *
   * S / M / L are the radius ladder: one decision with four numbers, and the
   * group's single help slot speaks for all of them. A bare field carries no
   * `data-control`, so "every control owns exactly one help slot" stays true
   * rather than being weakened to accommodate a row.
   */
  bare?: boolean;
}) {
  const px = size(base, factor);
  const { draft, setDraft, error, setError, sent } = useDraft(
    String(Number(px.toFixed(4))),
  );

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

  const input = (
    <Input
        type="number"
        aria-label={label}
        aria-invalid={error ? true : undefined}
        value={draft}
        step={base / 8}
        min={0}
        className="ce-control w-full"
        style={{ fontFamily: "var(--font-mono)" }}
        onChange={(e) => {
          setDraft(e.target.value);
          if (error) setError(null);
        }}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          if (e.key === "Escape") {
            setDraft(String(Number(px.toFixed(4))));
            setError(null);
          }
        }}
      />
  );

  if (bare && inline) {
    return (
      <div className="ce-row" style={{ gap: 4 }}>
        <span className="ce-label" style={{ flex: "none" }}>
          {label}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>{input}</div>
      </div>
    );
  }

  if (bare) {
    return (
      <div className="ce-stack-tight" style={{ gap: 4 }}>
        <span className="ce-label">{label}</span>
        {input}
      </div>
    );
  }

  return (
    <Field
      label={label}
      calm={calm}
      flags={flags}
      colours={colours}
      error={error}
      onAction={onAction}
      onReveal={onReveal}
      value={
        <span style={{ fontFamily: "var(--font-mono)" }}>
          {Number(px.toFixed(4))} px
        </span>
      }
    >
      {input}
    </Field>
  );
}

/* ---------------------------------------------------------------- a choice */

export function SelectField({
  label,
  value,
  options,
  calm,
  flags,
  colours,
  onAction,
  onChange,
  onReveal,
  reads,
}: FieldCommon & {
  value: string;
  options: { value: string; label: string }[];
  /** The resolved value, when it reads differently from the option's label. */
  reads?: ReactNode;
  onChange: (next: string) => void;
}) {
  return (
    <Field
      label={label}
      calm={calm}
      flags={flags}
      colours={colours}
      onAction={onAction}
      onReveal={onReveal}
      value={reads}
    >
      {/* okuro's own Select, not a bare <select>: it carries the app's focus
          ring, its dark mode and its portal behaviour, and a test drives it the
          way a person does — open the trigger, pick the option by name. */}
      {/* THE VALUE TRUNCATES RATHER THAN RUNNING UNDER ITS OWN CHEVRON.
          Measured: `scrollWidth` 260 inside a 229px trigger, `overflow: hidden`
          and `text-overflow: clip` — so the label was cut mid-word with no
          ellipsis and the last glyphs sat beneath the arrow. `min-w-0` lets the
          flex child shrink and the child span carries the ellipsis; the
          vendored primitive is untouched. */}
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger
          aria-label={label}
          className="ce-control w-full min-w-0 [&>span:first-child]:min-w-0 [&>span:first-child]:overflow-hidden [&>span:first-child]:text-ellipsis [&>span:first-child]:whitespace-nowrap"
        >
          <SelectValue placeholder={label} />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </Field>
  );
}

export function SegmentedField({
  label,
  value,
  options,
  calm,
  flags,
  colours,
  onAction,
  onChange,
  onReveal,
}: FieldCommon & {
  value: string;
  options: { value: string; label: string }[];
  onChange: (next: string) => void;
}) {
  return (
    <Field
      label={label}
      calm={calm}
      flags={flags}
      colours={colours}
      onAction={onAction}
      onReveal={onReveal}
    >
      <Segmented ariaLabel={label} value={value} onChange={onChange} options={options} />
    </Field>
  );
}

/* ------------------------------------------------- several colours, one row */

/**
 * Two to four colours on ONE row, each with its hex under it.
 *
 * Neutrals and Signals are one DECISION each, not two and four. Splitting them
 * into six stacked colour fields is a third of the 137-control rail and the
 * reason the eight things that matter were unfindable.
 */
export function SwatchRow({
  label,
  entries,
  calm,
  flags,
  colours,
  onAction,
  onReveal,
  onChange,
  expanded,
  onExpand,
  renderExtra,
}: FieldCommon & {
  entries: { key: string; name: string; value: string }[];
  onChange: (key: string, next: string) => void;
  /** Open to one full ColourField per entry. Closed is the row. */
  expanded?: boolean;
  onExpand?: (next: boolean) => void;
  /**
   * Rendered under one entry's row while EXPANDED — the place a per-entry
   * decision that is not the colour itself belongs.
   *
   * It exists for the signal FOREGROUND override (his ruling of 2026-08-18):
   * that control is about one role, it needs the two contrast readouts beside
   * it, and it must not appear on the collapsed row, which is four swatches in a
   * 272px column. A render prop keeps `SwatchRow` generic — it still knows
   * nothing about signals.
   */
  renderExtra?: (key: string) => React.ReactNode;
}) {
  /* FOUR SWATCHES ON ONE ROW — the anatomy's own row 5, and it needs the row to
     actually hold four. Swatch + hex is ~72px a piece, so four of them wrapped
     to two rows in a 272px column: 64px of rail for one decision. Above two
     entries the collapsed row shows the SWATCHES and the resolved reading is
     the ROLE NAMES, in the label row, which is the value the anatomy names.
     Every hex is still one click away under `Edit each`. */
  const dense = entries.length > 2;

  return (
    <div data-control={label} className="ce-stack-tight">
      <div className="ce-row" style={{ gap: 8 }}>
        <span className="ce-label">{label}</span>
        {onExpand && (
          <button
            type="button"
            className="ce-quiet"
            style={{ marginLeft: "auto" }}
            aria-expanded={expanded}
            onClick={() => onExpand(!expanded)}
          >
            {expanded ? "Done" : "Edit each"}
          </button>
        )}
      </div>

      {expanded ? (
        <div className="ce-stack">
          {entries.map((entry) => (
            <div key={entry.key} className="ce-stack-tight">
              <div className="ce-row">
                <Input
                  type="color"
                  aria-label={`${entry.name} colour`}
                  value={entry.value}
                  onChange={(e) => onChange(entry.key, e.target.value)}
                  className="ce-control shrink-0"
                  style={{ width: 40, padding: 0, cursor: "pointer" }}
                />
                <span className="ce-body" style={{ minWidth: 88 }}>
                  {entry.name}
                </span>
                <span
                  className="ce-value"
                  style={{ marginLeft: "auto", fontFamily: "var(--font-mono)" }}
                >
                  {entry.value}
                </span>
              </div>
              {renderExtra?.(entry.key)}
            </div>
          ))}
        </div>
      ) : (
        <div className="ce-row" style={{ gap: dense ? 8 : 16 }}>
          {entries.map((entry) => (
            <button
              key={entry.key}
              type="button"
              className="ce-row"
              data-swatch={entry.key}
              aria-label={`${entry.name} ${entry.value}`}
              onClick={() => onReveal?.()}
              style={{
                gap: 4,
                background: "transparent",
                border: 0,
                padding: 0,
                minHeight: 24,
                minWidth: 24,
                color: "inherit",
                cursor: "pointer",
              }}
            >
              <span className="ce-swatch" style={{ background: entry.value }} />
              {/* TWO entries print their hex; FOUR do not, because four hexes
                  or even four role names are 281px in a 271px column and the
                  row wraps — 64px of rail for one decision, and the wrap is
                  what criterion 12 measures. The role and the hex are both on
                  each swatch's accessible name, and both are one click away
                  under `Edit each`. "Four swatches on one row" is the anatomy's
                  own word for this decision. */}
              {!dense && (
                <span
                  className="ce-micro ce-2"
                  style={{ fontFamily: "var(--font-mono)" }}
                >
                  {entry.value}
                </span>
              )}
            </button>
          ))}
        </div>
      )}

      <HelpSlot
        calm={calm}
        flags={flags}
        colours={colours}
        onAction={onAction}
      />
    </div>
  );
}
