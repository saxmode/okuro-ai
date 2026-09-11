import { memo, useState, useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import { formatTime } from "@/lib/format";
import type { ActivityEvent, ReviewVerdictEvent } from "@/types/api";

interface ActivityFeedProps {
  events: ActivityEvent[];
  /**
   * When set, the feed narrows to events this subtask produced
   * (matched on subtask_id stamped by the dispatcher). Role-filter
   * chips still work within that slice.
   */
  selectedSubtaskId?: string;
  /**
   * Phase 4 — how many older events the merge dropped to bound the list.
   * Surfaced as an explicit "N older hidden" banner so a long stream never
   * appears complete when it isn't (no silent truncation).
   */
  hiddenOlder?: number;
  /**
   * Durable review verdicts from the task_events store (GET /review). The
   * SINGLE SOURCE OF TRUTH for review outcomes. A terminal review round renders
   * its findings from the matching verdict (by phase) so the outcome is visible
   * even when the best-effort `.activity.jsonl` stream dropped the finding rows.
   * Optional — omitted (legacy callers / tests) means stream-only behaviour.
   */
  reviewVerdicts?: ReviewVerdictEvent[];
}

/**
 * Bucket an event into its activity-feed group key.
 *
 * REAL subtasks own the timeline. Reviewer rows are no longer routed into a
 * trailing `reviewer:phase{N}` section — they interleave chronologically into
 * the round where they happened (see `buildRows`). For grouping/chip purposes
 * a streamed reviewer row (`reviewer:phase{N}:*`) is attributed to the FIRST
 * real subtask of its parent phase so it never spawns a phantom group; the
 * engine-emitted `review_starting` / `review_complete` markers already carry
 * the real subtask_id, so they bucket correctly on their own.
 */
function bucketKey(ev: ActivityEvent): string {
  const sid = ev.subtask_id ?? "";
  if (sid.startsWith("reviewer:")) {
    // Attribute to the parent phase so streamed reviewer rows fold into a real
    // subtask group rather than a separate section. The actual placement
    // (which subtask, which round) is resolved in buildRows; bucketKey only
    // needs a stable, real-subtask key for the chip/filter machinery.
    return "reviewer-stream";
  }
  return sid || "untagged";
}

/**
 * Extract the parent phase number from any subtask id. Handles canonical
 * dotted ids ("1.1" → 1, "3.2.1" → 3) AND deliberate-mode position-node
 * ids prefixed with `d` ("d1.5" → 1). Returns null when no leading
 * numeric component is parseable, so callers can fall back without
 * breaking on synthetic / legacy ids.
 */
function parentPhaseId(subtaskId: string | undefined | null): number | null {
  if (!subtaskId) return null;
  const m = subtaskId.replace(/^d/, "").match(/^(\d+)/);
  return m ? parseInt(m[1]!, 10) : null;
}

/**
 * Extract the phase id encoded in a reviewer-synthesized subtask id like
 * "reviewer:phase3:critic". Returns null on shape mismatch (e.g. the
 * malformed `reviewer:phase:critic` rows present in legacy logs) so callers
 * fall back gracefully instead of crashing.
 */
function reviewerPhaseId(subtaskId: string | undefined | null): number | null {
  if (!subtaskId || !subtaskId.startsWith("reviewer:phase")) return null;
  const m = subtaskId.match(/^reviewer:phase(\d+):/);
  return m ? parseInt(m[1]!, 10) : null;
}

function groupTestId(key: string): string {
  if (key === "untagged") return "activity-group-untagged";
  return `activity-group-subtask-${key}`;
}

function groupLabel(key: string): string {
  if (key === "untagged") return "Activity";
  return key;
}

// -- Verdict → design-system tone -------------------------------------------
// Single source of truth for verdict visuality. Reuses the exact token
// utilities the pipeline-view review badge uses (border/bg-subtle/text) so the
// activity feed and the tile badge never drift. Verdict is conveyed by
// text + icon, not colour alone (WCAG AA — colour is a redundant cue).
type VerdictTone = {
  /** border + subtle bg + text classes for the collapsed chip */
  chip: string;
  /** label shown on the chip */
  label: string;
  /** non-colour redundant glyph */
  icon: string;
  /** bullet colour for the live (in-progress) row */
  bullet: string;
};

const VERDICT_TONE: Record<string, VerdictTone> = {
  PASS: {
    chip: "border-success/50 bg-success-subtle/60 text-success",
    label: "PASS",
    icon: "✓",
    bullet: "bg-success",
  },
  CONDITIONAL: {
    chip: "border-warning/50 bg-warning-subtle/60 text-warning",
    label: "CONDITIONAL",
    icon: "!",
    bullet: "bg-warning",
  },
  FAIL: {
    chip: "border-error/50 bg-error-subtle/60 text-error",
    label: "FAIL",
    icon: "✕",
    bullet: "bg-error",
  },
  // ROCK-SOLID v5 P1.3 — CAP means "the retry budget is spent, your turn to
  // decide", the same "waiting on you" fact NEEDS_USER already gets right.
  // Was styled identically to FAIL (evidence inventory mismatch #8).
  CAP: {
    chip: "border-info/50 bg-info/10 text-info",
    label: "CAP",
    icon: "⊘",
    bullet: "bg-info",
  },
  NEEDS_USER: {
    chip: "border-info/50 bg-info/10 text-info",
    label: "NEEDS YOU",
    icon: "?",
    bullet: "bg-info",
  },
};

const UNKNOWN_TONE: VerdictTone = {
  chip: "border-warning/50 bg-warning-subtle/60 text-warning",
  label: "REVIEW",
  icon: "•",
  bullet: "bg-warning",
};

function verdictTone(v: string | undefined | null): VerdictTone {
  return (v && VERDICT_TONE[v]) || UNKNOWN_TONE;
}

// A review round bundles one reviewer pass over a subtask: the
// `review_starting` marker, every streamed critic/scorer detail row for that
// phase, and the closing `review_complete` marker (which carries the verdict).
// A round with a `review_complete` is TERMINAL → renders collapsed. A round
// still open (started, no complete) is LIVE → renders expanded.
interface ReviewRound {
  kind: "round";
  ts: string;
  verdict?: string;
  terminal: boolean;
  loadBearing?: number;
  // Parent phase of this round — used to look up the durable verdict + findings
  // from the task_events store (reviewVerdicts) as the authoritative outcome.
  phaseId?: number;
  detail: ActivityEvent[];
}

type FeedRow =
  | { kind: "event"; ts: string; event: ActivityEvent }
  | ReviewRound;

/**
 * Build the chronological, interleaved row list for ONE real-subtask group.
 *
 * `own` = the subtask's own rows + its review_starting/review_complete markers
 * (engine-tagged with the real subtask_id). `reviewerStream` = the streamed
 * critic/scorer rows for the subtask's parent phase (synthetic ids), to be
 * folded into whichever round was open when they fired.
 *
 * Algorithm: walk both streams merged by ts. A `review_starting` opens the
 * current round; reviewer-stream rows and the `review_complete` attach to it;
 * `review_complete` closes it (terminal). Agent rows pass through inline. This
 * yields: [agent r1] → [review r1 collapsed] → [agent r2] → [review r2] …
 */
function buildRows(
  own: ActivityEvent[],
  reviewerStream: ActivityEvent[],
): FeedRow[] {
  const merged = [...own, ...reviewerStream].sort((a, b) =>
    (a.ts || "").localeCompare(b.ts || ""),
  );

  const rows: FeedRow[] = [];
  let open: ReviewRound | null = null;

  for (const ev of merged) {
    const isStreamRow = (ev.subtask_id ?? "").startsWith("reviewer:");
    if (ev.type === "review_starting") {
      open = {
        kind: "round",
        ts: ev.ts,
        terminal: false,
        phaseId: ev.phase_id ?? parentPhaseId(ev.subtask_id) ?? undefined,
        detail: [ev],
      };
      rows.push(open);
      continue;
    }
    if (ev.type === "review_complete") {
      if (!open) {
        // Orphan completion (no matching start in this slice) — start a
        // terminal round so the verdict still surfaces.
        open = { kind: "round", ts: ev.ts, terminal: false, detail: [] };
        rows.push(open);
      }
      open.detail.push(ev);
      open.verdict = ev.verdict;
      open.loadBearing = ev.load_bearing_findings;
      if (open.phaseId == null)
        open.phaseId = ev.phase_id ?? parentPhaseId(ev.subtask_id) ?? undefined;
      open.terminal = true;
      open = null;
      continue;
    }
    if (isStreamRow) {
      // Streamed reviewer detail — attach to the open round if one is live,
      // else open an implicit (non-terminal) round so it never leaks into the
      // agent's own timeline as a bare row.
      if (!open) {
        open = { kind: "round", ts: ev.ts, terminal: false, detail: [] };
        rows.push(open);
      }
      if (open.phaseId == null)
        open.phaseId = reviewerPhaseId(ev.subtask_id) ?? undefined;
      open.detail.push(ev);
      continue;
    }
    // Plain agent row.
    rows.push({ kind: "event", ts: ev.ts, event: ev });
  }
  return rows;
}

export const ActivityFeed = memo(function ActivityFeed({
  events,
  selectedSubtaskId,
  hiddenOlder = 0,
  reviewVerdicts,
}: ActivityFeedProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  // Filter is INSTANCE-level (timeline bucket = subtask_id), NOT role.
  const [instanceFilter, setInstanceFilter] = useState<string | null>(null);

  // Subtask scope comes first — a clicked agent should never see another
  // agent's lines, even if they share a role. Graceful fallback: tasks that
  // ran before subtask_id tagging existed have no tags on any event; in that
  // case skip the filter so historical tasks still show full activity.
  //
  // Reviewer interleave: when a subtask is selected, also pull in the streamed
  // reviewer rows whose `reviewer:phase{N}:` prefix matches the subtask's
  // parent phase. Those rows then interleave INLINE (as collapsed review-round
  // chips) at the point they occurred — no separate reviewer section.
  const hasAnyTaggedEvent = events.some((e) => e.subtask_id);
  const selectedPhaseId = parentPhaseId(selectedSubtaskId);
  const scopedEvents =
    selectedSubtaskId && hasAnyTaggedEvent
      ? events.filter((e) => {
          if (e.subtask_id === selectedSubtaskId) return true;
          if (selectedPhaseId == null) return false;
          const revPhase = reviewerPhaseId(e.subtask_id);
          return revPhase != null && revPhase === selectedPhaseId;
        })
      : events;

  // Chips are one-per-INSTANCE, keyed on the real subtask_id. A fan-out phase
  // runs several subtasks of the SAME role in parallel; keying on the instance
  // lets the user isolate a single tile's stream. Reviewer-stream rows do not
  // get their own chip — they live inside the subtask groups they reviewed.
  const instances: { key: string; label: string }[] = [];
  const seenInstance = new Set<string>();
  for (const e of scopedEvents) {
    const key = bucketKey(e);
    if (key === "untagged" || key === "reviewer-stream" || seenInstance.has(key))
      continue;
    seenInstance.add(key);
    const label = e.role ? `${key} · ${e.role}` : key;
    instances.push({ key, label });
  }

  // Filtering to one instance keeps the reviewer rows of that instance's
  // parent phase, so the M3 critic/scorer judging it still interleaves inline.
  const filterPhaseId = parentPhaseId(instanceFilter);
  const filtered = instanceFilter
    ? scopedEvents.filter((e) => {
        if (bucketKey(e) === instanceFilter) return true;
        if (filterPhaseId == null) return false;
        const revPhase = reviewerPhaseId(e.subtask_id);
        return revPhase != null && revPhase === filterPhaseId;
      })
    : scopedEvents;

  // Partition into (a) real-subtask groups in first-seen order and (b) a pool
  // of streamed reviewer rows keyed by parent phase. Then each group's rows are
  // built by interleaving its own events with the reviewer-stream rows of its
  // phase (folded into collapsible rounds).
  const groupOrder: string[] = [];
  const ownByKey = new Map<string, ActivityEvent[]>();
  const reviewerByPhase = new Map<number, ActivityEvent[]>();
  for (const ev of filtered) {
    const sid = ev.subtask_id ?? "";
    if (sid.startsWith("reviewer:")) {
      const ph = reviewerPhaseId(sid);
      if (ph == null) continue; // drop malformed `reviewer:phase:critic` rows
      let bucket = reviewerByPhase.get(ph);
      if (!bucket) {
        bucket = [];
        reviewerByPhase.set(ph, bucket);
      }
      bucket.push(ev);
      continue;
    }
    const key = sid || "untagged";
    let own = ownByKey.get(key);
    if (!own) {
      own = [];
      ownByKey.set(key, own);
      groupOrder.push(key);
    }
    own.push(ev);
  }

  // The streamed reviewer rows for a phase belong to a single round at a time.
  // Attach them only to the FIRST subtask of each phase so they aren't
  // duplicated across every sibling subtask (the per-subtask review_starting /
  // review_complete markers still render the round chip in every sibling).
  const firstSubtaskOfPhase = new Map<number, string>();
  for (const key of groupOrder) {
    const ph = parentPhaseId(key);
    if (ph != null && !firstSubtaskOfPhase.has(ph)) {
      firstSubtaskOfPhase.set(ph, key);
    }
  }

  const groups = groupOrder.map((key) => {
    const ph = parentPhaseId(key);
    const stream =
      ph != null && firstSubtaskOfPhase.get(ph) === key
        ? reviewerByPhase.get(ph) ?? []
        : [];
    return { key, rows: buildRows(ownByKey.get(key) ?? [], stream) };
  });

  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [filtered.length, autoScroll]);

  const handleScroll = () => {
    if (!scrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    setAutoScroll(scrollHeight - scrollTop - clientHeight < 40);
  };

  return (
    <div className="flex h-full flex-col">
      {/* Header with instance filter */}
      <div className="flex h-row-dense flex-wrap items-center gap-1 border-b border-border px-3">
        <span className="mr-2 text-2xs uppercase tracking-wider text-tertiary">
          Activity{selectedSubtaskId ? ` · ${selectedSubtaskId}` : ""}
        </span>
        <button
          onClick={() => setInstanceFilter(null)}
          className={cn(
            "rounded px-1.5 py-0.5 text-3xs",
            !instanceFilter
              ? "bg-accent text-inverse"
              : "text-tertiary hover:text-fg-muted",
          )}
        >
          ALL
        </button>
        {instances.map((inst) => (
          <button
            key={inst.key}
            onClick={() =>
              setInstanceFilter(inst.key === instanceFilter ? null : inst.key)
            }
            title={inst.label}
            className={cn(
              "rounded px-1.5 py-0.5 text-3xs",
              instanceFilter === inst.key
                ? "bg-accent text-inverse"
                : "text-tertiary hover:text-fg-muted",
            )}
          >
            {inst.label}
          </button>
        ))}
        <span className="ml-auto text-3xs text-tertiary">
          {filtered.length} events
        </span>
      </div>

      {/* Timeline */}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto px-3 pt-3 pb-2"
      >
        {hiddenOlder > 0 && (
          <div
            data-testid="activity-hidden-older"
            className="mb-2 rounded border border-border bg-surface/60 px-2 py-1 text-3xs text-tertiary"
          >
            {hiddenOlder.toLocaleString()} older event
            {hiddenOlder === 1 ? "" : "s"} hidden — showing the most recent.
          </div>
        )}
        {filtered.length === 0 ? (
          <div className="flex h-full items-center justify-center text-xs text-tertiary">
            No activity yet
          </div>
        ) : (
          groups.map((g) => (
            <section
              key={g.key}
              data-testid={groupTestId(g.key)}
              className="mb-3 last:mb-0"
            >
              <h3 className="mb-1 text-3xs font-medium uppercase tracking-wider text-tertiary">
                {groupLabel(g.key)}
              </h3>
              <ol className="ml-1">
                {g.rows.map((row, i) =>
                  row.kind === "round" ? (
                    <ReviewRoundItem
                      key={i}
                      round={row}
                      reviewVerdicts={reviewVerdicts}
                      isLast={i === g.rows.length - 1}
                    />
                  ) : (
                    <TimelineItem
                      key={i}
                      event={row.event}
                      isLast={i === g.rows.length - 1}
                    />
                  ),
                )}
              </ol>
            </section>
          ))
        )}
      </div>
    </div>
  );
});

/**
 * A review round rendered inline at its chronological position.
 *
 * Terminal round → COLLAPSED chip: a real <button> showing the verdict via
 * colour + icon + label (redundant cues, WCAG AA), expandable to reveal the
 * round's detail rows. Live round (no terminal verdict yet) → rendered
 * expanded so the user watches the reviewer judge in real time.
 */
const ReviewRoundItem = memo(function ReviewRoundItem({
  round,
  reviewVerdicts,
  isLast,
}: {
  round: ReviewRound;
  reviewVerdicts?: ReviewVerdictEvent[];
  isLast: boolean;
}) {
  const tone = verdictTone(round.verdict);
  // Live rounds start expanded; terminal rounds start collapsed.
  const [expanded, setExpanded] = useState(!round.terminal);

  const showDetail = expanded || !round.terminal;

  // Authoritative findings from the durable task_events record (single source
  // of truth), matched to this round's phase. Used when the best-effort stream
  // carried no per-finding rows — so the outcome is never invisible just
  // because `.activity.jsonl` dropped them. Latest verdict for the phase wins.
  const streamHasFindings = round.detail.some(
    (e) => e.type === "critic_finding",
  );
  let durableFindings: { summary?: string; file?: string | null; line?: string | number | null }[] = [];
  if (round.phaseId != null && reviewVerdicts && reviewVerdicts.length > 0) {
    const match = reviewVerdicts
      .filter((v) => v.body?.phase_id === round.phaseId)
      .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
      .at(-1);
    durableFindings = match?.body?.load_bearing_critic_findings ?? [];
  }
  const showDurableFindings =
    showDetail && !streamHasFindings && durableFindings.length > 0;

  return (
    <li
      data-testid="review-round"
      data-verdict={round.verdict ?? (round.terminal ? "UNKNOWN" : "LIVE")}
      className={cn("relative pl-5", !isLast && "pb-3")}
    >
      {!isLast && (
        <span
          aria-hidden="true"
          className="absolute left-[4px] top-2 bottom-0 w-px bg-border/60"
        />
      )}
      <span
        aria-hidden="true"
        className={cn(
          "absolute left-0 top-1.5 h-2 w-2 rounded-full ring-2 ring-surface",
          round.terminal ? tone.bullet : "bg-warning motion-safe:animate-pulse",
        )}
      />

      {/* Collapsed chip header — a real button, keyboard-operable. */}
      <button
        type="button"
        data-testid="review-chip"
        aria-expanded={showDetail}
        onClick={round.terminal ? () => setExpanded((v) => !v) : undefined}
        disabled={!round.terminal}
        className={cn(
          "inline-flex items-center gap-1.5 rounded border px-1.5 py-0.5 text-3xs font-bold uppercase tracking-wider",
          round.terminal
            ? tone.chip
            : "border-warning/60 bg-warning-subtle/60 text-warning",
          round.terminal && "cursor-pointer hover:brightness-110",
        )}
        title={
          round.terminal
            ? `Review ${tone.label}${
                round.loadBearing != null
                  ? ` · ${round.loadBearing} load-bearing`
                  : ""
              } — ${showDetail ? "hide" : "show"} detail`
            : "Reviewer is judging this round"
        }
      >
        <span aria-hidden="true">
          {round.terminal ? tone.icon : "•"}
        </span>
        <span>
          {round.terminal ? `review ${tone.label}` : "reviewing…"}
        </span>
        {round.terminal && round.loadBearing != null && (
          <span className="font-normal normal-case opacity-80">
            {round.loadBearing} load-bearing
          </span>
        )}
        {round.terminal && (
          <span aria-hidden="true" className="font-normal opacity-70">
            {showDetail ? "▾" : "▸"}
          </span>
        )}
        <span className="ml-1 font-mono font-normal normal-case opacity-70">
          {formatTime(round.ts)}
        </span>
      </button>

      {/* Detail rows — the reviewer's own activity for this round. */}
      {showDetail && round.detail.length > 0 && (
        <ol className="ml-3 mt-1 border-l border-border/40 pl-2">
          {round.detail.map((ev, i) => (
            <TimelineItem
              key={i}
              event={ev}
              isLast={i === round.detail.length - 1}
            />
          ))}
        </ol>
      )}

      {/* Authoritative findings from the durable record — shown when the live
          stream carried none, so the "why it failed" is never lost. */}
      {showDurableFindings && (
        <ol
          data-testid="durable-findings"
          className="ml-3 mt-1 border-l border-border/40 pl-2"
        >
          {durableFindings.map((f, i) => (
            <li key={i} className="relative pb-2 pl-5">
              <span
                aria-hidden="true"
                className="absolute left-0 top-1.5 h-2 w-2 rounded-full bg-error ring-2 ring-surface"
              />
              <div className="flex items-baseline gap-2 text-3xs">
                <span className="font-bold uppercase tracking-wider text-error">
                  must-fix
                </span>
              </div>
              <p className="mt-0.5 whitespace-pre-wrap break-words text-xs text-fg-muted">
                {f.summary ?? "(no summary)"}
                {f.file ? ` (${f.file}${f.line != null ? `:${f.line}` : ""})` : ""}
              </p>
            </li>
          ))}
        </ol>
      )}
    </li>
  );
});

// Color of the bullet — maps event type to semantic status color.
// `tool_use` is resolved per-event: okuro calls → brand green, everything
// else → pure white. See TimelineItem below.
const BULLET_COLOR: Record<string, string> = {
  thinking: "bg-tertiary",
  text: "bg-fg-muted",
  session_start: "bg-accent",
  subtask_start: "bg-warning",
};

const LABEL_COLOR: Record<string, string> = {
  thinking: "text-tertiary",
  text: "text-fg-muted",
  session_start: "text-accent",
  subtask_start: "text-warning",
};

// Rewrite raw MCP tool names ("mcp__okuro__write_memory") into a more
// scannable shorthand ("okuro/write-memory"). Returns the original name
// unchanged when the prefix isn't present. Also tags okuro calls so the
// timeline can color them brand green.
function formatToolName(raw: string): { display: string; isOkuro: boolean } {
  const parts = raw.split("__");
  if (parts.length === 3 && parts[0] === "mcp") {
    const server = parts[1]!;
    const tool = parts[2]!.replace(/_/g, "-");
    return { display: `${server}/${tool}`, isOkuro: server === "okuro" };
  }
  return { display: raw, isOkuro: false };
}

const TimelineItem = memo(function TimelineItem({
  event,
  isLast,
}: {
  event: ActivityEvent;
  isLast: boolean;
}) {
  const [expanded, setExpanded] = useState(false);

  // Reviewer rows — either engine-emitted markers (review_starting /
  // review_complete) tagged on the real subtask_id, OR streamed
  // critic/scorer thinking/tool_use rows tagged with the synthetic
  // reviewer:phase{N}:* id. Both get the dim italic + REVIEW chip
  // treatment so the user instantly sees "this is the reviewer judging
  // me, not my own work".
  const isMarker =
    event.type === "review_starting" || event.type === "review_complete";
  const isReviewerStream = (event.subtask_id ?? "").startsWith("reviewer:");
  const isReviewer = isMarker || isReviewerStream;

  // Gate / retry / cap rows — each carries its own body copy derived from the
  // structured payload, so the user sees WHY a thing happened, not a bare type.
  const isSmartGate = event.type === "smart_gate_deliberation";
  const isRetry = event.type === "subtask_retry";
  const isCapped = event.type === "phase_blocked_by_review_cap";
  const isVerdict = event.type === "verdict";
  // One streamed Critic finding — the "what was flagged / why it failed" rows.
  const isFinding = event.type === "critic_finding";
  const isLoadBearing =
    isFinding && (event.severity ?? "").toLowerCase() === "load_bearing";

  // Build the body text for the structured rows. Soft, explanatory tone —
  // these are "here's what the system decided and why" rows.
  const structuredText = isSmartGate
    ? `${event.reason ?? "Routed to deliberation for a careful look."}${
        event.confidence != null
          ? ` (clarity ${(event.confidence * 100).toFixed(0)}%${
              event.band
                ? `, band ${(event.band[0] * 100).toFixed(0)}–${(
                    event.band[1] * 100
                  ).toFixed(0)}%`
                : ""
            })`
          : ""
      }`
    : isRetry
      ? `Retry ${event.retries ?? "?"} of ${event.max_retries ?? "?"}${
          event.reason ? ` — ${event.reason}` : ""
        }`
      : isCapped
        ? `Retry budget spent — ${
            event.capped_subtasks?.length
              ? `${event.capped_subtasks.join(", ")} `
              : ""
          }awaiting your override after reviewer ${event.verdict ?? "FAIL"}.`
        : isVerdict
          ? `Reviewer verdict: ${event.verdict ?? "—"}.`
          : isFinding
            ? `${event.finding_summary ?? "(no summary)"}${
                event.file
                  ? ` (${event.file}${event.line ? `:${event.line}` : ""})`
                  : ""
              }`
            : null;

  // For tool_use the label already shows the (formatted) tool name —
  // falling back to event.name in the body would just repeat it, so
  // only use preview/text for that type. Markers carry their copy in
  // `message`.
  const text = isMarker
    ? event.message ?? ""
    : structuredText != null
      ? structuredText
      : event.type === "tool_use"
        ? event.preview || event.text || ""
        : event.preview || event.text || event.name || "";
  const isLong = text.length > 200;

  const isResult = event.type === "result";
  const toolInfo =
    event.type === "tool_use" ? formatToolName(event.name ?? "") : null;

  let bulletColor: string;
  let labelColor: string;
  if (isMarker) {
    // review_starting → warning (yellow); review_complete colour-coded
    // by verdict so a glance tells PASS/FAIL/CONDITIONAL at a glance.
    if (event.type === "review_starting") {
      bulletColor = "bg-warning";
      labelColor = "text-warning";
    } else {
      const tone = verdictTone(event.verdict);
      bulletColor = tone.bullet;
      labelColor = tone.chip.split(" ").find((c) => c.startsWith("text-")) ??
        "text-warning";
    }
  } else if (isSmartGate) {
    // Informational auto-route — brand accent, not alarming.
    bulletColor = "bg-accent";
    labelColor = "text-accent";
  } else if (isRetry) {
    bulletColor = "bg-warning";
    labelColor = "text-warning";
  } else if (isCapped) {
    // ROCK-SOLID v5 P1.3 — "override needed" is a request for the user's
    // call, not a failure (evidence inventory mismatch #9: this row's own
    // label disagreed with its color). Matches the CAP verdict chip fix
    // above and the NEEDS_USER pattern.
    bulletColor = "bg-info";
    labelColor = "text-info";
  } else if (isVerdict) {
    const tone = verdictTone(event.verdict);
    bulletColor = tone.bullet;
    labelColor = tone.chip.split(" ").find((c) => c.startsWith("text-")) ??
      "text-error";
  } else if (isFinding) {
    // load-bearing (must-fix) → error red; cosmetic → muted amber.
    bulletColor = isLoadBearing ? "bg-error" : "bg-warning";
    labelColor = isLoadBearing ? "text-error" : "text-warning";
  } else if (isResult) {
    bulletColor = event.success ? "bg-success" : "bg-error";
    labelColor = event.success ? "text-success" : "text-error";
  } else if (event.type === "tool_use") {
    // okuro calls render in the brand accent; any other MCP tool renders
    // in pure white so they still read as "active work" without clashing
    // with the brand color.
    bulletColor = toolInfo?.isOkuro ? "bg-accent" : "bg-white";
    labelColor = toolInfo?.isOkuro ? "text-accent" : "text-white";
  } else {
    bulletColor = BULLET_COLOR[event.type] ?? "bg-tertiary";
    labelColor = LABEL_COLOR[event.type] ?? "text-tertiary";
  }

  const label = isMarker
    ? event.type === "review_starting"
      ? "review starting"
      : `review ${event.verdict ?? "complete"}`.toLowerCase()
    : isSmartGate
      ? "deliberation"
      : isRetry
        ? "retry"
        : isCapped
          ? "override needed"
          : isVerdict
            ? `verdict ${event.verdict ?? ""}`.trim().toLowerCase()
            : isFinding
              ? isLoadBearing
                ? "must-fix"
                : "finding"
              : event.type === "tool_use"
                ? toolInfo?.display ?? "tool"
                : event.type;

  return (
    <li
      data-testid={isReviewer ? "review-row" : undefined}
      className={cn(
        "relative pl-5",
        !isLast && "pb-3",
        // Reviewer rows render italic + slightly dimmed so they read as
        // commentary about the work, not as the work itself.
        isReviewer && "italic opacity-80",
      )}
    >
      {/* Vertical rail — not on the last row */}
      {!isLast && (
        <span
          aria-hidden="true"
          className="absolute left-[4px] top-2 bottom-0 w-px bg-border/60"
        />
      )}

      {/* Bullet */}
      <span
        aria-hidden="true"
        className={cn(
          "absolute left-0 top-1.5 h-2 w-2 rounded-full ring-2 ring-surface",
          bulletColor,
        )}
      />

      {/* Header row */}
      <div className="flex items-baseline gap-2 text-3xs">
        {isReviewer && (
          <span
            data-testid="review-row-badge"
            className="rounded border border-warning/60 px-1 py-px text-3xs uppercase tracking-wider text-warning"
          >
            REVIEW
          </span>
        )}
        <span className={cn("font-bold uppercase tracking-wider", labelColor)}>
          {label}
        </span>
        {event.role && (
          <span className="text-tertiary">{event.role}</span>
        )}
        {event.duration_ms != null && (
          <span className="text-tertiary">{event.duration_ms}ms</span>
        )}
        {event.type === "review_complete" &&
          event.load_bearing_findings != null && (
            <span className="text-tertiary">
              {event.load_bearing_findings} load-bearing
            </span>
          )}
        <span className="ml-auto font-mono text-tertiary">
          {formatTime(event.ts)}
        </span>
      </div>

      {/* Body */}
      {text && (
        <div className="mt-0.5">
          <p
            className={cn(
              "whitespace-pre-wrap break-words text-xs text-fg-muted",
              !expanded && isLong && "line-clamp-2",
            )}
          >
            {text}
          </p>
          {isLong && (
            <button
              onClick={() => setExpanded(!expanded)}
              className="text-3xs text-accent hover:underline"
            >
              {expanded ? "collapse" : "expand"}
            </button>
          )}
        </div>
      )}
    </li>
  );
});
