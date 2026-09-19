import { StatusBadge } from "@/components/ui/status-badge";
import { EmptyState } from "@/components/ui/empty-state";

/**
 * The vocabulary the four Models tabs share.
 *
 * One file rather than four copies, because the tiers, the relation words and
 * the speed classes are the SAME vocabulary the backend uses — a tab that
 * coloured PROTECTED differently from another tab would be telling the reader
 * that they mean different things.
 */

type Tone = "success" | "warning" | "error" | "info" | "neutral" | "accent";

/** PROTECTED is policy, not liveness: it holds while the service is stopped. */
export const TIER_TONE: Record<string, Tone> = {
  PROTECTED: "error",
  ACTIVE: "info",
  ARCHIVE: "neutral",
};

export const PLACEMENT_TONE: Record<string, Tone> = {
  fast: "success",
  usable: "info",
  slow: "warning",
};

export const STATE_TONE: Record<string, Tone> = {
  proposed: "neutral",
  testing: "warning",
  applied: "success",
  rejected: "neutral",
};

/** A relation is a claim about a LINE of models, so each one gets its own tone. */
export const RELATION_TONE: Record<string, Tone> = {
  "same-family-newer": "success",
  "same-family-same-version-other-quant": "info",
  "same-family-older": "neutral",
  "variant-of": "accent",
  identical: "warning",
  unrelated: "neutral",
};

export function TierBadge({ tier }: { tier?: string | null }) {
  if (!tier) return null;
  return (
    <StatusBadge
      tone={TIER_TONE[tier] ?? "neutral"}
      label={tier}
      showDot={tier === "PROTECTED"}
    />
  );
}

/**
 * A NULL relation is "the pass has not judged this row", which is a different
 * fact from `unrelated` — the rule migration 152 set and the reason this
 * renders an em-dash with a title rather than the word "unrelated".
 */
export function RelationBadge({
  relation,
  target,
}: {
  relation?: string | null;
  target?: string | null;
}) {
  if (!relation)
    return (
      <span className="text-xs text-fg-subtle" title="Not judged — NOT the same as unrelated.">
        —
      </span>
    );
  const short = (target ?? "").split(":").pop() ?? "";
  return (
    <span className="inline-flex flex-col gap-0.5">
      <StatusBadge tone={RELATION_TONE[relation] ?? "neutral"} label={relation} />
      {short && <span className="text-3xs text-fg-subtle">{short}</span>}
    </span>
  );
}

/**
 * R7 (372ccdb2) — A CAPPED LIST SHOWS THE TRUE TOTAL, NEVER THE CAP, AND AN
 * UNKNOWN TOTAL RENDERS AS A DASH.
 *
 * Main's four tabs each fetch with a `limit` and none of the endpoints returns
 * a total, so a response that comes back at exactly the ceiling is ambiguous:
 * it may be complete or it may be truncated. The honest reading of an
 * ambiguous number is that it is unknown, which is what `null` means here.
 *
 * ONE FUNCTION RATHER THAN FOUR COPIES, because this is the same rule the
 * MODELS leaf pass already applied to the discoveries label — and a rule
 * re-implemented per tab is a rule that will disagree with itself.
 */
export function fetchedTotal(rows: number, limit: number): number | null {
  return rows < limit ? rows : null;
}

/**
 * The clause a list adds when it RENDERS fewer rows than it holds.
 *
 * The render cap is a separate fact from the fetch cap and both can bite at
 * once: a table can hold 1,200 units, have fetched at a 2,000 ceiling, and
 * paint 400. Saying the total and silently painting a slice is the same defect
 * R7 names, one layer down — the number on screen has to describe what is on
 * screen.
 *
 * `available` is what the client holds AFTER filtering, not the fetched total,
 * because the cap bites on the filtered list. A filter that narrows 2,000 rows
 * to 12 paints all 12 and this clause correctly says nothing.
 */
export function renderCapNote(available: number, cap: number): string {
  return available > cap ? ` · showing the first ${cap}` : "";
}

export function NotConfigured({ reason }: { reason?: string }) {
  return (
    <EmptyState
      title="Not configured on this host"
      description={
        reason ??
        "This feature reads host-specific paths from ~/.okuro/config.yaml and none are declared."
      }
    />
  );
}

/** A timestamp as elapsed time. "never" is its own answer, not an empty cell. */
export function Since({ iso }: { iso?: string | null }) {
  if (!iso) return <span className="text-fg-subtle">never</span>;
  const then = new Date(iso.includes("T") ? iso : iso.replace(" ", "T") + "Z");
  const secs = Math.max(0, (Date.now() - then.getTime()) / 1000);
  const label =
    secs < 90
      ? "just now"
      : secs < 5400
        ? `${Math.round(secs / 60)} min ago`
        : secs < 172800
          ? `${Math.round(secs / 3600)} h ago`
          : `${Math.round(secs / 86400)} d ago`;
  return <span title={iso}>{label}</span>;
}

/** A unified diff, rendered so + and − are readable without color alone. */
export function DiffBlock({ diff }: { diff: string }) {
  return (
    /* D1 — `leading-relaxed` WAS NOT AN okuro-ds NAME. globals.css bridges 34
       `--text-*` names to engine names and ZERO `--leading-*`, so the utility
       resolved Tailwind's own default theme (1.625) and the kit had no say in
       the number — the defect closed in `note-editor.tsx` this week, ruled a
       real D1 violation rather than an allow-list case. `--type-body-line`
       (1.6) is the nearest emitted value and the right role for stacked lines
       of text. No fallback: a fallback is what let the last one hide.
       Filed for the class: okuro-design-systems todo f99cece9, 60 sites. */
    <pre
      className="overflow-x-auto rounded bg-surface-subtle p-2 text-3xs font-mono"
      style={{ lineHeight: "var(--type-body-line)" }}
    >
      {diff.split("\n").map((line, i) => (
        <div
          key={i}
          className={
            line.startsWith("+")
              ? "text-success"
              : line.startsWith("-")
                ? "text-error"
                : line.startsWith("@@")
                  ? "text-info"
                  : "text-fg-subtle"
          }
        >
          {line || " "}
        </div>
      ))}
    </pre>
  );
}
