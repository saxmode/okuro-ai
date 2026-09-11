// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism deck v2 — topic×depth matrix. One render path for both
//   the INDEX slide and the presenter GRID OVERVIEW (g). Axis-true to the viewer
//   (PRISM v4 §4, frozen): rows = TOPICS (down), columns = DEPTH L1–L4 (right),
//   cells = kit thumbnails (the SAME slide components, scale-transformed — kit
//   zoom-stage.css .thumb/.deck-matrix). Clicking a cell navigates to that grid
//   coordinate. Kit CSS is the styling authority.
// AGENT_HEADER_END -->
import { Fragment, useEffect, useRef } from "react";
import type { DeckDoc, DeckLevel, GridCoord } from "./deck-types";
import { DECK_LEVELS, DECK_LEVEL_LABELS, hasCell, resolveSlide } from "./deck-types";
import { SlideView } from "./archetypes";

const CANVAS_W = 1600;

interface DeckMatrixProps {
  deck: DeckDoc;
  current?: GridCoord | null;
  onNavigate: (coord: GridCoord) => void;
  /** Show the hero as a full-width thumbnail above the matrix (grid overview). */
  showHero?: boolean;
}

/** Scale a `.thumb`'s canvas to its width (top-left origin) — the kit's own
 *  thumbnail formula (board/zoom-stage.js scaleThumb), replicated for shadow
 *  nodes the kit auto-boot never sees. */
function useThumbScaling(rootRef: React.RefObject<HTMLElement | null>, dep: unknown) {
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    // Thumbnails are static previews: force every scroll-reveal element to its
    // shown state so archetypes that wrap content in `.reveal` (hero) paint —
    // the IntersectionObserver that arms reveal never fires for a thumbnail, so
    // without this a hero cell renders fully blank in the grid.
    root.querySelectorAll<HTMLElement>(".reveal").forEach((el) => {
      el.style.setProperty("--reveal-delay", "0ms");
      el.classList.add("in-view");
    });
    const thumbs = Array.from(root.querySelectorAll<HTMLElement>(".thumb"));
    const scale = (thumb: HTMLElement) => {
      const canvas = thumb.querySelector<HTMLElement>(".zoom-canvas");
      if (canvas) canvas.style.setProperty("--thumb-scale", String(thumb.clientWidth / CANVAS_W));
    };
    thumbs.forEach(scale);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => entries.forEach((e) => scale(e.target as HTMLElement)));
    thumbs.forEach((t) => ro.observe(t));
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dep]);
}

/** Compact doc-view preview for an L3 thumbnail — the doc title + section-heading
 *  TOC + the first section's opening prose, so an L3 cell paints as a recognizable
 *  document at grid scale (the reflow doc-view itself does not render at thumb
 *  scale). Kit classes only; the kit CSS styles them. */
function L3Preview({ l3 }: { l3: NonNullable<DeckDoc["topics"][number]["l3"]> }) {
  const headings = l3.sections.filter((s) => s.heading).slice(0, 5);
  const firstProse = l3.sections.find((s) => (s.md || "").trim())?.md?.trim() || "";
  return (
    <div className="slide" data-archetype="doc">
      <div className="slide-inner">
        <div className="slide-eyebrow">
          <span className="slide-num">L3</span> full document
        </div>
        <h1 className="slide-title">{l3.title}</h1>
        <ul className="bullet-list">
          {headings.map((s, i) => (
            <li className="bullet-row" key={s.id || i}>
              <div className="bullet-head">
                <span className="bullet-mark">{i + 1}</span>
                <div>
                  <p className="bullet-title">{s.heading}</p>
                </div>
              </div>
            </li>
          ))}
        </ul>
        {firstProse ? <p className="prose">{firstProse.slice(0, 320)}</p> : null}
      </div>
    </div>
  );
}

function Thumb({
  label,
  active,
  onClick,
  children,
}: {
  label: string;
  active?: boolean;
  onClick?: () => void;
  children?: React.ReactNode;
}) {
  return (
    <div
      className="thumb"
      style={active ? { borderColor: "var(--accent)" } : undefined}
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick?.();
        }
      }}
    >
      <div className="zoom-canvas">{children}</div>
      <div className="thumb-cap">
        <span>{label}</span>
      </div>
    </div>
  );
}

function EmptyCell() {
  return (
    <div
      className="thumb"
      aria-hidden
      style={{ cursor: "default", opacity: 0.35, display: "flex", alignItems: "center", justifyContent: "center" }}
    >
      <span className="tiny muted">—</span>
    </div>
  );
}

export function DeckMatrix({ deck, current, onNavigate, showHero = false }: DeckMatrixProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  useThumbScaling(rootRef, deck.id + (showHero ? "-hero" : ""));

  // Axis-true: columns = corner + depth L1..L4; each row is a topic.
  const cols = `120px repeat(${DECK_LEVELS.length}, 1fr)`;

  const levelThumb = (t: DeckDoc["topics"][number], col: number, level: DeckLevel) => {
    const key = `${level}-${t.id}`;
    if (!hasCell(t, level)) return <EmptyCell key={key} />;
    const active = current?.row === level && current?.col === col;
    const label = `${t.title} · ${DECK_LEVEL_LABELS[level]}`;
    if (level === "L3") {
      const fig = t.l3?.sections.find((s) => s.figure)?.figure;
      return (
        <Thumb key={key} label={label} active={active} onClick={() => onNavigate({ row: level, col })}>
          {fig ? <SlideView slide={fig.slide} /> : t.l3 ? <L3Preview l3={t.l3} /> : null}
        </Thumb>
      );
    }
    const cell = t.levels[level]!;
    return (
      <Thumb key={key} label={label} active={active} onClick={() => onNavigate({ row: level, col })}>
        <SlideView slide={resolveSlide(cell)} />
      </Thumb>
    );
  };

  return (
    <div ref={rootRef} className="deck-matrix" style={{ gridTemplateColumns: cols }}>
      {/* header row: corner + depth labels (L1..L4) */}
      <div className="matrix-rowlabel" />
      {DECK_LEVELS.map((l) => (
        <div className="matrix-collabel" key={l}>{DECK_LEVEL_LABELS[l]}</div>
      ))}

      {showHero ? (
        <>
          <div className="matrix-rowlabel">HERO</div>
          <Thumb label="Hero" active={current?.row === "hero"} onClick={() => onNavigate({ row: "hero", col: 0 })}>
            <SlideView slide={resolveSlide(deck.hero)} />
          </Thumb>
          {DECK_LEVELS.slice(1).map((l) => <div key={`hero-pad-${l}`} aria-hidden />)}
        </>
      ) : null}

      {deck.topics.map((t, col) => (
        <Fragment key={`row-${t.id}`}>
          <div className="matrix-rowlabel" title={t.title}>{col + 1}. {t.title}</div>
          {DECK_LEVELS.map((level: DeckLevel) => levelThumb(t, col, level))}
        </Fragment>
      ))}
    </div>
  );
}
