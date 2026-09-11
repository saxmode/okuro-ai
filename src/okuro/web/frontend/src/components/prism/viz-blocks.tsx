// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism uncertainty-viz blocks — the calibrated-uncertainty moat.
//   Frequency (probability as "N of M" icon arrays, not percentages) and Hops
//   (Hypothetical Outcome Plots — a forecast animated as cycling equally-likely
//   draws, with a static quantile-dotplot fallback under reduced-motion / print).
//   Dependency-free, brand-themed, theme-aware. Evidence: HOPs beat error bars
//   for lay decision-makers (Hullman 2015 / Kale 2018); frequency framing beats
//   percentages for mixed-numeracy rooms (Gigerenzer).
// index: STATE_COLOR | Frequency | Hops
// AGENT_HEADER_END -->
import { useEffect, useMemo, useState } from "react";

import { hopsStats, iconCells, resolveDraws } from "@/lib/viz";
import type { Block } from "@/lib/prism-api";

type FrequencyData = Extract<Block, { type: "frequency" }>;
type HopsData = Extract<Block, { type: "hops" }>;

const STATE_COLOR: Record<string, string> = {
  good: "var(--color-status-success,#11d425)",
  warn: "var(--color-status-warning,#c52998)",
  bad: "var(--color-status-error,#f92f77)",
};

/** FREQUENCY — a probability rendered as discrete counts ("10 of 100" filled
 *  dots), which a mixed-numeracy board reads more accurately than a percentage
 *  or a continuous area. Each item is its own icon array. */
export function Frequency({ data, accent }: { data: FrequencyData; accent: string }) {
  const items = (data.items ?? []).filter((it) => it && it.label && Number.isFinite(it.of));
  if (!items.length) return null;
  return (
    <div className="rounded-lg border border-current/10 p-3">
      <div className="space-y-4">
        {items.map((it, i) => {
          const { filled, total } = iconCells(it.count, it.of);
          const color = STATE_COLOR[it.state ?? ""] ?? accent;
          const cols = total > 50 ? 20 : 10;
          return (
            <div key={i}>
              <div className="mb-1.5 flex items-baseline gap-2 text-sm">
                <span className="font-medium">{it.label}</span>
                <span className="ml-auto tabular-nums opacity-60">
                  <span className="font-semibold" style={{ color }}>{it.count}</span> of {it.of}
                </span>
              </div>
              <div
                className="grid gap-[3px]"
                style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`, maxWidth: `${cols * 16}px` }}
                aria-label={`${it.count} of ${it.of}`}
              >
                {Array.from({ length: total }, (_, k) => (
                  <span
                    key={k}
                    className="aspect-square rounded-[2px]"
                    style={{ background: k < filled ? color : "currentColor", opacity: k < filled ? 1 : 0.12 }}
                  />
                ))}
              </div>
            </div>
          );
        })}
      </div>
      {data.caption && <p className="mt-3 text-xs opacity-60">{data.caption}</p>}
    </div>
  );
}

/** HOPS — a Hypothetical Outcome Plot. Instead of a static error bar (which lay
 *  readers misread as a hard bound), the forecast animates: a marker hops between
 *  equally-likely draws, so variance is felt, not decoded. Under reduced-motion
 *  or for print it collapses to a static quantile dotplot. Hard-switch frames,
 *  no tweening (~2.5fps), per the evidence. */
export function Hops({ data, accent }: { data: HopsData; accent: string }) {
  const draws = useMemo(() => resolveDraws(data.draws, data.quantiles), [data.draws, data.quantiles]);
  const stats = useMemo(() => hopsStats(draws), [draws]);

  const [reduced, setReduced] = useState(true); // SSR-safe default: static
  const [idx, setIdx] = useState(0);
  const [playing, setPlaying] = useState(true);

  useEffect(() => {
    const m = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(m.matches);
    const h = (e: MediaQueryListEvent) => setReduced(e.matches);
    m.addEventListener("change", h);
    return () => m.removeEventListener("change", h);
  }, []);

  const animate = !reduced && playing && draws.length > 1;
  useEffect(() => {
    if (!animate) return;
    const fps = Math.min(6, Math.max(1, data.fps ?? 2.5));
    const t = setInterval(() => setIdx((i) => (i + 1) % draws.length), 1000 / fps);
    return () => clearInterval(t);
  }, [animate, draws.length, data.fps]);

  if (!draws.length) return null;

  const lo = stats.min;
  const hi = stats.max;
  const span = hi - lo || Math.abs(hi) || 1;
  const pad = span * 0.08;
  const dlo = lo - pad;
  const dhi = hi + pad;
  const xpct = (v: number) => Math.max(0, Math.min(100, ((v - dlo) / (dhi - dlo)) * 100));
  const dec = data.decimals ?? 0;
  const fmt = (v: number) => `${v.toFixed(dec)}${data.unit ? ` ${data.unit}` : ""}`;

  const current = draws[idx % draws.length]!;
  const ticks: { v: number; label: string }[] = [
    { v: stats.p10, label: "p10" },
    { v: stats.p50, label: "p50" },
    { v: stats.p90, label: "p90" },
  ];

  return (
    <div className="rounded-lg border border-current/10 p-3">
      <div className="mb-2 flex items-center gap-2 text-xs">
        <span className="font-semibold uppercase tracking-wide opacity-70">
          {data.label || "Outcome range"}
        </span>
        {!reduced && draws.length > 1 && (
          <button
            onClick={() => setPlaying((p) => !p)}
            className="ml-auto rounded border border-current/20 px-1.5 py-0.5 text-[10px] uppercase tracking-wide opacity-70 hover:opacity-100"
          >
            {playing ? "❚❚ pause" : "▶ play"}
          </button>
        )}
        {reduced && <span className="ml-auto text-[10px] uppercase tracking-wide opacity-45">static view</span>}
      </div>

      {/* the value readout — the animated draw, or the median in static mode */}
      <div className="mb-1 text-2xl font-semibold tabular-nums" style={{ color: accent }}>
        {reduced ? fmt(stats.p50) : fmt(current)}
        {reduced && <span className="ml-2 align-middle text-xs font-normal opacity-50">median (p10 {fmt(stats.p10)} · p90 {fmt(stats.p90)})</span>}
      </div>

      {/* the track */}
      <div className="relative mt-4 h-14">
        {/* axis */}
        <div className="absolute bottom-4 left-0 right-0 h-px bg-current/20" />
        {/* reference marker */}
        {typeof data.reference === "number" && (
          <div className="absolute bottom-4 top-0" style={{ left: `${xpct(data.reference)}%` }}>
            <div className="h-full w-px" style={{ background: "var(--color-status-warning,#c52998)" }} />
            <div className="absolute -top-0.5 left-1 whitespace-nowrap text-[9px]" style={{ color: "var(--color-status-warning,#c52998)" }}>
              {data.referenceLabel || fmt(data.reference)}
            </div>
          </div>
        )}
        {/* static: every draw as a faint tick + quantile band */}
        {reduced &&
          draws.map((d, k) => (
            <span
              key={k}
              className="absolute bottom-4 h-2 w-px -translate-x-1/2"
              style={{ left: `${xpct(d)}%`, background: accent, opacity: 0.28 }}
            />
          ))}
        {reduced && (
          <div
            className="absolute bottom-[13px] h-1 -translate-y-1/2 rounded"
            style={{ left: `${xpct(stats.p10)}%`, width: `${xpct(stats.p90) - xpct(stats.p10)}%`, background: accent, opacity: 0.35 }}
          />
        )}
        {/* animated: the hopping marker */}
        {!reduced && (
          <div className="absolute bottom-4 -translate-x-1/2 transition-none" style={{ left: `${xpct(current)}%` }}>
            <div className="h-7 w-[3px] rounded" style={{ background: accent }} />
            <div className="absolute -bottom-4 left-1/2 -translate-x-1/2 whitespace-nowrap text-[10px] tabular-nums opacity-70">
              {fmt(current)}
            </div>
          </div>
        )}
        {/* quantile ticks */}
        {ticks.map((t) => (
          <div key={t.label} className="absolute bottom-0 -translate-x-1/2 text-center" style={{ left: `${xpct(t.v)}%` }}>
            <div className="mx-auto h-1.5 w-px bg-current/30" />
            <div className="text-[9px] uppercase tracking-wide opacity-40">{t.label}</div>
          </div>
        ))}
      </div>

      {data.caption && <p className="mt-2 text-xs opacity-60">{data.caption}</p>}
      <p className="mt-1 text-[10px] opacity-40">
        {draws.length} equally-likely outcomes{reduced ? " (static)" : " — each frame is one possible result"}
      </p>
    </div>
  );
}
