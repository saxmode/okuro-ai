import { useState } from "react";
import { formatAge, displayAgent, parseApiDate } from "@/lib/format";
import {
  DetailModal,
  DetailFields,
  DetailField,
  DetailSection,
} from "@/components/ui/detail-modal";
import {
  Timeline,
  TimelineEntry,
  TimelineBadges,
  TimelineBadge,
  TimelineTitle,
} from "@/components/ui/timeline";

interface Session {
  session_id: string;
  provider: string;
  task_hint: string;
  project: string;
  started_at: string;
  ended_at: string | null;
  compliance_normalized: number | null;
}

type PanelVariant = "compact" | "timeline";

export function SessionsPanel({
  sessions,
  variant = "compact",
}: {
  sessions: Session[];
  variant?: PanelVariant;
}) {
  const [active, setActive] = useState<Session | null>(null);

  if (!sessions.length) {
    return <p className="text-2xs text-tertiary">No sessions</p>;
  }

  return (
    <>
      {variant === "timeline" ? (
        <Timeline>
          {sessions.map((s) => (
            <TimelineEntry
              key={s.session_id}
              timestamp={s.started_at}
              tone={s.ended_at ? "muted" : "success"}
              onClick={() => setActive(s)}
            >
              <TimelineBadges>
                <TimelineBadge>{displayAgent(s.provider)}</TimelineBadge>
                {s.project && <TimelineBadge tone="muted">{s.project}</TimelineBadge>}
                {s.compliance_normalized != null && (
                  <TimelineBadge tone="muted">
                    {Math.round(s.compliance_normalized * 100)}% compliance
                  </TimelineBadge>
                )}
              </TimelineBadges>
              <TimelineTitle>{s.task_hint || s.project || "—"}</TimelineTitle>
            </TimelineEntry>
          ))}
        </Timeline>
      ) : (
        <div className="space-y-1">
          {sessions.map((s) => (
            <button
              key={s.session_id}
              type="button"
              onClick={() => setActive(s)}
              className="flex w-full items-center gap-3 border-b border-border-subtle px-1 py-2 text-left text-xs transition-fast last:border-b-0 hover:bg-surface-elevated/60"
            >
              <span
                className={`h-1.5 w-1.5 shrink-0 rounded-full ${s.ended_at ? "bg-fg-subtle" : "bg-success"}`}
              />
              <span
                className="w-32 shrink-0 truncate text-fg-muted"
                title={s.provider}
              >
                {displayAgent(s.provider)}
              </span>
              <span className="flex-1 truncate text-fg-subtle">
                {s.task_hint || s.project || "—"}
              </span>
              {s.compliance_normalized != null && (
                <span className="shrink-0 text-fg-subtle tabular-nums">
                  {Math.round(s.compliance_normalized * 100)}%
                </span>
              )}
              <span className="shrink-0 text-fg-subtle">
                {formatAge(s.started_at)}
              </span>
            </button>
          ))}
        </div>
      )}

      <DetailModal
        open={!!active}
        onOpenChange={(o) => !o && setActive(null)}
        eyebrow="Session"
        title={active ? displayAgent(active.provider) : ""}
        subtitle={active?.task_hint || active?.project || undefined}
      >
        {active && (
          <DetailSection title="Details">
            <DetailFields>
              <DetailField label="ID" mono>
                {active.session_id}
              </DetailField>
              <DetailField label="Provider">
                {displayAgent(active.provider)}
              </DetailField>
              {active.project && (
                <DetailField label="Project">{active.project}</DetailField>
              )}
              {active.task_hint && (
                <DetailField label="Hint">{active.task_hint}</DetailField>
              )}
              <DetailField label="Status">
                <span
                  className={
                    active.ended_at
                      ? "text-tertiary"
                      : "text-success font-semibold"
                  }
                >
                  {active.ended_at ? "ended" : "live"}
                </span>
              </DetailField>
              <DetailField label="Started">
                {parseApiDate(active.started_at).toLocaleString()}
                <span className="ml-2 text-tertiary">
                  · {formatAge(active.started_at)}
                </span>
              </DetailField>
              {active.ended_at && (
                <DetailField label="Ended">
                  {parseApiDate(active.ended_at).toLocaleString()}
                  <span className="ml-2 text-tertiary">
                    · {formatAge(active.ended_at)}
                  </span>
                </DetailField>
              )}
              {active.compliance_normalized != null && (
                <DetailField label="Compliance">
                  <span className="tabular-nums">
                    {Math.round(active.compliance_normalized * 100)}%
                  </span>
                </DetailField>
              )}
            </DetailFields>
          </DetailSection>
        )}
      </DetailModal>
    </>
  );
}
