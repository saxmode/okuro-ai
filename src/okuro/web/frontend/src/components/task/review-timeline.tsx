/**
 * ReviewTimeline — what the reviewer did to one subtask, round by round
 * (ROCK-SOLID v5 P3.9).
 *
 * Before this, a review was two bits of information: a "Reviewing" badge while
 * it ran, and "review N of M · VERDICT" once it stopped. Everything between —
 * how many rounds, what each one decided, whether findings were being resolved
 * or re-raised, where the time went — was in the log for an engineer and
 * nowhere for a person.
 *
 * A 30 ms deterministic round leaves the SAME shaped row as a 4-minute LLM
 * round, which is the plan's explicit requirement: a fast round that renders
 * as nothing teaches the reader that fast rounds do not happen.
 *
 * Covers both drivers. The session loop publishes a row per (subtask,
 * artifact, attempt); the phase gate publishes one per subtask in the phase
 * (P3.9 backend). They render identically here because from the reader's side
 * they ARE the same event: the work was judged, and this is what came back.
 */
import { useState } from "react";

import { cn } from "@/lib/utils";
import { useReviewRounds, type ReviewRound } from "@/hooks/use-review-rounds";

const VERDICT_TONE: Record<string, string> = {
  PASS: "border-success/50 bg-success-subtle/60 text-success",
  FAIL: "border-error/50 bg-error-subtle/60 text-error",
  CAP: "border-warning/50 bg-warning-subtle/60 text-warning",
  NEEDS_USER: "border-info/50 bg-info-subtle/60 text-info",
};

/** Stage keys in the order the reviewer runs them, with readable names. */
const STAGES: [string, string][] = [
  ["queue_wait_s", "queued"],
  ["deterministic_s", "checks"],
  ["critic_s", "critic"],
  ["scorer_s", "scorer"],
];

function secs(v: number): string {
  if (v < 1) return `${Math.round(v * 1000)}ms`;
  if (v < 60) return `${v.toFixed(1)}s`;
  const m = Math.floor(v / 60);
  return `${m}m ${Math.round(v % 60)}s`;
}

function RoundRow({ round, budget }: { round: ReviewRound; budget: number }) {
  const [open, setOpen] = useState(false);
  const timings = STAGES.filter(([k]) => round.stage_timings?.[k] != null);

  return (
    <li className="rounded border border-border bg-surface/60">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-2xs"
        aria-expanded={open}
      >
        <span className="font-mono text-tertiary">
          round {round.attempt} of {budget}
        </span>
        <span
          className={cn(
            "rounded border px-1 py-0.5 text-3xs font-bold uppercase tracking-wider",
            VERDICT_TONE[round.verdict] ??
              "border-border bg-surface text-fg-muted",
          )}
        >
          {round.verdict || "—"}
        </span>
        {round.findings_count > 0 && (
          <span className="text-tertiary">
            {round.findings_count} finding{round.findings_count === 1 ? "" : "s"}
          </span>
        )}
        {/* The delta is the convergence signal: resolving without re-raising
            is a loop that is going somewhere. */}
        {round.resolved_findings > 0 && (
          <span className="text-success">−{round.resolved_findings} resolved</span>
        )}
        {round.reason && (
          <span className="truncate text-tertiary">· {round.reason}</span>
        )}
        <span className="ml-auto shrink-0 text-tertiary/60">
          {open ? "−" : "+"}
        </span>
      </button>

      {open && (
        <div className="border-t border-border px-2 py-1.5 text-3xs text-tertiary">
          {timings.length > 0 ? (
            <div className="flex flex-wrap gap-x-3 gap-y-1">
              {timings.map(([key, label]) => (
                <span key={key}>
                  {label} {secs(round.stage_timings[key]!)}
                </span>
              ))}
            </div>
          ) : (
            // Rounds published before the schema declared stage_timings — see
            // P3.9. Said plainly rather than rendered as zeros, which would
            // read as "this round took no time".
            <span>no stage timings recorded for this round</span>
          )}
          <div className="mt-1 flex flex-wrap gap-x-3">
            <span>{round.open_findings} still open</span>
            {round.artifact_id && (
              <span className="font-mono text-tertiary/60">
                {round.artifact_id}
              </span>
            )}
            {round.ts && <span className="font-mono text-tertiary/60">{round.ts}</span>}
          </div>
        </div>
      )}
    </li>
  );
}

export function ReviewTimeline({
  taskId,
  subtaskId,
}: {
  taskId: string | undefined;
  subtaskId: string | undefined;
}) {
  const { data } = useReviewRounds(taskId);
  if (!subtaskId) return null;

  const rounds = data?.subtasks?.[subtaskId] ?? [];
  if (rounds.length === 0) return null;

  const budget = data?.max_attempts || 5;

  return (
    <div className="space-y-1.5">
      <div className="text-2xs uppercase tracking-wider text-tertiary">
        Review — {rounds.length} round{rounds.length === 1 ? "" : "s"} of {budget}
      </div>
      <ul className="space-y-1">
        {rounds.map((r, i) => (
          <RoundRow key={`${r.attempt}-${r.ts}-${i}`} round={r} budget={budget} />
        ))}
      </ul>
    </div>
  );
}
