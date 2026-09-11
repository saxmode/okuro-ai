// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism block renderers — the module system. One BlockView per
//   typed, provenance-tagged block (text, heading, stat, table, callout, image,
//   diagram). Rung content is an ordered list of these; the viewer wraps each
//   in a scroll-reveal.
// index: imports | MermaidBlock | CALLOUT_STYLE | Provenance | BlockView
// AGENT_HEADER_END -->
import { AlertTriangle, ArrowDownRight, ArrowUpRight, GitBranch, Info, Minus, Sparkles, TriangleAlert } from "lucide-react";
import { Suspense, lazy, type ReactNode } from "react";

import ChartBlock from "@/components/prism/chart-block";
import SmallMultiples from "@/components/prism/small-multiples";
import { MermaidViewer } from "@/components/mermaid/mermaid-viewer";
import { Cards, Compare, LensTabs, Options, Timeline } from "@/components/prism/story-blocks";
import { Checklist, CodeBlock, Cta, Matrix, Meter, Spec, Steps } from "@/components/prism/data-blocks";
import { AssumptionLedger, DecisionRecord, RedTeam, RiskRegister, Tripwires } from "@/components/prism/sparring-blocks";
import { AudioBrief } from "@/components/prism/media-blocks";
import { Simulator } from "@/components/prism/interactive-blocks";
import { Frequency, Hops } from "@/components/prism/viz-blocks";
import { BiasCheck, Evidence, MessageProof } from "@/components/prism/rigor-blocks";
import { Quote, Statement } from "@/components/prism/typography-blocks";
import { Gallery, Visualizer } from "@/components/prism/visualizer-blocks";
import { MarkdownContent } from "@/components/ui/markdown-content";
import type { Block, Layout } from "@/lib/prism-api";

// Heavy renderers (ReactFlow + dagre) — kept out of the /prism initial bundle,
// loaded only when a facet actually carries one. Chart is dependency-free SVG,
// so it imports eagerly above.
const GraphBlock = lazy(() => import("@/components/prism/graph-block"));
const FlowEmbed = lazy(() => import("@/components/prism/flow-embed"));

/** Placeholder while a lazy rich-viz module resolves. */
function BlockFallback() {
  return <div className="rounded-lg border border-current/10 p-6 text-center text-sm opacity-40">Loading…</div>;
}

/** Composition layer — render a rung's flattened block list either as a grid
 *  (when a `layout` is present) or the default vertical stack. Bulletproof to a
 *  stale/partial layout: out-of-range cell indices are ignored and any block no
 *  cell references is appended full-width, so a layout can only improve a rung.
 *  Single column below `md`; the column template applies only on wider screens. */
export function BlockGrid({ blocks, layout, accent }: { blocks: Block[]; layout?: Layout; accent?: string }) {
  if (!blocks.length) return null;
  const rows = layout?.rows?.filter((r) => r?.cells?.length);
  if (!rows?.length) {
    return (
      <div className="space-y-3">
        {blocks.map((b, i) => <BlockView key={i} block={b} accent={accent} />)}
      </div>
    );
  }
  const used = new Set<number>();
  const gridRows = rows.map((row, ri) => {
    const cells = row.cells.filter((c) => Number.isInteger(c.i) && c.i >= 0 && c.i < blocks.length);
    if (!cells.length) return null;
    cells.forEach((c) => used.add(c.i));
    const cols = cells.map((c) => `${Math.max(1, Math.min(12, c.span || Math.floor(12 / cells.length)))}fr`).join(" ");
    return (
      <div
        key={ri}
        className="grid grid-cols-1 gap-4 md:[grid-template-columns:var(--prism-cols)]"
        style={{ ["--prism-cols" as string]: cols }}
      >
        {cells.map((c, ci) => {
          const blk = blocks[c.i];
          return blk ? <div key={ci} className="min-w-0"><BlockView block={blk} accent={accent} /></div> : null;
        })}
      </div>
    );
  });
  const leftover = blocks.filter((_, i) => !used.has(i));
  return (
    <div className="space-y-4">
      {gridRows}
      {leftover.map((b, i) => <BlockView key={`x${i}`} block={b} accent={accent} />)}
    </div>
  );
}

/** Delegates to the shared MermaidViewer (zoom/pan + scratch source editor,
 *  mermaid still lazy-loaded into its own chunk). Export name/signature kept so
 *  callers are unchanged; the height is bounded so the pan/zoom viewport has a
 *  frame to work inside the flowing prism doc. */
/** LLM-authored mermaid specs sometimes carry a LITERAL `\n` (backslash-n) or
 *  `\t` inside a node label — valid in the model's JSON string, but mermaid
 *  11.x rejects it ("Syntax error in text") and the diagram falls back to raw
 *  source. Normalize at the single render chokepoint: `\n` → `<br/>` (multi-line
 *  label, supported in flowchart labels), `\t` → space. Real newlines that
 *  separate statements are untouched (they are actual `\n` chars, not the
 *  two-character escape sequence). */
function sanitizeMermaid(spec: string): string {
  return quoteMermaidLabels((spec || "").replace(/\\n/g, "<br/>").replace(/\\t/g, " "));
}

/** Mermaid 11.x rejects UNQUOTED node labels that contain parentheses, commas,
 *  slashes or an inline `-->` (all common in LLM-authored specs) — it parses the
 *  label as syntax and throws "Syntax error in text". Quoting the label makes it
 *  literal text. We wrap each node shape's inner label in quotes when it isn't
 *  already quoted, so any deck (past data or future generation) renders safely.
 *  Compound shapes are quoted first; the single-bracket passes then check the
 *  surrounding characters via the replace offset (NOT regex lookbehind, which
 *  older WebKitGTK rejects at parse time). Edge labels (`-->|text|`,
 *  `-. text .->`) carry no brackets and are untouched. */
function quoteMermaidLabels(spec: string): string {
  const q = (inner: string) => {
    const t = inner.trim();
    if (t.length >= 2 && t.startsWith('"') && t.endsWith('"')) return inner;
    return `"${t.replace(/"/g, "'")}"`;
  };
  return spec
    .replace(/\[\(([^)\]]*)\)\]/g, (_m, i) => `[(${q(i)})]`)
    .replace(/\(\(([^)]*)\)\)/g, (_m, i) => `((${q(i)}))`)
    .replace(/\(\[([^\])]*)\]\)/g, (_m, i) => `([${q(i)}])`)
    .replace(/\[\[([^\]]*)\]\]/g, (_m, i) => `[[${q(i)}]]`)
    .replace(/\{\{([^}]*)\}\}/g, (_m, i) => `{{${q(i)}}}`)
    .replace(/\[([^[\]]*)\]/g, (m, i: string, off: number, full: string) => {
      // skip when this bracket is part of an already-handled compound shape
      // (adjacent bracket) or already carries a quote (a compound pass ran).
      if (full[off - 1] === "[" || full[off - 1] === "(" || full[off + m.length] === "]" || i.includes('"')) return m;
      return `[${q(i)}]`;
    })
    .replace(/\{([^{}]*)\}/g, (m, i: string, off: number, full: string) => {
      if (full[off - 1] === "{" || full[off + m.length] === "}" || i.includes('"')) return m;
      return `{${q(i)}}`;
    });
}

export function MermaidBlock({ spec, accent }: { spec: string; accent?: string }) {
  return (
    <MermaidViewer
      code={sanitizeMermaid(spec)}
      accent={accent}
      className="my-1 h-[420px] rounded border border-current/10"
    />
  );
}

const CALLOUT_STYLE: Record<string, { icon: typeof AlertTriangle; cls: string }> = {
  risk: { icon: AlertTriangle, cls: "border-[var(--color-status-error,#f92f77)]/40 bg-[var(--color-status-error,#f92f77)]/10 text-[var(--color-status-error,#f92f77)]" },
  warn: { icon: TriangleAlert, cls: "border-[var(--color-status-error,#f92f77)]/40 bg-[var(--color-status-error,#f92f77)]/10 text-[var(--color-status-error,#f92f77)]" },
  decision: { icon: GitBranch, cls: "border-accent/40 bg-accent/10 text-accent" },
  info: { icon: Info, cls: "border-[var(--color-status-info,#2998fd)]/40 bg-[var(--color-status-info,#2998fd)]/10 text-[var(--color-status-info,#2998fd)]" },
  wow: { icon: Sparkles, cls: "border-warning/40 bg-warning/10 text-warning" },
};
const CALLOUT_DEFAULT = { icon: GitBranch, cls: "border-accent/40 bg-accent/10 text-accent" };

/** Optional per-row tint for a rich table. */
const TABLE_ROW_STATE: Record<string, string> = {
  good: "bg-[var(--color-status-success,#11d425)]/10",
  warn: "bg-[var(--color-status-warning,#c52998)]/10",
  bad: "bg-[var(--color-status-error,#f92f77)]/10",
  highlight: "bg-accent/10",
};

/** Render a table cell, wrapping `backtick` spans as inline <code>. */
function codeCell(s: string): ReactNode {
  if (!s.includes("`")) return s;
  return s.split("`").map((part, i) =>
    i % 2 === 1
      ? <code key={i} className="rounded bg-current/10 px-1 py-0.5 text-[0.85em]">{part}</code>
      : part);
}

/** Subtle drillable provenance tag — the per-claim source, hover for full ref. */
function Provenance({ source }: { source?: string }) {
  if (!source) return null;
  const href = /^https?:\/\//.test(source) ? source : undefined;
  const label = source.length > 48 ? source.slice(0, 45) + "…" : source;
  return (
    <div className="mt-1 text-[11px] opacity-70" title={`source: ${source}`}>
      {href ? <a href={href} target="_blank" rel="noreferrer" className="underline decoration-dotted underline-offset-2">source: {label}</a> : <span>source: {label}</span>}
    </div>
  );
}

/** A board-grade KPI tile: the big number, an optional trend arrow + delta
 *  pill, and an optional sub-line for context (target / baseline / period). A
 *  bare number forces the reader to guess whether it's good — the delta + sub
 *  give it direction and a reference point. `trend` picks the arrow; `state`
 *  (good/warn/bad) semantically colors it — decoupled because "up" isn't always
 *  good (burn rate up = bad). Falls back to trend-implied color, then neutral. */
type StatItem = Extract<Block, { type: "stat" }>["items"][number];
const STAT_STATE: Record<string, string> = {
  good: "var(--color-status-success,#11d425)",
  warn: "var(--color-status-warning,#c52998)",
  bad: "var(--color-status-error,#f92f77)",
};
function StatTile({ s, accent }: { s: StatItem; accent: string }) {
  const Arrow = s.trend === "up" ? ArrowUpRight : s.trend === "down" ? ArrowDownRight : s.trend === "flat" ? Minus : null;
  const trendColor = s.state
    ? STAT_STATE[s.state]
    : s.trend === "up"
      ? STAT_STATE.good
      : s.trend === "down"
        ? STAT_STATE.bad
        : "currentColor";
  const showDelta = Boolean(s.delta || Arrow);
  return (
    <div className="flex flex-col gap-1 rounded-lg border border-current/10 px-3.5 py-3">
      <div className="flex items-start justify-between gap-2">
        <div className="text-3xl font-semibold leading-none" style={{ color: accent }}>{s.value}</div>
        {showDelta && (
          <div className="flex max-w-[52%] shrink-0 items-center justify-end gap-0.5 text-right text-xs font-medium leading-tight" style={{ color: trendColor }}>
            {Arrow && <Arrow size={13} strokeWidth={2.5} className="shrink-0" />}
            {s.delta && <span>{s.delta}</span>}
          </div>
        )}
      </div>
      <div className="text-[11px] uppercase tracking-wide opacity-60">{s.label}</div>
      {s.sub && <div className="text-[11px] opacity-45">{s.sub}</div>}
    </div>
  );
}

/** Render one typed block. `accent` threads the brand accent into visuals. */
export function BlockView({ block, accent }: { block: Block; accent?: string }) {
  const acc = accent ?? "var(--color-accent)";
  switch (block.type) {
    case "text":
      // Cap prose measure (~70ch) so a full-width text block in a wide facet
      // doesn't sprawl into an unreadable line length; a no-op inside a narrow
      // grid cell that's already tighter than the cap (mono-hierarchy research).
      return (
        <div className="max-w-[70ch] opacity-90">
          <MarkdownContent variant="viewer">{block.md}</MarkdownContent>
          <Provenance source={block.source_ref} />
        </div>
      );
    case "heading": {
      // `display:"standalone"` turns a lone heading into a section-divider:
      // full-bleed, centered, oversized — a chapter break in the facet tree.
      const dividerHead = block.display === "standalone";
      return (
        <div className={dividerHead ? "flex min-h-[45vh] flex-col justify-center text-center" : ""}>
          {block.eyebrow && <p className={`font-medium uppercase ${dividerHead ? "text-sm tracking-[0.3em]" : "text-xs tracking-[0.2em]"}`} style={{ color: accent }}>{block.eyebrow}</p>}
          <h3 className={dividerHead ? "mt-2 text-4xl font-semibold tracking-tight sm:text-5xl" : "mt-1 text-xl font-semibold"}>{block.title}</h3>
          {block.lede && <p className={`opacity-70 ${dividerHead ? "mx-auto mt-3 max-w-[46ch] text-lg" : "mt-1"}`}>{block.lede}</p>}
          <Provenance source={block.source_ref} />
        </div>
      );
    }
    case "stat":
      return (
        <div>
          <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
            {block.items.map((s, i) => <StatTile key={i} s={s} accent={acc} />)}
          </div>
          <Provenance source={block.source_ref} />
        </div>
      );
    case "table":
      return (
        <div>
          <div className="overflow-x-auto rounded-lg border border-current/10">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-current/15">
                  {block.headers.map((h, i) => (
                    <th key={i} className="px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wide opacity-70">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, ri) => (
                  <tr key={ri} className={`border-b border-current/5 last:border-0 ${TABLE_ROW_STATE[block.rowStates?.[ri] ?? ""] ?? ""}`}>
                    {row.map((cell, ci) => <td key={ci} className="px-3 py-2 align-top">{codeCell(cell)}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Provenance source={block.source_ref} />
        </div>
      );
    case "callout": {
      const style = CALLOUT_STYLE[block.variant] ?? CALLOUT_DEFAULT;
      const Icon = style.icon;
      return (
        <div className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-sm ${style.cls}`}>
          <Icon size={15} className="mt-0.5 shrink-0" />
          <div>
            <span>{block.text}</span>
            <Provenance source={block.source_ref} />
          </div>
        </div>
      );
    }
    case "image": {
      // caption-over-image: the overlay lives INSIDE the figure's own box (a
      // contained gradient + text), so it never fights the flowing doc. With
      // display:"standalone" the image reads as a full-bleed hero.
      const standalone = block.display === "standalone";
      const ov = block.overlay;
      const hasOverlay = !!ov && (!!ov.title || !!ov.text);
      return (
        <figure>
          <div className="relative overflow-hidden rounded-lg border border-current/10">
            <img
              src={block.src}
              alt={block.caption ?? ov?.title ?? ""}
              className={standalone ? "max-h-[70vh] w-full object-cover" : "w-full max-w-full"}
            />
            {hasOverlay && (
              <div className={`absolute inset-0 flex flex-col justify-end gap-1 bg-gradient-to-t from-black/75 via-black/25 to-transparent p-4 ${ov!.align === "center" ? "items-center text-center" : "items-start"}`}>
                {ov!.title && <div className="text-lg font-semibold text-white sm:text-2xl">{ov!.title}</div>}
                {ov!.text && <div className="max-w-2xl text-sm text-white/85">{ov!.text}</div>}
              </div>
            )}
          </div>
          {block.caption && <figcaption className="mt-1 text-xs opacity-60">{block.caption}</figcaption>}
          <Provenance source={block.source_ref} />
        </figure>
      );
    }
    case "diagram":
      return (
        <div>
          <MermaidBlock spec={block.spec} accent={accent} />
          <Provenance source={block.source_ref} />
        </div>
      );
    case "chart":
      return (
        <div>
          <ChartBlock data={block} accent={accent} />
          <Provenance source={block.source_ref} />
        </div>
      );
    case "smallmultiples":
      return (
        <div>
          <SmallMultiples data={block} accent={accent} />
          <Provenance source={block.source_ref} />
        </div>
      );
    case "graph":
      return (
        <div>
          <Suspense fallback={<BlockFallback />}>
            <GraphBlock data={block} accent={accent} />
          </Suspense>
          <Provenance source={block.source_ref} />
        </div>
      );
    case "flow":
      return (
        <div>
          <Suspense fallback={<BlockFallback />}>
            <FlowEmbed flowId={block.flow_id} caption={block.caption} accent={accent} />
          </Suspense>
          <Provenance source={block.source_ref} />
        </div>
      );
    case "lens":
      return <div><LensTabs data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "timeline":
      return <div><Timeline data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "options":
      return <div><Options data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "compare":
      return <div><Compare data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "cards":
      return <div><Cards data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "matrix":
      return <div><Matrix data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "meter":
      return <div><Meter data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "steps":
      return <div><Steps data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "spec":
      return <div><Spec data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "code":
      return <div><CodeBlock data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "cta":
      return <div><Cta data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "checklist":
      return <div><Checklist data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "redteam":
      return <div><RedTeam data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "decisionrecord":
      return <div><DecisionRecord data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "assumptionledger":
      return <div><AssumptionLedger data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "tripwires":
      return <div><Tripwires data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "risk":
      return <div><RiskRegister data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "audiobrief":
      return <div><AudioBrief data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "simulator":
      return <div><Simulator data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "frequency":
      return <div><Frequency data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "hops":
      return <div><Hops data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "evidence":
      return <div><Evidence data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "biascheck":
      return <div><BiasCheck data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "messageproof":
      return <div><MessageProof data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "statement":
      return <div><Statement data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "quote":
      return <div><Quote data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "gallery":
      return <div><Gallery data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    case "visualizer":
      return <div><Visualizer data={block} accent={acc} /><Provenance source={block.source_ref} /></div>;
    default:
      return null;
  }
}
