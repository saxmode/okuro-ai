import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { Inbox } from "lucide-react";
import { SectionLabel } from "@/components/ui/section-label";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { InboxRow } from "@/components/inbox/InboxRow";
import { useInboxDispose } from "@/components/inbox/use-inbox-dispose";
import { inboxApi } from "@/lib/inbox-api";

/**
 * NOW dashboard — "Needs you" strip.
 *
 * Surfaces the top gated inbox items inline on the home page (Phase 4),
 * replacing the legacy "What needs you" / Signals / Digest zones. Reads
 * the same gated GET /api/inbox?state=surfaced overlay the /inbox page
 * uses, and disposes through the shared useInboxDispose hook so both
 * surfaces invalidate the same ["inbox"] query key.
 *
 * Reusable: pass `project` to scope the strip to one project's surfaced
 * items (used on the task-detail page). `title` / `viewAllTo` let callers
 * relabel and re-point the header. Global usage (home) passes nothing →
 * unchanged behavior.
 */
export function NeedsYouStrip({
  project,
  title = "Needs you",
  viewAllTo = "/inbox",
}: {
  project?: string;
  title?: string;
  viewAllTo?: string;
} = {}) {
  const { data, isLoading } = useQuery({
    queryKey: project ? ["inbox", "needs-you", project] : ["inbox", "needs-you"],
    queryFn: () =>
      inboxApi.list({ state: "surfaced", project, limit: 5 }),
  });
  const items = data?.inbox ?? [];
  const dispose = useInboxDispose();

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <SectionLabel>{title}</SectionLabel>
        <Link
          to={viewAllTo}
          className="text-2xs uppercase tracking-wider text-tertiary hover:text-fg-muted"
        >
          View all
        </Link>
      </div>

      {isLoading ? (
        <LoadingSkeleton lines={5} />
      ) : items.length === 0 ? (
        <EmptyState
          icon={<Inbox className="h-10 w-10" />}
          title="Inbox clear"
          description={
            project
              ? "Nothing from this project has cleared the surfacing bar. High-value tasks, signals, and continuations land here as the system finds them."
              : "Nothing has cleared the surfacing bar. High-value tasks, signals, and continuations land here as the system finds them."
          }
        />
      ) : (
        <div className="space-y-1" role="list" aria-label="Needs you items">
          {items.map((item) => (
            <InboxRow
              key={item.id}
              item={item}
              compact
              onDispose={(action) => dispose.mutate({ id: item.id, action })}
              disabled={dispose.isPending}
            />
          ))}
        </div>
      )}
    </section>
  );
}
