import { forwardRef, useCallback, useImperativeHandle, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Loader2,
  Pencil,
  Wrench,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { cn } from "@/lib/utils";
import type { ApprovalDecision, ToolCard, ToolCardState } from "@/types/inline";

const STATE_TONE: Record<
  ToolCardState,
  "success" | "warning" | "error" | "info" | "neutral" | "accent"
> = {
  "pending-approval": "warning",
  running: "accent",
  done: "neutral",
  denied: "error",
  error: "error",
};

const STATE_LABEL: Record<ToolCardState, string> = {
  "pending-approval": "Awaiting approval",
  running: "Running",
  done: "Done",
  denied: "Denied",
  error: "Error",
};

const COLLAPSE_AFTER = 5;

export interface ToolCardStackHandle {
  scrollTo: (call_id: string) => void;
}

interface ToolCardStackProps {
  cards: ToolCard[];
  onDecide: (
    call_id: string,
    decision: ApprovalDecision,
    edited_args?: Record<string, unknown>,
  ) => Promise<void>;
}

/**
 * Right-column stack of tool-call cards. Cards older than the latest
 * five are collapsed by default to keep the column scannable.
 */
export const ToolCardStack = forwardRef<ToolCardStackHandle, ToolCardStackProps>(
  function ToolCardStack({ cards, onDecide }, ref) {
    const rowsRef = useRef<Map<string, HTMLDivElement>>(new Map());
    useImperativeHandle(
      ref,
      () => ({
        scrollTo: (call_id) => {
          const el = rowsRef.current.get(call_id);
          el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
        },
      }),
      [],
    );

    const setRowRef = useCallback(
      (call_id: string) => (el: HTMLDivElement | null) => {
        if (el) rowsRef.current.set(call_id, el);
        else rowsRef.current.delete(call_id);
      },
      [],
    );

    if (cards.length === 0) {
      return (
        <aside className="flex h-full w-[56rem] shrink-0 border-l border-border-subtle bg-surface-subtle px-6 py-6 text-xs text-tertiary">
          <div className="mx-auto max-w-[36rem] text-center">
            <Wrench className="mx-auto mb-3 h-6 w-6 opacity-40" aria-hidden />
            No tool calls yet. When the assistant invokes a tool it will
            appear here.
          </div>
        </aside>
      );
    }

    return (
      <aside className="flex h-full w-[56rem] shrink-0 flex-col border-l border-border-subtle bg-surface-subtle">
        <div className="border-b border-border-subtle px-6 py-3 text-3xs uppercase tracking-widest text-tertiary">
          Tool calls · {cards.length}
        </div>
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
          {cards.map((card, idx) => {
            const collapsibleDefault =
              idx < cards.length - COLLAPSE_AFTER &&
              card.state !== "pending-approval";
            return (
              <ToolCardRow
                key={card.call_id}
                card={card}
                onDecide={onDecide}
                defaultCollapsed={collapsibleDefault}
                ref={setRowRef(card.call_id)}
              />
            );
          })}
        </div>
      </aside>
    );
  },
);

interface ToolCardRowProps {
  card: ToolCard;
  onDecide: ToolCardStackProps["onDecide"];
  defaultCollapsed: boolean;
  ref?: React.Ref<HTMLDivElement>;
}

function ToolCardRow({
  card,
  onDecide,
  defaultCollapsed,
  ref,
}: ToolCardRowProps) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const [busy, setBusy] = useState<ApprovalDecision | null>(null);
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState(() =>
    JSON.stringify(card.args, null, 2),
  );
  const [editError, setEditError] = useState<string | null>(null);

  const fire = useCallback(
    async (decision: ApprovalDecision, edited?: Record<string, unknown>) => {
      setBusy(decision);
      try {
        await onDecide(card.call_id, decision, edited);
      } catch (e) {
        setEditError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [card.call_id, onDecide],
  );

  const submitEdit = useCallback(async () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(editValue);
    } catch (e) {
      setEditError(
        e instanceof Error ? `invalid JSON: ${e.message}` : "invalid JSON",
      );
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      setEditError("args must be a JSON object");
      return;
    }
    setEditError(null);
    setEditing(false);
    await fire("edit", parsed as Record<string, unknown>);
  }, [editValue, fire]);

  const argsPreview = useMemo(() => previewArgs(card.args), [card.args]);

  return (
    <div
      ref={ref}
      className={cn(
        "rounded-md border bg-surface-elevated",
        card.state === "pending-approval"
          ? "border-warning/50 shadow-[0_0_0_1px_rgba(255,167,38,0.2)]"
          : card.state === "denied" || card.state === "error"
            ? "border-error/40"
            : "border-border-subtle",
      )}
    >
      <header
        className="flex cursor-pointer items-center gap-2 px-3 py-2"
        onClick={() => setCollapsed((c) => !c)}
      >
        <button
          type="button"
          aria-label={collapsed ? "Expand" : "Collapse"}
          className="text-tertiary hover:text-fg"
          onClick={(e) => {
            e.stopPropagation();
            setCollapsed((c) => !c);
          }}
        >
          {collapsed ? (
            <ChevronRight className="h-3.5 w-3.5" aria-hidden />
          ) : (
            <ChevronDown className="h-3.5 w-3.5" aria-hidden />
          )}
        </button>
        <Wrench className="h-3.5 w-3.5 text-tertiary" aria-hidden />
        <span className="flex-1 truncate text-xs text-fg font-medium">
          {card.tool_name}
        </span>
        <StatusBadge tone={STATE_TONE[card.state]} label={STATE_LABEL[card.state]} />
      </header>

      {!collapsed && (
        <div className="space-y-2 border-t border-border-subtle px-3 py-3">
          <Meta label="Tier" value={card.tier} />
          {card.duration_ms != null && (
            <Meta label="Duration" value={`${card.duration_ms} ms`} />
          )}

          {editing ? (
            <div className="space-y-2">
              <textarea
                value={editValue}
                onChange={(e) => setEditValue(e.target.value)}
                rows={Math.min(16, Math.max(4, editValue.split("\n").length))}
                className={cn(
                  "w-full rounded-md border border-border bg-surface px-2 py-1.5 font-mono text-xs",
                  "focus-visible:outline-none focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-accent/30",
                )}
                spellCheck={false}
              />
              {editError && (
                <p className="text-3xs text-error">{editError}</p>
              )}
              <div className="flex gap-2">
                <Button size="xs" variant="default" onClick={submitEdit}>
                  Submit edit
                </Button>
                <Button
                  size="xs"
                  variant="ghost"
                  onClick={() => {
                    setEditing(false);
                    setEditError(null);
                    setEditValue(JSON.stringify(card.args, null, 2));
                  }}
                >
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <details className="text-xs">
              <summary className="cursor-pointer text-tertiary hover:text-fg">
                Args ({argsPreview})
              </summary>
              <pre className="mt-2 max-h-64 overflow-auto rounded bg-surface px-2 py-1.5 font-mono text-3xs text-fg-muted">
                {JSON.stringify(card.args, null, 2)}
              </pre>
            </details>
          )}

          {card.error && (
            <p className="rounded bg-error/10 px-2 py-1 text-3xs text-error">
              {card.error}
            </p>
          )}

          {card.state === "pending-approval" && !editing && (
            <div className="flex flex-wrap gap-2 pt-1">
              <Button
                size="xs"
                variant="default"
                onClick={() => fire("approve")}
                disabled={busy !== null}
              >
                {busy === "approve" ? (
                  <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                ) : (
                  <Check className="h-3 w-3" aria-hidden />
                )}
                Approve
              </Button>
              <Button
                size="xs"
                variant="destructive"
                onClick={() => fire("deny")}
                disabled={busy !== null}
              >
                {busy === "deny" ? (
                  <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                ) : (
                  <X className="h-3 w-3" aria-hidden />
                )}
                Deny
              </Button>
              <Button
                size="xs"
                variant="outline"
                onClick={() => {
                  setEditing(true);
                  setEditValue(JSON.stringify(card.args, null, 2));
                  setEditError(null);
                }}
                disabled={busy !== null}
              >
                <Pencil className="h-3 w-3" aria-hidden />
                Edit args
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-3xs uppercase tracking-widest text-tertiary">
        {label}
      </span>
      <span className="font-mono text-3xs text-fg-muted">{value}</span>
    </div>
  );
}

function previewArgs(args: Record<string, unknown>): string {
  const keys = Object.keys(args);
  if (keys.length === 0) return "no args";
  if (keys.length <= 3) return keys.join(", ");
  return `${keys.slice(0, 3).join(", ")} +${keys.length - 3}`;
}
