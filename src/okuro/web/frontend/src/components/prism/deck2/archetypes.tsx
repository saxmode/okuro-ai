// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism deck v2 — archetype renderers. Each component emits the
//   kit's EXACT HTML structure + class names (src/okuro/prism/kit/board/
//   archetypes/*.html) from typed slot data. The kit CSS (injected into the
//   deck's shadow root) is the sole styling authority — these renderers add ZERO
//   styles beyond the inline geometry the kit templates themselves carry. Keep
//   markup byte-faithful to the templates so the runtime matches the specimen
//   gallery pixel-for-pixel. `SlideView` dispatches on `slide.archetype`.
// AGENT_HEADER_END -->
import { useState } from "react";
import type {
  Slide,
  Eyebrow as EyebrowT,
  TitleRun,
  Callout as CalloutT,
  BulletRow,
  HeroSlots,
  LensReframeSlots,
  DisclosureListSlots,
  ScenarioBeatsSlots,
  DecisionMatrixSlots,
  FrameworkTilesSlots,
  BinaryChoiceSlots,
  ClassificationTableSlots,
  FlowSequenceSlots,
  ProportionBarsSlots,
  CardSetSlots,
  AskCtaSlots,
} from "./deck-types";

// ── shared primitives ───────────────────────────────────────────────────────

function Eyebrow({ e }: { e: EyebrowT }) {
  return (
    <div className="slide-eyebrow" data-slot="eyebrow">
      {e.num ? <span className="slide-num">{e.num}</span> : null} {e.text}
    </div>
  );
}

function Title({ runs }: { runs: TitleRun[] }) {
  return (
    <h1 className="slide-title" data-slot="title">
      {runs.map((r, i) =>
        r.accent ? (
          <span className="accent" key={i}>
            {r.text}
          </span>
        ) : (
          <span key={i}>{r.text}</span>
        ),
      )}
    </h1>
  );
}

function Callout({ c, slot }: { c: CalloutT; slot?: string }) {
  const variant = c.variant ?? "info";
  return (
    <div className={`callout ${variant}`} data-slot={slot}>
      {c.label ? <div className="callout-label">{c.label}</div> : null}
      <p>{c.body}</p>
    </div>
  );
}

/** A disclosure/bullet row list (shared by disclosure-list + lens panes). Rows
 *  with a body expand on click — the kit hides `.bullet-body` until `.open` and
 *  provides the cursor/chevron affordance; the runtime owns the toggle. */
function BulletList({ rows, slot }: { rows: BulletRow[]; slot?: string }) {
  const [open, setOpen] = useState<Record<number, boolean>>({});
  const toggle = (i: number) => setOpen((o) => ({ ...o, [i]: !o[i] }));
  return (
    <ul className="bullet-list" data-slot={slot}>
      {rows.map((r, i) => (
        <li className={`bullet-row${open[i] ? " open" : ""}`} key={i}>
          <div
            className="bullet-head"
            onClick={r.body ? () => toggle(i) : undefined}
            role={r.body ? "button" : undefined}
            aria-expanded={r.body ? !!open[i] : undefined}
          >
            <span className={`bullet-mark${r.warn ? " warn" : ""}`}>{r.mark ?? i + 1}</span>
            <div>
              <p className="bullet-title">{r.title}</p>
              {r.sub ? <div className="bullet-sub">{r.sub}</div> : null}
            </div>
            {r.body ? (
              <span className="bullet-toggle">
                {r.toggle ?? "Detail"} <span className="chevron">▾</span>
              </span>
            ) : null}
          </div>
          {r.body ? (
            <div className="bullet-body">
              <p>{r.body}</p>
            </div>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

// ── 01 hero ─────────────────────────────────────────────────────────────────

function Hero({ s }: { s: HeroSlots }) {
  return (
    <section className="slide" data-archetype="hero">
      <div className="slide-inner">
        <div className="hero-inner reveal">
          <Eyebrow e={s.eyebrow} />
          <Title runs={s.title} />
          {s.lede ? (
            <p className="slide-lede" data-slot="lede">
              {s.lede}
            </p>
          ) : null}
          {s.personas?.length ? (
            <div className="persona-row" data-slot="personas">
              {s.personas.map((p, i) => (
                <div className="persona-pill" key={i}>
                  <span className={`persona-mark ${p.mark}`} />
                  <span className="persona-name">{p.name}</span>
                  <span className="persona-lens">{p.lens}</span>
                </div>
              ))}
            </div>
          ) : null}
          {s.meta?.length ? (
            <div className="hero-meta" data-slot="meta">
              {s.meta.map((m, i) => (
                <div className="hero-meta-item" key={i}>
                  <div className="label">{m.label}</div>
                  <div className="value">{m.value}</div>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}

// ── 02 lens-reframe (interactive tabs) ───────────────────────────────────────

function LensReframe({ s, activeLens, onLens }: { s: LensReframeSlots; activeLens: number; onLens: (i: number) => void }) {
  const active = Math.min(activeLens, s.lenses.length - 1);
  return (
    <section className="slide" data-archetype="lens-reframe">
      <div className="slide-inner">
        <Eyebrow e={s.eyebrow} />
        <Title runs={s.title} />
        <div className="lens-tabs" data-slot="lens-tabs" role="tablist">
          {s.lenses.map((l, i) => (
            <button
              className={`lens-tab${i === active ? " active" : ""}`}
              key={i}
              role="tab"
              aria-selected={i === active}
              onClick={() => onLens(i)}
            >
              <span className={`persona-mark ${l.persona.mark}`} />
              <span className="lens-label">
                <span className="lens-name">{l.persona.name}</span>
                <span className="lens-frame">{l.persona.lens}</span>
              </span>
            </button>
          ))}
        </div>
        {s.lenses.map((l, i) => (
          <div className={`lens-pane${i === active ? " active" : ""}`} data-slot="lens-pane" key={i} hidden={i !== active}>
            <BulletList rows={l.rows} />
          </div>
        ))}
      </div>
    </section>
  );
}

// ── 03 disclosure-list ───────────────────────────────────────────────────────

function DisclosureList({ s }: { s: DisclosureListSlots }) {
  return (
    <section className="slide" data-archetype="disclosure-list">
      <div className="slide-inner">
        <Eyebrow e={s.eyebrow} />
        <Title runs={s.title} />
        <BulletList rows={s.rows} slot="rows" />
      </div>
    </section>
  );
}

// ── 04 scenario-beats ─────────────────────────────────────────────────────────

function ScenarioBeats({ s }: { s: ScenarioBeatsSlots }) {
  return (
    <section className="slide" data-archetype="scenario-beats">
      <div className="slide-inner">
        <Eyebrow e={s.eyebrow} />
        <Title runs={s.title} />
        <ul className="beat-list" data-slot="beats">
          {s.beats.map((b, i) => (
            <li key={i}>
              <span className="t-mark">{b.mark}</span>
              <div className="t-body">
                {b.body}
                {b.who ? <span className="who">{b.who}</span> : null}
              </div>
            </li>
          ))}
        </ul>
        {s.callout ? <Callout c={s.callout} slot="callout" /> : null}
      </div>
    </section>
  );
}

// ── 05 decision-matrix ────────────────────────────────────────────────────────

function DecisionMatrix({ s }: { s: DecisionMatrixSlots }) {
  return (
    <section className="slide" data-archetype="decision-matrix">
      <div className="slide-inner">
        <Eyebrow e={s.eyebrow} />
        <Title runs={s.title} />
        <div className="option-grid" data-slot="options">
          {s.options.map((o, i) => (
            <div className={`option-card${o.recommended ? " recommended" : ""}`} key={i}>
              <div className="option-label">{o.label}</div>
              <h3>{o.title}</h3>
              {o.lines.map((ln, j) => (
                <div className="option-line" key={j}>
                  <span className="k">{ln.k}</span>
                  <span className="v">{ln.v}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

// ── 06 framework-tiles ────────────────────────────────────────────────────────

function FrameworkTiles({ s }: { s: FrameworkTilesSlots }) {
  const cols = s.tiles.length;
  return (
    <section className="slide" data-archetype="framework-tiles">
      <div className="slide-inner">
        <Eyebrow e={s.eyebrow} />
        <Title runs={s.title} />
        <div className="card-grid" data-slot="tiles" style={{ gridTemplateColumns: `repeat(${cols},1fr)` }}>
          {s.tiles.map((t, i) => (
            <div className="card" key={i}>
              <div className="card-label">{t.label}</div>
              <div className="card-value">{t.value}</div>
              {t.desc ? <div className="card-desc">{t.desc}</div> : null}
            </div>
          ))}
        </div>
        {s.table ? (
          <table className="prose" style={{ width: "100%" }} data-slot="table">
            <thead>
              <tr>
                {s.table.headers.map((h, i) => (
                  <th key={i}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {s.table.rows.map((row, i) => (
                <tr key={i}>
                  {row.map((cell, j) => (
                    <td key={j}>{cell.strong ? <strong>{cell.text}</strong> : cell.text}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </div>
    </section>
  );
}

// ── 07 binary-choice ──────────────────────────────────────────────────────────

function BinaryChoice({ s }: { s: BinaryChoiceSlots }) {
  return (
    <section className="slide" data-archetype="binary-choice">
      <div className="slide-inner">
        <Eyebrow e={s.eyebrow} />
        <Title runs={s.title} />
        <div className="card-grid" data-slot="paths" style={{ gridTemplateColumns: "repeat(2,1fr)" }}>
          {s.paths.map((p, i) => (
            <div
              className="card"
              key={i}
              style={{ borderLeft: `4px solid var(--${p.tone})` }}
            >
              <div className="card-eyebrow" style={p.tone === "warn" ? { color: "var(--warn-text)" } : undefined}>
                {p.eyebrow}
              </div>
              <h3>{p.title}</h3>
              <p>{p.body}</p>
              {p.footer ? <div className="card-footer">{p.footer}</div> : null}
            </div>
          ))}
        </div>
        {s.callout ? <Callout c={s.callout} slot="callout" /> : null}
      </div>
    </section>
  );
}

// ── 08 classification-table ───────────────────────────────────────────────────

function ClassificationTable({ s }: { s: ClassificationTableSlots }) {
  return (
    <section className="slide" data-archetype="classification-table">
      <div className="slide-inner">
        <Eyebrow e={s.eyebrow} />
        <Title runs={s.title} />
        {s.summary ? (
          <div className="verdict-callout" data-slot="callout">
            <span>{s.summary.text}</span>
            {s.summary.stats.map((st, i) => (
              <span className="vc-stat" key={i}>
                <span className="vc-dot" style={{ background: st.color }} />
                {st.text}
              </span>
            ))}
          </div>
        ) : null}
        <div className="verdict-table-wrap" data-slot="table">
          <table className="verdict-table">
            <thead>
              <tr data-slot="columns">
                {s.columns.map((c, i) => (
                  <th key={i}>{c.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {s.rows.map((r, i) => (
                <tr className={r.verdict ?? ""} key={i}>
                  {s.columns.map((c, ci) => {
                    const cell = r.cells[ci] ?? "";
                    if (c.role === "method") {
                      return (
                        <td key={ci}>
                          <span className={`method-badge ${cell.toLowerCase()}`}>{cell.toUpperCase()}</span>
                        </td>
                      );
                    }
                    if (c.role === "verdict") {
                      return (
                        <td key={ci}>
                          <span className={`verdict-badge ${r.verdict ?? ""}`}>{cell}</span>
                        </td>
                      );
                    }
                    return (
                      <td key={ci} className={ci === 0 ? "ep-path" : "ep-reason"}>
                        {cell}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

// ── 09 flow-sequence ──────────────────────────────────────────────────────────

function FlowSequence({ s }: { s: FlowSequenceSlots }) {
  return (
    <section className="slide" data-archetype="flow-sequence">
      <div className="slide-inner">
        <Eyebrow e={s.eyebrow} />
        <Title runs={s.title} />
        {s.lede ? (
          <p className="slide-lede" data-slot="lede">
            {s.lede}
          </p>
        ) : null}
        <div data-slot="steps">
          {s.steps.map((st, i) => (
            <div className="ceo-step" key={i}>
              <span className={`ceo-num ${st.actor}`}>{st.num}</span>
              <div className="ceo-what">{st.what}</div>
              {st.tool ? <span className="ceo-tool">{st.tool}</span> : null}
              {st.ms ? <span className="ceo-ms">{st.ms}</span> : null}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

// ── 10 proportion-bars ────────────────────────────────────────────────────────

function ProportionBars({ s }: { s: ProportionBarsSlots }) {
  return (
    <section className="slide" data-archetype="proportion-bars">
      <div className="slide-inner">
        <Eyebrow e={s.eyebrow} />
        <Title runs={s.title} />
        {s.lede ? (
          <p className="slide-lede" data-slot="lede">
            {s.lede}
          </p>
        ) : null}
        <div data-slot="rows" style={{ display: "flex", flexDirection: "column", gap: 18, marginTop: 8 }}>
          {s.rows.map((r, i) => (
            <div className="lat-seg-row" key={i} style={{ gridTemplateColumns: "220px 1fr 120px" }}>
              <span className="stage">{r.stage}</span>
              <span className="bar-cell">
                <span className={`token-bar ${r.state}`} style={{ width: `${r.pct}%` }} />
              </span>
              <span className="lat-e">{r.value}</span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

// ── 11 card-set ───────────────────────────────────────────────────────────────

function CardSet({ s }: { s: CardSetSlots }) {
  const cols = s.cards.length;
  return (
    <section className="slide" data-archetype="card-set">
      <div className="slide-inner">
        <Eyebrow e={s.eyebrow} />
        <Title runs={s.title} />
        {s.stats?.length ? (
          <div className="stat-row" data-slot="stats">
            {s.stats.map((st, i) => (
              <div className="stat-cell" key={i}>
                <div className={`stat-num${st.numState ? " " + st.numState : ""}`}>{st.num}</div>
                <div className="stat-lbl">{st.lbl}</div>
              </div>
            ))}
          </div>
        ) : null}
        <div className="card-grid" data-slot="cards" style={{ gridTemplateColumns: `repeat(${cols},1fr)` }}>
          {s.cards.map((c, i) => (
            <div className="cap-card" key={i}>
              {c.icon ? <div className="cap-card-icon">{c.icon}</div> : null}
              <div className="cap-card-name">{c.name}</div>
              {c.desc ? <div className="cap-card-desc">{c.desc}</div> : null}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

// ── 12 ask-cta ────────────────────────────────────────────────────────────────

function AskCta({ s }: { s: AskCtaSlots }) {
  return (
    <section className="slide tight" data-archetype="ask-cta">
      <div className="slide-inner">
        <Eyebrow e={s.eyebrow} />
        <Title runs={s.title} />
        <div className="ask-box" data-slot="ask">
          <div className="ask-box-title">{s.ask.title}</div>
          <ul>
            {s.ask.items.map((it, i) => (
              <li key={i}>{it}</li>
            ))}
          </ul>
        </div>
        {s.arch?.length ? (
          <div className="arch-diagram" data-slot="arch">
            {s.arch.map((a, i) => (
              <div className={`arch-item${a.ok ? " ok" : ""}`} key={i}>
                <div className="arch-item-label">{a.label}</div>
                <div className="arch-item-value">{a.value}</div>
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </section>
  );
}

// ── dispatcher ───────────────────────────────────────────────────────────────

export interface SlideViewProps {
  slide: Slide;
  /** lens-reframe interactive state (ignored by other archetypes). */
  activeLens?: number;
  onLens?: (i: number) => void;
}

/** Render one slide as the kit's HTML. Interactive archetypes (lens-reframe)
 *  accept controlled state; the rest are pure. */
export function SlideView({ slide, activeLens = 0, onLens }: SlideViewProps) {
  switch (slide.archetype) {
    case "hero":
      return <Hero s={slide.slots} />;
    case "lens-reframe":
      return <LensReframe s={slide.slots} activeLens={activeLens} onLens={onLens ?? (() => {})} />;
    case "disclosure-list":
      return <DisclosureList s={slide.slots} />;
    case "scenario-beats":
      return <ScenarioBeats s={slide.slots} />;
    case "decision-matrix":
      return <DecisionMatrix s={slide.slots} />;
    case "framework-tiles":
      return <FrameworkTiles s={slide.slots} />;
    case "binary-choice":
      return <BinaryChoice s={slide.slots} />;
    case "classification-table":
      return <ClassificationTable s={slide.slots} />;
    case "flow-sequence":
      return <FlowSequence s={slide.slots} />;
    case "proportion-bars":
      return <ProportionBars s={slide.slots} />;
    case "card-set":
      return <CardSet s={slide.slots} />;
    case "ask-cta":
      return <AskCta s={slide.slots} />;
    default: {
      // Exhaustiveness guard — a new archetype must add a case above.
      const _never: never = slide;
      return _never;
    }
  }
}
