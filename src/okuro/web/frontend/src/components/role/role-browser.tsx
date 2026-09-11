import { useQueryClient } from "@tanstack/react-query";
import { roleApi } from "@/lib/api";
import { formatAge } from "@/lib/format";
import type { RoleInfo } from "@/types/api";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface RoleBrowserProps {
  roles: RoleInfo[];
  onSelectRole: (roleId: string) => void;
  selectedRole?: string;
}

const TIERS = ["fast", "standard", "strategic"] as const;
const MODELS = ["haiku", "sonnet", "opus"] as const;

/**
 * Domain-grouped role list with inline tier/model editing.
 */
export function RoleBrowser({
  roles,
  onSelectRole,
  selectedRole,
}: RoleBrowserProps) {
  const queryClient = useQueryClient();

  // Group by domain
  const grouped = roles.reduce<Record<string, RoleInfo[]>>((acc, r) => {
    (acc[r.domain] ??= []).push(r);
    return acc;
  }, {});

  const domains = Object.keys(grouped).sort();

  const handleTierChange = async (roleId: string, tier: string) => {
    await roleApi.update(roleId, { tier } as Partial<RoleInfo>);
    queryClient.invalidateQueries({ queryKey: ["roles"] });
  };

  const handleModelChange = async (roleId: string, model: string) => {
    await roleApi.update(roleId, { model } as Partial<RoleInfo>);
    queryClient.invalidateQueries({ queryKey: ["roles"] });
  };

  return (
    <div className="space-y-6">
      <LegendRow />
      {domains.map((domain) => (
        <div key={domain}>
          <h3 className="mb-1.5 text-2xs font-medium uppercase tracking-wider text-tertiary">
            {domain}
            <span className="ml-1.5 text-tertiary">
              {grouped[domain]!.length}
            </span>
          </h3>
          <div className="space-y-0.5">
            {grouped[domain]!.map((role) => (
              <RoleRow
                key={role.id}
                role={role}
                selected={role.id === selectedRole}
                onSelect={() => onSelectRole(role.id)}
                onTierChange={(t) => handleTierChange(role.id, t)}
                onModelChange={(m) => handleModelChange(role.id, m)}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function LegendRow() {
  return (
    <div
      className="flex items-center gap-2 border-b border-border-subtle px-2 pb-2 text-xs font-medium text-fg-subtle"
      aria-hidden="true"
    >
      <span className="h-1.5 w-1.5 shrink-0" />
      <span className="flex-1 truncate">Role</span>
      <span className="shrink-0 w-6 text-right" title="Knowledge entries">
        Entries
      </span>
      <span className="min-w-[140px] text-center">Tier</span>
      <span className="min-w-[110px] text-center">Model</span>
      <span className="w-8 text-right" title="Session count">
        Sess.
      </span>
      <span className="w-8 text-right" title="Learning count">
        Learn
      </span>
      <span className="hidden w-20 text-right md:inline">Refreshed</span>
    </div>
  );
}

function RoleRow({
  role,
  selected,
  onSelect,
  onTierChange,
  onModelChange,
}: {
  role: RoleInfo;
  selected: boolean;
  onSelect: () => void;
  onTierChange: (tier: string) => void;
  onModelChange: (model: string) => void;
}) {
  return (
    <div
      className={`flex items-center gap-2 rounded px-2 py-1.5 transition-colors cursor-pointer ${
        selected
          ? "bg-accent-subtle border border-accent/30"
          : "hover:bg-surface-elevated border border-transparent"
      }`}
      onClick={onSelect}
      data-stale={role.stale ? "true" : "false"}
    >
      {/* Stale indicator dot */}
      <span
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${
          role.stale ? "bg-error" : "bg-success/60"
        }`}
        title={
          role.stale
            ? role.last_maintained
              ? `Stale since ${formatAge(role.last_maintained)}`
              : "Stale (never maintained)"
            : "Fresh"
        }
      />
      <span className="flex-1 truncate text-sm text-fg">{role.id}</span>

      {/* Knowledge count */}
      <span
        className="shrink-0 w-6 text-right text-3xs text-tertiary tabular-nums"
        title={`${role.knowledge_count ?? 0} knowledge entries`}
      >
        {role.knowledge_count ?? 0}
      </span>

      {/* Inline tier select — widened so `strategic` fits without truncating */}
      <Select value={role.tier} onValueChange={onTierChange}>
        <SelectTrigger
          className="h-7 min-w-[140px] border-border bg-surface text-xs text-fg-muted"
          onClick={(e) => e.stopPropagation()}
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {TIERS.map((t) => (
            <SelectItem key={t} value={t} className="text-xs">
              {t}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {/* Inline model select */}
      <Select value={role.model} onValueChange={onModelChange}>
        <SelectTrigger
          className="h-7 min-w-[110px] border-border bg-surface text-xs text-fg-muted"
          onClick={(e) => e.stopPropagation()}
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {MODELS.map((m) => (
            <SelectItem key={m} value={m} className="text-xs">
              {m}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {/* Stats */}
      <span
        className="text-3xs text-tertiary w-8 text-right tabular-nums"
        title={`${role.sessions} sessions`}
      >
        {role.sessions}
      </span>
      <span
        className="text-3xs text-tertiary w-8 text-right tabular-nums"
        title={`${role.learnings} learnings`}
      >
        {role.learnings}
      </span>
      {/* Last maintained */}
      <span className="hidden w-20 text-right text-3xs text-tertiary md:inline">
        {role.last_maintained ? formatAge(role.last_maintained) : "—"}
      </span>
    </div>
  );
}
