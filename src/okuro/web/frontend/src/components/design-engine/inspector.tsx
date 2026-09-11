/**
 * "Why is this value what it is" — anchored where it was asked.
 *
 * `RuleReveal` was good content in the wrong home: it lived in a tab of a right
 * rail, so the page's central explanatory act could be SWALLOWED. The root
 * cause was measured — `<Tabs defaultValue="rule">` was UNCONTROLLED, and the
 * frame's message handler could only re-open the rail, so clicking a value on
 * the canvas while GRAPH, NUMBERS or GUESTS was showing changed nothing
 * anywhere. Three of four tabs ate the gesture silently.
 *
 * With no tabs there is nothing to swallow it. This panel opens over the
 * canvas's right edge from EVERY scene, anchored near what was clicked, and
 * Escape or a click outside dismisses it.
 *
 * It says what it always said — swatch, label, value, the engine's own
 * `because`, the rule's title, formula and source, and the `reads` chips — plus
 * the one line the deleted graph is replaced by: how many values read this one,
 * and a button that shows them.
 */

import { useEffect, useRef } from "react";
import { FlagList } from "./verdict";
import { Note } from "./advice";
import { readersOf } from "./scenes";
import type { AdviceAction } from "./severity";
import type {
  BrandColourModel,
  Derivation,
  Flag,
  ResolvedGround,
  Rule,
} from "./types";

export interface InspectorProps {
  derivation: Derivation | null;
  rule: Rule | null;
  ground: ResolvedGround | null;
  /**
   * The finding a canvas MARK was clicked for.
   *
   * This is the replacement for the 339-to-681-character native `title` the
   * marks used to carry: the same `<Note>` the rail renders — the page's own
   * sentence, its one action — delivered where the reader asked for it.
   */
  flag?: Flag | null;
  colours?: BrandColourModel | null;
  onAction?: (action: AdviceAction, flag: Flag) => void;
  /** Where in the CANVAS PANE the value was clicked. The panel opens beside it. */
  anchorY: number | null;
  onPick: (slot: string) => void;
  onShowReaders: (slot: string) => void;
  onClose: () => void;
}

export function Inspector({
  derivation,
  rule,
  ground,
  flag,
  colours,
  onAction,
  anchorY,
  onPick,
  onShowReaders,
  onClose,
}: InspectorProps) {
  const box = useRef<HTMLDivElement>(null);

  /* THE PANEL TAKES FOCUS WHEN IT OPENS, and that is not a nicety: the gesture
     that opens it usually happens INSIDE the preview iframe, which is a
     different document with its own focus and its own key events. A keydown
     listener on the parent window would never see the Escape a reader pressed
     while the frame still held focus. */
  useEffect(() => {
    if (derivation || flag) box.current?.focus({ preventScroll: true });
  }, [derivation?.slot, flag?.code]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    /* A CLICK ON THE CHROME IS NOT A DISMISSAL. Switching scene, moving a
       slider or opening a rail group are all things a reader does WHILE
       reading this panel — closing it under them would make the page's central
       explanation the one thing you cannot keep on screen. Only a click
       elsewhere on the CANVAS dismisses it, which is the gesture that means
       "I am looking at something else now". */
    const onDown = (event: MouseEvent) => {
      const target = event.target as Element | null;
      if (!box.current || !target?.closest) return;
      if (box.current.contains(target)) return;
      if (target.closest('[data-pane="canvas"]')) onClose();
    };
    window.addEventListener("keydown", onKey);
    // `mousedown` on the CAPTURE phase would close before the canvas click that
    // opened it could land; the bubble phase and a next-tick registration keep
    // the opening gesture from immediately undoing itself.
    const timer = window.setTimeout(
      () => window.addEventListener("mousedown", onDown),
      0,
    );
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown);
    };
  }, [onClose]);

  if (!derivation && !flag) return null;

  const readers = derivation ? readersOf(ground, derivation.slot) : [];

  return (
    <aside
      ref={box}
      tabIndex={-1}
      data-inspector={derivation?.slot ?? flag?.code ?? ""}
      aria-label={
        derivation
          ? `why ${derivation.label} is ${derivation.value}`
          : `what is flagged here`
      }
      className="ce-stack ce-surface"
      style={{
        position: "absolute",
        right: 16,
        /* ANCHORED WHERE IT WAS CLICKED, and never over it: the panel is on the
           canvas's right edge, at the height of the gesture, clamped so it
           cannot run off either end of the pane. */
        top: Math.max(16, Math.min(anchorY ?? 16, 9999)),
        width: "var(--ce-inspector)",
        maxHeight: "calc(100% - 32px)",
        overflowY: "auto",
        padding: "var(--ce-f3)",
        boxShadow: "0 8px 32px rgba(0,0,0,0.45)",
        zIndex: 20,
      }}
    >
      <div className="ce-row">
        {derivation?.swatch && (
          <span className="ce-swatch" style={{ background: derivation.swatch }} />
        )}
        <span className="ce-h2">{derivation?.label ?? "What is flagged here"}</span>
        <button
          type="button"
          className="ce-quiet"
          aria-label="Close"
          style={{ marginLeft: "auto" }}
          onClick={onClose}
        >
          ×
        </button>
      </div>

      {/* THE FINDING FIRST, when a mark is what was clicked. Same sentence as
          the rail's, same single action — never a second copy of the engine's
          paragraph, and never a native tooltip. */}
      {flag && (
        <div data-inspector-flag={flag.code}>
          <Note flag={flag} colours={colours ?? null} onAction={onAction} />
        </div>
      )}

      {derivation && (
        <span
          className="ce-value"
          style={{ fontFamily: "var(--font-mono)", wordBreak: "break-all" }}
        >
          {derivation.value}
        </span>
      )}

      {/* THE ENGINE'S OWN SENTENCE, verbatim. "a light ground takes the brand's
          own black — 20.62:1 here" is the best writing in the product; the
          delivery was the failure, never the copy. */}
      {derivation && <p className="ce-body">{derivation.because}</p>}

      {rule && (
        /* `min-width: 0` on BOTH the section and the block. A flex column child
           defaults to `min-width: auto`, which is the widest thing inside it —
           so a 60-character formula made the 400px panel 1,400px wide and its
           own `overflow-x: auto` never fired. This is the same class of defect
           as the 1114px diagram in a 280px window, at 1/3 the scale. */
        <section className="ce-stack-tight" style={{ minWidth: 0 }}>
          <h3 className="ce-h2">{rule.title}</h3>
          <pre
            className="ce-micro"
            style={{
              margin: 0,
              padding: 12,
              minWidth: 0,
              maxWidth: "100%",
              border: "1px solid var(--ce-border)",
              borderRadius: "var(--ce-r-control)",
              background: "var(--ce-surface-2)",
              /* THE FORMULA KEEPS ITS OWN LINES.
                 `pre-wrap` + `overflow-wrap: anywhere` shredded a three-line
                 ternary into six, with `?` and `:` orphaned on lines of their
                 own — a rule rendered as nonsense inside the panel that exists
                 to explain it. The engine already breaks and indents these
                 strings for reading; the page's job is to not undo that. The
                 rare line that overruns 376px scrolls inside its own box, which
                 is the one place on this page a scroller is the right answer. */
              whiteSpace: "pre",
              overflowWrap: "normal",
              overflowX: "auto",
              fontFamily: "var(--font-mono)",
            }}
          >
            {rule.formula}
          </pre>
          {/* THE CITATION, not a leak. The engine writes its sources for a
              reader of the payload: ASCII `--`, and sometimes the emitted token
              path the rule is about. The dash becomes the page's own, and a
              machine path is set as code so it reads as a reference rather than
              as a sentence a person is expected to parse. */}
          <span className="ce-micro ce-2">{sourceParts(rule.source)}</span>
        </section>
      )}

      {derivation && derivation.reads.length > 0 && (
        <section className="ce-stack-tight" style={{ minWidth: 0 }}>
          <span className="ce-label">Reads</span>
          <div className="ce-row" style={{ flexWrap: "wrap", gap: 8 }}>
            {derivation.reads.map((read) => (
              <button
                key={read}
                type="button"
                className="ce-chip"
                onClick={() => onPick(read)}
              >
                {read}
              </button>
            ))}
          </div>
        </section>
      )}

      {/* THE GRAPH'S ONE UNIQUE JOB, as a sentence and a filter. */}
      {derivation && (
        <section className="ce-stack-tight">
          <span className="ce-body" data-readers={readers.length}>
            {readers.length} value{readers.length === 1 ? "" : "s"} read this.
          </span>
          {readers.length > 0 && (
            <button
              type="button"
              className="ce-chip"
              style={{ alignSelf: "flex-start" }}
              onClick={() => onShowReaders(derivation.slot)}
            >
              Show them
            </button>
          )}
        </section>
      )}

      {!flag && ground && ground.palette.flags.length > 0 && (
        <section className="ce-stack-tight">
          <span className="ce-label">On this ground</span>
          <FlagList flags={ground.palette.flags} />
        </section>
      )}
    </aside>
  );
}

/**
 * A rule's source line, rendered rather than dumped.
 *
 * Two things the engine's own strings carry that a page must not print raw: the
 * ASCII `--` it writes where the page writes `—`, and an emitted token path
 * (`THEME.COLORS.BASE.FOREGROUND`) which is a reference, not prose. The text is
 * unchanged; only its typography is.
 */
function sourceParts(source: string) {
  return source
    .replace(/\s--\s/g, " — ")
    .split(/([A-Z][A-Z0-9_]*(?:\.[A-Z][A-Z0-9_]*)+)/)
    .map((part, i) =>
      i % 2 === 1 ? (
        <code key={i} style={{ fontFamily: "var(--font-mono)" }}>
          {part}
        </code>
      ) : (
        part
      ),
    );
}

/** What the panel says before anything has been clicked. */
export function InspectorHint() {
  return (
    <p className="ce-body ce-2">
      Click any value — on the document, or in the token list — for the rule that
      produced it, the inputs that rule read, and the measurement that decided it.
    </p>
  );
}
