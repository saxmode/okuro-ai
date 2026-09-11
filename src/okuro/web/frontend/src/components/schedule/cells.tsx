import { cn } from "@/lib/utils";

// Shared table cells for the three Scheduled panels (recurring runs, daemon
// jobs, system timers). They exist so the panels read as ONE table rather
// than three that happen to sit on the same page: same header treatment,
// same identity column, same schedule column.
//
// Two rules the panels used to break:
//   1. Identifiers never wrap. A job id / unit name is an identifier, not
//      prose — it truncates with an ellipsis and keeps its full value in the
//      native tooltip.
//   2. A schedule leads with what it MEANS ("Mondays at 04:30"), with the
//      raw expression kept underneath so it stays inspectable and editable.

/** Header-cell classes — one geometry for every panel's <th>. */
export const TH = "px-3 py-2 font-medium whitespace-nowrap";

/** Header-row classes — one geometry for every panel's header <tr>. */
export const THEAD_ROW =
  "text-left text-2xs uppercase tracking-wider text-tertiary";

/**
 * Identity column: a non-wrapping identifier with an optional secondary
 * line (description, title, owning role). Both lines truncate; the full
 * text stays reachable via the native tooltip.
 */
export function IdentityCell({
  id,
  sub,
  mono = true,
  trailing,
  className,
}: {
  id: string;
  sub?: string | null;
  /** Render the primary line in the mono face (default — it's an id). */
  mono?: boolean;
  /** Badge or icon pinned to the right of the primary line. */
  trailing?: React.ReactNode;
  className?: string;
}) {
  // `w-full max-w-0` makes this the one column that absorbs the table's
  // slack while still being shrinkable — which is what lets the children's
  // `truncate` resolve against the real available width instead of a
  // guessed ch cap. Every other column carries `whitespace-nowrap` and so
  // takes its natural width.
  return (
    <td className={cn("w-full max-w-0 px-3 py-2 align-top", className)}>
      <div className="flex items-center gap-1.5">
        <span
          className={cn("truncate whitespace-nowrap text-fg", mono && "font-mono")}
          title={id}
        >
          {id}
        </span>
        {trailing}
      </div>
      {sub ? (
        <div
          className="truncate whitespace-nowrap text-2xs text-tertiary"
          title={sub}
        >
          {sub}
        </div>
      ) : null}
    </td>
  );
}

/**
 * Schedule column: human rendering first, raw expression second.
 *
 * `humanize` is injected so cron panels pass `humanizeCron` and the systemd
 * panel passes `humanizeSystemdCalendar` — same cell, same geometry, one
 * grammar per layer. When the humanizer can't parse the expression it
 * returns it unchanged; the raw line is then dropped so the value is not
 * printed twice.
 */
export function ScheduleCell({
  expr,
  humanize,
  className,
}: {
  expr: string | null;
  humanize: (expr: string) => string;
  className?: string;
}) {
  if (!expr) {
    return <td className={cn("px-3 py-2 text-tertiary", className)}>—</td>;
  }
  const human = humanize(expr);
  const understood = human !== expr;
  // A successful humanisation REPLACES the expression — it does not annotate
  // it. Showing both put the technical form back in front of every reader to
  // serve the rare one who wanted it, which is the opposite of the point.
  // The raw expression stays on the `title` for anyone who does, and the edit
  // control still reads and writes cron directly.
  return (
    <td className={cn("px-3 py-2 align-top", className)} title={expr}>
      <div className="whitespace-nowrap text-fg-muted">
        {understood ? human : <span className="font-mono">{expr}</span>}
      </div>
    </td>
  );
}
