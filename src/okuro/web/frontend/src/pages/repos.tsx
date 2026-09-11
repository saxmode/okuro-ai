import { useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  RefreshCw,
  Upload,
  Loader2,
  Plus,
  Trash2,
  GitBranch,
  Network,
  CircleAlert,
  CircleCheck,
  Sparkles,
  Boxes,
  Layers,
  Pencil,
  Search,
} from "lucide-react";
import { Link } from "react-router";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { toast } from "@/components/ui/toast";
import { PageHeader } from "@/components/shell/page-header";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatAge, parseApiDate } from "@/lib/format";

/**
 * /repos — clone and manage repositories for AI code work.
 *
 * Clone a remote repo → okuro stores it under ~/.okuro/repos/<workspace>/<name>,
 * registers it as a project (→ cortex root), and code-graph ingests it so agents
 * can query it via cortex_* and kg_* tools. Browse jumps to the /knowledge graph
 * scoped to the repo's project.
 *
 * Backend: /api/repos/* (src/okuro/orchestrator/api/repos.py).
 */

// ── Types ────────────────────────────────────────────────────────────

type Tier = "air" | "advanced" | "pro";

type Repo = {
  id: string;
  url: string;
  workspace: string;
  name: string;
  path: string;
  tier: Tier;
  status: "pending" | "cloning" | "indexing" | "ready" | "error";
  last_indexed_sha: string | null;
  error: string | null;
  default_branch: string | null;
  project_id: string | null;
  // Credential reference, not a secret: token_key names a keyring entry, and the
  // two usernames are account ids. Both are returned by RepoOut and edited in
  // EditRepoDialog — they were missing here, which `tsc -b` caught and
  // `tsc --noEmit` did not.
  token_key: string | null;
  username: string | null;
  pr_username: string | null;
  last_synced_at: string | null;
  last_change: RepoChange | null;
  created_at: string | null;
  updated_at: string | null;
};

type RepoChange = {
  prev_sha: string | null;
  new_sha: string | null;
  commits: number;
  files: number;
  insertions: number;
  deletions: number;
};

type AddPayload = {
  url: string;
  workspace: string;
  tier: Tier;
  name?: string;
  token_key?: string;
  username?: string;
  pr_username?: string;
};

type UpdatePayload = {
  url?: string;
  tier?: Tier;
  default_branch?: string;
  token_key?: string | null;
  username?: string | null;
  pr_username?: string | null;
  verify?: boolean;
  force?: boolean;
};

/** What the backend proves before it writes a credential. */
type VerifyResult = {
  git: { ok: boolean; detail: string };
  rest?: { ok: boolean; detail: string };
};

type UpdateResult = {
  id: string;
  updated: Record<string, unknown>;
  verified: VerifyResult | null;
  status: string;
  refused?: string;
};

type DiscoveredRepo = {
  name: string;
  full_name: string;
  url: string;
  default_branch: string | null;
  updated_on: string | null;
  private: boolean | null;
  language: string | null;
  description: string | null;
  host: string;
  managed: boolean;
  repo_id: string | null;
  managed_status: string | null;
  last_synced_at: string | null;
  token_key: string | null;
  username: string | null;
  pr_username: string | null;
};

type DiscoverResult = {
  sources: { host: string | null; workspace: string | null; token_key: string | null }[];
  repos: DiscoveredRepo[];
  managed: number;
  new: number;
  errors: { host: string; workspace: string | null; token_key: string | null; detail: string }[];
};

type GodNode = {
  node: string;
  degree: number;
  in: number;
  out: number;
  layer: string | null;
};

type Insights = {
  project: string;
  god_nodes: GodNode[];
  communities: { community: string; files: number }[];
  community_count: number;
};

// ── API ──────────────────────────────────────────────────────────────

const reposApi = {
  list: () => api<Repo[]>("/api/repos"),
  add: (p: AddPayload) =>
    api<{ id: string; status: string; message: string }>("/api/repos", {
      method: "POST",
      body: JSON.stringify(p),
    }),
  sync: ({ id, full = false }: { id: string; full?: boolean }) =>
    api<{ id: string; status: string }>(
      `/api/repos/${encodeURIComponent(id)}/sync${full ? "?full=true" : ""}`,
      { method: "POST" },
    ),
  syncAll: (full = false) =>
    api<{ accepted: number; ids: string[]; message: string }>(
      `/api/repos/sync-all${full ? "?full=true" : ""}`,
      { method: "POST" },
    ),
  push: (id: string) =>
    api<{
      id: string;
      source_branch: string | null;
      dest_branch: string;
      pushed: boolean;
      up_to_date: boolean;
      pr_status: "opened" | "failed" | "skipped";
      pr_url: string | null;
      pr_error: string | null;
      detail: string;
    }>(`/api/repos/${encodeURIComponent(id)}/push`, { method: "POST" }),
  remove: (id: string, deleteFiles: boolean) =>
    api<Record<string, unknown>>(
      `/api/repos/${encodeURIComponent(id)}?delete_files=${deleteFiles}`,
      { method: "DELETE" },
    ),
  update: ({ id, ...body }: UpdatePayload & { id: string }) =>
    api<UpdateResult>(`/api/repos/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  discover: () => api<DiscoverResult>("/api/repos/discover/forge"),
  insights: (id: string) =>
    api<Insights>(`/api/repos/${encodeURIComponent(id)}/insights?top_n=15`),
};

const TIERS: { value: Tier; label: string; hint: string }[] = [
  { value: "air", label: "Air", hint: "CPU · tree-sitter code graph" },
  { value: "advanced", label: "Advanced", hint: "+ local embeddings (on demand)" },
  { value: "pro", label: "Pro", hint: "+ cross-repo symbols (on demand)" },
];

const BUSY = new Set(["pending", "cloning", "indexing"]);

// Matches the backend's cortex annotation threshold (one nightly-sync cycle plus
// slack). Keep the two in step: a row the UI calls fresh must not be a row that
// cortex flags stale.
const STALE_AFTER_DAYS = 7;

function ageDays(iso: string | null): number | null {
  if (!iso) return null;
  const d = parseApiDate(iso).getTime();
  return Number.isNaN(d) ? null : (Date.now() - d) / 86_400_000;
}

// ── Change summary ───────────────────────────────────────────────────

/** What the last sync pulled in: commits + file delta, or "up to date". */
function ChangeSummary({ change }: { change: RepoChange | null }) {
  if (!change) return null;
  if (change.commits === 0)
    return <span className="text-muted-foreground/70">up to date</span>;
  return (
    <span className="inline-flex items-center gap-2">
      <span className="text-foreground">
        ↑{change.commits} {change.commits === 1 ? "commit" : "commits"}
      </span>
      {change.files > 0 && (
        <span>
          {change.files} {change.files === 1 ? "file" : "files"}
          {(change.insertions > 0 || change.deletions > 0) && (
            <>
              {" "}
              {change.insertions > 0 && (
                <span className="text-success">+{change.insertions}</span>
              )}
              {change.deletions > 0 && (
                <span className="text-error">
                  {change.insertions > 0 ? " " : ""}−{change.deletions}
                </span>
              )}
            </>
          )}
        </span>
      )}
    </span>
  );
}

// ── Status badge ─────────────────────────────────────────────────────

function StatusBadge({ status }: { status: Repo["status"] }) {
  if (status === "ready")
    return (
      <Badge className="gap-1 bg-success/15 text-success">
        <CircleCheck className="size-3" /> ready
      </Badge>
    );
  if (status === "error")
    return (
      <Badge className="gap-1 bg-error/15 text-error">
        <CircleAlert className="size-3" /> error
      </Badge>
    );
  return (
    <Badge className="gap-1 bg-warning/15 text-warning">
      <Loader2 className="size-3 animate-spin" /> {status}
    </Badge>
  );
}

// ── Page ─────────────────────────────────────────────────────────────

export function ReposPage() {
  const qc = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);
  const [removing, setRemoving] = useState<Repo | null>(null);
  const [editing, setEditing] = useState<Repo | null>(null);
  const [discoverOpen, setDiscoverOpen] = useState(false);
  const [insightsOf, setInsightsOf] = useState<Repo | null>(null);

  const { data: repos = [], isLoading } = useQuery({
    queryKey: ["repos"],
    queryFn: reposApi.list,
    // Poll while any repo is mid clone/ingest so status flips to ready live.
    refetchInterval: (q) => {
      const rows = (q.state.data as Repo[] | undefined) ?? [];
      return rows.some((r) => BUSY.has(r.status)) ? 2000 : false;
    },
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["repos"] });

  const addMut = useMutation({
    mutationFn: reposApi.add,
    onSuccess: () => {
      toast.success("Clone started — indexing in the background");
      setAddOpen(false);
      invalidate();
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "add failed"),
  });

  const syncMut = useMutation({
    mutationFn: reposApi.sync,
    onSuccess: (_r, vars) => {
      toast.success(vars.full ? "Full re-index started" : "Sync started");
      invalidate();
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "sync failed"),
  });

  const syncAllMut = useMutation({
    mutationFn: reposApi.syncAll,
    onSuccess: (r, full) => {
      const verb = full ? "Full re-indexing" : "Syncing";
      toast.success(
        r.accepted > 0
          ? `${verb} ${r.accepted} ${r.accepted === 1 ? "repo" : "repos"}…`
          : "Nothing to sync — all repos busy",
      );
      invalidate();
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "sync-all failed"),
  });

  const pushMut = useMutation({
    mutationFn: reposApi.push,
    onSuccess: (r) => {
      if (r.up_to_date) {
        toast.success("Nothing to push — up to date with the remote");
      } else if (r.pr_status === "opened") {
        toast.success(`PR opened → ${r.dest_branch}${r.pr_url ? `: ${r.pr_url}` : ""}`);
      } else if (r.pr_status === "failed") {
        toast.error(
          `Pushed branch ${r.source_branch}, but opening the PR failed: ${r.pr_error ?? "unknown"}. Open it manually.`,
        );
      } else {
        toast.success(`Pushed to branch ${r.source_branch}`);
      }
      invalidate();
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "push failed"),
  });

  const anyBusy = repos.some((r) => BUSY.has(r.status));

  const removeMut = useMutation({
    mutationFn: ({ id, deleteFiles }: { id: string; deleteFiles: boolean }) =>
      reposApi.remove(id, deleteFiles),
    onSuccess: () => {
      toast.success("Repo removed");
      setRemoving(null);
      invalidate();
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "remove failed"),
  });

  const editMut = useMutation({
    mutationFn: reposApi.update,
    onSuccess: (r) => {
      // A refusal is a 200 with a reason — the credential was PROVEN bad and
      // deliberately not written. Surfacing it as success would be a lie.
      if (r.refused) {
        toast.error(r.refused);
        return;
      }
      const n = Object.keys(r.updated).length;
      toast.success(
        n === 0
          ? "No changes"
          : `Updated ${Object.keys(r.updated).join(", ")}${
              r.verified?.git.ok ? " — credential verified" : ""
            }`,
      );
      setEditing(null);
      invalidate();
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "update failed"),
  });

  const grouped = useMemo(() => {
    const m = new Map<string, Repo[]>();
    for (const r of repos) {
      const arr = m.get(r.workspace) ?? [];
      arr.push(r);
      m.set(r.workspace, arr);
    }
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [repos]);

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-6">
      <PageHeader
        title="Repos"
        subtitle="Clone repositories and prepare them for precise AI code work."
        right={
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              className="gap-2"
              disabled={repos.length === 0 || anyBusy || syncAllMut.isPending}
              onClick={() => syncAllMut.mutate(false)}
              title="Sync every repo (git pull + incremental re-ingest)"
            >
              <RefreshCw
                className={cn("size-4", (anyBusy || syncAllMut.isPending) && "animate-spin")}
              />
              Sync all
            </Button>
            <Button
              variant="outline"
              className="gap-2"
              disabled={repos.length === 0 || anyBusy || syncAllMut.isPending}
              onClick={() => syncAllMut.mutate(true)}
              title="Full re-index every repo — re-parses all files to re-tier the code graph (extracted / inferred / ambiguous). Use once after upgrading the tier engine."
            >
              <Layers className="size-4" />
              Re-tier all
            </Button>
            <Button
              variant="outline"
              className="gap-2"
              onClick={() => setDiscoverOpen((o) => !o)}
              title="List every repo your stored credentials can see on their forge"
            >
              <Search className="size-4" /> Discover
            </Button>
            <Button onClick={() => setAddOpen(true)} className="gap-2">
              <Plus className="size-4" /> Add repo
            </Button>
          </div>
        }
      />

      {isLoading ? (
        <div className="flex justify-center py-16 text-muted-foreground">
          <Loader2 className="size-5 animate-spin" />
        </div>
      ) : repos.length === 0 ? (
        <EmptyState
          icon={<Network className="size-6" />}
          title="No repositories yet"
          description="Add a git URL to clone, index, and query a codebase."
          action={
            <Button onClick={() => setAddOpen(true)} className="gap-2">
              <Plus className="size-4" /> Add your first repo
            </Button>
          }
        />
      ) : (
        <div className="flex flex-col gap-8">
          {grouped.map(([ws, rows]) => (
            <section key={ws} className="flex flex-col gap-3">
              <h2 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                {ws}
              </h2>
              <div className="flex flex-col divide-y divide-border rounded-lg border border-border">
                {rows.map((r) => (
                  <div
                    key={r.id}
                    className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="truncate font-medium">{r.name}</span>
                        <StatusBadge status={r.status} />
                        <span className="text-xs uppercase text-muted-foreground">
                          {r.tier}
                        </span>
                      </div>
                      <div className="mt-0.5 flex items-center gap-3 text-xs text-muted-foreground">
                        {r.default_branch && (
                          <span className="inline-flex items-center gap-1">
                            <GitBranch className="size-3" />
                            {r.default_branch}
                          </span>
                        )}
                        {r.last_indexed_sha && (
                          <span className="font-mono">
                            {r.last_indexed_sha.slice(0, 8)}
                          </span>
                        )}
                        {r.last_synced_at && (
                          <span
                            className={cn(
                              "inline-flex items-center gap-1",
                              (ageDays(r.last_synced_at) ?? 0) >= STALE_AFTER_DAYS &&
                                "font-medium text-warning",
                            )}
                            title={
                              (ageDays(r.last_synced_at) ?? 0) >= STALE_AFTER_DAYS
                                ? `Stale — last synced ${r.last_synced_at} UTC. Answers drawn from this repo may describe old code.`
                                : `Last synced ${r.last_synced_at} UTC`
                            }
                          >
                            <RefreshCw className="size-3" />
                            {formatAge(r.last_synced_at)}
                            {(ageDays(r.last_synced_at) ?? 0) >= STALE_AFTER_DAYS && " · stale"}
                          </span>
                        )}
                        <ChangeSummary change={r.last_change} />
                        {r.status === "error" && r.error && (
                          <span className="truncate text-error" title={r.error}>
                            {r.error}
                          </span>
                        )}
                      </div>
                    </div>

                    <div className="flex items-center gap-1">
                      <Button
                        asChild
                        variant="ghost"
                        size="sm"
                        disabled={r.status !== "ready"}
                        className={cn(r.status !== "ready" && "pointer-events-none opacity-40")}
                      >
                        <Link
                          to={`/repos/${encodeURIComponent(r.id)}`}
                          title="Open code intelligence"
                        >
                          <Network className="size-4" />
                        </Link>
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={r.status !== "ready"}
                        className={cn(r.status !== "ready" && "opacity-40")}
                        onClick={() => setInsightsOf(r)}
                        title="Insights — hub symbols + subsystems"
                      >
                        <Sparkles className="size-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={BUSY.has(r.status) || syncMut.isPending}
                        onClick={() => syncMut.mutate({ id: r.id, full: false })}
                        title="Sync (git pull + incremental re-ingest)"
                      >
                        <RefreshCw
                          className={cn(
                            "size-4",
                            syncMut.isPending &&
                              syncMut.variables?.id === r.id &&
                              !syncMut.variables?.full &&
                              "animate-spin",
                          )}
                        />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={BUSY.has(r.status) || syncMut.isPending}
                        onClick={() => syncMut.mutate({ id: r.id, full: true })}
                        title="Full re-index — re-parse every file to re-tier the code graph (extracted / inferred / ambiguous)"
                      >
                        <Layers
                          className={cn(
                            "size-4",
                            syncMut.isPending &&
                              syncMut.variables?.id === r.id &&
                              syncMut.variables?.full &&
                              "animate-spin",
                          )}
                        />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={BUSY.has(r.status) || pushMut.isPending}
                        onClick={() => pushMut.mutate(r.id)}
                        title="Push local commits to a new branch & open a PR (never pushes to the default branch)"
                      >
                        <Upload
                          className={cn(
                            "size-4",
                            pushMut.isPending &&
                              pushMut.variables === r.id &&
                              "animate-pulse",
                          )}
                        />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setEditing(r)}
                        title="Edit — credentials, url, tier, branch"
                      >
                        <Pencil className="size-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setRemoving(r)}
                        title="Remove"
                      >
                        <Trash2 className="size-4" />
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}

      <DiscoverSection
        open={discoverOpen}
        onToggle={() => setDiscoverOpen((o) => !o)}
        onAdd={(p) => addMut.mutate(p)}
        adding={addMut.isPending}
      />

      <AddRepoDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        submitting={addMut.isPending}
        onSubmit={(p) => addMut.mutate(p)}
      />

      <EditRepoDialog
        repo={editing}
        onOpenChange={(o) => !o && setEditing(null)}
        submitting={editMut.isPending}
        result={editMut.data ?? null}
        onSubmit={(p) => editing && editMut.mutate({ id: editing.id, ...p })}
      />

      <RemoveRepoDialog
        repo={removing}
        onOpenChange={(o) => !o && setRemoving(null)}
        submitting={removeMut.isPending}
        onConfirm={(deleteFiles) =>
          removing && removeMut.mutate({ id: removing.id, deleteFiles })
        }
      />

      <InsightsDialog
        repo={insightsOf}
        onOpenChange={(o) => !o && setInsightsOf(null)}
      />
    </div>
  );
}

// ── Insights dialog ──────────────────────────────────────────────────

function InsightsDialog({
  repo,
  onOpenChange,
}: {
  repo: Repo | null;
  onOpenChange: (o: boolean) => void;
}) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["repo-insights", repo?.id],
    queryFn: () => reposApi.insights(repo!.id),
    enabled: !!repo,
  });

  const maxDeg = data?.god_nodes[0]?.degree ?? 1;
  const maxFiles = data?.communities[0]?.files ?? 1;

  return (
    <Dialog open={!!repo} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="size-4" /> {repo?.name} — insights
          </DialogTitle>
          <DialogDescription>
            The hub symbols everything flows through, and the detected subsystems.
          </DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <div className="flex justify-center py-12 text-muted-foreground">
            <Loader2 className="size-5 animate-spin" />
          </div>
        ) : isError || !data ? (
          <p className="py-8 text-center text-sm text-muted-foreground">
            No code graph yet — try syncing the repo.
          </p>
        ) : (
          <div className="grid max-h-[60vh] gap-6 overflow-y-auto py-2 md:grid-cols-2">
            <section className="flex flex-col gap-2">
              <h3 className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-muted-foreground">
                <Network className="size-3.5" /> God nodes
              </h3>
              {data.god_nodes.length === 0 ? (
                <p className="text-sm text-muted-foreground">None found.</p>
              ) : (
                <ul className="flex flex-col gap-1.5">
                  {data.god_nodes.map((g) => (
                    <li key={g.node} className="flex flex-col gap-0.5">
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="truncate font-mono text-xs" title={g.node}>
                          {g.node}
                        </span>
                        <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                          {g.degree}
                        </span>
                      </div>
                      <div className="h-1 rounded-full bg-muted">
                        <div
                          className="h-1 rounded-full bg-primary"
                          style={{ width: `${(g.degree / maxDeg) * 100}%` }}
                        />
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="flex flex-col gap-2">
              <h3 className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wider text-muted-foreground">
                <Boxes className="size-3.5" /> Subsystems ({data.community_count})
              </h3>
              {data.communities.length === 0 ? (
                <p className="text-sm text-muted-foreground">None detected.</p>
              ) : (
                <ul className="flex flex-col gap-1.5">
                  {data.communities.slice(0, 15).map((c) => (
                    <li key={c.community} className="flex flex-col gap-0.5">
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="truncate text-xs">{c.community}</span>
                        <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                          {c.files} {c.files === 1 ? "file" : "files"}
                        </span>
                      </div>
                      <div className="h-1 rounded-full bg-muted">
                        <div
                          className="h-1 rounded-full bg-success"
                          style={{ width: `${(c.files / maxFiles) * 100}%` }}
                        />
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ── Add dialog ───────────────────────────────────────────────────────

function AddRepoDialog({
  open,
  onOpenChange,
  submitting,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  submitting: boolean;
  onSubmit: (p: AddPayload) => void;
}) {
  const [url, setUrl] = useState("");
  const [workspace, setWorkspace] = useState("default");
  const [tier, setTier] = useState<Tier>("air");
  const [tokenKey, setTokenKey] = useState("");
  const [username, setUsername] = useState("");
  const [prUsername, setPrUsername] = useState("");

  const submit = () => {
    if (!url.trim()) return;
    onSubmit({
      url: url.trim(),
      workspace: workspace.trim() || "default",
      tier,
      token_key: tokenKey.trim() || undefined,
      username: username.trim() || undefined,
      pr_username: prUsername.trim() || undefined,
    });
  };

  const field =
    "w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add repository</DialogTitle>
          <DialogDescription>
            Clone a git repo, index its code graph, and make it queryable.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4 py-2">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">Git URL</span>
            <input
              className={field}
              placeholder="https://github.com/org/repo"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              autoFocus
            />
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-muted-foreground">Workspace</span>
              <input
                className={field}
                value={workspace}
                onChange={(e) => setWorkspace(e.target.value)}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-muted-foreground">Tier</span>
              <select
                className={field}
                value={tier}
                onChange={(e) => setTier(e.target.value as Tier)}
              >
                {TIERS.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label} — {t.hint}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-muted-foreground">
                Keyring token name{" "}
                <span className="opacity-60">(private repos)</span>
              </span>
              <input
                className={field}
                placeholder="e.g. github-pat"
                value={tokenKey}
                onChange={(e) => setTokenKey(e.target.value)}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-muted-foreground">
                Git username{" "}
                <span className="opacity-60">(Bitbucket = username, not email)</span>
              </span>
              <input
                className={field}
                placeholder="auto per host"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </label>
          </div>

          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">
              PR author{" "}
              <span className="opacity-60">
                (opening pull requests — Bitbucket = Atlassian account email)
              </span>
            </span>
            <input
              className={field}
              placeholder="you@example.com"
              value={prUsername}
              onChange={(e) => setPrUsername(e.target.value)}
            />
          </label>
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={submitting || !url.trim()} className="gap-2">
            {submitting && <Loader2 className="size-4 animate-spin" />}
            Clone & index
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ── Discover section ─────────────────────────────────────────────────

/**
 * What can my credentials actually SEE that I am not indexing yet?
 *
 * Adding a repo used to require typing a clone url from memory — you cannot
 * type what you have forgotten, and a typo becomes a failed clone instead of a
 * missing repo. This lists the forge's answer and offers the gap as one-click
 * adds. Fetched on demand, not on page load: it is N live forge calls.
 */
function DiscoverSection({
  open,
  onToggle,
  onAdd,
  adding,
}: {
  open: boolean;
  onToggle: () => void;
  onAdd: (p: AddPayload) => void;
  adding: boolean;
}) {
  const [showManaged, setShowManaged] = useState(false);
  const { data, isFetching, refetch, error } = useQuery({
    queryKey: ["repos", "discover"],
    queryFn: reposApi.discover,
    enabled: open,
    staleTime: 60_000,
  });

  if (!open) return null;

  const rows = (data?.repos ?? []).filter((r) => showManaged || !r.managed);

  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
          Discover
        </h2>
        {data && (
          <span className="text-xs text-muted-foreground">
            {data.new} not indexed · {data.managed} already managed ·{" "}
            {data.sources.length}{" "}
            {data.sources.length === 1 ? "source" : "sources"}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={showManaged}
              onChange={(e) => setShowManaged(e.target.checked)}
            />
            Show already-managed
          </label>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => refetch()}
            disabled={isFetching}
            title="Re-ask every forge"
          >
            <RefreshCw className={cn("size-4", isFetching && "animate-spin")} />
          </Button>
          <Button variant="ghost" size="sm" onClick={onToggle}>
            Hide
          </Button>
        </div>
      </div>

      {/* A failed source is named, never swallowed: the repos behind a healthy
          credential still list, and the dead one says which it was. */}
      {data?.errors?.map((e) => (
        <div
          key={`${e.host}/${e.workspace ?? ""}`}
          className="flex items-start gap-2 rounded-md border border-error/30 bg-error/5 px-3 py-2 text-xs"
        >
          <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-error" />
          <span className="min-w-0">
            <span className="font-medium">
              {e.host}
              {e.workspace ? `/${e.workspace}` : ""}
            </span>
            <span className="block break-words opacity-80">{e.detail}</span>
          </span>
        </div>
      ))}

      {error ? (
        <div className="rounded-lg border border-error/30 bg-error/5 px-4 py-3 text-sm text-error">
          {error instanceof Error ? error.message : "discovery failed"}
        </div>
      ) : isFetching && !data ? (
        <div className="flex justify-center py-10 text-muted-foreground">
          <Loader2 className="size-5 animate-spin" />
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-lg border border-border px-4 py-6 text-center text-sm text-muted-foreground">
          {data?.repos.length
            ? "Everything your credentials can see is already indexed."
            : "No repositories found. Check that a credential is stored for this forge."}
        </div>
      ) : (
        <div className="flex flex-col divide-y divide-border rounded-lg border border-border">
          {rows.map((r) => (
            <div
              key={r.url}
              className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate font-medium">{r.full_name}</span>
                  {r.managed && (
                    <Badge className="gap-1 bg-success/15 text-success">
                      <CircleCheck className="size-3" /> managed
                    </Badge>
                  )}
                  {r.private === false && (
                    <span className="text-xs uppercase text-muted-foreground">
                      public
                    </span>
                  )}
                </div>
                <div className="mt-0.5 flex items-center gap-3 text-xs text-muted-foreground">
                  {r.default_branch && (
                    <span className="inline-flex items-center gap-1">
                      <GitBranch className="size-3" />
                      {r.default_branch}
                    </span>
                  )}
                  {r.language && <span>{r.language}</span>}
                  {r.updated_on && (
                    <span title={`Last activity on the forge: ${r.updated_on}`}>
                      pushed {formatAge(r.updated_on)}
                    </span>
                  )}
                  {r.description && (
                    <span className="truncate opacity-80">{r.description}</span>
                  )}
                </div>
              </div>

              {r.managed ? (
                <Button asChild variant="ghost" size="sm">
                  <Link to={`/repos/${encodeURIComponent(r.repo_id ?? "")}`}>
                    Open
                  </Link>
                </Button>
              ) : (
                <Button
                  size="sm"
                  className="gap-2"
                  disabled={adding}
                  onClick={() =>
                    // Send the credential that LISTED this repo. Omitting it does
                    // NOT make add_repo infer one — it stores null and clones
                    // anonymously, which fails on every private repo with an
                    // error about the repo rather than the missing credential.
                    onAdd({
                      url: r.url,
                      workspace: "default",
                      tier: "air",
                      token_key: r.token_key ?? undefined,
                      username: r.username ?? undefined,
                      pr_username: r.pr_username ?? undefined,
                    })
                  }
                >
                  <Plus className="size-4" /> Add
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

// ── Edit dialog ──────────────────────────────────────────────────────

/**
 * Rotate a repo's credential or fix its metadata without a re-clone.
 *
 * The two username fields are deliberately NOT one field: a Bitbucket Atlassian
 * API token authenticates git as the Bitbucket username and the REST API as the
 * Atlassian email. Collapsing them is what broke every managed repo on
 * 2026-09-03, so the form states which is which and the backend proves both
 * before writing.
 */
function EditRepoDialog({
  repo,
  onOpenChange,
  submitting,
  result,
  onSubmit,
}: {
  repo: Repo | null;
  onOpenChange: (o: boolean) => void;
  submitting: boolean;
  result: UpdateResult | null;
  onSubmit: (p: UpdatePayload) => void;
}) {
  const [url, setUrl] = useState("");
  const [tier, setTier] = useState<Tier>("air");
  const [branch, setBranch] = useState("");
  const [tokenKey, setTokenKey] = useState("");
  const [username, setUsername] = useState("");
  const [prUsername, setPrUsername] = useState("");
  const [force, setForce] = useState(false);

  // Re-seed whenever a different repo is opened, so the form always shows what
  // is actually stored rather than the previous repo's values.
  useEffect(() => {
    if (!repo) return;
    setUrl(repo.url);
    setTier(repo.tier);
    setBranch(repo.default_branch ?? "");
    setTokenKey(repo.token_key ?? "");
    setUsername(repo.username ?? "");
    setPrUsername(repo.pr_username ?? "");
    setForce(false);
  }, [repo]);

  const field =
    "w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring";

  // Send only what changed — an unchanged field must not bump updated_at, or
  // the audit trail lies about when the credential last moved.
  const diff = (): UpdatePayload => {
    if (!repo) return {};
    const out: UpdatePayload = {};
    const norm = (v: string) => (v.trim() === "" ? null : v.trim());
    if (url.trim() && url.trim() !== repo.url) out.url = url.trim();
    if (tier !== repo.tier) out.tier = tier;
    if (norm(branch) !== repo.default_branch) out.default_branch = norm(branch) ?? undefined;
    if (norm(tokenKey) !== repo.token_key) out.token_key = norm(tokenKey);
    if (norm(username) !== repo.username) out.username = norm(username);
    if (norm(prUsername) !== repo.pr_username) out.pr_username = norm(prUsername);
    return out;
  };

  const changes = repo ? diff() : {};
  const nChanges = Object.keys(changes).length;
  const refused = result?.refused;
  const v = result?.verified;

  return (
    <Dialog open={!!repo} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Edit {repo?.name}</DialogTitle>
          <DialogDescription>
            Rotate the credential or fix metadata in place — no re-clone, no
            re-ingest. A credential change is probed against the forge and
            refused if it fails.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4 py-2">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">Git URL</span>
            <input className={field} value={url} onChange={(e) => setUrl(e.target.value)} />
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-muted-foreground">Tier</span>
              <select
                className={field}
                value={tier}
                onChange={(e) => setTier(e.target.value as Tier)}
              >
                {TIERS.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label} — {t.hint}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-muted-foreground">Default branch</span>
              <input
                className={field}
                placeholder="main"
                value={branch}
                onChange={(e) => setBranch(e.target.value)}
              />
            </label>
          </div>

          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">
              Keyring token name{" "}
              <span className="opacity-60">(blank = public / anonymous)</span>
            </span>
            <input
              className={field}
              placeholder="e.g. bitbucket-access-token"
              value={tokenKey}
              onChange={(e) => setTokenKey(e.target.value)}
            />
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-muted-foreground">
                Git username{" "}
                <span className="opacity-60">(clone · sync · push)</span>
              </span>
              <input
                className={field}
                placeholder="auto per host"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-muted-foreground">
                REST / PR user{" "}
                <span className="opacity-60">(pull requests)</span>
              </span>
              <input
                className={field}
                placeholder="you@example.com"
                value={prUsername}
                onChange={(e) => setPrUsername(e.target.value)}
              />
            </label>
          </div>

          <p className="text-xs text-muted-foreground">
            Bitbucket Atlassian API tokens need both, and they differ: git wants
            your Bitbucket <em>username</em>, the REST API wants your Atlassian{" "}
            <em>account email</em>.
          </p>

          {v && (
            <div className="flex flex-col gap-1 rounded-md border border-border p-3 text-xs">
              <VerifyLine label="git (clone · sync · push)" r={v.git} />
              {v.rest && <VerifyLine label="REST (pull requests)" r={v.rest} />}
            </div>
          )}

          {refused && (
            <label className="flex items-start gap-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={force}
                onChange={(e) => setForce(e.target.checked)}
                className="mt-0.5"
              />
              <span>
                Save anyway. The credential is proven not to work — sync and push
                will keep failing until it is fixed.
              </span>
            </label>
          )}
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => onSubmit({ ...changes, force })}
            disabled={submitting || nChanges === 0}
            className="gap-2"
          >
            {submitting && <Loader2 className="size-4 animate-spin" />}
            {nChanges === 0
              ? "No changes"
              : force
                ? `Force-save ${nChanges}`
                : `Verify & save ${nChanges}`}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function VerifyLine({
  label,
  r,
}: {
  label: string;
  r: { ok: boolean; detail: string };
}) {
  return (
    <div className="flex items-start gap-2">
      {r.ok ? (
        <CircleCheck className="mt-0.5 size-3.5 shrink-0 text-emerald-500" />
      ) : (
        <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-destructive" />
      )}
      <span className="min-w-0">
        <span className="font-medium">{label}</span>
        {!r.ok && (
          <span className="block break-words opacity-70">{r.detail}</span>
        )}
      </span>
    </div>
  );
}

// ── Remove dialog ────────────────────────────────────────────────────

function RemoveRepoDialog({
  repo,
  onOpenChange,
  submitting,
  onConfirm,
}: {
  repo: Repo | null;
  onOpenChange: (o: boolean) => void;
  submitting: boolean;
  onConfirm: (deleteFiles: boolean) => void;
}) {
  const [deleteFiles, setDeleteFiles] = useState(false);

  return (
    <Dialog open={!!repo} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Remove {repo?.name}?</DialogTitle>
          <DialogDescription>
            Unregisters the repo and deactivates its project. Its code graph stays
            in the knowledge base unless you also delete the files.
          </DialogDescription>
        </DialogHeader>

        <label className="flex items-center gap-2 py-2 text-sm">
          <input
            type="checkbox"
            checked={deleteFiles}
            onChange={(e) => setDeleteFiles(e.target.checked)}
          />
          Also delete the cloned files from disk
        </label>

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={() => onConfirm(deleteFiles)}
            disabled={submitting}
            className="gap-2"
          >
            {submitting && <Loader2 className="size-4 animate-spin" />}
            Remove
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
