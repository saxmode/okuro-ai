/**
 * The shell: a toolbar, ONE rail, and a canvas that gets everything else.
 *
 * TWO COLUMNS, NEVER THREE. The right rail is deleted and its four tabs are
 * relocated — RULE to an anchored inspector, GRAPH to nothing (its jobs went to
 * inline provenance and a filtered list), NUMBERS to the utility foot of the
 * TOKENS scene, GUESTS to a full-width canvas scene. Measured before: at 1280
 * the canvas was 294px, NARROWER THAN EITHER RAIL, and at 1440 it held 31.5 %
 * of the viewport on a page whose whole subject is the thing in it.
 *
 * THE APP'S OWN CHROME COLLAPSES BELOW 1600. This page is a workbench, not a
 * document. A 48px strip restores it, Escape restores it, and leaving the page
 * restores it — the gesture already exists in `useChrome()` and this page
 * already owned that API. It is what turns 31.5 % at 1440 into 74 %.
 *
 * `FOCUS` IS DELETED, not re-labelled. A control for the APP's chrome does not
 * belong in a DESIGN toolbar: it is the one button on the page that changes
 * something outside the page, and its tooltip was the only thing that said so.
 * The auto-collapse does the same work without asking.
 */

import { useEffect } from "react";
import { useChrome } from "@/lib/chrome-context";
import { CHROME_COLLAPSE_BELOW, CHROME_STRIP } from "./chrome";

/**
 * The one rail: a floating studio panel over a full-width canvas.
 *
 * PX, NOT REM, and that is the 50 %-root rule rather than a style choice: a
 * hand-written rem length is denominated in the ROOT, so under the engine's
 * `html { font-size: 50% }` this rail would render at half width — 160px where
 * 320 was meant. A rail width is chrome, not a factor of the system base.
 */
export function Rail({
  title,
  open,
  onOpen,
  children,
}: {
  title: string;
  open: boolean;
  onOpen: (open: boolean) => void;
  children: React.ReactNode;
}) {
  if (!open) {
    return (
      <aside
        aria-label={title}
        data-rail="left"
        data-open="false"
        style={{
          position: "absolute",
          zIndex: 30,
          top: 24,
          left: 24,
          display: "flex",
          alignItems: "center",
          border: "1px solid var(--ce-border)",
          borderRadius: 12,
          background: "var(--ce-surface)",
          boxShadow: "0 16px 48px rgba(0, 0, 0, 0.24)",
        }}
      >
        <button
          type="button"
          className="ce-quiet ce-row"
          aria-label={`Open ${title}`}
          style={{ height: 40, padding: "0 16px", gap: 8 }}
          onClick={() => onOpen(true)}
        >
          <span aria-hidden>+</span>
          <span className="ce-label">{title}</span>
        </button>
      </aside>
    );
  }

  return (
    <aside
      aria-label={title}
      data-rail="left"
      data-open="true"
      style={{
        position: "absolute",
        zIndex: 30,
        top: 24,
        bottom: 24,
        left: 24,
        width: "var(--ce-rail)",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        border: "1px solid var(--ce-border)",
        borderRadius: 16,
        background: "var(--ce-surface)",
        boxShadow:
          "0 0 0 1px color-mix(in srgb, var(--ce-fg) 4%, transparent), 0 24px 72px rgba(0, 0, 0, 0.34)",
      }}
    >
      {/* The title row is a stable grab line above the scrolling decisions. */}
      <div
        className="ce-row"
        style={{
          height: 48,
          flex: "none",
          padding: "0 20px",
          borderBottom: "1px solid var(--ce-border)",
        }}
      >
        <span className="ce-label">{title}</span>
        <button
          type="button"
          className="ce-quiet"
          aria-label={`Close ${title}`}
          style={{ marginLeft: "auto" }}
          onClick={() => onOpen(false)}
        >
          ‹
        </button>
      </div>
      <div
        data-rail-scroller
        style={{
          minHeight: 0,
          flex: 1,
          overflowY: "auto",
          /* f1 down the page, f3 across it. The horizontal padding is what keeps
             the 272px measure the copy is written to and does not move. The
             VERTICAL is pure scroller: the rail's own 24px header already carries
             a hairline, so the first group needs no second boundary above it, and
             f3 -> f2 -> f1 is a walk down the same scale rather than an off-scale
             number. It is the last 16px of the ratio in his item 9 — 1,105px of
             content is 1.51 screens at 1440x900 and 1,089 is 1.49. */
          padding: "16px 20px 24px",
        }}
      >
        {children}
      </div>
    </aside>
  );
}

/**
 * The app chrome, collapsed while this page is narrow.
 *
 * Escape restores it and leaving the page restores it, which is the same
 * contract okuro·flow and /design-construction already ship. Above the
 * threshold the page never touches it: 334px of a 2560px screen is not the
 * canvas's problem, and taking a reader's own navigation away for no reason is.
 */
export function useAutoCollapsedChrome(escapeTaken = false) {
  const chrome = useChrome();
  const { hide, show, hidden } = chrome;

  useEffect(() => {
    const query = window.matchMedia(`(max-width: ${CHROME_COLLAPSE_BELOW - 1}px)`);
    const apply = () => (query.matches ? hide() : show());
    apply();
    query.addEventListener("change", apply);
    return () => {
      query.removeEventListener("change", apply);
      show();
    };
  }, [hide, show]);

  /* ESCAPE BELONGS TO WHATEVER IS OPEN, and only then to the chrome.
     Measured: with the inspector open at 1440 the FIRST Escape restored the
     app's navigation — shrinking the canvas the reader was reading — and left
     the panel on screen; only the second closed it. Two handlers for one key,
     and the wrong one won. While an overlay says it has taken Escape, this one
     stands down; the next press restores the chrome, which is the escalation a
     reader expects. */
  useEffect(() => {
    if (!hidden || escapeTaken) return;
    const onEsc = (event: KeyboardEvent) => {
      if (event.key === "Escape") show();
    };
    window.addEventListener("keydown", onEsc);
    return () => window.removeEventListener("keydown", onEsc);
  }, [hidden, show, escapeTaken]);

  return chrome;
}

/**
 * The strip that brings the app's own chrome back when it is collapsed.
 *
 * IT IS THE FULL HEIGHT OF THE PAGE, beside the toolbar rather than under it,
 * and that is a collision fix rather than a composition preference. The APP's
 * own `ChromeRestoreButton` is `position:absolute; left:8px; top:8px; 24x24`
 * over the routed page — so with the strip starting below the toolbar, that
 * button landed 8px on top of this page's `h1`, and the first glyph of the
 * brand name was replaced by the button's own icon at 1280 and 1440. One 48px
 * gutter down the whole left edge gives the app's affordance somewhere to be.
 * The 40px top inset is that button's own height plus its offset, so the two
 * never share a row.
 */
export function ChromeStrip({ hidden, onShow }: { hidden: boolean; onShow: () => void }) {
  if (!hidden) return null;
  return (
    <aside
      data-chrome-strip
      style={{
        width: CHROME_STRIP,
        flex: "none",
        borderRight: "1px solid var(--ce-border)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        paddingTop: 40,
      }}
    >
      <button
        type="button"
        className="ce-quiet"
        onClick={onShow}
        title="Show okuro's navigation — Escape does the same"
      >
        <span
          className="ce-micro"
          style={{ writingMode: "vertical-rl", whiteSpace: "nowrap" }}
        >
          okuro
        </span>
      </button>
    </aside>
  );
}

/** The bar above everything: identity on the left, one filled action right. */
export function Toolbar({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="ce-row"
      data-engine-toolbar
      style={{
        height: 64,
        flex: "none",
        gap: 16,
        padding: "0 var(--ce-f4)",
        borderBottom: "1px solid var(--ce-border)",
        background: "var(--ce-surface)",
      }}
    >
      {children}
    </div>
  );
}
