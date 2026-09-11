/**
 * EnrichPanel — the three profile-population paths, unified.
 *
 * Presented inside RightPanel when a person node is selected. All three
 * paths write through the same /api/people/{id}/sources or apply-preset,
 * and surface via /api/people/{id}/sources for the provenance chip row.
 *
 *  1. Drop files (eml / pdf / txt / md)  → peopleApi.ingestSourceFile
 *  2. Paste notes / description           → peopleApi.ingestSourceText
 *  3. Generate a questionnaire link       → peopleApi.mintQuestionnaire
 *
 * Chips show the most recent applied sources so the user can see what
 * is actually driving the profile.
 */

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Loader2,
  Upload,
  Link as LinkIcon,
  Check,
  Wand2,
  FileText,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/toast";
import { peopleApi, type GraphNode } from "@/lib/people-api";

function groupLabel(source_type: string, source_ref: string | null): string {
  if (source_type === "preset") return `${source_ref || "role"} preset`;
  if (source_type === "questionnaire") return "self-report survey";
  return source_ref || source_type;
}

// Removable list of sources (uploaded files, presets, surveys). Each row can
// be deleted; the server reverts the fields that source set.
function SourceList({
  personId,
  onChange,
}: {
  personId: string;
  onChange: () => void;
}) {
  const [armed, setArmed] = useState<string | null>(null);
  const q = useQuery({
    queryKey: ["people", "source-groups", personId],
    queryFn: () => peopleApi.listSourceGroups(personId),
  });

  const removeMut = useMutation({
    mutationFn: (g: { source_type: string; source_ref: string | null }) =>
      peopleApi.removeSource(personId, g.source_type, g.source_ref),
    onSuccess: (data) => {
      toast.success(
        `Removed — reverted ${data.fields_reverted.length} field${data.fields_reverted.length === 1 ? "" : "s"}`,
      );
      setArmed(null);
      onChange();
    },
    onError: (err: Error) => toast.error(err.message || "Remove failed"),
  });

  const groups = q.data?.groups || [];
  if (q.isPending) return null;
  if (groups.length === 0) {
    return (
      <div className="text-[10px] text-fg-subtle">
        No sources yet — drop a file or share the questionnaire.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      {groups.map((g) => {
        const key = `${g.source_type}:${g.source_ref}`;
        const isArmed = armed === key;
        const pending = removeMut.isPending && removeMut.variables
          ? removeMut.variables.source_type === g.source_type &&
            removeMut.variables.source_ref === g.source_ref
          : false;
        return (
          <div
            key={key}
            className="flex items-center gap-2 rounded border border-border bg-surface-subtle px-2 py-1.5 text-xs"
          >
            <FileText className="h-3.5 w-3.5 shrink-0 text-fg-subtle" />
            <div className="min-w-0 flex-1">
              <div className="truncate text-fg" title={groupLabel(g.source_type, g.source_ref)}>
                {groupLabel(g.source_type, g.source_ref)}
              </div>
              <div className="text-[10px] text-fg-subtle">
                {g.source_type} · {g.field_count} field
                {g.field_count === 1 ? "" : "s"} · {g.latest_at?.slice(0, 10)}
              </div>
            </div>
            {isArmed ? (
              <div className="flex shrink-0 items-center gap-1">
                <button
                  className="rounded px-1.5 py-0.5 text-[10px] text-destructive hover:bg-destructive/10"
                  disabled={pending}
                  onClick={() =>
                    removeMut.mutate({
                      source_type: g.source_type,
                      source_ref: g.source_ref,
                    })
                  }
                >
                  {pending ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    "Remove?"
                  )}
                </button>
                <button
                  className="rounded px-1.5 py-0.5 text-[10px] text-fg-subtle hover:bg-surface"
                  disabled={pending}
                  onClick={() => setArmed(null)}
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button
                className="shrink-0 rounded p-1 text-fg-subtle hover:bg-destructive/10 hover:text-destructive"
                title="Remove this source"
                onClick={() => setArmed(key)}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function EnrichPanel({ person }: { person: GraphNode }) {
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [notes, setNotes] = useState("");
  const [copied, setCopied] = useState(false);

  const invalidate = () => {
    queryClient.invalidateQueries({
      queryKey: ["people", "sources", person.id],
    });
    queryClient.invalidateQueries({
      queryKey: ["people", "source-groups", person.id],
    });
    queryClient.invalidateQueries({ queryKey: ["people", "graph"] });
    queryClient.invalidateQueries({ queryKey: ["people", "lens", person.id] });
  };

  const fileMut = useMutation({
    mutationFn: (file: File) => peopleApi.ingestSourceFile(person.id, file),
    onSuccess: (data) => {
      if (data.sources_written === 0) {
        toast.warning(data.note || "No signal extracted from the file");
      } else {
        toast.success(
          `Updated ${data.fields_changed.length} field${data.fields_changed.length === 1 ? "" : "s"} from ${data.source_ref || "file"}`,
        );
      }
      invalidate();
    },
    onError: (err: Error) => toast.error(err.message || "Upload failed"),
  });

  const notesMut = useMutation({
    mutationFn: () =>
      peopleApi.ingestSourceText(person.id, notes, "notes", "pasted-notes"),
    onSuccess: (data) => {
      if (data.sources_written === 0) {
        toast.warning(data.note || "No signal extracted");
      } else {
        toast.success(`Updated ${data.fields_changed.length} fields from notes`);
        setNotes("");
      }
      invalidate();
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const mintMut = useMutation({
    mutationFn: () => peopleApi.mintQuestionnaire(person.id),
    onSuccess: async (data) => {
      const origin =
        typeof window !== "undefined" ? window.location.origin : "";
      const full = `${origin}${data.share_url}`;
      try {
        await navigator.clipboard.writeText(full);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
        toast.success(
          `Link copied — ${data.question_count} question${data.question_count === 1 ? "" : "s"}, expires ${data.expires_at?.slice(0, 10)}`,
        );
      } catch {
        toast.info(`Link: ${full}`);
      }
    },
    onError: (err: Error) => toast.error(err.message),
  });

  function onFilePick(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (f) fileMut.mutate(f);
    e.target.value = "";
  }

  function onDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    const f = e.dataTransfer.files?.[0];
    if (f) fileMut.mutate(f);
  }

  return (
    <div className="mt-4 flex flex-col gap-3 border-t border-border pt-4">
      <div className="text-[10px] font-medium uppercase tracking-wider text-fg-subtle">
        Enrich
      </div>

      <SourceList personId={person.id} onChange={invalidate} />

      {/* File drop */}
      <div
        className="flex items-center justify-between gap-2 rounded border border-dashed border-border bg-surface-subtle p-3 transition-colors hover:border-accent/40"
        onDragOver={(e) => e.preventDefault()}
        onDrop={onDrop}
      >
        <div className="flex items-center gap-2 text-xs text-fg-subtle">
          <FileText className="h-3.5 w-3.5" />
          <span>Drop .eml / .pdf / .txt — I'll extract a profile delta</span>
        </div>
        <Button
          variant="outline"
          size="xs"
          onClick={() => fileInputRef.current?.click()}
          disabled={fileMut.isPending}
        >
          {fileMut.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Upload className="h-3 w-3" />
          )}
          {fileMut.isPending ? "…" : "Pick file"}
        </Button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".eml,.pdf,.txt,.md"
          className="hidden"
          onChange={onFilePick}
        />
      </div>

      {/* Notes paste */}
      <div>
        <Textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Paste anything — a thread, a description, a venting note. I'll turn it into profile signal."
          rows={3}
          className="text-xs"
        />
        <div className="mt-1 flex justify-end">
          <Button
            size="xs"
            disabled={!notes.trim() || notesMut.isPending}
            onClick={() => notesMut.mutate()}
          >
            {notesMut.isPending ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <Wand2 className="h-3 w-3" />
            )}
            Extract from notes
          </Button>
        </div>
      </div>

      {/* Questionnaire mint */}
      <div className="flex items-center justify-between gap-2 rounded border border-border bg-surface-subtle p-2.5">
        <div className="text-[11px] text-fg-muted">
          Ask them directly — mint a self-report link
        </div>
        <Button
          variant="outline"
          size="xs"
          onClick={() => mintMut.mutate()}
          disabled={mintMut.isPending}
        >
          {mintMut.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : copied ? (
            <Check className="h-3 w-3" />
          ) : (
            <LinkIcon className="h-3 w-3" />
          )}
          {mintMut.isPending ? "…" : copied ? "Copied" : "Copy link"}
        </Button>
      </div>
    </div>
  );
}
