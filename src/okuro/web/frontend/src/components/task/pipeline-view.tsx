import { memo, useRef, useMemo, useState, useCallback } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { formatDuration } from "@/lib/format";
import type {
  Intervention,
  PhaseSummary,
  SubtaskSummary,
  TaskSnapshot,
} from "@/types/api";
// renderTone is the FE-side switch on the closed `color_class` enum.
import { renderTone } from "@/lib/color-class";
import { ConnectionSVG, type Connection } from "./connection-svg";
import { InterventionCard } from "./intervention-card";
import { STEP_LOWER, stepLabel } from "@/lib/nouns";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

interface PipelineViewProps {
  /**
   * C10 — when supplied, snapshot.phases is the canonical source for the
   * pipeline. Each phase already carries `color_class` + `label` + `state`
   * resolved by the BE; the FE never re-derives status. The legacy
   * `phases` prop is kept for callers that still pass TaskState.phases —
   * those phases also carry the snapshot-supplied fields after C2/C9.
   */
  snapshot?: TaskSnapshot;
  phases?: PhaseSummary[];
  interventions?: Intervention[];
  selectedSubtask?: string;
  onSelectSubtask?: (id: string) => void;
  onModelOverride?: (subtaskId: string, model: string) => void;
  /** Delete a queued, not-yet-started continuation prompt. Only wired
   *  through to *trailing* interventions (those whose target phase never
   *  materialized) — the only deletable kind. Omitted ⇒ no delete UI. */
  onDeleteIntervention?: (interventionId: string) => void;
  /** Phase ids currently running in parallel — BE-supplied. PhaseDivider
   *  renders a small "PARALLEL" marker when its id is in this list AND
   *  more than one phase is running, so the user sees "phase 2 is
   *  active while phase 1 is blocked". */
  parallelPhases?: number[];
}

/**
 * Pipeline view — vertical tree.
 * Subtasks are centered per row; connections follow subtask.dependencies.
 */
export const PipelineView = memo(function PipelineView({
  snapshot,
  phases: phasesProp,
  interventions,
  selectedSubtask,
  onSelectSubtask,
  onModelOverride,
  onDeleteIntervention,
  parallelPhases,
}: PipelineViewProps) {
  // C10 — snapshot wins. Snapshot phases already carry color_class +
  // label + state from the BE; no FE-side derivation. Falls back to the
  // legacy `phases` prop while the rest of the app finishes the
  // useTaskState → useTaskSnapshot migration.
  const phases: PhaseSummary[] = useMemo(
    () =>
      (snapshot?.phases as unknown as PhaseSummary[] | undefined) ??
      phasesProp ??
      [],
    [snapshot, phasesProp],
  );
  const effectiveParallel = useMemo(
    () => parallelPhases ?? snapshot?.parallel_phases ?? [],
    [parallelPhases, snapshot],
  );
  const parallelSet = useMemo(
    () => new Set(effectiveParallel.filter((id) => id !== undefined)),
    [effectiveParallel],
  );
  const isMultiPhaseParallel = parallelSet.size > 1;
  const containerRef = useRef<HTMLDivElement>(null);

  // FIX C — a trailing continuation (intervention with no materialized
  // phase yet) is IN-FLIGHT while the engine is still planning/active, and
  // only FAILED once the task has settled into a terminal state. Reading
  // the snapshot lifecycle keeps the FE free of status re-derivation
  // (C10 contract). Defaults to "in flight" when lifecycle is absent so a
  // just-submitted continuation never momentarily reads as failed.
  const continuationInFlight = useMemo(() => {
    const state = snapshot?.lifecycle?.state ?? "";
    if (!state) return true;
    return ["planning", "active", "generating", "deliberating"].includes(state);
  }, [snapshot]);

  // Show only the user's own voice — strip agent-suggested continuations
  // and any `unknown`-source historical entries. Initial description and
  // user-typed continuations are the cards that actually represent the
  // user's intent in the flow chart.
  const visibleInterventions = useMemo(
    () =>
      (interventions ?? []).filter(
        (iv) => iv.source === "initial" || iv.source === "user_typed",
      ),
    [interventions],
  );

  // Group interventions by the phase id they sit ABOVE. Sort by ts so
  // multiple continuations targeting the same phase render in chronological
  // order. before_phase_id can point past the last phase id (e.g. when a
  // continuation crashed before producing any phases) — those render at
  // the very end so they're never lost.
  const interventionsByPhase = useMemo(() => {
    const map = new Map<number, Intervention[]>();
    for (const iv of visibleInterventions) {
      const list = map.get(iv.before_phase_id) ?? [];
      list.push(iv);
      map.set(iv.before_phase_id, list);
    }
    for (const list of map.values()) {
      list.sort((a, b) => (a.ts || "").localeCompare(b.ts || ""));
    }
    return map;
  }, [visibleInterventions]);

  // Trailing interventions = continuation prompts whose target phase
  // never materialized (e.g. decomposer crashed). Rather than render
  // them in a separate block at the bottom of the chart, group them
  // by their before_phase_id and surface each group as a synthetic
  // "phase" so they slot into the chronological flow with the same
  // wrapper treatment as inline interventions.
  const trailingPhaseGroups = useMemo(() => {
    if (visibleInterventions.length === 0) return [];
    const maxPhaseId = phases.reduce((m, p) => Math.max(m, p.id), 0);
    const groups = new Map<number, Intervention[]>();
    for (const iv of visibleInterventions) {
      if (iv.before_phase_id <= maxPhaseId) continue;
      const list = groups.get(iv.before_phase_id) ?? [];
      list.push(iv);
      groups.set(iv.before_phase_id, list);
    }
    for (const list of groups.values()) {
      list.sort((a, b) => (a.ts || "").localeCompare(b.ts || ""));
    }
    return Array.from(groups.entries()).sort(([a], [b]) => a - b);
  }, [visibleInterventions, phases]);

  // Flat list used by the connection graph builder so trailing edges
  // can chain off the last real subtask through the synthetic phase.
  const trailingInterventions = useMemo(
    () => trailingPhaseGroups.flatMap(([, ivs]) => ivs),
    [trailingPhaseGroups],
  );

  // Collapse state per phase. `undefined` = use the default rule (done →
  // collapsed, everything else → open). User toggling stamps an explicit
  // boolean so the phase doesn't snap closed mid-watch when its status
  // happens to flip to done while the user has it expanded.
  const [collapseOverride, setCollapseOverride] = useState<
    Map<number, boolean>
  >(() => new Map());

  const collapsedIds = useMemo(() => {
    const set = new Set<number>();
    // The MOST-RECENTLY-completed phase stays expanded by default — when a
    // phase finishes the user wants to see what it produced, not have it
    // collapse out from under them. Only EARLIER clean-done phases auto-
    // collapse. (Highest id among done phases = the latest one.)
    const doneIds = phases
      .filter((p) => (p.state ?? p.status) === "done")
      .map((p) => p.id);
    const lastDoneId = doneIds.length ? Math.max(...doneIds) : null;
    for (const p of phases) {
      const override = collapseOverride.get(p.id);
      // Default collapse only for clean DONE phases that are NOT the latest
      // completed one. Snapshot's phase.state === "done" is authoritative;
      // falls back to status. blocked_review / failed / running phases stay
      // expanded so the user sees the situation immediately.
      const isCleanDone = (p.state ?? p.status) === "done";
      const autoCollapse = isCleanDone && p.id !== lastDoneId;
      const collapsed = override === undefined ? autoCollapse : override;
      if (collapsed) set.add(p.id);
    }
    return set;
  }, [phases, collapseOverride]);

  const togglePhase = useCallback(
    (phaseId: number, currentlyCollapsed: boolean) => {
      setCollapseOverride((prev) => {
        const next = new Map(prev);
        next.set(phaseId, !currentlyCollapsed);
        return next;
      });
    },
    [],
  );

  const phaseAnchorId = (phaseId: number) => `phase-${phaseId}`;

  const connections = useMemo(() => {
    const conns: Connection[] = [];

    // Helpers: when a phase is open the visual anchor for outgoing edges
    // is its LAST subtask, and incoming edges land on each NO-DEP subtask.
    // When a phase is collapsed both anchors collapse to the synthetic
    // phase-N node, which renders as a single tile.
    const lastSubtaskOf = (p: PhaseSummary) =>
      p.subtasks.length > 0 ? p.subtasks[p.subtasks.length - 1]! : null;
    const noDepSubtasksOf = (p: PhaseSummary) =>
      p.subtasks.filter((s) => (s.dependencies ?? []).length === 0);
    const outgoingAnchor = (p: PhaseSummary): string | null => {
      if (collapsedIds.has(p.id)) return phaseAnchorId(p.id);
      const last = lastSubtaskOf(p);
      return last ? last.id : null;
    };
    const incomingAnchors = (p: PhaseSummary, status: string): {
      id: string;
      status: string;
    }[] => {
      if (collapsedIds.has(p.id)) {
        return [{ id: phaseAnchorId(p.id), status }];
      }
      return noDepSubtasksOf(p).map((s) => ({ id: s.id, status: s.status }));
    };

    // Intra-phase explicit dependencies — only when the phase is open
    // (when collapsed, subtasks aren't rendered so their edges have no
    // DOM anchors and would be drawn against `[data-subtask-id]` lookup
    // misses anyway).
    for (const phase of phases) {
      if (collapsedIds.has(phase.id)) continue;
      for (const st of phase.subtasks) {
        const deps = st.dependencies ?? [];
        for (const depId of deps) {
          conns.push({
            id: `${depId}->${st.id}`,
            fromId: depId,
            toId: st.id,
            status: st.status,
          });
        }
      }
    }

    // Implicit "phase boundary" edges — possibly routed through any
    // interventions that sit between the phases. For phase N with k
    // interventions {i_1, …, i_k}, the chain becomes:
    //   prev_phase_outgoing → i_1 → i_2 → … → i_k → phase_N_incoming
    // When prev or N is collapsed, the corresponding endpoint is the
    // phase-anchor synthetic id instead of a subtask id.
    for (const phase of phases) {
      const ivs = interventionsByPhase.get(phase.id) ?? [];
      const incomings = incomingAnchors(phase, phase.status);
      const prevPhase = phases.find((p) => p.id === phase.id - 1);
      const prevOut = prevPhase ? outgoingAnchor(prevPhase) : null;

      if (ivs.length > 0) {
        if (prevOut) {
          conns.push({
            id: `${prevOut}->${ivs[0]!.id}`,
            fromId: prevOut,
            toId: ivs[0]!.id,
            status: "done",
          });
        }
        for (let i = 0; i < ivs.length - 1; i++) {
          conns.push({
            id: `${ivs[i]!.id}->${ivs[i + 1]!.id}`,
            fromId: ivs[i]!.id,
            toId: ivs[i + 1]!.id,
            status: "done",
          });
        }
        const tail = ivs[ivs.length - 1]!;
        for (const target of incomings) {
          conns.push({
            id: `${tail.id}->${target.id}`,
            fromId: tail.id,
            toId: target.id,
            status: target.status,
          });
        }
      } else if (prevOut) {
        for (const target of incomings) {
          conns.push({
            id: `${prevOut}->${target.id}`,
            fromId: prevOut,
            toId: target.id,
            status: target.status,
          });
        }
      }
    }

    // Trailing interventions — anchored past the last phase. Chain them
    // off the outgoing anchor of the last phase so they read as "happened
    // after the work", not as orphans.
    if (trailingInterventions.length > 0 && phases.length > 0) {
      const lastPhase = phases[phases.length - 1]!;
      const lastOut = outgoingAnchor(lastPhase);
      if (lastOut) {
        conns.push({
          id: `${lastOut}->${trailingInterventions[0]!.id}`,
          fromId: lastOut,
          toId: trailingInterventions[0]!.id,
          status: "done",
        });
      }
      for (let i = 0; i < trailingInterventions.length - 1; i++) {
        conns.push({
          id: `${trailingInterventions[i]!.id}->${trailingInterventions[i + 1]!.id}`,
          fromId: trailingInterventions[i]!.id,
          toId: trailingInterventions[i + 1]!.id,
          status: "done",
        });
      }
    }

    return conns;
  }, [phases, interventionsByPhase, trailingInterventions, collapsedIds]);

  if (phases.length === 0) {
    return (
      <div className="flex h-32 items-center justify-center text-sm text-tertiary">
        Waiting for plan...
      </div>
    );
  }

  return (
    <div ref={containerRef} className="relative space-y-5 px-10 py-4">
      <ConnectionSVG connections={connections} containerRef={containerRef} />

      {phases.map((phase) => {
        const isCollapsed = collapsedIds.has(phase.id);
        return (
          <div key={phase.id} className="flex flex-col items-center gap-3">
            {/* Intervention(s) that produced this phase — render ABOVE the
                phase divider, in chronological order. */}
            {(interventionsByPhase.get(phase.id) ?? []).map((iv) => (
              <div
                key={iv.id}
                className="relative z-10 flex justify-center"
              >
                <InterventionCard intervention={iv} />
              </div>
            ))}

            {/* Phase divider — present in BOTH states. When collapsed, it
                is also the connection-graph anchor (data-subtask-id);
                when open, the divider is purely a section header and
                edges route to/from individual subtasks below. */}
            <PhaseDivider
              phase={phase}
              isCollapsed={isCollapsed}
              onClick={() => togglePhase(phase.id, isCollapsed)}
              parallel={isMultiPhaseParallel && parallelSet.has(phase.id)}
            />

            {/* Subtask row — only when open */}
            {!isCollapsed && (
              <div className="relative z-10 flex flex-wrap justify-center gap-3">
                {phase.subtasks.map((st) => (
                  <SubtaskCard
                    key={st.id}
                    subtask={st}
                    selected={st.id === selectedSubtask}
                    onSelect={() => onSelectSubtask?.(st.id)}
                    onModelOverride={onModelOverride}
                  />
                ))}
              </div>
            )}
          </div>
        );
      })}

      {/* Trailing interventions — continuation prompts whose target
          phase never materialized. Rendered as synthetic phase blocks
          using the same wrapper as real phases so the card sits in
          the chronological flow with a phase header underneath, not
          as a stranded "end of chart" block. */}
      {trailingPhaseGroups.map(([phaseId, ivs]) => (
        <div
          key={`trailing-${phaseId}`}
          className="flex flex-col items-center gap-3"
        >
          {ivs.map((iv) => (
            <div key={iv.id} className="relative z-10 flex justify-center">
              <InterventionCard
                intervention={iv}
                onDelete={onDeleteIntervention}
              />
            </div>
          ))}
          {/* FIX C — distinguish "engine is still generating the plan"
              (in-flight, the normal window right after a continuation POST)
              from "the decomposer crashed and produced nothing" (terminal).
              The old single "pending — no plan generated" label read as
              failure even during the healthy in-flight window, making a
              working continuation look broken. */}
          {continuationInFlight ? (
            <div className="flex items-center gap-2 text-2xs uppercase tracking-wider text-accent">
              <span
                className="inline-block h-1.5 w-1.5 rounded-full bg-accent motion-safe:animate-pulse"
                aria-hidden="true"
              />
              Continuing — generating plan…
            </div>
          ) : (
            <div className="text-2xs uppercase tracking-wider text-tertiary">
              Continuation failed — no plan generated
            </div>
          )}
        </div>
      ))}
    </div>
  );
});

const MODELS = ["haiku", "sonnet", "opus"] as const;
type ModelName = (typeof MODELS)[number];

const MODEL_COLOR: Record<ModelName, string> = {
  haiku: "text-tertiary",
  sonnet: "text-fg-muted",
  opus: "text-fg",
};

function SubtaskCard({
  subtask,
  selected,
  onSelect,
  onModelOverride,
}: {
  subtask: SubtaskSummary;
  selected: boolean;
  onSelect: () => void;
  onModelOverride?: (subtaskId: string, model: string) => void;
}) {
  const isBlockedByGate = !!subtask.blocked_by_gate;
  const blockingGatePhase = subtask.blocking_gate_phase ?? 0;
  // C10 — single source of truth: subtask.color_class (BE-supplied).
  // The closed 5-token enum routes through renderTone for the four
  // Tailwind classes. Pre-snapshot data falls through to the neutral
  // row, which keeps the card visible without the FE re-deriving status.
  const style = renderTone(subtask.color_class);

  const isRunning = subtask.status === "running";
  const model =
    subtask.model_override ||
    subtask.planned_model ||
    subtask.model_used ||
    "";
  const modelKey = model.toLowerCase() as ModelName;
  const modelColor = MODEL_COLOR[modelKey] ?? "text-tertiary";
  const hasOverride = !!subtask.model_override;
  const canOverride = subtask.status === "pending" && !!onModelOverride;

  const artifacts = subtask.artifacts ?? (subtask.artifact ? [subtask.artifact] : []);

  return (
    <div
      data-subtask-id={subtask.id}
      onClick={onSelect}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      className={cn(
        "relative cursor-pointer overflow-hidden rounded border pl-3 pr-3 py-2 text-left transition-all min-w-[180px] max-w-[220px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
        style.border,
        style.bg,
        selected && "ring-2 ring-accent/60",
        isRunning && "shadow-[0_0_12px_var(--color-accent-subtle)]",
      )}
    >
      {/* Color rail (status strip) */}
      <div className={cn("absolute left-0 top-0 h-full w-1", style.rail)} />

      {/* Role + status label */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-2xs font-medium text-fg-muted truncate">
          {subtask.role}
        </span>
        <span className={cn("text-3xs font-bold uppercase tracking-wider", style.text)}>
          {subtask.label
            ? subtask.label
            : isBlockedByGate
              ? `awaiting adr · ph${blockingGatePhase}`
              : subtask.status.replace("_", " ")}
        </span>
      </div>
      {/* Review summary badge — "reviewed Nx · PASS/FAIL". Surfaces the
          M3 reviewer round count + latest verdict on the tile so the user
          sees the review outcome without opening the activity feed. Only
          renders once a verdict has landed (PASS/CONDITIONAL/FAIL) and the
          reviewer isn't mid-judgement — the live state has its own
          "Reviewing" badge below. A single clean pass (1x · PASS) is still
          shown so "was this reviewed at all?" is answerable at a glance. */}
      {!subtask.review_in_progress &&
        subtask.review_verdict &&
        (subtask.review_attempt ?? 0) > 0 && (
          <div className="mt-1">
            <span
              className={cn(
                "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-3xs font-bold uppercase tracking-wider",
                subtask.review_verdict === "PASS"
                  ? "border-success/50 bg-success-subtle/60 text-success"
                  : subtask.review_verdict === "FAIL"
                    ? "border-error/50 bg-error-subtle/60 text-error"
                    : "border-warning/50 bg-warning-subtle/60 text-warning",
              )}
              title={`Review round ${subtask.review_attempt} of ${subtask.review_max_attempts || 5} · ${subtask.review_verdict}`}
            >
              {/* P3.10 — the BUDGET is on the tile, not only in the tooltip.
                  It read "reviewed 2× · FAIL", which makes a FAIL look like a
                  surprise: nothing said whether round 2 was the last one. "2
                  of 2 · FAIL" reads as a budget running out, which is what it
                  is. Both numbers were already on the snapshot — this was a
                  rendering gap, not missing data. */}
              review {subtask.review_attempt} of{" "}
              {subtask.review_max_attempts || 5} · {subtask.review_verdict}
            </span>
          </div>
        )}
      {/* REVIEWING badge — fires while the M3 reviewer is judging this
          subtask's phase. Co-locates reviewer activity with the work so
          the user doesn't have to look at the global thinker pill. */}
      {subtask.review_in_progress && (
        <div className="mt-1 inline-flex items-center gap-1.5 rounded border border-warning/60 bg-warning-subtle/60 px-1.5 py-0.5 text-3xs font-bold uppercase tracking-wider text-warning">
          <span
            className="inline-block h-1.5 w-1.5 rounded-full bg-warning motion-safe:animate-pulse"
            aria-hidden
          />
          Reviewing
        </div>
      )}
      {/* Retry / loop badge — a subtask looped (retries>0). The verdict shown
          must reflect the CURRENT review outcome, not the mere fact a loop
          happened: a subtask that looped then PASSED must read "recovered",
          NOT "reviewer FAIL". Only an actual FAIL/CAP verdict shows "reviewer
          FAIL" + the stale error. (The summary badge above carries the
          "reviewed N× · PASS" detail.) */}
      {(subtask.retries ?? 0) > 0 &&
        (() => {
          const v = (subtask.review_verdict ?? "").toUpperCase();
          const failing = v === "FAIL" || v === "CAP";
          return (
            <div className="mt-1 space-y-0.5">
              <div className="flex items-center gap-1">
                <span
                  className={cn(
                    "rounded border px-1.5 py-0.5 text-3xs font-bold uppercase tracking-wider",
                    failing
                      ? "border-warning/50 bg-warning-subtle text-warning"
                      : "border-border bg-surface text-tertiary",
                  )}
                >
                  loop {subtask.retries}
                </span>
                {subtask.review_in_progress ? (
                  <span className="text-3xs text-tertiary">re-reviewing…</span>
                ) : failing ? (
                  // ROCK-SOLID v5 P1.3 — matches its sibling "loop {n}"
                  // badge's warning tone (evidence inventory mismatch #10:
                  // this was the one place a tile carried two separate red
                  // FAIL indicators). An automatic retry loop mid-flight
                  // isn't an open gate yet.
                  <span className="text-3xs text-warning">reviewer FAIL</span>
                ) : v === "PASS" ? (
                  <span className="text-3xs text-success">recovered</span>
                ) : null}
              </div>
              {/* Show the FAIL summary only while the subtask is actually
                  failing — never on a recovered/passed node. */}
              {failing && subtask.error && (
                <p
                  className="line-clamp-3 text-3xs leading-snug text-warning/80"
                  title={subtask.error}
                >
                  {subtask.error}
                </p>
              )}
            </div>
          );
        })()}

      {/* Failure reason — a plain failed subtask (no reviewer-retry loop)
          previously rendered its error nowhere: the block above is gated
          behind retries > 0, and halt-on-exit sets retries=0. Surface the
          error string in error tone so the red tile carries its own
          "why" without the user opening the activity feed. */}
      {subtask.status === "failed" &&
        (subtask.retries ?? 0) === 0 &&
        subtask.error && (
          <p
            className="mt-1 line-clamp-3 text-3xs leading-snug text-error/90"
            title={subtask.error}
          >
            {subtask.error}
          </p>
        )}

      {/* Meta row: model + duration + risk */}
      <div className="mt-1 flex items-center gap-2">
        {model &&
          (canOverride ? (
            <DropdownMenu>
              <DropdownMenuTrigger
                onClick={(e) => e.stopPropagation()}
                className={cn(
                  "rounded px-1 py-0.5 text-3xs uppercase tracking-wider outline-none transition-colors hover:text-accent",
                  modelColor,
                  hasOverride && "border-b border-dashed border-warning",
                )}
                aria-label={`Model: ${model}. Click to change.`}
              >
                {model}
              </DropdownMenuTrigger>
              <DropdownMenuContent
                align="start"
                className="min-w-[10rem] p-0.5"
                onClick={(e) => e.stopPropagation()}
              >
                {MODELS.map((m) => (
                  <DropdownMenuItem
                    key={m}
                    onSelect={(e) => {
                      e.preventDefault();
                      onModelOverride?.(subtask.id, m);
                    }}
                    className={cn(
                      "px-2 py-1 text-3xs uppercase tracking-wider",
                      m === modelKey ? "text-accent" : MODEL_COLOR[m],
                    )}
                  >
                    {m}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          ) : (
            <span
              className={cn(
                "rounded px-1 py-0.5 text-3xs uppercase tracking-wider",
                modelColor,
                hasOverride && "border-b border-dashed border-warning",
              )}
            >
              {model}
            </span>
          ))}
        {subtask.duration > 0 && (
          <span className="text-3xs text-tertiary">
            {formatDuration(subtask.duration)}
          </span>
        )}
        {subtask.risk === "HIGH" && (
          <span className="text-3xs text-error">HIGH</span>
        )}
      </div>

      {/* Artifacts produced by this role */}
      {artifacts.length > 0 && (
        <ul className="mt-1.5 space-y-0.5 border-t border-border/40 pt-1.5">
          {artifacts.slice(0, 4).map((a) => (
            <li
              key={a}
              className="truncate text-3xs text-fg-muted"
              title={a}
            >
              <span className="text-tertiary">▸</span> {a}
            </li>
          ))}
          {artifacts.length > 4 && (
            <li className="text-3xs text-tertiary">
              +{artifacts.length - 4} more
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

/**
 * PhaseDivider — section opener for every phase, present in both states.
 * Thin full-width line on each side, label + chevron in the middle. When
 * collapsed it ALSO serves as the connection-graph anchor (the bar carries
 * `data-subtask-id="phase-N"` only in that case — when open, edges still
 * route to/from individual subtasks). When collapsed the inline summary
 * grows to "Phase N: name · n/m · STATUS"; when open it stays compact.
 *
 * Visual goal: clearly NOT a process step. No card chrome, no rail —
 * reads as an IDE-style fold / section divider that's consistent across
 * collapsed and open states.
 */
function PhaseDivider({
  phase,
  isCollapsed,
  onClick,
  parallel = false,
}: {
  phase: PhaseSummary;
  isCollapsed: boolean;
  onClick: () => void;
  parallel?: boolean;
}) {
  // C10 — phase tone reads phase.color_class + phase.label only. The BE
  // already resolves the screenshot-bug case where every subtask is done
  // but the phase is blocked_review: snapshot writes color_class=warning,
  // label="Blocked — reviewer FAIL". No FE derivation, no closed-switch
  // fallback that could silently regress when the BE adds a new state.
  const tone = renderTone(phase.color_class);
  const total = phase.subtasks.length;
  const done = phase.subtasks.filter((s) => s.status === "done").length;
  const statusLabel = phase.label ?? (phase.state ?? phase.status ?? "")
    .replace(/_/g, " ")
    .toUpperCase();

  return (
    <button
      type="button"
      data-testid={`phase-divider-${phase.id}`}
      data-subtask-id={isCollapsed ? `phase-${phase.id}` : undefined}
      onClick={onClick}
      aria-expanded={!isCollapsed}
      aria-label={`${isCollapsed ? "Expand" : "Collapse"} ${STEP_LOWER} ${phase.id}: ${phase.name}`}
      className={cn(
        "group relative z-10 flex w-full items-center gap-3 self-stretch py-1 text-left text-2xs uppercase tracking-wider transition-colors hover:text-fg focus-visible:outline-none focus-visible:rounded focus-visible:ring-2 focus-visible:ring-accent/40",
        tone.text,
      )}
    >
      <span className="h-px flex-1 bg-border" aria-hidden="true" />
      <span className="flex items-center gap-2 whitespace-nowrap">
        {isCollapsed ? (
          <ChevronRight
            className="h-3 w-3 shrink-0 transition-transform group-hover:translate-x-0.5"
            aria-hidden="true"
          />
        ) : (
          <ChevronDown className="h-3 w-3 shrink-0" aria-hidden="true" />
        )}
        <span className="text-fg-muted normal-case tracking-normal">
          {stepLabel(phase.id)}: {phase.name}
        </span>
        {parallel && (
          <span
            className="rounded border border-info/50 bg-info-subtle px-1.5 py-0.5 text-3xs font-bold uppercase tracking-wider text-info"
            title="This step is running concurrently with another step"
          >
            parallel
          </span>
        )}
        <span className="text-tertiary/60">·</span>
        <span>
          {done}/{total}
        </span>
        <span className="text-tertiary/60">·</span>
        <span className={cn("font-bold", tone.text)}>{statusLabel}</span>
      </span>
      <span className="h-px flex-1 bg-border" aria-hidden="true" />
    </button>
  );
}
