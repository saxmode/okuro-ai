// <!-- AGENT_HEADER
// role: code
// purpose: okuro·prism rigor blocks — the trust + decision-science moat that
//   makes an AI recommendation credible to a skeptical board. Evidence (a
//   source-graded "nutrition label" with confidence + as-of provenance),
//   BiasCheck (names the cognitive biases threatening THIS decision + pre-empts
//   them), MessageProof (each core message × distinct proof points, re-weighted
//   per audience — Bain's five-element equity story). Dependency-free, themed.
// index: GRADE | CONF_PIPS | Evidence | SEV | BiasCheck | MessageProof
// AGENT_HEADER_END -->
import { useState } from "react";

import type { Block } from "@/lib/prism-api";

type EvidenceData = Extract<Block, { type: "evidence" }>;
type BiasData = Extract<Block, { type: "biascheck" }>;
type MessageProofData = Extract<Block, { type: "messageproof" }>;

// Source grade → how much weight it carries. Primary source (data/filing) is the
// gold standard; a model output or a bare assumption is flagged as weaker — the
// honesty that earns board trust.
const GRADE: Record<string, { label: string; color: string }> = {
  primary: { label: "primary", color: "var(--color-status-success,#11d425)" },
  secondary: { label: "secondary", color: "var(--color-status-info,#2998fd)" },
  model: { label: "model", color: "var(--color-status-warning,#c52998)" },
  assumption: { label: "assumption", color: "var(--color-status-error,#f92f77)" },
};

const CONF_PIPS: Record<string, number> = { high: 3, med: 2, low: 1 };

/** EVIDENCE — the provenance nutrition label. Every claim carries its source,
 *  a confidence meter, an as-of date (what we knew when), and a source GRADE, so
 *  a skeptical reader can audit the recommendation instead of trusting it. */
export function Evidence({ data, accent }: { data: EvidenceData; accent: string }) {
  const claims = (data.claims ?? []).filter((c) => c && c.text);
  if (!claims.length) return null;
  return (
    <div className="rounded-lg border border-current/10 p-3">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide opacity-70">
        <span aria-hidden>🔎</span> Evidence
      </div>
      <ul className="space-y-3">
        {claims.map((c, i) => {
          const g = GRADE[c.grade ?? ""] ?? null;
          const pips = CONF_PIPS[c.confidence ?? ""] ?? 0;
          return (
            <li key={i} className="border-l-2 pl-3" style={{ borderColor: g?.color ?? accent }}>
              <p className="text-sm">{c.text}</p>
              <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] opacity-70">
                {g && (
                  <span className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase" style={{ background: `${g.color}22`, color: g.color }}>
                    {g.label}
                  </span>
                )}
                {c.source && <span className="opacity-90">source: {c.source}</span>}
                {c.as_of && <span>as of {c.as_of}</span>}
                {pips > 0 && (
                  <span className="flex items-center gap-1">
                    confidence
                    <span className="flex gap-0.5">
                      {[0, 1, 2].map((k) => (
                        <span
                          key={k}
                          className="inline-block h-2 w-2 rounded-full"
                          style={{ background: accent, opacity: k < pips ? 0.9 : 0.2 }}
                        />
                      ))}
                    </span>
                  </span>
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

const SEV: Record<string, string> = {
  high: "var(--color-status-error,#f92f77)",
  med: "var(--color-status-warning,#c52998)",
  low: "var(--color-status-success,#11d425)",
};

/** BIASCHECK — board-bias pre-emption. Names the specific cognitive biases that
 *  threaten THIS decision (authority, confirmation, sunk-cost…) and pre-empts
 *  each with counter-evidence / a dissent frame. Signals the recommendation
 *  survived its own scrutiny — the opposite of a one-sided pitch. */
export function BiasCheck({ data, accent }: { data: BiasData; accent: string }) {
  const biases = (data.biases ?? []).filter((b) => b && b.bias);
  if (!biases.length) return null;
  return (
    <div className="rounded-lg border border-current/10 p-3">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide opacity-70">
        <span aria-hidden>🧭</span> Bias pre-emption
      </div>
      <ul className="space-y-3">
        {biases.map((b, i) => {
          const color = SEV[b.severity ?? ""] ?? accent;
          return (
            <li key={i} className="border-l-2 pl-3" style={{ borderColor: color }}>
              <div className="flex items-center gap-2">
                <span className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase" style={{ background: `${color}22`, color }}>
                  {b.bias}
                </span>
              </div>
              <p className="mt-1 text-sm opacity-80">
                <span className="font-semibold opacity-90">Risk:</span> {b.risk}
              </p>
              {b.counter && (
                <p className="mt-1 text-sm opacity-80">
                  <span className="font-semibold opacity-90">Counter:</span> {b.counter}
                </p>
              )}
            </li>
          );
        })}
      </ul>
      {data.caption && <p className="mt-3 text-xs opacity-60">{data.caption}</p>}
    </div>
  );
}

const WEIGHT_OPACITY: Record<string, number> = { high: 1, med: 0.8, low: 0.6 };

/** MESSAGEPROOF — the equity story. Each core message is backed by distinct
 *  proof points; when an audience is selected, the proofs that land for THAT
 *  role surface and the rest dim — so one deck carries a board version, an
 *  investor version, a regulator version, and shows WHY each believes it. */
export function MessageProof({ data, accent }: { data: MessageProofData; accent: string }) {
  const messages = (data.messages ?? []).filter((m) => m && m.message);
  const audiences = (data.audiences ?? []).filter(Boolean);
  const [aud, setAud] = useState<string | null>(null);
  if (!messages.length) return null;

  const matches = (proofAuds?: string[]) => !aud || !proofAuds?.length || proofAuds.includes(aud);

  return (
    <div className="rounded-lg border border-current/10 p-3">
      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs font-semibold uppercase tracking-wide opacity-70">
        <span aria-hidden>🎯</span> Message &amp; proof
        {audiences.length > 0 && (
          <span className="ml-auto flex flex-wrap gap-1">
            <button
              onClick={() => setAud(null)}
              className="rounded border px-1.5 py-0.5 text-[10px] normal-case tracking-normal"
              style={{ borderColor: aud === null ? accent : "currentColor", color: aud === null ? accent : undefined, opacity: aud === null ? 1 : 0.5 }}
            >
              all
            </button>
            {audiences.map((a) => (
              <button
                key={a}
                onClick={() => setAud(a)}
                className="rounded border px-1.5 py-0.5 text-[10px] normal-case tracking-normal"
                style={{ borderColor: aud === a ? accent : "currentColor", color: aud === a ? accent : undefined, opacity: aud === a ? 1 : 0.5 }}
              >
                {a}
              </button>
            ))}
          </span>
        )}
      </div>
      <div className="space-y-3">
        {messages.map((m, i) => {
          const proofs = (m.proofs ?? []).filter((p) => p && p.text);
          const ranked = proofs
            .slice()
            .sort((a, b) => (WEIGHT_OPACITY[b.weight ?? "med"] ?? 0.8) - (WEIGHT_OPACITY[a.weight ?? "med"] ?? 0.8));
          return (
            <div key={i} className="rounded-md border border-current/10 p-2.5">
              <p className="text-sm font-semibold" style={{ color: accent }}>{m.message}</p>
              <ul className="mt-1.5 space-y-1">
                {ranked.map((p, k) => {
                  const on = matches(p.audiences);
                  return (
                    <li key={k} className="flex items-start gap-2 text-sm" style={{ opacity: on ? (WEIGHT_OPACITY[p.weight ?? "med"] ?? 0.8) : 0.25 }}>
                      <span className="mt-1 shrink-0" style={{ color: accent }}>▸</span>
                      <span>
                        {p.text}
                        {p.audiences?.length ? (
                          <span className="ml-1.5 text-[10px] uppercase tracking-wide opacity-50">[{p.audiences.join(", ")}]</span>
                        ) : null}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </div>
      {data.caption && <p className="mt-3 text-xs opacity-60">{data.caption}</p>}
    </div>
  );
}
