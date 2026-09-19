/**
 * SavedQueryChips — chip row above the FacetSidebar.
 *
 * Each chip is a bookmarked filter slice. Click → apply. Hover → reveal X.
 * "Save current view" button on the right opens a tiny inline input to name
 * the current filter state. New chips appear at the front (most-recently-used).
 */

import { useState } from "react";
import { X, Plus, Bookmark, Check } from "lucide-react";
import { useSavedQueries, useSaveQueryMutations } from "@/hooks/use-knowledge";
import { cn } from "@/lib/utils";
import type { FilterState } from "@/components/knowledge/facet-sidebar";

interface SavedQueryChipsProps {
  currentFilters: FilterState;
  onApply: (filters: FilterState) => void;
  hasActive: boolean;
}

export function SavedQueryChips({
  currentFilters,
  onApply,
  hasActive,
}: SavedQueryChipsProps) {
  const { data: queries = [], isLoading } = useSavedQueries();
  const { create, remove, touch } = useSaveQueryMutations();
  const [adding, setAdding] = useState(false);
  /** R5 — which chip's delete is armed. One slot, so an arm cannot travel. */
  const [armed, setArmed] = useState<string | null>(null);
  const [name, setName] = useState("");

  const applyChip = async (id: string, dsl: Record<string, unknown>) => {
    onApply(dsl as FilterState);
    try {
      await touch.mutateAsync(id);
    } catch {
      // non-fatal: chip is still applied
    }
  };

  const saveCurrent = async () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    await create.mutateAsync({
      name: trimmed,
      filter_dsl: currentFilters as unknown as Record<string, unknown>,
    });
    setName("");
    setAdding(false);
  };

  if (isLoading && queries.length === 0) {
    return null;
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {queries.map((q) => (
        <div
          key={q.id}
          className="group inline-flex items-center rounded-sm border border-border-subtle bg-surface hover:border-accent"
        >
          <button
            type="button"
            onClick={() => applyChip(q.id, q.filter_dsl)}
            className="flex items-center gap-1 px-2 py-0.5 text-xs text-fg-muted hover:text-accent"
            title={JSON.stringify(q.filter_dsl)}
          >
            <Bookmark className="h-3 w-3" />
            <span className="font-medium">{q.name}</span>
          </button>
          {/* R5 (372ccdb2) — TWO-STEP ARM, and the arm rather than a modal is a
              judgement worth stating. The row really is deleted, but the thing
              lost is a NAME over the current filters, and "save view" recreates
              it in one click — the same reading by which the START pass gave
              `dismiss` an arm. A modal to drop a filter chip would be friction
              with no recovery value. Keyed per chip, so an arm on one cannot
              confirm another. */}
          {armed === q.id ? (
            <button
              type="button"
              onClick={() => {
                setArmed(null);
                remove.mutate(q.id);
              }}
              aria-label={`Confirm delete ${q.name}`}
              className="px-1 py-0.5 text-error"
              title="Click again to delete this view"
            >
              <Check className="h-3 w-3" />
            </button>
          ) : (
            <button
              type="button"
              onClick={() => setArmed(q.id)}
              aria-label={`Delete ${q.name}`}
              className="px-1 py-0.5 text-tertiary opacity-0 hover:text-error group-hover:opacity-100"
            >
              <X className="h-3 w-3" />
            </button>
          )}
        </div>
      ))}

      {adding ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void saveCurrent();
          }}
          className="inline-flex items-center gap-1 rounded-sm border border-accent bg-accent-subtle px-1.5 py-0.5"
        >
          <input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="name this view…"
            className="w-32 bg-transparent text-xs text-fg outline-none placeholder:text-tertiary"
          />
          <button
            type="submit"
            disabled={create.isPending || !name.trim()}
            className="text-2xs case-label tracking-wider text-accent disabled:text-fg-disabled"
          >
            save
          </button>
          <button
            type="button"
            onClick={() => {
              setAdding(false);
              setName("");
            }}
            className="text-tertiary hover:text-fg-muted"
            aria-label="Cancel"
          >
            <X className="h-3 w-3" />
          </button>
        </form>
      ) : (
        <button
          type="button"
          onClick={() => setAdding(true)}
          disabled={!hasActive}
          className={cn(
            "inline-flex items-center gap-1 rounded-sm border border-border-subtle px-2 py-0.5 text-xs",
            hasActive
              ? "text-fg-muted hover:border-accent hover:text-accent"
              : "text-fg-disabled cursor-not-allowed",
          )}
          title={hasActive ? "Save current filters as a chip" : "Apply some filters first"}
        >
          <Plus className="h-3 w-3" />
          save view
        </button>
      )}
    </div>
  );
}
