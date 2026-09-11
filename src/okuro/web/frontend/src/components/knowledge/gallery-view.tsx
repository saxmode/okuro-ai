/**
 * GalleryView — sortable, searchable card grid over the unified knowledge set.
 *
 * Visual encoding locked across all views (Gallery / Table / Graph):
 *   hue (left border)    = entity type
 *   opacity              = confidence (0.5 baseline + 0.5 * confidence)
 *   outline ring         = supersedes-chain head
 *
 * Sort defaults to recency (DESC). Search is client-side over label,
 * topic, project, body_preview — fast for the 200-cap page.
 */

import { useMemo, useState } from "react";
import { Search, X, ArrowDownNarrowWide, ArrowUpNarrowWide } from "lucide-react";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { nodeTypeFromId } from "@/hooks/use-knowledge";
import { useIncremental } from "@/hooks/use-incremental";
import type { KnowledgeNode, KnowledgeNodeType } from "@/types/knowledge";

type SortKey = "recency" | "confidence" | "project" | "label";

interface GalleryViewProps {
  nodes: KnowledgeNode[];
  onSelect: (id: string) => void;
  selectedId?: string | null;
}

const TYPE_LABEL: Record<KnowledgeNodeType, string> = {
  memory: "Memory",
  thought: "Thought",
  artifact: "Artifact",
  progress: "Progress",
  kg_entity: "KG entity",
};

const TYPE_BORDER: Record<KnowledgeNodeType, string> = {
  memory: "border-l-accent",
  thought: "border-l-warning",
  artifact: "border-l-info",
  progress: "border-l-tertiary",
  kg_entity: "border-l-fg-muted",
};

export function GalleryView({ nodes, onSelect, selectedId }: GalleryViewProps) {
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("recency");
  const [sortAsc, setSortAsc] = useState(false);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    let out = nodes;
    if (q) {
      out = nodes.filter((n) => {
        const hay = [n.label, n.topic, n.project, n.body_preview]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        return hay.includes(q);
      });
    }
    const sorted = [...out].sort((a, b) => {
      let av: string | number;
      let bv: string | number;
      switch (sortKey) {
        case "recency":
          av = a.updated_at ?? a.created_at ?? "";
          bv = b.updated_at ?? b.created_at ?? "";
          break;
        case "confidence":
          av = a.confidence ?? -1;
          bv = b.confidence ?? -1;
          break;
        case "project":
          av = a.project ?? "";
          bv = b.project ?? "";
          break;
        case "label":
          av = a.label ?? "";
          bv = b.label ?? "";
          break;
      }
      if (av < bv) return sortAsc ? -1 : 1;
      if (av > bv) return sortAsc ? 1 : -1;
      return 0;
    });
    return sorted;
  }, [nodes, search, sortKey, sortAsc]);

  // Render in pages — mounting all 800+ cards at once costs seconds of layout.
  const { count, sentinelRef } = useIncremental(filtered.length);

  return (
    <div className="space-y-3">
      <Controls
        search={search}
        onSearch={setSearch}
        sortKey={sortKey}
        onSortKey={setSortKey}
        sortAsc={sortAsc}
        onToggleAsc={() => setSortAsc((v) => !v)}
        count={filtered.length}
        total={nodes.length}
      />

      <div className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
        {filtered.slice(0, count).map((n) => (
          <GalleryCard
            key={n.id}
            node={n}
            selected={n.id === selectedId}
            onClick={() => onSelect(n.id)}
          />
        ))}
        {filtered.length === 0 && (
          <div className="col-span-full py-12 text-center text-2xs uppercase tracking-wider text-tertiary">
            no matches
          </div>
        )}
      </div>
      {count < filtered.length && (
        <div
          ref={sentinelRef}
          className="py-6 text-center text-2xs uppercase tracking-wider text-tertiary"
        >
          {count} / {filtered.length} — scroll for more
        </div>
      )}
    </div>
  );
}

// ── Controls ─────────────────────────────────────────────────────────

function Controls({
  search,
  onSearch,
  sortKey,
  onSortKey,
  sortAsc,
  onToggleAsc,
  count,
  total,
}: {
  search: string;
  onSearch: (v: string) => void;
  sortKey: SortKey;
  onSortKey: (k: SortKey) => void;
  sortAsc: boolean;
  onToggleAsc: () => void;
  count: number;
  total: number;
}) {
  return (
    <div className="flex items-center gap-3">
      <div className="relative w-64">
        <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-tertiary" />
        <Input
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder="Search labels, topics, content…"
          className="bg-surface pl-8 pr-8 text-sm"
        />
        {search && (
          <button
            type="button"
            onClick={() => onSearch("")}
            aria-label="Clear search"
            className="absolute right-2 top-2 text-tertiary hover:text-fg-muted"
          >
            <X className="h-4 w-4" />
          </button>
        )}
      </div>

      <div className="flex items-center gap-1 text-2xs uppercase tracking-wider text-tertiary">
        <span>sort</span>
        <select
          value={sortKey}
          onChange={(e) => onSortKey(e.target.value as SortKey)}
          className="rounded-sm border border-border-subtle bg-surface px-1.5 py-0.5 text-fg-muted"
        >
          <option value="recency">recency</option>
          <option value="confidence">confidence</option>
          <option value="project">project</option>
          <option value="label">label</option>
        </select>
        <button
          type="button"
          onClick={onToggleAsc}
          aria-label="Toggle sort direction"
          className="rounded-sm border border-border-subtle p-0.5 text-fg-muted hover:bg-surface-elevated"
        >
          {sortAsc ? (
            <ArrowUpNarrowWide className="h-3 w-3" />
          ) : (
            <ArrowDownNarrowWide className="h-3 w-3" />
          )}
        </button>
      </div>

      <div className="ml-auto text-2xs uppercase tracking-wider text-tertiary">
        <span className="font-mono text-fg-muted">{count}</span> / {total}
      </div>
    </div>
  );
}

// ── Card ─────────────────────────────────────────────────────────────

function GalleryCard({
  node,
  onClick,
  selected,
}: {
  node: KnowledgeNode;
  onClick: () => void;
  selected: boolean;
}) {
  const typ = nodeTypeFromId(node.id) ?? node.type;
  const opacity =
    node.confidence == null ? 1 : 0.55 + 0.45 * Math.max(0, Math.min(1, node.confidence));
  const chips: string[] = [];
  if (node.kind) chips.push(node.kind);
  if (node.status) chips.push(node.status);
  if (node.is_supersedes_head) chips.push("head");
  if (node.confidence != null) chips.push(`conf ${node.confidence.toFixed(2)}`);

  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "text-left border-l-2 border-y border-r bg-surface px-3 py-2.5 transition-colors",
        "hover:border-accent hover:bg-surface-elevated",
        selected ? "border-accent ring-1 ring-accent" : "border-border-subtle",
        TYPE_BORDER[typ],
        node.is_supersedes_head && "outline outline-1 outline-accent/30",
      )}
      style={{ opacity }}
    >
      <div className="flex items-center justify-between gap-2 text-2xs uppercase tracking-wider text-tertiary">
        <span>{TYPE_LABEL[typ]}</span>
        {node.project && <span className="font-mono truncate max-w-[20rem]">{node.project}</span>}
      </div>
      <div className="mt-1 text-sm font-medium text-fg line-clamp-1">
        {node.topic ? `[${node.topic}] ` : ""}
        {node.label}
      </div>
      {node.body_preview && (
        <div className="mt-1 text-xs text-fg-muted line-clamp-2">{node.body_preview}</div>
      )}
      {chips.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {chips.map((c) => (
            <span
              key={c}
              className="rounded-sm border border-border-subtle px-1 py-0 text-[10px] font-mono uppercase tracking-wider text-tertiary"
            >
              {c}
            </span>
          ))}
        </div>
      )}
    </button>
  );
}
