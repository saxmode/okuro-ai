import { Link } from "react-router";
import { useEffect, useState } from "react";
import { useTasks, useSystemStatus } from "@/hooks/use-tasks";
import { useDashboardBrain } from "@/hooks/use-dashboard";
import { usePulseData, type LiveAgent } from "@/hooks/use-activity-stream";
import { formatAge, shortId, displayAgent } from "@/lib/format";
import type { TaskSummary } from "@/types/api";
import { SectionLabel } from "@/components/ui/section-label";
import { EmptyState } from "@/components/ui/empty-state";
import { WelcomePanel } from "@/components/dashboard/welcome-panel";
import { NeedsYouStrip } from "@/components/inbox/NeedsYouStrip";
import { parseApiDate } from "@/lib/format";

const LAST_VISIT_KEY = "okuro-last-visit";

/**
 * `path` IS GONE (S8). It was stamped as `"/"`, which stopped being this
 * leaf's address when it became `/start/now`, and nothing has ever read the
 * field — the deltas below are computed from `counts` alone. A key that
 * outlives its last reader is the shape three post-p10 defects had, and a key
 * that outlives its last reader while ALSO holding a stale address is worse:
 * the first thing to read it would read a wrong answer.
 */
interface LastVisit {
  at: string;
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
  const [previousVisit] = useState(() => loadLastVisit());

  // Persist this visit after first paint so `previousVisit` reflects the
  // *previous* session's counts, not the one we just rendered.
  useEffect(() => {
    if (!brain) return;
    saveLastVisit({
      at: new Date().toISOString(),
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

      {/* R3 / S3 — THE TWO ZONES ARE PANE-AWARE, and they used to be neither.
          `lg:grid-cols-5` is a WINDOW query, and under the engine's 8px root
          `lg:` is 512px, so it was true in every state the shell can produce —
          the grid was always five columns whether the pane was 728px or
          1514px. Measured consequence at 1366 with the panel open: the strip
          got `col-span-3` of a 728px pane = 426px, its compact row's title
          cell 125.5px, and the first title rendered as one letter.

          `@4xl:` is the PANE query the shell provides (`.pane` is a query
          container named `pane`), and `--container-4xl` is 112rem = 896px under
          the 8px root — the same rung the inbox row uses for its salience bar,
          deliberately, so the two leaves flip together. Below it the zones
          stack and each gets the full pane; above it they sit side by side as
          before. This does NOT close the open Tailwind-breakpoint todo: that
          one re-anchors `--breakpoint-*` for the WINDOW and stays open. */}
      <div className="grid gap-6 @4xl:grid-cols-5">
        {/* Live agents zone (2/5 once the pane is wide enough) */}
        {/* `min-w-0` ON BOTH GRID ITEMS, and it is load-bearing rather than
            defensive. A grid track is `minmax(auto, 1fr)`, so its MINIMUM is
            the item's min-content width — and the live-agent row carries an
            agent's `current_task_hint`, which is an arbitrary sentence. With
            the zones stacked at 1366 that pushed the track to 1072px inside a
            728px pane: `.pane` scrollWidth 1072 vs clientWidth 728, a
            horizontal overflow, which the container contract forbids outright.
            Measured, then fixed, then re-measured at 728 == 728. The `truncate`
            already on the hint cannot help — a truncating span is still
            `min-width:auto` to its own parent, so the clamp has to be on the
            item the TRACK sizes. */}
        <section className="min-w-0 @4xl:col-span-2 space-y-3">
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
        <div className="min-w-0 @4xl:col-span-3">
          <NeedsYouStrip />
        </div>
      </div>

      {/* Resume anchor */}
      <ResumeAnchor
        previousVisit={previousVisit}
        sinceCounts={sinceCounts}
        recentTasks={recentTasks}
      />

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

/**
 * R1 (86b8f1f0) — WHAT IS LEFT OF THIS AFTER THE TITLE WENT.
 *
 * It used to be a `PageHeader`: `<h1>Now</h1>` plus a live sentence. The shell
 * renders `<h1 class="c-title">Now</h1>` above this pane, and on NOW the
 * duplication was literal — START's topic sentence is "Start with okuro now"
 * and the leaf is "NOW". So the h1 is gone and only the sentence remains.
 *
 * The SENTENCE IS NOT A SUBTITLE and that is why it survived the cut: it is
 * assembled from four live counts and degrades to "Connecting to orchestrator…"
 * while the socket is down, so it is the page's only statement of whether
 * anything is running. A title removal that took it with it would have deleted
 * content, not chrome.
 */
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
    return <p className="type-small text-fg-muted">Connecting to orchestrator…</p>;
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

  return <p className="type-small text-fg-muted">{fragments.join(" · ")}</p>;
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
            {/* `/work/tasks/{id}`, not `/work/{id}` (S8). The short form
                resolves only through the two-segment WORK special case in
                `routes.ts:462` — the one rule that file's own comment calls a
                judgement call, and the most fragile of the seven legacy links
                this leaf carried. A template literal is invisible to a source
                scan, so the gate for THIS one is a rendered-href assertion in
                home.test.tsx. */}
            <Link to={`/work/tasks/${lastTask.id}`} className="text-accent hover:underline">
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
