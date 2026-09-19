import { useQueryClient } from "@tanstack/react-query";
import { roleApi } from "@/lib/api";
import { formatAge } from "@/lib/format";
import type { RoleFitSummary, RoleInfo } from "@/types/api";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { FitBar, SEGMENT_LABEL, SEGMENT_MEANING, SEGMENT_ORDER } from "@/components/role/role-fit";

interface RoleBrowserProps {
  roles: RoleInfo[];
  onSelectRole: (roleId: string) => void;
  selectedRole?: string;
  /** Sort rows worst-fit first instead of alphabetically within a domain. */
  sortByWorstFit?: boolean;
}

/** A row's fit, or null when the API could not compute one for it. */
function fitOf(role: RoleInfo): RoleFitSummary | null {
  const fit = role.fit;
  if (!fit || "error" in fit) return null;
  return fit;
}

/** The knowledge chip's tooltip — the three buckets, not just the total. */
function knowledgeTitle(role: RoleInfo): string {
  const total = role.knowledge_count ?? 0;
  const fit = fitOf(role);
  if (!fit) return `${total} knowledge entries`;
  const { claimed, unverified, filler, suppressed, sourced_label } =
    fit.knowledge;
  return (
    `${total} knowledge entries\n` +
    `${claimed} ${sourced_label} · ${unverified} unverified · ` +
    `${filler} no-change filler · ${suppressed} suppressed`
  );
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
  sortByWorstFit = false,
}: RoleBrowserProps) {
  const queryClient = useQueryClient();

  // Group by domain
  const grouped = roles.reduce<Record<string, RoleInfo[]>>((acc, r) => {
    (acc[r.domain] ??= []).push(r);
    return acc;
  }, {});

  /* Sorting by the WORST segment, not by the mean. The mean of a structurally
     perfect role with no knowledge and a broken role with good knowledge is
     the same number, and only one of them needs work today. A role whose fit
     could not be computed sorts last rather than first — an error is not
     evidence of a bad role. */
  if (sortByWorstFit) {
    for (const domain of Object.keys(grouped)) {
      grouped[domain]!.sort((a, b) => {
        const fa = fitOf(a);
        const fb = fitOf(b);
        if (!fa && !fb) return a.id.localeCompare(b.id);
        if (!fa) return 1;
        if (!fb) return -1;
        /* A role whose segments could not be measured has no worst segment.
           It sorts last, above 100, rather than first: nothing measured is
           not evidence of a bad role. */
        const key = (n: number | null) => (n === null ? 101 : n);
        return (
          key(fa.worst_score) - key(fb.worst_score) ||
          key(fa.overall) - key(fb.overall) ||
          a.id.localeCompare(b.id)
        );
      });
    }
  }

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
          <h3 className="mb-1.5 text-2xs font-medium case-label tracking-wider text-tertiary">
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
      {/* THE HEADER LABEL WAS WIDER THAN THE COLUMN IT NAMES, and with
          `overflow: visible` it printed itself over its neighbour rather than
          being clipped — measured "Entries" at 56px inside a 28px box. The
          column is sized for a COUNT, so the label is abbreviated to match and
          keeps the full text in `title`. Same for Sess. and Learn below. */}
      <span className="w-6 shrink-0 truncate text-right" title="Knowledge entries">
        Ent.
      </span>
      <span
        className="w-9 shrink-0 truncate text-center"
        title={SEGMENT_ORDER.map(
          (s) => `${SEGMENT_LABEL[s]}: ${SEGMENT_MEANING[s]}`,
        ).join("\n\n")}
      >
        Fit
      </span>
      <span className="min-w-[140px] text-center">Tier</span>
      <span className="min-w-[110px] text-center">Model</span>
      <span className="w-8 shrink-0 truncate text-right" title="Session count">
        Ses.
      </span>
      <span className="w-8 shrink-0 truncate text-right" title="Learning count">
        Lrn.
      </span>
      {/* "Refreshed" read as a quality signal and is not one: the sweep resets
          this clock by writing a no-change row, so green here means the cron
          ran, not that the role is current. The quality answer is the Fit
          column. Relabelled to say only what it measures. */}
      <span
        className="hidden w-20 text-right @2xl:inline"
        title="When the maintenance sweep last ran on this role. Not a quality signal — the sweep resets this clock even when it found nothing."
      >
        Sweep ran
      </span>
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
  const fit = fitOf(role);
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
        title={knowledgeTitle(role)}
      >
        {role.knowledge_count ?? 0}
      </span>

      {/* Five-segment fit bar. Sits next to the knowledge chip because the
          knowledge segment is one of the five and the two are read together. */}
      <span className="flex w-9 shrink-0 justify-center">
        {fit ? (
          <FitBar scores={fit.scores} rubricVersion={fit.rubric_version} />
        ) : (
          <span className="text-3xs text-tertiary" title="Fit could not be computed">
            —
          </span>
        )}
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
      {/* When the sweep last ran — deliberately NOT a quality signal. */}
      <span
        className="hidden w-20 text-right text-3xs text-tertiary @2xl:inline"
        title={
          role.last_maintained
            ? `Sweep last ran ${formatAge(role.last_maintained)}. Says nothing about whether it found anything.`
            : "The sweep has never run on this role."
        }
      >
        {role.last_maintained ? formatAge(role.last_maintained) : "—"}
      </span>
    </div>
  );
}
