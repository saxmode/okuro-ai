/**
 * TableView — dense sortable rows over the unified knowledge set.
 *
 * Columns: type | label | topic | project | confidence | updated.
 * Click row → opens detail drawer (lifted via onSelect prop).
 * Sort by clicking column header. Same dataset/encoding as GalleryView.
 */

import { useMemo, useState } from "react";
import { ChevronDown, ChevronUp, Search, X } from "lucide-react";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { nodeTypeFromId } from "@/hooks/use-knowledge";
import { useIncremental } from "@/hooks/use-incremental";
import type { KnowledgeNode, KnowledgeNodeType } from "@/types/knowledge";

type Col = "type" | "label" | "topic" | "project" | "confidence" | "updated";

interface TableViewProps {
  nodes: KnowledgeNode[];
  onSelect: (id: string) => void;
  selectedId?: string | null;
}

const TYPE_LABEL: Record<KnowledgeNodeType, string> = {
  memory: "MEM",
  thought: "THG",
  artifact: "ART",
  progress: "PRG",
  kg_entity: "KGE",
};

const TYPE_CLASS: Record<KnowledgeNodeType, string> = {
  memory: "text-accent",
  thought: "text-warning",
  artifact: "text-info",
  progress: "text-tertiary",
  kg_entity: "text-fg-muted",
};

export function TableView({ nodes, onSelect, selectedId }: TableViewProps) {
  const [search, setSearch] = useState("");
  const [sortCol, setSortCol] = useState<Col>("updated");
  const [sortAsc, setSortAsc] = useState(false);

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    let out = nodes;
    if (q) {
      out = nodes.filter((n) =>
        [n.label, n.topic, n.project, n.body_preview]
          .filter(Boolean)
          .join(" ")
          .toLowerCase()
          .includes(q),
      );
    }
    const sorted = [...out].sort((a, b) => {
      const cmp = compare(a, b, sortCol);
      return sortAsc ? cmp : -cmp;
    });
    return sorted;
  }, [nodes, search, sortCol, sortAsc]);

  const clickHeader = (c: Col) => {
    if (c === sortCol) setSortAsc((v) => !v);
    else {
      setSortCol(c);
      setSortAsc(false);
    }
  };

  // Render rows in pages — 800+ rows at once is seconds of layout.
  const { count, sentinelRef } = useIncremental(rows.length);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <div className="relative w-64">
          <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-tertiary" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search…"
            className="bg-surface pl-8 pr-8 text-sm"
          />
          {search && (
            <button
              type="button"
              onClick={() => setSearch("")}
              aria-label="Clear search"
              className="absolute right-2 top-2 text-tertiary hover:text-fg-muted"
            >
              <X className="h-4 w-4" />
            </button>
          )}
        </div>
        <div className="ml-auto text-2xs uppercase tracking-wider text-tertiary">
          <span className="font-mono text-fg-muted">{rows.length}</span> / {nodes.length}
        </div>
      </div>

      <div className="overflow-x-auto rounded-sm border border-border-subtle">
        <table className="w-full text-xs">
          <thead className="bg-surface-subtle">
            <tr>
              <Th col="type" sortCol={sortCol} sortAsc={sortAsc} onClick={clickHeader}>
                type
              </Th>
              <Th col="label" sortCol={sortCol} sortAsc={sortAsc} onClick={clickHeader}>
                label
              </Th>
              <Th col="topic" sortCol={sortCol} sortAsc={sortAsc} onClick={clickHeader}>
                topic
              </Th>
              <Th col="project" sortCol={sortCol} sortAsc={sortAsc} onClick={clickHeader}>
                project
              </Th>
              <Th col="confidence" sortCol={sortCol} sortAsc={sortAsc} onClick={clickHeader}>
                conf
              </Th>
              <Th col="updated" sortCol={sortCol} sortAsc={sortAsc} onClick={clickHeader}>
                updated
              </Th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, count).map((n) => {
              const typ = nodeTypeFromId(n.id) ?? n.type;
              const opacity =
                n.confidence == null ? 1 : 0.55 + 0.45 * Math.max(0, Math.min(1, n.confidence));
              return (
                <tr
                  key={n.id}
                  onClick={() => onSelect(n.id)}
                  className={cn(
                    "cursor-pointer border-t border-border-subtle transition-colors",
                    n.id === selectedId
                      ? "bg-accent-subtle"
                      : "hover:bg-surface-elevated",
                  )}
                  style={{ opacity }}
                >
                  <td className={cn("px-2 py-1.5 font-mono font-medium", TYPE_CLASS[typ])}>
                    {TYPE_LABEL[typ]}
                  </td>
                  <td className="max-w-[48rem] truncate px-2 py-1.5 text-fg">{n.label}</td>
                  <td className="max-w-[20rem] truncate px-2 py-1.5 text-fg-muted">
                    {n.topic ?? ""}
                  </td>
                  <td className="max-w-[24rem] truncate px-2 py-1.5 font-mono text-tertiary">
                    {n.project ?? ""}
                  </td>
                  <td className="px-2 py-1.5 font-mono text-tertiary">
                    {n.confidence?.toFixed(2) ?? "—"}
                  </td>
                  <td className="px-2 py-1.5 font-mono text-tertiary">
                    {(n.updated_at ?? n.created_at ?? "").slice(0, 10)}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={6} className="py-12 text-center text-2xs uppercase tracking-wider text-tertiary">
                  no matches
                </td>
              </tr>
            )}
            {count < rows.length && (
              <tr ref={sentinelRef}>
                <td colSpan={6} className="py-4 text-center text-2xs uppercase tracking-wider text-tertiary">
                  {count} / {rows.length} — scroll for more
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Th({
  col,
  sortCol,
  sortAsc,
  onClick,
  children,
}: {
  col: Col;
  sortCol: Col;
  sortAsc: boolean;
  onClick: (c: Col) => void;
  children: React.ReactNode;
}) {
  const active = col === sortCol;
  const Chev = sortAsc ? ChevronUp : ChevronDown;
  return (
    <th
      onClick={() => onClick(col)}
      className={cn(
        "cursor-pointer select-none px-2 py-1.5 text-left text-2xs uppercase tracking-wider",
        active ? "text-accent" : "text-tertiary hover:text-fg-muted",
      )}
    >
      <span className="inline-flex items-center gap-0.5">
        {children}
        {active && <Chev className="h-3 w-3" />}
      </span>
    </th>
  );
}

function compare(a: KnowledgeNode, b: KnowledgeNode, col: Col): number {
  const get = (n: KnowledgeNode) => {
    switch (col) {
      case "type":
        return n.type;
      case "label":
        return n.label ?? "";
      case "topic":
        return n.topic ?? "";
      case "project":
        return n.project ?? "";
      case "confidence":
        return n.confidence ?? -1;
      case "updated":
        return n.updated_at ?? n.created_at ?? "";
    }
  };
  const av = get(a);
  const bv = get(b);
  if (av < bv) return -1;
  if (av > bv) return 1;
  return 0;
}
