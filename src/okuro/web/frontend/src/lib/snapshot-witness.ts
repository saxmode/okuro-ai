import type { ActivityEvent, TaskSnapshot } from "@/types/api";

/**
 * Cross-channel staleness witness.
 *
 * The run view is push-first: WS `snapshot` events are written straight into
 * the react-query cache and the poll is disabled while the socket is up
 * (`{paused: connected}`). That leaves one hole with no recovery path — a
 * snapshot push dropped while the socket STAYS CONNECTED. No reconnect fires,
 * so the cursor replay in `api/main.py` never runs, and nothing re-checks
 * anything. The UI sits wrong until some unrelated event invalidates the
 * query. Observed live on task-20260727-005417: activity for subtask 1.2
 * streamed 01:11:45→01:12:13 while every status surface read PENDING, with the
 * API and plan.yaml both correct at that moment.
 *
 * WHY NOT GAP DETECTION ON `seq`. `seq` is `last_updated` epoch-ms
 * (state_reader.py), not a contiguous counter, so a missing push leaves no
 * hole to find. And a real counter would not help here either: it can only
 * reveal a gap when the NEXT push arrives, while the failure being fixed is
 * that no further push comes.
 *
 * WHAT DOES WORK. Activity rides a different channel from status, and that
 * separation — the thing that makes the two able to disagree — is also the
 * detector. Activity for a subtask the snapshot still calls pending is PROOF
 * the snapshot is behind, whatever the seq says and whether or not another
 * push is ever sent. One resync then repairs it.
 *
 * Deliberately conservative: fires only when the subtask is present in the
 * snapshot AND reads as not-yet-started. A subtask absent from the snapshot is
 * also evidence of staleness, but the reviewer streams rows under synthetic
 * ids (`reviewer:phase1:critic`) that are absent by design, and treating those
 * as contradictions would resync on every review pass.
 */

/** Activity types that can only be emitted once a subtask is actually running. */
const RUNNING_EVIDENCE: ReadonlySet<string> = new Set([
  "session_start",
  "subtask_start",
  "tool_use",
  "thinking",
  "text",
  "result",
  "review_starting",
  "review_complete",
  "subtask_retry",
  "verdict",
]);

/**
 * Snapshot statuses meaning "has not started". Anything else — running, done,
 * failed, blocked — is not contradicted by activity arriving.
 */
const NOT_YET_STARTED: ReadonlySet<string> = new Set([
  "pending",
  "queued",
  "waiting",
  "",
]);

/** The subtask an activity row belongs to, across the two field spellings. */
export function activitySubtaskId(event: ActivityEvent | null | undefined): string | null {
  if (!event) return null;
  const id = event.subtask_id ?? event.subtask;
  return typeof id === "string" && id.length > 0 ? id : null;
}

function findSubtaskStatus(
  snapshot: TaskSnapshot | null | undefined,
  subtaskId: string,
): string | null {
  if (!snapshot?.phases) return null;
  for (const phase of snapshot.phases) {
    for (const st of phase.subtasks ?? []) {
      if (st.id === subtaskId) return st.status ?? "";
    }
  }
  return null;
}

/**
 * True when this activity row proves the cached snapshot is stale.
 *
 * The characterization the run view has to satisfy: activity arrives for a
 * subtask whose snapshot still says pending → the UI must not render pending.
 */
export function activityContradictsSnapshot(
  snapshot: TaskSnapshot | null | undefined,
  event: ActivityEvent | null | undefined,
): boolean {
  if (!snapshot || !event) return false;
  if (!RUNNING_EVIDENCE.has(event.type)) return false;

  const subtaskId = activitySubtaskId(event);
  if (!subtaskId) return false;

  const status = findSubtaskStatus(snapshot, subtaskId);
  // null = not in the snapshot at all. Conservative: see the module docstring.
  if (status === null) return false;

  return NOT_YET_STARTED.has(status.toLowerCase());
}
