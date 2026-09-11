// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism deck v2 — the runtime application (renders inside the deck
//   shadow root via its own React root, see deck-shadow-host.tsx). PRISM v4 W4
//   viewer: axis-frozen 2D navigation (TOPICS DOWN ↑/↓ · DEPTH RIGHT →/← with
//   wrap + non-silent terminal), persistent orientation (mini-map + 1-indexed
//   "topic X/N · L k/4" counter + hero key-legend + named breadcrumb), axis-true
//   transitions (translateY topic / translateX depth, reduced-motion aware),
//   drill-down peek, accuracy rendering (deficit/synth/fallback/count/gap), the
//   deck controls (brand switch · retailor · A/B swap · export · delete), ZoomStage
//   per cell, L4 doc-view, URL-hash deep links, and zero dead keys.
// index: navigation helpers | hash | DeckApp
// AGENT_HEADER_END -->
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { BrandId, DeckDoc, DeckLevel, GridCoord, GridRow, Slide, DeckCell } from "./deck-types";
import { DECK_LEVEL_LABELS, DEEPEST_LEVEL, accuracyForCell } from "./deck-types";
import { SlideView } from "./archetypes";
import { ComposedCell } from "./composed-cell";
import { CellVisualView, ProvenanceButton, LensTabs, BrandLogoMark, polarityFromBg } from "./deck-extras";
import { ZoomStage } from "./zoom-stage";
import { DeckMatrix } from "./deck-matrix";
import { DeckMiniMap } from "./deck-minimap";
import { AccuracyStrip } from "./deck-accuracy";
import { L3DocView } from "./l3-docview";
import { revealApi } from "./kit-assets";
import {
  type NavAxis, type NavDir, existingLevels, isLevel, jumpToTopic,
  lastCoord, navigate, positionLabel,
} from "./deck-nav";

const BRANDS: BrandId[] = ["okuro", "northwind", "meridian"];

export interface DeckAppProps {
  deck: DeckDoc;
  /** Persist an A/B choice (edit mode). row is "hero" or a level; col indexes the
   *  topic (ignored for hero). pick 0 = primary, 1..N = alternates[pick-1]. */
  onPickAlt?: (row: "hero" | DeckLevel, col: number, pick: number) => void;
  /** Deck controls (wired by the page that owns the deck + API). */
  onBrandSwitch?: (brand: BrandId) => void;   // live restyle (visuals only)
  onRetailor?: () => void;                     // entry to audience re-tailoring
  onExport?: () => void;                        // standalone-HTML export
  onDelete?: () => void;                        // delete this deck (confirmed here)
  /** Whether deck-management controls (export/delete/retailor/brand) are live —
   *  false for the committed demo fixture (no backend id). */
  canManage?: boolean;
}

// ── hash <-> coord (deep link per cell; schema unchanged for back-compat) ─────

const VALID_ROWS: GridRow[] = ["hero", "index", "L0", "L1", "L2", "L3"];
function parseHash(deck: DeckDoc): { coord: GridCoord; grid: boolean } | null {
  const h = window.location.hash.replace(/^#/, "");
  if (!h) return null;
  const p = new URLSearchParams(h);
  const d = p.get("d");
  const t = p.get("t");
  if (!d || !VALID_ROWS.includes(d as GridRow)) return null;
  const col = Math.max(0, Math.min(deck.topics.length - 1, parseInt(t ?? "0", 10) || 0));
  return { coord: { row: d as GridRow, col }, grid: p.get("m") === "grid" };
}
function writeHash(c: GridCoord, grid: boolean) {
  const p = new URLSearchParams();
  p.set("d", c.row);
  if (isLevel(c.row) || c.row === "index") p.set("t", String(c.col));
  if (grid) p.set("m", "grid");
  const next = "#" + p.toString();
  if (next !== window.location.hash) window.history.replaceState(null, "", next);
}

function cellSlides(cell: DeckCell): Slide[] {
  return [cell.slide, ...(cell.alternates ?? [])];
}

const KEY_LEGEND = "↑↓ topics · ←→ depth · g map · p peek · ? help";

export function DeckApp({
  deck, onPickAlt, onBrandSwitch, onRetailor, onExport, onDelete, canManage = false,
}: DeckAppProps) {
  const initial = useMemo(
    () => parseHash(deck) ?? { coord: { row: "hero", col: 0 } as GridCoord, grid: false },
    [deck],
  );
  const [coord, setCoordState] = useState<GridCoord>(initial.coord);
  const [gridMode, setGridMode] = useState<boolean>(initial.grid);
  const [editMode, setEditMode] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [peekLevel, setPeekLevel] = useState<DeckLevel | null>(null);
  const [lensState, setLensState] = useState<Record<string, number>>({});
  const [activeDeckLens, setActiveDeckLens] = useState<string | null>(null);
  const [logoPolarity, setLogoPolarity] = useState<"dark" | "light">("dark");
  const [picks, setPicks] = useState<Record<string, number>>({});
  const [animAxis, setAnimAxis] = useState<NavAxis>(null);
  const [animSeq, setAnimSeq] = useState(0);
  const [hint, setHint] = useState<{ msg: string; sticky?: boolean } | null>(null);
  const stageWrapRef = useRef<HTMLDivElement>(null);
  const rootElRef = useRef<HTMLDivElement>(null);
  const touch = useRef<{ x: number; y: number } | null>(null);
  const hintTimer = useRef<number | null>(null);

  const topic = deck.topics[coord.col];

  const flash = useCallback((msg: string, sticky = false) => {
    setHint({ msg, sticky });
    if (hintTimer.current) window.clearTimeout(hintTimer.current);
    if (!sticky) hintTimer.current = window.setTimeout(() => setHint(null), 2200);
  }, []);

  /** All coordinate changes flow through here so the transition axis + reveal fire
   *  consistently whatever the source (keys, minimap, breadcrumb, accuracy chip). */
  const go = useCallback((c: GridCoord, axis: NavAxis = null) => {
    setCoordState((prev) => {
      let ax = axis;
      if (ax === null) {
        if (isLevel(prev.row) && isLevel(c.row)) ax = c.col !== prev.col ? "y" : c.row !== prev.row ? "x" : null;
      }
      setAnimAxis(ax);
      setAnimSeq((s) => s + 1);
      return c;
    });
  }, []);

  // keep the URL hash in sync (deep-link per cell)
  useEffect(() => { writeHash(coord, gridMode); }, [coord, gridMode]);

  // respond to back/forward + external hash edits
  useEffect(() => {
    const onHash = () => {
      const parsed = parseHash(deck);
      if (parsed) { setCoordState(parsed.coord); setGridMode(parsed.grid); }
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [deck]);

  const doNav = useCallback((dir: NavDir) => {
    setCoordState((prev) => {
      const r = navigate(deck, prev, dir);
      if (r.event === "terminal") { flash("End of deck — press Home to return to the cover", true); return prev; }
      if (r.event === "top") { flash("Top of deck"); return prev; }
      if (r.event === "bottom") { flash("Last topic"); return prev; }
      setAnimAxis(r.axis);
      setAnimSeq((s) => s + 1);
      return r.coord;
    });
    setPeekLevel(null);
  }, [deck, flash]);

  // global keyboard navigation — ZERO dead keys (every key acts or visibly explains)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (e.metaKey || e.ctrlKey || e.altKey) return; // leave OS/browser shortcuts alone
      const k = e.key;
      let handled = true;

      if (k === "ArrowUp" || k === "k") doNav("up");
      else if (k === "ArrowDown" || k === "j") doNav("down");
      else if (k === "ArrowRight" || k === "PageDown" || k === " " || k === "Enter") doNav("right");
      else if (k === "ArrowLeft" || k === "PageUp" || k === "Backspace") doNav("left");
      else if (k === "Home") go({ row: "hero", col: 0 });
      else if (k === "End") go(lastCoord(deck));
      else if (k === "g" || k === "G") setGridMode((v) => !v);
      else if (k === "e" || k === "E") setEditMode((v) => !v);
      else if (k === "p" || k === "P") setPeekLevel((cur) => (cur ? null : nextDeeper(deck, coord)));
      else if (k === "b" || k === "B") { if (canManage && onBrandSwitch) cycleBrand(); else flash("Brand switch unavailable on the demo deck"); }
      else if (k === "l" || k === "L") { if (deck.lenses?.length) cycleLens(); else flash("No perspectives on this deck"); }
      else if (k === "?") setHelpOpen((v) => !v);
      else if (k === "Escape") {
        if (helpOpen) setHelpOpen(false);
        else if (peekLevel) setPeekLevel(null);
        else if (gridMode) setGridMode(false);
        else go({ row: "hero", col: 0 });
      } else if (/^[1-9]$/.test(k)) {
        const target = jumpToTopic(deck, coord, parseInt(k, 10) - 1);
        if (target) { go(target); setGridMode(false); }
        else flash(`No topic ${k}`);
      } else {
        handled = false;
        // never a silent dead key: any unmapped key explains the grammar
        if (k.length === 1 || k === "Tab") flash(KEY_LEGEND);
      }
      if (handled) e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deck, coord, gridMode, helpOpen, peekLevel, canManage]);

  const cycleBrand = useCallback(() => {
    const i = BRANDS.indexOf(deck.brand);
    const next = BRANDS[(i + 1) % BRANDS.length]!;
    onBrandSwitch?.(next);
    flash(`Brand → ${next} (look only — retailor to change the voice)`);
  }, [deck.brand, onBrandSwitch, flash]);

  // Multi-perspective: cycle Overview → each lens → Overview. Overlay is text-only.
  const cycleLens = useCallback(() => {
    const ids = (deck.lenses ?? []).map((l) => l.id);
    setActiveDeckLens((cur) => {
      const i = cur === null ? -1 : ids.indexOf(cur);
      const next = i + 1 >= ids.length ? null : ids[i + 1]!;
      const label = next === null ? "Overview"
        : deck.lenses?.find((l) => l.id === next)?.persona.name ?? next;
      flash(`Perspective → ${label}`);
      return next;
    });
  }, [deck.lenses, flash]);

  // arm reveal on focused slide mount (hero animates; reduced-motion respected).
  useEffect(() => {
    if (gridMode) return;
    const wrap = stageWrapRef.current;
    if (!wrap) return;
    revealApi()?.scan(wrap);
    const t = window.setTimeout(() => revealApi()?.revealAll(wrap), 600);
    return () => window.clearTimeout(t);
  }, [coord, gridMode, animSeq]);

  // luminance-aware logo: read the deck canvas background and pick the ink polarity
  // (dark canvas → light logo, light canvas → dark logo). Re-measures on brand switch.
  useEffect(() => {
    if (!deck.brandLogo) return;
    const el = rootElRef.current;
    if (!el) return;
    let node: HTMLElement | null = el;
    let bg = "";
    while (node) {
      const c = getComputedStyle(node).backgroundColor;
      if (c && c !== "transparent" && !/rgba\([^)]*,\s*0\s*\)/.test(c)) { bg = c; break; }
      node = node.parentElement;
    }
    setLogoPolarity(polarityFromBg(bg));
  }, [deck.brand, deck.brandLogo]);

  // swipe (touch) — both axes, axis-true
  const onTouchStart = (e: React.TouchEvent) => {
    const t = e.touches[0];
    if (t) touch.current = { x: t.clientX, y: t.clientY };
  };
  const onTouchEnd = (e: React.TouchEvent) => {
    const s = touch.current;
    const t = e.changedTouches[0];
    if (!s || !t) return;
    const dx = t.clientX - s.x, dy = t.clientY - s.y, AB = 48;
    if (Math.abs(dx) < AB && Math.abs(dy) < AB) return;
    if (Math.abs(dx) > Math.abs(dy)) doNav(dx < 0 ? "right" : "left");
    else doNav(dy < 0 ? "down" : "up");
    touch.current = null;
  };

  // ── A/B ──
  const pickKey = (row: "hero" | DeckLevel, col: number) => `${row}-${col}`;
  const getPick = (row: "hero" | DeckLevel, col: number, cell: DeckCell) =>
    picks[pickKey(row, col)] ?? cell.pick ?? 0;
  const setPick = (row: "hero" | DeckLevel, col: number, _cell: DeckCell, pick: number) => {
    setPicks((p) => ({ ...p, [pickKey(row, col)]: pick }));
    onPickAlt?.(row, col, pick);
  };

  const lensKey = `${coord.row}-${coord.col}`;
  const setLens = (i: number) => setLensState((s) => ({ ...s, [lensKey]: i }));

  // ── render one focused stage (hero or a level cell) ──
  function renderStage(row: "hero" | DeckLevel, col: number, cell: DeckCell, opts?: { hero?: boolean }) {
    const slides = cellSlides(cell);
    const pick = Math.min(getPick(row, col, cell), slides.length - 1);
    const baseSlide = slides[pick] ?? slides[0]!;
    // Phase D: an active lens overlay (text-only rewrite) wins over the A/B base.
    const slide = (activeDeckLens && cell.lensSlides?.[activeDeckLens])
      ? cell.lensSlides[activeDeckLens]!
      : baseSlide;
    const lensActive = !!(activeDeckLens && cell.lensSlides?.[activeDeckLens]);
    const showAB = editMode && slides.length > 1 && !lensActive;
    const units = isLevel(row) ? accuracyForCell(deck.accuracy, row, col) : [];
    return (
      <>
        <ZoomStage controls keyboard>
          {cell.composed && !lensActive
            ? <ComposedCell composed={cell.composed} variant={pick > 0 ? 1 : 0} />
            : <SlideView slide={slide} activeLens={lensState[lensKey] ?? 0} onLens={setLens} />}
        </ZoomStage>
        {cell.visual ? <CellVisualView visual={cell.visual} /> : null}
        {cell.provenance?.length ? <ProvenanceButton items={cell.provenance} /> : null}
        {opts?.hero ? (
          <div className="deck-hero-legend" data-testid="deck-hero-legend">
            <span className="hl-keys">↓ topics · → deeper · <kbd>g</kbd> grid · <kbd>?</kbd> help</span>
            <button className="hl-begin" onClick={() => go(lastCoordEntry())}>Enter to begin →</button>
          </div>
        ) : null}
        {units.length ? (
          <AccuracyStrip units={units} onDrillToL4={() => go({ row: DEEPEST_LEVEL, col }, "x")} />
        ) : null}
        {showAB ? (
          <div className="deck-ab" data-testid="deck-ab">
            <span className="deck-ab-label">A/B</span>
            {slides.map((_, i) => (
              <button key={i} className={i === pick ? "active" : ""} data-testid={`ab-${i}`}
                onClick={() => setPick(row, col, cell, i)} title={i === 0 ? "Primary" : `Alternate ${i}`}>
                {i === 0 ? "A" : `B${i}`}
              </button>
            ))}
          </div>
        ) : null}
      </>
    );
  }
  const lastCoordEntry = useCallback((): GridCoord => {
    const t0 = deck.topics[0];
    const lvl = t0 ? existingLevels(t0)[0] : null;
    return lvl ? { row: lvl, col: 0 } : { row: "index", col: 0 };
  }, [deck]);

  // ── body by coord.row ──
  let body: React.ReactNode;
  if (coord.row === "hero") {
    body = renderStage("hero", 0, deck.hero, { hero: true });
  } else if (coord.row === "index") {
    body = (
      <div className="deck-index-wrap" data-testid="deck-index">
        <div className="deck-index-title">Index · topic × depth</div>
        <DeckMatrix deck={deck} current={coord} onNavigate={(c) => go(c)} />
      </div>
    );
  } else if (coord.row === "L3") {
    const l3 = topic?.l3;
    body = l3 ? (
      <div className="deck-docwrap" data-testid="deck-l3">
        <L3DocView doc={l3} onNavigateTopic={(topicId) => {
          const i = deck.topics.findIndex((t) => t.id === topicId);
          if (i >= 0) go({ row: "L3", col: i }, "y");
        }} />
      </div>
    ) : <div className="deck-empty">No L4 document for this topic.</div>;
  } else {
    const cell = topic?.levels[coord.row];
    body = cell ? renderStage(coord.row, coord.col, cell) : <div className="deck-empty">No {DECK_LEVEL_LABELS[coord.row]} slide.</div>;
  }

  const pos = positionLabel(deck, coord);
  const crumbLevel = isLevel(coord.row) ? DECK_LEVEL_LABELS[coord.row] : coord.row === "index" ? "Index" : "Hero";
  const isStage = coord.row === "hero" || (isLevel(coord.row) && coord.row !== "L3");

  return (
    <div className="deck-root" data-testid="deck-root" ref={rootElRef}>
      <div className="deck-topbar">
        <span className="deck-title">{deck.title}</span>
        <span className="deck-breadcrumb" data-testid="deck-breadcrumb">
          <span className="deck-crumb" onClick={() => go({ row: "hero", col: 0 })}>Hero</span>
          <span className="deck-crumb-sep">›</span>
          <span className={`deck-crumb${coord.row === "index" ? " current" : ""}`}
            onClick={() => go({ row: "index", col: coord.col })}>
            {coord.row === "index" ? "Index" : isLevel(coord.row) ? (topic?.title ?? "—") : "—"}
          </span>
          {isLevel(coord.row) ? (
            <>
              <span className="deck-crumb-sep">›</span>
              <span className="deck-crumb current">{crumbLevel}</span>
            </>
          ) : null}
        </span>
        {pos ? (
          <span className="deck-counter" data-testid="deck-counter">
            topic {pos.topic}/{pos.topics} · L {pos.level}/{pos.levels}
          </span>
        ) : null}
        <span className="deck-spacer" />
        <div className="deck-btns">
          {canManage && onBrandSwitch ? (
            <label className="deck-brandsel" title="Switches the LOOK — the voice follows the audience; retailor to change it">
              brand
              <select data-testid="brand-select" value={deck.brand}
                onChange={(e) => { onBrandSwitch(e.target.value as BrandId); flash(`Brand → ${e.target.value} (look only)`); }}>
                {BRANDS.map((b) => <option key={b} value={b}>{b}</option>)}
              </select>
            </label>
          ) : null}
          <button className="deck-btn" data-testid="btn-peek" title="Peek one level deeper (p)"
            onClick={() => setPeekLevel((cur) => (cur ? null : nextDeeper(deck, coord)))}>
            Peek <kbd>p</kbd>
          </button>
          <button className={`deck-btn${gridMode ? " active" : ""}`} data-testid="btn-grid"
            onClick={() => setGridMode((v) => !v)}>Grid <kbd>g</kbd></button>
          <button className={`deck-btn${editMode ? " active" : ""}`} data-testid="btn-edit"
            onClick={() => setEditMode((v) => !v)}>Edit <kbd>e</kbd></button>
          {canManage && onRetailor ? (
            <button className="deck-btn" data-testid="btn-retailor" title="Re-tailor this deck to another audience (voice + depth)"
              onClick={onRetailor}>Retailor</button>
          ) : null}
          {canManage && onExport ? (
            <button className="deck-btn" data-testid="btn-export" title="Download a self-contained HTML deck (opens offline, no server)"
              onClick={onExport}>Export</button>
          ) : null}
          {canManage && onDelete ? (
            <button className="deck-btn deck-btn-danger" data-testid="btn-delete" title="Delete this deck"
              onClick={() => { if (window.confirm(`Delete deck “${deck.title}”? This cannot be undone.`)) onDelete(); }}>
              Delete
            </button>
          ) : null}
          <button className="deck-btn" data-testid="btn-help" title="Keyboard help (?)"
            onClick={() => setHelpOpen((v) => !v)}>?</button>
        </div>
      </div>

      {deck.lenses?.length ? (
        <LensTabs lenses={deck.lenses} active={activeDeckLens} onSelect={setActiveDeckLens} />
      ) : null}

      <div className="deck-body">
        {deck.brandLogo ? <BrandLogoMark logo={deck.brandLogo} polarity={logoPolarity} /> : null}
        <div
          className="deck-stage-wrap deck-transition"
          data-anim={animAxis ?? "none"}
          data-testid="deck-transition"
          key={`${coord.row}-${coord.col}-${animSeq}`}
          ref={stageWrapRef}
          onTouchStart={onTouchStart}
          onTouchEnd={onTouchEnd}
        >
          {body}
        </div>

        {/* persistent orientation map — always on, de-modalised */}
        {isStage || coord.row === "L3" ? (
          <DeckMiniMap deck={deck} current={coord} onNavigate={(c) => go(c)} />
        ) : null}

        {/* drill-down peek — a deeper level without leaving the cell */}
        {peekLevel && isLevel(coord.row) ? (
          <PeekPanel deck={deck} col={coord.col} level={peekLevel}
            onClose={() => setPeekLevel(null)}
            onOpen={() => { go({ row: peekLevel, col: coord.col }, "x"); setPeekLevel(null); }} />
        ) : null}

        {gridMode ? (
          <div className="deck-grid-overlay" data-testid="grid-overlay">
            <div className="deck-grid-head">
              <h2>Grid overview · topics ↓ · depth →</h2>
              <span className="tiny muted" style={{ fontFamily: "var(--font-mono)" }}>click a cell · g to close</span>
            </div>
            <DeckMatrix deck={deck} current={coord} showHero
              onNavigate={(c) => { go(c); setGridMode(false); }} />
          </div>
        ) : null}

        {helpOpen ? (
          <div className="deck-help" data-testid="deck-help" onClick={() => setHelpOpen(false)}>
            <div className="deck-help-card" onClick={(e) => e.stopPropagation()}>
              <h3>Navigation</h3>
              <ul>
                <li><kbd>↑</kbd><kbd>↓</kbd> previous / next topic (same depth)</li>
                <li><kbd>→</kbd> deeper · <kbd>←</kbd> shallower (→ at L4 wraps to the next topic)</li>
                <li><kbd>Space</kbd>/<kbd>PageDn</kbd> advance in reading order</li>
                <li><kbd>1</kbd>–<kbd>9</kbd> jump to a topic (at the current depth)</li>
                <li><kbd>Home</kbd> cover · <kbd>End</kbd> last topic</li>
                <li><kbd>g</kbd> grid map · <kbd>p</kbd> peek deeper · <kbd>e</kbd> edit (A/B)</li>
                {deck.lenses?.length ? <li><kbd>l</kbd> cycle perspective (lens tabs)</li> : null}
                <li><kbd>Esc</kbd> close / back to cover</li>
              </ul>
            </div>
          </div>
        ) : null}

        {hint ? (
          <div className={`deck-hint${hint.sticky ? " sticky" : ""}`} data-testid="deck-hint" role="status">
            {hint.msg}
            {hint.sticky ? <button className="hint-dismiss" onClick={() => setHint(null)}>✕</button> : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

// ── peek helpers ──────────────────────────────────────────────────────────────

/** The next-deeper level that exists for the current topic (null if none). */
function nextDeeper(deck: DeckDoc, c: GridCoord): DeckLevel | null {
  if (!isLevel(c.row)) return null;
  const topic = deck.topics[c.col];
  if (!topic) return null;
  const ex = existingLevels(topic);
  const i = ex.indexOf(c.row);
  return i >= 0 && i < ex.length - 1 ? ex[i + 1]! : null;
}

function PeekPanel({ deck, col, level, onClose, onOpen }: {
  deck: DeckDoc; col: number; level: DeckLevel; onClose: () => void; onOpen: () => void;
}) {
  const topic = deck.topics[col];
  const cell = level === "L3" ? null : topic?.levels[level];
  const slide = cell?.slide ?? null;
  return (
    <div className="deck-peek" data-testid="deck-peek">
      <div className="deck-peek-head">
        <span className="deck-peek-title">Peek · {DECK_LEVEL_LABELS[level]}</span>
        <button className="deck-peek-open" onClick={onOpen}>Open ↗</button>
        <button className="deck-peek-close" onClick={onClose} aria-label="Close peek">✕</button>
      </div>
      <div className="deck-peek-body">
        {level === "L3" ? (
          topic?.l3 ? <div className="tiny muted" style={{ padding: 16 }}>{topic.l3.title} — {topic.l3.sections.length} sections. Open to read the full text.</div> : null
        ) : cell?.composed ? (
          <ZoomStage><ComposedCell composed={cell.composed} /></ZoomStage>
        ) : slide ? (
          <ZoomStage><SlideView slide={slide} /></ZoomStage>
        ) : <div className="deck-empty">Nothing deeper here.</div>}
      </div>
    </div>
  );
}
