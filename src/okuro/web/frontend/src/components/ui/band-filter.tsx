import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Filter as FilterIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export interface BandFilterChip {
  /** Stable key — the facet's field, not its value. */
  key: string;
  /** What is switched on, in the viewer's words: `Staging`, `Signal`. */
  label: string;
}

interface BandFilterProps {
  /** The active facets, and the ONLY thing the collapsed item shows. */
  chips: ReadonlyArray<BandFilterChip>;
  /** Every filter element, revealed on the band's second row. */
  children: ReactNode;
  /** The item's own name. `Filter` unless a leaf has a better word. */
  label?: string;
  /** What the expanded group filters, for a screen reader. */
  ariaLabel: string;
}

/**
 * ONE ITEM THAT OPENS INTO A ROW — D7, the owner 2026-09-17.
 *
 * *"the filter groups COLLAPSE into ONE item labelled 'Filter'; on click it
 * takes the full width and shows all filter elements; on collapse (click
 * outside) only the ACTIVE filters stay visible."*
 *
 * ===========================================================================
 * WHY THIS IS A PRIMITIVE AND NOT A LINE IN `inbox.tsx`
 * ===========================================================================
 * Inbox is where it was measured — two segmented groups laying out 1085.2px of
 * controls into an 840px band, 245.2px of filters cut off with nothing on
 * screen saying so — but nothing about the defect is Inbox's. The band states
 * one size for controls every leaf builds at its own scale, so whether a given
 * leaf's row fits is a property of that leaf's contents, and every leaf is one
 * added filter away from being the next Inbox. Trimming Inbox's filters would
 * have been the instance fix and it was the original D7; collapsing them is the
 * class fix, and it is available to `/know/brain`, `/work/tasks` and the rest
 * the day one of them needs it.
 *
 * ===========================================================================
 * IT DOES NOT ASK THE BAND FOR A ROW, IT NAMES ITSELF AND THE BAND DECIDES
 * ===========================================================================
 * `data-band-row="full"` while open. `shell.css` carries the one rule that
 * reads it — `.c-band-row:has([data-band-row="full"]) .c-band-actions` — for
 * the same reason `c-band-pin` exists: a published node is rendered in the
 * plate's tree and cannot style the shell's own elements, so the leaf's half of
 * the contract is an attribute and the geometry stays the band's.
 *
 * Without it, whether an expanded filter got its own row would depend on how
 * long the leaf's TITLE happens to be, because the band's `flex-wrap` compares
 * the two. An expanded panel is wider than the band whatever the title says, so
 * it declares that rather than discovering it.
 *
 * ===========================================================================
 * THE OPEN STATE LIVES HERE, AND THAT IS DELIBERATE
 * ===========================================================================
 * The alternative is a `useState` in the leaf threaded through the
 * `useSectionTitle` memo — which would republish the whole slot on every open
 * and close. `PageTitle.tsx` records what lives in that path: a slot that
 * changes identity while its leaf is retiring is the case the owner stamp had
 * to be frozen at mount for. Keeping the disclosure internal means a toggle is
 * a local re-render and the published slot never moves.
 *
 * The leaf's FACETS still travel through the memo, as they must — they are the
 * leaf's state and the URL's. React reconciles this component by type and
 * position, so a facet click re-renders the item without remounting it and the
 * panel stays open, which is what "click OUTSIDE closes it" requires.
 */
export function BandFilter({
  chips,
  children,
  label = "Filter",
  ariaLabel,
}: BandFilterProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelId = useId();

  /* CLOSE ON OUTSIDE PRESS AND ON ESCAPE, and both listeners exist only while
     the panel is open — a closed filter costs the document nothing.

     `pointerdown` RATHER THAN `click`, because the band sits on a plate over a
     scroller: a press that starts outside and ends inside a moved layout never
     produces a `click` on anything the check would recognise. The capture phase
     is deliberate too, so a leaf's own handler cannot swallow the dismissal.

     FOCUS COMES BACK TO THE TRIGGER on Escape. The panel's controls are inside
     the element being removed, so without this the tab position is lost to the
     document root — the defect every hand-rolled disclosure has. */
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const root = rootRef.current;
      if (root && event.target instanceof Node && !root.contains(event.target)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onKeyDown, true);
    };
  }, [open]);

  return (
    <div
      ref={rootRef}
      className={cn("flex items-center gap-2", open && "w-full flex-wrap")}
      data-band-row={open ? "full" : undefined}
    >
      <button
        ref={triggerRef}
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((was) => !was)}
        /* `rounded-md`, `border-border-subtle`, `bg-surface-subtle` — the same
           three the segmented wrapper uses, so the collapsed item reads as one
           more control in the row rather than as a new kind of thing. The
           band's own rule sizes it to 24px tall with a 10px glyph; nothing here
           states a height. */
        className={cn(
          "inline-flex shrink-0 items-center gap-1.5 rounded-md border px-2.5",
          "text-xs font-medium transition-fast",
          open
            ? "border-transparent bg-primary text-primary-foreground"
            : "border-border-subtle bg-surface-subtle text-fg-muted hover:text-fg",
        )}
      >
        <FilterIcon aria-hidden="true" />
        <span>{label}</span>
        {/* THE COLLAPSED ITEM SHOWS THE ACTIVE FACETS AND NOTHING ELSE — the
            second half of D7. They are inside the button rather than beside it:
            the item is ONE control, and a chip that looked pressable but was
            not would be worse than a chip that plainly is not. Hidden while
            open, where the real controls say the same thing and say it
            truthfully. */}
        {!open &&
          chips.map((chip) => (
            <span
              key={chip.key}
              className="rounded-inner bg-primary/15 px-1.5 text-fg"
            >
              {chip.label}
            </span>
          ))}
        {!open && chips.length === 0 && (
          <span className="sr-only">no filters active</span>
        )}
      </button>
      {open && (
        <div
          id={panelId}
          role="group"
          aria-label={ariaLabel}
          className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1"
        >
          {children}
        </div>
      )}
    </div>
  );
}
