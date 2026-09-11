import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { roleApi } from "@/lib/api";
import type { RoleDetail } from "@/types/api";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import { MarkdownContent } from "@/components/ui/markdown-content";
import { KnowledgePanel } from "@/components/roles/knowledge-panel";
import { MaintenancePanel } from "@/components/roles/maintenance-panel";

type GradeTab =
  | "full"
  | "lean"
  | "micro"
  | "entries"
  | "maintenance";

const TABS: { key: GradeTab; label: string }[] = [
  { key: "full", label: "FULL" },
  { key: "lean", label: "LEAN" },
  { key: "micro", label: "MICRO" },
  { key: "entries", label: "KNOWLEDGE" },
  { key: "maintenance", label: "MAINTENANCE" },
];

interface RoleViewerProps {
  roleId: string;
  onClose: () => void;
}

/**
 * Full-screen role viewer with grade tabs and inline editing.
 */
export function RoleViewer({ roleId, onClose }: RoleViewerProps) {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<GradeTab>("full");
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [saving, setSaving] = useState(false);

  const { data: role, isLoading } = useQuery({
    queryKey: ["role", roleId],
    queryFn: () => roleApi.get(roleId),
  });

  const getContent = (r: RoleDetail): string => {
    switch (tab) {
      case "full":
        return r.prompt ?? "(no full prompt)";
      case "lean":
        return r.lean_prompt ?? "(no lean prompt)";
      case "micro":
        return r.micro_prompt ?? "(no micro prompt)";
      default:
        return "";
    }
  };

  const isMarkdownTab =
    tab === "full" || tab === "lean" || tab === "micro";

  const startEdit = () => {
    if (!role) return;
    setEditContent(getContent(role));
    setEditing(true);
  };

  const handleSave = async () => {
    if (!role || !isMarkdownTab) return;
    setSaving(true);
    const field =
      tab === "full" ? "prompt" : tab === "lean" ? "lean_prompt" : "micro_prompt";
    await roleApi.update(roleId, { [field]: editContent });
    queryClient.invalidateQueries({ queryKey: ["role", roleId] });
    queryClient.invalidateQueries({ queryKey: ["roles"] });
    setEditing(false);
    setSaving(false);
  };

  const handleCancel = () => {
    setEditing(false);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-surface/95 backdrop-blur-sm">
      <div className="flex h-[85vh] w-[75vw] flex-col rounded border border-border bg-surface-elevated">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="flex items-center gap-3">
            <span className="text-sm font-bold text-fg">{roleId}</span>
            {role && (
              <>
                <span className="text-2xs text-tertiary">
                  {role.domain}
                </span>
                <span className="text-2xs text-tertiary">
                  {role.tier} / {role.model}
                </span>
                <span className="text-2xs text-tertiary">
                  {role.sessions} sessions, {role.learnings} learnings
                </span>
              </>
            )}
          </div>
          <div className="flex items-center gap-2">
            {!editing && isMarkdownTab && (
              <Button variant="outline" size="sm" onClick={startEdit} className="text-xs">
                Edit
              </Button>
            )}
            {editing && (
              <>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleCancel}
                  className="text-xs"
                >
                  Cancel
                </Button>
                <Button
                  size="sm"
                  onClick={handleSave}
                  disabled={saving}
                  className="text-xs"
                >
                  {saving ? "Saving..." : "Save"}
                </Button>
              </>
            )}
            <button
              onClick={onClose}
              aria-label="Close role viewer"
              className="text-tertiary hover:text-fg transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:rounded-sm"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
        </div>

        {/* Grade tabs */}
        <div className="flex border-b border-border px-4">
          {TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => {
                setTab(t.key);
                setEditing(false);
              }}
              className={`px-3 py-2 text-2xs font-medium uppercase tracking-wider transition-colors ${
                tab === t.key
                  ? "border-b-2 border-accent text-accent"
                  : "text-tertiary hover:text-fg-muted"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Content */}
        <div className="flex-1 min-h-0 p-4">
          {isLoading ? (
            <p className="text-sm text-tertiary">Loading...</p>
          ) : !role ? (
            <p className="text-sm text-tertiary">Role not found</p>
          ) : tab === "entries" ? (
            <KnowledgePanel roleId={roleId} />
          ) : tab === "maintenance" ? (
            <ScrollArea className="h-full">
              <MaintenancePanel roleId={roleId} role={role} />
            </ScrollArea>
          ) : editing ? (
            <Textarea
              value={editContent}
              onChange={(e) => setEditContent(e.target.value)}
              className="min-h-[400px] bg-surface font-mono text-xs text-fg"
            />
          ) : (
            <ScrollArea className="h-full">
              <MarkdownContent>{getContent(role)}</MarkdownContent>
            </ScrollArea>
          )}
        </div>

        {/* Description footer */}
        {role && (
          <div className="shrink-0 border-t border-border px-4 py-2">
            <p className="text-xs text-tertiary">{role.description}</p>
          </div>
        )}
      </div>
    </div>
  );
}
