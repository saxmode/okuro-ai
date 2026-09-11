import type { ReactNode } from "react";
import { useRef } from "react";
import { cn } from "@/lib/utils";

export interface SegmentedOption<V extends string | null = string> {
  label: string;
  value: V;
  count?: number;
  icon?: ReactNode;
  /**
   * An option the control OFFERS and cannot honour.
   *
   * It exists because hiding such an option is worse: /design-construction
   * offered nine rung tabs against seven `data-rung` blocks, and the two that
   * silently resolved the default rung looked like a bug in the emitter. A
   * disabled option with a stated `reason` says which two and why.
   */
  disabled?: boolean;
  /** Why this option cannot be picked. Announced and shown on hover. */
  reason?: string;
}

interface SegmentedProps<V extends string | null = string> {
  options: ReadonlyArray<SegmentedOption<V>>;
  value: V;
  onChange: (next: V) => void;
  /** Pass-through for the outer wrapper. */
  className?: string;
  /**
   * Visual variant:
   *  - `pills` (default): rounded capsule row, active = accent bg
   *  - `ghost`: no outer chrome, each option is a ghost pill
   */
  variant?: "pills" | "ghost";
  /** Accessible label describing what the segmented control toggles. */
  ariaLabel?: string;
  /**
   * The id of the VISIBLE label, when the control has one on screen.
   *
   * Preferred over `ariaLabel` wherever a label is already rendered: an
   * `aria-label` of "Theme mode" beside a visible "theme" tells a screen-reader
   * user and a sighted user two different names for one control, which is the
   * defect M18 names. Pointing at the label that is already there cannot drift.
   */
  ariaLabelledBy?: string;
  /** The id of the hint that describes this control. `aria-describedby`. */
  describedBy?: string;
  /** The group holds a value the server refused. `aria-invalid` on the group. */
  invalid?: boolean;
}

/**
 * Segmented one-of-N control.
 *
 * Replaces the ad-hoc "one active pill, rest are plain text links"
 * patterns on /work, /models, /stack, /reminders (each had a different
 * look). Single primitive, token-driven colors, keyboard-traversable.
 *
 * IT IS A RADIO GROUP, NOT A ROW OF TOGGLE BUTTONS. `aria-pressed` on
 * mutually-exclusive options tells a screen reader N independent toggles, and
 * a plain button row makes every option its OWN tab stop — measured on
 * /design-construction as ~75 stops between the page top and the rung axis,
 * because seventy rail controls and nine rung tabs all sat in the sequence.
 * The WAI-ARIA APG pattern is a radio group with a ROVING TABINDEX: the group
 * is one stop, and the arrows move within it. That is the fix for the tab
 * path, and it is a fix in the primitive rather than in any one page.
 *
 * ARROWS MOVE THE SELECTION, not just the focus. That is the APG behaviour for
 * a radio group whose options are all in the DOM, and it is what makes a
 * keyboard sweep of eight theme modes cost eight keystrokes rather than
 * sixteen. Disabled options are skipped rather than selected-and-refused.
 */
export function Segmented<V extends string | null = string>({
  options,
  value,
  onChange,
  className,
  variant = "pills",
  ariaLabel,
  ariaLabelledBy,
  describedBy,
  invalid,
}: SegmentedProps<V>) {
  const wrapperCls =
    variant === "pills"
      ? // `flex-wrap` because a group can be wider than the column it is given:
        // eight theme modes in a 400px cell overflowed the cell and printed
        // itself over the badge beside it. The ghost variant already wrapped.
        //
        // `--ds-corner-gap` is the DISTANCE this container puts between its own
        // edge and a pill: `p-0.5` plus the 1px `border`. `rounded-md` publishes
        // the corner itself (globals.css), so the pill's `rounded-inner` resolves
        // to `outer − distance` with no reference back to this element. The gap
        // is declared here rather than derived because a border-width is not
        // readable out of a computed style into a custom property, and the
        // padding and the border are both authored on this one line.
        "inline-flex flex-wrap items-center gap-0.5 rounded-md border border-border-subtle bg-surface-subtle p-0.5 [--ds-corner-gap:calc(var(--spacing)*0.5+1px)]"
      : // The ghost variant is a row of loose chips: no container corner, so it
        // publishes nothing and each chip keeps its own.
        "inline-flex flex-wrap items-center gap-1";

  const refs = useRef<Array<HTMLButtonElement | null>>([]);

  const enabled = options
    .map((opt, index) => ({ opt, index }))
    .filter((entry) => !entry.opt.disabled);

  const activeIndex = options.findIndex((opt) => opt.value === value);

  /**
   * The ONE tab stop. Nothing selected — or a selection the caller no longer
   * offers — still has to be reachable, so the first enabled option holds it.
   * Without this a group whose `value` matches nothing is unreachable by
   * keyboard entirely, which is how a roving tabindex usually breaks.
   */
  const stopIndex =
    activeIndex >= 0 && !options[activeIndex]?.disabled
      ? activeIndex
      : enabled[0]?.index ?? -1;

  const moveTo = (position: number) => {
    const entry = enabled[position];
    if (!entry) return;
    onChange(entry.opt.value);
    refs.current[entry.index]?.focus();
  };

  const onKeyDown = (event: React.KeyboardEvent, index: number) => {
    const here = enabled.findIndex((entry) => entry.index === index);
    if (here < 0) return;
    switch (event.key) {
      case "ArrowRight":
      case "ArrowDown":
        event.preventDefault();
        moveTo((here + 1) % enabled.length);
        break;
      case "ArrowLeft":
      case "ArrowUp":
        event.preventDefault();
        moveTo((here - 1 + enabled.length) % enabled.length);
        break;
      case "Home":
        event.preventDefault();
        moveTo(0);
        break;
      case "End":
        event.preventDefault();
        moveTo(enabled.length - 1);
        break;
      default:
        break;
    }
  };

  return (
    <div
      role="radiogroup"
      aria-label={ariaLabelledBy ? undefined : ariaLabel}
      aria-labelledby={ariaLabelledBy}
      aria-describedby={describedBy}
      aria-invalid={invalid || undefined}
      className={cn(wrapperCls, className)}
    >
      {options.map((opt, index) => {
        const active = opt.value === value;
        const disabled = Boolean(opt.disabled);
        const base =
          variant === "pills"
            ? cn(
                // `rounded-inner`, not `rounded`: the pill's corner is the
                // container's minus the distance between them (register P0 q3),
                // not Tailwind's 0.25rem literal, which no token could move.
                "rounded-inner px-3 py-1 text-xs font-medium transition-fast",
                // `bg-accent` in this codebase resolves to accent-SUBTLE
                // (12% opacity — shadcn convention), which made the
                // active pill look like an unpressed pill with inverse
                // text. Use `bg-primary` / `text-primary-foreground`
                // which map to the strong accent + inverse foreground
                // from the design tokens.
                active
                  ? "bg-primary text-primary-foreground"
                  : "text-fg-muted hover:bg-surface-elevated hover:text-fg",
              )
            : cn(
                "rounded-md px-2.5 py-1 text-xs font-medium transition-fast",
                // Same answer as the pills variant above, for the same reason:
                // a 10 % tint of the brand carries 10 % of its chroma, so an
                // active segment went grey as soon as the brand's on-light
                // adaptation was shaded toward its own black.
                active
                  ? "bg-primary text-primary-foreground"
                  : "text-fg-muted hover:bg-surface-elevated hover:text-fg",
              );
        return (
          <button
            key={String(opt.value)}
            ref={(node) => {
              refs.current[index] = node;
            }}
            type="button"
            role="radio"
            aria-checked={active}
            disabled={disabled}
            // The reason travels with the option in BOTH channels — the
            // accessible name for a screen reader, the tooltip for a pointer.
            // A disabled control with no stated reason is the defect this
            // replaces, not a smaller version of it.
            aria-label={opt.reason ? `${opt.label} — ${opt.reason}` : undefined}
            title={opt.reason}
            tabIndex={index === stopIndex ? 0 : -1}
            onClick={() => onChange(opt.value)}
            onKeyDown={(event) => onKeyDown(event, index)}
            className={cn(
              base,
              "inline-flex items-center gap-1.5",
              disabled &&
                "cursor-not-allowed opacity-50 hover:bg-transparent hover:text-fg-muted",
            )}
          >
            {opt.icon}
            <span>{opt.label}</span>
            {opt.count != null && (
              <span
                className={cn(
                  "tabular-nums",
                  active ? "opacity-80" : "text-fg-subtle",
                )}
              >
                {opt.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
