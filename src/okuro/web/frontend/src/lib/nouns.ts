// Plan vocabulary — the ONE place the user-facing nouns for a plan's units
// are spelled. Backend counterpart: okuro/orchestrator/gate_messages.py
// (STEP_NOUN / PART_NOUN / step_label / part_label). The two must agree.
//
// Why a module for two words (ROCK-SOLID v5, decision D-A):
// the noun was re-authored as inline JSX text at ~20 sites and as an f-string
// at ~12 backend emit sites. When the gate copy moved to "Step N / part N.M"
// in P1.3, every other surface kept saying "Phase" — the same card could say
// "Step 2 needs your decision" while the divider two lines above it said
// "Phase 2". A find-and-replace fixes those 32 sites; it does not stop the
// 33rd from drifting. Importing the noun does.
//
// MACHINE names are deliberately NOT renamed and never route through here:
// `phase.id`, `data-phase-*`, `data-testid="phase-divider-N"`, the
// `/api/tasks/{id}/phases/...` routes, plan.yaml's `phases:` key. Those are
// contracts, not copy. This module owns only what a person reads.

/** The user-facing noun for a plan phase. Capitalised — starts a label. */
export const STEP = "Step";

/** Lowercase form, for mid-sentence use ("this step is running…"). */
export const STEP_LOWER = "step";

/** Plural, e.g. "synthesizing execution steps". */
export const STEPS_LOWER = "steps";

/** The user-facing noun for a subtask. Lowercase — it appears mid-sentence
 *  far more often than it starts a label ("part 2.1", "every part"). */
export const PART = "part";

/** Capitalised form, for labels that open with it ("Part detail"). */
export const PART_CAP = "Part";

/** Plural, e.g. "once parts have started". */
export const PARTS = "parts";

/** "Step 2" — the canonical way to name a phase to a person. */
export function stepLabel(phaseId: number | string): string {
  return `${STEP} ${phaseId}`;
}

/** "part 2.1" — the canonical way to name a subtask to a person. */
export function partLabel(subtaskId: number | string): string {
  return `${PART} ${subtaskId}`;
}
