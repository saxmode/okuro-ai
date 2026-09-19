import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, ChevronDown, ChevronRight, Clock, Loader2, Play, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { MarkdownContent } from "@/components/ui/markdown-content";
import { cn } from "@/lib/utils";
import { formatAge } from "@/lib/format";
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
  // accent — the kinds that are the USER'S OWN WORDS, as opposed to something
  // okuro inferred. Two kinds share it because the palette has six tones for
  // nine kinds, so sharing is the design, not a collision: `success` already
  // covers continue + saved and `neutral` covers research + forgotten, and
  // `StatusBadge` carries the meaning in the uppercase label and the dot, never
  // in colour alone. Grouping by PROVENANCE is what makes the six legible.
  note: "accent",
  commitment: "accent",
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
  commitment: "Commitment",
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
  "commitment",
  "task",
  "saved",
  "signal",
  "reminder",
  "research",
  "forgotten",
];

/**
 * WHICH DISPOSITIONS HAVE TO BE CONFIRMED, and why it is exactly one.
 *
 * R5 (372ccdb2): "modal dialog for permanent deletions, two-step arm (the
 * TASKS pattern) for reversible actions, window.confirm removed everywhere."
 * Read against what each action actually writes
 * (`_PRODUCER_TRANSITION` in `okuro/sense/inbox/__init__.py`):
 *
 *   defer   — touches the producer NOT AT ALL. "A UI-level snooze; the row
 *             simply comes back later." Nothing to confirm.
 *   act     — todos -> 'doing', commitments -> 'acted'. Means "I am on it",
 *             explicitly NOT "it is finished". In-progress, not terminal.
 *   dismiss — todos -> 'dropped', signals -> 'discarded', thoughts /
 *             commitments / reminders -> 'dismissed'. TERMINAL: the row leaves
 *             the queue and `_PRODUCER_OPEN_STATES` makes a second dispose a
 *             no-op, so no click in this UI walks it back.
 *
 * So dismiss is the one action a mis-click cannot undo. It is not a DELETION
 * either — the producer row survives, only its status moves — so R5 sends it
 * to the two-step arm rather than to a modal. A modal on every dismiss would
 * cost a keystroke on each of fifty rows in a triage queue built around
 * one-click disposition; the arm costs a second click only when you actually
 * dismiss.
 *
 * ARMED PER ACTION, WHICH THE TASKS PATTERN GETS WRONG. `tasks.tsx:188` tests
 * `if (!confirming)`, so arming Stop and then clicking Del EXECUTES Del on the
 * first click. Keyed by action here, so arming one disarms and re-arms rather
 * than firing the other.
 */
const NEEDS_CONFIRM: ReadonlySet<InboxAction> = new Set<InboxAction>(["dismiss"]);

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

  // R5 two-step arm — see NEEDS_CONFIRM above. Null = nothing armed.
  const [armed, setArmed] = useState<InboxAction | null>(null);
  const request = (action: InboxAction) => {
    if (NEEDS_CONFIRM.has(action) && armed !== action) {
      setArmed(action);
      return;
    }
    setArmed(null);
    onDispose(action);
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
            ? "grid-cols-[auto_auto_1fr_auto_auto] px-3 py-1.5"
            : "grid-cols-[auto_auto_1fr_auto_auto_auto] px-3 py-2",
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
          {/* S3 — THE BADGES YIELD BEFORE THE TITLE DOES.
              Measured at 1366x1024, kit `standard`, pane 728: the title cell
              is 361.8px and ONE badge was taking 102-164px of it, because both
              badges were `shrink-0` and the title was the only thing that could
              give. So 48 of 50 titles clipped while a project slug rendered in
              full. Now they truncate and carry the full value in `title=`, and
              the ORDER of yielding matches the order of the question a triage
              queue answers: what is this (the title) before which project.
              `18ch`/`12ch` are local constants — the engine names no badge
              width (it ships --field-* for form controls and nothing else),
              documented per D1.

              THE TRUNCATION NEEDS AN INNER SPAN AND `justify-start`, and the
              screenshot is what caught it. `Badge`'s base class carries
              `justify-center` and `overflow-hidden` (badge.tsx:8), so a
              `truncate` ON the badge clipped the string and CENTRED the
              overflow: "meridian-review" rendered as "eridian-revie" — the
              middle of the word, no ellipsis at either end, because the
              ellipsis applies to a block and the overflowing text was an
              anonymous flex item. The text gets its own truncating span, and
              the badge stops centring it. `min-w-0` twice on purpose: a flex
              item defaults to `min-width:auto` and will not shrink below its
              content without it. */}
          {/* THE COMPACT VARIANT SHOWS NEITHER BADGE, and the screenshot is
              why. Measured at 1366x1024, kit `standard`: the NOW strip is
              426px of a 728px pane (`lg:col-span-3` of five), its title cell
              125.5px, and ONE badge took 72-108px of that — so the title span
              got 8px on the first row and 76px on the rest, against 340-820px
              of text. Row one rendered as the single letter "P".
              `compact` already drops the salience bar for the same reason: a
              glance surface spends its width on the thing you are glancing at.
              Both values stay reachable — the full /start/inbox row shows
              them, and the row's own `title` attribute carries the text. */}
          {!compact && item.project && (
            <Badge
              variant="outline"
              className="min-w-0 max-w-[18ch] justify-start text-3xs"
              title={item.project}
            >
              <span className="min-w-0 truncate">{item.project}</span>
            </Badge>
          )}
          {/* Topic only while collapsed — the subject is the one tag worth
              scanning at a glance. The rest (who, where from, why) is context
              and stays behind the expand, per progressive disclosure. */}
          {!compact && item.tags?.topic && (
            <Badge
              variant="secondary"
              className="min-w-0 max-w-[12ch] justify-start text-3xs"
              title={item.tags.topic}
            >
              <span className="min-w-0 truncate">{item.tags.topic}</span>
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
            /* R3 / S3 — THE SALIENCE BAR IS PANE-AWARE, not window-aware.
               It costs a 108.1px column (bar 64 + number 24 + gap), measured,
               and it is the least load-bearing thing in the row: a relative
               rank within one kind, which the row order already expresses. So
               it yields to the title while the pane is narrow.

               `@4xl:` is the PANE query the shell already provides — `.pane`
               is a query container named `pane` (shell.css:~960) and Tailwind
               v4's `@` variants resolve against the nearest one. `--container-4xl`
               is 112rem = 896px under the engine's 8px root, which sits exactly
               between the two states that matter: pane 728 at 1366 with the
               panel open (bar hidden, title wins) and pane 946/990/1262 when
               the user gives the page room (bar returns). A `lg:` here would be
               true in every one of those states — the window cannot tell a
               728px column from a 1262px one. */
            className="hidden items-center gap-1.5 @4xl:flex"
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

        {/* S5 — AGE, ON A QUEUE THAT HAD NO CLOCK ON IT ANYWHERE.
            The payload carries `age_anchor_at`, `created_at` and `surfaced_at`
            (measured: all three non-null on all 60 surfaced rows) and the page
            rendered none of them, while `GRAVITY` decays the salience by age on
            every reduce pass. Two rows could draw the same bar and be a week
            apart, so the score's main input was the one thing you could not
            see.

            `age_anchor_at` AND NOT `created_at`, because that is the field the
            SCORER measures from — showing a different clock than the ranking
            uses would explain the order wrongly. It falls back to `created_at`
            only when the anchor is absent.

            ~36px at `text-3xs`, and it does not yield with the pane: 47 of 60
            live titles are longer than any single line this pane can hold
            (median 78 chars, p75 180, max 1303 against ~59 that fit), so a
            fixed 36px changes nothing about the clip and answers the question
            the bar cannot. */}
        <span
          className="whitespace-nowrap text-right text-3xs tabular-nums text-fg-muted"
          title={`Ranked from ${item.age_anchor_at ?? item.created_at}`}
        >
          {formatAge(item.age_anchor_at ?? item.created_at)}
        </span>

        <div className="flex items-center gap-1">
          <IconAction
            label="Act — dispatch now"
            onClick={() => request("act")}
            disabled={disabled}
            tone="success"
          >
            <Play className="h-3.5 w-3.5" aria-hidden="true" />
          </IconAction>
          <IconAction
            label="Defer"
            onClick={() => request("defer")}
            disabled={disabled}
            tone="neutral"
          >
            <Clock className="h-3.5 w-3.5" aria-hidden="true" />
          </IconAction>
          <IconAction
            label={
              armed === "dismiss"
                ? "Confirm dismiss — this removes the item for good"
                : "Dismiss"
            }
            onClick={() => request("dismiss")}
            disabled={disabled}
            tone="error"
            armed={armed === "dismiss"}
          >
            {armed === "dismiss" ? (
              <Check className="h-3.5 w-3.5" aria-hidden="true" />
            ) : (
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            )}
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
          <dt className="case-label text-3xs font-bold tracking-wider text-tertiary">
            Why it&apos;s here
          </dt>
          <dd className="mt-0.5 text-fg-muted">{tags.rationale}</dd>
        </div>
      )}
      {from && (
        <div>
          <dt className="case-label text-3xs font-bold tracking-wider text-tertiary">From</dt>
          <dd className="mt-0.5 text-fg-muted">{from}</dd>
        </div>
      )}
      {entities.length > 0 && (
        <div>
          <dt className="case-label text-3xs font-bold tracking-wider text-tertiary">About</dt>
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
  /**
   * R5 — this action is armed and the next click commits it. Renders filled
   * rather than dimmed, so the state is visible without colour alone: the
   * glyph swaps to a check AND the accessible name says what will happen.
   */
  armed?: boolean;
}

export function IconAction({
  label,
  tone,
  disabled,
  onClick,
  children,
  armed = false,
}: IconActionProps) {
  const toneClass = armed
    ? "bg-error text-inverse"
    : tone === "success"
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
        // `disabled:text-fg-disabled` rather than `disabled:opacity-40`:
        // 6ba4d789 — an ink tier is a NAME, never an opacity. The two resolved
        // to the same pixel by coincidence (`--color-foreground-disabled` is
        // the engine's rung 40), and a coincidence is not a token. Opacity also
        // dimmed the whole button including its focus ring; the name dims only
        // the ink. `!` beats the tone class, which also sets a text colour.
        "rounded p-1 transition-colors disabled:cursor-not-allowed disabled:!text-fg-disabled",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
        toneClass,
      )}
    >
      {children}
    </button>
  );
}
