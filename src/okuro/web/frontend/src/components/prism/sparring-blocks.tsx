// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism sparring blocks — the adversarial module class that turns
//   a deck from assertion into contest. RedTeam (a multi-model adversary panel's
//   ranked objections + rebuttals) and DecisionRecord (an auditable ADR with a
//   reversal trigger). Dependency-free, brand-themed, theme-aware.
// index: SEVERITY | RedTeam | DecisionRecord
// AGENT_HEADER_END -->
import type { Block } from "@/lib/prism-api";

type RedTeamData = Extract<Block, { type: "redteam" }>;
type DecisionData = Extract<Block, { type: "decisionrecord" }>;
type LedgerData = Extract<Block, { type: "assumptionledger" }>;
type TripwireData = Extract<Block, { type: "tripwires" }>;
type RiskData = Extract<Block, { type: "risk" }>;

// Objection damage → color. Reuses the design system's status ramp so severity
// reads the same as good/warn/bad everywhere else in a deck.
const SEVERITY: Record<string, { color: string; label: string }> = {
  high: { color: "var(--color-status-error,#f92f77)", label: "HIGH" },
  med: { color: "var(--color-status-warning,#c52998)", label: "MED" },
  low: { color: "var(--color-status-success,#11d425)", label: "LOW" },
};

/** RED TEAM — a panel of adversaries stress-testing the argument. Each row is
 *  one objection, ranked by the damage it does, tagged with WHICH mind raised it
 *  (the multi-model provenance), with the steelman rebuttal beneath. */
export function RedTeam({ data, accent }: { data: RedTeamData; accent: string }) {
  const challenges = (data.challenges ?? []).filter((c) => c && c.objection);
  if (!challenges.length) return null;
  return (
    <div className="rounded-lg border border-current/10 p-3">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide opacity-70">
        <span aria-hidden>⚔</span> Red team
        {data.panel?.length ? (
          <span className="ml-auto font-normal normal-case opacity-60">panel: {data.panel.join(" · ")}</span>
        ) : null}
      </div>
      <ol className="space-y-3">
        {challenges.map((c, i) => {
          const sev = SEVERITY[c.severity ?? ""] ?? null;
          return (
            <li key={i} className="border-l-2 pl-3" style={{ borderColor: sev?.color ?? accent }}>
              <div className="flex items-start gap-2">
                {sev && (
                  <span className="mt-0.5 rounded px-1.5 py-0.5 text-[10px] font-bold" style={{ background: `${sev.color}22`, color: sev.color }}>
                    {sev.label}
                  </span>
                )}
                <p className="text-sm font-medium">{c.objection}</p>
              </div>
              {c.rebuttal && (
                <p className="mt-1 text-sm opacity-70">
                  <span className="font-semibold opacity-90">Rebuttal:</span> {c.rebuttal}
                </p>
              )}
              {c.who && <div className="mt-1 text-[10px] uppercase tracking-wide opacity-45">raised by {c.who}</div>}
            </li>
          );
        })}
      </ol>
      {data.caption && <p className="mt-3 text-xs opacity-60">{data.caption}</p>}
    </div>
  );
}

/** DECISION RECORD — the sparring crystallized into an auditable ADR: the
 *  question, the call, what was rejected and why, and the trip-wire that would
 *  reverse it. The artifact a high player signs off on. */
export function DecisionRecord({ data, accent }: { data: DecisionData; accent: string }) {
  if (!data.decision && !data.question) return null;
  const options = (data.options ?? []).filter((o) => o && o.label);
  return (
    <div className="rounded-lg border p-4" style={{ borderColor: `${accent}55` }}>
      <div className="mb-1 text-xs font-semibold uppercase tracking-wide opacity-60">Decision record</div>
      {data.question && <p className="text-sm opacity-70">{data.question}</p>}
      {data.decision && (
        <p className="mt-1 text-base font-semibold" style={{ color: accent }}>{data.decision}</p>
      )}
      {options.length > 0 && (
        <ul className="mt-3 space-y-1.5 text-sm">
          {options.map((o, i) => (
            <li key={i} className="flex items-start gap-2">
              <span className="mt-0.5 shrink-0" style={{ color: o.chosen ? accent : "currentColor", opacity: o.chosen ? 1 : 0.4 }}>
                {o.chosen ? "✓" : "✗"}
              </span>
              <span>
                <span className={o.chosen ? "font-medium" : "opacity-70"}>{o.label}</span>
                {!o.chosen && o.rejected_because && (
                  <span className="opacity-55"> — {o.rejected_because}</span>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      {data.reversal_trigger && (
        <div className="mt-3 rounded border-l-2 py-1 pl-3 text-sm" style={{ borderColor: "var(--color-status-warning,#c52998)" }}>
          <span className="font-semibold uppercase opacity-70">Reverse if:</span>{" "}
          <span className="opacity-80">{data.reversal_trigger}</span>
        </div>
      )}
      {data.confidence && <div className="mt-2 text-[11px] uppercase tracking-wide opacity-50">confidence: {data.confidence}</div>}
    </div>
  );
}

// Assumption confidence → a small filled/empty pip meter (3 pips) + color.
const CONF: Record<string, { pips: number; color: string }> = {
  high: { pips: 3, color: "var(--color-status-success,#11d425)" },
  med: { pips: 2, color: "var(--color-status-warning,#c52998)" },
  low: { pips: 1, color: "var(--color-status-error,#f92f77)" },
};

/** ASSUMPTION LEDGER — what the whole case rests on. Each row: the assumption,
 *  a confidence meter, and the FALSIFIER (what would change your mind). The
 *  sparring move: make the load-bearing beliefs explicit and attackable. */
export function AssumptionLedger({ data, accent }: { data: LedgerData; accent: string }) {
  const rows = (data.assumptions ?? []).filter((a) => a && a.text);
  if (!rows.length) return null;
  return (
    <div className="rounded-lg border border-current/10 p-3">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide opacity-70">
        <span aria-hidden>⚖</span> Assumption ledger
      </div>
      <ul className="space-y-3">
        {rows.map((a, i) => {
          const conf = CONF[a.confidence ?? ""] ?? null;
          return (
            <li key={i} className="flex items-start gap-3">
              <span className="mt-1 flex shrink-0 gap-0.5" title={a.confidence ? `confidence: ${a.confidence}` : undefined}>
                {[0, 1, 2].map((p) => (
                  <span key={p} className="inline-block h-2 w-2 rounded-full" style={{ background: conf && p < conf.pips ? conf.color : "currentColor", opacity: conf && p < conf.pips ? 1 : 0.15 }} />
                ))}
              </span>
              <div>
                <p className={`text-sm ${a.load_bearing ? "font-semibold" : ""}`}>
                  {a.text}
                  {a.load_bearing && <span className="ml-2 rounded px-1 py-0.5 text-[9px] uppercase tracking-wide" style={{ background: `${accent}22`, color: accent }}>load-bearing</span>}
                </p>
                {a.falsifier && (
                  <p className="mt-0.5 text-xs opacity-60">
                    <span className="uppercase tracking-wide opacity-80">changes if:</span> {a.falsifier}
                  </p>
                )}
              </div>
            </li>
          );
        })}
      </ul>
      {data.caption && <p className="mt-3 text-xs opacity-60">{data.caption}</p>}
    </div>
  );
}

// Trip-wire status → color + glyph. Pre-committed reversal conditions.
const WIRE: Record<string, { color: string; glyph: string }> = {
  clear: { color: "var(--color-status-success,#11d425)", glyph: "●" },
  watch: { color: "var(--color-status-warning,#c52998)", glyph: "◐" },
  tripped: { color: "var(--color-status-error,#f92f77)", glyph: "✕" },
};

/** TRIP-WIRES — pre-committed reversal conditions with live status. Decision
 *  hygiene high players respect: "we agreed to reverse if X crosses Y." */
export function Tripwires({ data }: { data: TripwireData; accent: string }) {
  const wires = (data.wires ?? []).filter((w) => w && w.condition);
  if (!wires.length) return null;
  return (
    <div className="rounded-lg border border-current/10 p-3">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide opacity-70">
        <span aria-hidden>⚠</span> Trip-wires
      </div>
      <ul className="space-y-2">
        {wires.map((w, i) => {
          const st = WIRE[w.status ?? ""] ?? { color: "var(--color-status-success,#11d425)", glyph: "●" };
          return (
            <li key={i} className="flex items-start gap-2 text-sm">
              <span className="mt-0.5 shrink-0" style={{ color: st.color }} title={w.status ?? "clear"}>{st.glyph}</span>
              <span>
                <span className="opacity-90">{w.condition}</span>
                {w.metric && <span className="ml-1 opacity-55">— {w.metric}</span>}
              </span>
            </li>
          );
        })}
      </ul>
      {data.caption && <p className="mt-3 text-xs opacity-60">{data.caption}</p>}
    </div>
  );
}

/** RISK REGISTER — the named risks of a plan, each graded by severity (impact)
 *  and likelihood, with its mitigation and owner. A severity ring keys the row
 *  to the same status ramp used across the deck. */
export function RiskRegister({ data }: { data: RiskData; accent: string }) {
  const items = (data.items ?? []).filter((r) => r && r.title);
  if (!items.length) return null;
  return (
    <div className="rounded-lg border border-current/10 p-3">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide opacity-70">
        <span aria-hidden>⚑</span> Risk register
      </div>
      <ul className="space-y-2.5">
        {items.map((r, i) => {
          const sev = SEVERITY[r.severity ?? ""] ?? { color: "var(--color-status-warning,#c52998)", label: "MED" };
          return (
            <li key={i} className="flex items-start gap-2.5 border-t border-current/5 pt-2.5 text-sm first:border-0 first:pt-0">
              <span className="mt-1 h-2.5 w-2.5 shrink-0 rounded-full ring-2 ring-current/20" style={{ background: sev.color }} title={`severity ${sev.label}`} />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className="font-medium">{r.title}</span>
                  <span className="text-[10px] uppercase tracking-wide opacity-50">
                    sev {sev.label}{r.likelihood ? ` · likely ${(SEVERITY[r.likelihood] ?? { label: r.likelihood }).label}` : ""}
                  </span>
                  {r.owner && <span className="ml-auto rounded border border-current/15 px-1.5 py-0.5 text-[10px] opacity-60">{r.owner}</span>}
                </div>
                {r.mitigation && <p className="mt-0.5 opacity-65"><span className="opacity-50">→ </span>{r.mitigation}</p>}
              </div>
            </li>
          );
        })}
      </ul>
      {data.caption && <p className="mt-3 text-xs opacity-60">{data.caption}</p>}
    </div>
  );
}
