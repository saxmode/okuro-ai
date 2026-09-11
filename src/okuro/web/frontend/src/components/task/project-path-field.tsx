/**
 * Inline-editable display of a task's ``project_path`` — the filesystem
 * directory where the task realizes its work.
 *
 * Renders right above the PreviewButton on task-detail. The preview
 * proposer scans this path FIRST. Subagents declare it via
 * brief['project_path'] in their role-handover; this component is the
 * user-edit path that the SPA's "did you mean?" picker uses for legacy
 * tasks where no path was declared.
 *
 * Display logic:
 *   - When set: show the path as a small monospaced label, click-to-edit
 *   - When unset: show a discreet "Set project path" prompt + ranked
 *     candidate pills (build-marker > frequency) fetched from
 *     /api/tasks/{id}/project-path/suggestions
 *   - Edit mode: text input + save / cancel; empty save clears the field
 *
 * Validation lives server-side (PUT /api/tasks/{id}/project-path rejects
 * non-existent dirs); this component just surfaces the error toast.
 */

import { useEffect, useRef, useState } from "react";
import { Folder, Pencil, X, Check, Sparkles, GitBranch } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";
import { taskApi } from "@/lib/api";

interface ProjectPathFieldProps {
  taskId: string;
  value: string;
}

interface PathSuggestion {
  path: string;
  hits: number;
  build_markers: string[];
  has_git: boolean;
}

export function ProjectPathField({ taskId, value }: ProjectPathFieldProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();

  // Fetch ranked candidates only when no path is set — never hits the
  // server for tasks that already have a declared project_path. Same
  // 30-second stale time as other task-shape data; the suggestions are
  // expensive to compute (mines activity.jsonl) and rarely change.
  const { data: suggestionsData } = useQuery({
    queryKey: ["projectPathSuggestions", taskId],
    queryFn: () => taskApi.projectPathSuggestions(taskId, 3),
    enabled: !value && !editing,
    staleTime: 30_000,
  });
  const suggestions: PathSuggestion[] = suggestionsData?.suggestions ?? [];

  useEffect(() => {
    setDraft(value);
  }, [value]);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  const persist = async (next: string) => {
    if (next === value) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await taskApi.setProjectPath(taskId, next);
      // Invalidate so PreviewButton re-runs detection against the new path,
      // and so the suggestions query reruns (it short-circuits when value
      // is non-empty, but the cache key still needs to refresh on path
      // changes for any future "show alternatives" flow).
      await queryClient.invalidateQueries({ queryKey: ["taskState", taskId] });
      await queryClient.invalidateQueries({ queryKey: ["preview", taskId, "status"] });
      await queryClient.invalidateQueries({ queryKey: ["projectPathSuggestions", taskId] });
      setEditing(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to save project path");
    } finally {
      setSaving(false);
    }
  };

  const save = () => persist(draft.trim());

  const pickSuggestion = (path: string) => persist(path);

  const cancel = () => {
    setDraft(value);
    setEditing(false);
  };

  if (editing) {
    return (
      <div className="flex w-full items-center gap-1 text-2xs">
        <Folder className="h-3 w-3 shrink-0 text-tertiary" />
        <input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void save();
            else if (e.key === "Escape") cancel();
          }}
          placeholder="/absolute/path/to/project"
          className="min-w-0 flex-1 rounded border border-border bg-surface-elevated/40 px-1.5 py-1 font-mono text-2xs text-fg outline-none focus:border-accent"
        />
        <Button
          variant="ghost"
          size="sm"
          onClick={() => void save()}
          disabled={saving}
          aria-label="Save project path"
          className="h-6 w-6 p-0"
        >
          <Check className="h-3 w-3" />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={cancel}
          disabled={saving}
          aria-label="Cancel"
          className="h-6 w-6 p-0"
        >
          <X className="h-3 w-3" />
        </Button>
      </div>
    );
  }

  return (
    <div className="flex w-full flex-col gap-1">
      <button
        type="button"
        onClick={() => setEditing(true)}
        className="group flex w-full items-center gap-1 rounded px-1 py-0.5 text-left text-2xs text-tertiary hover:bg-surface-elevated/40 hover:text-fg"
        title={value || "No project path declared — pick a candidate below or enter one"}
      >
        <Folder className="h-3 w-3 shrink-0" />
        <span className="min-w-0 flex-1 truncate font-mono">
          {value || "Set project path…"}
        </span>
        <Pencil className="h-3 w-3 shrink-0 opacity-0 transition-opacity group-hover:opacity-100" />
      </button>
      {!value && suggestions.length > 0 && (
        <SuggestionList
          suggestions={suggestions}
          onPick={pickSuggestion}
          disabled={saving}
        />
      )}
    </div>
  );
}

interface SuggestionListProps {
  suggestions: PathSuggestion[];
  onPick: (path: string) => void;
  disabled: boolean;
}

/**
 * Ranked candidate paths rendered as one-click pills with provenance
 * (build-marker count + git presence + activity hits). One click
 * persists the choice — no confirm dialog because the action is
 * trivially reversible (the same field re-edits the path).
 */
function SuggestionList({ suggestions, onPick, disabled }: SuggestionListProps) {
  return (
    <div className="flex flex-col gap-1 rounded border border-border bg-surface-elevated/20 p-1.5">
      <div className="flex items-center gap-1 text-3xs uppercase tracking-wider text-tertiary">
        <Sparkles className="h-2.5 w-2.5" />
        <span>Candidates from task signals</span>
      </div>
      {suggestions.map((s) => (
        <button
          key={s.path}
          type="button"
          onClick={() => onPick(s.path)}
          disabled={disabled}
          className="group flex items-start gap-1.5 rounded px-1.5 py-1 text-left text-2xs hover:bg-surface-elevated/60 disabled:cursor-not-allowed disabled:opacity-50"
          title={`Set as project_path: ${s.path}`}
        >
          <Folder className="mt-0.5 h-3 w-3 shrink-0 text-tertiary" />
          <div className="min-w-0 flex-1">
            <div className="truncate font-mono text-fg">{s.path}</div>
            <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-3xs text-tertiary">
              <span>{s.hits} signal{s.hits === 1 ? "" : "s"}</span>
              {s.has_git && (
                <span className="flex items-center gap-0.5">
                  <GitBranch className="h-2.5 w-2.5" />
                  git
                </span>
              )}
              {s.build_markers.length > 0 && (
                <span className="rounded bg-surface-elevated/40 px-1 py-0.5 font-mono text-[10px] text-fg-muted">
                  {s.build_markers.slice(0, 2).join(", ")}
                  {s.build_markers.length > 2 ? ` +${s.build_markers.length - 2}` : ""}
                </span>
              )}
            </div>
          </div>
        </button>
      ))}
    </div>
  );
}
