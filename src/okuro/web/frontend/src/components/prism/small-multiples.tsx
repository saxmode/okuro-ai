// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism small-multiples block (Phase C viz) — a grid of tiny
//   same-shape charts that SHARE one value scale, so many segments are visually
//   comparable at a glance (Tufte small multiples). Dependency-free SVG, brand-
//   themed via accent; reuses the chart idiom in miniature (no axis chrome).
// index: MiniChart | SmallMultiples
// AGENT_HEADER_END -->
import type { Block } from "@/lib/prism-api";

type Data = Extract<Block, { type: "smallmultiples" }>;

const W = 170;
const H = 84;
const PAD = 8;

function fmt(n: number): string {
  const a = Math.abs(n);
  if (a >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
  if (a >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, "") + "k";
  return Number.isInteger(n) ? String(n) : n.toFixed(a < 1 ? 2 : 1);
}

/** One panel, drawn against a SHARED [min,max] so panels are comparable. */
function MiniChart({ kind, series, min, max, accent }: {
  kind: Data["chart"]; series: { label: string; value: number }[];
  min: number; max: number; accent: string;
}) {
  const vals = series.filter((d) => Number.isFinite(d.value));
  const span = max - min || 1;
  const plotW = W - PAD * 2;
  const plotH = H - PAD * 2;
  const n = vals.length;
  const y = (v: number) => PAD + plotH * (1 - (v - min) / span);
  const cx = (i: number) => PAD + (n > 0 ? (plotW / n) * (i + 0.5) : plotW / 2);
  const base = y(Math.max(0, min));

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" preserveAspectRatio="xMidYMid meet">
      <line x1={PAD} x2={W - PAD} y1={base} y2={base} stroke="currentColor" strokeOpacity={0.15} strokeWidth={1} />
      {(kind === "line" || kind === "area") && n > 1 && (
        <>
          {kind === "area" && (
            <polygon
              points={`${cx(0)},${base} ${vals.map((d, i) => `${cx(i)},${y(d.value)}`).join(" ")} ${cx(n - 1)},${base}`}
              fill={accent} fillOpacity={0.16}
            />
          )}
          <polyline points={vals.map((d, i) => `${cx(i)},${y(d.value)}`).join(" ")}
            fill="none" stroke={accent} strokeWidth={1.75} strokeLinejoin="round" strokeLinecap="round" />
        </>
      )}
      {kind === "bar" && vals.map((d, i) => {
        const bw = Math.min(22, (plotW / Math.max(1, n)) * 0.62);
        const top = Math.min(y(d.value), base);
        return (
          <rect key={i} x={cx(i) - bw / 2} y={top} width={bw} height={Math.max(1, Math.abs(base - y(d.value)))}
            rx={1.5} fill={accent}>
            <title>{`${d.label}: ${fmt(d.value)}`}</title>
          </rect>
        );
      })}
      {(kind === "line" || kind === "area") && vals.map((d, i) => (
        <circle key={i} cx={cx(i)} cy={y(d.value)} r={2} fill={accent}><title>{`${d.label}: ${fmt(d.value)}`}</title></circle>
      ))}
    </svg>
  );
}

/** Small-multiples grid — identical mini-charts, one shared scale. */
export default function SmallMultiples({ data, accent = "var(--color-accent)" }: { data: Data; accent?: string }) {
  const panels = (data.panels ?? []).filter((p) => p.series?.length);
  if (!panels.length) return null;
  const all = panels.flatMap((p) => p.series.map((s) => s.value)).filter(Number.isFinite);
  const max = Math.max(1, 0, ...all);
  const min = Math.min(0, ...all);
  const peak = (p: Data["panels"][number]) => Math.max(...p.series.map((s) => s.value).filter(Number.isFinite));

  return (
    <figure className="rounded-lg border border-current/10 p-3">
      <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
        {panels.map((p, i) => (
          <div key={i} className="min-w-0">
            <div className="mb-0.5 flex items-baseline justify-between gap-1">
              <span className="truncate text-xs opacity-75" title={p.label}>{p.label}</span>
              <span className="shrink-0 text-xs font-medium tabular-nums" style={{ color: accent }}>
                {fmt(peak(p))}{data.unit ? ` ${data.unit}` : ""}
              </span>
            </div>
            <MiniChart kind={data.chart} series={p.series} min={min} max={max} accent={accent} />
          </div>
        ))}
      </div>
      {data.caption && <figcaption className="mt-2 text-xs opacity-60">{data.caption}</figcaption>}
    </figure>
  );
}
