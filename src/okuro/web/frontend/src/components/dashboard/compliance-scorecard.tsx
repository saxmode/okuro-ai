import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { SectionLabel } from "@/components/ui/section-label";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { EmptyState } from "@/components/ui/empty-state";
import { cn } from "@/lib/utils";
import { displayAgent } from "@/lib/format";

interface ComplianceRow {
  provider: string;
  total_sessions: number;
  scored_sessions: number;
  avg_score: number;
  avg_normalized: number;
  bootstrap_rate: number;
  report_rate: number;
  cortex_rate: number;
  memory_rate: number;
  progress_rate: number;
  last_updated: string | null;
}

interface ComplianceData {
  providers: ComplianceRow[];
}

/**
 * Per-provider compliance scorecard — Wave 2 W2.4.
 * Shows bootstrap / cortex / memory / progress / report rates.
 * Color cue on avg_normalized (green ≥0.8, warning ≥0.6, error below).
 */
export function ComplianceScorecard() {
  const { data, isLoading } = useQuery({
    queryKey: ["compliance"],
    queryFn: () => api<ComplianceData>("/api/dashboard/compliance"),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  return (
    <section className="space-y-3">
      <SectionLabel>Provider compliance</SectionLabel>
      {isLoading ? (
        <LoadingSkeleton lines={3} />
      ) : !data || data.providers.length === 0 ? (
        <EmptyState
          title="No compliance data yet"
          description="Maintenance aggregates provider stats from the sessions table. Run okuro maintenance to populate."
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-3xs uppercase tracking-wider text-tertiary text-left">
                <th className="py-2 pr-3 font-medium">Provider</th>
                <th className="py-2 pr-3 font-medium text-right">Sessions</th>
                <th className="py-2 pr-3 font-medium text-right">Score</th>
                <th className="py-2 pr-3 font-medium text-right">Bootstrap</th>
                <th className="py-2 pr-3 font-medium text-right">Cortex</th>
                <th className="py-2 pr-3 font-medium text-right">Memory</th>
                <th className="py-2 pr-3 font-medium text-right">Progress</th>
                <th className="py-2 pr-3 font-medium text-right">Report</th>
              </tr>
            </thead>
            <tbody>
              {data.providers
                .filter((p) => p.provider && p.provider.toLowerCase() !== "unknown")
                .map((p) => (
                  <ProviderRow key={p.provider} row={p} />
                ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function ProviderRow({ row }: { row: ComplianceRow }) {
  const scoreTone = scoreColor(row.avg_normalized);
  return (
    <tr className="border-t border-border-subtle">
      <td className="py-2 pr-3 text-sm font-medium text-fg" title={row.provider}>
        {displayAgent(row.provider)}
      </td>
      <td className="py-2 pr-3 text-right tabular-nums text-fg-muted">
        {row.total_sessions}
      </td>
      <td className="py-2 pr-3 text-right tabular-nums">
        <span className={cn("font-medium", scoreTone)}>
          {Math.round(row.avg_normalized * 100)}
        </span>
      </td>
      <RateCell value={row.bootstrap_rate} />
      <RateCell value={row.cortex_rate} />
      <RateCell value={row.memory_rate} />
      <RateCell value={row.progress_rate} />
      <RateCell value={row.report_rate} />
    </tr>
  );
}

function RateCell({ value }: { value: number }) {
  return (
    <td className="py-2 pr-3 text-right tabular-nums">
      <div className="inline-flex items-center gap-1.5">
        <div className="h-1 w-10 rounded-full bg-border">
          <div
            className={cn("h-full rounded-full", rateFill(value))}
            style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
            aria-hidden="true"
          />
        </div>
        <span className={cn("w-8 text-right", rateText(value))}>
          {Math.round(value)}
        </span>
      </div>
    </td>
  );
}

function scoreColor(v: number): string {
  if (v >= 0.8) return "text-success";
  if (v >= 0.6) return "text-warning";
  return "text-error";
}

function rateFill(v: number): string {
  if (v >= 80) return "bg-success";
  if (v >= 60) return "bg-warning";
  return "bg-error";
}

function rateText(v: number): string {
  if (v >= 80) return "text-fg-muted";
  if (v >= 60) return "text-warning";
  return "text-error";
}
