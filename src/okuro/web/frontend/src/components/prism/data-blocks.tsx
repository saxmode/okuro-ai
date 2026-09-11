// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism M5 decision-artifact blocks — the reference deck's "more than
//   slides" data-viz (no new deps): Matrix (✓/✗/~ comparison grid, winner col) ·
//   Meter (horizontal proportion/budget/latency bars, good/warn/bad) · Steps
//   (numbered pipeline, actor-colored badges) · Spec (color-bordered tier/model
//   cards with k-v rows). Accent-themed, theme-aware via currentColor.
// index: cellGlyph | Matrix | Meter | Steps | Spec | CodeBlock | Cta | Checklist
// AGENT_HEADER_END -->
import { Check, Copy } from "lucide-react";
import { useState } from "react";

import type { Block } from "@/lib/prism-api";

const STATE_COLOR: Record<string, string> = {
  good: "var(--color-status-success,#11d425)",
  warn: "var(--color-status-warning,#c52998)",
  bad: "var(--color-status-error,#f92f77)",
};
const ACTOR_COLOR: Record<string, string> = {
  edge: "var(--color-status-success,#11d425)",
  cloud: "#4aa3ff",
  user: "#ffc24d",
  process: "var(--color-accent)",
};

/** Normalize a matrix cell to a glyph (✓/✗/~) or raw text. */
function cellGlyph(v: string | boolean): { glyph: string; color?: string } | { text: string } {
  if (v === true) return { glyph: "✓", color: STATE_COLOR.good };
  if (v === false) return { glyph: "✗", color: STATE_COLOR.bad };
  const s = String(v).trim().toLowerCase();
  if (s === "yes" || s === "true" || s === "✓") return { glyph: "✓", color: STATE_COLOR.good };
  if (s === "no" || s === "false" || s === "✗") return { glyph: "✗", color: STATE_COLOR.bad };
  if (s === "partial" || s === "~" || s === "maybe") return { glyph: "~", color: STATE_COLOR.warn };
  return { text: String(v) };
}

/** Feature comparison grid — ✓/✗/~ cells, an optional highlighted winner column. */
export function Matrix({ data, accent }: { data: Extract<Block, { type: "matrix" }>; accent: string }) {
  const cols = data.columns ?? [];
  const rows = data.rows ?? [];
  if (!cols.length || !rows.length) return null;
  const hl = data.highlightCol;
  return (
    <figure className="overflow-x-auto rounded-lg border border-current/10">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-current/15">
            <th className="px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wide opacity-70" />
            {cols.map((c, i) => (
              <th key={i} className="px-3 py-2 text-center text-[11px] font-semibold uppercase tracking-wide"
                style={i === hl ? { color: accent } : { opacity: 0.7 }}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={ri} className="border-b border-current/5 last:border-0">
              <td className="px-3 py-2 text-left font-medium">{row.label}</td>
              {cols.map((_, ci) => {
                const c = cellGlyph(row.cells?.[ci] ?? "");
                return (
                  <td key={ci} className="px-3 py-2 text-center"
                    style={ci === hl ? { background: `${accent}12` } : undefined}>
                    {"glyph" in c
                      ? <span className="text-base font-semibold" style={{ color: c.color }}>{c.glyph}</span>
                      : <span className="opacity-80">{c.text}</span>}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {data.caption && <figcaption className="px-3 py-2 text-xs opacity-60">{data.caption}</figcaption>}
    </figure>
  );
}

/** Horizontal bars — proportion / budget / latency, with good/warn/bad coloring. */
export function Meter({ data, accent }: { data: Extract<Block, { type: "meter" }>; accent: string }) {
  const items = (data.items ?? []).filter((d) => Number.isFinite(d.value));
  if (!items.length) return null;
  const maxAll = Math.max(...items.map((d) => d.max ?? d.value), 1);
  return (
    <figure className="space-y-2 rounded-lg border border-current/10 p-3">
      {items.map((d, i) => {
        const cap = d.max ?? maxAll;
        const pct = Math.max(0, Math.min(100, (d.value / (cap || 1)) * 100));
        const color = d.state ? STATE_COLOR[d.state] : accent;
        return (
          <div key={i}>
            <div className="mb-0.5 flex items-baseline justify-between text-xs">
              <span className="opacity-80">{d.label}</span>
              <span className="tabular-nums opacity-60">{d.value}{data.unit ? ` ${data.unit}` : ""}{d.note ? ` · ${d.note}` : ""}</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-current/10">
              <div className="h-full rounded-full transition-all" style={{ width: `${pct}%`, background: color }} />
            </div>
          </div>
        );
      })}
      {data.caption && <figcaption className="mt-1 text-xs opacity-60">{data.caption}</figcaption>}
    </figure>
  );
}

/** Numbered pipeline — step → actor-colored number → title/detail → badge + meta. */
export function Steps({ data, accent }: { data: Extract<Block, { type: "steps" }>; accent: string }) {
  const steps = data.steps ?? [];
  if (!steps.length) return null;
  return (
    <figure className="space-y-2">
      {steps.map((s, i) => {
        const color = s.actor ? ACTOR_COLOR[s.actor] : accent;
        return (
          <div key={i} className="flex items-start gap-3 rounded-lg border border-current/10 p-2.5">
            <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold"
              style={{ background: `${color}22`, color }}>{s.n ?? i + 1}</span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="font-medium">{s.title}</span>
                {s.badge && <span className="rounded px-1.5 py-0.5 text-[10px]" style={{ background: `${color}22`, color }}>{s.badge}</span>}
                {s.meta && <span className="ml-auto text-[11px] tabular-nums opacity-60">{s.meta}</span>}
              </div>
              {s.detail && <p className="mt-0.5 text-sm opacity-70">{s.detail}</p>}
            </div>
          </div>
        );
      })}
      {data.caption && <figcaption className="text-xs opacity-60">{data.caption}</figcaption>}
    </figure>
  );
}

const SPEC_BORDER: Record<string, string> = {
  active: "var(--color-status-success,#11d425)",
  research: "#b07cff",
  ambient: "#ffc24d",
  default: "currentColor",
};

/** Tier/model spec cards — color-bordered, badge, k-v spec rows. */
export function Spec({ data, accent }: { data: Extract<Block, { type: "spec" }>; accent: string }) {
  const cards = data.cards ?? [];
  if (!cards.length) return null;
  return (
    <figure>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(min(200px,100%),1fr))] gap-3">
        {cards.map((c, i) => {
          const border = SPEC_BORDER[c.variant ?? "default"] ?? "currentColor";
          return (
            <div key={i} className="rounded-lg border p-3" style={{ borderColor: border, borderWidth: 1, opacity: c.variant && c.variant !== "default" ? 1 : 0.95 }}>
              <div className="mb-2 flex items-center gap-2">
                <span className="font-semibold">{c.title}</span>
                {c.badge && <span className="ml-auto rounded-full px-2 py-0.5 text-[10px]" style={{ background: `${accent}22`, color: accent }}>{c.badge}</span>}
              </div>
              <dl className="space-y-1 text-sm">
                {(c.rows ?? []).map((r, j) => (
                  <div key={j} className="flex items-baseline justify-between gap-2">
                    <dt className="shrink-0 text-[11px] uppercase tracking-wide opacity-50">{r.k}</dt>
                    <dd className="min-w-0 flex-1 truncate text-right opacity-85">{r.v}</dd>
                  </div>
                ))}
              </dl>
            </div>
          );
        })}
      </div>
      {data.caption && <figcaption className="mt-2 text-xs opacity-60">{data.caption}</figcaption>}
    </figure>
  );
}

/** Fenced code with an optional filename header + copy-to-clipboard (reference-deck
 *  prompt console). No syntax highlighting — mono + theme surface, dep-free. */
export function CodeBlock({ data }: { data: Extract<Block, { type: "code" }>; accent: string }) {
  const [copied, setCopied] = useState(false);
  if (!data.code) return null;
  const copy = () => {
    navigator.clipboard?.writeText(data.code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => {});
  };
  return (
    <figure className="overflow-hidden rounded-lg border border-current/10">
      <div className="flex items-center gap-2 border-b border-current/10 bg-black/20 px-3 py-1.5 text-[11px]">
        <span className="opacity-60">{data.filename || data.lang || "code"}</span>
        <button onClick={copy} className="ml-auto flex items-center gap-1 opacity-60 transition-opacity hover:opacity-100">
          {copied ? <Check size={12} /> : <Copy size={12} />}{copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="overflow-x-auto p-3 text-xs leading-relaxed"><code>{data.code}</code></pre>
    </figure>
  );
}

/** Board-ask / open-questions box — a prominent accent panel with an optional
 *  deep-link action. Heavier than a callout (which is a one-line alert). */
export function Cta({ data, accent }: { data: Extract<Block, { type: "cta" }>; accent: string }) {
  const items = data.items ?? [];
  if (!data.title && !items.length) return null;
  return (
    <div className="rounded-lg border p-4" style={{ borderColor: accent, background: `${accent}0f` }}>
      {data.title && <p className="text-sm font-semibold" style={{ color: accent }}>{data.title}</p>}
      {items.length > 0 && (
        <ul className="mt-2 space-y-1 text-sm">
          {items.map((it, i) => <li key={i} className="flex gap-2 opacity-85"><span style={{ color: accent }}>›</span>{it}</li>)}
        </ul>
      )}
      {data.action?.href && data.action?.label && (
        <a href={data.action.href} className="mt-3 inline-block rounded-md px-3 py-1 text-sm font-medium"
          style={{ background: accent, color: "var(--color-background-base,#0b0b0f)" }}>{data.action.label} →</a>
      )}
    </div>
  );
}

/** Coverage checklist — a ✓ grid (done) with dimmed open items. */
export function Checklist({ data, accent }: { data: Extract<Block, { type: "checklist" }>; accent: string }) {
  const items = data.items ?? [];
  if (!items.length) return null;
  const min = data.columns && data.columns > 0 ? `${Math.floor(100 / data.columns) - 2}%` : "160px";
  return (
    <ul className="grid gap-1.5" style={{ gridTemplateColumns: `repeat(auto-fit,minmax(min(${min},100%),1fr))` }}>
      {items.map((it, i) => {
        const done = it.done !== false;
        return (
          <li key={i} className={`flex items-start gap-1.5 text-sm ${done ? "" : "opacity-45"}`}>
            <Check size={14} className="mt-0.5 shrink-0" style={{ color: done ? accent : "currentColor" }} />
            <span>{it.text}</span>
          </li>
        );
      })}
    </ul>
  );
}
