// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism SLIDE TEMPLATES (frontend) — render a backend-bound
//   template {id, slots} as a DESIGNED slide, themed entirely by per-brand CSS
//   tokens. The intentional-layout replacement for BlockGrid's flat packer.
// index: BRAND_TOKENS | brandStyle | TemplateSlide
// AGENT_HEADER_END -->
import "./templates.css";
import type { CSSProperties } from "react";
import { BlockView } from "@/components/prism/blocks";
import type { Block } from "@/lib/prism-api";

/** A backend-bound template: an id + the extracted slot data (okuro.prism.templates). */
export interface BoundTemplate { id: string; slots: Record<string, unknown> }

const MONO = '"JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace';
const SANS = '"Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

/** Per-brand design tokens → the --tpl-* CSS vars every template reads. A brand
 *  changes the ENTIRE look (colour, type, surface) without touching a template.
 *  Unknown brand → the noir default. */
const NOIR: Record<string, string> = {
  "--tpl-bg": "#0a0a0a", "--tpl-card": "#161616", "--tpl-subtle": "#1f1f1f",
  "--tpl-primary": "#f2f2f2", "--tpl-body": "#d0d0d0", "--tpl-secondary": "#a0a0a0",
  "--tpl-tertiary": "#707070", "--tpl-muted": "#555555",
  "--tpl-accent": "#22c55e", "--tpl-accent-dim": "rgba(34,197,94,0.22)", "--tpl-accent-soft": "rgba(34,197,94,0.08)",
  "--tpl-border": "#2a2a2a", "--tpl-border-strong": "#3a3a3a",
  "--tpl-font-body": MONO, "--tpl-font-mono": MONO,
};
/** Same surface as NOIR, sans body instead of mono. There used to be a
 *  hardcoded customer theme here carrying a customer's accent; okuro ships one
 *  brand, its own, and a user's brand comes from their brand registry. */
const NOIR_SANS: Record<string, string> = {
  ...NOIR,
  "--tpl-font-body": SANS,
};
const LIGHT: Record<string, string> = {
  "--tpl-bg": "#faf9f6", "--tpl-card": "#ffffff", "--tpl-subtle": "#ececec",
  "--tpl-primary": "#111111", "--tpl-body": "#333333", "--tpl-secondary": "#555555",
  "--tpl-tertiary": "#888888", "--tpl-muted": "#aaaaaa",
  "--tpl-accent": "#2b5cff", "--tpl-accent-dim": "rgba(43,92,255,0.20)", "--tpl-accent-soft": "rgba(43,92,255,0.06)",
  "--tpl-border": "#e4e4e4", "--tpl-border-strong": "#cccccc",
  "--tpl-font-body": SANS, "--tpl-font-mono": MONO,
};
export const BRAND_TOKENS: Record<string, Record<string, string>> = {
  okuro: NOIR,
  "the-machine-internal": NOIR,
  __sans: NOIR_SANS,
  __light: LIGHT,
};

/** CSS-var style object for a brand id (falls back to noir). */
export function brandStyle(brandId?: string, accent?: string): CSSProperties {
  const base = BRAND_TOKENS[brandId || ""] || NOIR;
  // A deck's resolved accent (from brand resolution) overrides the token accent.
  const style: Record<string, string> = { ...base };
  if (accent) { style["--tpl-accent"] = accent; }
  return style as CSSProperties;
}

const S = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : String(v));

function Tabs({ active }: { active: string }) {
  const cols = ["Cover", "Key Points", "Detail", "Full Doc"];
  return (
    <div className="tabs">
      {cols.map((c) => <span key={c} className={c === active ? "on" : "off"}>{c}</span>)}
    </div>
  );
}

/** Render a bound template as a designed slide. Unknown id → null (caller falls
 *  back to the legacy block stack). `active` marks the level tab. */
export function TemplateSlide({ template, active, accent }: { template: BoundTemplate; active: string; accent?: string }) {
  const s = template.slots || {};
  switch (template.id) {
    case "hero-block": {
      const block = s.block as Block | undefined;
      const lens = s.lens as Block | undefined;
      if (!block) return null;
      return (
        <div>
          <Tabs active={active} />
          {(s.title || s.claim) ? <h1 className="title">{S(s.title) || S(s.claim)}</h1> : null}
          {s.claim && s.title ? <p className="lede">{S(s.claim)}</p> : null}
          <div className="stage"><BlockView block={block} accent={accent} /></div>
          {lens ? <div className="lensrow"><div className="setlabel">Per audience</div><BlockView block={lens} accent={accent} /></div> : null}
        </div>
      );
    }
    case "cover":
    case "section": {
      const isSection = template.id === "section";
      const kicker = S(s.kicker); const line = S(s.line); const children = Number(s.children) || 0;
      return (
        <div className={`tpl-cover ${isSection ? "section" : ""}`}>
          <div className="tabs"><span className="on">{isSection ? "Section" : "Cover"}</span></div>
          {(kicker && kicker !== line) && <div className="eyebrow">{kicker}</div>}
          {isSection && !kicker && <div className="eyebrow">Section</div>}
          <div className="inner"><div className="big">{line}</div>
            {isSection && children > 0 && <div className="follow">{children} sub-topic{children === 1 ? "" : "s"} follow →</div>}
          </div>
        </div>
      );
    }
    case "metric-duel": {
      const metrics = (Array.isArray(s.metrics) ? s.metrics : []) as { k: string; bars: { who: string; display: string; ratio: number; win: boolean }[] }[];
      const setData = s.set as { columns?: number; cards?: { tag: string; name: string; desc: string }[] } | null;
      return (
        <div>
          <Tabs active={active} />
          {(s.title || s.claim) ? <h1 className="title">{S(s.title) || S(s.claim)}</h1> : null}
          <div className="duel">
            <div className="claim">{S(s.claim) || `${S(s.winner)} vs ${S(s.loser)}`}</div>
            <div className="metrics">
              {metrics.map((m, i) => (
                <div className="mrow" key={i}>
                  <div className="k">{m.k}</div>
                  <div className="mbars">
                    {m.bars.map((b, j) => (
                      <div className={`mbar ${b.win ? "win" : "lose"}`} key={j}>
                        <span className="who">{b.who}</span>
                        <span className="track"><span className="fill" style={{ width: `${Math.max(3, Math.round(b.ratio * 100))}%` }} /></span>
                        <span className="val">{b.display}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
          {setData?.cards?.length ? <CardSet columns={setData.columns} cards={setData.cards} /> : null}
        </div>
      );
    }
    case "card-grid": {
      const cards = (Array.isArray(s.cards) ? s.cards : []) as { tag: string; name: string; desc: string }[];
      return (
        <div>
          <Tabs active={active} />
          {(s.title || s.claim) ? <h1 className="title">{S(s.title) || S(s.claim)}</h1> : null}
          <CardSet columns={Number(s.columns) || cards.length} cards={cards} lead />
        </div>
      );
    }
    case "hero-stat": {
      const support = (Array.isArray(s.support) ? s.support : []) as { value: string; label: string }[];
      return (
        <div>
          <Tabs active={active} />
          {(s.title || s.claim) ? <h1 className="title">{S(s.title) || S(s.claim)}</h1> : null}
          <div className="herostat">
            <div><div className="big">{S(s.value)}</div><div className="lab">{S(s.label)}</div></div>
            <div>
              {s.sub ? <div className="claim">{S(s.sub)}</div> : null}
              {support.length ? (
                <div className="support">{support.map((x, i) => (
                  <div key={i}><div className="v">{x.value}</div><div className="l">{x.label}</div></div>
                ))}</div>
              ) : null}
            </div>
          </div>
        </div>
      );
    }
    default:
      return null;
  }
}

function CardSet({ columns, cards, lead }: { columns?: number; cards: { tag: string; name: string; desc: string }[]; lead?: boolean }) {
  const n = Math.max(2, Math.min(4, columns || cards.length));
  return (
    <>
      {!lead && <div className="setlabel">Details</div>}
      <div className={`cards c${n}`} style={lead ? { marginTop: 32 } : undefined}>
        {cards.map((c, i) => (
          <div className="card" key={i}>
            {c.tag && <span className="tag">{c.tag}</span>}
            <span className="name">{c.name}</span>
            {c.desc && <span className="desc">{c.desc}</span>}
          </div>
        ))}
      </div>
    </>
  );
}

/** True when a rung has a bound template the frontend can render. */
export function hasTemplate(t: unknown): t is BoundTemplate {
  return !!t && typeof t === "object" && typeof (t as BoundTemplate).id === "string";
}
