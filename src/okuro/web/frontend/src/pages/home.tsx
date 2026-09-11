import { Link } from "react-router";
import { Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { useTasks, useSystemStatus } from "@/hooks/use-tasks";
import { useDashboardBrain } from "@/hooks/use-dashboard";
import { usePulseData, type LiveAgent } from "@/hooks/use-activity-stream";
import { formatAge, shortId, displayAgent } from "@/lib/format";
import type { TaskSummary } from "@/types/api";
import { Button } from "@/components/ui/button";
import { SectionLabel } from "@/components/ui/section-label";
import { EmptyState } from "@/components/ui/empty-state";
import { CreateDialog } from "@/components/task/create-dialog";
import { WelcomePanel } from "@/components/dashboard/welcome-panel";
import { PageHeader } from "@/components/shell/page-header";
import { NeedsYouStrip } from "@/components/inbox/NeedsYouStrip";
import { parseApiDate } from "@/lib/format";

const LAST_VISIT_KEY = "okuro-last-visit";

interface LastVisit {
  at: string;
  path?: string;
  counts?: {
    memory: number;
    progress: number;
    sessions: number;
  };
}

function loadLastVisit(): LastVisit | null {
  try {
    const raw = localStorage.getItem(LAST_VISIT_KEY);
    return raw ? (JSON.parse(raw) as LastVisit) : null;
  } catch {
    return null;
  }
}

function saveLastVisit(v: LastVisit) {
  try {
    localStorage.setItem(LAST_VISIT_KEY, JSON.stringify(v));
  } catch {
    /* ignore */
  }
}

/**
 * / — the "Now" page. Answer three questions in <3 seconds each:
 *  1) Are my agents working right now?
 *  2) What needs me?
 *  3) Is anything broken?
 *
 * No 2×2 grid. One narrative sentence. Two focal zones. One resume anchor.
 */
export function HomePage() {
  const { data: status } = useSystemStatus();
  const { data: tasksData } = useTasks({ limit: 20 });
  const { data: brain } = useDashboardBrain();
  const { activeRoles, liveAgents, activity, connected } = usePulseData();
  const [createOpen, setCreateOpen] = useState(false);
  const [previousVisit] = useState(() => loadLastVisit());

  // Persist this visit after first paint so `previousVisit` reflects the
  // *previous* session's counts, not the one we just rendered.
  useEffect(() => {
    if (!brain) return;
    saveLastVisit({
      at: new Date().toISOString(),
      path: "/",
      counts: {
        memory: brain.memory.length,
        progress: brain.progress.length,
        sessions: brain.sessions.length,
      },
    });
  }, [brain]);

  const activeTasks = (tasksData?.tasks ?? []).filter(
    (t) => t.status === "active" || t.status === "planning",
  );
  const recentTasks = (tasksData?.tasks ?? [])
    .filter((t) => t.status !== "active" && t.status !== "planning")
    .slice(0, 5);

  // Stale thoughts: open, older than 3 days
  const threeDaysAgoMs = Date.now() - 3 * 24 * 60 * 60 * 1000;
  const staleThoughts = (brain?.thoughts ?? []).filter((t) => {
    if (t.status !== "open") return false;
    const ts = parseApiDate(t.created_at).getTime();
    return !isNaN(ts) && ts < threeDaysAgoMs;
  });

  // Counts delta since last visit (only meaningful if we have prior counts)
  const sinceCounts = previousVisit?.counts
    ? {
        memory: Math.max(0, (brain?.memory.length ?? 0) - previousVisit.counts.memory),
        progress: Math.max(0, (brain?.progress.length ?? 0) - previousVisit.counts.progress),
        sessions: Math.max(0, (brain?.sessions.length ?? 0) - previousVisit.counts.sessions),
      }
    : null;

  // Live-agent count comes from the new agents layer (heartbeat-based),
  // NOT from `sessions.ended_at IS NULL` which silently dropped CLIs every
  // time they called session_report. activeRoles (orchestrator-task scope)
  // is kept as a supplement for roles that don't surface as their own agent.
  const agentsRunning = liveAgents.length;
  const callsPerMin = activity?.calls ?? 0;

  return (
    <div className="page-shell space-y-10">
      <WelcomePanel />
      <NarrativeHeader
        agentsRunning={agentsRunning}
        callsPerMin={callsPerMin}
        activeTasks={activeTasks.length}
        staleThoughts={staleThoughts.length}
        connected={connected}
      />

      <div className="grid gap-6 lg:grid-cols-5">
        {/* Live agents zone (2/5) */}
        <section className="lg:col-span-2 space-y-3">
          <SectionLabel>Live agents</SectionLabel>
          {agentsRunning === 0 ? (
            <EmptyState
              title="No agents running"
              description="The pulse is idle. Kick off a task or wait for a recurring trigger."
            />
          ) : (
            <div className="space-y-2">
              {liveAgents.map((a) => (
                <LiveAgentRow key={a.id} agent={a} />
              ))}
              {activeRoles
                .filter((r) => !liveAgents.some((a) => a.provider === r))
                .map((role) => (
                  <LiveAgentRow key={`role:${role}`} roleOnly={role} />
                ))}
            </div>
          )}
        </section>

        {/* Needs-you zone (3/5) — top gated inbox items, surfaced inline.
            Replaces the legacy What-needs-you / Signals / Digest zones
            (Phase 4). The standalone /inbox page remains the full view. */}
        <div className="lg:col-span-3">
          <NeedsYouStrip />
        </div>
      </div>

      {/* Resume anchor */}
      <ResumeAnchor
        previousVisit={previousVisit}
        sinceCounts={sinceCounts}
        recentTasks={recentTasks}
      />

      {/* Create FAB */}
      <Button
        onClick={() => setCreateOpen(true)}
        aria-label="Create new task"
        className="fixed bottom-6 right-6 z-20 h-12 w-12 rounded-full p-0 shadow-lg"
      >
        <Plus className="h-5 w-5" aria-hidden="true" />
      </Button>
      <CreateDialog open={createOpen} onOpenChange={setCreateOpen} />

      {/* System status band — low-ink, for answering "is anything broken?" */}
      {status && (
        <div className="flex items-center gap-4 text-3xs text-tertiary">
          <span>
            {status.tasks_count} tasks
          </span>
          <span className="text-fg-subtle">·</span>
          <span>{status.tools_count} tools</span>
          <span className="text-fg-subtle">·</span>
          <span>up {formatUptime(status.uptime_seconds)}</span>
        </div>
      )}
    </div>
  );
}

function NarrativeHeader({
  agentsRunning,
  callsPerMin,
  activeTasks,
  staleThoughts,
  connected,
}: {
  agentsRunning: number;
  callsPerMin: number;
  activeTasks: number;
  staleThoughts: number;
  connected: boolean;
}) {
  if (!connected) {
    return <PageHeader title="Now" subtitle="Connecting to orchestrator…" />;
  }

  const fragments: string[] = [];
  fragments.push(
    agentsRunning === 0
      ? "no agents running"
      : agentsRunning === 1
        ? "1 agent running"
        : `${agentsRunning} agents running`,
  );
  if (callsPerMin > 0) fragments.push(`${callsPerMin} tool calls in the last minute`);
  if (activeTasks > 0)
    fragments.push(activeTasks === 1 ? "1 active task" : `${activeTasks} active tasks`);
  if (staleThoughts > 0)
    fragments.push(
      staleThoughts === 1 ? "1 stale thought" : `${staleThoughts} stale thoughts`,
    );

  return <PageHeader title="Now" subtitle={fragments.join(" · ")} />;
}

function LiveAgentRow({
  agent,
  roleOnly,
}: {
  agent?: LiveAgent;
  roleOnly?: string;
}) {
  const label = agent ? displayAgent(agent.provider) : (roleOnly ?? "agent");
  const hint = agent?.current_task_hint;
  const pid = agent?.pid;
  const project = agent?.current_project;

  return (
    <div className="flex items-center gap-3 rounded-md border border-border-subtle bg-surface-elevated px-3 py-2">
      <span
        aria-hidden="true"
        className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent motion-safe:animate-pulse"
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 text-sm font-medium text-fg">
          <span className="truncate">{label}</span>
          {pid ? (
            <span className="shrink-0 text-3xs tabular-nums text-tertiary">
              pid {pid}
            </span>
          ) : null}
        </div>
        {hint ? (
          <span className="truncate text-2xs text-fg-muted" title={hint}>
            {hint}
          </span>
        ) : (
          <span className="text-2xs italic text-tertiary">idle</span>
        )}
        {project ? (
          <span className="truncate text-3xs text-tertiary">{project}</span>
        ) : null}
      </div>
    </div>
  );
}

function ResumeAnchor({
  previousVisit,
  sinceCounts,
  recentTasks,
}: {
  previousVisit: LastVisit | null;
  sinceCounts: { memory: number; progress: number; sessions: number } | null;
  recentTasks: TaskSummary[];
}) {
  const deltaFragments: string[] = [];
  if (sinceCounts) {
    if (sinceCounts.memory > 0)
      deltaFragments.push(
        sinceCounts.memory === 1 ? "1 new memory" : `${sinceCounts.memory} new memories`,
      );
    if (sinceCounts.progress > 0)
      deltaFragments.push(
        sinceCounts.progress === 1
          ? "1 new progress entry"
          : `${sinceCounts.progress} new progress entries`,
      );
    if (sinceCounts.sessions > 0)
      deltaFragments.push(
        sinceCounts.sessions === 1
          ? "1 new session"
          : `${sinceCounts.sessions} new sessions`,
      );
  }

  const lastTask = recentTasks[0];

  if (!previousVisit && !lastTask) return null;

  return (
    <section className="rounded-md border border-border-subtle bg-surface-subtle px-4 py-3">
      <SectionLabel size="sm" className="mb-2">
        Resume
      </SectionLabel>
      <div className="space-y-1 text-xs text-fg-muted">
        {lastTask && (
          <div>
            Last task:{" "}
            <Link to={`/work/${lastTask.id}`} className="text-accent hover:underline">
              {shortId(lastTask.id)}
            </Link>{" "}
            — {lastTask.description.slice(0, 80)}{" "}
            <span className="text-tertiary">
              ({formatAge(lastTask.created_at)})
            </span>
          </div>
        )}
        {deltaFragments.length > 0 && (
          <div className="text-tertiary">
            Since last visit: {deltaFragments.join(" · ")}
          </div>
        )}
      </div>
    </section>
  );
}

function formatUptime(seconds: number): string {
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}
