import { useMemo } from "react";
import {
  Timeline,
  TimelineEntry,
  TimelineBadges,
  TimelineBadge,
  TimelineTitle,
  TimelineBody,
} from "@/components/ui/timeline";
import { parseApiDate } from "@/lib/format";
import type { KnowledgeNode, KnowledgeNodeType } from "@/types/knowledge";

/**
 * Timeline lens for /knowledge — the "when it happened" axis.
 *
 * Nodes are sorted newest-first by updated_at (falling back to
 * created_at) and grouped into day buckets so scanning reads as a
 * reverse-chronological journal. Reuses the shared <Timeline> primitive
 * (same one /brain uses) so the click→DetailDrawer path stays identical
 * to Gallery/Table. Nodes with no timestamp collapse into a trailing
 * "Undated" bucket rather than being dropped.
 */

const TYPE_LABEL: Record<KnowledgeNodeType, string> = {
  memory: "MEM",
  thought: "THG",
  artifact: "ART",
  progress: "PRG",
  kg_entity: "KGE",
};

// Dot tone per node type — mirrors the Gallery/Table colour language
// (memory = accent, progress = success, kg = muted).
const TYPE_TONE: Record<KnowledgeNodeType, "default" | "accent" | "muted" | "success"> = {
  memory: "accent",
  thought: "default",
  artifact: "default",
  progress: "success",
  kg_entity: "muted",
};

const DAY_FMT = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  month: "short",
  day: "numeric",
  year: "numeric",
});

function nodeTs(n: KnowledgeNode): string | null {
  return n.updated_at ?? n.created_at ?? null;
}

interface DayBucket {
  key: string;
  label: string;
  nodes: KnowledgeNode[];
}

function bucketByDay(nodes: KnowledgeNode[]): { buckets: DayBucket[]; undated: number } {
  const dated = nodes.filter((n) => nodeTs(n));
  const undated = nodes.length - dated.length;

  // Newest first.
  dated.sort((a, b) => parseApiDate(nodeTs(b)!).getTime() - parseApiDate(nodeTs(a)!).getTime());

  const buckets: DayBucket[] = [];
  const index = new Map<string, DayBucket>();
  for (const n of dated) {
    const d = parseApiDate(nodeTs(n)!);
    const key = d.toDateString();
    let bucket = index.get(key);
    if (!bucket) {
      bucket = { key, label: DAY_FMT.format(d), nodes: [] };
      index.set(key, bucket);
      buckets.push(bucket);
    }
    bucket.nodes.push(n);
  }

  return { buckets, undated };
}

export function TimelineView({
  nodes,
  onSelect,
  selectedId,
}: {
  nodes: KnowledgeNode[];
  onSelect: (id: string) => void;
  selectedId: string | null;
}) {
  const { buckets, undated } = useMemo(() => bucketByDay(nodes), [nodes]);

  return (
    <div className="space-y-8">
      {buckets.map((bucket) => (
        <section key={bucket.key}>
          <div className="mb-3 flex items-baseline gap-2">
            <h3 className="text-xs font-medium uppercase tracking-wider text-fg-muted">
              {bucket.label}
            </h3>
            <span className="text-2xs uppercase tracking-wider text-tertiary">
              {bucket.nodes.length} item{bucket.nodes.length === 1 ? "" : "s"}
            </span>
          </div>

          <Timeline>
            {bucket.nodes.map((node) => {
              const ts = nodeTs(node);
              const selected = node.id === selectedId;
              return (
                <TimelineEntry
                  key={node.id}
                  timestamp={ts ?? ""}
                  tone={TYPE_TONE[node.type]}
                  onClick={() => onSelect(node.id)}
                >
                  <TimelineTitle muted={!selected}>
                    {node.topic ? `[${node.topic}] ` : ""}
                    {node.label}
                  </TimelineTitle>
                  {node.body_preview && (
                    <TimelineBody clamp={2}>{node.body_preview}</TimelineBody>
                  )}
                  <TimelineBadges>
                    <TimelineBadge tone={node.type === "memory" ? "accent" : "default"}>
                      {TYPE_LABEL[node.type]}
                    </TimelineBadge>
                    {node.project && <TimelineBadge tone="muted">{node.project}</TimelineBadge>}
                    {node.status && <TimelineBadge tone="muted">{node.status}</TimelineBadge>}
                  </TimelineBadges>
                </TimelineEntry>
              );
            })}
          </Timeline>
        </section>
      ))}

      {undated > 0 && (
        <p className="text-2xs uppercase tracking-wider text-tertiary">
          {undated} undated item{undated === 1 ? "" : "s"} hidden — see Gallery or Table
        </p>
      )}
    </div>
  );
}
