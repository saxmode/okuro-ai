/**
 * SlidersPanel — Mode B of the recipient profiler.
 *
 * Eight 5-step axes drive how okuro reshapes content for the recipient.
 * Each axis is a labeled segmented control (1..5 buttons). Each click
 * fires a partial PUT — no separate "save" button to forget.
 *
 * The server is the source of truth for axis order + labels (loaded
 * once from /api/people/slider-meta) so axis renames don't require a
 * frontend redeploy. Defaults are seeded from the person's role
 * (loaded from /api/people/role-defaults?role=…) and shown as a faint
 * "default" hint when the slider matches; deviating from the default
 * shows a "modified" chip.
 *
 * Replaces the manual Communication + Cognitive form sections in
 * PersonDetail. The user explicitly disliked those — too many
 * dropdowns, no semantic anchor, no defaults.
 */

import { useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";
import { peopleApi } from "@/lib/people-api";
import { cn } from "@/lib/utils";

const TICKS = [1, 2, 3, 4, 5] as const;

type SliderRow = {
  key: string;
  left_label: string;
  right_label: string;
};

export function SlidersPanel({
  personId,
  role,
}: {
  personId: string;
  role?: string | null;
}) {
  const queryClient = useQueryClient();
  const [local, setLocal] = useState<Record<string, number>>({});

  // Slider metadata (axis names + pole labels) — server-driven so a
  // rename in cognitive_profile.py reaches the UI on next page load.
  const metaQ = useQuery({
    queryKey: ["people", "slider-meta"],
    queryFn: () => peopleApi.getSliderMeta(),
    staleTime: 1000 * 60 * 60, // metadata is effectively static per build
  });

  // Role defaults — seeds the "Reset" button + the deviation badge.
  const defaultsQ = useQuery({
    queryKey: ["people", "role-defaults", role || "default"],
    queryFn: () => peopleApi.getRoleDefaults(role || undefined),
  });

  // Current sliders for this person (read from the same /api/people/{id}
  // payload the parent page already has cached).
  const personQ = useQuery({
    queryKey: ["person", personId],
    queryFn: () => peopleApi.get(personId),
  });

  // Hydrate the local model when person data arrives. We track local
  // state so the UI updates instantly on click; the mutation reconciles
  // server state in the background.
  useEffect(() => {
    const stored = (personQ.data?.cognitive?.sliders ?? {}) as Record<
      string,
      number
    >;
    if (Object.keys(stored).length > 0) {
      setLocal(stored);
    } else if (defaultsQ.data?.sliders) {
      // No stored vector yet — show the role default so the panel never
      // looks empty. Persists nothing until the user actually clicks.
      setLocal(defaultsQ.data.sliders);
    }
  }, [personQ.data, defaultsQ.data]);

  const putMut = useMutation({
    mutationFn: (sliders: Record<string, number>) =>
      peopleApi.putSliders(personId, sliders),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["person", personId] });
    },
    onError: (err: Error) => {
      toast.error(err.message || "Slider save failed");
    },
  });

  const resetMut = useMutation({
    mutationFn: async () => {
      const defaults = defaultsQ.data?.sliders ?? {};
      return peopleApi.putSliders(personId, defaults);
    },
    onSuccess: () => {
      toast.success("Reset to role defaults");
      queryClient.invalidateQueries({ queryKey: ["person", personId] });
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const onTickClick = (key: string, value: number) => {
    setLocal((prev) => ({ ...prev, [key]: value }));
    putMut.mutate({ [key]: value });
  };

  const defaults = defaultsQ.data?.sliders ?? {};
  // Provenance + persisted vector drive the per-axis confidence chip. An axis
  // is a "prior" (assumption) when it's a role-seeded preview not yet stored,
  // or its stored confidence is low. User-set axes (confidence ≥ .6) and legacy
  // stored-without-provenance axes get no chip — matches the lens semantics.
  const provenance = personQ.data?.cognitive?.slider_provenance ?? {};
  const stored = (personQ.data?.cognitive?.sliders ?? {}) as Record<string, number>;
  const meta: SliderRow[] = useMemo(
    () => metaQ.data?.sliders ?? [],
    [metaQ.data],
  );

  if (metaQ.isPending || personQ.isPending) {
    return (
      <div className="flex items-center justify-center py-6 text-xs text-fg-subtle">
        <Loader2 className="mr-2 h-3 w-3 animate-spin" />
        Loading sliders…
      </div>
    );
  }

  if (meta.length === 0) {
    return (
      <div className="text-xs text-fg-subtle">
        Slider metadata unavailable.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="text-[10px] font-medium uppercase tracking-wider text-fg-subtle">
          Sliders
          {role ? (
            <span className="ml-2 font-normal normal-case tracking-normal">
              defaults seeded from <span className="font-mono">{role}</span>
            </span>
          ) : null}
        </div>
        <Button
          variant="outline"
          size="xs"
          disabled={resetMut.isPending}
          onClick={() => resetMut.mutate()}
          title="Reset all 8 axes to the role default"
        >
          {resetMut.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <RotateCcw className="h-3 w-3" />
          )}
          Reset to default
        </Button>
      </div>

      <div className="space-y-2.5">
        {meta.map((row) => {
          const current = local[row.key];
          const def = defaults[row.key];
          const modified = current !== undefined && def !== undefined && current !== def;
          const prov = provenance[row.key];
          const confidence = typeof prov?.confidence === "number" ? prov.confidence : undefined;
          const isPrior =
            (confidence !== undefined && confidence < 0.6) ||
            (prov === undefined && stored[row.key] === undefined);
          return (
            <div
              key={row.key}
              className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-3 rounded border border-border bg-surface p-2.5"
            >
              <div className="text-right text-[11px] text-fg-muted">
                {row.left_label}
              </div>

              <div className="flex items-center gap-1">
                {TICKS.map((tick) => {
                  const selected = current === tick;
                  const isDefault = def === tick;
                  return (
                    <button
                      key={tick}
                      type="button"
                      onClick={() => onTickClick(row.key, tick)}
                      className={cn(
                        "h-7 w-7 rounded text-[11px] font-medium transition-colors",
                        selected
                          ? "bg-accent text-accent-foreground"
                          : "bg-surface-subtle text-fg-muted hover:bg-surface-elevated",
                        isDefault && !selected
                          ? "ring-1 ring-inset ring-border"
                          : null,
                      )}
                      title={
                        isDefault
                          ? `${tick} — role default`
                          : selected
                            ? `${tick} — selected`
                            : `Set ${row.key.replace(/_/g, " ")} to ${tick}`
                      }
                    >
                      {tick}
                    </button>
                  );
                })}
              </div>

              <div className="flex items-center justify-between gap-2 text-[11px] text-fg-muted">
                <span>{row.right_label}</span>
                <span className="flex items-center gap-1">
                  {isPrior ? (
                    <span
                      className="rounded-full border border-warning/40 bg-warning/10 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-warning"
                      title="Role-seeded assumption — not confirmed by the user yet. Click a value to confirm."
                    >
                      prior
                    </span>
                  ) : null}
                  {modified ? (
                    <span
                      className="rounded-full border border-border bg-surface-subtle px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-fg-subtle"
                      title={`Default for this role: ${def}`}
                    >
                      modified
                    </span>
                  ) : null}
                </span>
              </div>
            </div>
          );
        })}
      </div>

      <div className="space-y-1 text-[10px] text-fg-subtle">
        <p>
          <span className="text-warning">prior</span> = a role-seeded
          assumption; click any value to confirm it as observed.
        </p>
        <p>
          Each click saves immediately. The recipient is anonymized to the
          translation model — only the slider vector reaches the LLM, never
          their name, organization, or contact.
        </p>
      </div>
    </div>
  );
}
