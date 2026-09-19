import type { ReactNode } from "react";
import type {
  FitScores,
  FitSegment,
  KnowledgeBuckets,
  RoleFitDetail,
} from "@/types/api";

/**
 * FIVE SEGMENTS, NEVER ONE COMPOSITE NUMBER.
 *
 * A single "tailoring score" sorts the fleet and tells you nothing about which
 * of five different repairs a role needs. The five say it in the row: a red
 * `structure` block means missing sections, a red `knowledge` block means the
 * sweep filled the role with no-change filler. Same pixel budget, five times
 * the information.
 *
 * The row sorts by its WORST segment rather than by the mean, because the mean
 * of a perfect role with no knowledge and a broken role with good knowledge is
 * the same number and only one of them needs work today.
 */

export const SEGMENT_ORDER: FitSegment[] = [
  "structure",
  "tiers",
  "size",
  "knowledge",
  "hygiene",
];

export const SEGMENT_LABEL: Record<FitSegment, string> = {
  structure: "Structure",
  tiers: "Tiers",
  size: "Size",
  knowledge: "Knowledge",
  hygiene: "Hygiene",
};

/** What each segment measures, in the words the repair would be described in. */
export const SEGMENT_MEANING: Record<FitSegment, string> = {
  structure:
    "Required sections present, per grade, against the canonical rubric. The worst grade wins — a perfect FULL does not make up for a micro that fails to parse.",
  tiers: "How many of the three grades exist at all.",
  size: "Lean and micro against their character budgets. FULL is reported, never scored — it is served on demand and never inlined.",
  knowledge:
    "Sourced findings as a share of the role's non-filler rows, aged by how recent the newest one is. Filler and suppressed rows are reported but kept out of the denominator.",
  hygiene:
    "A maintenance schedule, and no strings naming things that no longer exist. Tier is reported but not scored — it does not reach model routing.",
};

/**
 * Bar fill. Three bands, because a continuous gradient is not readable at 12px.
 *
 * `null` is NOT a band. A segment with nothing to measure gets the neutral
 * track colour, never green — a role with no lean and no micro showing a full
 * green size block was the exact misreading this guards.
 */
export function segmentTone(score: number | null): string {
  if (score === null) return "bg-tertiary/30";
  if (score >= 90) return "bg-success";
  if (score >= 60) return "bg-warning";
  return "bg-error";
}

export function segmentTextTone(score: number | null): string {
  if (score === null) return "text-tertiary";
  if (score >= 90) return "text-success";
  if (score >= 60) return "text-warning";
  return "text-error";
}

/** A score for a reader: the number, or N/A when nothing was measured. */
export function formatScore(score: number | null): string {
  return score === null ? "N/A" : String(score);
}

/**
 * The row-level bar: five columns, each filled to its score.
 *
 * `height` is a computed percentage, so it is a style rather than a class.
 * Colour never is — the three tones are design-system tokens.
 */
export function FitBar({
  scores,
  rubricVersion,
  className = "",
}: {
  scores: FitScores;
  rubricVersion?: string;
  className?: string;
}) {
  const scorable = SEGMENT_ORDER.filter((s) => scores[s] !== null);
  const worst = scorable.length
    ? scorable.reduce((a, b) => (scores[b]! < scores[a]! ? b : a))
    : null;
  const label = SEGMENT_ORDER.map(
    (s) => `${SEGMENT_LABEL[s]} ${formatScore(scores[s])}`,
  ).join(" · ");
  return (
    <span
      className={`inline-flex h-3.5 shrink-0 items-end gap-px ${className}`}
      title={`${label}${rubricVersion ? ` · rubric ${rubricVersion}` : ""}\n${
        worst ? `Worst: ${SEGMENT_LABEL[worst]}` : "Nothing measurable"
      }`}
      aria-label={`Fit: ${label}`}
    >
      {SEGMENT_ORDER.map((segment) => {
        const score = scores[segment];
        return (
          <span
            key={segment}
            className="relative h-full w-1 overflow-hidden rounded-[1px] bg-surface-elevated"
          >
            {/* N/A fills the whole column in the neutral tone rather than
                leaving a tall empty track that reads as a zero. */}
            <span
              className={`absolute inset-x-0 bottom-0 ${segmentTone(score)}`}
              style={{ height: score === null ? "100%" : `${Math.max(score, 4)}%` }}
            />
          </span>
        );
      })}
    </span>
  );
}

/** Five-column legend for the bar, rendered once above a list. */
export function FitBarLegend() {
  return (
    <span
      className="inline-flex shrink-0 items-center gap-px"
      title={SEGMENT_ORDER.map((s) => `${SEGMENT_LABEL[s]}: ${SEGMENT_MEANING[s]}`).join(
        "\n",
      )}
    >
      Fit
    </span>
  );
}

/**
 * The knowledge chip's grey band.
 *
 * Rows that are neither filler nor verified are NOT evidence that the role is
 * badly maintained — they predate the fetch store, so nobody knows whether the
 * URL they cite was ever fetched. They change the denominator and score
 * neither up nor down, and this band is how that shows up rather than being
 * quietly folded into one of the two buckets that do mean something.
 */
function KnowledgeBands({
  total,
  claimed,
  filler,
  unverified,
  suppressed,
  scored_rows: scoredRows,
  sourced_label: sourcedLabel,
}: KnowledgeBuckets) {
  if (total === 0) {
    return <p className="type-small text-tertiary">No knowledge rows.</p>;
  }
  const pct = (n: number) => `${(100 * n) / total}%`;
  const denominator = scoredRows;
  return (
    <div className="space-y-1.5">
      <div className="flex h-2 w-full overflow-hidden rounded-full bg-surface-elevated">
        <span className="bg-success" style={{ width: pct(claimed) }} />
        <span className="bg-tertiary/50" style={{ width: pct(unverified) }} />
        <span className="bg-error" style={{ width: pct(filler) }} />
        <span className="bg-surface" style={{ width: pct(suppressed) }} />
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-2xs text-tertiary">
        <span>
          <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-success align-middle" />
          {claimed} {sourcedLabel}
        </span>
        <span>
          <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-tertiary/50 align-middle" />
          {unverified} unverified — never counted as sourced
        </span>
        <span>
          <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-error align-middle" />
          {filler} no-change filler — reported, not in the ratio
        </span>
        {suppressed > 0 ? (
          <span>
            <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-surface align-middle" />
            {suppressed} suppressed — superseded, not scored
          </span>
        ) : null}
      </div>
      <p className="type-small text-tertiary">
        Score is {claimed} of {denominator} scored rows. Filler and suppressed
        rows are counted above but kept out of the denominator, so moving them
        out of the table cannot raise this number on its own.
      </p>
    </div>
  );
}

function SegmentCard({
  segment,
  score,
  defects,
  notes = [],
  advisory = [],
  children,
}: {
  segment: FitSegment;
  score: number | null;
  defects: string[];
  notes?: string[];
  /** Reported, never scored. Rendered apart from defects so the reader can
   *  tell "this costs you points" from "you may want to know". */
  advisory?: string[];
  children?: ReactNode;
}) {
  return (
    <section className="rounded border border-border-subtle bg-surface px-3 py-2.5">
      <header className="flex items-baseline justify-between gap-3">
        <h4 className="text-2xs font-medium case-label tracking-wider text-fg-muted">
          {SEGMENT_LABEL[segment]}
        </h4>
        <span className={`text-sm tabular-nums ${segmentTextTone(score)}`}>
          {formatScore(score)}
        </span>
      </header>
      <p className="mt-1 type-small text-tertiary">{SEGMENT_MEANING[segment]}</p>
      {children}
      {score === null ? (
        <p className="mt-2 type-small text-tertiary">
          Nothing to measure here — not scored, and left out of the fleet mean.
        </p>
      ) : defects.length > 0 ? (
        <ul className="mt-2 space-y-0.5">
          {defects.map((d) => (
            <li key={d} className="type-small text-error">
              {d}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 type-small text-success">Nothing to repair.</p>
      )}
      {notes.map((n) => (
        <p key={n} className="mt-1 type-small text-tertiary">
          {n}
        </p>
      ))}
      {advisory.map((a) => (
        <p key={a} className="mt-1 type-small text-warning">
          Advisory, not scored: {a}
        </p>
      ))}
    </section>
  );
}

/** The Fit tab body — every defect by name, never a count. */
export function FitPanel({ fit }: { fit: RoleFitDetail }) {
  const { segments } = fit;
  return (
    <div className="space-y-3 pb-4">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded border border-border-subtle bg-surface px-3 py-2">
        <div className="flex items-center gap-3">
          <FitBar scores={fit.scores} rubricVersion={fit.rubric_version} />
          <span className="type-small text-fg-muted">
            {fit.worst
              ? `Worst segment: ${SEGMENT_LABEL[fit.worst]} (${fit.worst_score})`
              : "No segment could be measured"}
          </span>
        </div>
        <span
          className="text-2xs text-tertiary"
          title="The rubric these scores were taken under. A score is only comparable to another score under the same version — editing the rubric would otherwise 'fix' the whole fleet at once."
        >
          rubric {fit.rubric_version}
        </span>
      </div>

      <SegmentCard
        segment="structure"
        score={segments.structure.score}
        defects={segments.structure.defects}
        notes={segments.structure.notes}
      />
      <SegmentCard
        segment="tiers"
        score={segments.tiers.score}
        defects={segments.tiers.defects}
      />
      <SegmentCard
        segment="size"
        score={segments.size.score}
        defects={[...segments.size.defects, ...segments.size.notes]}
      >
        <p className="mt-1.5 type-small text-tertiary">
          FULL is {segments.size.references_chars.toLocaleString()} characters.
          Reported, not scored — see Tier usage below.
        </p>
      </SegmentCard>
      <SegmentCard
        segment="knowledge"
        score={segments.knowledge.score}
        defects={segments.knowledge.defects}
      >
        <div className="mt-2">
          <KnowledgeBands
            total={segments.knowledge.total}
            claimed={segments.knowledge.claimed}
            filler={segments.knowledge.filler}
            unverified={segments.knowledge.unverified}
            suppressed={segments.knowledge.suppressed}
            scored_rows={segments.knowledge.scored_rows}
            sourced_label={segments.knowledge.sourced_label}
          />
        </div>
      </SegmentCard>
      <SegmentCard
        segment="hygiene"
        score={segments.hygiene.score}
        defects={segments.hygiene.defects}
        advisory={segments.hygiene.advisory}
      />

      <TierUsagePanel />
    </div>
  );
}

/**
 * STATIC, because the mapping is a property of the code and not of any role.
 *
 * The third row is the reason this panel exists. 1.56 million characters of
 * FULL prompt are authored across the fleet and nothing inlines them — that is
 * the single most surprising fact about okuro roles and it is invisible
 * everywhere else in the UI.
 */
export function TierUsagePanel() {
  const rows = [
    {
      grade: "micro",
      budget: "≤ 2,500 chars",
      reader: "roles_get over MCP (its default level) and the bootstrap role slice",
      purpose:
        "What every interactive Claude Code or Codex session actually receives.",
    },
    {
      grade: "lean",
      budget: "≤ 7,500 chars",
      reader: "Orchestrator dispatch, through resolve_role",
      purpose: "What every subagent receives. This is the size-scored grade.",
    },
    {
      grade: "full",
      budget: "reported, not scored",
      reader: "Nobody, unless asked: an explicit roles_get at level full, the maintenance mandate's URL scrape, and the fallback chain when lean and micro are both empty",
      purpose:
        "Reference depth, on demand only. It is never inlined into a dispatch, so its length costs nothing at run time — which is why an oversized FULL body is not a defect.",
    },
  ];

  return (
    <section className="rounded border border-border-subtle bg-surface px-3 py-2.5">
      <h4 className="text-2xs font-medium case-label tracking-wider text-fg-muted">
        Tier usage
      </h4>
      <p className="mt-1 type-small text-tertiary">
        Which grade is read on which path. Not a property of this role — of the
        code.
      </p>
      <div className="mt-2 space-y-2">
        {rows.map((r) => (
          <div
            key={r.grade}
            className="grid gap-x-3 gap-y-0.5 border-t border-border-subtle pt-2 @2xl:grid-cols-[4rem_9rem_1fr]"
          >
            <span className="text-2xs font-medium case-label tracking-wider text-accent">
              {r.grade}
            </span>
            <span className="type-small text-fg-muted">{r.budget}</span>
            <div>
              <p className="type-small text-fg-muted">{r.reader}</p>
              <p className="type-small text-tertiary">{r.purpose}</p>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
