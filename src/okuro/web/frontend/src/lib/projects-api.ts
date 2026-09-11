import { api } from "@/lib/api";

/**
 * /projects — the RESUME BOARD's data layer.
 *
 * Reads GET /api/projects, which is the same `projects_overview` function the
 * MCP tool calls, so an agent session and this screen cannot disagree about
 * where a project stands.
 *
 * THE PAYLOAD IS COMPACT BY DESIGN AND THAT SHAPES EVERY TYPE HERE. The
 * backend drops None-valued keys (it cost 299 tokens/project before, 87% of a
 * bootstrap budget at 38 rows), so almost everything is optional and an absent
 * key means "absent" rather than "zero". `progress: null` is a project nobody
 * ever logged; `counts.brain_rows` absent is a project with no authored rows.
 * Never default an absent key to 0 and then render it as a measurement.
 */

// ── Payload types (mirror sense/overview.py::projects_overview) ───────

export type StalenessVerdict = "no_progress" | "unverifiable" | "stale" | "current";

export interface ProjectProgress {
  status: string | null;
  agent: string | null;
  phase?: string | null;
  updated_at: string;
  age_days: number;
  summary?: string;
  next_steps?: string;
  blockers?: string;
}

export interface ProjectPhases {
  done?: number;
  total: number;
  pending?: number;
  active?: number;
  dropped?: number;
  active_key?: string;
  active_title?: string;
  next_pending?: string;
  percent_done?: number | null;
}

export interface ProjectCounts {
  open_todos?: number;
  urgent_todos?: number;
  brain_rows?: number;
}

export interface ProjectStaleness {
  verdict: StalenessVerdict;
  progress_age_days?: number;
  repo_age_days?: number;
  /** NOTE the name: `_thin_staleness` renames status.py's
   *  `newest_brain_row_age_days` to this on the way out. Reading the long name
   *  here silently yields undefined — that exact mismatch dropped two live
   *  projects from the 14d filter before it was fixed. */
  brain_age_days?: number;
}

/** Present ONLY on projects that moved since the reference point. Its absence
 *  is the answer "this did not move" — never read it as a zero. */
export interface ProjectDelta {
  /** Which of the three signals moved. "the repo moved but nobody logged it"
   *  and "an agent logged progress" are different events. */
  moved: ("progress" | "repo" | "brain")[];
  newest_at: string;
  signals: number;
}

export interface ProjectRow {
  slug: string;
  name: string | null;
  kind: string | null;
  provisional: boolean;
  path_known: boolean;
  has_charter: boolean;
  charter_chars: number;
  phases: ProjectPhases;
  progress: ProjectProgress | null;
  counts: ProjectCounts;
  staleness: ProjectStaleness;
  delta?: ProjectDelta;
}

export interface ProjectsMeta {
  returned: number;
  filtered_out_by_age: number;
  stale_threshold_days: number;
  touched_within_days: number | null;
  active_only: boolean;
  repo_ages_included: boolean;
  note: string;
  shape_note: string;
  counting_note: string;
  /** Echoed back so "you never asked for a diff" is distinguishable from
   *  "you asked and nothing moved" — both yield zero delta blocks. */
  since: string | null;
  moved: number | null;
}

export interface ProjectsPayload {
  projects: ProjectRow[];
  meta: ProjectsMeta;
}

/**
 * Always fetches the WHOLE registry and windows it in the browser.
 *
 * The backend can filter by age, and the board deliberately does not ask it to.
 * Two reasons, and the first is a correctness one:
 *
 * ROTTING IS UNREACHABLE THROUGH THE SERVER FILTER. That state requires 14d+ of
 * silence; `touched_within_days=14` keeps only what moved INSIDE 14d. The two
 * are complements, so the default window would have hidden every rotting
 * project — the one state the board exists to surface — and it would have
 * looked like there simply were none.
 *
 * Second, it is cheap: 48 KB for 101 projects against 24 KB for 37, and the
 * window pills then re-filter with no refetch at all.
 */
/** A surface's "when did you last look" record. */
export interface Visit {
  surface: string;
  last_seen_at: string | null;
  /** THE field the ribbon diffs against — the visit BEFORE this one. Diffing
   *  against `last_seen_at` always yields nothing, because opening the board
   *  is what set it. */
  previous_seen_at: string | null;
  first_visit: boolean;
}

export const projectsApi = {
  list: (since?: string | null) =>
    api<ProjectsPayload>(
      `/api/projects${since ? `?since=${encodeURIComponent(since)}` : ""}`,
    ),
  /** Records the visit and returns the shifted record in one round-trip. */
  markVisit: (surface: string) =>
    api<Visit>(`/api/visits/${encodeURIComponent(surface)}`, { method: "POST" }),
};

/**
 * Zone 1 ordering — by SIZE OF CHANGE, not recency.
 *
 * A project where all three signals moved is a bigger event than one where a
 * single commit landed, and sorting by time alone buries it under whatever
 * happened most recently. Recency breaks the tie.
 */
export function sortByChangeSize(rows: ProjectRow[]): ProjectRow[] {
  return [...rows]
    .filter((r) => r.delta)
    .sort((a, b) => {
      const bySignals = (b.delta?.signals ?? 0) - (a.delta?.signals ?? 0);
      if (bySignals !== 0) return bySignals;
      const an = a.delta?.newest_at ?? "";
      const bn = b.delta?.newest_at ?? "";
      if (an !== bn) return an < bn ? 1 : -1;
      return a.slug.localeCompare(b.slug);
    });
}

/**
 * Apply the time window client-side, with the carve-out that makes the window
 * safe: a ROTTING project is never hidden by it. "Touched in the last 14 days"
 * is a convenience filter, and silently dropping work you started, blocked, and
 * still owe is not a convenience.
 */
export function applyWindow(
  rows: ProjectRow[],
  windowDays: number | null,
): { kept: ProjectRow[]; hidden: number } {
  if (windowDays === null) return { kept: rows, hidden: 0 };
  const kept = rows.filter((row) => {
    if (boardState(row) === "rotting") return true;
    const newest = newestActivityDays(row);
    return newest !== null && newest <= windowDays;
  });
  return { kept, hidden: rows.length - kept.length };
}

// ── Derived board state ──────────────────────────────────────────────

/**
 * The chip a card wears. DERIVED from the three ages plus urgency and
 * blockers — never declared, so it cannot rot independently of what it
 * measures.
 *
 * `stale` is not in the original design's chip table but the backend produces
 * that verdict and it means something none of the others do: the project moved
 * and nobody logged what changed. Folding it into `live` would make the board
 * assert a status the data does not support, which is the one failure mode the
 * whole surface exists to avoid.
 */
export type BoardState =
  | "rotting"
  | "stale"
  | "unverifiable"
  | "live"
  | "quiet"
  | "archive";

/** No signal for this long and nothing outstanding → a deletion candidate. */
export const ARCHIVE_DAYS = 90;
/** No signal for this long → it stopped, whatever the last log claimed. */
export const QUIET_DAYS = 14;

/** Youngest of the three activity ages, or null when the project has no signal
 *  at all. Absent keys are skipped rather than read as 0 — a missing git age
 *  is "no repo", not "committed today". */
export function newestActivityDays(row: ProjectRow): number | null {
  const ages = [
    row.progress?.age_days,
    row.staleness.repo_age_days,
    row.staleness.brain_age_days,
  ].filter((a): a is number => typeof a === "number");
  return ages.length ? Math.min(...ages) : null;
}

export function boardState(row: ProjectRow): BoardState {
  const newest = newestActivityDays(row);
  const urgent = row.counts.urgent_todos ?? 0;
  const open = row.counts.open_todos ?? 0;
  const blocked = Boolean(row.progress?.blockers);

  // No dated signal anywhere. Not "fresh" — unmeasurable, and with nothing
  // outstanding there is nothing to come back for.
  if (newest === null) return open === 0 ? "archive" : "quiet";

  if (newest >= ARCHIVE_DAYS && open === 0) return "archive";
  // The chip that earns the screen: started, still owed, nobody has looked.
  if (newest >= QUIET_DAYS && (urgent > 0 || blocked)) return "rotting";
  if (newest >= QUIET_DAYS) return "quiet";

  if (row.staleness.verdict === "unverifiable") return "unverifiable";
  if (row.staleness.verdict === "stale") return "stale";
  return "live";
}

export function hasResume(row: ProjectRow): boolean {
  return Boolean(row.progress?.next_steps);
}

/**
 * Actionability rank — lower sorts first. Alphabetical is how a 100-row list
 * becomes wallpaper, so slug order is never the primary key.
 *
 * A resume line is worth a whole tier: a card you can act on immediately beats
 * a card in the same state that only tells you it exists.
 */
/**
 * Spaced by 2 so the no-resume penalty below can occupy the odd slots without
 * colliding with the next state.
 *
 * LIVE OUTRANKS STALE, and that ordering was corrected against real data: a
 * first cut put stale first (it looks like a problem, and problems feel
 * actionable) and the live board then opened with three projects whose progress
 * was 22-28d old, pushing the project touched *that minute* below the fold.
 * Stale means the label is untrustworthy, not that the work is owed. Only
 * ROTTING outranks what you are actually working on.
 */
const TIER: Record<BoardState, number> = {
  rotting: 0,
  live: 2,
  stale: 4,
  unverifiable: 6,
  quiet: 8,
  archive: 10,
};

export function actionabilityRank(row: ProjectRow): number {
  const state = boardState(row);
  const base = TIER[state];
  // Within the actionable states, having a resume line promotes the card one
  // tier. quiet/archive are not promoted — a resume line on a project nobody
  // has touched in a month is not more actionable, it is just older.
  if (state === "quiet" || state === "archive") return base;
  return hasResume(row) ? base : base + 1;
}

/** Tiers where the reader wants the freshest first; elsewhere the most
 *  neglected first, because neglect is the finding. */
const FRESHEST_FIRST = new Set<BoardState>(["live", "stale", "unverifiable"]);

export function sortByActionability(rows: ProjectRow[]): ProjectRow[] {
  return [...rows].sort((a, b) => {
    const rank = actionabilityRank(a) - actionabilityRank(b);
    if (rank !== 0) return rank;

    const aAge = newestActivityDays(a);
    const bAge = newestActivityDays(b);
    // A project with no signal at all sorts last within its tier — it can be
    // neither the freshest nor the most neglected, because it has no age.
    if (aAge === null && bAge === null) return a.slug.localeCompare(b.slug);
    if (aAge === null) return 1;
    if (bAge === null) return -1;

    const freshest = FRESHEST_FIRST.has(boardState(a));
    const byAge = freshest ? aAge - bAge : bAge - aAge;
    if (byAge !== 0) return byAge;
    return a.slug.localeCompare(b.slug);
  });
}
