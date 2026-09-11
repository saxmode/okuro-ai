// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism M4 storytelling modules — narrative-shaped presentational
//   blocks (no new deps): LensTabs (same content, N persona reframings) ·
//   Timeline (ordered beats) · Options (decision matrix, ★ recommended) ·
//   Compare (before→after) · Cards (card-grid). All accent-themed + theme-aware
//   via currentColor; light enough to import eagerly in BlockView.
// index: LensTabs | Timeline | Options | Compare | Cards
// AGENT_HEADER_END -->
import { Check, Star, X } from "lucide-react";
import { useState } from "react";

import { MarkdownContent } from "@/components/ui/markdown-content";
import type { Block } from "@/lib/prism-api";

/** Same content, N persona reframings — the reference deck's core rhetorical device. */
export function LensTabs({ data, accent }: { data: Extract<Block, { type: "lens" }>; accent: string }) {
  const tabs = data.tabs ?? [];
  const [active, setActive] = useState(0);
  if (!tabs.length) return null;
  const cur = tabs[Math.min(active, tabs.length - 1)];
  return (
    <div className="rounded-xl border border-current/15">
      <div className="flex flex-wrap border-b border-current/15">
        {tabs.map((t, i) => (
          <button
            key={i}
            onClick={() => setActive(i)}
            className={`-mb-px border-b-2 px-4 py-2.5 text-sm font-medium tracking-tight transition-colors ${
              i === active ? "opacity-100" : "border-transparent opacity-55 hover:opacity-90"
            }`}
            style={i === active ? { color: accent, borderColor: accent } : undefined}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="px-4 py-3.5 text-[15px] leading-relaxed opacity-90">
        <MarkdownContent variant="viewer">{cur?.md ?? ""}</MarkdownContent>
      </div>
    </div>
  );
}

/** Ordered beats — T+N → title + body + actor. */
export function Timeline({ data, accent }: { data: Extract<Block, { type: "timeline" }>; accent: string }) {
  const events = data.events ?? [];
  if (!events.length) return null;
  return (
    <ol className="relative ml-2 space-y-4 border-l border-current/15 pl-5">
      {events.map((e, i) => (
        <li key={i} className="relative">
          <span className="absolute -left-[27px] top-1 h-3 w-3 rounded-full border-2" style={{ background: accent, borderColor: accent }} />
          <div className="flex flex-wrap items-baseline gap-2">
            {e.time && <span className="text-xs font-semibold tracking-wide" style={{ color: accent }}>{e.time}</span>}
            <span className="font-medium">{e.title}</span>
            {e.actor && <span className="rounded-full border border-current/20 px-1.5 py-0.5 text-[10px] opacity-70">{e.actor}</span>}
          </div>
          {e.body && <p className="mt-0.5 text-sm opacity-70">{e.body}</p>}
        </li>
      ))}
    </ol>
  );
}

/** Decision matrix — N options, pros/cons, ★ the recommended one. */
export function Options({ data, accent }: { data: Extract<Block, { type: "options" }>; accent: string }) {
  const options = data.options ?? [];
  if (!options.length) return null;
  return (
    <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-3">
      {options.map((o, i) => (
        <div
          key={i}
          className="flex flex-col gap-2 rounded-lg border p-3"
          style={o.recommended ? { borderColor: accent, boxShadow: `inset 0 0 0 1px ${accent}` } : { borderColor: "currentColor", borderStyle: "solid", opacity: 0.95, borderWidth: 1 }}
        >
          <div className="flex items-center gap-1.5">
            {o.recommended && <Star size={14} fill={accent} color={accent} className="shrink-0" />}
            <span className="font-semibold">{o.title}</span>
          </div>
          {(o.pros ?? []).length > 0 && (
            <ul className="space-y-0.5 text-sm">
              {o.pros!.map((p, j) => <li key={j} className="flex items-start gap-1.5"><Check size={13} className="mt-0.5 shrink-0 text-[var(--color-status-success,#11d425)]" /><span className="opacity-80">{p}</span></li>)}
            </ul>
          )}
          {(o.cons ?? []).length > 0 && (
            <ul className="space-y-0.5 text-sm">
              {o.cons!.map((c, j) => <li key={j} className="flex items-start gap-1.5"><X size={13} className="mt-0.5 shrink-0 text-[var(--color-status-error,#f92f77)]" /><span className="opacity-80">{c}</span></li>)}
            </ul>
          )}
          {o.note && <p className="mt-auto text-xs opacity-60">{o.note}</p>}
        </div>
      ))}
    </div>
  );
}

/** Before → after — two columns, the after side carries the accent. */
export function Compare({ data, accent }: { data: Extract<Block, { type: "compare" }>; accent: string }) {
  const { before, after } = data;
  const Col = ({ side, tint }: { side: { label?: string; items: string[] }; tint?: boolean }) => (
    <div className="flex-1 rounded-lg border p-3" style={tint ? { borderColor: accent } : { borderColor: "currentColor", opacity: 0.9 }}>
      <p className="mb-1.5 text-[11px] uppercase tracking-wide" style={tint ? { color: accent } : { opacity: 0.6 }}>{side?.label ?? (tint ? "After" : "Before")}</p>
      <ul className="space-y-1 text-sm">
        {(side?.items ?? []).map((it, i) => <li key={i} className="opacity-85">{it}</li>)}
      </ul>
    </div>
  );
  return (
    <div className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-center">
      <Col side={before} />
      <div className="shrink-0 self-center px-1 text-lg opacity-50" style={{ color: accent }}>→</div>
      <Col side={after} tint />
    </div>
  );
}

/** Card grid — one grid, titled cards with optional tag + body. */
export function Cards({ data, accent }: { data: Extract<Block, { type: "cards" }>; accent: string }) {
  const cards = data.cards ?? [];
  if (!cards.length) return null;
  const min = data.columns && data.columns > 0 ? `${Math.floor(100 / data.columns) - 2}%` : "180px";
  return (
    <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(auto-fit,minmax(min(${min},100%),1fr))` }}>
      {cards.map((c, i) => (
        <div key={i} className="rounded-lg border border-current/10 p-3">
          <div className="flex items-center gap-2">
            <span className="font-semibold">{c.title}</span>
            {c.tag && <span className="ml-auto rounded-full px-2 py-0.5 text-[10px]" style={{ background: `${accent}22`, color: accent }}>{c.tag}</span>}
          </div>
          {c.body && <p className="mt-1 text-sm opacity-70">{c.body}</p>}
        </div>
      ))}
    </div>
  );
}
