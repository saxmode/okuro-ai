// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism deck v2 — PRISM v4 W5 Phase D viewer extras. Four viewer
//   surfaces the base kit does not carry: (1) CellVisualView renders a topic's L1
//   REAL visual (data chart / count-stat / generated illustration) from claim data;
//   (2) ProvenanceButton surfaces a ref-carrying claim's verbatim source span in a
//   clickable popover; (3) LensTabs switches the deck's multi-perspective framing;
//   (4) BrandLogoMark places a luminance-aware brand logo in a corner. All chrome —
//   styled from kit tokens (deck-chrome.css), never restyling a kit slide.
// AGENT_HEADER_END -->
import { useState } from "react";
import type { CellVisual, Provenance, DeckLens, BrandLogo } from "./deck-types";

// ── L1 REAL visual (§3c orphan gate) ─────────────────────────────────────────

export function CellVisualView({ visual }: { visual: CellVisual }) {
  if (visual.kind === "illustration" && visual.svg) {
    return (
      <figure className="deck-visual deck-visual-illus" data-testid="cell-visual">
        <div className="dv-illus" dangerouslySetInnerHTML={{ __html: visual.svg }} />
        {visual.title ? <figcaption className="dv-cap">{visual.title}</figcaption> : null}
      </figure>
    );
  }
  if (visual.kind === "stat" && visual.stat) {
    return (
      <figure className="deck-visual deck-visual-stat" data-testid="cell-visual">
        <div className="dv-stat-num">{visual.stat.value}</div>
        <div className="dv-stat-lbl">{visual.stat.label}</div>
        {visual.stat.sub ? <div className="dv-stat-sub">{visual.stat.sub}</div> : null}
      </figure>
    );
  }
  const bars = visual.bars ?? [];
  return (
    <figure className="deck-visual deck-visual-bars" data-testid="cell-visual">
      {visual.title ? <figcaption className="dv-cap">{visual.title}</figcaption> : null}
      <div className="dv-bars">
        {bars.map((b, i) => (
          <div className={`dv-bar-row${b.state ? ` s-${b.state}` : ""}`} key={i}>
            <span className="dv-bar-label" title={b.label}>{b.label}</span>
            <span className="dv-bar-track">
              <span className="dv-bar-fill" style={{ width: `${Math.max(2, Math.min(100, b.value))}%` }} />
            </span>
            <span className="dv-bar-val">{b.display}</span>
          </div>
        ))}
      </div>
    </figure>
  );
}

// ── provenance affordance (§3a orphan gate) ──────────────────────────────────

export function ProvenanceButton({ items }: { items: Provenance[] }) {
  const [open, setOpen] = useState(false);
  if (!items.length) return null;
  return (
    <div className="deck-prov" data-testid="cell-provenance">
      <button className="deck-prov-btn" data-testid="prov-toggle"
        aria-expanded={open} onClick={() => setOpen((v) => !v)}
        title="Show the source spans this cell is grounded in">
        ◇ source{items.length > 1 ? ` (${items.length})` : ""}
      </button>
      {open ? (
        <div className="deck-prov-pop" data-testid="prov-popover" role="dialog">
          <div className="deck-prov-head">
            <span>Grounded in</span>
            <button className="deck-prov-x" onClick={() => setOpen(false)} aria-label="Close">✕</button>
          </div>
          <ul className="deck-prov-list">
            {items.map((p, i) => (
              <li className="deck-prov-item" key={i}>
                <blockquote className="deck-prov-quote">“{p.quote}”</blockquote>
                <a className="deck-prov-src" href={`/artifacts/${p.artifactId}`}
                   target="_blank" rel="noreferrer"
                   title={p.artifactId}>
                  {p.artifactTitle || p.artifactId}
                </a>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

// ── multi-perspective lens tabs (answer 10) ──────────────────────────────────

export function LensTabs({
  lenses, active, onSelect,
}: { lenses: DeckLens[]; active: string | null; onSelect: (id: string | null) => void }) {
  if (!lenses.length) return null;
  return (
    <div className="deck-lenstabs" data-testid="deck-lenstabs" role="tablist" aria-label="Perspective">
      <button role="tab" aria-selected={active === null}
        className={`deck-lenstab${active === null ? " active" : ""}`}
        data-testid="lenstab-overview" onClick={() => onSelect(null)}>
        Overview
      </button>
      {lenses.map((l) => (
        <button role="tab" key={l.id} aria-selected={active === l.id}
          className={`deck-lenstab${active === l.id ? " active" : ""}`}
          data-testid={`lenstab-${l.id}`} onClick={() => onSelect(l.id)}
          title={l.persona.lens}>
          <span className={`lt-mark ${l.persona.mark}`} aria-hidden />
          {l.persona.name}
        </button>
      ))}
    </div>
  );
}

// ── luminance-aware brand logo (§3c orphan gate) ─────────────────────────────

export function BrandLogoMark({ logo, polarity }: { logo: BrandLogo; polarity: "dark" | "light" }) {
  const svg = polarity === "dark" ? logo.dark : logo.light;
  const corner = logo.corner ?? "tl";
  return (
    <div className={`deck-logo deck-logo-${corner}`} data-testid="deck-logo"
      data-polarity={polarity} aria-hidden
      dangerouslySetInnerHTML={{ __html: svg }} />
  );
}

/** Relative luminance of a CSS rgb(a) color string → "dark" | "light" background.
 *  Defaults to "dark" when the color is unreadable/transparent (board decks are
 *  dark), so the logo always resolves to a legible ink. */
export function polarityFromBg(bg: string | null | undefined): "dark" | "light" {
  if (!bg) return "dark";
  const m = bg.match(/rgba?\(([^)]+)\)/);
  if (!m) return "dark";
  const parts = m[1]!.split(",").map((s) => parseFloat(s.trim()));
  const [r, g, b, a] = parts;
  if (a !== undefined && a < 0.1) return "dark";           // transparent → assume dark canvas
  const lin = (c: number) => {
    const s = (c ?? 0) / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  const L = 0.2126 * lin(r ?? 0) + 0.7152 * lin(g ?? 0) + 0.0722 * lin(b ?? 0);
  return L < 0.5 ? "dark" : "light";
}
