import { useEffect, useRef, useState } from "react";
import type { Deck } from "./scene";
import { elementsMaxDepth } from "./scene";
import { SlideDeck } from "./slide-deck";
import { SlideThumb } from "./slide-thumb";

/**
 * PresentMode — fullscreen presentation overlay.
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
 * Presenter view splits the overlay into a large CURRENT slide, a smaller NEXT
 * preview, the current slide's speaker notes, an elapsed timer (mm:ss, resets),
 * and an i/n counter.
 */
export function PresentMode({ deck, onExit }: { deck: Deck; onExit: () => void }) {
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
  const last = deck.slides.length - 1;

  // Reveal level for the current slide's progressive disclosure. Starts at the
  // deck's stored default (a re-tailored terse variant lowers it) else 1 = glance.
  const slideMax = (i: number) => elementsMaxDepth(deck.slides[i]?.elements ?? []);
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
      else if (k === "t") setMode((m) => ((m ?? deck.transition.mode ?? "morph") === "push" ? "morph" : "push"));
      else if (k === "b") setBlank((b) => (b === "black" ? null : "black"));
      else if (k === "w") setBlank((b) => (b === "white" ? null : "white"));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
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
          background: "#ff4d6d",
          boxShadow: "0 0 12px 4px rgba(255,77,109,0.7)",
        }}
      />
    ) : null;

  const btnCls = "rounded border border-white/20 px-3 py-1 hover:border-white/40 disabled:opacity-30";

  return (
    <div className="fixed inset-0 z-[200] flex flex-col bg-black" role="dialog" aria-label="Presentation">
      {/* Blank screen overlay — press again or navigate to restore. */}
      {blank && (
        <button
          aria-label={`Blank ${blank} screen — click or press any key to restore`}
          onClick={() => setBlank(null)}
          className="absolute inset-0 z-[60]"
          style={{ background: blank === "black" ? "#000" : "#fff" }}
        />
      )}

      {/* Overview / jump grid. */}
      {overview && (
        <div className="absolute inset-0 z-50 overflow-auto bg-black/95 p-8" role="dialog" aria-label="Slide overview">
          <div className="mb-4 flex items-center justify-between text-sm text-white/70">
            <span>Jump to slide</span>
            <button onClick={() => setOverview(false)} className={btnCls} aria-label="Close overview">Close (Esc)</button>
          </div>
          <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-4">
            {deck.slides.map((s, i) => (
              <button
                key={s.id}
                onClick={() => { jumpTo(i); setOverview(false); }}
                aria-label={`Go to slide ${i + 1}`}
                aria-current={i === index}
                className={`relative rounded-lg border p-1 text-left transition ${i === index ? "border-accent" : "border-white/15 hover:border-accent/60"}`}
              >
                <SlideThumb deck={deck} slideIndex={i} width={210} />
                <span className="absolute bottom-1 right-2 rounded bg-black/70 px-1.5 text-xs text-white/80">{i + 1}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {presenter ? (
        /* ── Presenter view ─────────────────────────────────────────── */
        <div className="flex flex-1 flex-col gap-4 p-6 lg:flex-row">
          <div className="flex flex-[2] flex-col gap-2">
            <span className="text-xs uppercase tracking-wide text-white/40">Current</span>
            <div className="relative" onMouseMove={onStageMove}>
              <SlideDeck deck={deck} index={index} mode={mode} maxDepth={depth} />
              {laserDot}
            </div>
          </div>
          <div className="flex flex-1 flex-col gap-4">
            <div className="flex flex-col gap-2">
              <span className="text-xs uppercase tracking-wide text-white/40">Next</span>
              {index < last ? (
                <SlideThumb deck={deck} slideIndex={index + 1} width={380} />
              ) : (
                <div className="rounded border border-white/10 p-6 text-sm text-white/40">End of deck</div>
              )}
            </div>
            <div className="flex items-center gap-4 text-sm text-white/70">
              <span className="font-mono text-2xl tabular-nums text-white">{mmss(elapsed)}</span>
              <button onClick={resetTimer} className={btnCls} aria-label="Reset timer">Reset</button>
              <span className="ml-auto font-mono">
                {index + 1} / {last + 1}
                {curMax > 1 && <span className="ml-2 text-white/40">detail {shownDepth}/{curMax}</span>}
              </span>
            </div>
            <div className="flex-1 overflow-y-auto rounded border border-white/10 bg-white/[0.03] p-3 text-sm leading-relaxed text-white/80 whitespace-pre-wrap">
              {notes || <span className="text-white/30">No notes for this slide.</span>}
            </div>
          </div>
        </div>
      ) : (
        /* ── Audience view ──────────────────────────────────────────── */
        <div className="flex flex-1 items-center justify-center p-6">
          <div className="relative w-full max-w-[1600px]" onMouseMove={onStageMove}>
            <SlideDeck deck={deck} index={index} mode={mode} maxDepth={depth} />
            {laserDot}
          </div>
        </div>
      )}

      {/* Notes drawer (audience view only — presenter view has its own pane). */}
      {!presenter && showNotes && notes && (
        <div className="max-h-40 overflow-y-auto border-t border-white/15 bg-black/80 p-4 text-sm text-white/80 whitespace-pre-wrap">
          {notes}
        </div>
      )}

      {/* Control bar. */}
      <div className="flex flex-wrap items-center justify-center gap-3 p-3 text-xs text-white/50">
        <button onClick={retreat} disabled={atStart} className={btnCls} aria-label="Back (un-reveal / previous slide)">← Back</button>
        <span className="font-mono">
          {index + 1} / {last + 1}
          {curMax > 1 && <span className="ml-2 text-white/40">detail {shownDepth}/{curMax}</span>}
        </span>
        <button onClick={advance} disabled={atEnd} className={btnCls} aria-label="Forward (reveal / next slide)">Forward →</button>
        <button onClick={() => setPresenter((s) => !s)} className={`${btnCls} ${presenter ? "border-accent text-accent" : ""}`} aria-pressed={presenter} aria-label="Toggle presenter view">Presenter</button>
        <button onClick={() => setOverview(true)} className={btnCls} aria-label="Open slide overview">Overview</button>
        <button onClick={() => setLaser((s) => !s)} className={`${btnCls} ${laser ? "border-[#ff4d6d] text-[#ff4d6d]" : ""}`} aria-pressed={laser} aria-label="Toggle laser pointer">Laser</button>
        {(() => {
          const active = (mode ?? deck.transition.mode ?? "morph") === "push";
          return (
            <button
              onClick={() => setMode(active ? "morph" : "push")}
              className={`${btnCls} ${active ? "border-accent text-accent" : ""}`}
              aria-pressed={active}
              aria-label="Toggle push transition"
            >
              {active ? "Push" : "Morph"}
            </button>
          );
        })()}
        <span className="hidden sm:inline text-white/30">+/- detail · P presenter · O grid · L laser · T push · B/W blank · N notes · Esc exit</span>
        <button onClick={onExit} className={btnCls} aria-label="Exit presentation">Exit</button>
      </div>
    </div>
  );
}
