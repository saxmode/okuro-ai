import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CornerDownLeft,
  FolderOpen,
  GitCommitHorizontal,
  Brain,
  NotebookPen,
  TriangleAlert,
} from "lucide-react";
import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { displayAgent, parseApiDate } from "@/lib/format";
import {
  applyWindow,
  boardState,
  hasResume,
  projectsApi,
  sortByActionability,
  sortByChangeSize,
  type BoardState,
  type ProjectRow,
} from "@/lib/projects-api";

/**
 * /projects — the RESUME BOARD.
 *
 * A status dashboard tells you state. This tells you your next move: the
 * verbatim `progress.next_steps` the last agent wrote, per project, with an
 * age on it. That field is the highest-value one in okuro's store for someone
 * who context-switches, and before this screen it was invisible unless you
 * called get_progress on a slug you had already thought of.
 *
 * THREE RULES THIS SCREEN DOES NOT BEND:
 *
 * 1. THE RESUME LINE IS VERBATIM. Never summarised, never model-rewritten.
 *    Its whole value is being what was actually written, and a paraphrase of a
 *    stale note is a confident lie about a stale note.
 * 2. A MISSING RESUME LINE IS A FINDING, NOT A BLANK. It renders as an
 *    explicit "no resume" so the discipline gap is legible on the surface
 *    where it costs something.
 * 3. NO STATE DOT WITHOUT ITS NUMBERS. Hovering any dot shows the three ages
 *    it was derived from and the threshold. A bare colour is a claim with the
 *    evidence hidden.
 *
 * Zone 2 of the design (artifact "RESUME BOARD"). Zone 1 (the DELTA ribbon,
 * needs last_seen_at) and Zone 3 (hygiene triage) are phases p3 and h2-h5.
 *
 * Backend: GET /api/projects → sense/overview.py::projects_overview.
 */

// ── State chips ──────────────────────────────────────────────────────

const STATE_META: Record<
  BoardState,
  { label: string; dot: string; chip: string; hint: string }
> = {
  rotting: {
    label: "ROTTING",
    dot: "bg-error",
    chip: "bg-error/15 text-error",
    hint: "No activity in 14d, and it still owes you urgent todos or a blocker.",
  },
  stale: {
    label: "STALE",
    dot: "bg-warning",
    chip: "bg-warning/15 text-warning",
    hint: "The project moved but nobody logged what changed — treat the status below as unverified.",
  },
  unverifiable: {
    label: "UNVERIFIABLE",
    dot: "bg-fg-faint",
    chip: "bg-fg-faint/15 text-tertiary",
    hint: "Progress was declared, but no git repo and no dated rows exist to confirm or contradict it.",
  },
  live: {
    label: "LIVE",
    dot: "bg-success",
    chip: "bg-success/15 text-success",
    hint: "Progress is in step with the newest independent activity.",
  },
  quiet: {
    label: "QUIET",
    dot: "bg-fg-faint",
    chip: "bg-fg-faint/15 text-tertiary",
    hint: "No activity in 14d, and nothing urgent outstanding.",
  },
  archive: {
    label: "ARCHIVE?",
    dot: "bg-border-hover",
    chip: "bg-surface-subtle text-disabled",
    hint: "No activity in 90d and no open todos. Nothing here is waiting for you.",
  },
};

const STATE_ORDER: BoardState[] = [
  "rotting",
  "stale",
  "live",
  "unverifiable",
  "quiet",
  "archive",
];

/** Render an age the way the reader thinks about it, never as a raw float. */
function ageLabel(days: number | null | undefined): string {
  if (days === null || days === undefined) return "—";
  if (days < 1 / 24) return "just now";
  if (days < 1) return `${Math.round(days * 24)}h ago`;
  if (days < 30) return `${Math.round(days)}d ago`;
  return `${Math.round(days / 30)}mo ago`;
}

/**
 * The dot, and the evidence behind it. Every age the verdict was computed
 * from is in the tooltip — including the ones that are absent, said as
 * "no repo" / "no rows" rather than left out, because a reader cannot tell an
 * omitted signal from a signal that reported zero.
 */
function StateDot({ row }: { row: ProjectRow }) {
  const state = boardState(row);
  const meta = STATE_META[state];
  const s = row.staleness;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn("size-2.5 shrink-0 rounded-full", meta.dot)}
          aria-label={meta.label}
        />
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">
        <div className="flex flex-col gap-1 text-xs">
          <span className="font-medium">{meta.label}</span>
          <span className="text-muted-foreground">{meta.hint}</span>
          <span className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 tabular-nums">
            <span className="text-muted-foreground">progress</span>
            <span>
              {s.progress_age_days === undefined
                ? "never logged"
                : ageLabel(s.progress_age_days)}
            </span>
            <span className="text-muted-foreground">git HEAD</span>
            <span>
              {s.repo_age_days === undefined ? "no repo" : ageLabel(s.repo_age_days)}
            </span>
            <span className="text-muted-foreground">newest row</span>
            <span>
              {s.brain_age_days === undefined ? "no rows" : ageLabel(s.brain_age_days)}
            </span>
          </span>
        </div>
      </TooltipContent>
    </Tooltip>
  );
}

// ── Cost chips ───────────────────────────────────────────────────────

/**
 * Line 4. `no charter` and `no path` are COST chips, not neutral counts — each
 * one is re-learning every future session on that project has to pay for.
 */
function CostChips({ row }: { row: ProjectRow }) {
  const { open_todos = 0, urgent_todos = 0, brain_rows } = row.counts;
  const phases = row.phases;

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 type-small text-tertiary tabular-nums">
      {urgent_todos > 0 && (
        <span className="font-medium text-error">urgent ×{urgent_todos}</span>
      )}
      {open_todos > 0 && <span>todos {open_todos}</span>}
      {typeof brain_rows === "number" && <span>brain {brain_rows}</span>}
      {/* total 0 means NO PLAN DECLARED, which is not the same fact as 0% done
          — so a project without a plan gets no bar at all, not an empty one.
          Labelled "plan" rather than the project_phases noun: the plan-nouns
          guard owns that word for the ORCHESTRATOR's units, and borrowing its
          replacement ("Step") here would contradict project_status and every
          memory, which call these a project's phases. */}
      {phases.total > 0 && (
        <span>
          plan {phases.done ?? 0}/{phases.total}
        </span>
      )}
      {!row.has_charter && <span className="text-warning">no charter</span>}
      {!row.path_known && <span className="text-warning">no path</span>}
      {row.provisional && <span>provisional</span>}
    </div>
  );
}

// ── Card ─────────────────────────────────────────────────────────────

/**
 * Fixed four-line geometry, identical on every card, so the eye can pattern-
 * match down the column instead of re-parsing each one. Lines 3 and 4 are
 * ABSENT rather than empty when they have nothing to say.
 */
function ProjectCard({ row }: { row: ProjectRow }) {
  const state = boardState(row);
  const meta = STATE_META[state];
  const progress = row.progress;

  return (
    <li className="rounded-lg border border-border bg-surface-elevated px-4 py-3 transition-colors hover:border-border-hover">
      {/* 1 — identity */}
      <div className="flex items-center gap-2">
        <StateDot row={row} />
        <span className="truncate font-medium text-fg">{row.slug}</span>
        <Badge className={cn("shrink-0 text-[10px] tracking-wide", meta.chip)}>
          {meta.label}
        </Badge>
        {row.kind && (
          <span className="shrink-0 type-small text-tertiary">{row.kind}</span>
        )}
      </div>

      {/* 2 — the resume line, verbatim, or the absence of one said out loud */}
      <div className="mt-2 flex gap-2">
        <CornerDownLeft className="mt-0.5 size-3.5 shrink-0 text-tertiary" />
        {progress?.next_steps ? (
          <p className="type-small text-fg-muted">
            {progress.next_steps}{" "}
            <span className="whitespace-nowrap text-tertiary">
              — {displayAgent(progress.agent)}, {ageLabel(progress.age_days)}
            </span>
          </p>
        ) : (
          <p className="type-small text-disabled">
            {progress
              ? `no resume — ${displayAgent(progress.agent)} closed without next_steps, ${ageLabel(progress.age_days)}`
              : "no resume — nothing was ever logged for this project"}
          </p>
        )}
      </div>

      {/* 3 — blocker, only when there is one */}
      {progress?.blockers && (
        <div className="mt-1.5 flex gap-2">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" />
          <p className="type-small text-warning">{progress.blockers}</p>
        </div>
      )}

      {/* 4 — what this project costs */}
      <div className="mt-2">
        <CostChips row={row} />
      </div>
    </li>
  );
}

// ── Zone 1 — DELTA ───────────────────────────────────────────────────

const SIGNAL_META = {
  progress: { icon: NotebookPen, label: "an agent logged progress" },
  repo: { icon: GitCommitHorizontal, label: "the repo moved" },
  brain: { icon: Brain, label: "new rows were authored" },
} as const;

/** "since Sat 22:14" — the reference point, said out loud. A ribbon that does
 *  not name what it is diffing against is a claim without its basis. */
function sinceLabel(iso: string): string {
  const d = parseApiDate(iso);
  const days = (Date.now() - d.getTime()) / 86_400_000;
  if (days < 1) {
    return `since ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`;
  }
  if (days < 7) {
    return `since ${d.toLocaleDateString("en-GB", { weekday: "short" })} ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`;
  }
  return `since ${d.toLocaleDateString("en-GB", { day: "numeric", month: "short" })}`;
}

/**
 * Zone 1 — a DIFF, not a report.
 *
 * Only projects that moved since your previous visit, ordered by how much
 * moved. SILENCE IS INFORMATION: a project that did not move is absent, and an
 * empty ribbon is the honest answer "nothing changed", not a broken panel.
 *
 * Bounded height on purpose — this is the re-orientation glance, and a ribbon
 * that can grow to fill the screen has become the board it sits above.
 */
function DeltaRibbon({
  rows,
  since,
  firstVisit,
}: {
  rows: ProjectRow[];
  since: string | null;
  firstVisit: boolean;
}) {
  const moved = useMemo(() => sortByChangeSize(rows), [rows]);

  // A first visit has no reference point. Saying so beats listing all 101
  // projects as "new", which is technically true and completely useless.
  if (firstVisit || !since) {
    return (
      <section className="rounded-lg border border-dashed border-border px-4 py-3">
        <p className="type-small text-tertiary">
          First visit — nothing to compare against yet. Come back and this
          shows what moved while you were away.
        </p>
      </section>
    );
  }

  return (
    <section className="rounded-lg border border-border bg-surface-subtle px-4 py-3">
      <div className="flex items-baseline gap-2">
        <h2 className="type-label text-fg">DELTA</h2>
        <span className="type-small text-tertiary">{sinceLabel(since)}</span>
      </div>

      {moved.length === 0 ? (
        <p className="mt-2 type-small text-tertiary">
          Nothing moved. That is the answer, not an empty panel.
        </p>
      ) : (
        <ul className="mt-2 flex max-h-56 flex-col gap-1.5 overflow-y-auto">
          {moved.map((row) => (
            <li key={row.slug} className="flex items-center gap-2 type-small">
              <span className="flex shrink-0 items-center gap-1">
                {row.delta!.moved.map((sig) => {
                  const Icon = SIGNAL_META[sig].icon;
                  return (
                    <Tooltip key={sig}>
                      <TooltipTrigger asChild>
                        <Icon className="size-3.5 text-accent" />
                      </TooltipTrigger>
                      <TooltipContent>{SIGNAL_META[sig].label}</TooltipContent>
                    </Tooltip>
                  );
                })}
              </span>
              <span className="truncate font-medium text-fg">{row.slug}</span>
              <span className="ml-auto shrink-0 tabular-nums text-tertiary">
                {ageLabel(
                  (Date.now() - parseApiDate(row.delta!.newest_at).getTime()) /
                    86_400_000,
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ── Filters ──────────────────────────────────────────────────────────

const WINDOWS: { label: string; days: number | null }[] = [
  { label: "14 days", days: 14 },
  { label: "90 days", days: 90 },
  { label: "All", days: null },
];

function FilterPill({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-full border px-2.5 py-0.5 type-small transition-colors",
        active
          ? "border-accent bg-accent-subtle text-fg"
          : "border-border text-tertiary hover:border-border-hover hover:text-fg-muted",
      )}
    >
      {children}
    </button>
  );
}

/**
 * The honest coverage line. It reports what the board could NOT show as
 * prominently as what it did — a filtered-out project looks exactly like a
 * project that was never there, and only this sentence tells them apart.
 */
function CoverageNote({
  shown,
  hiddenByWindow,
  inWindow,
  withResume,
}: {
  shown: number;
  hiddenByWindow: number;
  inWindow: number;
  withResume: number;
}) {
  return (
    <p className="type-small text-tertiary">
      {shown} shown
      {hiddenByWindow > 0 && ` · ${hiddenByWindow} outside the window`}
      {" · "}
      {withResume} of {inWindow} have a resume line
    </p>
  );
}

// ── Page ─────────────────────────────────────────────────────────────

export function ProjectsPage() {
  const [windowDays, setWindowDays] = useState<number | null>(14);
  const [stateFilter, setStateFilter] = useState<BoardState | null>(null);
  const [query, setQuery] = useState("");

  /**
   * Record the visit ONCE per mount, and hold its reference point still.
   *
   * The POST shifts last_seen_at into previous_seen_at and hands back the
   * latter — the visit BEFORE this one, which is what the ribbon diffs
   * against. Pinned in a ref so re-renders, refetches and window-pill clicks
   * cannot move the goalposts mid-read; a StrictMode double-mount is guarded
   * because a second shift would collapse the reference onto this same visit
   * and silently empty the ribbon.
   */
  const [reference, setReference] = useState<{
    since: string | null;
    firstVisit: boolean;
  } | null>(null);
  const visitMarked = useRef(false);

  useEffect(() => {
    if (visitMarked.current) return;
    visitMarked.current = true;
    let cancelled = false;
    projectsApi
      .markVisit("projects")
      .then((v) => {
        if (cancelled) return;
        setReference({
          since: v.previous_seen_at,
          // No previous visit means no reference point, whether this is the
          // very first visit or the second one.
          firstVisit: v.previous_seen_at === null,
        });
      })
      .catch(() => {
        // The board is still fully useful without Zone 1 — degrade to "no
        // reference" rather than blocking the read the user came for.
        if (!cancelled) setReference({ since: null, firstVisit: true });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // One fetch for the whole registry; the window pills re-filter in place.
  // Waits for the reference so the rows arrive already carrying their deltas —
  // a second fetch would double the payload for one field.
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["projects", reference?.since ?? null],
    queryFn: () => projectsApi.list(reference?.since),
    enabled: reference !== null,
    staleTime: 30_000,
  });

  const { kept: inWindow, hidden: hiddenByWindow } = useMemo(
    () => applyWindow(data?.projects ?? [], windowDays),
    [data, windowDays],
  );

  const stateCounts = useMemo(() => {
    const counts = {} as Record<BoardState, number>;
    for (const row of inWindow) {
      const s = boardState(row);
      counts[s] = (counts[s] ?? 0) + 1;
    }
    return counts;
  }, [inWindow]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = inWindow.filter((row) => {
      if (stateFilter && boardState(row) !== stateFilter) return false;
      if (q && !row.slug.toLowerCase().includes(q)) return false;
      return true;
    });
    return sortByActionability(filtered);
  }, [inWindow, stateFilter, query]);

  const withResume = useMemo(() => inWindow.filter(hasResume).length, [inWindow]);

  return (
    <div className="page-shell space-y-8">
      <PageHeader
        title="Projects"
        subtitle="Where you left off — the last thing each agent said to do next, newest first by what is worth acting on."
      />

      {/* Zone 1 — the diff, above the board. Reads the WHOLE registry, never
          the windowed subset: something that moved while you were away is news
          whether or not it falls inside a convenience filter. */}
      {data && reference && (
        <DeltaRibbon
          rows={data.projects}
          since={reference.since}
          firstVisit={reference.firstVisit}
        />
      )}

      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <div className="flex items-center gap-1.5">
            <span className="type-small text-tertiary">Touched</span>
            {WINDOWS.map((w) => (
              <FilterPill
                key={w.label}
                active={windowDays === w.days}
                onClick={() => setWindowDays(w.days)}
              >
                {w.label}
              </FilterPill>
            ))}
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            {STATE_ORDER.filter((s) => stateCounts[s]).map((s) => (
              <FilterPill
                key={s}
                active={stateFilter === s}
                onClick={() => setStateFilter(stateFilter === s ? null : s)}
              >
                {STATE_META[s].label} {stateCounts[s]}
              </FilterPill>
            ))}
          </div>

          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by slug"
            className="ml-auto w-44 rounded-md border border-border bg-surface px-2.5 py-1 type-small outline-none focus:border-accent"
          />
        </div>

        {data && (
          <CoverageNote
            shown={visible.length}
            hiddenByWindow={hiddenByWindow}
            inWindow={inWindow.length}
            withResume={withResume}
          />
        )}
      </div>

      {isLoading && (
        <p className="type-small text-tertiary">Loading projects…</p>
      )}

      {isError && (
        <EmptyState
          title="Could not load projects"
          description={error instanceof Error ? error.message : "Unknown error"}
          icon={<TriangleAlert className="size-10 text-error" />}
        />
      )}

      {data && visible.length === 0 && (
        <EmptyState
          title="Nothing matches"
          description={
            stateFilter || query
              ? "Clear the filters to see the rest of the window."
              : "No project has been touched inside this window. Widen it to see the long tail."
          }
          icon={<FolderOpen className="size-10 text-tertiary" />}
        />
      )}

      {visible.length > 0 && (
        <ul className="flex flex-col gap-2">
          {visible.map((row) => (
            <ProjectCard key={row.slug} row={row} />
          ))}
        </ul>
      )}
    </div>
  );
}
