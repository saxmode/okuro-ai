import { useMemo, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Search, X, Check, Ban, CircleDashed, TriangleAlert, Plus, Trash2,
  Palette, Layout, Server, Scale, Package,
} from "lucide-react";
import {
  brandAssetsApi,
  tokenizeApiSrc,
  type BrandAsset,
  type BgTarget,
} from "@/lib/slides-api";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { PageHeader } from "@/components/shell/page-header";
import { SectionLabel } from "@/components/ui/section-label";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  STACK_STATUS_CONFIG,
  type StackEntry,
  type StackLayer,
  type StackProfileSummary,
  type StackProfileScope,
  type StackProposal,
  type StackResolvedProfile,
  type StackValidation,
  type SlotKind,
  type Brand,
  type BrandSummary,
  type BrandResolved,
  type ResolvedSlotItem,
  type PrincipleSetSummary,
} from "@/types/api";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { useUrlTab } from "@/hooks/use-url-tab";
import { Segmented } from "@/components/ui/segmented";

/**
 * /stack — tech stack registry UI.
 *
 * Three tabs:
 *   Browse   — entries grouped by category/layer, with status filter + search
 *   Profiles — opinionated compositions (pick one per project)
 *   Review   — whole-registry validation + pending proposals
 */
export function StackPage() {
  const tab = useUrlTab("brands");
  const [newBrandOpen, setNewBrandOpen] = useState(false);

  return (
    <div className="page-shell space-y-8">
      <PageHeader
        title="Stack"
        subtitle="Brands compose design + frontend + backend + principles. Profiles and entries are the building blocks."
        right={
          tab.value === "brands" ? (
            <Button size="sm" onClick={() => setNewBrandOpen(true)}>
              <Plus className="mr-1 h-3 w-3" />
              New brand
            </Button>
          ) : undefined
        }
      />

      <Tabs value={tab.value} onValueChange={tab.onValueChange}>
        <TabsList>
          <TabsTrigger value="brands">
            <Package className="h-3.5 w-3.5 mr-1.5" />
            Brands
          </TabsTrigger>
          <TabsTrigger value="profiles">Profiles</TabsTrigger>
          <TabsTrigger value="entries">Entries</TabsTrigger>
          <TabsTrigger value="review">Review</TabsTrigger>
        </TabsList>

        <TabsContent value="brands" className="mt-4">
          <BrandsTab
            newBrandOpen={newBrandOpen}
            onNewBrandOpenChange={setNewBrandOpen}
          />
        </TabsContent>

        <TabsContent value="profiles" className="mt-4">
          <ProfilesTab />
        </TabsContent>

        <TabsContent value="entries" className="mt-4">
          <BrowseTab />
        </TabsContent>

        <TabsContent value="review" className="mt-4">
          <ReviewTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Fetch helpers — thin wrappers over the shared `api()` client so every
// request carries the bearer token (see src/lib/api.ts). Using raw fetch()
// here previously produced a silent 401 on every /api/stack/* call — the
// brands list stayed empty and the create dialog's mutation errored with
// no visible feedback.
// ---------------------------------------------------------------------------

function getJSON<T>(path: string): Promise<T> {
  return api<T>(path);
}

function postJSON<T>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: "POST", body: JSON.stringify(body) });
}

// ---------------------------------------------------------------------------
// Browse tab — entries grouped by category & layer
// ---------------------------------------------------------------------------

const CATEGORY_ORDER = ["runtime", "frontend", "backend", "api", "ops"] as const;
const STATUS_FILTERS: Array<{ value: string; label: string }> = [
  { value: "all",        label: "ALL" },
  { value: "approved",   label: "APPROVED" },
  { value: "trial",      label: "TRIAL" },
  { value: "deprecated", label: "DEPRECATED" },
  { value: "banned",     label: "BANNED" },
];

function BrowseTab() {
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [viewing, setViewing] = useState<string | undefined>();

  const layers = useQuery({
    queryKey: ["stack", "layers"],
    queryFn: () => getJSON<{ layers: StackLayer[] }>("/api/stack/layers"),
    staleTime: 60_000,
  });

  const entries = useQuery({
    queryKey: ["stack", "entries", statusFilter],
    queryFn: () =>
      getJSON<{ entries: StackEntry[] }>(
        statusFilter === "all"
          ? "/api/stack/entries"
          : `/api/stack/entries?status=${statusFilter}`,
      ),
    staleTime: 30_000,
  });

  const filtered = useMemo(() => {
    const all = entries.data?.entries ?? [];
    if (!search) return all;
    const q = search.toLowerCase();
    return all.filter(
      (e) =>
        e.id.toLowerCase().includes(q) ||
        e.name.toLowerCase().includes(q) ||
        e.rationale.toLowerCase().includes(q) ||
        e.layer.toLowerCase().includes(q),
    );
  }, [entries.data, search]);

  const byLayerGroups = useMemo(() => {
    const layerMap = new Map<string, StackLayer>();
    for (const l of layers.data?.layers ?? []) layerMap.set(l.id, l);

    const grouped: Record<string, Record<string, StackEntry[]>> = {};
    for (const e of filtered) {
      const layer = layerMap.get(e.layer);
      const cat = layer?.category ?? "other";
      const lname = layer?.name ?? e.layer;
      grouped[cat] ??= {};
      grouped[cat][lname] ??= [];
      grouped[cat][lname].push(e);
    }
    return grouped;
  }, [filtered, layers.data]);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-tertiary" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search entries (name, id, rationale, layer)…"
            className="bg-surface pl-8 text-sm"
          />
          {search && (
            <button
              onClick={() => setSearch("")}
              aria-label="Clear"
              className="absolute right-2 top-2 text-tertiary hover:text-fg-muted"
            >
              <X className="h-4 w-4" />
            </button>
          )}
        </div>
        <Segmented
          ariaLabel="Filter stack entries by status"
          value={statusFilter}
          onChange={setStatusFilter}
          options={STATUS_FILTERS.map((f) => ({
            label: f.label.charAt(0) + f.label.slice(1).toLowerCase(),
            value: f.value,
          }))}
        />
      </div>

      {entries.isLoading && (
        <p className="text-sm text-tertiary">Loading…</p>
      )}

      {!entries.isLoading && filtered.length === 0 && (
        <EmptyState title="No entries" description="No entries match the current filters." />
      )}

      <div className="space-y-6">
        {CATEGORY_ORDER.map((cat) => {
          const layerGroups = byLayerGroups[cat];
          if (!layerGroups) return null;
          return (
            <section key={cat} className="space-y-2">
              <SectionLabel className="px-0">{cat}</SectionLabel>
              <div className="space-y-3">
                {Object.entries(layerGroups).map(([layerName, entries]) => (
                  <div key={layerName} className="space-y-1.5">
                    <div className="text-2xs uppercase tracking-wider text-tertiary">
                      {layerName}
                    </div>
                    <div className="grid grid-cols-1 gap-1.5 md:grid-cols-2 lg:grid-cols-3">
                      {entries.map((e) => (
                        <EntryCard key={e.id} entry={e} onClick={() => setViewing(e.id)} />
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </section>
          );
        })}
      </div>

      {viewing && (
        <EntryDetailDialog entryId={viewing} onClose={() => setViewing(undefined)} />
      )}
    </div>
  );
}

function EntryCard({ entry, onClick }: { entry: StackEntry; onClick: () => void }) {
  const cfg = STACK_STATUS_CONFIG[entry.status];
  return (
    <button
      onClick={onClick}
      className="group rounded border border-border bg-surface p-2.5 text-left transition-colors hover:border-border-hover hover:bg-surface-subtle"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div
            className="truncate text-sm font-medium text-fg group-hover:text-accent"
            title={entry.id}
          >
            {entry.name}
          </div>
        </div>
        <span className={cn("shrink-0 text-2xs font-medium uppercase tracking-wider", cfg.tone)}>
          {cfg.label}
        </span>
      </div>
      {entry.version && (
        <div className="mt-1 font-mono text-2xs text-tertiary">{entry.version}</div>
      )}
    </button>
  );
}

function EntryDetailDialog({
  entryId,
  onClose,
}: {
  entryId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const entry = useQuery({
    queryKey: ["stack", "entry", entryId],
    queryFn: () => getJSON<StackEntry>(`/api/stack/entries/${entryId}`),
  });

  const [proposalKind, setProposalKind] = useState<string>("");
  const [rationale, setRationale] = useState("");
  const propose = useMutation({
    mutationFn: () =>
      postJSON("/api/stack/proposals", {
        entry_id: entryId,
        kind: proposalKind,
        rationale,
        proposed_by: "web-ui",
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["stack", "proposals"] });
      setRationale("");
      setProposalKind("");
    },
  });

  const e = entry.data;

  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="font-mono text-sm">{entryId}</DialogTitle>
          {e && (
            <DialogDescription className="space-x-2">
              <span>{e.name}</span>
              {e.version && <span className="font-mono text-2xs">{e.version}</span>}
              <Badge variant="outline" className={STACK_STATUS_CONFIG[e.status].tone}>
                {STACK_STATUS_CONFIG[e.status].label}
              </Badge>
            </DialogDescription>
          )}
        </DialogHeader>

        {entry.isLoading && <p className="text-sm text-tertiary">Loading…</p>}
        {e && (
          <div className="space-y-3 text-sm">
            {e.rationale && (
              <section>
                <div className="text-2xs uppercase tracking-wider text-tertiary">Rationale</div>
                <p>{e.rationale}</p>
              </section>
            )}

            {e.use_when.length > 0 && (
              <section>
                <div className="text-2xs uppercase tracking-wider text-tertiary">Use when</div>
                <ul className="list-disc pl-5">
                  {e.use_when.map((u) => <li key={u}>{u}</li>)}
                </ul>
              </section>
            )}

            {e.avoid_when.length > 0 && (
              <section>
                <div className="text-2xs uppercase tracking-wider text-tertiary">Avoid when</div>
                <ul className="list-disc pl-5">
                  {e.avoid_when.map((u) => <li key={u}>{u}</li>)}
                </ul>
              </section>
            )}

            {e.depends_on.length > 0 && (
              <section>
                <div className="text-2xs uppercase tracking-wider text-tertiary">Depends on</div>
                <div className="flex flex-wrap gap-1">
                  {e.depends_on.map((d) => (
                    <span key={d} className="rounded bg-surface-subtle px-2 py-0.5 font-mono text-2xs">
                      {d}
                    </span>
                  ))}
                </div>
              </section>
            )}

            {e.alternatives_considered.length > 0 && (
              <section>
                <div className="text-2xs uppercase tracking-wider text-tertiary">Alternatives considered</div>
                <ul className="space-y-1">
                  {e.alternatives_considered.map((a) => (
                    <li key={a.id} className="text-2xs">
                      <span className="font-mono">{a.id}</span>
                      {a.reason_rejected ? ` — ${a.reason_rejected}` : null}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {(e.owner || e.replaces || e.last_reviewed) && (
              <section className="grid grid-cols-3 gap-2 text-2xs text-tertiary">
                {e.owner && <div>Owner: <span className="text-fg-muted">{e.owner}</span></div>}
                {e.replaces && <div>Replaces: <span className="font-mono text-fg-muted">{e.replaces}</span></div>}
                {e.last_reviewed && <div>Reviewed: <span className="text-fg-muted">{e.last_reviewed}</span></div>}
              </section>
            )}

            <section className="space-y-2 border-t border-border pt-3">
              <div className="text-2xs uppercase tracking-wider text-tertiary">Propose change</div>
              <div className="flex flex-wrap gap-1.5">
                {(["promote", "deprecate", "ban", "reinstate"] as const).map((k) => (
                  <button
                    key={k}
                    onClick={() => setProposalKind(k)}
                    className={cn(
                      "rounded px-2 py-1 text-2xs font-medium uppercase tracking-wider",
                      proposalKind === k
                        ? "bg-accent text-on-accent"
                        : "bg-surface-subtle text-tertiary hover:text-fg-muted",
                    )}
                  >
                    {k}
                  </button>
                ))}
              </div>
              {proposalKind && (
                <>
                  <Textarea
                    value={rationale}
                    onChange={(ev) => setRationale(ev.target.value)}
                    placeholder="Why this change? (required)"
                    rows={3}
                  />
                  <Button
                    size="sm"
                    disabled={!rationale || propose.isPending}
                    onClick={() => propose.mutate()}
                  >
                    File proposal
                  </Button>
                </>
              )}
              {propose.isSuccess && (
                <p className="text-2xs text-success">Proposal filed.</p>
              )}
              {propose.isError && (
                <p className="text-2xs text-error">{(propose.error as Error).message}</p>
              )}
            </section>
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Profiles tab
// ---------------------------------------------------------------------------

const SCOPE_FILTERS: Array<{ value: string; label: string }> = [
  { value: "all",       label: "All" },
  { value: "frontend",  label: "Frontend" },
  { value: "backend",   label: "Backend" },
  { value: "fullstack", label: "Fullstack" },
  { value: "agent",     label: "Agent" },
];

const SCOPE_TONE: Record<StackProfileScope, string> = {
  frontend:  "border-info/40 text-info",
  backend:   "border-success/40 text-success",
  fullstack: "border-warning/40 text-warning",
  agent:     "border-accent/40 text-accent",
  other:     "border-border text-tertiary",
};

function ProfilesTab() {
  const [selected, setSelected] = useState<string | undefined>();
  const [scopeFilter, setScopeFilter] = useState<string>("all");

  const profiles = useQuery({
    queryKey: ["stack", "profiles", scopeFilter],
    queryFn: () =>
      getJSON<{ profiles: StackProfileSummary[] }>(
        scopeFilter === "all"
          ? "/api/stack/profiles"
          : `/api/stack/profiles?scope=${scopeFilter}`,
      ),
    staleTime: 30_000,
  });

  return (
    <div className="space-y-4">
      <Segmented
        ariaLabel="Filter profiles by scope"
        value={scopeFilter}
        onChange={setScopeFilter}
        options={SCOPE_FILTERS}
      />

      <div className="grid grid-cols-1 gap-6 md:grid-cols-[260px_1fr]">
        <aside className="space-y-1">
          <SectionLabel className="px-0">Profiles</SectionLabel>
          {(profiles.data?.profiles ?? []).map((p) => (
            <button
              key={p.name}
              onClick={() => setSelected(p.name)}
              className={cn(
                "w-full rounded px-2.5 py-2 text-left transition-colors",
                selected === p.name
                  ? "bg-accent-subtle text-fg"
                  : "text-fg-muted hover:bg-surface-subtle",
              )}
            >
              <div className="flex items-center gap-2">
                <div className="flex-1 truncate text-sm font-medium">{p.label}</div>
                <Badge
                  variant="outline"
                  className={cn("text-3xs uppercase tracking-wider", SCOPE_TONE[p.scope])}
                >
                  {p.scope}
                </Badge>
              </div>
              <div className="flex items-center justify-between text-2xs text-tertiary">
                <span className="font-mono">{p.name}</span>
                <span>{p.entry_count} entries</span>
              </div>
            </button>
          ))}
          {profiles.data?.profiles.length === 0 && (
            <p className="px-2 text-2xs text-tertiary">No profiles match.</p>
          )}
        </aside>

        <main>
          {!selected && (
            <EmptyState
              title="Pick a profile"
              description="Profiles are opinionated compositions — scoped building blocks that brands compose."
            />
          )}
          {selected && <ProfileDetail name={selected} />}
        </main>
      </div>
    </div>
  );
}

function ProfileDetail({ name }: { name: string }) {
  const qc = useQueryClient();
  const resolved = useQuery({
    queryKey: ["stack", "profile", name],
    queryFn: () => getJSON<StackResolvedProfile>(`/api/stack/profiles/${name}`),
  });
  const lint = useQuery({
    queryKey: ["stack", "profile-lint", name],
    queryFn: () => getJSON<StackValidation>(`/api/stack/profiles/${name}/lint`),
  });

  const [projectSlug, setProjectSlug] = useState("");
  const assign = useMutation({
    mutationFn: () =>
      postJSON(`/api/stack/profiles/${name}/assign`, { project_slug: projectSlug }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["stack", "profile", name] });
      setProjectSlug("");
    },
  });

  if (resolved.isLoading) return <p className="text-sm text-tertiary">Loading…</p>;
  const p = resolved.data;
  if (!p) return <EmptyState title="Not found" description={name} />;

  return (
    <div className="space-y-5">
      <header>
        <div className="flex items-baseline gap-2">
          <h2 className="text-lg font-bold text-fg">{p.label}</h2>
          <span className="font-mono text-2xs text-tertiary">{p.name}</span>
          <Badge variant="outline">{p.status}</Badge>
        </div>
        {p.description && <p className="mt-1 text-sm text-tertiary">{p.description}</p>}
      </header>

      {lint.data && (
        <div
          className={cn(
            "rounded border px-3 py-2 text-2xs",
            lint.data.ok
              ? "border-success/30 bg-success/10 text-success"
              : "border-error/30 bg-error/10 text-error",
          )}
        >
          <div className="flex items-center gap-1.5 font-medium uppercase tracking-wider">
            {lint.data.ok ? <Check className="h-3 w-3" /> : <TriangleAlert className="h-3 w-3" />}
            Lint: {lint.data.ok ? "OK" : `${lint.data.errors.length} errors`}
          </div>
          {lint.data.errors.map((e) => <div key={e}>• {e}</div>)}
          {lint.data.warnings.map((w) => (
            <div key={w} className="text-warning">! {w}</div>
          ))}
        </div>
      )}

      {CATEGORY_ORDER.map((cat) => {
        const rows = p.by_category[cat] ?? [];
        if (rows.length === 0) return null;
        return (
          <section key={cat} className="space-y-2">
            <SectionLabel className="px-0">{cat}</SectionLabel>
            <table className="w-full text-sm">
              <thead className="text-2xs uppercase tracking-wider text-tertiary">
                <tr>
                  <th className="text-left font-medium">Layer</th>
                  <th className="text-left font-medium">Choice</th>
                  <th className="text-left font-medium">Version</th>
                  <th className="text-left font-medium">Role</th>
                  <th className="text-left font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="border-t border-border">
                    <td className="py-1.5 text-tertiary">{r.layer_name}</td>
                    <td className="py-1.5">
                      <div className="font-medium">{r.name}</div>
                      <div className="font-mono text-2xs text-tertiary">{r.id}</div>
                    </td>
                    <td className="py-1.5 font-mono text-2xs">{r.version ?? "—"}</td>
                    <td className="py-1.5 text-2xs uppercase tracking-wider">{r.role}</td>
                    <td className={cn("py-1.5 text-2xs uppercase tracking-wider", STACK_STATUS_CONFIG[r.status].tone)}>
                      {STACK_STATUS_CONFIG[r.status].label}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        );
      })}

      {p.transitive.length > 0 && (
        <section className="space-y-2">
          <SectionLabel className="px-0">Transitive deps</SectionLabel>
          <div className="flex flex-wrap gap-1">
            {p.transitive.map((t) => (
              <span key={t.id} className="rounded bg-surface-subtle px-2 py-0.5 font-mono text-2xs text-tertiary">
                {t.id}
              </span>
            ))}
          </div>
        </section>
      )}

      <section className="space-y-2 border-t border-border pt-4">
        <SectionLabel className="px-0">Bound projects</SectionLabel>
        <div className="flex flex-wrap gap-1">
          {p.projects.length > 0 ? (
            p.projects.map((ps) => (
              <span key={ps} className="rounded bg-accent-subtle px-2 py-0.5 font-mono text-2xs text-fg">
                {ps}
              </span>
            ))
          ) : (
            <span className="text-2xs text-tertiary">None</span>
          )}
        </div>
        <div className="flex items-center gap-2 pt-1">
          <Input
            value={projectSlug}
            onChange={(e) => setProjectSlug(e.target.value)}
            placeholder="project-slug"
            className="max-w-xs text-sm"
          />
          <Button size="sm" disabled={!projectSlug || assign.isPending} onClick={() => assign.mutate()}>
            Bind project
          </Button>
          {assign.isSuccess && <span className="text-2xs text-success">Bound.</span>}
          {assign.isError && <span className="text-2xs text-error">{(assign.error as Error).message}</span>}
        </div>
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Review tab — validation + proposals
// ---------------------------------------------------------------------------

function ReviewTab() {
  const validation = useQuery({
    queryKey: ["stack", "validate"],
    queryFn: () => getJSON<StackValidation>("/api/stack/validate"),
    staleTime: 10_000,
  });

  const proposals = useQuery({
    queryKey: ["stack", "proposals", "pending"],
    queryFn: () =>
      getJSON<{ proposals: StackProposal[] }>("/api/stack/proposals?outcome=pending"),
    staleTime: 10_000,
  });

  return (
    <div className="space-y-6">
      <section>
        <SectionLabel className="px-0">Registry validation</SectionLabel>
        {validation.data && (
          <div
            className={cn(
              "mt-2 rounded border px-3 py-2 text-sm",
              validation.data.ok
                ? "border-success/30 bg-success/10"
                : "border-error/30 bg-error/10",
            )}
          >
            <div className="flex items-center gap-2 text-2xs uppercase tracking-wider">
              {validation.data.ok ? (
                <Check className="h-3 w-3 text-success" />
              ) : (
                <TriangleAlert className="h-3 w-3 text-error" />
              )}
              <span>{validation.data.ok ? "All checks passing" : `${validation.data.errors.length} errors`}</span>
              <span className="text-tertiary">
                {Object.entries(validation.data.stats)
                  .map(([k, v]) => `${k}: ${v}`)
                  .join("   ")}
              </span>
            </div>
            {validation.data.errors.length > 0 && (
              <ul className="mt-2 list-disc pl-5 text-xs text-error">
                {validation.data.errors.map((e) => <li key={e}>{e}</li>)}
              </ul>
            )}
            {validation.data.warnings.length > 0 && (
              <ul className="mt-2 list-disc pl-5 text-xs text-warning">
                {validation.data.warnings.map((w) => <li key={w}>{w}</li>)}
              </ul>
            )}
          </div>
        )}
      </section>

      <section>
        <SectionLabel className="px-0">Pending proposals</SectionLabel>
        {proposals.data?.proposals.length === 0 && (
          <div className="mt-2 flex items-center gap-1.5 text-2xs text-tertiary">
            <CircleDashed className="h-3 w-3" />
            Nothing waiting.
          </div>
        )}
        <ul className="mt-2 space-y-2">
          {(proposals.data?.proposals ?? []).map((p) => (
            <ProposalRow key={p.id} proposal={p} />
          ))}
        </ul>
      </section>
    </div>
  );
}

function ProposalRow({ proposal }: { proposal: StackProposal }) {
  const qc = useQueryClient();
  const decide = useMutation({
    mutationFn: (outcome: "accepted" | "rejected") =>
      postJSON(`/api/stack/proposals/${proposal.id}/decide`, {
        outcome,
        decided_by: "web-ui",
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["stack"] });
    },
  });

  return (
    <li className="rounded border border-border bg-surface p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm">
            <span className="font-mono">{proposal.entry_id}</span>
            <Badge variant="outline">{proposal.kind}</Badge>
          </div>
          <p className="mt-1 text-xs text-tertiary">{proposal.rationale}</p>
          <div className="mt-1 flex items-center gap-2 text-2xs text-tertiary">
            <span>by {proposal.proposed_by ?? "unknown"}</span>
            <span>·</span>
            <span>{proposal.created_at}</span>
          </div>
        </div>
        <div className="flex shrink-0 gap-1">
          <Button size="sm" onClick={() => decide.mutate("accepted")} disabled={decide.isPending}>
            <Check className="mr-1 h-3 w-3" />
            Accept
          </Button>
          <Button size="sm" variant="outline" onClick={() => decide.mutate("rejected")} disabled={decide.isPending}>
            <Ban className="mr-1 h-3 w-3" />
            Reject
          </Button>
        </div>
      </div>
    </li>
  );
}


// ===========================================================================
// Brands tab — top-level compositions of design + fe stack + be stack +
// principles, driven by the configurable slot_kinds registry.
// ===========================================================================

const SLOT_KIND_ICON: Record<string, typeof Package> = {
  design:     Palette,
  fe_stack:   Layout,
  be_stack:   Server,
  principles: Scale,
};

function slotKindIcon(kind: string) {
  return SLOT_KIND_ICON[kind] ?? Package;
}

function deleteJSON<T>(path: string): Promise<T> {
  return api<T>(path, { method: "DELETE" });
}

function putJSON<T>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: "PUT", body: JSON.stringify(body) });
}

function BrandsTab({
  newBrandOpen,
  onNewBrandOpenChange,
}: {
  newBrandOpen: boolean;
  onNewBrandOpenChange: (open: boolean) => void;
}) {
  const [editing, setEditing] = useState<string | undefined>();

  const brands = useQuery({
    queryKey: ["stack", "brands"],
    queryFn: () =>
      getJSON<{ brands: BrandSummary[] }>("/api/stack/brands"),
    staleTime: 15_000,
  });

  const slotKinds = useQuery({
    queryKey: ["stack", "slot_kinds"],
    queryFn: () =>
      getJSON<{ slot_kinds: SlotKind[] }>("/api/stack/slot_kinds"),
    staleTime: 60_000,
  });

  return (
    <div className="space-y-4">
      <SectionLabel className="px-0">
        {brands.data?.brands.length ?? 0} brands
      </SectionLabel>

      {brands.isLoading && <p className="text-sm text-tertiary">Loading…</p>}
      {brands.data?.brands.length === 0 && (
        <EmptyState
          title="No brands yet"
          description="A brand composes a design profile, frontend stack, backend stack, and principle set into one project-facing identity."
        />
      )}

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        {(brands.data?.brands ?? []).map((b) => (
          <BrandCard
            key={b.id}
            brand={b}
            slotKinds={slotKinds.data?.slot_kinds ?? []}
            onClick={() => setEditing(b.id)}
          />
        ))}
      </div>

      {(editing || newBrandOpen) && (
        <BrandEditor
          brandId={editing}
          slotKinds={slotKinds.data?.slot_kinds ?? []}
          onClose={() => {
            setEditing(undefined);
            onNewBrandOpenChange(false);
          }}
        />
      )}
    </div>
  );
}

function BrandCard({
  brand,
  slotKinds,
  onClick,
}: {
  brand: BrandSummary;
  slotKinds: SlotKind[];
  onClick: () => void;
}) {
  const resolved = useQuery({
    queryKey: ["stack", "brand-resolve", brand.id],
    queryFn: () => getJSON<BrandResolved>(`/api/stack/brands/${brand.id}/resolve`),
    staleTime: 30_000,
  });

  const filledKinds = Object.keys(resolved.data?.slots ?? {});

  return (
    <button
      onClick={onClick}
      className="group rounded border border-border bg-surface p-3 text-left transition-colors hover:border-accent hover:bg-surface-subtle"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-fg group-hover:text-accent">
            {brand.name}
          </div>
          <div className="truncate font-mono text-2xs text-tertiary">{brand.id}</div>
        </div>
        <Badge
          variant="outline"
          className={cn(
            "shrink-0 text-3xs uppercase tracking-wider",
            brand.status === "active" && "border-success/40 text-success",
            brand.status === "draft" && "border-warning/40 text-warning",
            brand.status === "archived" && "border-tertiary/40 text-tertiary",
          )}
        >
          {brand.status}
        </Badge>
      </div>

      {brand.description && (
        <p className="mt-1 line-clamp-2 text-2xs text-fg-muted">
          {brand.description}
        </p>
      )}

      {/* Slot summary */}
      <div className="mt-3 grid grid-cols-2 gap-1.5">
        {slotKinds.map((k) => {
          const Icon = slotKindIcon(k.kind);
          const filled = filledKinds.includes(k.kind);
          const slot = resolved.data?.slots[k.kind];
          const refLabel = (() => {
            if (!filled || !slot) return null;
            if (Array.isArray(slot)) {
              return slot.map((s) => (s as ResolvedSlotItem).ref_id).join(", ");
            }
            const s = slot as ResolvedSlotItem;
            return s.ref_id ?? "—";
          })();
          return (
            <div
              key={k.kind}
              className={cn(
                "flex items-center gap-1.5 rounded border px-2 py-1 text-2xs",
                filled ? "border-border bg-surface-subtle" : "border-dashed border-border text-tertiary",
              )}
              title={k.description}
            >
              <Icon className="h-3 w-3 shrink-0" />
              <span className="truncate font-mono" title={refLabel ?? ""}>
                {refLabel ?? <span className="italic">empty</span>}
              </span>
            </div>
          );
        })}
      </div>

      <div className="mt-2 flex items-center justify-between text-3xs text-tertiary">
        <span>{brand.project_count} projects</span>
        <span>{brand.slot_count} slots filled</span>
      </div>
    </button>
  );
}

function BrandEditor({
  brandId,
  slotKinds,
  onClose,
}: {
  brandId?: string;
  slotKinds: SlotKind[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const isNew = !brandId;

  const existing = useQuery({
    queryKey: ["stack", "brand", brandId],
    queryFn: () => getJSON<Brand>(`/api/stack/brands/${brandId}`),
    enabled: !isNew,
  });

  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [status, setStatus] = useState<"active" | "draft" | "archived">("draft");
  const [slots, setSlots] = useState<Record<string, string>>({});
  const [logoCorner, setLogoCorner] = useState<"tl" | "tr" | "bl" | "br">("tl");

  // Hydrate from existing.
  useMemo(() => {
    if (existing.data) {
      setId(existing.data.id);
      setName(existing.data.name);
      setDescription(existing.data.description ?? "");
      setStatus(existing.data.status);
      setLogoCorner(existing.data.logo_corner ?? "tl");
      // Coerce array slots to first entry for editor (single-card UI).
      const flat: Record<string, string> = {};
      for (const [k, v] of Object.entries(existing.data.slots ?? {})) {
        flat[k] = Array.isArray(v) ? v[0] ?? "" : (v as string);
      }
      setSlots(flat);
    }
  }, [existing.data]);

  const save = useMutation({
    mutationFn: async () => {
      // Strip empty slots so they don't persist as NULL refs.
      const cleanSlots: Record<string, string> = {};
      for (const [k, v] of Object.entries(slots)) {
        if (v && v.trim()) cleanSlots[k] = v;
      }
      const body = {
        id: id.trim(),
        name: name.trim(),
        description,
        status,
        slots: cleanSlots,
        logo_corner: logoCorner,
      };
      if (isNew) {
        return postJSON<Brand>("/api/stack/brands", body);
      }
      return putJSON<Brand>(`/api/stack/brands/${brandId}`, body);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["stack", "brands"] });
      qc.invalidateQueries({ queryKey: ["stack", "brand-resolve"] });
      onClose();
    },
  });

  const del = useMutation({
    mutationFn: () => deleteJSON(`/api/stack/brands/${brandId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["stack", "brands"] });
      onClose();
    },
  });

  const lint = useQuery({
    queryKey: ["stack", "brand-lint", brandId],
    queryFn: () =>
      getJSON<StackValidation>(`/api/stack/brands/${brandId}/lint`),
    enabled: !isNew,
  });

  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent className="max-w-2xl sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>
            {isNew ? "New brand" : existing.data?.name ?? brandId}
          </DialogTitle>
          <DialogDescription>
            A brand composes one pick per slot. Scope filters ensure a frontend
            slot only accepts frontend profiles, etc.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 text-sm">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <div className="text-2xs uppercase tracking-wider text-tertiary">ID</div>
              <Input
                value={id}
                onChange={(e) => setId(e.target.value)}
                disabled={!isNew}
                placeholder="acme"
                className="font-mono text-xs"
              />
            </div>
            <div className="space-y-1">
              <div className="text-2xs uppercase tracking-wider text-tertiary">Name</div>
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Post CH"
              />
            </div>
          </div>

          <div className="space-y-1">
            <div className="text-2xs uppercase tracking-wider text-tertiary">Description</div>
            <Textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
            />
          </div>

          <div className="space-y-1">
            <div className="text-2xs uppercase tracking-wider text-tertiary">Status</div>
            <Select value={status} onValueChange={(v) => setStatus(v as typeof status)}>
              <SelectTrigger className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="draft">Draft</SelectItem>
                <SelectItem value="active">Active</SelectItem>
                <SelectItem value="archived">Archived</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2 border-t border-border pt-3">
            <div className="text-2xs uppercase tracking-wider text-tertiary">Slots</div>
            {slotKinds.map((k) => (
              <SlotPickerRow
                key={k.kind}
                slotKind={k}
                value={slots[k.kind] ?? ""}
                onChange={(v) =>
                  setSlots((prev) => ({ ...prev, [k.kind]: v }))
                }
              />
            ))}
          </div>

          <div className="space-y-2 border-t border-border pt-3">
            <div className="flex items-center justify-between gap-2">
              <div className="text-2xs uppercase tracking-wider text-tertiary">Logos</div>
              <div className="flex items-center gap-1.5">
                <span className="text-2xs text-tertiary">Default corner</span>
                <Select value={logoCorner} onValueChange={(v) => setLogoCorner(v as typeof logoCorner)}>
                  <SelectTrigger className="h-7 w-32 text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="tl">Top-left</SelectItem>
                    <SelectItem value="tr">Top-right</SelectItem>
                    <SelectItem value="bl">Bottom-left</SelectItem>
                    <SelectItem value="br">Bottom-right</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <p className="text-2xs text-tertiary">
              Upload a light-bg and a dark-bg logo, or one marked “both”. Decks
              auto-pick the right one by canvas background; a deck can override
              the corner per-deck.
            </p>
            {id.trim() ? (
              <BrandLogos brandId={id.trim()} />
            ) : (
              <p className="rounded border border-dashed border-border px-2 py-3 text-center text-2xs italic text-tertiary">
                Enter an ID above to attach logos.
              </p>
            )}
          </div>

          {!isNew && lint.data && (
            <div
              className={cn(
                "rounded border p-2 text-2xs",
                lint.data.ok
                  ? "border-success/40 text-success"
                  : "border-error/40 text-error",
              )}
            >
              <div className="font-medium">
                {lint.data.ok ? "Lint: clean" : `Lint: ${lint.data.errors.length} errors`}
              </div>
              {lint.data.errors.map((e, i) => (
                <div key={i} className="mt-0.5">
                  • {e}
                </div>
              ))}
              {lint.data.warnings.map((w, i) => (
                <div key={i} className="mt-0.5 text-warning">
                  ⚠ {w}
                </div>
              ))}
            </div>
          )}
        </div>

        {(save.isError || del.isError) && (
          <p className="text-2xs text-error">
            {((save.error ?? del.error) as Error | null)?.message}
          </p>
        )}

        <DialogFooter>
          {!isNew && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                if (confirm(`Delete brand "${brandId}"? This cannot be undone.`)) {
                  del.mutate();
                }
              }}
              disabled={del.isPending}
              className="mr-auto text-error"
            >
              <Trash2 className="mr-1 h-3 w-3" />
              Delete
            </Button>
          )}
          <Button size="sm" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={() => save.mutate()}
            disabled={save.isPending || !id.trim() || !name.trim()}
          >
            <Check className="mr-1 h-3 w-3" />
            {isNew ? "Create" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Brand logos — upload up to a light-bg + a dark-bg logo (or one 'both'),
// backed by /api/brand-assets (kind=logo). Thumbnails preview on a tile that
// matches their bg_target so you see the logo the way a deck will render it.
// ---------------------------------------------------------------------------

const BG_TARGET_OPTS: { value: BgTarget; label: string }[] = [
  { value: "light", label: "Light bg" },
  { value: "dark", label: "Dark bg" },
  { value: "both", label: "Both" },
];

function logoTileStyle(bg: BgTarget): React.CSSProperties {
  if (bg === "dark") return { background: "#0a0a0a" };
  if (bg === "light") return { background: "#ffffff" };
  // 'both' — split tile: light top-left, dark bottom-right.
  return { background: "linear-gradient(135deg, #ffffff 0 50%, #0a0a0a 50% 100%)" };
}

function BrandLogos({ brandId }: { brandId: string }) {
  const qc = useQueryClient();
  const [bgTarget, setBgTarget] = useState<BgTarget>("both");
  const fileRef = useRef<HTMLInputElement>(null);

  const logos = useQuery({
    queryKey: ["brand-logos", brandId],
    queryFn: () => brandAssetsApi.list(brandId, "logo"),
    enabled: !!brandId,
    staleTime: 15_000,
  });

  const upload = useMutation({
    mutationFn: (file: File) => brandAssetsApi.upload(brandId, "logo", file, bgTarget),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["brand-logos", brandId] }),
    onSettled: () => {
      if (fileRef.current) fileRef.current.value = "";
    },
  });

  const del = useMutation({
    mutationFn: (id: string) => brandAssetsApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["brand-logos", brandId] }),
  });

  const items: BrandAsset[] = logos.data?.assets ?? [];

  return (
    <div className="rounded border border-border bg-surface p-2">
      <div className="flex flex-wrap items-center gap-2">
        <Select value={bgTarget} onValueChange={(v) => setBgTarget(v as BgTarget)}>
          <SelectTrigger className="w-32">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {BG_TARGET_OPTS.map((o) => (
              <SelectItem key={o.value} value={o.value}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          className="hidden"
          id={`logo-file-${brandId}`}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) upload.mutate(f);
          }}
        />
        <Button
          size="sm"
          variant="outline"
          asChild
          disabled={upload.isPending}
        >
          <label htmlFor={`logo-file-${brandId}`} className="cursor-pointer">
            <Plus className="mr-1 h-3 w-3" />
            {upload.isPending ? "Uploading…" : "Upload logo"}
          </label>
        </Button>
      </div>

      {upload.isError && (
        <p className="mt-1 text-2xs text-error">{(upload.error as Error).message}</p>
      )}

      {items.length === 0 ? (
        <p className="mt-2 text-2xs italic text-tertiary">No logos yet.</p>
      ) : (
        <div className="mt-2 grid grid-cols-3 gap-2 sm:grid-cols-4">
          {items.map((a) => (
            <div key={a.id} className="group relative">
              <div
                className="flex h-16 w-full items-center justify-center overflow-hidden rounded border border-border"
                style={logoTileStyle(a.bg_target)}
                title={`${a.name} · ${a.bg_target} bg`}
              >
                <img
                  src={tokenizeApiSrc(a.url)}
                  alt={a.name}
                  className="max-h-[80%] max-w-[80%] object-contain"
                />
              </div>
              <button
                type="button"
                onClick={() => del.mutate(a.id)}
                aria-label="delete logo"
                className="absolute -right-1 -top-1 hidden h-4 w-4 items-center justify-center rounded-full bg-error text-[10px] text-white group-hover:flex"
              >
                ×
              </button>
              <span className="mt-0.5 block text-center text-3xs uppercase tracking-wider text-tertiary">
                {a.bg_target}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function SlotPickerRow({
  slotKind,
  value,
  onChange,
}: {
  slotKind: SlotKind;
  value: string;
  onChange: (v: string) => void;
}) {
  const Icon = slotKindIcon(slotKind.kind);

  // Load options based on the slot's target registry.
  const profiles = useQuery({
    queryKey: ["stack", "profiles", slotKind.scope_filter],
    queryFn: () =>
      getJSON<{ profiles: StackProfileSummary[] }>(
        slotKind.scope_filter
          ? `/api/stack/profiles?scope=${slotKind.scope_filter}`
          : "/api/stack/profiles",
      ),
    enabled: slotKind.registry === "stack_profile",
    staleTime: 30_000,
  });

  // THE DESIGN SLOT PICKS A KIT, because /design-engine is the one place a
  // design system is managed. This listed v0 profiles and sat beside a "From
  // URL" scraper and a full profile editor — a second management surface for
  // the same concern, which is what the owner asked to end. The picker survives
  // (assigning a design to a brand is a stack question); the editing does not.
  const designProfiles = useQuery({
    queryKey: ["design", "profiles"],
    queryFn: async () => {
      const res = await getJSON<{ kits: Array<{ id: string }> }>(
        "/api/design-engine/kits",
      );
      return res.kits.map((k) => ({ id: k.id, name: k.id }));
    },
    enabled: slotKind.registry === "design_profile",
    staleTime: 60_000,
  });

  const principleSets = useQuery({
    queryKey: ["principle_sets"],
    queryFn: () =>
      getJSON<{ principle_sets: PrincipleSetSummary[] }>("/api/principle_sets"),
    enabled: slotKind.registry === "principle_set",
    staleTime: 30_000,
  });

  const options: Array<{ id: string; label: string }> = useMemo(() => {
    if (slotKind.registry === "stack_profile") {
      return (profiles.data?.profiles ?? []).map((p) => ({
        id: p.name,
        label: `${p.label} · ${p.name}`,
      }));
    }
    if (slotKind.registry === "design_profile") {
      return (designProfiles.data ?? []).map((d) => ({
        id: d.id,
        label: `${d.name} · ${d.id}`,
      }));
    }
    if (slotKind.registry === "principle_set") {
      return (principleSets.data?.principle_sets ?? []).map((s) => ({
        id: s.id,
        label: `${s.name} · ${s.id}`,
      }));
    }
    return [];
  }, [slotKind.registry, profiles.data, designProfiles.data, principleSets.data]);

  return (
    <div className="rounded border border-border bg-surface p-2">
      {/* Top row: icon + label + badges */}
      <div className="flex items-center gap-2">
        <Icon className="h-4 w-4 shrink-0 text-tertiary" />
        <span className="text-xs font-medium text-fg">{slotKind.kind}</span>
        {slotKind.scope_filter && (
          <Badge
            variant="outline"
            className={cn("text-3xs", SCOPE_TONE[slotKind.scope_filter])}
          >
            {slotKind.scope_filter}
          </Badge>
        )}
        {slotKind.required && (
          <Badge variant="outline" className="text-3xs border-error/40 text-error">
            required
          </Badge>
        )}
      </div>
      {slotKind.description && (
        <div className="mt-0.5 ml-6 text-2xs text-tertiary">
          {slotKind.description}
        </div>
      )}
      {/* Picker row */}
      <div className="mt-2 ml-6 flex items-center gap-2">
        <Select value={value || "__none__"} onValueChange={(v) => onChange(v === "__none__" ? "" : v)}>
          <SelectTrigger className="flex-1">
            <SelectValue placeholder="Pick…" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__none__">— none —</SelectItem>
            {options.map((o) => (
              <SelectItem key={o.id} value={o.id}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {/* NO EDIT OR "FROM URL" BUTTON HERE ANY MORE. This row sat beside a
            scraper and a full design-profile editor — a second management
            surface for a concern that now has exactly one, /design-engine.
            Assigning a design to a brand is a stack question and stays;
            authoring one is not. */}
      </div>
    </div>
  );
}

