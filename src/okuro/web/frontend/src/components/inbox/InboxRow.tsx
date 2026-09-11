import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Clock, Loader2, Play, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { MarkdownContent } from "@/components/ui/markdown-content";
import { cn } from "@/lib/utils";
import {
  inboxApi,
  type InboxAction,
  type InboxItem,
  type InboxKind,
  type TagBlock,
} from "@/lib/inbox-api";

/**
 * Shared inbox row — used by both the standalone /inbox page (full
 * variant, with salience bar) and the NOW dashboard "Needs you" strip
 * (compact variant, salience bar dropped).
 *
 * Extracted from pages/inbox.tsx (Phase 4) so both surfaces share one
 * row contract and the same disposition wiring.
 */

export const KIND_TONE: Record<
  InboxKind,
  "info" | "warning" | "neutral" | "error" | "success" | "accent"
> = {
  continue: "success",
  // accent — the only kind that is the user's own words. Visually distinct from
  // every derived/inferred kind at a glance.
  note: "accent",
  task: "info",
  saved: "success",
  signal: "warning",
  research: "neutral",
  reminder: "error",
  forgotten: "neutral",
};

export const KIND_LABEL: Record<InboxKind, string> = {
  continue: "Continue",
  note: "Note",
  task: "Task",
  saved: "Saved",
  signal: "Signal",
  research: "Research",
  reminder: "Reminder",
  forgotten: "Forgotten",
};

export const KIND_ORDER: InboxKind[] = [
  "continue",
  "note",
  "task",
  "saved",
  "signal",
  "reminder",
  "research",
  "forgotten",
];

export interface InboxRowProps {
  item: InboxItem;
  onDispose: (action: InboxAction) => void;
  disabled: boolean;
  /**
   * Compact variant for the NOW "Needs you" strip: drops the salience
   * bar (no inline width-% style) and tightens padding. Keeps the
   * StatusBadge + title + the 3 disposition actions.
   */
  compact?: boolean;
  /**
   * Highest salience among rows of THIS row's kind in the current list.
   * The bar fills relative to it — see the scorePct note below. Omit and the
   * bar is hidden rather than drawn against a meaningless scale.
   */
  peerMax?: number;
}

export function InboxRow({
  item,
  onDispose,
  disabled,
  compact = false,
  peerMax,
}: InboxRowProps) {
  const tone = KIND_TONE[item.kind] ?? "neutral";

  // The bar fills relative to the best row OF THE SAME KIND currently listed.
  //
  // It used to be `salience / 0.1` — a fixed global ceiling, wrong at both
  // ends. Measured 2026-07-15: real salience spans ~0.000002..0.243, five
  // orders of magnitude, so 2 rows pinned at 100% while 18 of 46 (39%) drew a
  // literal 0% bar — the UI told the user his inbox was worthless. Raising the
  // ceiling to the true max makes that worse, not better: linear cannot show
  // five decades.
  //
  // Cross-kind comparison is meaningless anyway — kind ceilings differ ~35x
  // (commitment 0.243 vs task 0.007) because TYPE_WEIGHT and GRAVITY differ,
  // so a task drawn on a global scale is always near-empty no matter how
  // urgent. Within one kind the comparison is sound, so that is what the bar
  // now claims and nothing more.
  const hasBar = typeof peerMax === "number" && peerMax > 0;
  const scorePct = hasBar
    ? Math.max(2, Math.min(100, Math.round((item.salience / peerMax!) * 100)))
    : 0;

  // Expand-to-read: lazy-fetch the source row's markdown body. The server
  // resolver handles every ref_table, so any kind can carry content.
  const [expanded, setExpanded] = useState(false);

  // Title truncation: the chevron should also appear when the title is
  // clipped, so expanding can reveal it in full. Measured against the clamped
  // element while collapsed (scrollWidth > clientWidth ⇒ clipped).
  const titleRef = useRef<HTMLSpanElement>(null);
  const [truncated, setTruncated] = useState(false);
  useEffect(() => {
    const el = titleRef.current;
    if (el && !expanded) setTruncated(el.scrollWidth > el.clientWidth + 1);
  }, [item.title, expanded]);

  // Show the expand affordance only when there's something to reveal: a body
  // (has_detail from the list payload), a tag block explaining the row, or a
  // clipped title.
  const hasTags = Boolean(
    item.tags && (item.tags.rationale || item.tags.context || item.tags.entities?.length),
  );
  const canExpand = Boolean(item.has_detail) || hasTags || truncated;

  const { data: detail, isLoading } = useQuery({
    queryKey: ["inbox-detail", item.ref_table, item.ref_id],
    queryFn: () => inboxApi.detail(item.ref_table, item.ref_id),
    enabled: expanded && Boolean(item.has_detail),
    staleTime: 60_000,
  });

  // Don't print the same sentence twice in one row. The body is worth showing
  // only when it says something the row doesn't already say:
  //   - a signal's body is its summary, which is also its title (reducer._score
  //     titles signals from summary) — identical unless a suggested_action got
  //     appended;
  //   - a note todo's body is its rationale, which the tag panel already renders
  //     under a "Why it's here" label — and the labelled one is the better read.
  // Exact-match only, so a body that merely starts with the title still renders.
  const body = detail?.body?.trim();
  const bodyIsRationale = Boolean(body && item.tags?.rationale && body === item.tags.rationale.trim());
  const bodyIsTitle = Boolean(body && body === item.title.trim());
  const bodyRedundant = bodyIsRationale || bodyIsTitle;
  const toggle = () => {
    if (canExpand) setExpanded((e) => !e);
  };

  return (
    <div
      role="listitem"
      className="rounded-md border border-border-subtle bg-surface-subtle transition-colors hover:border-border"
    >
      <div
        className={cn(
          "grid items-center gap-3",
          compact
            ? "grid-cols-[auto_auto_1fr_auto] px-3 py-1.5"
            : "grid-cols-[auto_auto_1fr_auto_auto] px-3 py-2",
        )}
      >
        <button
          type="button"
          onClick={toggle}
          aria-expanded={expanded}
          aria-label={expanded ? "Collapse" : "Expand"}
          className={cn(
            "rounded p-0.5 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
            canExpand ? "text-tertiary hover:text-fg" : "invisible",
          )}
          tabIndex={canExpand ? 0 : -1}
        >
          {expanded ? (
            <ChevronDown className="h-4 w-4" aria-hidden="true" />
          ) : (
            <ChevronRight className="h-4 w-4" aria-hidden="true" />
          )}
        </button>

        <StatusBadge tone={tone} label={KIND_LABEL[item.kind]} showDot />

        <button
          type="button"
          onClick={toggle}
          className={cn(
            "flex min-w-0 items-center gap-2 text-left",
            canExpand && "cursor-pointer",
          )}
        >
          <span
            ref={titleRef}
            className={cn("text-sm text-fg", expanded ? "whitespace-normal" : "truncate")}
            title={item.title}
          >
            {item.title}
          </span>
          {item.project && (
            <Badge variant="outline" className="shrink-0 text-3xs">
              {item.project}
            </Badge>
          )}
          {/* Topic only while collapsed — the subject is the one tag worth
              scanning at a glance. The rest (who, where from, why) is context
              and stays behind the expand, per progressive disclosure. */}
          {item.tags?.topic && (
            <Badge variant="secondary" className="shrink-0 text-3xs">
              {item.tags.topic}
            </Badge>
          )}
          {item.dup_count > 1 && (
            <Badge variant="secondary" className="shrink-0 text-3xs tabular-nums">
              ×{item.dup_count}
            </Badge>
          )}
        </button>

        {/* Grid cell is always rendered in the full variant — the column count
            is fixed by grid-cols above, so dropping it would shift the actions. */}
        {!compact && (
          <div
            className="flex items-center gap-1.5"
            title={
              hasBar
                ? `Rank within ${KIND_LABEL[item.kind] ?? item.kind} — relative to the top item of this kind`
                : undefined
            }
          >
            {hasBar && (
              <>
                <div className="h-1.5 w-16 rounded-full bg-border">
                  <div
                    className="h-full rounded-full bg-accent"
                    style={{ width: `${scorePct}%` }}
                    aria-hidden="true"
                  />
                </div>
                <span className="w-6 text-right text-3xs tabular-nums text-tertiary">
                  {scorePct}
                </span>
              </>
            )}
          </div>
        )}

        <div className="flex items-center gap-1">
          <IconAction
            label="Act — dispatch now"
            onClick={() => onDispose("act")}
            disabled={disabled}
            tone="success"
          >
            <Play className="h-3.5 w-3.5" aria-hidden="true" />
          </IconAction>
          <IconAction
            label="Defer"
            onClick={() => onDispose("defer")}
            disabled={disabled}
            tone="neutral"
          >
            <Clock className="h-3.5 w-3.5" aria-hidden="true" />
          </IconAction>
          <IconAction
            label="Dismiss"
            onClick={() => onDispose("dismiss")}
            disabled={disabled}
            tone="error"
          >
            <X className="h-3.5 w-3.5" aria-hidden="true" />
          </IconAction>
        </div>
      </div>

      {expanded && (item.has_detail || hasTags) && (
        <div className="space-y-3 border-t border-border-subtle px-3 py-3">
          {item.has_detail &&
            !bodyRedundant &&
            (isLoading ? (
              <div className="flex items-center gap-2 text-xs text-tertiary">
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                Loading…
              </div>
            ) : body ? (
              <MarkdownContent>{detail!.body!}</MarkdownContent>
            ) : (
              !hasTags && <p className="text-xs text-tertiary">No additional content.</p>
            ))}
          {hasTags && item.tags && (
            <TagDetail
              tags={item.tags}
              showDivider={Boolean(item.has_detail) && !bodyRedundant}
            />
          )}
        </div>
      )}
    </div>
  );
}

interface TagDetailProps {
  tags: TagBlock;
  /** Rule above the block, only when a markdown body precedes it. */
  showDivider: boolean;
}

/**
 * The "why is this here?" panel.
 *
 * Ordered by the question the user actually asks on opening a row they didn't
 * create: why am I looking at this (rationale), where did it come from
 * (context/note), and who does it touch (entities). Topic is deliberately
 * absent — it already renders inline on the collapsed row.
 */
function TagDetail({ tags, showDivider }: TagDetailProps) {
  const entities = tags.entities ?? [];
  const from = [tags.note_title, tags.context].filter(Boolean).join(" — ");

  return (
    <dl
      className={cn(
        "space-y-2.5 text-xs",
        showDivider && "border-t border-border-subtle pt-3",
      )}
    >
      {tags.rationale && (
        <div>
          <dt className="text-3xs font-bold uppercase tracking-wider text-tertiary">
            Why it&apos;s here
          </dt>
          <dd className="mt-0.5 text-fg-muted">{tags.rationale}</dd>
        </div>
      )}
      {from && (
        <div>
          <dt className="text-3xs font-bold uppercase tracking-wider text-tertiary">From</dt>
          <dd className="mt-0.5 text-fg-muted">{from}</dd>
        </div>
      )}
      {entities.length > 0 && (
        <div>
          <dt className="text-3xs font-bold uppercase tracking-wider text-tertiary">About</dt>
          <dd className="mt-1 flex flex-wrap gap-1">
            {entities.map((e) => (
              <Badge key={e} variant="outline" className="text-3xs">
                {e}
              </Badge>
            ))}
          </dd>
        </div>
      )}
    </dl>
  );
}

interface IconActionProps {
  label: string;
  tone: "success" | "error" | "neutral";
  disabled: boolean;
  onClick: () => void;
  children: React.ReactNode;
}

export function IconAction({ label, tone, disabled, onClick, children }: IconActionProps) {
  const toneClass =
    tone === "success"
      ? "text-tertiary hover:text-success"
      : tone === "error"
        ? "text-tertiary hover:text-error"
        : "text-tertiary hover:text-fg-muted";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className={cn(
        "rounded p-1 transition-colors disabled:cursor-not-allowed disabled:opacity-40",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
        toneClass,
      )}
    >
      {children}
    </button>
  );
}
