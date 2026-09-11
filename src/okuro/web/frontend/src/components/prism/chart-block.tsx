// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism chart block (M2 rich-viz) — a dependency-free, brand-
//   themed SVG chart (bar | line | area | pie). Single-series = one accent hue
//   (safe-by-construction, no rainbow); pie = an accent opacity ramp with a
//   text legend so identity is never color-alone. Direct value labels, recessive
//   axes, native <title> tooltips, theme-aware via currentColor. Portable — no
//   ReactFlow / charting lib, so it also survives the Resonance website export.
// index: helpers | BarLineArea | Pie | ChartBlock
// AGENT_HEADER_END -->
import type { Block } from "@/lib/prism-api";

type ChartData = Extract<Block, { type: "chart" }>;

const W = 640;
const H = 300;
const PAD = { top: 24, right: 16, bottom: 40, left: 44 };
const PLOT_W = W - PAD.left - PAD.right;
const PLOT_H = H - PAD.top - PAD.bottom;

/** Compact tick label for a number (1_250 → "1.3k", 0.5 → "0.5"). */
function fmt(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
  if (abs >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, "") + "k";
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(abs < 1 ? 2 : 1);
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

/** Bar / line / area share a cartesian frame — value axis + category axis. */
function BarLineArea({ data, accent }: { data: ChartData; accent: string }) {
  const series = data.series.filter((d) => Number.isFinite(d.value));
  const values = series.map((d) => d.value);
  const rawMax = Math.max(0, ...values);
  const rawMin = Math.min(0, ...values);
  const max = rawMax === rawMin ? rawMax + 1 : rawMax;
  const min = rawMin;
  const span = max - min || 1;
  const y = (v: number) => PAD.top + PLOT_H * (1 - (v - min) / span);
  const n = series.length;
  const step = n > 0 ? PLOT_W / n : PLOT_W;
  const cx = (i: number) => PAD.left + step * (i + 0.5);
  const gridVals = [min, min + span / 2, max];

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" preserveAspectRatio="xMidYMid meet">
      {/* recessive value grid + ticks */}
      {gridVals.map((gv, i) => (
        <g key={i}>
          <line x1={PAD.left} x2={W - PAD.right} y1={y(gv)} y2={y(gv)} stroke="currentColor" strokeOpacity={0.1} strokeWidth={1} />
          <text x={PAD.left - 6} y={y(gv)} textAnchor="end" dominantBaseline="middle" fontSize={11} fill="currentColor" fillOpacity={0.5}>
            {fmt(gv)}{data.unit ? ` ${data.unit}` : ""}
          </text>
        </g>
      ))}

      {data.chart === "area" && n > 1 && (
        <polygon
          points={
            `${cx(0)},${y(min)} ` +
            series.map((d, i) => `${cx(i)},${y(d.value)}`).join(" ") +
            ` ${cx(n - 1)},${y(min)}`
          }
          fill={accent}
          fillOpacity={0.18}
        />
      )}

      {(data.chart === "line" || data.chart === "area") && n > 1 && (
        <polyline points={series.map((d, i) => `${cx(i)},${y(d.value)}`).join(" ")} fill="none" stroke={accent} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      )}

      {series.map((d, i) => {
        const label = `${d.label}: ${fmt(d.value)}${data.unit ? ` ${data.unit}` : ""}`;
        if (data.chart === "bar") {
          const bw = Math.min(48, step * 0.62);
          const top = y(Math.max(d.value, min === max ? d.value : Math.max(0, min)));
          const base = y(Math.max(0, min));
          const hgt = Math.max(1, Math.abs(base - y(d.value)));
          const ry = Math.min(4, bw / 2);
          return (
            <g key={i}>
              <rect x={cx(i) - bw / 2} y={Math.min(top, base) === base ? y(d.value) : top} width={bw} height={hgt} rx={ry} ry={ry} fill={accent}>
                <title>{label}</title>
              </rect>
              <text x={cx(i)} y={y(d.value) - 6} textAnchor="middle" fontSize={11} fill="currentColor" fillOpacity={0.75}>{fmt(d.value)}</text>
            </g>
          );
        }
        // line / area point markers + selective labels
        return (
          <g key={i}>
            <circle cx={cx(i)} cy={y(d.value)} r={4} fill={accent}>
              <title>{label}</title>
            </circle>
            {n <= 8 && <text x={cx(i)} y={y(d.value) - 8} textAnchor="middle" fontSize={11} fill="currentColor" fillOpacity={0.75}>{fmt(d.value)}</text>}
          </g>
        );
      })}

      {/* category axis */}
      {series.map((d, i) => (
        <text key={i} x={cx(i)} y={H - PAD.bottom + 16} textAnchor="middle" fontSize={11} fill="currentColor" fillOpacity={0.6}>
          {truncate(d.label, n > 8 ? 6 : 12)}
        </text>
      ))}

      {/* annotations — a leader from the top rail down to the called-out point,
          an emphasis ring, and a short label. Drawn last so it reads on top. */}
      {(data.annotations ?? []).map((a, k) => {
        if (!Number.isInteger(a.at) || a.at < 0 || a.at >= n) return null;
        const point = series[a.at];
        if (!point) return null;
        const px = cx(a.at);
        const py = y(point.value);
        const labelX = Math.max(PAD.left + 40, Math.min(W - PAD.right - 40, px));
        return (
          <g key={`ann-${k}`}>
            <line x1={px} x2={px} y1={PAD.top + 12} y2={py - 8} stroke={accent} strokeWidth={1} strokeDasharray="3 3" strokeOpacity={0.7} />
            <circle cx={px} cy={py} r={6} fill="none" stroke={accent} strokeWidth={2} />
            <text x={labelX} y={PAD.top + 4} textAnchor="middle" fontSize={11} fontWeight={600} fill={accent}>
              {truncate(a.text, 32)}
              <title>{a.text}</title>
            </text>
          </g>
        );
      })}
    </svg>
  );
}

/** Composition — accent opacity ramp for slices, identity carried by a text
 *  legend (never color-alone). */
function Pie({ data, accent }: { data: ChartData; accent: string }) {
  const series = data.series.filter((d) => Number.isFinite(d.value) && d.value > 0);
  const total = series.reduce((s, d) => s + d.value, 0) || 1;
  const R = 100;
  const cx = 130;
  const cy = 130;
  let a0 = -Math.PI / 2;
  const opacityFor = (i: number) => Math.max(0.35, 1 - i * 0.15);

  return (
    <div className="flex flex-wrap items-center gap-6">
      <svg viewBox="0 0 260 260" className="h-52 w-52 shrink-0" role="img">
        {series.map((d, i) => {
          const frac = d.value / total;
          const a1 = a0 + frac * Math.PI * 2;
          const large = frac > 0.5 ? 1 : 0;
          const x0 = cx + R * Math.cos(a0);
          const y0 = cy + R * Math.sin(a0);
          const x1 = cx + R * Math.cos(a1);
          const y1 = cy + R * Math.sin(a1);
          const path = series.length === 1
            ? `M ${cx - R} ${cy} a ${R} ${R} 0 1 1 ${R * 2} 0 a ${R} ${R} 0 1 1 ${-R * 2} 0`
            : `M ${cx} ${cy} L ${x0} ${y0} A ${R} ${R} 0 ${large} 1 ${x1} ${y1} Z`;
          a0 = a1;
          return (
            <path key={i} d={path} fill={accent} fillOpacity={opacityFor(i)} stroke="var(--color-background-base, #0b0b0f)" strokeWidth={2}>
              <title>{`${d.label}: ${fmt(d.value)} (${Math.round(frac * 100)}%)`}</title>
            </path>
          );
        })}
      </svg>
      <ul className="min-w-[16rem] space-y-1 text-sm">
        {series.map((d, i) => (
          <li key={i} className="flex items-center gap-2">
            <span className="inline-block h-3 w-3 shrink-0 rounded-sm" style={{ background: accent, opacity: opacityFor(i) }} />
            <span className="opacity-80">{d.label}</span>
            <span className="ml-auto tabular-nums opacity-60">{Math.round((d.value / total) * 100)}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** M2 chart module — dispatches on chart kind. `accent` threads brand accent. */
export default function ChartBlock({ data, accent = "var(--color-accent)" }: { data: ChartData; accent?: string }) {
  if (!data.series?.length) return null;
  return (
    <figure className="rounded-lg border border-current/10 p-3">
      {data.chart === "pie" ? <Pie data={data} accent={accent} /> : <BarLineArea data={data} accent={accent} />}
      {data.caption && <figcaption className="mt-2 text-xs opacity-60">{data.caption}</figcaption>}
    </figure>
  );
}
