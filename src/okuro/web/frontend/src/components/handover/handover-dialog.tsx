// <!-- AGENT_HEADER
// role: code
// purpose: Shared "hand this selection to another tool" dialog. Fed a Content
//   IR by any source page (notes selection, flow subgraph); discovers
//   reachable targets, renders only their ask/optional fields, sends, and
//   offers to open the produced doc. One dialog, every source wires it the
//   same way — see pages/notes.tsx and components/flow-designer/flow-designer.tsx.
// index: RecipientField | HandoverDialog
// AGENT_HEADER_END -->
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { toast } from "sonner";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FieldLabel } from "@/components/ui/form-primitives";
import { Segmented } from "@/components/ui/segmented";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Bot, Diamond, Image, Network, NotebookPen, Presentation, Send, type LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { startFlowDraw } from "@/lib/flow-stream";
import { peopleApi, type PersonSummary } from "@/lib/people-api";
import {
  handoverApi,
  targetGroupsApi,
  type ContentIR,
  type HandoverField,
  type RecipientValue,
  type TargetGroupSummary,
} from "@/lib/handover-api";

/** Target `icon` is a lucide name (backend-authoritative) — render the matching
 *  component so the picker stays on the design-system icon set, never emoji. */
const TARGET_ICONS: Record<string, LucideIcon> = {
  "notebook-pen": NotebookPen,
  network: Network,
  diamond: Diamond,
  presentation: Presentation,
  send: Send,
  bot: Bot,
  image: Image,
};

function TargetIcon({ name }: { name: string }) {
  const Icon = TARGET_ICONS[name];
  return Icon ? <Icon size={14} /> : null;
}

/** Person-or-group picker for a `recipient`-typed field. Sends either
 *  {kind:"person", id} or {kind:"group", id} — never both. Groups with zero
 *  members are valid (backend resolves them as a generic archetype), so the
 *  count is shown but never disables the option. */
function RecipientField({
  value,
  onChange,
  mode,
  onModeChange,
  people,
  peopleLoading,
  groups,
  groupsLoading,
  invalid,
}: {
  value?: RecipientValue;
  onChange: (v: RecipientValue | undefined) => void;
  mode: "person" | "group";
  onModeChange: (mode: "person" | "group") => void;
  people: PersonSummary[];
  peopleLoading: boolean;
  groups: TargetGroupSummary[];
  groupsLoading: boolean;
  invalid?: boolean;
}) {
  return (
    <div className="space-y-2">
      <Segmented
        options={[
          { label: "Person", value: "person" as const },
          { label: "Group", value: "group" as const },
        ]}
        value={mode}
        onChange={(next) => {
          onModeChange(next);
          onChange(undefined);
        }}
        ariaLabel="Recipient type"
      />
      {mode === "person" ? (
        <Select
          value={value?.kind === "person" ? value.id : ""}
          onValueChange={(id) => onChange({ kind: "person", id })}
        >
          <SelectTrigger className={cn("w-full", invalid && "border-error")}>
            <SelectValue placeholder={peopleLoading ? "Loading…" : "Pick a person"} />
          </SelectTrigger>
          <SelectContent>
            {people.map((p) => (
              <SelectItem key={p.id} value={p.id}>
                {p.display_name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : (
        <Select
          value={value?.kind === "group" ? value.id : ""}
          onValueChange={(id) => onChange({ kind: "group", id })}
        >
          <SelectTrigger className={cn("w-full", invalid && "border-error")}>
            <SelectValue placeholder={groupsLoading ? "Loading…" : "Pick a group"} />
          </SelectTrigger>
          <SelectContent>
            {groups.map((g) => (
              <SelectItem key={g.id} value={g.id}>
                {g.name}
                <span className="text-fg-subtle"> · {g.member_count}</span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </div>
  );
}

function isFieldSatisfied(field: HandoverField, inputs: Record<string, unknown>): boolean {
  const v = inputs[field.key];
  if (field.type === "recipient") {
    return !!(v && typeof v === "object" && (v as RecipientValue).kind && (v as RecipientValue).id);
  }
  return !!v;
}

export function HandoverDialog({
  open,
  onOpenChange,
  content,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Built by the caller right before opening — see notes.tsx / flow-designer.tsx. */
  content: ContentIR | null;
}) {
  const navigate = useNavigate();
  const [targetId, setTargetId] = useState<string | null>(null);
  const [inputs, setInputs] = useState<Record<string, unknown>>({});
  const [recipientMode, setRecipientMode] = useState<"person" | "group">("person");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [missingKeys, setMissingKeys] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<{ url?: string; label?: string; target?: string } | null>(null);

  // Fresh intake state every time the dialog opens for a (possibly new) selection.
  useEffect(() => {
    if (!open) return;
    setTargetId(null);
    setInputs({});
    setRecipientMode("person");
    setErrorMsg(null);
    setMissingKeys(new Set());
    setResult(null);
  }, [open, content]);

  const targetsQuery = useQuery({
    queryKey: ["handover", "targets", content],
    queryFn: () => handoverApi.targets(content as ContentIR),
    enabled: open && !!content,
  });
  const targets = targetsQuery.data?.targets ?? [];
  const target = targets.find((t) => t.id === targetId) ?? null;
  // Render ONLY ask + optional fields — "auto" fields resolve silently server-side.
  const fields = (target?.fields ?? []).filter((f) => f.mode === "ask" || f.mode === "optional");
  const needsRecipient = fields.some((f) => f.type === "recipient");

  const peopleQuery = useQuery({
    queryKey: ["people", "handover"],
    queryFn: () => peopleApi.list(),
    enabled: open && needsRecipient,
  });
  const people = peopleQuery.data?.people ?? [];

  const groupsQuery = useQuery({
    queryKey: ["target-groups", "handover"],
    queryFn: () => targetGroupsApi.list(),
    enabled: open && needsRecipient,
  });
  const groups = groupsQuery.data?.groups ?? [];

  const send = useMutation({
    mutationFn: () => handoverApi.send(content as ContentIR, (target as { id: string }).id, inputs),
    onSuccess: (r) => {
      setErrorMsg(null);
      setMissingKeys(new Set());
      setResult(r);
      toast.success(`Sent to ${target?.label ?? r.target ?? "target"}`, {
        description: r.label,
      });
    },
    onError: (e: unknown) => {
      const msg = e instanceof Error ? e.message : String(e);
      setErrorMsg(msg);
      const m = /needs: (.+)$/.exec(msg);
      const missing = m?.[1] ?? "";
      setMissingKeys(new Set(missing.split(",").map((s) => s.trim()).filter(Boolean)));
    },
  });

  const setField = (key: string, value: unknown) => {
    setInputs((prev) => ({ ...prev, [key]: value }));
    setMissingKeys((prev) => {
      if (!prev.has(key)) return prev;
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
  };

  const canSend = useMemo(() => {
    if (!target) return false;
    const askFields = target.fields.filter((f) => f.mode === "ask");
    return askFields.every((f) => isFieldSatisfied(f, inputs));
  }, [target, inputs]);

  // text/facet → flow is drawn LIVE via the existing draw-stream pipeline (the
  // flow SSE endpoint generates + saves), not the blocking /api/handover/send.
  // subgraph → flow stays on /send (it's an instant passthrough of the graph).
  const isStreamFlow =
    target?.id === "flow" && (content?.kind === "text" || content?.kind === "facet");

  const onSend = () => {
    if (isStreamFlow && content) {
      // Seed the draw with the selection: title adds signal only when it isn't
      // already the body's opening line.
      const prompt =
        content.title && !content.body_md.startsWith(content.title)
          ? `${content.title}\n\n${content.body_md}`.trim()
          : content.body_md;
      // Queue the prompt + bring the canvas up (draw in place if a designer is
      // already mounted, else navigate so one mounts and drains the queue).
      onOpenChange(false);
      startFlowDraw(prompt, navigate);
      return;
    }
    send.mutate();
  };

  const openResult = () => {
    if (!result?.url) return;
    onOpenChange(false);
    navigate(result.url);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Hand over</DialogTitle>
          <DialogDescription>
            {content?.title ? `"${content.title}" → another tool` : "Send this selection to another tool"}
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <div className="space-y-4">
            <p className="text-sm text-fg">
              Sent to <span className="font-medium">{result.label ?? target?.label ?? result.target}</span>.
            </p>
            <DialogFooter>
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                Close
              </Button>
              {result.url && <Button onClick={openResult}>Open</Button>}
            </DialogFooter>
          </div>
        ) : (
          <div className="space-y-4">
            {targetsQuery.isLoading && (
              <p className="text-sm text-fg-muted">Finding targets…</p>
            )}
            {targetsQuery.isError && (
              <p className="text-sm text-error">
                Could not load targets: {(targetsQuery.error as Error).message}
              </p>
            )}
            {!targetsQuery.isLoading && targets.length === 0 && (
              <p className="text-sm text-fg-muted">No tool accepts this selection.</p>
            )}

            <div className="flex flex-wrap gap-2">
              {targets.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => {
                    setTargetId(t.id);
                    setInputs({});
                    setErrorMsg(null);
                    setMissingKeys(new Set());
                  }}
                  className={cn(
                    "flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm transition-colors",
                    targetId === t.id
                      ? "border-accent bg-accent-subtle text-accent"
                      : "border-border-subtle text-fg-muted hover:bg-surface-elevated hover:text-fg",
                  )}
                >
                  <TargetIcon name={t.icon} />
                  <span>{t.label}</span>
                </button>
              ))}
            </div>

            {target && fields.length > 0 && (
              <div className="space-y-3">
                {fields.map((f) => (
                  <div key={f.key}>
                    <FieldLabel>
                      {f.label}
                      {f.mode === "optional" ? " (optional)" : ""}
                    </FieldLabel>
                    {f.type === "recipient" ? (
                      <RecipientField
                        value={inputs[f.key] as RecipientValue | undefined}
                        onChange={(v) => setField(f.key, v)}
                        mode={recipientMode}
                        onModeChange={setRecipientMode}
                        people={people}
                        peopleLoading={peopleQuery.isLoading}
                        groups={groups}
                        groupsLoading={groupsQuery.isLoading}
                        invalid={missingKeys.has(f.key)}
                      />
                    ) : f.type === "select" ? (
                      <Select
                        value={(inputs[f.key] as string) || ""}
                        onValueChange={(v) => setField(f.key, v)}
                      >
                        <SelectTrigger
                          className={cn("w-full", missingKeys.has(f.key) && "border-error")}
                        >
                          <SelectValue placeholder={f.help || `Pick ${f.label.toLowerCase()}`} />
                        </SelectTrigger>
                        <SelectContent>
                          {(f.options ?? []).map((opt) => (
                            <SelectItem key={opt} value={opt}>
                              {opt}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : (
                      <Input
                        value={(inputs[f.key] as string) ?? ""}
                        onChange={(e) => setField(f.key, e.target.value)}
                        placeholder={f.help || f.label}
                        aria-invalid={missingKeys.has(f.key)}
                      />
                    )}
                    {f.help && (
                      <p className="mt-1 text-2xs text-fg-subtle">{f.help}</p>
                    )}
                  </div>
                ))}
              </div>
            )}

            {errorMsg && (
              <p className="rounded border border-error/30 bg-error/5 p-2 text-xs text-error">
                {errorMsg}
              </p>
            )}

            <DialogFooter>
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button onClick={onSend} disabled={!canSend || send.isPending}>
                {send.isPending ? "Sending…" : isStreamFlow ? "Draw live" : "Send"}
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
