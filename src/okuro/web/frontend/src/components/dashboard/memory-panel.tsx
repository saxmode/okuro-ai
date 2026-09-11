import { useState } from "react";
import { formatAge, memoryTopicLabel, parseApiDate } from "@/lib/format";
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
  TimelineBody,
} from "@/components/ui/timeline";

interface Memory {
  topic: string;
  content: string;
  project: string;
  confidence: number;
  source_agent: string;
  created_at: string;
}

type PanelVariant = "compact" | "timeline";

export function MemoryPanel({
  memories,
  variant = "compact",
}: {
  memories: Memory[];
  variant?: PanelVariant;
}) {
  const [active, setActive] = useState<Memory | null>(null);

  if (!memories.length) {
    return <p className="text-xs text-fg-subtle">No memories</p>;
  }

  return (
    <>
      {variant === "timeline" ? (
        <Timeline>
          {memories.map((m, i) => (
            <TimelineEntry
              key={i}
              timestamp={m.created_at}
              tone="accent"
              onClick={() => setActive(m)}
            >
              <TimelineBadges>
                <TimelineBadge tone="accent">{memoryTopicLabel(m.topic)}</TimelineBadge>
                {m.project && <TimelineBadge tone="muted">{m.project}</TimelineBadge>}
                {m.source_agent && (
                  <TimelineBadge tone="muted">{m.source_agent}</TimelineBadge>
                )}
              </TimelineBadges>
              <TimelineBody clamp={3}>{m.content}</TimelineBody>
            </TimelineEntry>
          ))}
        </Timeline>
      ) : (
        <div className="space-y-0">
          {memories.map((m, i) => (
            <button
              key={i}
              type="button"
              onClick={() => setActive(m)}
              className="block w-full border-b border-border-subtle px-1 py-2.5 text-left text-xs transition-fast last:border-b-0 hover:bg-surface-elevated/60"
            >
              <div className="flex items-center gap-3">
                <span
                  className="shrink-0 font-medium text-fg-muted"
                  title={m.topic}
                >
                  {memoryTopicLabel(m.topic)}
                </span>
                {m.project && (
                  <span className="shrink-0 text-fg-subtle">{m.project}</span>
                )}
                <span className="ml-auto shrink-0 text-fg-subtle">
                  {formatAge(m.created_at)}
                </span>
              </div>
              <p className="mt-1 truncate text-fg-subtle">{m.content}</p>
            </button>
          ))}
        </div>
      )}

      <DetailModal
        open={!!active}
        onOpenChange={(o) => !o && setActive(null)}
        eyebrow="Memory"
        title={active ? memoryTopicLabel(active.topic) : ""}
        subtitle={active?.project || undefined}
      >
        {active && (
          <>
            <DetailSection title="Details">
              <DetailFields>
                <DetailField label="Topic">{active.topic}</DetailField>
                {active.project && (
                  <DetailField label="Project">{active.project}</DetailField>
                )}
                {active.source_agent && (
                  <DetailField label="Source">{active.source_agent}</DetailField>
                )}
                {typeof active.confidence === "number" && (
                  <DetailField label="Confidence">
                    <span className="tabular-nums">
                      {(active.confidence * 100).toFixed(0)}%
                    </span>
                  </DetailField>
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
          </>
        )}
      </DetailModal>
    </>
  );
}
