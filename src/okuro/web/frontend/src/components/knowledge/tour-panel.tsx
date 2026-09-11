/**
 * TourPanel — left-rail drawer that lists tour steps from kind=tour artifacts.
 *
 * Source: GET /api/tours?project=<slug> returns the most-recent N tour
 * artifacts as parsed TourBody objects (the tour-builder role emits one
 * per invocation). Clicking a step calls onFocus(stepFileId) so the
 * parent /knowledge page can highlight the matching node in the graph
 * and open the detail-drawer.
 *
 * Pedagogy: each step's prerequisites are STEP INDICES (not paths) — we
 * render them as small back-references so a learner can see the
 * dependency chain inside the tour at a glance.
 */

import { useMemo, useState } from "react";
import { X, MapPin, Loader2 } from "lucide-react";
import { useTours } from "@/hooks/use-knowledge";
import { LAYER_COLOR } from "./unified-graph";
import { cn } from "@/lib/utils";

interface TourPanelProps {
  project?: string;
  onFocus?: (nodeId: string) => void;
  onClose?: () => void;
}

export function TourPanel({ project, onFocus, onClose }: TourPanelProps) {
  const { data, isLoading, isError, error } = useTours(project, true);
  const tours = data?.tours ?? [];
  const [activeId, setActiveId] = useState<string | null>(null);

  const activeTour = useMemo(() => {
    if (activeId) return tours.find((t) => t.id === activeId);
    return tours[0]; // default to most recent
  }, [tours, activeId]);

  return (
    <div className="flex h-[68vh] w-[40rem] flex-col rounded-sm border border-border-subtle bg-surface">
      <div className="flex items-center justify-between border-b border-border-subtle px-3 py-2">
        <div className="flex items-center gap-1.5 text-xs uppercase tracking-wider text-tertiary">
          <MapPin className="h-3.5 w-3.5" />
          <span className="font-mono">tour</span>
          {project && (
            <span className="font-mono text-fg-muted">/ {project}</span>
          )}
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="rounded-sm p-1 text-fg-muted hover:bg-surface-subtle hover:text-fg"
            aria-label="Close tour panel"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      {tours.length > 1 && (
        <div className="border-b border-border-subtle px-3 py-1.5">
          <select
            value={activeTour?.id ?? ""}
            onChange={(e) => setActiveId(e.target.value)}
            className="w-full bg-transparent text-xs text-fg outline-none"
          >
            {tours.map((t) => (
              <option key={t.id} value={t.id}>
                {t.title} {t.created_at ? `· ${t.created_at.slice(0, 10)}` : ""}
              </option>
            ))}
          </select>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading ? (
          <div className="flex h-full items-center justify-center text-tertiary">
            <Loader2 className="h-4 w-4 animate-spin" />
          </div>
        ) : isError ? (
          <EmptyMsg
            title="Couldn't load tours"
            body={(error as Error | undefined)?.message ?? ""}
          />
        ) : !activeTour ? (
          <EmptyMsg
            title="No tours yet"
            body={
              project
                ? `Run the tour-builder role for ${project} (needs in_layer + imports triples in KG).`
                : "Tours are project-scoped — pick a project filter or run the tour-builder role."
            }
          />
        ) : (
          <ol className="divide-y divide-subtle">
            {activeTour.body.steps.map((step) => {
              const nodeId = `kge:${step.file}`;
              const layerColor = LAYER_COLOR[step.layer] ?? LAYER_COLOR.unknown;
              return (
                <li key={step.index}>
                  <button
                    type="button"
                    onClick={() => onFocus?.(nodeId)}
                    className={cn(
                      "group block w-full px-3 py-2 text-left transition-colors",
                      "hover:bg-surface-subtle",
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <span
                        className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-sm font-mono text-2xs"
                        style={{
                          background: `${layerColor}1f`,
                          color: layerColor,
                        }}
                      >
                        {step.index}
                      </span>
                      <span className="truncate text-xs font-mono text-fg">
                        {step.file}
                      </span>
                      <span className="ml-auto text-[9px] uppercase tracking-wider text-tertiary">
                        {step.layer}
                      </span>
                    </div>
                    <p className="mt-1 line-clamp-2 text-xs text-fg-muted">
                      {step.why}
                    </p>
                    {step.symbols.length > 0 && (
                      <div className="mt-1 flex flex-wrap gap-1">
                        {step.symbols.slice(0, 5).map((s) => (
                          <span
                            key={s}
                            className="rounded-sm bg-surface-subtle px-1.5 py-0.5 font-mono text-[10px] text-tertiary"
                          >
                            {s}
                          </span>
                        ))}
                      </div>
                    )}
                    {step.prerequisites.length > 0 && (
                      <div className="mt-1 text-[10px] text-tertiary">
                        after step{step.prerequisites.length > 1 ? "s" : ""}{" "}
                        <span className="font-mono">
                          {step.prerequisites.join(", ")}
                        </span>
                      </div>
                    )}
                  </button>
                </li>
              );
            })}
          </ol>
        )}
      </div>

      {activeTour && (
        <div className="border-t border-border-subtle px-3 py-1.5 text-2xs uppercase tracking-wider text-tertiary">
          {activeTour.body.steps.length} steps
          {activeTour.body.focus_layer
            ? ` · focus ${activeTour.body.focus_layer}`
            : ""}
        </div>
      )}
    </div>
  );
}

function EmptyMsg({ title, body }: { title: string; body: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-1 px-4 text-center">
      <div className="text-xs text-fg">{title}</div>
      {body && <div className="text-2xs text-tertiary">{body}</div>}
    </div>
  );
}
