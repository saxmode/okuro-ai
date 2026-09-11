/**
 * DetailDrawer — right-side slide-in for a selected knowledge node.
 *
 * Layout (top → bottom):
 *   Header     — entity type chip, project, close button
 *   Relation strip — supersedes ↑ / superseded-by ↓ / parent / children
 *                    / memory_refs / tunnels / kg_triples / progress_refs.
 *                    Each chip is clickable → re-targets the drawer.
 *   Provenance — created_at, agent, role, kind, confidence, status
 *   Body       — full content, rendered as markdown (shared MarkdownContent module)
 *
 * Width is user-adjustable via a drag handle on the left edge (mirror of
 * the shell Sidebar's resize handle — same pixel-clamp + localStorage
 * persistence convention, see components/shell/sidebar.tsx) since longer
 * artifact bodies need more than the 24rem default to read comfortably.
 *
 * Stacked panes (Roam-style) deferred — for v1 clicking a related node
 * REPLACES the drawer contents. Stacking adds nav history without a
 * back button, which is its own UX problem; ship simple first.
 */

import { useCallback, useMemo, useRef, useState } from "react";
import { X, ArrowUp, ArrowDown, ArrowRight, Loader2 } from "lucide-react";
import { useNodeDetail } from "@/hooks/use-knowledge";
import type {
  KnowledgeNode,
  KnowledgeNodeType,
  NodeRelations,
  RelationItem,
} from "@/types/knowledge";
import { cn } from "@/lib/utils";
import { MarkdownContent } from "@/components/ui/markdown-content";

interface DetailDrawerProps {
  nodeId: string | null;
  onClose: () => void;
  onSelect: (nodeId: string) => void;
}

const WIDTH_KEY = "okuro-knowledge-drawer-width";
const DEFAULT_WIDTH = 384; // 24rem
const MIN_WIDTH = 320;
const MAX_WIDTH = 900;

function loadWidth(): number {
  try {
    const stored = localStorage.getItem(WIDTH_KEY);
    if (stored) return Number(stored);
  } catch {
    /* ignore */
  }
  return DEFAULT_WIDTH;
}

function saveWidth(width: number): void {
  try {
    localStorage.setItem(WIDTH_KEY, String(width));
  } catch {
    /* ignore */
  }
}

const TYPE_LABEL: Record<KnowledgeNodeType, string> = {
  memory: "Memory",
  thought: "Thought",
  artifact: "Artifact",
  progress: "Progress",
  kg_entity: "KG entity",
};

const TYPE_CLASS: Record<KnowledgeNodeType, string> = {
  memory: "bg-accent-subtle text-accent",
  thought: "bg-warning/15 text-warning",
  artifact: "bg-info/15 text-info",
  progress: "bg-tertiary/15 text-tertiary",
  kg_entity: "bg-fg-muted/10 text-fg-muted",
};

export function DetailDrawer({ nodeId, onClose, onSelect }: DetailDrawerProps) {
  const { data, isLoading, isError, error } = useNodeDetail(nodeId);
  const [width, setWidth] = useState(loadWidth);

  const onResize = useCallback((next: number) => {
    setWidth(next);
    saveWidth(next);
  }, []);

  if (!nodeId) return null;

  return (
    <aside
      className="fixed right-0 top-0 z-30 flex h-screen flex-col border-l border-border-subtle bg-surface shadow-[-4px_0_12px_rgba(0,0,0,0.3)]"
      style={{ width }}
      role="dialog"
      aria-label="Node detail"
    >
      <ResizeHandle minWidth={MIN_WIDTH} maxWidth={MAX_WIDTH} onResize={onResize} />

      <Header
        node={data?.node ?? null}
        onClose={onClose}
        loading={isLoading}
      />

      {isError ? (
        <div className="p-4 text-sm text-error">
          Couldn't load node: {(error as Error | undefined)?.message ?? "unknown"}
        </div>
      ) : isLoading || !data ? (
        <div className="flex flex-1 items-center justify-center text-tertiary">
          <Loader2 className="h-4 w-4 animate-spin" />
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto">
          <RelationStrip relations={data.relations} onSelect={onSelect} />
          <Provenance node={data.node} extras={data.extras} />
          <Body body={data.body} />
        </div>
      )}
    </aside>
  );
}

// ── Resize handle ────────────────────────────────────────────────────
// Mirror of components/shell/sidebar.tsx's ResizeHandle, flipped for a
// right-anchored panel: the handle sits on the LEFT edge, and dragging
// left (negative clientX delta) grows the drawer instead of shrinking it.

function ResizeHandle({
  minWidth,
  maxWidth,
  onResize,
}: {
  minWidth: number;
  maxWidth: number;
  onResize: (width: number) => void;
}) {
  const [dragging, setDragging] = useState(false);
  const startXRef = useRef(0);
  const startWidthRef = useRef(0);

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      setDragging(true);
      startXRef.current = e.clientX;
      const drawer = (e.target as HTMLElement).parentElement;
      startWidthRef.current = drawer?.offsetWidth ?? DEFAULT_WIDTH;

      const onPointerMove = (ev: PointerEvent) => {
        const delta = startXRef.current - ev.clientX;
        const next = Math.max(minWidth, Math.min(maxWidth, startWidthRef.current + delta));
        onResize(next);
      };
      const onPointerUp = () => {
        setDragging(false);
        window.removeEventListener("pointermove", onPointerMove);
        window.removeEventListener("pointerup", onPointerUp);
      };
      window.addEventListener("pointermove", onPointerMove);
      window.addEventListener("pointerup", onPointerUp);
    },
    [minWidth, maxWidth, onResize],
  );

  return (
    <div
      onPointerDown={onPointerDown}
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize detail panel"
      className={cn(
        "absolute left-0 top-0 z-30 h-full w-1 cursor-col-resize transition-colors",
        dragging ? "bg-accent" : "hover:bg-border-hover",
      )}
    />
  );
}

// ── Header ───────────────────────────────────────────────────────────

function Header({
  node,
  onClose,
  loading,
}: {
  node: KnowledgeNode | null;
  onClose: () => void;
  loading: boolean;
}) {
  return (
    <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border-subtle px-4 py-3">
      <div className="flex min-w-0 flex-1 items-center gap-2">
        {node && (
          <span
            className={cn(
              "rounded-sm px-1.5 py-0.5 text-[10px] font-mono uppercase tracking-wider",
              TYPE_CLASS[node.type],
            )}
          >
            {TYPE_LABEL[node.type]}
          </span>
        )}
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-fg">
            {node ? (node.topic ? `[${node.topic}] ${node.label}` : node.label) : "—"}
          </div>
          {node?.project && (
            <div className="font-mono text-2xs uppercase tracking-wider text-tertiary">
              {node.project}
            </div>
          )}
        </div>
        {loading && <Loader2 className="h-3 w-3 animate-spin text-tertiary" />}
      </div>
      <button
        type="button"
        onClick={onClose}
        aria-label="Close detail"
        className="rounded-sm p-1 text-tertiary hover:bg-surface-elevated hover:text-fg-muted"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}

// ── Relation strip ───────────────────────────────────────────────────

interface RelGroup {
  label: string;
  icon: typeof ArrowUp;
  items: RelationItem[];
}

function RelationStrip({
  relations,
  onSelect,
}: {
  relations: NodeRelations;
  onSelect: (nodeId: string) => void;
}) {
  const groups = useMemo<RelGroup[]>(
    () => [
      { label: "supersedes", icon: ArrowUp, items: relations.supersedes },
      { label: "superseded by", icon: ArrowDown, items: relations.superseded_by },
      { label: "parent", icon: ArrowUp, items: relations.parents },
      { label: "children", icon: ArrowDown, items: relations.children },
      { label: "refs out", icon: ArrowRight, items: relations.memory_refs_out },
      { label: "refs in", icon: ArrowDown, items: relations.memory_refs_in },
      { label: "tunnels", icon: ArrowRight, items: relations.tunnels },
      { label: "kg → out", icon: ArrowRight, items: relations.kg_triples_out },
      { label: "kg ← in", icon: ArrowDown, items: relations.kg_triples_in },
      { label: "progress → out", icon: ArrowRight, items: relations.progress_refs_out },
      { label: "progress ← in", icon: ArrowDown, items: relations.progress_refs_in },
    ],
    [relations],
  );

  const nonEmpty = groups.filter((g) => g.items.length > 0);

  if (nonEmpty.length === 0) {
    return (
      <div className="px-4 py-3 text-2xs uppercase tracking-wider text-tertiary">
        no relations
      </div>
    );
  }

  return (
    <div className="space-y-2 border-b border-border-subtle px-4 py-3">
      {nonEmpty.map((g) => (
        <div key={g.label}>
          <div className="flex items-center gap-1.5 text-2xs uppercase tracking-wider text-tertiary">
            <g.icon className="h-3 w-3" />
            <span>{g.label}</span>
            <span className="font-mono text-tertiary">({g.items.length})</span>
          </div>
          <div className="mt-1 flex flex-wrap gap-1">
            {g.items.slice(0, 30).map((it) => (
              <button
                key={it.node.id + it.edge_type + it.direction}
                type="button"
                onClick={() => onSelect(it.node.id)}
                className={cn(
                  "max-w-[32rem] truncate rounded-sm px-1.5 py-0.5 text-xs font-medium",
                  "border border-border-subtle hover:border-accent hover:text-accent",
                  TYPE_CLASS[it.node.type],
                )}
                title={it.edge_label ?? undefined}
              >
                {it.node.topic ? `[${it.node.topic}] ` : ""}
                {it.node.label}
              </button>
            ))}
            {g.items.length > 30 && (
              <span className="text-2xs uppercase tracking-wider text-tertiary">
                +{g.items.length - 30} more
              </span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Provenance ───────────────────────────────────────────────────────

function Provenance({
  node,
  extras,
}: {
  node: KnowledgeNode;
  extras: Record<string, unknown>;
}) {
  const items: Array<{ k: string; v: string | number | null | undefined }> = [
    { k: "created", v: node.created_at },
    { k: "updated", v: node.updated_at },
    { k: "confidence", v: node.confidence != null ? node.confidence.toFixed(2) : "—" },
    { k: "status", v: node.status },
    { k: "kind", v: node.kind },
  ];
  for (const [k, v] of Object.entries(extras)) {
    if (v == null) continue;
    if (typeof v === "object") continue; // skip nested for now
    items.push({ k, v: String(v) });
  }
  const visible = items.filter((it) => it.v != null && it.v !== "");
  if (visible.length === 0) return null;
  return (
    <div className="border-b border-border-subtle px-4 py-3">
      <div className="text-2xs uppercase tracking-wider text-tertiary">Provenance</div>
      <dl className="mt-2 grid grid-cols-[6rem_1fr] gap-y-1 font-mono text-xs">
        {visible.map((it) => (
          <Row key={it.k} k={it.k} v={String(it.v)} />
        ))}
      </dl>
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <>
      <dt className="text-tertiary">{k}</dt>
      <dd className="break-all text-fg-muted">{v}</dd>
    </>
  );
}

// ── Body ─────────────────────────────────────────────────────────────

function Body({ body }: { body: string | null | undefined }) {
  if (!body) {
    return (
      <div className="px-4 py-3 text-2xs uppercase tracking-wider text-tertiary">
        no body
      </div>
    );
  }
  return (
    <div className="px-4 py-3">
      <div className="text-2xs uppercase tracking-wider text-tertiary">Content</div>
      <div className="mt-2">
        <MarkdownContent variant="compact">{body}</MarkdownContent>
      </div>
    </div>
  );
}
