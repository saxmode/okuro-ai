import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { clsx } from "clsx";
import { OnboardingPulseMark } from "./pulse-mark";
import { ProgressDots } from "./progress-dots";

/**
 * Full-viewport onboarding shell.
 *
 * Owns a single persistent pulse element that morphs between two poses
 * based on the current page:
 *
 *   - Page 0 (splash): big pulse centered in viewport, big "okuro" h1 below,
 *     no dots. The first page is the splash itself — a process step that
 *     happens to be the brand landing.
 *
 *   - Page 1+ (wizard): small pulse at top, compact "okuro" wordmark
 *     overlapping, dot progress below, scroll-snap content underneath.
 *
 * Because the pulse element never unmounts, the canvas + PulseEngine are
 * preserved; the morph is pure CSS transitions on position / size.
 *
 * Pages scroll-snap vertically; Enter / ↓ advance, Shift+Enter / ↑ go back,
 * scroll wheel works natively. IntersectionObserver syncs `current` with
 * whichever section is in view.
 */

export interface OnboardingPageDef {
  key: string;
  node: React.ReactNode;
  /** Page 0 is the splash by convention; it renders full-bleed with a
   *  centered content column positioned below the big pulse. Any other
   *  page flagged `splash` behaves the same, though you rarely want more
   *  than one. */
  splash?: boolean;
  /** Short label shown next to this page's marker in the vertical
   *  progress rail. Omit for pages that shouldn't appear in the table-
   *  of-contents (splashes, merged identity sub-pages, etc.); the rail
   *  still reserves a marker for them but without text. */
  label?: string;
}

// Pulse is always rendered at its intrinsic size; the morph is pure
// `transform: scale()` + vertical `top`. Both values are driven directly
// by scroll position (see scroll listener below) — NOT by a CSS transition.
// That way the browser's native scroll-smoothing IS the animation clock;
// the pulse and the content move on exactly the same timeline.
const PULSE_INTRINSIC_SIZE = 640;
const HEADER_SCALE = 160 / PULSE_INTRINSIC_SIZE; // 0.25 → 160px visual
const HEADER_CENTER_Y = 24 + 160 / 2; // 104px — center of the 160px visual at top:24
const SPLASH_OFFSET_Y = 140; // shift splash pulse up so content reads below

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

export function OnboardingShell({
  pages,
  onFinish,
  initialIndex = 0,
}: {
  pages: OnboardingPageDef[];
  onFinish?: () => void;
  /** Which page to land on when the shell mounts. Used by
   *  `onboarding-preview` to resume at the first not-done backend step
   *  instead of always showing the splash on reload. Clamped to the page
   *  range, so callers can pass a computed index without guarding. */
  initialIndex?: number;
}) {
  const [current, setCurrent] = useState(() =>
    Math.max(0, Math.min(initialIndex, pages.length - 1)),
  );
  // 0 = fully splash pose, 1 = fully header pose. Driven by scrollTop so the
  // pulse morph tracks the scroll in real time — no independent CSS timeline.
  const [morphProgress, setMorphProgress] = useState(0);
  const [viewportH, setViewportH] = useState(
    typeof window !== "undefined" ? window.innerHeight : 800,
  );
  const scrollerRef = useRef<HTMLDivElement>(null);
  const sectionRefs = useRef<Array<HTMLElement | null>>([]);
  const hasSplash = pages[0]?.splash ?? false;

  const setSectionRef = useCallback(
    (index: number) => (el: HTMLElement | null) => {
      sectionRefs.current[index] = el;
    },
    [],
  );

  const jumpTo = useCallback(
    (index: number) => {
      const clamped = Math.max(0, Math.min(index, pages.length - 1));
      const el = sectionRefs.current[clamped];
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    },
    [pages.length],
  );

  // Per-page advance handlers. Each OnboardingPage registers itself against
  // its own index; shell.next() awaits the handler for the CURRENT index
  // before scrolling on. A handler that throws cancels the advance so the
  // user stays on the page and sees the error, nothing silently skipped.
  const advanceHandlers = useRef<Map<number, () => Promise<void> | void>>(new Map());
  const registerAdvanceHandler = useCallback(
    (index: number, fn: (() => Promise<void> | void) | null) => {
      if (fn) {
        advanceHandlers.current.set(index, fn);
      } else {
        advanceHandlers.current.delete(index);
      }
    },
    [],
  );

  const next = useCallback(async () => {
    const handler = advanceHandlers.current.get(current);
    if (handler) {
      try {
        await handler();
      } catch {
        return; // handler already surfaced the error in-page
      }
    }
    if (current < pages.length - 1) jumpTo(current + 1);
    else onFinish?.();
  }, [current, pages.length, jumpTo, onFinish]);

  const prev = useCallback(() => jumpTo(current - 1), [current, jumpTo]);

  // Keyboard — Enter advances, Shift+Enter backs up.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase() ?? "";
      const inTextarea = tag === "textarea" || target?.isContentEditable;
      if (inTextarea) return;

      if (e.key === "Enter") {
        e.preventDefault();
        if (e.shiftKey) prev();
        else void next();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        void next();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        prev();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [next, prev]);

  // Scroll-driven morph: the pulse pose is a continuous function of scrollTop
  // in the range [0, viewport-height]. While the browser smooth-scrolls
  // between pages, `scrollTop` animates; our listener fires every frame and
  // lerps the pose. Single clock for content + pulse → perfect sync.
  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller || !hasSplash) {
      setMorphProgress(1); // no splash → always header pose
      return;
    }
    let raf = 0;
    const update = () => {
      const p = Math.max(0, Math.min(1, scroller.scrollTop / viewportH));
      setMorphProgress(p);
    };
    const onScroll = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        update();
      });
    };
    update();
    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      if (raf) cancelAnimationFrame(raf);
      scroller.removeEventListener("scroll", onScroll);
    };
  }, [hasSplash, viewportH]);

  // Track viewport height so splashTop stays correct after window resize.
  useEffect(() => {
    const onResize = () => setViewportH(window.innerHeight);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Land on `initialIndex` after first layout. Runs once — subsequent
  // scroll is driven by user interaction + IntersectionObserver. Without
  // this, a wizard reload always shows the splash no matter how far the
  // user actually got, and scroll restoration (browser / pywebview) can
  // leave scrollTop at a stale value from a prior session.
  //
  // We use the section's actual `offsetTop` rather than `initialIndex *
  // viewportH`. The old multiply assumed every section was exactly one
  // viewport tall, but non-splash sections use `min-h-screen` and grow
  // when content overflows (long forms, tall option grids). The multiply
  // therefore landed inside an EARLIER section than intended — the user
  // saw e.g. "Location" (page 5) when the backend resume target was
  // "communication" (page 7), because earlier sections had pushed the
  // real offsets past the assumed grid.
  //
  // 2026-04-27: extra hardening after a Mac install report where the
  // wizard opened on page 5 even with `initialIndex = 0`. Cause is
  // `history.scrollRestoration = "auto"` (the browser default + pywebview
  // default): browsers auto-restore scroll across same-URL reloads, and
  // the restoration ran AFTER our initial mount on some pywebview /
  // Safari combinations, clobbering our scrollTop=0. Fix: disable
  // browser scroll restoration explicitly AND force the scroll twice
  // (once sync on mount, once on next animation frame) so even if
  // restoration runs late we win.
  useEffect(() => {
    if (typeof history !== "undefined" && "scrollRestoration" in history) {
      try {
        history.scrollRestoration = "manual";
      } catch {
        // Some embedded webviews disallow the assignment; safe to ignore.
      }
    }
    const scroller = scrollerRef.current;
    if (!scroller) return;
    const target = sectionRefs.current[initialIndex];
    if (!target) return;
    const targetTop = target.offsetTop;
    // Force the scroll on mount — UNCONDITIONAL, so restoration can't
    // sneak in via `if (scrollTop !== targetTop)` skipping us.
    scroller.scrollTop = targetTop;
    // Re-assert after the next paint to defeat late restoration.
    const raf = requestAnimationFrame(() => {
      if (scrollerRef.current && scrollerRef.current.scrollTop !== targetTop) {
        scrollerRef.current.scrollTop = targetTop;
      }
    });
    return () => cancelAnimationFrame(raf);
    // Intentionally empty deps: jump-to-initial runs on mount only. If the
    // user navigates forward/back later, the shell's own next()/prev() own
    // the scroll and initialIndex stays frozen at its first value.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // IntersectionObserver keeps `current` in sync with manual scroll.
  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    const observer = new IntersectionObserver(
      (entries) => {
        let bestIndex = -1;
        let bestRatio = 0;
        entries.forEach((entry) => {
          const idx = Number((entry.target as HTMLElement).dataset.pageIndex ?? "-1");
          if (idx < 0) return;
          if (entry.intersectionRatio > bestRatio) {
            bestRatio = entry.intersectionRatio;
            bestIndex = idx;
          }
        });
        if (bestIndex >= 0 && bestRatio > 0.5) setCurrent(bestIndex);
      },
      { root: scroller, threshold: [0.25, 0.5, 0.75, 1] },
    );
    sectionRefs.current.forEach((el) => {
      if (el) observer.observe(el);
    });
    return () => observer.disconnect();
  }, [pages.length]);

  // Lerped pose values — recomputed every render from morphProgress.
  const splashTopPx = viewportH / 2 - SPLASH_OFFSET_Y;
  const pulseTopPx = lerp(splashTopPx, HEADER_CENTER_Y, morphProgress);
  const pulseScale = lerp(1, HEADER_SCALE, morphProgress);
  const chromeOpacity = morphProgress; // wordmark + dots fade in with scroll

  return (
    <div className="relative h-full w-full overflow-hidden bg-surface text-fg-primary">
      {/* ─── Shared pulse ─────────────────────────────────────────────
          Morph driven directly by scroll position (see morphProgress).
          No CSS transition — the native smooth-scroll timeline IS the
          animation clock, so content and pulse are pixel-synchronized. */}
      <div
        aria-hidden
        style={{
          position: "fixed",
          left: "50%",
          top: `${pulseTopPx}px`,
          width: PULSE_INTRINSIC_SIZE,
          height: PULSE_INTRINSIC_SIZE,
          transform: `translate(-50%, -50%) scale(${pulseScale})`,
          transformOrigin: "center center",
          willChange: "top, transform",
          zIndex: 30,
          pointerEvents: "none",
        }}
      >
        <OnboardingPulseMark />
      </div>

      {/* ─── Wordmark — also scroll-driven opacity ────────────────── */}
      <div
        aria-hidden
        className="pointer-events-none fixed left-1/2 -translate-x-1/2 text-sm font-light tracking-[0.4em] text-fg-secondary uppercase select-none"
        style={{
          top: `${24 + 160 - 40}px`, // overlap lower band of pulse (160px visual)
          opacity: chromeOpacity,
          zIndex: 40,
        }}
      >
        okuro
      </div>

      {/* ─── Vertical progress rail — scroll-driven opacity ───────────
           Sits on the left edge of the viewport, vertically centered, so
           the user can read at a glance what's done / current / upcoming
           without the rail fighting the centered content column (max-w-2xl
           lives well clear of `left-6`). Labels come from each page def so
           the rail doubles as a table-of-contents — click any completed /
           current label to jump back.
           Hidden while the splash is in view (chromeOpacity < 0.5) so the
           first screen stays clean. */}
      <div
        className="fixed left-4 md:left-6 top-1/2 -translate-y-1/2 max-h-[80vh] overflow-y-auto pr-2"
        style={{
          opacity: chromeOpacity,
          pointerEvents: chromeOpacity > 0.5 ? "auto" : "none",
          zIndex: 40,
        }}
      >
        <ProgressDots
          total={pages.length}
          current={current}
          labels={pages.map((p) => p.label ?? null)}
          onJump={jumpTo}
        />
      </div>

      {/* ─── Scroll-snap pages ───────────────────────────────────────
           `snap-proximity` (not -mandatory) so tall pages (e.g. principles
           with a long options grid) can be scrolled through freely; the
           browser still snaps at boundaries when the user releases near
           one. Non-splash sections use `min-h-screen` + `items-start` +
           a 280px top pad so tall content grows the section instead of
           overflowing the centered box — that overflow was the root cause
           of the "dead buttons" report on the principles page: the top of
           the grid fell under the fixed ProgressDots strip (zIndex 40,
           pointer-events auto), eating clicks. */}
      <div
        ref={scrollerRef}
        className="h-full w-full overflow-y-auto overflow-x-hidden snap-y snap-proximity scroll-smooth"
      >
        {pages.map((p, i) => (
          <section
            key={p.key}
            ref={setSectionRef(i)}
            data-page-index={i}
            className={clsx(
              "w-screen snap-start px-8 flex justify-center",
              p.splash
                ? "h-screen items-center snap-always"
                : "min-h-screen items-start",
            )}
          >
            <div
              className={clsx(
                "w-full",
                p.splash ? "max-w-xl" : "max-w-2xl",
              )}
              style={
                p.splash
                  ? {
                      // Push content below the big pulse (viewport center)
                      paddingTop: "calc(50vh + 200px)",
                      paddingBottom: "4rem",
                    }
                  : {
                      // Leave room for header chrome (pulse 160 + wordmark + dots + padding)
                      paddingTop: "280px",
                      paddingBottom: "5rem",
                    }
              }
            >
              <OnboardingPageContext.Provider
                value={{ next, prev, index: i, total: pages.length, registerAdvanceHandler }}
              >
                {p.node}
              </OnboardingPageContext.Provider>
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}

// Shared context so page components can call next() / prev() without drilling props.
interface PageContextValue {
  next: () => void | Promise<void>;
  prev: () => void;
  index: number;
  total: number;
  /** Register a pre-advance handler for the page at `index`. Passing null
   *  (or the effect cleanup) unregisters it. The shell awaits this before
   *  scrolling forward; a throw cancels the advance. */
  registerAdvanceHandler: (
    index: number,
    fn: (() => Promise<void> | void) | null,
  ) => void;
}

const OnboardingPageContext = createContext<PageContextValue>({
  next: () => {},
  prev: () => {},
  index: 0,
  total: 1,
  registerAdvanceHandler: () => {},
});

export function useOnboardingPage() {
  return useContext(OnboardingPageContext);
}
