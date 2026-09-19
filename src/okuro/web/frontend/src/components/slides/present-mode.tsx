import { useCallback, useEffect, useRef, useState } from "react";
import type { Deck } from "./scene";
import { elementsMaxDepth } from "./scene";
import { SlideDeck } from "./slide-deck";
import { SlideThumb } from "./slide-thumb";
import { cn } from "@/lib/utils";

/**
 * PresentMode — the deck, full screen, with a presenter view.
 *
 * Audience view (default): the smart-animate deck full-bleed.
 *
 * Progressive disclosure: each slide reveals its depth levels in reading order.
 * →/Space/↓ raises the reveal level (detail N/N); once a slide is fully revealed
 * it advances to the next slide, and ←/↑ walks back the same way — so the forward
 * arrows step THROUGH detail, not just between slides. +/- step the level only.
 *
 * Keys:
 *   →/Space/↓  reveal / next   ←/↑   un-reveal / prev   Esc  exit (or close grid/blank)
 *   +/=  more detail   -  less detail
 *   N          notes     P         presenter view toggle
 *   O / G      overview/jump grid   L  laser pointer   B  black   W  white
 *
 * ── D5: IT USED TO PRESENT INTO A 738×825 BOX, AND THE CAUSE IS THE SHELL'S
 *    OWN CONTAINMENT, not a missing portal.
 *
 * The overlay is `position: fixed; inset: 0`, which normally means the viewport.
 * But `.c-panes > .pane` carries `container: pane / inline-size`
 * (`shell/styles/shell.css:971`), and `container-type: inline-size` implies
 * `contain: layout style inline-size` — which makes the pane a containing block
 * for every `position: fixed` descendant. The shell's own comment swept for
 * exactly this and found "exactly ONE non-portaled fixed descendant in any
 * pane": NOTES' drag cursor. This was the second, and it was missed because it
 * only exists while presenting.
 *
 * THE FIX IS THE FULLSCREEN API ON THE PANE ITSELF, which is the smallest
 * correct answer rather than a portal:
 *   - a fullscreened pane IS the viewport, so `fixed inset-0` becomes the
 *     screen again with no change to this component's geometry;
 *   - Esc is the browser's, which is what a presenter expects, and it is
 *     wired back to `onExit` through the `fullscreenchange` event;
 *   - nothing has to re-establish `data-appearance` or the engine sheet by
 *     hand, which a portal to `document.body` would.
 * If the request is refused — no user gesture, a policy, an old engine — the
 * overlay still renders and still works, just inside the pane. It says so
 * rather than pretending, so a refused request is visible instead of silent.
 *
 * ── THE 25 WHITE/BLACK LITERALS ARE NOW FOUR DECLARED NAMES.
 *
 * A presentation surface is deliberately black whatever appearance the user
 * runs, because nobody projects a white page, so its ink is on-black and
 * okuro-ds has no on-black role to reach for. The obvious trick does NOT work
 * and was measured before being rejected: putting `data-appearance="dark"` on
 * this overlay leaves every `--color-*` unchanged, because the engine scopes
 * its tokens on `:root` alone. Under a light kit a nested dark appearance reads
 * back `--color-foreground-primary: #131313` — near-black ink on a black
 * ground. So these are D1's documented-local case, declared once at the root of
 * the overlay and used by name below, the way `graph.css` replaced 21 scattered
 * values with 20 declared ones.
 */

/** The presentation surface's own palette. See the note above for why these are
 *  locals and not kit names. Declared once; every rule below reads them. */
const SURFACE_VARS = {
  /* The projected ground. Black, not the kit's background. */
  "--pm-ground": "#000000",
  /* Ink on that ground, and its two quieter tiers. */
  "--pm-ink": "#ffffff",
  "--pm-ink-dim": "rgba(255, 255, 255, 0.7)",
  "--pm-ink-faint": "rgba(255, 255, 255, 0.4)",
  /* Edges and plates on that ground. */
  "--pm-edge": "rgba(255, 255, 255, 0.2)",
  "--pm-edge-strong": "rgba(255, 255, 255, 0.4)",
  "--pm-plate": "rgba(255, 255, 255, 0.03)",
  /* The laser dot. A hue rather than a polarity, so it is visible on any
     slide — the same reason the editor's alignment guide is a hue. */
  "--pm-laser": "#ff4d6d",
  "--pm-laser-glow": "rgba(255, 77, 109, 0.7)",
} as React.CSSProperties;

export function PresentMode({
  deck,
  onExit,
  surface,
}: {
  deck: Deck;
  onExit: () => void;
  /** The element to take full screen — the leaf's pane. When absent or refused,
   *  the overlay renders inside whatever containing block it has. */
  surface?: HTMLElement | null;
}) {
  const [index, setIndex] = useState(0);
  const [showNotes, setShowNotes] = useState(false);
  const [presenter, setPresenter] = useState(false);
  const [overview, setOverview] = useState(false);
  const [laser, setLaser] = useState(false);
  // Transition engine override: undefined = use the deck's own mode; explicit
  // value flips it live for the whole session (T key / control button).
  const [mode, setMode] = useState<"morph" | "push" | undefined>(undefined);
  const [blank, setBlank] = useState<null | "black" | "white">(null);
  const [elapsed, setElapsed] = useState(0); // seconds since timer start
  const startRef = useRef<number>(Date.now());
  const [laserPos, setLaserPos] = useState<{ x: number; y: number } | null>(null);
  /** Whether the browser actually granted full screen. Reported, not assumed. */
  const [full, setFull] = useState(false);
  const last = deck.slides.length - 1;

  /* Ask for full screen once, on open. The click on ▶ Present IS the user
     gesture the API requires, so this is a legitimate request rather than a
     hopeful one. Exiting full screen by any route (Esc, the OS, the browser
     chrome) leaves presentation too — otherwise Esc would drop the presenter
     into a pane-sized overlay, which is the confusing half-state. */
  useEffect(() => {
    const el = surface;
    if (!el?.requestFullscreen || !document.fullscreenEnabled) return;
    let asked = false;
    void el.requestFullscreen().then(
      () => { asked = true; setFull(true); },
      () => { setFull(false); },
    );
    const onChange = () => {
      const on = document.fullscreenElement === el;
      setFull(on);
      if (asked && !on) onExit();
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => {
      document.removeEventListener("fullscreenchange", onChange);
      if (document.fullscreenElement === el) void document.exitFullscreen().catch(() => {});
    };
  }, [surface, onExit]);

  // Reveal level for the current slide's progressive disclosure. Starts at the
  // deck's stored default (a re-tailored terse variant lowers it) else 1 = glance.
  const slideMax = useCallback(
    (i: number) => elementsMaxDepth(deck.slides[i]?.elements ?? []),
    [deck],
  );
  const [depth, setDepth] = useState(() => Math.min(Math.max(1, deck.maxDepth ?? 1), slideMax(0)));

  const go = (next: number) => {
    setIndex(Math.min(last, Math.max(0, next)));
    setBlank(null); // any nav restores from blank screen
  };
  // Overview jump lands on a fully-revealed slide.
  const jumpTo = (i: number) => {
    go(i);
    setDepth(slideMax(i));
  };
  // Forward: reveal the next level, or (if fully revealed) advance a slide at
  // level 1. Backward: hide a level, or (at level 1) step back to the prior
  // slide fully revealed. This threads detail into the natural reading flow.
  const advance = () => {
    setBlank(null);
    if (depth < slideMax(index)) setDepth(depth + 1);
    else if (index < last) { setIndex(index + 1); setDepth(1); }
  };
  const retreat = () => {
    setBlank(null);
    if (depth > 1) setDepth(depth - 1);
    else if (index > 0) { const pi = index - 1; setIndex(pi); setDepth(slideMax(pi)); }
  };
  const raiseDepth = () => setDepth((d) => Math.min(slideMax(index), d + 1));
  const lowerDepth = () => setDepth((d) => Math.max(1, d - 1));
  const curMax = slideMax(index);
  const shownDepth = Math.min(depth, curMax);
  const atStart = index === 0 && depth <= 1;
  const atEnd = index === last && depth >= curMax;

  // Elapsed timer — ticks every second from the moment present mode opened.
  useEffect(() => {
    const t = window.setInterval(() => setElapsed(Math.floor((Date.now() - startRef.current) / 1000)), 1000);
    return () => window.clearInterval(t);
  }, []);

  /* The overlay owns the keyboard for as long as it is up, so this listener is
     NOT gated on the pane being active — an inactive pane cannot be presenting,
     because the component only exists while it is. */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const k = e.key.toLowerCase();
      if (e.key === "Escape") {
        if (overview) setOverview(false);
        else if (blank) setBlank(null);
        else onExit();
      } else if (e.key === "ArrowRight" || e.key === " " || e.key === "ArrowDown") {
        e.preventDefault();
        advance();
      } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
        e.preventDefault();
        retreat();
      } else if (e.key === "+" || e.key === "=") raiseDepth();
      else if (e.key === "-") lowerDepth();
      else if (k === "n") setShowNotes((s) => !s);
      else if (k === "p") setPresenter((s) => !s);
      else if (k === "o" || k === "g") setOverview((s) => !s);
      else if (k === "l") setLaser((s) => !s);
      else if (k === "t") setMode((m) => ((m ?? deck.transition?.mode ?? "morph") === "push" ? "morph" : "push"));
      else if (k === "b") setBlank((b) => (b === "black" ? null : "black"));
      else if (k === "w") setBlank((b) => (b === "white" ? null : "white"));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [last, onExit, overview, blank, index, depth]);

  const notes = deck.slides[index]?.notes?.trim();
  const mmss = (s: number) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  const resetTimer = () => {
    startRef.current = Date.now();
    setElapsed(0);
  };

  // Laser dot follows the cursor over the slide stage.
  const onStageMove = (e: React.MouseEvent) => {
    if (!laser) return;
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    setLaserPos({ x: e.clientX - r.left, y: e.clientY - r.top });
  };

  const laserDot =
    laser && laserPos ? (
      <div
        aria-hidden
        className="pointer-events-none absolute z-10 h-4 w-4 -translate-x-1/2 -translate-y-1/2 rounded-full"
        style={{
          left: laserPos.x,
          top: laserPos.y,
          background: "var(--pm-laser)",
          boxShadow: "0 0 12px 4px var(--pm-laser-glow)",
        }}
      />
    ) : null;

  const btnCls =
    "rounded border px-3 py-1 disabled:opacity-30 " +
    "border-[color:var(--pm-edge)] hover:border-[color:var(--pm-edge-strong)]";
  const onBtn = "border-[color:var(--pm-laser)] text-[color:var(--pm-laser)]";

  return (
    <div
      className="fixed inset-0 z-[200] flex flex-col"
      style={{ ...SURFACE_VARS, background: "var(--pm-ground)", color: "var(--pm-ink)" }}
      role="dialog"
      aria-label="Presentation"
    >
      {/* Blank screen overlay — press again or navigate to restore. */}
      {blank && (
        <button
          aria-label={`Blank ${blank} screen — click or press any key to restore`}
          onClick={() => setBlank(null)}
          className="absolute inset-0 z-[60]"
          style={{ background: blank === "black" ? "var(--pm-ground)" : "var(--pm-ink)" }}
        />
      )}

      {/* Overview / jump grid. */}
      {overview && (
        <div
          className="absolute inset-0 z-50 overflow-auto p-8"
          style={{ background: "var(--pm-ground)" }}
          role="dialog"
          aria-label="Slide overview"
        >
          <div className="mb-4 flex items-center justify-between text-sm" style={{ color: "var(--pm-ink-dim)" }}>
            <span>Jump to slide</span>
            <button onClick={() => setOverview(false)} className={btnCls} aria-label="Close overview">Close (Esc)</button>
          </div>
          <div className="grid grid-cols-[repeat(auto-fill,minmax(0,1fr))] gap-4 @2xl:grid-cols-3 @5xl:grid-cols-4">
            {deck.slides.map((s, i) => (
              <button
                key={s.id}
                onClick={() => { jumpTo(i); setOverview(false); }}
                aria-label={`Go to slide ${i + 1}`}
                aria-current={i === index}
                className="relative rounded-lg border p-1 text-left transition"
                style={{ borderColor: i === index ? "var(--pm-laser)" : "var(--pm-edge)" }}
              >
                <SlideThumb deck={deck} slideIndex={i} width={210} />
                <span
                  className="absolute bottom-1 right-2 rounded px-1.5 text-xs"
                  style={{ background: "var(--pm-ground)", color: "var(--pm-ink-dim)" }}
                >
                  {i + 1}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {presenter ? (
        /* ── Presenter view ─────────────────────────────────────────── */
        <div className="flex min-h-0 flex-1 flex-col gap-4 p-6 @4xl:flex-row">
          <div className="flex min-w-0 flex-[2] flex-col gap-2">
            <span className="case-label text-xs" style={{ color: "var(--pm-ink-faint)" }}>Current</span>
            <div className="relative min-w-0" onMouseMove={onStageMove}>
              <SlideDeck deck={deck} index={index} mode={mode} maxDepth={depth} />
              {laserDot}
            </div>
          </div>
          <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-4">
            <div className="flex min-w-0 flex-col gap-2">
              <span className="case-label text-xs" style={{ color: "var(--pm-ink-faint)" }}>Next</span>
              {index < last ? (
                <SlideThumb deck={deck} slideIndex={index + 1} width={380} />
              ) : (
                <div
                  className="rounded border p-6 text-sm"
                  style={{ borderColor: "var(--pm-edge)", color: "var(--pm-ink-faint)" }}
                >
                  End of deck
                </div>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-4 text-sm" style={{ color: "var(--pm-ink-dim)" }}>
              <span className="font-mono text-2xl tabular-nums" style={{ color: "var(--pm-ink)" }}>{mmss(elapsed)}</span>
              <button onClick={resetTimer} className={btnCls} aria-label="Reset timer">Reset</button>
              <span className="ml-auto font-mono">
                {index + 1} / {last + 1}
                {curMax > 1 && (
                  <span className="ml-2" style={{ color: "var(--pm-ink-faint)" }}>detail {shownDepth}/{curMax}</span>
                )}
              </span>
            </div>
            <div
              className="min-h-0 flex-1 overflow-y-auto whitespace-pre-wrap rounded border p-3 text-sm leading-relaxed"
              style={{ borderColor: "var(--pm-edge)", background: "var(--pm-plate)", color: "var(--pm-ink-dim)" }}
            >
              {notes || <span style={{ color: "var(--pm-ink-faint)" }}>No notes for this slide.</span>}
            </div>
          </div>
        </div>
      ) : (
        /* ── Audience view ──────────────────────────────────────────── */
        <div className="flex min-h-0 flex-1 items-center justify-center p-6">
          <div className="relative w-full min-w-0" onMouseMove={onStageMove}>
            <SlideDeck deck={deck} index={index} mode={mode} maxDepth={depth} />
            {laserDot}
          </div>
        </div>
      )}

      {/* Notes drawer (audience view only — presenter view has its own pane). */}
      {!presenter && showNotes && notes && (
        <div
          className="max-h-40 shrink-0 overflow-y-auto whitespace-pre-wrap border-t p-4 text-sm"
          style={{ borderColor: "var(--pm-edge)", color: "var(--pm-ink-dim)" }}
        >
          {notes}
        </div>
      )}

      {/* Control bar. `flex-wrap` rather than a fixed row: presenting into a
          narrow surface is the refused-fullscreen case, and a control the
          presenter cannot reach is the defect this whole pass is about. */}
      <div
        className="flex shrink-0 flex-wrap items-center justify-center gap-3 p-3 text-xs"
        style={{ color: "var(--pm-ink-dim)" }}
      >
        <button onClick={retreat} disabled={atStart} className={btnCls} aria-label="Back (un-reveal / previous slide)">← Back</button>
        <span className="font-mono">
          {index + 1} / {last + 1}
          {curMax > 1 && (
            <span className="ml-2" style={{ color: "var(--pm-ink-faint)" }}>detail {shownDepth}/{curMax}</span>
          )}
        </span>
        <button onClick={advance} disabled={atEnd} className={btnCls} aria-label="Forward (reveal / next slide)">Forward →</button>
        <button onClick={() => setPresenter((s) => !s)} className={cn(btnCls, presenter && onBtn)} aria-pressed={presenter} aria-label="Toggle presenter view">Presenter</button>
        <button onClick={() => setOverview(true)} className={btnCls} aria-label="Open slide overview">Overview</button>
        <button onClick={() => setLaser((s) => !s)} className={cn(btnCls, laser && onBtn)} aria-pressed={laser} aria-label="Toggle laser pointer">Laser</button>
        {(() => {
          const active = (mode ?? deck.transition?.mode ?? "morph") === "push";
          return (
            <button
              onClick={() => setMode(active ? "morph" : "push")}
              className={cn(btnCls, active && onBtn)}
              aria-pressed={active}
              aria-label="Toggle push transition"
            >
              {active ? "Push" : "Morph"}
            </button>
          );
        })()}
        <span className="hidden @2xl:inline" style={{ color: "var(--pm-ink-faint)" }}>
          +/- detail · P presenter · O grid · L laser · T push · B/W blank · N notes · Esc exit
        </span>
        {/* Said out loud rather than assumed: if the browser refused full
            screen, the presenter can see that this is the pane-sized fallback
            instead of wondering why the deck is small. */}
        {!full && (
          <span style={{ color: "var(--pm-ink-faint)" }} title="The browser did not grant full screen">
            in-pane
          </span>
        )}
        <button onClick={onExit} className={btnCls} aria-label="Exit presentation">Exit</button>
      </div>
    </div>
  );
}
