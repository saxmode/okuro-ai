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
          <summary className="cursor-pointer text-2xs case-label tracking-wider text-tertiary">
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

/**
 * S2 — the artifact this decision is made AGAINST, named above the
 * question. A gate whose basis is unnamed is how an approval ends up
 * attached to text nobody read, so the title leads and the id follows as
 * the handle you fetch it by. Clicking the id copies it in full; only a
 * prefix is shown because a bare UUID in a sentence is clutter.
 *
 * Renders nothing for a gate with no subject — every legacy gate.
 */
function GateSubjectLine({ gate }: { gate: DecisionGateInfo }) {
  const [copied, setCopied] = useState(false);
  const id = gate.subject_artifact_id;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard blocked (no permission, insecure origin). The prefix
      // stays on screen, so the user is never left with nothing.
    }
  };

  if (!id) return null;

  return (
    <div className="mt-1 flex items-baseline gap-1.5">
      <span className="text-3xs case-label tracking-wider text-tertiary">
        basis
      </span>
      <span className="text-xs text-fg-muted">
        {gate.subject_title || "untitled artifact"}
      </span>
      <button
        type="button"
        onClick={copy}
        title={`Copy artifact id ${id}`}
        className="font-mono text-3xs text-tertiary underline decoration-dotted underline-offset-2 hover:text-fg-muted"
      >
        {copied ? "copied" : id.slice(0, 8)}
      </button>
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
        <div className="text-2xs font-medium case-label tracking-wider text-accent">
          decision gate · {STEP_LOWER} {gate.phase_id}
        </div>
      </div>
      <GateSubjectLine gate={gate} />
      {gate.basis_moved && (
        // Said BEFORE the options, not after the submit. Letting someone
        // read, weigh and pick, then answering 409, spends their attention
        // on a decision that was never going to be accepted.
        <p className="mt-1.5 rounded border border-warning/40 bg-warning-subtle px-2 py-1 text-xs text-fg">
          What this decision is about has changed since the question was
          asked. Answering will be refused — the gate has to be posed again
          against the current version, or skipped.
        </p>
      )}
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
                  <span className="text-2xs font-medium case-label tracking-wider text-fg">
                    {opt.label}
                  </span>
                  {opt.recommended && (
                    <span className="rounded bg-accent/20 px-1.5 py-0.5 text-3xs case-label tracking-wider text-accent">
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
        <div className="text-2xs case-label tracking-wider text-fg-muted">
          {STEP_LOWER} {gate.phase_id}
        </div>
        <div className="text-3xs case-label tracking-wider text-tertiary">
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
      {/* The locked decision keeps naming its basis: an ADR read six
          weeks later is only as good as the text it points at. */}
      <GateSubjectLine gate={gate} />
    </div>
  );
}
