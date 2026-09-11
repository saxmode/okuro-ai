/**
 * FacetSidebar — left rail with live counts per facet.
 *
 * Single-select per facet for v1 (Obsidian-style; multi-select is feature-creep
 * before we know users want it). Active filters appear as removable chips above
 * the facet list. Counts derive from the API's pre-computed facet aggregation
 * over the currently-loaded slice — they DO update as filters narrow.
 */

import { X, ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";
import type {
  FacetCount,
  KnowledgeFacets,
  KnowledgeNodeType,
  KnowledgeGraphParams,
} from "@/types/knowledge";
import { cn } from "@/lib/utils";

type FilterState = Pick<
  KnowledgeGraphParams,
  "topic" | "project" | "status" | "confidence_min" | "confidence_max"
> & {
  entityType?: KnowledgeNodeType | null;
};

interface FacetSidebarProps {
  facets: KnowledgeFacets | null;
  filters: FilterState;
  onChange: (next: FilterState) => void;
  totalCount: number;
}

const ENTITY_LABEL: Record<KnowledgeNodeType, string> = {
  memory: "Memory",
  thought: "Thought",
  artifact: "Artifact",
  progress: "Progress",
  kg_entity: "KG entity",
};

const ENTITY_DOT: Record<KnowledgeNodeType, string> = {
  memory: "bg-accent",
  thought: "bg-warning",
  artifact: "bg-info",
  progress: "bg-tertiary",
  kg_entity: "bg-fg-muted",
};

export function FacetSidebar({
  facets,
  filters,
  onChange,
  totalCount,
}: FacetSidebarProps) {
  const activeChips = buildActiveChips(filters);

  return (
    <div className="space-y-3 text-sm">
      {activeChips.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {activeChips.map((c) => (
            <button
              key={c.key}
              type="button"
              onClick={() =>
                onChange({ ...filters, [c.field]: undefined } as FilterState)
              }
              className="flex items-center gap-1 rounded-sm border border-accent bg-accent-subtle px-1.5 py-0.5 text-2xs uppercase tracking-wider text-accent hover:bg-accent/30"
            >
              <span className="font-mono">{c.field}:</span>
              <span className="font-medium">{c.value}</span>
              <X className="h-3 w-3" />
            </button>
          ))}
        </div>
      )}

      <div className="text-2xs uppercase tracking-wider text-tertiary">
        Showing <span className="font-mono text-fg-muted">{totalCount}</span>
      </div>

      <FacetSection
        title="Entity type"
        items={facets?.entity_types ?? []}
        activeValue={filters.entityType ?? null}
        onSelect={(v) =>
          onChange({
            ...filters,
            entityType: (v as KnowledgeNodeType | null) ?? undefined,
          })
        }
        renderPrefix={(v) => {
          const t = v as KnowledgeNodeType;
          return ENTITY_DOT[t] ? (
            <span className={cn("h-2 w-2 rounded-sm", ENTITY_DOT[t])} />
          ) : null;
        }}
      />

      <FacetSection
        title="Topic"
        items={facets?.topics ?? []}
        activeValue={filters.topic ?? null}
        onSelect={(v) => onChange({ ...filters, topic: v ?? undefined })}
      />

      <FacetSection
        title="Project"
        items={facets?.projects ?? []}
        activeValue={filters.project ?? null}
        onSelect={(v) => onChange({ ...filters, project: v ?? undefined })}
      />

      <FacetSection
        title="Status"
        items={facets?.statuses ?? []}
        activeValue={filters.status ?? null}
        onSelect={(v) => onChange({ ...filters, status: v ?? undefined })}
      />

      <ConfidenceFacet
        bins={facets?.confidence_bins ?? []}
        min={filters.confidence_min ?? null}
        max={filters.confidence_max ?? null}
        onChange={(min, max) =>
          onChange({
            ...filters,
            confidence_min: min ?? undefined,
            confidence_max: max ?? undefined,
          })
        }
      />
    </div>
  );
}

// ── Section primitive ────────────────────────────────────────────────

interface FacetSectionProps {
  title: string;
  items: FacetCount[];
  activeValue: string | null;
  onSelect: (value: string | null) => void;
  renderPrefix?: (value: string) => React.ReactNode;
}

function FacetSection({
  title,
  items,
  activeValue,
  onSelect,
  renderPrefix,
}: FacetSectionProps) {
  const [open, setOpen] = useState(true);
  if (items.length === 0) return null;
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1 text-2xs uppercase tracking-wider text-tertiary hover:text-fg-muted"
      >
        <Chevron className="h-3 w-3" />
        <span>{title}</span>
        <span className="ml-auto font-mono text-tertiary">{items.length}</span>
      </button>
      {open && (
        <ul className="mt-1.5 space-y-0.5">
          {items.slice(0, 14).map((it) => {
            const active = it.value === activeValue;
            return (
              <li key={it.value}>
                <button
                  type="button"
                  onClick={() => onSelect(active ? null : it.value)}
                  className={cn(
                    "flex w-full items-center gap-1.5 rounded-sm px-1.5 py-0.5 text-xs transition-colors",
                    active
                      ? "bg-accent-subtle text-accent"
                      : "text-fg-muted hover:bg-surface-elevated",
                  )}
                >
                  {renderPrefix?.(it.value)}
                  <span className="truncate">{it.value}</span>
                  <span className="ml-auto font-mono text-tertiary">{it.count}</span>
                </button>
              </li>
            );
          })}
          {items.length > 14 && (
            <li className="px-1.5 text-2xs uppercase tracking-wider text-tertiary">
              +{items.length - 14} more
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

// ── Confidence (range slider as buttons) ─────────────────────────────

const CONF_BANDS: Array<{ label: string; min: number; max: number }> = [
  { label: "high (≥0.8)", min: 0.8, max: 1.0 },
  { label: "mid (0.4–0.8)", min: 0.4, max: 0.8 },
  { label: "low (<0.4)", min: 0.0, max: 0.4 },
];

function ConfidenceFacet({
  bins,
  min,
  max,
  onChange,
}: {
  bins: FacetCount[];
  min: number | null;
  max: number | null;
  onChange: (min: number | null, max: number | null) => void;
}) {
  const [open, setOpen] = useState(true);
  // count helper: sum bins inside a band
  const inBand = (lo: number, hi: number): number => {
    let c = 0;
    for (const b of bins) {
      const v = b.value;
      if (v === "unknown") continue;
      const [a, z] = v.split("-").map(Number);
      if (a == null || z == null) continue;
      if (a >= lo && z <= hi) c += b.count;
    }
    return c;
  };
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1 text-2xs uppercase tracking-wider text-tertiary hover:text-fg-muted"
      >
        <Chevron className="h-3 w-3" />
        <span>Confidence</span>
      </button>
      {open && (
        <ul className="mt-1.5 space-y-0.5">
          {CONF_BANDS.map((b) => {
            const active = min === b.min && max === b.max;
            return (
              <li key={b.label}>
                <button
                  type="button"
                  onClick={() => onChange(active ? null : b.min, active ? null : b.max)}
                  className={cn(
                    "flex w-full items-center gap-1.5 rounded-sm px-1.5 py-0.5 text-xs transition-colors",
                    active
                      ? "bg-accent-subtle text-accent"
                      : "text-fg-muted hover:bg-surface-elevated",
                  )}
                >
                  <span className="truncate">{b.label}</span>
                  <span className="ml-auto font-mono text-tertiary">{inBand(b.min, b.max)}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// ── Active chips builder ─────────────────────────────────────────────

interface Chip {
  key: string;
  field: keyof FilterState;
  value: string;
}

function buildActiveChips(filters: FilterState): Chip[] {
  const out: Chip[] = [];
  if (filters.entityType) {
    out.push({ key: "entityType", field: "entityType", value: ENTITY_LABEL[filters.entityType] });
  }
  if (filters.topic) out.push({ key: "topic", field: "topic", value: filters.topic });
  if (filters.project) out.push({ key: "project", field: "project", value: filters.project });
  if (filters.status) out.push({ key: "status", field: "status", value: filters.status });
  if (filters.confidence_min != null || filters.confidence_max != null) {
    out.push({
      key: "confidence",
      field: "confidence_min",
      value: `${filters.confidence_min ?? "*"}–${filters.confidence_max ?? "*"}`,
    });
  }
  return out;
}

export type { FilterState };
