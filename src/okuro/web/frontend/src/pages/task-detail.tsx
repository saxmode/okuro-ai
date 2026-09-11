import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
  useCallback,
  useRef,
} from "react";
import { useParams, Link } from "react-router";
import { ArrowLeft, ChevronDown, ChevronUp, Square, Trash2 } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { useTaskState, useTaskSnapshot, useActivity } from "@/hooks/use-tasks";
import { useTaskReview } from "@/hooks/use-review";
import { useWebSocket } from "@/hooks/use-websocket";
import { taskApi } from "@/lib/api";
import { registerSnapshotContext } from "@/lib/handover-context";
import { formatDuration } from "@/lib/format";
import { TONE_TEXT } from "@/lib/color-class";
import { deriveTaskView } from "@/lib/task-view";
import { mergeActivity } from "@/lib/activity-merge";
import type {
  LogEntry,
  ActivityEvent,
  TaskSnapshot,
} from "@/types/api";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { PipelineView } from "@/components/task/pipeline-view";
import { MobileProcessView } from "@/components/task/mobile-process-view";
import { useIsMobile } from "@/hooks/use-is-mobile";
import { WaterfallTrace } from "@/components/task/waterfall-trace";
import { LogStream } from "@/components/task/log-stream";
import { ActivityFeed } from "@/components/task/activity-feed";
import { AgentPanel } from "@/components/task/agent-panel";
import { ApprovalBanner } from "@/components/task/approval-banner";
import { DeliberationPanel } from "@/components/task/deliberation-panel";
import { useCapabilityGap, useAllRoles } from "@/hooks/use-deliberation";
import { BlockerCard } from "@/components/task/BlockerCard";
import { FailureBanner, type SubtaskFailure } from "@/components/task/failure-banner";
import { EngineStalenessChip } from "@/components/task/engine-staleness-chip";
import { LivenessBanner } from "@/components/task/liveness-banner";
import { DecomposerActivityPanel } from "@/components/task/decomposer-activity-panel";
import { ContinueBar } from "@/components/task/continue-bar";
import { ArtifactsViewer } from "@/components/task/artifacts-viewer";
import { InlineThinker } from "@/components/task/inline-thinker";
import { PreviewButton } from "@/components/task/preview-button";
import { ProjectPathField } from "@/components/task/project-path-field";
import { FlowFeedbackCard } from "@/components/task/flow-feedback-card";
import { NeedsYouStrip } from "@/components/inbox/NeedsYouStrip";
import { toast } from "@/components/ui/toast";
import { signalThinking, signalIdle } from "@/hooks/use-orchestrator-state";
import { STEP, stepLabel } from "@/lib/nouns";

// Per-subtask buffer cap. Group events by `subtask_id` (events without one
// land under "_global"), keep at most `perBucket` events per bucket, then
// return the merged list ordered by original arrival (input is already
// chronological). Pre-fix a single `.slice(-50)` over the flat array let
// 4 parallel agents flood the buffer; done agents' history disappeared
// once a sibling emitted 50+ events. Bucketing preserves each agent's
// tail while keeping memory bounded by N_subtasks × perBucket.
export function TaskDetailPage({
  snapshotOverride,
}: {
  /**
   * Test-only injection point. When supplied, the page renders a minimal
   * snapshot-driven surface (header + BlockerCard + PipelineView) without
   * mounting useTaskState / WebSocket / activity. Production callers do
   * not pass this — they go through the real snapshot hook below.
   *
   * Wired into the C10 correctness tests under
   * src/__tests__/correctness/c10-consumer-routing.test.tsx.
   */
  snapshotOverride?: TaskSnapshot;
} = {}) {
  // Test-mode short-circuit. Renders only the snapshot-driven surfaces
  // so the C10 tests can assert against header / blocker / pipeline
  // without standing up an HTTP mock for every secondary hook.
  if (snapshotOverride) {
    return <TaskDetailSnapshotOnly snapshot={snapshotOverride} />;
  }

  const { id } = useParams<{ id: string }>();
  const queryClient = useQueryClient();

  // Real-time logs and activity from WebSocket. Bursty backends would re-render
  // the whole detail tree per event — buffer events between rAF flushes so a
  // burst becomes one state update per frame.
  const [wsLogs, setWsLogs] = useState<LogEntry[]>([]);
  const [wsActivity, setWsActivity] = useState<ActivityEvent[]>([]);

  const wsLogsBufferRef = useRef<LogEntry[]>([]);
  const wsActivityBufferRef = useRef<ActivityEvent[]>([]);
  const flushHandleRef = useRef<number | null>(null);

  const scheduleFlush = useCallback(() => {
    if (flushHandleRef.current !== null) return;
    flushHandleRef.current = requestAnimationFrame(() => {
      flushHandleRef.current = null;
      if (wsLogsBufferRef.current.length > 0) {
        const buf = wsLogsBufferRef.current;
        wsLogsBufferRef.current = [];
        setWsLogs((prev) => [...prev, ...buf].slice(-200));
      }
      if (wsActivityBufferRef.current.length > 0) {
        const buf = wsActivityBufferRef.current;
        wsActivityBufferRef.current = [];
        // Phase 4 — keep the live WS tail as a flat, generously-bounded
        // buffer (memory cap only). The old per-subtask cap(50) silently
        // dropped the MIDDLE of any subtask that exceeded 50 events. Dedupe
        // + ordering + the explicit "N older hidden" bound now happen once,
        // at the mergeActivity funnel, so nothing is hidden without saying so.
        setWsActivity((prev) => [...prev, ...buf].slice(-3000));
      }
    });
  }, []);

  useEffect(() => {
    return () => {
      if (flushHandleRef.current !== null) {
        cancelAnimationFrame(flushHandleRef.current);
        flushHandleRef.current = null;
      }
    };
  }, []);

  const onLog = useCallback(
    (entry: LogEntry) => {
      wsLogsBufferRef.current.push(entry);
      scheduleFlush();
    },
    [scheduleFlush],
  );
  const onActivity = useCallback(
    (event: ActivityEvent) => {
      wsActivityBufferRef.current.push(event);
      scheduleFlush();
    },
    [scheduleFlush],
  );

  const { connected } = useWebSocket({ taskId: id, onLog, onActivity });

  // While WS is delivering state_change + agent_activity events live, the
  // background polling timers are pure churn — every refetch re-allocates
  // phases/artifacts identities and cascades memo invalidation through the
  // detail tree. Pause polling when connected; the WS handler still
  // invalidates these queries on relevant events, which triggers an
  // immediate refetch even while paused.
  const { data: state, isLoading } = useTaskState(id, { paused: connected });
  // C10 — useTaskSnapshot is now the primary surface for lifecycle /
  // blocker / phase color render. useTaskState stays mounted for the
  // legacy concerns (recent_logs, artifacts, mode, intelligence,
  // interventions, awaitingApproval) until those fields land on the
  // snapshot envelope. Snapshot push from the WS handler keeps both
  // caches in lock-step.
  const { data: snapshot } = useTaskSnapshot(id, { paused: connected });
  const { data: activityData } = useActivity(id, { paused: connected });
  // Durable review outcomes (single source of truth) — feeds the activity
  // feed's review rounds so verdict findings survive a lossy activity stream.
  const { data: reviewData } = useTaskReview(id);

  // L2 snapshot context — expose the loaded task state so a captured snapshot of
  // this page hands over the task's DATA (status, phases, artifacts, logs), not
  // just pixels, to whatever tool receives it. Unregisters on unmount / change.
  useEffect(() => {
    if (!state) return;
    return registerSnapshotContext(() => ({
      entity: { type: "task", id },
      data: state,
    }));
  }, [state, id]);

  // Merge polled + WS data — memoized so children with React.memo don't see
  // a new array reference on every parent render.
  const allLogs = useMemo(
    () => [...(state?.recent_logs ?? []), ...wsLogs],
    [state?.recent_logs, wsLogs],
  );
  // Phase 4 — dedupe (reconnect re-fetches the polled list that overlaps the
  // live WS tail), order by ts, and bound with an explicit hidden-older count.
  const mergedActivity = useMemo(
    () => mergeActivity(activityData?.events, wsActivity),
    [activityData?.events, wsActivity],
  );
  const allActivity = mergedActivity.events;
  const activityHiddenOlder = mergedActivity.hiddenOlder;

  // Selected subtask
  const [selectedId, setSelectedId] = useState<string | undefined>();
  const selectedSubtask = useMemo(
    () =>
      state?.phases.flatMap((p) => p.subtasks).find((s) => s.id === selectedId),
    [state?.phases, selectedId],
  );

  // Find subtask waiting approval
  const awaitingApproval = useMemo(
    () =>
      state?.phases
        .flatMap((p) => p.subtasks)
        .find((s) => s.status === "waiting_approval"),
    [state?.phases],
  );

  // Failed subtasks carrying an error — drives the top-of-detail
  // FailureBanner. Deduped by error string so a halt that flips N
  // parallel subtasks to the same "task halted before subtask completed"
  // reason renders one row, not N copies.
  const failures = useMemo<SubtaskFailure[]>(() => {
    const seen = new Set<string>();
    const out: SubtaskFailure[] = [];
    for (const s of state?.phases.flatMap((p) => p.subtasks) ?? []) {
      if (s.status !== "failed") continue;
      const error = (s.error ?? "").trim();
      if (!error) continue;
      if (seen.has(error)) continue;
      seen.add(error);
      out.push({ id: s.id, role: s.role, error });
    }
    return out;
  }, [state?.phases]);

  // Filtered artifacts (drop stdout dumps). Memoized so ArtifactsViewer's
  // useMemo deps don't invalidate just because the parent re-rendered.
  const filteredArtifacts = useMemo(
    () => (state?.artifacts ?? []).filter((a) => !a.name.endsWith(".stdout.md")),
    [state?.artifacts],
  );

  // Stable inline-lambda replacement for ArtifactsViewer prop, declared
  // alongside its state owner.
  const clearSubtaskFilter = useCallback(() => setSelectedId(undefined), []);

  // Resizable panels
  const [leftWidth, setLeftWidth] = useState(55); // percentage (desktop only)
  const isMobile = useIsMobile();

  // Log minimize state — collapsed by default, expand on demand
  const [logCollapsed, setLogCollapsed] = useState(true);
  const toggleLogCollapsed = useCallback(() => setLogCollapsed((v) => !v), []);

  // Scroll the pipeline panel to the bottom on initial load so the user
  // lands on the most recent / current phase rather than the first one.
  // One-shot per task: gated by a ref so subsequent re-renders (live
  // status flips) don't yank the scroll position from under the user.
  const pipelineScrollRef = useRef<HTMLDivElement>(null);
  const didInitialScrollRef = useRef(false);
  useEffect(() => {
    if (didInitialScrollRef.current) return;
    if (!state || !state.phases || state.phases.length === 0) return;
    const el = pipelineScrollRef.current;
    if (!el) return;
    // Defer one frame so the pipeline children have laid out and
    // scrollHeight reflects the real document height.
    const raf = requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight;
      didInitialScrollRef.current = true;
    });
    return () => cancelAnimationFrame(raf);
  }, [state]);

  // Reset the initial-scroll gate when navigating to a different task so
  // the new task also lands at the bottom on its first paint.
  useEffect(() => {
    didInitialScrollRef.current = false;
  }, [id]);

  // Expandable prompt title
  const [promptExpanded, setPromptExpanded] = useState(false);
  const [promptHasOverflow, setPromptHasOverflow] = useState(false);
  const titleRef = useRef<HTMLHeadingElement>(null);

  // Actions
  const [confirming, setConfirming] = useState<"stop" | "delete" | null>(null);
  const [suggesting, setSuggesting] = useState(false);

  // Optimistic acknowledgment. Set synchronously on click, before any await,
  // so the acted surface reacts in the same frame — independent of the socket,
  // the backend, and the 8 s poll. The OkuroThinker widget already lit up
  // globally; what was missing was a reaction where the user is looking.
  const [pendingAction, setPendingAction] = useState<string | null>(null);

  // Every mutation goes through this helper so silent failures are
  // impossible — the user always sees either a success or an error toast.
  // Also lights up the OkuroThinker widget for the duration of the call
  // via the module-level signal, so every button click produces instant
  // UI feedback even before the backend responds.
  // Wrapped in useCallback so downstream handlers stay stable across
  // renders, which lets React.memo'd children skip reconciliation.
  const runMutation = useCallback(
    async <T,>(
      action: () => Promise<T>,
      { onSuccess, onError, invalidate = true, thinkerLabel }: {
        onSuccess?: (result: T) => void;
        onError?: (err: Error) => string | undefined;
        invalidate?: boolean;
        thinkerLabel?: string;
      } = {},
    ): Promise<T | undefined> => {
      if (thinkerLabel) {
        signalThinking(thinkerLabel);
        setPendingAction(thinkerLabel);
      }
      try {
        const result = await action();
        if (invalidate) {
          queryClient.invalidateQueries({ queryKey: ["taskState", id] });
          // taskSnapshot is the PRIMARY surface for lifecycle / blocker /
          // phase colour (see the useSnapshot consumer below), so omitting it
          // meant a mutation whose endpoint emits NO allowlisted event left
          // that surface stale until the 8 s poll. Measured example:
          // DELETE /interventions/{id} — state.delete_intervention (state.py)
          // writes no log line at all, so no state_change frame is ever
          // produced and the WS handler has nothing to react to.
          queryClient.invalidateQueries({ queryKey: ["taskSnapshot", id] });
        }
        onSuccess?.(result);
        return result;
      } catch (err) {
        const e = err instanceof Error ? err : new Error(String(err));
        const msg = onError?.(e) ?? e.message ?? "Action failed";
        toast.error(msg);
        return undefined;
      } finally {
        if (thinkerLabel) {
          signalIdle();
          setPendingAction(null);
        }
      }
    },
    [queryClient, id],
  );

  const handleStop = async () => {
    if (!id) return;
    if (!confirming) {
      setConfirming("stop");
      return;
    }
    await runMutation(() => taskApi.cancel(id), {
      thinkerLabel: "Cancelling task…",
      onSuccess: () => toast.success("Task cancelled"),
      onError: (e) => `Cancel failed: ${e.message}`,
    });
    setConfirming(null);
  };

  const handleDelete = async () => {
    if (!id) return;
    if (!confirming) {
      setConfirming("delete");
      return;
    }
    const ok = await runMutation(
      () => taskApi.delete(id, true),
      {
        thinkerLabel: "Deleting task…",
        invalidate: false,
        onSuccess: () => {
          toast.success("Task deleted");
          queryClient.invalidateQueries({ queryKey: ["tasks"] });
        },
        onError: (e) => `Delete failed: ${e.message}`,
      },
    );
    setConfirming(null);
    if (ok !== undefined) window.history.back();
  };

  const handleIntelligence = async () => {
    if (!id || !state) return;
    const next = state.intelligence === "max" ? "" : "max";
    await runMutation(() => taskApi.setIntelligence(id, next), {
      thinkerLabel: next === "max" ? "Switching to max…" : "Switching to default…",
      onError: (e) => `Intelligence toggle failed: ${e.message}`,
    });
  };

  const handleModelOverride = useCallback(
    async (subtaskId: string, model: string) => {
      if (!id) return;
      await runMutation(
        () => taskApi.setSubtaskModel(id, subtaskId, model),
        {
          thinkerLabel: `Setting ${subtaskId} to ${model}…`,
          onError: (e) => `Model override failed: ${e.message}`,
        },
      );
    },
    [id, runMutation],
  );

  const handleDeleteIntervention = useCallback(
    async (interventionId: string) => {
      if (!id) return;
      await runMutation(
        () => taskApi.deleteIntervention(id, interventionId),
        {
          // This endpoint emits NO event (state.delete_intervention writes no
          // log line), so the optimistic ack and the taskSnapshot
          // invalidation in runMutation are the ONLY feedback the user gets
          // before the 8 s poll.
          thinkerLabel: "Deleting queued prompt…",
          onSuccess: () => toast.success("Queued prompt deleted"),
          onError: (e) => `Delete failed: ${e.message}`,
        },
      );
    },
    [id, runMutation],
  );

  const handleApprove = useCallback(
    async (subtaskId: string) => {
      if (!id) return;
      await runMutation(
        () => taskApi.approve(id, subtaskId, "approve"),
        {
          thinkerLabel: `Approving ${subtaskId}…`,
          onError: (e) => `Approve failed: ${e.message}`,
        },
      );
    },
    [id, runMutation],
  );

  const handleSkip = useCallback(
    async (subtaskId: string) => {
      if (!id) return;
      await runMutation(
        () => taskApi.approve(id, subtaskId, "skip"),
        {
          thinkerLabel: `Skipping ${subtaskId}…`,
          onError: (e) => `Skip failed: ${e.message}`,
        },
      );
    },
    [id, runMutation],
  );

  const handleReject = useCallback(
    async (subtaskId: string) => {
      if (!id) return;
      await runMutation(
        () => taskApi.approve(id, subtaskId, "reject"),
        {
          thinkerLabel: `Rejecting ${subtaskId}…`,
          onError: (e) => `Reject failed: ${e.message}`,
        },
      );
    },
    [id, runMutation],
  );

  const handleContinue = useCallback(
    async (description: string, files?: File[]) => {
      if (!id) return;
      await runMutation(
        () =>
          taskApi.continue(
            id,
            { description, auto_approve: true },
            files?.length ? { files } : undefined,
          ),
        {
          thinkerLabel: "Continuing task…",
          onSuccess: (res) =>
            toast.success(
              res?.status === "queued"
                ? "Queued — runs after the current step"
                : "Continuing task",
            ),
          onError: (e) => `Continue failed: ${e.message}`,
        },
      );
    },
    [id, runMutation],
  );

  const handleResume = useCallback(async () => {
    if (!id) return;
    await runMutation(() => taskApi.resume(id), {
      thinkerLabel: "Resuming task…",
      onSuccess: () => toast.success("Resuming"),
      onError: (e) => `Resume failed: ${e.message}`,
    });
  }, [id, runMutation]);

  const handleRetry = useCallback(async () => {
    if (!id) return;
    await runMutation(() => taskApi.retry(id), {
      thinkerLabel: "Retrying task…",
      onSuccess: () => toast.success("Retrying"),
      onError: (e) => `Retry failed: ${e.message}`,
    });
  }, [id, runMutation]);

  // P3.4 — routed through runMutation instead of hand-rolling its shape.
  // The hand-rolled version invalidated ["taskState"] ONLY, never
  // ["taskSnapshot"] — and taskSnapshot is the primary surface (lifecycle,
  // blocker, phase colour; see runMutation's own note). It also duplicated
  // the thinker signal, the toast-on-error and the finally/idle dance, which
  // is three chances to drift from every other mutation on this page.
  //
  // `suggesting` stays: it drives the button's own pending state, which
  // runMutation's shared pendingAction does not express per-button.
  const handleSuggest = useCallback(async () => {
    if (!id || suggesting) return;
    setSuggesting(true);
    try {
      await runMutation(() => taskApi.suggest(id), {
        thinkerLabel: "Generating 3 next steps…",
        onSuccess: (res) => {
          const count = res?.suggestions?.length ?? 0;
          if (count === 0) {
            toast.error("No suggestions generated — check orchestrator logs");
          } else {
            toast.success(`${count} next step${count === 1 ? "" : "s"} ready`);
          }
        },
        onError: (e) => `Suggestion failed: ${e.message}`,
      });
    } finally {
      setSuggesting(false);
    }
  }, [id, suggesting, runMutation]);

  // Detect whether the prompt title overflows its 2-line clamp — only then
  // show the expand toggle. Re-measure on description change + window resize.
  useLayoutEffect(() => {
    const el = titleRef.current;
    if (!el || promptExpanded) return;
    const measure = () =>
      setPromptHasOverflow(el.scrollHeight > el.clientHeight + 1);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [state?.description, promptExpanded]);

  if (isLoading || !state) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-tertiary">
        Loading task...
      </div>
    );
  }

  // C10 — header reads snapshot.lifecycle.{label,color_class} only.
  // snapshot may not have arrived yet on first paint; fall back to a
  // neutral neutral-tone placeholder so the header never blanks. No
  // re-derivation from state.status — that was the D8/D9 bug.
  const cfg = snapshot
    ? {
        label: snapshot.lifecycle.label,
        color: TONE_TEXT[snapshot.lifecycle.color_class],
      }
    : {
        label: state.status.toUpperCase(),
        color: "text-tertiary",
      };

  // Phase 2 — single source of truth. deriveTaskView reads every duplicated
  // fact off the snapshot (canonical surface), falling back to useTaskState
  // only for the pre-snapshot first paint, so the header / progress /
  // continue-bar / footer / pipeline can never disagree and a terminal state
  // flips atomically everywhere.
  const {
    lifecycleState,
    intelligence,
    currentPhase,
    progressPercent,
    description,
    mode,
    totalPhases,
    totalDuration,
    isTerminal,
    isWorking,
    isFailed,
  } = deriveTaskView(snapshot, state);
  // The follow-up / retry / resume bar is reachable in every continuable
  // terminal state (done/failed/blocked/halted/waiting_user).
  const isDone = isTerminal;

  // Once the user resolves the discussion gate the orchestrator switches
  // from "panel + positions" to "decompose -> execute". The deliberation
  // panel should collapse the moment that happens, even though phases is
  // still 0 while decompose_task is running. recent_logs is the signal —
  // a `discussion_resolved` or `decompose_started` event means the
  // post-decompose layout (Pipeline + CollapsibleDeliberation + the
  // InlineThinker) should take over.
  const hasResolvedDiscussion =
    mode === "deliberate" &&
    !!state.recent_logs?.some(
      (e) =>
        e.type === "discussion_resolved" ||
        e.type === "decompose_started" ||
        e.type === "decompose_failed",
    );

  // Pre-decompose, the inline DeliberationPanel renders its own
  // DiscussionResolver (with a Proceed button). When it's on-screen, the
  // floating blocker toast for a `discussion_proceed` kind pins to the bottom
  // and overlaps that Proceed button — users reported the toast hiding the
  // real action so the panel "doesn't work". The toast adds nothing the
  // resolver doesn't already offer, so suppress it for that one kind here —
  // the same duplication the panel_confirmation→suppressProposal path avoids.
  const deliberationResolverVisible =
    mode === "deliberate" && totalPhases === 0 && !hasResolvedDiscussion;

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="shrink-0 px-10 py-3">
        <div className="flex items-center gap-3">
          <Link
            to="/work"
            className="text-2xs uppercase tracking-wider text-tertiary hover:text-fg-muted"
          >
            <ArrowLeft className="inline h-3 w-3 mr-1" aria-hidden="true" />
            Work
          </Link>
          <span className="text-2xs text-tertiary">{id}</span>
          <span className={`text-2xs font-bold uppercase tracking-wider ${cfg.color}`}>
            {cfg.label}
          </span>

          {/* Intelligence toggle */}
          <button
            onClick={handleIntelligence}
            className={`rounded px-2 py-0.5 text-3xs font-bold uppercase tracking-wider ${
              intelligence === "max"
                ? "bg-accent text-inverse"
                : "text-disabled hover:text-fg-muted"
            }`}
          >
            {intelligence === "max" ? "MAX" : "STD"}
          </button>

          <span className="text-2xs text-tertiary">
            {STEP} {currentPhase}/{totalPhases}
          </span>
          <span className="text-2xs text-fg-muted">
            {progressPercent}%
          </span>
          {totalDuration > 0 && (
            <span className="text-2xs text-tertiary">
              {formatDuration(totalDuration)}
            </span>
          )}

          <div className="ml-auto flex items-start gap-1">
            {/* Optimistic ack — rendered from local state set on click, so it
                appears in the same frame whether the socket is up or down. */}
            {pendingAction && (
              <span className="animate-pulse self-center pr-1 text-2xs text-accent">
                {pendingAction}
              </span>
            )}
            {isWorking && (
              <Button
                variant="outline"
                size="sm"
                onClick={handleStop}
                disabled={!!pendingAction}
                className={`text-2xs ${confirming === "stop" ? "border-error text-error" : ""}`}
              >
                <Square className="mr-1 h-3 w-3" />
                {confirming === "stop" ? "Confirm" : "Stop"}
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={handleDelete}
              disabled={!!pendingAction}
              className={`text-2xs ${confirming === "delete" ? "border-error text-error" : ""}`}
            >
              <Trash2 className="mr-1 h-3 w-3" />
              {confirming === "delete" ? "Confirm" : "Delete"}
            </Button>
          </div>
        </div>

        {/* Progress bar */}
        <div className="mt-2 h-1 rounded-full bg-border">
          <div
            className={`h-full rounded-full transition-[width] duration-500 ${
              isFailed ? "bg-error" : "bg-accent"
            }`}
            style={{ width: `${progressPercent}%` }}
          />
        </div>

        {/* Title (primary page anchor) — expandable when it exceeds 2 lines */}
        <div className="mt-3 flex items-start gap-2">
          <h1
            ref={titleRef}
            className={`prose-width flex-1 text-xl font-semibold tracking-tight text-fg ${
              promptExpanded ? "whitespace-pre-wrap" : "line-clamp-2"
            }`}
            title={promptExpanded ? undefined : description}
          >
            {description}
          </h1>
          {(promptHasOverflow || promptExpanded) && (
            <button
              type="button"
              onClick={() => setPromptExpanded((v) => !v)}
              className="mt-1 shrink-0 rounded p-1 text-tertiary hover:text-fg-muted"
              aria-expanded={promptExpanded}
              aria-label={promptExpanded ? "Collapse prompt" : "Expand prompt"}
            >
              {promptExpanded ? (
                <ChevronUp className="h-4 w-4" />
              ) : (
                <ChevronDown className="h-4 w-4" />
              )}
            </button>
          )}
        </div>
      </div>
      {/* Separator: inset to match the page gutter so the hairline starts at
          the same x as the title, not at the raw container edge. */}
      <div className="mx-10 h-px shrink-0 bg-border" aria-hidden="true" />

      {/* Failure banner — pinned to the top for terminal failed/halted
          tasks. Surfaces the reason (per-subtask error or the
          interrupted-engine explanation) where the user looks first;
          recovery actions stay in the ContinueBar below. */}
      <FailureBanner
        lifecycleState={snapshot?.lifecycle.state ?? state.status}
        label={cfg.label}
        failures={failures}
        haltReason={snapshot?.lifecycle.halt_reason}
        onRetry={handleRetry}
      />

      {/* Liveness banner — engine wedged/recovering while the task still
          reads "running". Pinned above the work surface so a stalled or
          respawning engine never looks like a working one. */}
      <LivenessBanner engineState={snapshot?.engine_state} />
            {/* P4.6 — sits beside liveness because both answer "is the thing
                running the thing I think it is?". Self-hides when the shas agree. */}
            <EngineStalenessChip staleness={snapshot?.engine_staleness} />

      {/* Approval banner */}
      {awaitingApproval && (
        <ApprovalBanner
          subtask={awaitingApproval}
          onApprove={handleApprove}
          onSkip={handleSkip}
          onReject={handleReject}
        />
      )}

      {/* Role-creation banner — always-on global signal that Phase 0
          (role-researcher → role-designer) is running. Stays visible on
          every tab so the user can't mistake role-research activity for
          task work. Self-hides outside Phase 0. */}
      {id && <RoleCreationBanner taskId={id} />}

      {/* Two-column layout */}
      <div className="flex flex-1 min-h-0">
        {/* Left column (full-width single column on mobile) */}
        <div
          className="flex w-full flex-col border-border md:border-r"
          style={isMobile ? undefined : { width: `${leftWidth}%` }}
        >
          {/* Pipeline / Trace / Deliberation — wrapped in a relative box so
              the BlockerCard can float as a bottom-pinned toast (below)
              that overlays the pipeline instead of scrolling with it. */}
          <div className="relative flex flex-1 min-h-0 flex-col">
          <div
            ref={pipelineScrollRef}
            className="flex-1 overflow-y-auto [scrollbar-gutter:stable]"
          >
            {/* Decomposer activity surface — replaces the silent
                "Decomposing council into execution phases…" period
                with a live stream of decomposer events. Self-hides
                outside of generating/planning state. */}
            {id && <DecomposerActivityPanel taskId={id} />}
            {deliberationResolverVisible ? (
              // Pre-decompose: only deliberation panel exists. Pipeline has nothing to render.
              // Suppress the in-flow proposal when the floating panel_confirmation
              // BlockerCard already owns the proposed-role picker — otherwise both
              // render the same panel and overlap (visible bleed-through on mobile).
              <DeliberationPanel
                taskId={id!}
                suppressProposal={
                  snapshot?.blocker?.kind === "panel_confirmation" &&
                  snapshot?.lifecycle?.state === "waiting_user"
                }
              />
            ) : isMobile ? (
              // Mobile: single-column hybrid IA — accordion phases + drill-in
              // subtask detail — instead of the desktop pipeline/trace split and
              // side-by-side detail column (which cramped to a %-width sliver).
              <MobileProcessView
                snapshot={snapshot}
                activity={allActivity}
                taskId={id!}
                artifacts={filteredArtifacts}
              />
            ) : mode === "deliberate" &&
              (totalPhases > 0 || hasResolvedDiscussion) ? (
              // Post-decompose (or decompose-in-flight): pipeline is primary.
              // Deliberation history collapses into a header above the pipeline.
              // When phases is still empty because decompose_task is running,
              // InlineThinker fills the slot so the user sees the orchestrator
              // is working instead of a static deliberation panel.
              <Tabs defaultValue="pipeline">
                <div className="relative">
                  <TabsList className="px-10 data-[orientation=horizontal]:border-b-0">
                    <TabsTrigger value="pipeline">Pipeline</TabsTrigger>
                    <TabsTrigger value="trace">Trace</TabsTrigger>
                  </TabsList>
                  <div
                    className="pointer-events-none absolute inset-x-10 bottom-0 h-px bg-border"
                    aria-hidden="true"
                  />
                </div>
                <TabsContent value="pipeline" className="mt-0">
                  <CollapsibleDeliberation taskId={id!} />
                  {/* Always-on activity indicator — surfaces reviewer +
                      retry + decompose work that previously left the
                      pipeline silent for minutes at a time. The thinker
                      self-hides when state=idle so it never adds noise. */}
                  {id && <InlineThinker taskId={id} slot="top" terminal={isTerminal} />}
                  {/* Phase 2 — pipeline roster + parallel marker read the
                      snapshot (PipelineView prefers snapshot.phases /
                      snapshot.parallel_phases). The old state.phases /
                      state.parallel_phases props overrode it and lagged. */}
                  <PipelineView
                    snapshot={snapshot}
                    interventions={state.interventions}
                    selectedSubtask={selectedId}
                    onSelectSubtask={setSelectedId}
                    onModelOverride={handleModelOverride}
                    onDeleteIntervention={handleDeleteIntervention}
                  />
                </TabsContent>
                <TabsContent value="trace" className="mt-0">
                  <WaterfallTrace
                    phases={state.phases}
                    selectedSubtask={selectedId}
                    onSelectSubtask={setSelectedId}
                  />
                </TabsContent>
              </Tabs>
            ) : (
              <Tabs defaultValue="pipeline">
                {/* Shared TabsList has `border-b`; we replace it with an
                    inset separator so the hairline starts at the px-10 gutter
                    instead of the raw column edge. */}
                <div className="relative">
                  <TabsList className="px-10 data-[orientation=horizontal]:border-b-0">
                    <TabsTrigger value="pipeline">Pipeline</TabsTrigger>
                    <TabsTrigger value="trace">Trace</TabsTrigger>
                  </TabsList>
                  <div
                    className="pointer-events-none absolute inset-x-10 bottom-0 h-px bg-border"
                    aria-hidden="true"
                  />
                </div>
                <TabsContent value="pipeline" className="mt-0">
                  {/* Top slot — activity indicator for NEW tasks where the
                      first phase doesn't exist yet (planning/decomposing). */}
                  {(lifecycleState === "planning" || totalPhases === 0) && id && (
                    <InlineThinker taskId={id} slot="top" terminal={isTerminal} />
                  )}
                  <PipelineView
                    snapshot={snapshot}
                    interventions={state.interventions}
                    selectedSubtask={selectedId}
                    onSelectSubtask={setSelectedId}
                    onModelOverride={handleModelOverride}
                    onDeleteIntervention={handleDeleteIntervention}
                  />
                  {/* Bottom slot — activity indicator where the NEXT node
                      would appear (follow-up subtask, continuation suggestions,
                      retry). Hidden when planning because the top slot owns
                      that state. */}
                  {totalPhases > 0 && lifecycleState !== "planning" && id && (
                    <InlineThinker taskId={id} slot="bottom" terminal={isTerminal} />
                  )}
                </TabsContent>
                <TabsContent value="trace" className="mt-0">
                  <WaterfallTrace
                    phases={state.phases}
                    selectedSubtask={selectedId}
                    onSelectSubtask={setSelectedId}
                  />
                </TabsContent>
              </Tabs>
            )}
          </div>
            {/* C10 — single BlockerCard dispatcher (replaces the previous
                AwaitingCard + GatePanel pair (D13) and the deliberation-
                panel-gated CapabilityGapCard (D14)). Floated as a
                non-closable toast pinned to the BOTTOM of the process
                section: absolute over the pipeline so it stays visible
                regardless of scroll position, sits above the pipeline
                (z-40) but below modals (z-50). Only mounted when a blocker
                exists so it reserves no space / blocks no clicks when idle;
                BlockerCard itself also self-hides on null.

                Phase 3 — mount on `snapshot.blocker` PRESENCE, not on
                lifecycle.state === "waiting_user". The BE sets a blocker
                for every awaiting kind AND synthesizes one for a phase
                parked in blocked_review — but that case derives
                lifecycle.state = "blocked_review", NOT waiting_user, so the
                old double-gate hid an actionable card and dead-ended the
                user. Blocker presence is the single authoritative signal
                that the engine needs the human. */}
            {snapshot?.blocker &&
              !(
                deliberationResolverVisible &&
                snapshot.blocker.kind === "discussion_proceed"
              ) &&
              !(
                Boolean(awaitingApproval) &&
                snapshot.blocker.kind === "subtask_approval"
              ) && (
              // Mobile: a viewport-fixed bottom sheet so the input is always
              // reachable regardless of the cramped %-width column. Desktop:
              // the original in-column toast pinned above the pipeline.
              // Also suppressed for subtask_approval while ApprovalBanner is
              // mounted: engine wait_for_approval writes BOTH state fields for
              // ONE gate — plan.yaml subtask.status="waiting_approval" (drives
              // ApprovalBanner) and task.yaml awaiting.kind="subtask_approval"
              // (drives this card). Two readers, one gate → the user saw the
              // same approval twice. Worse, "subtask_approval" is absent from
              // BlockerCard's KNOWN_KINDS, so this card fell to GenericBlocker
              // whose single Continue button POSTs "{}" to /approve — which
              // requires subtask_id AND action, so it 422s every time. The
              // banner owns this gate; it is the surface that can resolve it.
              // Suppressed for discussion_proceed while the inline resolver is
              // visible — its own Proceed button owns the action there.
              <div className="pointer-events-none fixed inset-x-0 bottom-0 z-50 md:absolute md:inset-x-4 md:bottom-4 md:z-40">
                <div className="pointer-events-auto max-h-[85vh] overflow-y-auto rounded-t-xl border border-border bg-surface-elevated p-4 shadow-2xl md:max-h-none md:overflow-visible md:rounded-lg">
                  <BlockerCard blocker={snapshot.blocker} taskId={id!} />
                </div>
              </div>
            )}
          </div>

          {/* Continue bar (when done/failed/blocked) — above the log */}
          {isDone && (
            <>
              <div className="mx-10 h-px shrink-0 bg-border" aria-hidden="true" />
              <ContinueBar
                taskStatus={state.status}
                suggestions={state.continuation_suggestions ?? []}
                onContinue={handleContinue}
                onResume={handleResume}
                onRetry={handleRetry}
                onSuggest={handleSuggest}
                suggesting={suggesting}
              />
              {/* F3 — post-flow rating + forward note (learning loop) */}
              {id && (
                <div className="mx-10">
                  {/* Keyed: this route only swaps its :id param, so React
                      reuses the element instead of remounting. Without the key
                      the card carries one task's draft rating onto the next. */}
                  <FlowFeedbackCard key={id} taskId={id} />
                </div>
              )}
            </>
          )}

          {/* Log stream — minimized by default, below proposed next steps */}
          <div className="mx-10 h-px shrink-0 bg-border" aria-hidden="true" />
          <div className={`${logCollapsed ? "" : "h-[200px]"} shrink-0`}>
            <LogStream
              logs={allLogs}
              wsConnected={connected}
              collapsed={logCollapsed}
              onToggleCollapsed={toggleLogCollapsed}
            />
          </div>
        </div>

        {/* Resize handle + right column — desktop only. On mobile the subtask
            detail opens full-screen via drill-in, so there's no side pane. */}
        {!isMobile && (
          <>
        <ResizeHandle onResize={setLeftWidth} />

        {/* Right column — resizable vertical stack */}
        <RightColumn
          hasArtifacts={state.artifacts.length > 0}
          agent={<AgentPanel subtask={selectedSubtask} taskId={id} />}
          activity={
            <ActivityFeed
              events={allActivity}
              selectedSubtaskId={selectedId}
              hiddenOlder={activityHiddenOlder}
              reviewVerdicts={reviewData?.verdicts}
            />
          }
          artifacts={
            <ArtifactsViewer
              taskId={id!}
              artifacts={filteredArtifacts}
              selectedSubtaskId={selectedId}
              onClearSubtaskFilter={clearSubtaskFilter}
              project={state?.project}
            />
          }
          footer={
            isDone && id ? (
              <div className="flex w-full flex-col gap-3">
                <div className="flex w-full flex-col gap-1">
                  <ProjectPathField taskId={id} value={state.project_path ?? ""} />
                  <PreviewButton taskId={id} onFixWithAgent={handleContinue} />
                </div>
                {state.project ? (
                  <NeedsYouStrip
                    project={state.project}
                    title="Related to this work"
                    viewAllTo={`/inbox?project=${state.project}`}
                  />
                ) : null}
              </div>
            ) : null
          }
        />
          </>
        )}
      </div>
    </div>
  );
}

function CollapsibleDeliberation({ taskId }: { taskId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-2 mx-10 mt-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between rounded border border-border bg-surface-elevated px-3 py-2 text-left transition-colors hover:bg-surface"
      >
        <span className="text-2xs font-medium uppercase tracking-wider text-tertiary">
          Deliberation
        </span>
        <span className="flex items-center gap-2 text-3xs text-tertiary">
          <span>{open ? "hide" : "show"} council</span>
          {open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
        </span>
      </button>
      {open && (
        <div className="mt-2 rounded border border-border p-2">
          <DeliberationPanel taskId={taskId} settled />
        </div>
      )}
    </div>
  );
}

function ResizeHandle({
  onResize,
}: {
  onResize: (pct: number) => void;
}) {
  const dragging = useRef(false);

  const handlePointerDown = (e: React.PointerEvent) => {
    e.preventDefault();
    dragging.current = true;
    const parent = (e.target as HTMLElement).parentElement;
    if (!parent) return;

    const parentWidth = parent.offsetWidth;

    const onMove = (ev: PointerEvent) => {
      if (!dragging.current) return;
      const rect = parent.getBoundingClientRect();
      const pct = ((ev.clientX - rect.left) / parentWidth) * 100;
      onResize(Math.max(30, Math.min(70, pct)));
    };

    const onUp = () => {
      dragging.current = false;
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  return (
    <div
      onPointerDown={handlePointerDown}
      className="w-1 shrink-0 cursor-col-resize transition-colors hover:bg-border-hover"
    />
  );
}

/**
 * Vertical resize handle. Uses pointer capture + ref-tracked container height
 * so drag deltas stream reliably regardless of which element the pointer hovers
 * during drag.
 */
function VerticalResizeHandle({
  onResize,
}: {
  onResize: (deltaPct: number) => void;
}) {
  const stateRef = useRef({
    dragging: false,
    lastY: 0,
    parentHeight: 0,
    pointerId: -1,
    el: null as HTMLDivElement | null,
  });

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const el = e.currentTarget;
      const parent = el.parentElement;
      if (!parent) return;
      stateRef.current = {
        dragging: true,
        lastY: e.clientY,
        parentHeight: parent.offsetHeight,
        pointerId: e.pointerId,
        el,
      };
      el.setPointerCapture(e.pointerId);
      e.preventDefault();
    },
    [],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const s = stateRef.current;
      if (!s.dragging || s.parentHeight <= 0) return;
      const dy = e.clientY - s.lastY;
      s.lastY = e.clientY;
      onResize((dy / s.parentHeight) * 100);
    },
    [onResize],
  );

  const onPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const s = stateRef.current;
    if (!s.dragging) return;
    s.dragging = false;
    if (s.el && s.el.hasPointerCapture(e.pointerId)) {
      s.el.releasePointerCapture(e.pointerId);
    }
  }, []);

  return (
    <div
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      role="separator"
      aria-orientation="horizontal"
      // 8 px hit-zone, 1 px visible hairline default → grows to 3 px +
      // accent on hover. Mirrors the vertical Sidebar ResizeHandle's
      // subtle → accent transition so every resizable separator in the
      // app reads the same way. Layout-stable: the outer h-2 never
      // changes so adjacent panels don't jiggle when the cursor enters.
      className="group relative flex h-2 shrink-0 items-center cursor-row-resize"
    >
      <span
        aria-hidden="true"
        className="pointer-events-none block w-full bg-border transition-all duration-150 h-px group-hover:h-[3px] group-hover:bg-accent/70"
      />
      {/* Extended invisible hit zone so users can grab the handle without
          landing exactly on the 1 px line. */}
      <span aria-hidden="true" className="absolute inset-x-0 -top-1 -bottom-1" />
    </div>
  );
}

const clamp = (v: number, lo: number, hi: number) =>
  Math.max(lo, Math.min(hi, v));

function RightColumn({
  hasArtifacts,
  agent,
  activity,
  artifacts,
  footer,
}: {
  hasArtifacts: boolean;
  agent: React.ReactNode;
  activity: React.ReactNode;
  artifacts: React.ReactNode;
  footer?: React.ReactNode;
}) {
  const [agentPct, setAgentPct] = useState(28);
  const [artifactsPct, setArtifactsPct] = useState(22);

  // Refs mirror state so drag handlers see live values across cross-constraints
  // without recreating listeners.
  const agentPctRef = useRef(agentPct);
  const artifactsPctRef = useRef(artifactsPct);
  const hasArtifactsRef = useRef(hasArtifacts);
  useEffect(() => {
    agentPctRef.current = agentPct;
  }, [agentPct]);
  useEffect(() => {
    artifactsPctRef.current = artifactsPct;
  }, [artifactsPct]);
  useEffect(() => {
    hasArtifactsRef.current = hasArtifacts;
  }, [hasArtifacts]);

  const effArtifacts = hasArtifacts ? artifactsPct : 0;
  const activityPct = Math.max(10, 100 - agentPct - effArtifacts);

  const handleAgentDrag = useCallback((delta: number) => {
    setAgentPct((prev) => {
      const effArt = hasArtifactsRef.current ? artifactsPctRef.current : 0;
      return clamp(prev + delta, 10, 100 - effArt - 15);
    });
  }, []);

  const handleArtifactsDrag = useCallback((delta: number) => {
    setArtifactsPct((prev) =>
      clamp(prev - delta, 10, 100 - agentPctRef.current - 15),
    );
  }, []);

  return (
    <div className="flex flex-1 flex-col min-w-0">
      <div className="flex flex-1 flex-col min-h-0">
        <div
          className="overflow-y-auto [scrollbar-gutter:stable]"
          style={{ height: `${agentPct}%` }}
        >
          {agent}
        </div>
        <VerticalResizeHandle onResize={handleAgentDrag} />
        <div
          className="min-h-0 overflow-hidden"
          style={{ height: `${activityPct}%` }}
        >
          {activity}
        </div>
        {hasArtifacts && (
          <>
            <VerticalResizeHandle onResize={handleArtifactsDrag} />
            <div
              className="overflow-hidden"
              style={{ height: `${artifactsPct}%` }}
            >
              {artifacts}
            </div>
          </>
        )}
      </div>
      {footer && (
        <div className="shrink-0 border-t border-border bg-surface p-3">
          {footer}
        </div>
      )}
    </div>
  );
}

/**
 * Minimal test-only render of the snapshot-driven surfaces. Drives the
 * C10 correctness suite without standing up the full activity / log /
 * deliberation tree. Real callers go through TaskDetailPage's normal
 * code path; this branch never mounts in production.
 */
function TaskDetailSnapshotOnly({ snapshot }: { snapshot: TaskSnapshot }) {
  const cfg = {
    label: snapshot.lifecycle.label,
    color: TONE_TEXT[snapshot.lifecycle.color_class],
  };
  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 px-10 py-3">
        <div className="flex items-center gap-3">
          <span
            className={`text-2xs font-bold uppercase tracking-wider ${cfg.color}`}
          >
            {cfg.label}
          </span>
        </div>
      </div>
      <div className="px-10 pt-3">
        <BlockerCard
          blocker={snapshot.blocker}
          taskId={snapshot.task_id}
        />
      </div>
      <div className="px-10 pt-3">
        <PipelineView snapshot={snapshot} />
      </div>
    </div>
  );
}

/**
 * RoleCreationBanner — always-visible signal that Phase 0 (role-researcher
 * → role-designer) is running. Mounts on the task header strip; self-hides
 * outside Phase 0. Prevents the user from mistaking role-research tool_use
 * rows for task-completion work.
 */
function RoleCreationBanner({ taskId }: { taskId: string }) {
  const { data: gap } = useCapabilityGap(taskId);
  const { data: roles } = useAllRoles();
  if (!gap) return null;
  const unpromotedDrafts = (roles ?? []).filter((r) => r.maturity === "draft").length;
  const active =
    gap.status === "accepted" ||
    (gap.status === "phase0_complete" && unpromotedDrafts > 0);
  if (!active) return null;

  const runningStep = gap.phase0.find((s) => s.id.includes("missing_role"));
  const label =
    gap.status === "accepted"
      ? `Creating new roles · ${runningStep?.role ?? "role-researcher"} running`
      : `New roles drafted (${unpromotedDrafts}) — promote to proceed`;

  return (
    <div
      className="flex items-center gap-3 border-b-2 border-warning/50 bg-warning-subtle/40 px-10 py-2"
      data-testid="role-creation-banner"
    >
      <span
        className="inline-block h-2.5 w-2.5 animate-pulse rounded-full bg-warning"
        aria-hidden="true"
      />
      <span className="text-2xs font-semibold uppercase tracking-wider text-warning">
        {stepLabel(0)} — role creation in progress
      </span>
      <span className="text-xs text-fg">{label}</span>
      <span className="ml-auto text-3xs text-tertiary">
        panel proposer will rerun once {stepLabel(0)} finishes
      </span>
    </div>
  );
}

export default TaskDetailPage;
