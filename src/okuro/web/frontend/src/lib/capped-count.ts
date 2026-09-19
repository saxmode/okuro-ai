/**
 * R7 (372ccdb2) — "a capped list shows the TRUE TOTAL, never the cap; unknown
 * values render as a dash, never a neighbouring field."
 *
 * WHY THIS IS SHARED AND NOT THREE FIXES. p3 found the same fault on three
 * KNOW leaves at once (summary 5fa61313 §5), each with its own wording:
 *
 *   BRAIN      "200 sessions"                    /api/brain LIMIT 200, four arrays
 *   KNOWLEDGE  "MEMORIES 200 · THOUGHTS 200 …"   per_type_cap 200, hardcoded
 *   LESSONS    "AWAITING YOUR DECISION (100)"    limit=100, real total 492
 *
 * Three wordings for one problem is how it got there, so the rule lives in one
 * place. Q5 of the p3 summary asked for exactly this and recommended it.
 *
 * WHEN THE TOTAL IS UNKNOWABLE, SAY SO. None of the three endpoints returns a
 * total beside its capped array, and the charter keeps the backend untouched
 * until p5, so a count query is not available to this pass (the same
 * sequencing the START pass's Q2 settled). At the cap the honest statement is
 * that the total is unknown — that is the dash, and it is the same choice
 * `ui/segmented`'s third count state makes for the inbox chips.
 */

/** What a list prints when it cannot know how many there are. */
export const UNKNOWN_TOTAL = "—";

/**
 * Is this array the server's ceiling rather than the whole set?
 *
 * `>=` and not `===`: a cap that moves up leaves this correct, and an endpoint
 * that returns one row more than asked is still telling you it truncated.
 */
export function isCapped(rows: readonly unknown[] | undefined, cap: number): boolean {
  return (rows?.length ?? 0) >= cap;
}

/**
 * The total to hand to `countLabel`: the array's length when it IS the whole
 * set, and `null` — unknown — when the array is the cap.
 */
export function totalOrUnknown(
  rows: readonly unknown[] | undefined,
  cap: number,
): number | null {
  return isCapped(rows, cap) ? null : (rows?.length ?? 0);
}

/**
 * "n of N noun", with every honest variant:
 *
 *   countLabel(12, 12, "sessions")    -> "12 sessions"        nothing hidden
 *   countLabel(3, 12, "sessions")     -> "3 of 12 sessions"   filtered
 *   countLabel(200, null, "sessions") -> "200 of — sessions"  total unknown
 *
 * The noun arrives already plural because the callers' nouns are irregular
 * ("memories", "progress entries") — pluralising here would need a dictionary
 * to do worse.
 */
export function countLabel(shown: number, total: number | null, noun: string): string {
  if (total === null) return `${shown} of ${UNKNOWN_TOTAL} ${noun}`;
  if (shown !== total) return `${shown} of ${total} ${noun}`;
  return `${shown} ${noun}`;
}
