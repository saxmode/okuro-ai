// Lifecycle-state semantics — single source of truth for "has the engine
// stopped working on this task?". The authoritative value lives on
// `snapshot.lifecycle.state` (BE-supplied, same vocabulary as TaskStatus).
//
// Consumers: the activity chips (InlineThinker / OkuroThinker) use this to
// suppress a stale busy label when the task has actually settled — the chip
// otherwise trusts WS `/ws/activity` deltas only and sticks on a busy label
// if it misses the single `idle` frame. task-detail.tsx derives its own
// continuable-terminal set (same list) for the continue bar; keep them in
// sync — adding a terminal state means adding it here.

/**
 * Terminal lifecycle states — the orchestrator engine is no longer working
 * on the task. A chip must NEVER show a busy ("Generating…"/"Running…")
 * label while the task sits in one of these, regardless of missed WS deltas.
 */
const TERMINAL_LIFECYCLE_STATES = new Set([
  "done",
  "completed_partial",
  "failed",
  "blocked",
  "halted",
  "waiting_user",
]);

/**
 * True when `state` (a `snapshot.lifecycle.state` value) means the engine
 * has settled — done / failed / blocked / halted / waiting_user. Undefined
 * or any non-terminal value (planning / active / deliberating / …) → false,
 * so the chip keeps animating for genuinely-active tasks.
 */
export function isTerminalLifecycle(state: string | undefined | null): boolean {
  return state != null && TERMINAL_LIFECYCLE_STATES.has(state);
}
