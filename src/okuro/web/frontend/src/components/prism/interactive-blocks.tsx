// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism interactive blocks — the LIVE-simulation class. Simulator:
//   assumption sliders that recompute outcome metrics client-side via a safe
//   arithmetic evaluator (no eval). War-gaming in the deck: move a lever, watch
//   the number move.
// index: fmtNum | Simulator
// AGENT_HEADER_END -->
import { useState } from "react";
import type { Block } from "@/lib/prism-api";
import { evalExpr } from "@/lib/expr";

type SimData = Extract<Block, { type: "simulator" }>;

/** Compact number format — thousands separators, sensible decimals. */
function fmtNum(n: number, decimals?: number): string {
  if (!Number.isFinite(n)) return "—";
  const d = decimals ?? (Math.abs(n) >= 100 ? 0 : Math.abs(n) >= 1 ? 1 : 2);
  return n.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 });
}

/** SIMULATOR — assumption sliders → live-recomputed outcome tiles. The deck
 *  becomes a manipulable model: the reader tests the case themselves instead of
 *  trusting one static number. Compute is a safe arithmetic evaluator, so an
 *  authored formula can never execute code. */
export function Simulator({ data, accent }: { data: SimData; accent: string }) {
  const inputs = (data.inputs ?? []).filter((i) => i && i.key);
  const outputs = (data.outputs ?? []).filter((o) => o && o.expr);
  const [vals, setVals] = useState<Record<string, number>>(() =>
    Object.fromEntries(inputs.map((i) => [i.key, i.value])),
  );
  if (!inputs.length || !outputs.length) return null;

  const cols = Math.min(outputs.length, 3);
  return (
    <div className="rounded-lg border border-current/10 p-4">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide opacity-70">
        <span aria-hidden>▶</span> Simulator
        <span className="ml-auto font-normal normal-case opacity-50">move a lever — the numbers update live</span>
      </div>

      <div className="space-y-3">
        {inputs.map((inp) => (
          <label key={inp.key} className="block">
            <div className="flex items-baseline justify-between text-sm">
              <span className="opacity-80">{inp.label}</span>
              <span className="tabular-nums font-medium" style={{ color: accent }}>
                {fmtNum(vals[inp.key] ?? inp.value)}{inp.unit ? ` ${inp.unit}` : ""}
              </span>
            </div>
            <input
              type="range" min={inp.min} max={inp.max} step={inp.step ?? 1}
              value={vals[inp.key] ?? inp.value}
              onChange={(e) => setVals((v) => ({ ...v, [inp.key]: parseFloat(e.target.value) }))}
              className="mt-1 w-full" style={{ accentColor: accent }}
            />
          </label>
        ))}
      </div>

      <div className="mt-4 grid gap-3" style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
        {outputs.map((o, i) => (
          <div key={i} className="rounded border border-current/10 p-2">
            <div className="text-2xl font-semibold tabular-nums" style={{ color: accent }}>
              {fmtNum(evalExpr(o.expr, vals), o.decimals)}{o.unit ? ` ${o.unit}` : ""}
            </div>
            <div className="mt-0.5 text-xs uppercase tracking-wide opacity-60">{o.label}</div>
          </div>
        ))}
      </div>

      {data.caption && <p className="mt-3 text-xs opacity-60">{data.caption}</p>}
    </div>
  );
}
