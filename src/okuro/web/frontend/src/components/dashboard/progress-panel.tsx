import { useState } from "react";
import { formatAge, parseApiDate } from "@/lib/format";
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

interface ProgressHistoryEntry {
  status: string;
  summary: string;
  updated_at: string;
  next_steps?: string;
  files_touched?: string[];
}

interface ProgressEntry {
  project: string;
  agent: string;
  status: string;
  summary: string;
  updated_at: string;
  next_steps?: string;
  files_touched?: string[];
  /**
   * Prior entries for this (project, agent) pair, newest-first, capped at
   * ~20 by the backend. Absent on legacy payloads — guard with `?? []`.
   */
  history?: ProgressHistoryEntry[];
}

type PanelVariant = "compact" | "timeline";

export function ProgressPanel({
  entries,
  variant = "compact",
}: {
  entries: ProgressEntry[];
  variant?: PanelVariant;
}) {
  const [active, setActive] = useState<ProgressEntry | null>(null);

  if (!entries.length) {
    return <p className="text-2xs text-tertiary">No progress entries</p>;
  }

  const statusTone = (s: string): "accent" | "success" | "muted" | "default" => {
    if (s === "blocked" || s === "waiting_for_user") return "muted";
    if (s === "testing") return "success";
    if (s === "implementing" || s === "exploring") return "accent";
    return "default";
  };

  return (
    <>
      {variant === "timeline" ? (
        <Timeline>
          {entries.map((e, i) => (
            <TimelineEntry
              key={i}
              timestamp={e.updated_at}
              tone={statusTone(e.status)}
              onClick={() => setActive(e)}
            >
              <TimelineBadges>
                <TimelineBadge tone="accent">{e.project}</TimelineBadge>
                <TimelineBadge tone="muted">
                  {e.status.replace(/_/g, " ")}
                </TimelineBadge>
                {e.agent && e.agent !== "unknown" && (
                  <TimelineBadge tone="muted">{e.agent}</TimelineBadge>
                )}
                {(e.history?.length ?? 0) > 0 && (
                  <TimelineBadge tone="muted">
                    +{e.history!.length} prior
                  </TimelineBadge>
                )}
              </TimelineBadges>
              <TimelineTitle>{e.summary || "—"}</TimelineTitle>
              {e.next_steps && (
                <TimelineBody>
                  <span className="uppercase tracking-wider text-tertiary">
                    Next:
                  </span>{" "}
                  {e.next_steps}
                </TimelineBody>
              )}
            </TimelineEntry>
          ))}
        </Timeline>
      ) : (
        <div className="space-y-1.5">
          {entries.map((e, i) => (
            <button
              key={i}
              type="button"
              onClick={() => setActive(e)}
              className="block w-full text-left text-2xs hover:bg-surface/60 rounded px-1 py-0.5"
            >
              <div className="flex items-center gap-2">
                <span className="font-medium text-fg-muted">{e.project}</span>
                <span className="ml-auto text-tertiary">
                  {formatAge(e.updated_at)}
                </span>
              </div>
              <p className="truncate text-tertiary">{e.summary}</p>
            </button>
          ))}
        </div>
      )}

      <DetailModal
        open={!!active}
        onOpenChange={(o) => !o && setActive(null)}
        eyebrow="Progress"
        title={active?.project ?? ""}
        subtitle={active?.status.replace(/_/g, " ")}
      >
        {active && (
          <>
            <DetailSection title="Details">
              <DetailFields>
                <DetailField label="Status">
                  {active.status.replace(/_/g, " ")}
                </DetailField>
                {active.agent && active.agent !== "unknown" && (
                  <DetailField label="Agent">{active.agent}</DetailField>
                )}
                <DetailField label="Updated">
                  {parseApiDate(active.updated_at).toLocaleString()}
                  <span className="ml-2 text-tertiary">
                    · {formatAge(active.updated_at)}
                  </span>
                </DetailField>
              </DetailFields>
            </DetailSection>

            <DetailSection title="Summary">
              <DetailProse>{active.summary}</DetailProse>
            </DetailSection>

            {active.next_steps && (
              <DetailSection title="Next steps">
                <DetailProse>{active.next_steps}</DetailProse>
              </DetailSection>
            )}

            {(active.history?.length ?? 0) > 0 && (
              <DetailSection
                title={`Timeline (${active.history!.length} prior ${
                  active.history!.length === 1 ? "entry" : "entries"
                })`}
              >
                <ol className="relative space-y-3 border-l border-border-subtle pl-4">
                  {active.history!.map((h, i) => (
                    <li key={i} className="relative">
                      <span className="absolute -left-[21px] top-1.5 h-2 w-2 rounded-full bg-border" />
                      <div className="flex items-baseline gap-2 text-2xs">
                        <span className="font-medium text-fg-muted">
                          {h.status.replace(/_/g, " ")}
                        </span>
                        <span className="text-tertiary">
                          {parseApiDate(h.updated_at).toLocaleString()}
                        </span>
                        <span className="text-tertiary">
                          · {formatAge(h.updated_at)}
                        </span>
                      </div>
                      <p className="mt-0.5 whitespace-pre-wrap text-xs text-fg">
                        {h.summary}
                      </p>
                      {h.next_steps && (
                        <p className="mt-1 text-2xs text-tertiary">
                          <span className="uppercase tracking-wider text-tertiary">
                            Next:
                          </span>{" "}
                          {h.next_steps}
                        </p>
                      )}
                    </li>
                  ))}
                </ol>
              </DetailSection>
            )}
          </>
        )}
      </DetailModal>
    </>
  );
}
