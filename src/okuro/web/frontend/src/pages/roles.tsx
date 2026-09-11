import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Plus, RefreshCw, Search, X } from "lucide-react";
import { roleApi } from "@/lib/api";
import { formatAge } from "@/lib/format";
import type { RoleInfo } from "@/types/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { RoleBrowser } from "@/components/role/role-browser";
import { RoleViewer } from "@/components/role/role-viewer";
import { CreateRoleDialog } from "@/components/role/create-role-dialog";
import { PageHeader } from "@/components/shell/page-header";

export function RolesPage() {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [staleOnly, setStaleOnly] = useState(false);
  const [domainFilter, setDomainFilter] = useState<string | null>(null);
  const [selectedRole, setSelectedRole] = useState<string | undefined>();
  const [viewingRole, setViewingRole] = useState<string | undefined>();
  const [createOpen, setCreateOpen] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["roles"],
    queryFn: () => roleApi.list(),
    staleTime: 30_000,
  });

  const runAllMutation = useMutation({
    mutationFn: () => roleApi.runAllStale(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["roles"] });
      qc.invalidateQueries({ queryKey: ["roles-maintenance"] });
    },
  });

  const allRoles: RoleInfo[] = data?.roles ?? [];

  const filtered = allRoles.filter((r) => {
    if (
      search &&
      !r.id.toLowerCase().includes(search.toLowerCase()) &&
      !r.description.toLowerCase().includes(search.toLowerCase())
    ) {
      return false;
    }
    if (staleOnly && !r.stale) return false;
    if (domainFilter && r.domain !== domainFilter) return false;
    return true;
  });

  const domains = useMemo(
    () => [...new Set(allRoles.map((r) => r.domain))].sort(),
    [allRoles],
  );

  const staleCount = allRoles.filter((r) => r.stale).length;
  const knowledgeTotal = allRoles.reduce(
    (s, r) => s + (r.knowledge_count ?? 0),
    0,
  );
  const lastMaintainedTs = allRoles
    .map((r) => r.last_maintained)
    .filter((t): t is string => !!t)
    .sort()
    .pop();

  const handleSelectRole = (id: string) => {
    setSelectedRole(id);
    setViewingRole(id);
  };

  return (
    <div className="page-shell space-y-8">
      <PageHeader
        title="Roles"
        subtitle={
          <>
            {allRoles.length} roles · {staleCount} stale · {knowledgeTotal} knowledge entries
            {lastMaintainedTs ? (
              <> · Last maintenance {formatAge(lastMaintainedTs)}</>
            ) : null}
          </>
        }
        right={
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant={staleCount === 0 ? "outline" : "default"}
              onClick={() => runAllMutation.mutate()}
              disabled={staleCount === 0 || runAllMutation.isPending}
              className="h-8 text-xs"
            >
              {runAllMutation.isPending ? (
                <>
                  <RefreshCw className="mr-1 h-3 w-3 animate-spin" /> Triggering…
                </>
              ) : (
                <>
                  <RefreshCw className="mr-1 h-3 w-3" />
                  Maintain all stale ({staleCount})
                </>
              )}
            </Button>
            <Button size="sm" onClick={() => setCreateOpen(true)} className="h-8">
              <Plus className="mr-1.5 h-3.5 w-3.5" />
              Create
            </Button>
          </div>
        }
      />

      {runAllMutation.data && (
        <div className="rounded border border-info/40 bg-info/10 px-3 py-2 text-xs text-info flex items-center gap-3">
          <span>
            Spawned role-researcher task for{" "}
            {runAllMutation.data.role_count} stale role
            {runAllMutation.data.role_count === 1 ? "" : "s"}.
          </span>
          {runAllMutation.data.task_id && (
            <Link
              to={`/work/${runAllMutation.data.task_id}`}
              className="ml-auto text-info underline"
            >
              open task →
            </Link>
          )}
        </div>
      )}

      {/* Cross-nav hint to global schedule view */}
      <div className="flex items-center gap-2 text-2xs text-tertiary">
        <CalendarClock className="h-3 w-3" />
        <Link
          to="/work?focus=role-refresh"
          className="text-info hover:underline"
        >
          See schedule + history →
        </Link>
        <span>Daily role refresh runs at 5am by default.</span>
      </div>

      {/* Filters */}
      <div className="space-y-2">
        <div className="relative">
          <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-tertiary" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search roles..."
            className="bg-surface pl-8 text-sm text-fg placeholder:text-tertiary"
          />
          {search && (
            <button
              onClick={() => setSearch("")}
              aria-label="Clear search"
              className="absolute right-2 top-2 text-tertiary hover:text-fg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:rounded-sm"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          )}
        </div>

        {/* Domain chips + stale toggle */}
        <div className="flex flex-wrap items-center gap-1.5">
          <Chip
            active={domainFilter === null}
            onClick={() => setDomainFilter(null)}
            label={`all (${allRoles.length})`}
          />
          {domains.map((d) => {
            const count = allRoles.filter((r) => r.domain === d).length;
            return (
              <Chip
                key={d}
                active={domainFilter === d}
                onClick={() =>
                  setDomainFilter(domainFilter === d ? null : d)
                }
                label={`${d} (${count})`}
              />
            );
          })}
          <button
            type="button"
            onClick={() => setStaleOnly((s) => !s)}
            className={`ml-auto inline-flex h-6 items-center gap-1.5 rounded-full px-2.5 text-2xs font-medium uppercase tracking-wider transition-colors ${
              staleOnly
                ? "bg-error/10 text-error"
                : "bg-surface-elevated text-tertiary hover:text-fg-muted"
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                staleOnly ? "bg-error" : "bg-tertiary"
              }`}
            />
            Stale only
          </button>
        </div>
      </div>

      {/* Role browser */}
      {isLoading ? (
        <p className="text-sm text-tertiary">Loading roles...</p>
      ) : (
        <RoleBrowser
          roles={filtered}
          onSelectRole={handleSelectRole}
          selectedRole={selectedRole}
        />
      )}

      {/* Role viewer overlay */}
      {viewingRole && (
        <RoleViewer
          roleId={viewingRole}
          onClose={() => setViewingRole(undefined)}
        />
      )}

      {/* Create role dialog — tier/model selection + domain autocomplete */}
      <CreateRoleDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        existingDomains={domains}
      />
    </div>
  );
}

function Chip({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`h-6 rounded-full px-2.5 text-2xs font-medium transition-colors ${
        active
          ? "bg-accent/20 text-accent"
          : "bg-surface-elevated text-tertiary hover:text-fg-muted"
      }`}
    >
      {label}
    </button>
  );
}
