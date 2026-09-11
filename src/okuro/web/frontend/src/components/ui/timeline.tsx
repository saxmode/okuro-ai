import * as React from "react";
import { cn } from "@/lib/utils";
import { formatAge, parseApiDate } from "@/lib/format";

/**
 * Vertical timeline primitive used by /brain tab content.
 *
 * The panel components render compactly (one-line, dense) in the sidebar
 * and switch to Timeline on /brain, so the same data flows through one
 * click→DetailModal path without duplicating state. Compose via:
 *
 *   <Timeline>
 *     {items.map(i => (
 *       <TimelineEntry timestamp={i.ts} onClick={...}>
 *         <TimelineBadges>{chips}</TimelineBadges>
 *         <TimelineTitle>{i.title}</TimelineTitle>
 *         <TimelineBody>{i.body}</TimelineBody>
 *       </TimelineEntry>
 *     ))}
 *   </Timeline>
 */

/**
 * THE TRACK'S OWN NUMBERS, declared once, read by both the track and the marker.
 *
 * The marker used to be positioned at a hardcoded `-left-[37px]` against a line
 * whose position is `border-left + padding-left`. Nothing linked the two numbers,
 * so they silently disagreed by 2.5px at every rung — and no test could catch it,
 * because there was no shared expression to assert. Measured on the deployed
 * page: line centre 29.5, dot centre 32.0.
 *
 * The generalizable rule, and the reason this is three custom properties instead
 * of a corrected literal: an offset that must CENTRE on a track has to be an
 * expression over the track's own numbers. `-39.5px` would be right today and
 * wrong the moment the padding changes; `calc(-1 * (pad + border/2 + dot/2))` is
 * right by construction.
 *
 * THE UNIT IS `--spacing`, NOT `rem`, and getting that wrong once is instructive:
 * `pl-8` and `h-3.5` compile to `calc(var(--spacing) * N)`, and `--spacing` is
 * bound to the profile's grid unit (globals.css:272), not to the root font size.
 * Writing the same lengths as `2rem` / `0.875rem` halved them — the dot stayed
 * centred, because both sides derived from one number, and the whole track moved.
 * Deriving from `--spacing` also means the track follows the rung for free.
 */
const TRACK: React.CSSProperties = {
  "--tl-border": "1px",
  "--tl-pad": "calc(var(--spacing) * 8)",
  "--tl-dot": "calc(var(--spacing) * 3.5)",
} as React.CSSProperties;

/** Centres a `--tl-dot`-wide marker on the track's line. See `TRACK`. */
const DOT_LEFT = "calc(-1 * (var(--tl-pad) + var(--tl-border) / 2 + var(--tl-dot) / 2))";

export function Timeline({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <ol
      style={{
        ...TRACK,
        borderLeftWidth: "var(--tl-border)",
        paddingLeft: "var(--tl-pad)",
      }}
      className={cn(
        "relative ml-2 space-y-7 border-l border-border-subtle",
        className,
      )}
    >
      {children}
    </ol>
  );
}

interface TimelineEntryProps {
  timestamp: string;
  onClick?: () => void;
  tone?: "default" | "accent" | "muted" | "success";
  children: React.ReactNode;
}

export function TimelineEntry({
  timestamp,
  onClick,
  tone = "default",
  children,
}: TimelineEntryProps) {
  const dotClass = cn(
    "absolute top-3 rounded-full border-2 border-background",
    tone === "accent"
      ? "bg-accent"
      : tone === "success"
        ? "bg-success"
        : tone === "muted"
          ? "bg-border"
          : "bg-fg-muted",
  );

  const inner = (
    <>
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-2xs leading-normal text-tertiary">
        <time dateTime={timestamp} className="tabular-nums">
          {parseApiDate(timestamp).toLocaleString()}
        </time>
        <span>·</span>
        <span>{formatAge(timestamp)}</span>
      </div>
      {children}
    </>
  );

  return (
    <li className="relative">
      <span
        className={dotClass}
        style={{ left: DOT_LEFT, width: "var(--tl-dot)", height: "var(--tl-dot)" }}
        aria-hidden
      />
      {onClick ? (
        <button
          type="button"
          onClick={onClick}
          className="block w-full rounded text-left transition-colors hover:bg-surface-elevated/40 -mx-2 px-2 py-1.5"
        >
          {inner}
        </button>
      ) : (
        <div className="py-1">{inner}</div>
      )}
    </li>
  );
}

export function TimelineBadges({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-2.5 flex flex-wrap items-center gap-2 text-2xs">
      {children}
    </div>
  );
}

export function TimelineBadge({
  tone = "default",
  children,
}: {
  tone?: "default" | "accent" | "muted";
  children: React.ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2.5 py-1 uppercase leading-none tracking-wider",
        // A branded badge surface carries the brand at full strength, not a
        // 10 % tint of it — see `facet-bar` for the measurement.
        tone === "accent"
          ? "border-accent bg-primary text-primary-foreground"
          : tone === "muted"
            ? "border-border-subtle bg-transparent text-tertiary"
            : "border-border bg-transparent text-fg-muted",
      )}
    >
      {children}
    </span>
  );
}

export function TimelineTitle({
  children,
  muted,
}: {
  children: React.ReactNode;
  muted?: boolean;
}) {
  return (
    <p
      className={cn(
        "mt-2.5 text-sm font-medium leading-normal",
        muted ? "text-fg-muted" : "text-fg",
      )}
    >
      {children}
    </p>
  );
}

export function TimelineBody({ children, clamp = 2 }: { children: React.ReactNode; clamp?: 1 | 2 | 3 }) {
  const clampClass =
    clamp === 1 ? "line-clamp-1" : clamp === 3 ? "line-clamp-3" : "line-clamp-2";
  return (
    <p className={cn("mt-2 whitespace-pre-wrap text-xs leading-relaxed text-tertiary", clampClass)}>
      {children}
    </p>
  );
}
