import { useMemo, useState } from "react";
import { useGates, useResolveGate } from "@/hooks/use-gates";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { DecisionGateInfo } from "@/lib/api";
import { STEP_LOWER } from "@/lib/nouns";

/**
 * M1 gate panel — surfaces every pending DecisionGate on a task and lets
 * the user lock an ADR via POST /api/tasks/{id}/gates/{gate_id}/resolve.
 * The engine's headless poll picks up the resolution within 2s.
 *
 * Renders nothing when the task has gates_enabled=false or zero gates,
 * so it's safe to mount unconditionally on the task-detail page.
 */
export function GatePanel({ taskId }: { taskId: string }) {
  const { data, isLoading } = useGates(taskId);

  const pending = useMemo(
    () => data?.gates?.filter((g) => g.status === "pending") ?? [],
    [data],
  );
  const resolved = useMemo(
    () => data?.gates?.filter((g) => g.status !== "pending") ?? [],
    [data],
  );

  if (isLoading) return null;
  if (!data?.gates_enabled) return null;
  if (!data?.gates?.length) return null;

  return (
    <div className="space-y-3">
      {pending.length > 0 && (
        <div className="space-y-3">
          {pending.map((gate) => (
            <PendingGateCard
              key={gate.gate_id}
              taskId={taskId}
              gate={gate}
            />
          ))}
        </div>
      )}

      {resolved.length > 0 && (
        <details className="rounded border border-border bg-surface p-2">
          <summary className="cursor-pointer text-2xs uppercase tracking-wider text-tertiary">
            Locked Decisions ({resolved.length})
          </summary>
          <div className="mt-2 space-y-1.5">
            {resolved.map((gate) => (
              <ResolvedGateRow key={gate.gate_id} gate={gate} />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

function PendingGateCard({
  taskId,
  gate,
}: {
  taskId: string;
  gate: DecisionGateInfo;
}) {
  const recommended = gate.options.find((o) => o.recommended);
  const [selected, setSelected] = useState<string>(
    recommended?.id ?? gate.options[0]?.id ?? "",
  );
  const [rationale, setRationale] = useState("");
  const resolve = useResolveGate();

  const submit = (skip = false) => {
    if (!skip && !selected) return;
    resolve.mutate({
      taskId,
      gateId: gate.gate_id,
      selectedOptionId: skip ? undefined : selected,
      rationale: rationale || undefined,
      skipped: skip,
    });
  };

  return (
    <div className="rounded border border-accent/40 bg-accent-subtle p-3">
      <div className="flex items-baseline justify-between">
        <div className="text-2xs font-medium uppercase tracking-wider text-accent">
          decision gate · {STEP_LOWER} {gate.phase_id}
        </div>
      </div>
      <p className="mt-1 text-sm text-fg">{gate.prompt}</p>

      <div className="mt-3 space-y-1.5">
        {gate.options.map((opt) => {
          const active = selected === opt.id;
          return (
            <button
              key={opt.id}
              onClick={() => setSelected(opt.id)}
              className={`w-full rounded border p-2.5 text-left transition-colors ${
                active
                  ? "border-accent bg-surface-elevated"
                  : "border-border bg-surface hover:border-border-hover"
              }`}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span
                    className={`inline-block h-2 w-2 rounded-full ${
                      active ? "bg-accent" : "bg-border"
                    }`}
                  />
                  <span className="text-2xs font-medium uppercase tracking-wider text-fg">
                    {opt.label}
                  </span>
                  {opt.recommended && (
                    <span className="rounded bg-accent/20 px-1.5 py-0.5 text-3xs uppercase tracking-wider text-accent">
                      recommended
                    </span>
                  )}
                </div>
              </div>
              {opt.description && (
                <p className="mt-1 text-xs text-fg-muted">{opt.description}</p>
              )}
              {(opt.pros || opt.cons) && (
                <div className="mt-1 grid grid-cols-2 gap-2 text-2xs">
                  {opt.pros && (
                    <div>
                      <span className="text-tertiary">pros:</span>{" "}
                      <span className="text-fg-muted">{opt.pros}</span>
                    </div>
                  )}
                  {opt.cons && (
                    <div>
                      <span className="text-tertiary">cons:</span>{" "}
                      <span className="text-fg-muted">{opt.cons}</span>
                    </div>
                  )}
                </div>
              )}
            </button>
          );
        })}
      </div>

      <div className="mt-3 space-y-2">
        <Textarea
          placeholder="Rationale (optional — surfaces in the ADR + every downstream brief)"
          value={rationale}
          onChange={(e) => setRationale(e.target.value)}
          rows={2}
          className="text-xs"
        />
        <div className="flex items-center justify-end gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => submit(true)}
            disabled={resolve.isPending}
          >
            Skip
          </Button>
          <Button
            size="sm"
            onClick={() => submit(false)}
            disabled={resolve.isPending || !selected}
          >
            {resolve.isPending ? "Locking…" : "Lock ADR"}
          </Button>
        </div>
        {resolve.isError && (
          <p className="text-2xs text-error">
            {(resolve.error as Error)?.message ?? "Failed to resolve gate"}
          </p>
        )}
      </div>
    </div>
  );
}

function ResolvedGateRow({ gate }: { gate: DecisionGateInfo }) {
  const selected = gate.options.find((o) => o.id === gate.selected_option_id);
  return (
    <div className="rounded border border-border bg-surface px-2.5 py-1.5">
      <div className="flex items-center justify-between">
        <div className="text-2xs uppercase tracking-wider text-fg-muted">
          {STEP_LOWER} {gate.phase_id}
        </div>
        <div className="text-3xs uppercase tracking-wider text-tertiary">
          {gate.status}
        </div>
      </div>
      {gate.status === "resolved" && (
        <div className="mt-0.5 text-xs text-fg">
          → {selected?.label ?? gate.selected_option_id}
          {gate.selected_rationale && (
            <span className="text-fg-muted"> — {gate.selected_rationale}</span>
          )}
        </div>
      )}
    </div>
  );
}
