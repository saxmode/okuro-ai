import { useEffect, useMemo, useState } from "react";
import { Rocket } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { thoughtsApi } from "@/lib/api";
import { formatAge, parseApiDate } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { CreateDialog } from "@/components/task/create-dialog";
import {
  DetailModal,
  DetailFields,
  DetailField,
  DetailSection,
  DetailProse,
} from "@/components/ui/detail-modal";
import {
  Timeline,
  TimelineEntry,
  TimelineBadges,
  TimelineBadge,
  TimelineTitle,
  TimelineBody,
} from "@/components/ui/timeline";

interface Thought {
  id: string;
  content: string;
  status: string;
  project: string;
  created_at: string;
}

type ThoughtStatus = "open" | "in_progress" | "resolved" | "dismissed";

const STATUS_MARK: Record<string, string> = {
  open: "○",
  in_progress: "◐",
  resolved: "●",
  dismissed: "—",
};

type PanelVariant = "compact" | "timeline";

export function ThoughtsPanel({
  thoughts,
  variant = "compact",
  autoOpenId,
}: {
  thoughts: Thought[];
  variant?: PanelVariant;
  /** When provided (e.g. from `?id=` on /brain), auto-open the matching
   *  thought's detail modal on mount or when the id changes. */
  autoOpenId?: string | null;
}) {
  const [showAll, setShowAll] = useState(false);
  const [active, setActive] = useState<Thought | null>(null);
  const [launchOpen, setLaunchOpen] = useState(false);
  const qc = useQueryClient();

  useEffect(() => {
    if (!autoOpenId) return;
    const match = thoughts.find((t) => t.id === autoOpenId);
    if (match) setActive(match);
  }, [autoOpenId, thoughts]);

  const mutate = useMutation({
    mutationFn: ({ id, status }: { id: string; status: ThoughtStatus }) =>
      thoughtsApi.setStatus(id, status),
    onSuccess: (_d, vars) => {
      qc.invalidateQueries({ queryKey: ["dashboard", "brain"] });
      if (vars.status === "resolved" || vars.status === "dismissed") {
        setActive(null);
      } else {
        setActive((a) => (a ? { ...a, status: vars.status } : a));
      }
    },
  });

  const filtered = useMemo(() => {
    if (showAll) return thoughts;
    return thoughts.filter(
      (t) => t.status === "open" || t.status === "in_progress",
    );
  }, [thoughts, showAll]);

  if (!thoughts.length) {
    return <p className="text-2xs text-tertiary">No thoughts</p>;
  }

  return (
    <>
      {variant === "timeline" ? (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-3xs uppercase tracking-wider text-tertiary">
              {showAll ? "all" : "inbox"} · {filtered.length}
            </span>
            <button
              type="button"
              onClick={() => setShowAll((v) => !v)}
              className="text-3xs uppercase tracking-wider text-tertiary hover:text-fg-muted"
            >
              {showAll ? "show inbox" : "show all"}
            </button>
          </div>
          {filtered.length === 0 ? (
            <p className="text-2xs text-tertiary">Inbox clear</p>
          ) : (
            <Timeline>
              {filtered.map((t) => {
                const isActive = t.status === "in_progress";
                const isDone = t.status === "resolved" || t.status === "dismissed";
                return (
                  <TimelineEntry
                    key={t.id}
                    timestamp={t.created_at}
                    tone={isActive ? "accent" : isDone ? "muted" : "default"}
                    onClick={() => setActive(t)}
                  >
                    <TimelineBadges>
                      <TimelineBadge tone={isActive ? "accent" : "muted"}>
                        {t.status.replace(/_/g, " ")}
                      </TimelineBadge>
                      {t.project && <TimelineBadge tone="muted">{t.project}</TimelineBadge>}
                    </TimelineBadges>
                    <TimelineTitle>{t.content.split("\n")[0]}</TimelineTitle>
                    {t.content.includes("\n") && (
                      <TimelineBody>
                        {t.content.split("\n").slice(1).join(" ")}
                      </TimelineBody>
                    )}
                  </TimelineEntry>
                );
              })}
            </Timeline>
          )}
        </div>
      ) : (
        <div className="space-y-1">
          <div className="flex items-center justify-between pb-1">
            <span className="text-3xs uppercase tracking-wider text-tertiary">
              {showAll ? "all" : "inbox"} · {filtered.length}
            </span>
            <button
              type="button"
              onClick={() => setShowAll((v) => !v)}
              className="text-3xs uppercase tracking-wider text-tertiary hover:text-fg-muted"
            >
              {showAll ? "show inbox" : "show all"}
            </button>
          </div>
          {filtered.length === 0 ? (
            <p className="text-2xs text-tertiary">Inbox clear</p>
          ) : (
            filtered.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => setActive(t)}
                className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-2xs hover:bg-surface/60"
              >
                <span className="text-tertiary">
                  {STATUS_MARK[t.status] ?? "○"}
                </span>
                <span className="flex-1 truncate text-fg-muted">
                  {t.content.split("\n")[0]}
                </span>
                {t.project && (
                  <span className="shrink-0 text-tertiary">{t.project}</span>
                )}
                <span className="shrink-0 text-tertiary">
                  {formatAge(t.created_at)}
                </span>
              </button>
            ))
          )}
        </div>
      )}

      <DetailModal
        open={!!active}
        onOpenChange={(o) => !o && setActive(null)}
        eyebrow="Thought"
        title={
          active
            ? (active.content.split("\n")[0] ?? "").slice(0, 140)
            : ""
        }
        subtitle={active?.status.replace(/_/g, " ")}
      >
        {active && (
          <>
            <DetailSection title="Details">
              <DetailFields>
                <DetailField label="Status">
                  {active.status.replace(/_/g, " ")}
                </DetailField>
                {active.project && (
                  <DetailField label="Project">{active.project}</DetailField>
                )}
                <DetailField label="Created">
                  {parseApiDate(active.created_at).toLocaleString()}
                  <span className="ml-2 text-tertiary">
                    · {formatAge(active.created_at)}
                  </span>
                </DetailField>
              </DetailFields>
            </DetailSection>

            <DetailSection title="Content">
              <DetailProse>{active.content}</DetailProse>
            </DetailSection>

            <DetailSection title="Actions">
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  variant="default"
                  onClick={() => setLaunchOpen(true)}
                  title="Send this thought to the orchestrator as a new task. The thought will be resolved when the task completes."
                >
                  <Rocket className="mr-1 h-3.5 w-3.5" />
                  Process now
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={mutate.isPending || active.status === "in_progress"}
                  onClick={() =>
                    mutate.mutate({ id: active.id, status: "in_progress" })
                  }
                >
                  In progress
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={mutate.isPending}
                  onClick={() =>
                    mutate.mutate({ id: active.id, status: "resolved" })
                  }
                >
                  Resolve
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={mutate.isPending}
                  onClick={() =>
                    mutate.mutate({ id: active.id, status: "dismissed" })
                  }
                >
                  Dismiss
                </Button>
              </div>
            </DetailSection>
          </>
        )}
      </DetailModal>

      <CreateDialog
        open={launchOpen}
        onOpenChange={setLaunchOpen}
        initialDescription={active?.content}
        sourceThoughtId={active?.id}
      />
    </>
  );
}
