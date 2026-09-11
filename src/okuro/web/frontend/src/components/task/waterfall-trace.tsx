import { useMemo } from "react";
import type { PhaseSummary, SubtaskStatus } from "@/types/api";
import { SUBTASK_STATUS_CONFIG } from "@/types/api";
import { formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import { SectionLabel } from "@/components/ui/section-label";
import { parseApiDate } from "@/lib/format";

interface WaterfallTraceProps {
  phases: PhaseSummary[];
  selectedSubtask?: string;
  onSelectSubtask?: (id: string) => void;
}

interface Span {
  id: string;
  role: string;
  phaseId: number;
  startMs: number;
  endMs: number;
  status: SubtaskStatus | string;
  durationMs: number;
}

/**
 * Horizontal waterfall — each subtask rendered as a span on a shared time
 * axis. LangSmith / Phoenix / Langfuse-style trace viewer.
 *
 * Wave 2 W2.1. Native fit for subagent trees: phase lanes show parallelism,
 * bar width shows duration, tone shows status.
 */
export function WaterfallTrace({
  phases,
  selectedSubtask,
  onSelectSubtask,
}: WaterfallTraceProps) {
  const spans = useMemo(() => buildSpans(phases), [phases]);

  if (spans.length === 0) {
    return (
      <EmptyState
        title="No timing data yet"
        description="Waterfall populates once parts have started. Watch the pipeline view for live status."
      />
    );
  }

  const minMs = Math.min(...spans.map((s) => s.startMs));
  const maxMs = Math.max(...spans.map((s) => s.endMs));
  const totalMs = Math.max(1, maxMs - minMs);
  const scale = (ms: number) => `${((ms - minMs) / totalMs) * 100}%`;
  const width = (dur: number) => `${Math.max(0.5, (dur / totalMs) * 100)}%`;

  // Time gridlines — 5 evenly spaced ticks
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => ({
    pct: f * 100,
    ms: minMs + f * totalMs,
  }));

  return (
    <div className="space-y-3 px-10 py-4">
      <div className="flex items-baseline justify-between">
        <SectionLabel>Trace · {spans.length} spans</SectionLabel>
        <span className="text-3xs text-tertiary tabular-nums">
          total {formatDuration((totalMs) / 1000)}
        </span>
      </div>

      {/* Time axis header */}
      <div className="relative h-5 border-b border-border-subtle">
        {ticks.map((t) => (
          <div
            key={t.pct}
            className="absolute top-0 h-full border-l border-border-subtle text-3xs text-tertiary tabular-nums pl-1"
            style={{ left: `${t.pct}%` }}
          >
            {t.pct === 0
              ? "0"
              : `+${formatDuration((t.ms - minMs) / 1000)}`}
          </div>
        ))}
      </div>

      {/* Span rows */}
      <div className="space-y-0.5">
        {spans.map((s) => (
          <SpanRow
            key={s.id}
            span={s}
            selected={s.id === selectedSubtask}
            scaleLeft={scale(s.startMs)}
            barWidth={width(s.durationMs)}
            onSelect={() => onSelectSubtask?.(s.id)}
          />
        ))}
      </div>
    </div>
  );
}

function SpanRow({
  span,
  selected,
  scaleLeft,
  barWidth,
  onSelect,
}: {
  span: Span;
  selected: boolean;
  scaleLeft: string;
  barWidth: string;
  onSelect: () => void;
}) {
  // C10 — color now travels via span.color_class on the snapshot; the
  // glyph table here holds icons only. Bar background continues to map
  // span.status directly via the small switch below — that's the
  // chronological view's own concern, not the closed FE color enum.
  const cfg =
    SUBTASK_STATUS_CONFIG[span.status as SubtaskStatus] ?? { icon: "·" };
  const bg =
    span.status === "done"
      ? "bg-success/60"
      : span.status === "failed"
        ? "bg-error/60"
        : span.status === "running"
          ? "bg-accent/60 motion-safe:animate-pulse"
          : "bg-fg-muted/30";

  return (
    <button
      onClick={onSelect}
      title={span.role}
      className={cn(
        "group grid w-full grid-cols-[auto_auto_minmax(8rem,11rem)_1fr_auto] items-center gap-3 rounded px-2 py-1.5 text-left transition-colors",
        selected
          ? "bg-accent-subtle border border-accent/30"
          : "border border-transparent hover:bg-surface-elevated",
      )}
    >
      <span className="text-3xs tabular-nums text-tertiary w-8">
        P{span.phaseId}
      </span>
      <span className={cn("text-2xs shrink-0 text-tertiary")}>
        {cfg.icon}
      </span>
      <span className="truncate text-2xs text-fg-muted">
        {span.role}
      </span>
      <div className="relative h-4 min-w-0">
        {/* Track */}
        <div className="absolute inset-0 top-1.5 h-0.5 bg-border-subtle rounded-full" />
        {/* Bar */}
        <div
          className={cn("absolute top-0.5 h-3 rounded-sm", bg)}
          style={{ left: scaleLeft, width: barWidth }}
        />
      </div>
      <span className="text-3xs tabular-nums text-tertiary w-16 text-right">
        {formatDuration(span.durationMs / 1000)}
      </span>
    </button>
  );
}

function buildSpans(phases: PhaseSummary[]): Span[] {
  const spans: Span[] = [];
  for (const phase of phases) {
    for (const st of phase.subtasks) {
      const startStr = st.started_at;
      if (!startStr) continue;
      const startMs = parseApiDate(startStr).getTime();
      if (isNaN(startMs)) continue;
      // Duration known if subtask has `duration` (seconds) or is still running.
      const durationSec =
        typeof st.duration === "number" && st.duration > 0
          ? st.duration
          : st.status === "running"
            ? (Date.now() - startMs) / 1000
            : 0;
      const durationMs = durationSec * 1000;
      spans.push({
        id: st.id,
        role: st.role,
        phaseId: phase.id,
        startMs,
        endMs: startMs + durationMs,
        durationMs,
        status: st.status,
      });
    }
  }
  spans.sort((a, b) => a.startMs - b.startMs);
  return spans;
}
