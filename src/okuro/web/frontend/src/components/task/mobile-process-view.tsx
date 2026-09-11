import { useState } from "react";
import { ChevronDown, ChevronRight, ArrowLeft } from "lucide-react";
import type {
  TaskSnapshot,
  PhaseSummary,
  SubtaskSummary,
  ActivityEvent,
  ArtifactInfo,
} from "@/types/api";
import { Segmented } from "@/components/ui/segmented";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { AgentPanel } from "./agent-panel";
import { ActivityFeed } from "./activity-feed";
import { ArtifactsViewer } from "./artifacts-viewer";
import { useTaskReview } from "@/hooks/use-review";
import { cn } from "@/lib/utils";

/**
 * MobileProcessView — phone information architecture for the task pipeline.
 *
 * Replaces the desktop two-column split (which forces a cramped %-width
 * column + 3 nested scroll areas onto a phone). Hybrid pattern:
 *   - Phases render as accordion sections (2-level: phase → subtasks).
 *   - Tapping a subtask drills into a full-screen detail sheet with
 *     segmented sub-tabs (Transcript / Activity / Files) — one focus per
 *     screen, a single scroll, a back button.
 * Desktop never mounts this; it stays behind the task-detail mobile branch.
 */

// color_class tone → dot background. Tokens already exist in the build.
const DOT: Record<string, string> = {
  success: "bg-success",
  warning: "bg-warning",
  error: "bg-error",
  danger: "bg-error",
  accent: "bg-accent",
  info: "bg-info",
  tertiary: "bg-tertiary",
  disabled: "bg-disabled",
};
const dot = (cc?: string) => DOT[cc ?? ""] ?? "bg-tertiary";

// Phases that should start expanded — the ones that need attention now.
const OPEN_BY_DEFAULT = new Set(["running", "executing", "blocked_review"]);

type DetailTab = "transcript" | "activity" | "files";

interface MobileProcessViewProps {
  snapshot: TaskSnapshot | null | undefined;
  activity: ActivityEvent[];
  taskId: string;
  artifacts: ArtifactInfo[];
}

export function MobileProcessView({
  snapshot,
  activity,
  taskId,
  artifacts,
}: MobileProcessViewProps) {
  const phases: PhaseSummary[] = (snapshot?.phases as PhaseSummary[] | undefined) ?? [];
  const { data: reviewData } = useTaskReview(taskId);

  const [open, setOpen] = useState<Set<number>>(
    () => new Set(phases.filter((p) => OPEN_BY_DEFAULT.has(p.state ?? "")).map((p) => p.id)),
  );
  const [detail, setDetail] = useState<SubtaskSummary | null>(null);
  const [tab, setTab] = useState<DetailTab>("transcript");

  function togglePhase(id: number) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function openDetail(sub: SubtaskSummary) {
    setDetail(sub);
    setTab("transcript");
  }

  if (phases.length === 0) {
    return (
      <p className="p-6 text-center text-sm text-tertiary">
        No steps yet — the orchestrator is still planning.
      </p>
    );
  }

  return (
    <div className="space-y-2 p-3">
      {phases.map((phase) => {
        const isOpen = open.has(phase.id);
        const subs = phase.subtasks ?? [];
        const done = subs.filter((s) => s.status === "done").length;
        return (
          <section
            key={phase.id}
            className="overflow-hidden rounded-lg border border-border bg-surface"
          >
            <button
              type="button"
              onClick={() => togglePhase(phase.id)}
              aria-expanded={isOpen}
              className="flex w-full items-center gap-3 px-3 py-3 text-left transition-colors hover:bg-surface-elevated"
            >
              <span className={cn("h-2.5 w-2.5 shrink-0 rounded-full", dot(phase.color_class))} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium text-fg">{phase.name}</div>
                <div className="text-2xs uppercase tracking-wider text-tertiary">
                  {phase.label ?? phase.status} · {done}/{subs.length}
                </div>
              </div>
              {isOpen ? (
                <ChevronDown className="h-4 w-4 shrink-0 text-tertiary" />
              ) : (
                <ChevronRight className="h-4 w-4 shrink-0 text-tertiary" />
              )}
            </button>

            {isOpen && (
              <ul className="border-t border-border">
                {subs.length === 0 ? (
                  <li className="px-3 py-3 text-2xs text-tertiary">No parts.</li>
                ) : (
                  subs.map((s) => (
                    <li key={s.id} className="border-b border-border last:border-b-0">
                      <button
                        type="button"
                        onClick={() => openDetail(s)}
                        className="flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-surface-elevated"
                      >
                        <span className={cn("h-2 w-2 shrink-0 rounded-full", dot(s.color_class))} />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-baseline gap-2">
                            <span className="text-2xs font-mono text-tertiary">{s.id}</span>
                            <span className="truncate text-xs font-medium text-fg">{s.role}</span>
                          </div>
                          <div className="truncate text-2xs text-tertiary">{s.label}</div>
                        </div>
                        <ChevronRight className="h-4 w-4 shrink-0 text-tertiary" />
                      </button>
                    </li>
                  ))
                )}
              </ul>
            )}
          </section>
        );
      })}

      {/* Drill-in detail — full-screen, segmented sub-tabs, back button. */}
      <Sheet open={!!detail} onOpenChange={(o) => !o && setDetail(null)}>
        <SheetContent side="right" className="w-full max-w-full p-0" showCloseButton={false}>
          <SheetTitle className="sr-only">
            {detail ? `${detail.id} ${detail.role}` : "Part detail"}
          </SheetTitle>
          <div className="flex items-center gap-2 border-b border-border px-3 py-2.5">
            <button
              type="button"
              onClick={() => setDetail(null)}
              aria-label="Back to process"
              className="flex h-8 w-8 items-center justify-center rounded text-tertiary transition-colors hover:bg-surface-elevated hover:text-fg"
            >
              <ArrowLeft className="h-4 w-4" />
            </button>
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline gap-2">
                <span className="text-2xs font-mono text-tertiary">{detail?.id}</span>
                <span className="truncate text-sm font-medium text-fg">{detail?.role}</span>
              </div>
            </div>
          </div>
          <div className="px-3 py-2">
            <Segmented<DetailTab>
              ariaLabel="Part detail view"
              value={tab}
              onChange={setTab}
              options={[
                { label: "Transcript", value: "transcript" },
                { label: "Activity", value: "activity" },
                { label: "Files", value: "files" },
              ]}
            />
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {detail && tab === "transcript" && <AgentPanel subtask={detail} />}
            {detail && tab === "activity" && (
              <ActivityFeed
                events={activity}
                selectedSubtaskId={detail.id}
                reviewVerdicts={reviewData?.verdicts}
              />
            )}
            {detail && tab === "files" && (
              <ArtifactsViewer
                taskId={taskId}
                artifacts={artifacts}
                selectedSubtaskId={detail.id}
                onClearSubtaskFilter={() => {}}
              />
            )}
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}
