import { useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { ExternalLink, Plus, Trash2 } from "lucide-react";
import { roleApi } from "@/lib/api";
import { formatAge } from "@/lib/format";
import type { RoleKnowledgeEntry } from "@/types/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const TYPES = ["research", "insight", "source", "decision", "pitfall"] as const;
type EntryType = (typeof TYPES)[number];

const TYPE_TONE: Record<string, string> = {
  research: "text-info",
  insight: "text-success",
  source: "text-warning",
  decision: "text-accent",
  pitfall: "text-error",
};

interface KnowledgePanelProps {
  roleId: string;
}

/**
 * Per-role knowledge browser.
 *
 * Lists entries (newest first), filters by type + min-confidence, lets the
 * user soft-delete or add new entries. Hides entries with confidence=0 by
 * default (soft-deleted rows) — raise the min-confidence slider to 0 to see
 * them.
 */
export function KnowledgePanel({ roleId }: KnowledgePanelProps) {
  const qc = useQueryClient();
  const [typeFilter, setTypeFilter] = useState<string>("all");
  const [minConfidence, setMinConfidence] = useState<number>(0.1);
  const [adding, setAdding] = useState(false);
  const [addType, setAddType] = useState<EntryType>("insight");
  const [addContent, setAddContent] = useState("");
  const [addSource, setAddSource] = useState("");
  const [addConfidence, setAddConfidence] = useState(0.7);

  const { data, isLoading } = useQuery({
    queryKey: ["role-knowledge", roleId, typeFilter, minConfidence],
    queryFn: () =>
      roleApi.listKnowledge(roleId, {
        type: typeFilter === "all" ? undefined : typeFilter,
        min_confidence: minConfidence,
        limit: 200,
      }),
    staleTime: 10_000,
  });

  const addMutation = useMutation({
    mutationFn: () =>
      roleApi.addKnowledge(roleId, {
        type: addType,
        content: addContent,
        source_url: addSource || undefined,
        confidence: addConfidence,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["role-knowledge", roleId] });
      qc.invalidateQueries({ queryKey: ["role", roleId] });
      qc.invalidateQueries({ queryKey: ["roles"] });
      setAdding(false);
      setAddContent("");
      setAddSource("");
    },
  });

  const delMutation = useMutation({
    mutationFn: (entryId: string) => roleApi.deleteKnowledge(roleId, entryId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["role-knowledge", roleId] });
      qc.invalidateQueries({ queryKey: ["role", roleId] });
    },
  });

  const entries = data ?? [];

  const handleDelete = (entryId: string, content: string) => {
    const preview = content.length > 60 ? content.slice(0, 60) + "…" : content;
    if (!window.confirm(`Soft-delete this entry?\n\n"${preview}"`)) return;
    delMutation.mutate(entryId);
  };

  return (
    <div className="flex h-full flex-col gap-3">
      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3 text-xs">
        <div className="flex items-center gap-2">
          <span className="text-2xs uppercase tracking-wider text-tertiary">
            Type
          </span>
          <Select value={typeFilter} onValueChange={setTypeFilter}>
            <SelectTrigger className="h-7 w-[120px] border-border bg-surface text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">all</SelectItem>
              {TYPES.map((t) => (
                <SelectItem key={t} value={t}>
                  {t}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-2xs uppercase tracking-wider text-tertiary">
            Min confidence
          </span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={minConfidence}
            onChange={(e) => setMinConfidence(parseFloat(e.target.value))}
            className="h-2 w-24 accent-accent"
          />
          <span className="w-8 text-right text-xs tabular-nums text-fg-muted">
            {minConfidence.toFixed(2)}
          </span>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <span className="text-2xs text-tertiary">
            {entries.length} entries
          </span>
          <Button
            size="sm"
            variant={adding ? "outline" : "default"}
            onClick={() => setAdding((a) => !a)}
            className="h-7 text-xs"
          >
            <Plus className="mr-1 h-3 w-3" />
            {adding ? "Cancel" : "Add entry"}
          </Button>
        </div>
      </div>

      {/* Add form */}
      {adding && (
        <div className="rounded border border-border bg-surface-elevated p-3 space-y-2">
          <div className="flex gap-2">
            <div className="flex-1">
              <label className="mb-1 block text-2xs uppercase tracking-wider text-tertiary">
                Type
              </label>
              <Select
                value={addType}
                onValueChange={(v) => setAddType(v as EntryType)}
              >
                <SelectTrigger className="h-7 border-border bg-surface text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TYPES.map((t) => (
                    <SelectItem key={t} value={t}>
                      {t}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex-1">
              <label className="mb-1 block text-2xs uppercase tracking-wider text-tertiary">
                Source URL (optional)
              </label>
              <Input
                value={addSource}
                onChange={(e) => setAddSource(e.target.value)}
                placeholder="https://…"
                className="h-7 bg-surface text-xs"
              />
            </div>
            <div>
              <label className="mb-1 block text-2xs uppercase tracking-wider text-tertiary">
                Confidence
              </label>
              <div className="flex h-7 items-center gap-2">
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={addConfidence}
                  onChange={(e) =>
                    setAddConfidence(parseFloat(e.target.value))
                  }
                  className="w-20 accent-accent"
                />
                <span className="w-8 text-right text-xs tabular-nums">
                  {addConfidence.toFixed(2)}
                </span>
              </div>
            </div>
          </div>
          <Textarea
            value={addContent}
            onChange={(e) => setAddContent(e.target.value)}
            placeholder="Learning content…"
            className="min-h-[80px] bg-surface text-xs"
          />
          <div className="flex justify-end">
            <Button
              size="sm"
              onClick={() => addMutation.mutate()}
              disabled={!addContent.trim() || addMutation.isPending}
              className="h-7 text-xs"
            >
              {addMutation.isPending ? "Saving…" : "Save entry"}
            </Button>
          </div>
        </div>
      )}

      {/* Entries */}
      <div className="flex-1 min-h-0 overflow-auto space-y-2">
        {isLoading ? (
          <p className="p-3 text-xs text-tertiary">Loading knowledge…</p>
        ) : entries.length === 0 ? (
          <p className="p-3 text-xs text-tertiary">
            No knowledge entries yet. Agents populate these via{" "}
            <code className="text-accent">roles_learn</code>.
          </p>
        ) : (
          entries.map((e) => (
            <EntryRow
              key={e.id}
              entry={e}
              onDelete={() => handleDelete(e.id, e.content)}
              deleting={delMutation.isPending}
            />
          ))
        )}
      </div>
    </div>
  );
}

/** Show just the host (e.g. "developer.mozilla.org") — the readable proof label. */
function proofHost(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function EntryRow({
  entry,
  onDelete,
  deleting,
}: {
  entry: RoleKnowledgeEntry;
  onDelete: () => void;
  deleting: boolean;
}) {
  const confidence = entry.confidence ?? 0;
  const confidenceLabel = confidence.toFixed(2);
  const confidenceTone =
    confidence >= 0.7
      ? "text-success"
      : confidence >= 0.3
        ? "text-warning"
        : "text-tertiary";

  return (
    <div
      className={`group rounded border border-border bg-surface-elevated/60 p-3 transition-colors ${
        confidence === 0 ? "opacity-50" : ""
      }`}
    >
      {/* Header: type + meta + delete */}
      <div className="mb-2 flex items-center gap-2">
        <span
          className={`shrink-0 rounded bg-surface px-1.5 py-0.5 text-3xs font-semibold uppercase tracking-wider ${
            TYPE_TONE[entry.type] ?? "text-fg-muted"
          }`}
        >
          {entry.type}
        </span>
        <span className={`text-3xs tabular-nums ${confidenceTone}`}>
          conf {confidenceLabel}
        </span>
        {entry.created_at && (
          <span className="text-3xs text-tertiary">
            {formatAge(entry.created_at)}
          </span>
        )}
        <button
          type="button"
          onClick={onDelete}
          disabled={deleting || confidence === 0}
          aria-label="Soft-delete entry"
          className="ml-auto text-tertiary opacity-0 transition-opacity hover:text-error group-hover:opacity-100 disabled:opacity-30"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* The knowledge — full, always visible, the star of the row */}
      <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-fg">
        {entry.content}
      </p>

      {/* Proof — clearly secondary; the knowledge above stands without it */}
      {entry.source_url && (
        <div className="mt-2 flex items-center gap-1.5 border-t border-border-subtle pt-2 text-3xs text-tertiary">
          <span className="uppercase tracking-wider">Proof</span>
          <a
            href={entry.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-0.5 text-info hover:underline"
            title={entry.source_url}
          >
            {proofHost(entry.source_url)}
            <ExternalLink className="h-2.5 w-2.5" />
          </a>
        </div>
      )}
    </div>
  );
}
