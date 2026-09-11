import { useEffect, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Search,
  Code2,
  RefreshCw,
  Loader2,
  X,
  ArrowRight,
  FileText,
  ChevronDown,
  ChevronRight,
  FolderPlus,
  FolderOpen,
} from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { PageHeader } from "@/components/shell/page-header";
import { toast } from "@/components/ui/toast";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * /cortex — codebase search + index visibility.
 *
 * Surfaces the cortex MCP tools to the browser: semantic search,
 * literal (ripgrep) search, concept routing, stats, and reindex.
 *
 * Backend: /api/cortex/* (see src/okuro/orchestrator/api/cortex.py).
 */

// ── Types (mirror backend Pydantic models) ──────────────────────────

type CortexStats = {
  files_indexed: number;
  total_documents: number;
  sections_indexed: number;
  content_chunks: number;
  embedding_model: string;
  root: string;
};

type SearchHit = {
  path: string;
  score: number;
  snippet: string | null;
  purpose: string | null;
  matched_section: string | null;
};

type SearchResponse = {
  query: string;
  results: SearchHit[];
};

type CodeHit = {
  path: string;
  line: number;
  text: string;
};

type CodeSearchResponse = {
  query: string;
  backend: string;
  matches: CodeHit[];
};

type HeaderResponse = {
  path: string;
  found: boolean;
  purpose: string | null;
  role: string | null;
  index: { description: string; line: number | null }[];
};

type FileSliceResponse = {
  path: string;
  start_line: number;
  end_line: number;
  total_lines: number;
  content: string;
};

type ReindexResponse = {
  job_id: string;
  status: string;
  started_at: number;
};

type ReindexStatus = {
  job_id: string;
  status: "running" | "done" | "failed";
  started_at: number;
  finished_at: number | null;
  total: number | null;
  indexed: number | null;
  error: string | null;
  root: string;
};

type RegisteredRoot = {
  slug: string | null;
  path: string;
  files: number;
  documents: number;
};

type ProjectsResponse = {
  schema_version: string;
  roots: RegisteredRoot[];
};

type RegisterProjectRequest = {
  path: string;
  slug?: string;
  name?: string;
  reindex?: boolean;
};

type RegisterProjectResponse = {
  slug: string;
  name: string;
  path: string;
  action: "created" | "updated" | "already_active";
  pre_existing_slug: string | null;
  reindex_job_id: string | null;
};

// ── API helpers ──────────────────────────────────────────────────────

const cortexApi = {
  stats: () => api<CortexStats>("/api/cortex/stats"),
  search: (q: string, n: number, fileType?: string, project?: string) => {
    const params = new URLSearchParams({ q, n: String(n) });
    if (fileType) params.set("file_type", fileType);
    if (project) params.set("project", project);
    return api<SearchResponse>(`/api/cortex/search?${params.toString()}`);
  },
  searchCode: (q: string, n: number, path?: string) => {
    const params = new URLSearchParams({ q, n: String(n) });
    if (path) params.set("path", path);
    return api<CodeSearchResponse>(`/api/cortex/search-code?${params.toString()}`);
  },
  route: (q: string) =>
    api<SearchResponse>(`/api/cortex/route?q=${encodeURIComponent(q)}`),
  header: (path: string) =>
    api<HeaderResponse>(`/api/cortex/header?path=${encodeURIComponent(path)}`),
  fileSlice: (path: string, start: number, end: number) => {
    const p = new URLSearchParams({
      path,
      start: String(start),
      end: String(end),
    });
    return api<FileSliceResponse>(`/api/cortex/file?${p.toString()}`);
  },
  reindex: (path?: string) => {
    const qp = path ? `?path=${encodeURIComponent(path)}` : "";
    return api<ReindexResponse>(`/api/cortex/reindex${qp}`, { method: "POST" });
  },
  reindexStatus: (jobId: string) =>
    api<ReindexStatus>(`/api/cortex/reindex/status?job=${encodeURIComponent(jobId)}`),
  projects: () => api<ProjectsResponse>("/api/cortex/projects"),
  registerProject: (body: RegisterProjectRequest) =>
    api<RegisterProjectResponse>("/api/cortex/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
};

// ── Page ─────────────────────────────────────────────────────────────

export function CortexPage() {
  const [mode, setMode] = useState<"semantic" | "code" | "route">("semantic");
  const [query, setQuery] = useState("");
  const [committedQuery, setCommittedQuery] = useState("");
  const [pathFilter, setPathFilter] = useState("");
  const [fileType, setFileType] = useState("");
  const [resultCount, setResultCount] = useState(20);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [selectedHit, setSelectedHit] = useState<SearchHit | CodeHit | null>(null);
  const [reindexJobId, setReindexJobId] = useState<string | null>(null);

  const queryClient = useQueryClient();

  // ── Stats ──────────────────────────────────────────────────────────
  const statsQuery = useQuery({
    queryKey: ["cortex", "stats"],
    queryFn: cortexApi.stats,
    staleTime: 30_000,
  });

  // ── Search ─────────────────────────────────────────────────────────
  const semanticQuery = useQuery({
    queryKey: ["cortex", "search", committedQuery, resultCount, fileType, pathFilter],
    queryFn: () =>
      cortexApi.search(
        committedQuery,
        resultCount,
        fileType || undefined,
        pathFilter || undefined,
      ),
    enabled: mode === "semantic" && committedQuery.length > 0,
  });

  const codeQuery = useQuery({
    queryKey: ["cortex", "search-code", committedQuery, resultCount, pathFilter],
    queryFn: () =>
      cortexApi.searchCode(committedQuery, resultCount, pathFilter || undefined),
    enabled: mode === "code" && committedQuery.length > 0,
  });

  const routeQuery = useQuery({
    queryKey: ["cortex", "route", committedQuery],
    queryFn: () => cortexApi.route(committedQuery),
    enabled: mode === "route" && committedQuery.length > 0,
  });

  // ── Reindex ────────────────────────────────────────────────────────
  const reindexMutation = useMutation({
    mutationFn: () => cortexApi.reindex(),
    onSuccess: (data) => {
      setReindexJobId(data.job_id);
      toast.success(`Reindex started — job ${data.job_id}`);
    },
    onError: (err) => {
      toast.error(err instanceof Error ? err.message : "Reindex failed");
    },
  });

  const reindexStatusQuery = useQuery({
    queryKey: ["cortex", "reindex-status", reindexJobId],
    queryFn: () => cortexApi.reindexStatus(reindexJobId!),
    enabled: reindexJobId !== null,
    refetchInterval: (q) => {
      const d = q.state.data as ReindexStatus | undefined;
      return d && d.status === "running" ? 1500 : false;
    },
  });

  // Phase 5 — toast ONCE per terminal job. The effect re-runs on every
  // reindexStatusQuery.data ref change (each poll tick returns a fresh
  // object), so a status that stays "done" for a tick or two re-fired the
  // toast. Track which (jobId:status) we've already announced.
  const toastedReindexRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    const status = reindexStatusQuery.data;
    if (!status || reindexJobId === null) return;
    const key = `${reindexJobId}:${status.status}`;
    if (status.status === "done") {
      queryClient.invalidateQueries({ queryKey: ["cortex", "stats"] });
      if (!toastedReindexRef.current.has(key)) {
        toastedReindexRef.current.add(key);
        toast.success(
          `Reindex done — ${status.indexed ?? 0}/${status.total ?? 0} files`,
        );
      }
      const t = setTimeout(() => setReindexJobId(null), 5_000);
      return () => clearTimeout(t);
    }
    if (status.status === "failed") {
      if (!toastedReindexRef.current.has(key)) {
        toastedReindexRef.current.add(key);
        toast.error(`Reindex failed: ${status.error ?? "unknown"}`);
      }
      const t = setTimeout(() => setReindexJobId(null), 8_000);
      return () => clearTimeout(t);
    }
  }, [reindexStatusQuery.data, queryClient, reindexJobId]);

  // ── Handlers ───────────────────────────────────────────────────────
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = query.trim();
    setCommittedQuery(trimmed);
    setSelectedHit(null);
  };

  const stats = statsQuery.data;
  const reindexStatus = reindexStatusQuery.data;
  const isReindexing =
    reindexStatus?.status === "running" || reindexMutation.isPending;

  return (
    <div className="page-shell space-y-8">
      <PageHeader
        title="Cortex"
        subtitle="Codebase search — by meaning, by exact text, or top files per concept"
      />

      {/* Stats bar */}
      <StatsBar
        stats={stats}
        loading={statsQuery.isLoading}
        onReindex={() => reindexMutation.mutate()}
        reindexing={isReindexing}
      />

      {/* Reindex progress */}
      {reindexStatus && reindexStatus.status === "running" && (
        <ReindexProgressBar status={reindexStatus} />
      )}
      {reindexStatus && reindexStatus.status === "failed" && (
        <div className="rounded border border-error/50 bg-error/10 px-3 py-2 text-xs text-error">
          Reindex failed: {reindexStatus.error ?? "unknown"}
        </div>
      )}

      {/* Registered project roots */}
      <RegisteredRootsPanel onReindexStarted={setReindexJobId} />

      {/* Mode tabs + search */}
      <Tabs value={mode} onValueChange={(v) => setMode(v as typeof mode)}>
        <TabsList>
          <TabsTrigger value="semantic">
            <Search className="h-3.5 w-3.5 mr-1.5" />
            Semantic
          </TabsTrigger>
          <TabsTrigger value="code">
            <Code2 className="h-3.5 w-3.5 mr-1.5" />
            Code
          </TabsTrigger>
          <TabsTrigger value="route">
            <ArrowRight className="h-3.5 w-3.5 mr-1.5" />
            Route
          </TabsTrigger>
        </TabsList>

        <form onSubmit={submit} className="mt-4 space-y-2">
          <div className="relative">
            <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-tertiary" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={
                mode === "semantic"
                  ? "Search by meaning — e.g. 'bootstrap assembly'"
                  : mode === "code"
                    ? "Exact string or regex — e.g. 'async def'"
                    : "Concept to locate — e.g. 'keyring unlock session'"
              }
              className="bg-surface pl-8 pr-20 text-sm"
            />
            {query && (
              <button
                type="button"
                onClick={() => {
                  setQuery("");
                  setCommittedQuery("");
                }}
                aria-label="Clear query"
                className="absolute right-16 top-2 text-tertiary hover:text-fg-muted"
              >
                <X className="h-4 w-4" />
              </button>
            )}
            <Button
              type="submit"
              size="sm"
              className="absolute right-1 top-1 h-7 text-xs"
              disabled={!query.trim()}
            >
              Search
            </Button>
          </div>

          {/* Advanced row */}
          <div>
            <button
              type="button"
              onClick={() => setAdvancedOpen((v) => !v)}
              className="text-2xs uppercase tracking-wider text-tertiary hover:text-fg-muted inline-flex items-center gap-1"
            >
              {advancedOpen ? (
                <ChevronDown className="h-3 w-3" />
              ) : (
                <ChevronRight className="h-3 w-3" />
              )}
              Advanced
            </button>
            {advancedOpen && (
              <div className="mt-2 grid grid-cols-1 sm:grid-cols-3 gap-2">
                <div>
                  <label className="text-2xs uppercase tracking-wider text-tertiary">
                    Path filter
                  </label>
                  <Input
                    value={pathFilter}
                    onChange={(e) => setPathFilter(e.target.value)}
                    placeholder="src/okuro/cortex"
                    className="bg-surface text-xs h-8"
                  />
                </div>
                {mode === "semantic" && (
                  <div>
                    <label className="text-2xs uppercase tracking-wider text-tertiary">
                      File type
                    </label>
                    <select
                      value={fileType}
                      onChange={(e) => setFileType(e.target.value)}
                      className="w-full h-8 rounded border border-border bg-surface px-2 text-xs text-fg"
                    >
                      <option value="">Any</option>
                      <option value="code">code</option>
                      <option value="doc">doc</option>
                      <option value="config">config</option>
                      <option value="data">data</option>
                      <option value="template">template</option>
                      <option value="test">test</option>
                    </select>
                  </div>
                )}
                {mode !== "route" && (
                  <div>
                    <label className="text-2xs uppercase tracking-wider text-tertiary">
                      Max results
                    </label>
                    <Input
                      type="number"
                      value={resultCount}
                      onChange={(e) =>
                        setResultCount(Math.max(1, Math.min(200, Number(e.target.value) || 20)))
                      }
                      min={1}
                      max={200}
                      className="bg-surface text-xs h-8"
                    />
                  </div>
                )}
              </div>
            )}
          </div>
        </form>

        <div className="mt-4 grid grid-cols-1 lg:grid-cols-[1fr_420px] gap-4">
          {/* Results column */}
          <div>
            <TabsContent value="semantic" className="mt-0">
              <SemanticResults
                query={semanticQuery}
                committed={committedQuery}
                onSelect={setSelectedHit}
                selected={selectedHit}
              />
            </TabsContent>
            <TabsContent value="code" className="mt-0">
              <CodeResults
                query={codeQuery}
                committed={committedQuery}
                onSelect={setSelectedHit}
                selected={selectedHit}
              />
            </TabsContent>
            <TabsContent value="route" className="mt-0">
              <SemanticResults
                query={routeQuery}
                committed={committedQuery}
                onSelect={setSelectedHit}
                selected={selectedHit}
                label="Top-5 files"
              />
            </TabsContent>
          </div>

          {/* Detail drawer */}
          <div className="border-l border-border pl-4 hidden lg:block">
            <DetailPane hit={selectedHit} />
          </div>
        </div>
      </Tabs>

      {/* Mobile detail (below results) */}
      {selectedHit && (
        <div className="lg:hidden mt-4 border-t border-border pt-4">
          <DetailPane hit={selectedHit} />
        </div>
      )}
    </div>
  );
}

// ── Stats ────────────────────────────────────────────────────────────

function StatsBar({
  stats,
  loading,
  onReindex,
  reindexing,
}: {
  stats: CortexStats | undefined;
  loading: boolean;
  onReindex: () => void;
  reindexing: boolean;
}) {
  return (
    <div className="rounded border border-border bg-surface p-3 flex items-center flex-wrap gap-6">
      <StatItem label="Files" value={stats?.files_indexed ?? "—"} loading={loading} />
      <StatItem label="Documents" value={stats?.total_documents ?? "—"} loading={loading} />
      <StatItem label="Sections" value={stats?.sections_indexed ?? "—"} loading={loading} />
      <div className="min-w-0">
        <div className="text-2xs uppercase tracking-wider text-tertiary">Model</div>
        <div className="text-xs text-fg truncate font-mono">
          {stats?.embedding_model ?? "—"}
        </div>
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-2xs uppercase tracking-wider text-tertiary">Root</div>
        <div className="text-xs text-fg truncate font-mono">{stats?.root ?? "—"}</div>
      </div>
      <Button
        size="sm"
        variant="outline"
        onClick={onReindex}
        disabled={reindexing}
        className="ml-auto"
      >
        {reindexing ? (
          <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
        ) : (
          <RefreshCw className="h-3.5 w-3.5 mr-1.5" />
        )}
        Reindex now
      </Button>
    </div>
  );
}

function StatItem({
  label,
  value,
  loading,
}: {
  label: string;
  value: number | string;
  loading: boolean;
}) {
  return (
    <div className="min-w-[72px]">
      <div className="text-2xs uppercase tracking-wider text-tertiary">
        {label}
      </div>
      <div className="text-sm font-medium text-fg tabular-nums">
        {loading ? "…" : value}
      </div>
    </div>
  );
}

function RegisteredRootsPanel({
  onReindexStarted,
}: {
  onReindexStarted: (jobId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [path, setPath] = useState("");
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [reindex, setReindex] = useState(true);
  const queryClient = useQueryClient();

  const projectsQuery = useQuery({
    queryKey: ["cortex", "projects"],
    queryFn: cortexApi.projects,
    staleTime: 30_000,
  });

  const registerMutation = useMutation({
    mutationFn: (body: RegisterProjectRequest) => cortexApi.registerProject(body),
    onSuccess: (data) => {
      const verb =
        data.action === "created"
          ? "Registered"
          : data.action === "updated"
            ? "Updated"
            : "Already active:";
      toast.success(`${verb} ${data.slug} at ${data.path}`);
      setPath("");
      setSlug("");
      setName("");
      queryClient.invalidateQueries({ queryKey: ["cortex", "projects"] });
      queryClient.invalidateQueries({ queryKey: ["cortex", "stats"] });
      if (data.reindex_job_id) {
        onReindexStarted(data.reindex_job_id);
      }
    },
    onError: (err) => {
      toast.error(err instanceof Error ? err.message : "Register failed");
    },
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = path.trim();
    if (!trimmed) return;
    registerMutation.mutate({
      path: trimmed,
      slug: slug.trim() || undefined,
      name: name.trim() || undefined,
      reindex,
    });
  };

  const roots = projectsQuery.data?.roots ?? [];

  return (
    <div className="rounded border border-border bg-surface/50">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full px-3 py-2 flex items-center justify-between text-left hover:bg-surface"
      >
        <div className="flex items-center gap-2">
          <FolderOpen className="h-4 w-4 text-tertiary" />
          <span className="text-sm font-medium text-fg">
            Registered project roots
          </span>
          <Badge variant="secondary" className="tabular-nums">
            {roots.length}
          </Badge>
        </div>
        {open ? (
          <ChevronDown className="h-4 w-4 text-tertiary" />
        ) : (
          <ChevronRight className="h-4 w-4 text-tertiary" />
        )}
      </button>

      {open && (
        <div className="border-t border-border p-3 space-y-4">
          {/* Existing roots table */}
          {projectsQuery.isLoading ? (
            <div className="text-xs text-tertiary">Loading…</div>
          ) : roots.length === 0 ? (
            <div className="text-xs text-tertiary">No registered roots.</div>
          ) : (
            <div className="overflow-x-auto rounded border border-border">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-background text-2xs uppercase tracking-wider text-tertiary">
                    <th className="text-left px-2 py-1.5 font-medium">Slug</th>
                    <th className="text-left px-2 py-1.5 font-medium">Path</th>
                    <th className="text-right px-2 py-1.5 font-medium tabular-nums">
                      Files
                    </th>
                    <th className="text-right px-2 py-1.5 font-medium tabular-nums">
                      Docs
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {roots.map((r) => (
                    <tr key={r.path} className="border-t border-border">
                      <td className="px-2 py-1.5">
                        {r.slug ? (
                          <Badge variant="secondary" className="font-mono">
                            {r.slug}
                          </Badge>
                        ) : (
                          <span className="text-tertiary italic">
                            (unregistered)
                          </span>
                        )}
                      </td>
                      <td className="px-2 py-1.5 font-mono text-fg-muted truncate max-w-[360px]">
                        {r.path}
                      </td>
                      <td className="px-2 py-1.5 text-right tabular-nums text-fg">
                        {r.files}
                      </td>
                      <td className="px-2 py-1.5 text-right tabular-nums text-fg-muted">
                        {r.documents}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Register form */}
          <form onSubmit={onSubmit} className="space-y-2">
            <div className="flex items-center gap-2">
              <FolderPlus className="h-4 w-4 text-tertiary" />
              <span className="text-2xs uppercase tracking-wider text-tertiary">
                Add a project folder
              </span>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-[2fr_1fr_1fr] gap-2">
              <Input
                value={path}
                onChange={(e) => setPath(e.target.value)}
                placeholder="/absolute/path/to/project"
                className="bg-surface text-xs h-8 font-mono"
                required
              />
              <Input
                value={slug}
                onChange={(e) => setSlug(e.target.value)}
                placeholder="slug (optional)"
                className="bg-surface text-xs h-8"
              />
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="display name (optional)"
                className="bg-surface text-xs h-8"
              />
            </div>
            <div className="flex items-center justify-between">
              <label className="inline-flex items-center gap-2 text-xs text-fg-muted">
                <input
                  type="checkbox"
                  checked={reindex}
                  onChange={(e) => setReindex(e.target.checked)}
                  className="h-3.5 w-3.5 rounded border-border"
                />
                Reindex immediately after registering
              </label>
              <Button
                type="submit"
                size="sm"
                className="h-7 text-xs"
                disabled={!path.trim() || registerMutation.isPending}
              >
                {registerMutation.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />
                ) : (
                  <FolderPlus className="h-3.5 w-3.5 mr-1.5" />
                )}
                Register
              </Button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}

function ReindexProgressBar({ status }: { status: ReindexStatus }) {
  const indexed = status.indexed ?? 0;
  const total = status.total ?? 0;
  const pct = total > 0 ? Math.round((indexed / total) * 100) : null;

  return (
    <div className="rounded border border-accent/50 bg-accent/5 p-3 space-y-1.5">
      <div className="flex items-center justify-between text-xs">
        <span className="text-fg font-medium">
          Reindexing — job {status.job_id}
        </span>
        <span className="text-tertiary tabular-nums">
          {indexed}
          {total > 0 && ` / ${total}`}
          {pct !== null && ` (${pct}%)`}
        </span>
      </div>
      <div className="h-1.5 rounded-full bg-border overflow-hidden">
        <div
          className={cn(
            "h-full bg-accent transition-all duration-500",
            pct === null && "animate-pulse w-1/3",
          )}
          style={pct !== null ? { width: `${pct}%` } : undefined}
        />
      </div>
    </div>
  );
}

// ── Results ──────────────────────────────────────────────────────────

function SemanticResults({
  query,
  committed,
  onSelect,
  selected,
  label,
}: {
  query: ReturnType<typeof useQuery<SearchResponse>>;
  committed: string;
  onSelect: (h: SearchHit) => void;
  selected: SearchHit | CodeHit | null;
  label?: string;
}) {
  if (!committed) {
    return (
      <EmptyState
        title="Enter a query"
        description="Semantic search understands intent — try concepts like 'bootstrap', 'keyring unlock', 'MCP middleware'."
      />
    );
  }
  if (query.isLoading) {
    return <div className="text-sm text-tertiary">Searching…</div>;
  }
  if (query.isError) {
    return (
      <EmptyState
        title="Search failed"
        description={
          query.error instanceof Error ? query.error.message : "Unknown error"
        }
      />
    );
  }
  const results = query.data?.results ?? [];
  if (results.length === 0) {
    return <EmptyState title="No results" description={`No matches for "${committed}"`} />;
  }

  return (
    <div className="space-y-2">
      <div className="text-2xs uppercase tracking-wider text-tertiary">
        {label ?? `${results.length} hits`}
      </div>
      {results.map((hit) => {
        const isSelected =
          selected && "score" in selected && selected.path === hit.path;
        return (
          <button
            key={hit.path}
            onClick={() => onSelect(hit)}
            className={cn(
              "w-full text-left rounded border p-3 transition-colors hover:bg-surface",
              isSelected
                ? "border-accent bg-accent/5"
                : "border-border bg-background",
            )}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <div className="font-mono text-xs text-fg truncate">
                  {hit.path}
                </div>
                {hit.purpose && (
                  <div className="text-xs text-fg-muted mt-0.5 line-clamp-1">
                    {hit.purpose}
                  </div>
                )}
              </div>
              <Badge variant="secondary" className="shrink-0 tabular-nums">
                {hit.score.toFixed(2)}
              </Badge>
            </div>
            {hit.snippet && (
              <div className="mt-2 text-2xs text-tertiary font-mono line-clamp-2 whitespace-pre-wrap">
                {hit.snippet}
              </div>
            )}
          </button>
        );
      })}
    </div>
  );
}

function CodeResults({
  query,
  committed,
  onSelect,
  selected,
}: {
  query: ReturnType<typeof useQuery<CodeSearchResponse>>;
  committed: string;
  onSelect: (h: CodeHit) => void;
  selected: SearchHit | CodeHit | null;
}) {
  if (!committed) {
    return (
      <EmptyState
        title="Enter a pattern"
        description="Literal match via ripgrep. Regex supported. Example: 'async def \\w+_handler'."
      />
    );
  }
  if (query.isLoading) {
    return <div className="text-sm text-tertiary">Grepping…</div>;
  }
  if (query.isError) {
    return (
      <EmptyState
        title="Search failed"
        description={
          query.error instanceof Error ? query.error.message : "Unknown error"
        }
      />
    );
  }
  const matches = query.data?.matches ?? [];
  if (matches.length === 0) {
    return <EmptyState title="No matches" description={`Nothing matched "${committed}"`} />;
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="text-2xs uppercase tracking-wider text-tertiary">
          {matches.length} matches
        </div>
        <Badge variant="outline" className="text-2xs">
          {query.data?.backend}
        </Badge>
      </div>
      {matches.map((hit, i) => {
        const isSelected =
          selected &&
          "line" in selected &&
          selected.path === hit.path &&
          selected.line === hit.line;
        return (
          <button
            key={`${hit.path}:${hit.line}:${i}`}
            onClick={() => onSelect(hit)}
            className={cn(
              "w-full text-left rounded border p-2 transition-colors hover:bg-surface",
              isSelected ? "border-accent bg-accent/5" : "border-border bg-background",
            )}
          >
            <div className="font-mono text-2xs text-tertiary truncate">
              {hit.path}:{hit.line}
            </div>
            <div className="mt-1 font-mono text-xs text-fg whitespace-pre-wrap break-all">
              {hit.text}
            </div>
          </button>
        );
      })}
    </div>
  );
}

// ── Detail pane ──────────────────────────────────────────────────────

function DetailPane({ hit }: { hit: SearchHit | CodeHit | null }) {
  if (!hit) {
    return (
      <div className="text-xs text-tertiary py-6 text-center">
        Select a result to see its header + source.
      </div>
    );
  }

  const isCodeHit = "line" in hit;
  const targetLine = isCodeHit ? hit.line : 1;

  return (
    <div className="space-y-4">
      <div>
        <div className="flex items-center gap-1.5">
          <FileText className="h-3.5 w-3.5 text-tertiary" />
          <div className="text-2xs uppercase tracking-wider text-tertiary">
            File
          </div>
        </div>
        <div className="mt-1 font-mono text-xs text-fg break-all">{hit.path}</div>
        <button
          className="mt-1 text-2xs text-tertiary hover:text-fg-muted underline underline-offset-2"
          onClick={() => navigator.clipboard.writeText(hit.path).then(
            () => toast.success("Path copied"),
            () => toast.error("Copy failed"),
          )}
        >
          Copy path
        </button>
      </div>

      <HeaderBlock path={hit.path} />

      <SectionReader path={hit.path} initialLine={targetLine} />
    </div>
  );
}

function HeaderBlock({ path }: { path: string }) {
  const q = useQuery({
    queryKey: ["cortex", "header", path],
    queryFn: () => cortexApi.header(path),
    staleTime: 30_000,
  });

  if (q.isLoading) {
    return (
      <div className="text-2xs text-tertiary">Loading header…</div>
    );
  }
  if (q.isError) {
    return (
      <div className="text-2xs text-error">Header load failed</div>
    );
  }
  const header = q.data!;
  if (!header.found) {
    return (
      <div className="text-2xs text-tertiary">
        No AGENT_HEADER in this file
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <div>
        <div className="text-2xs uppercase tracking-wider text-tertiary">
          Purpose
        </div>
        <div className="text-xs text-fg mt-0.5">{header.purpose || "—"}</div>
      </div>
      {header.role && (
        <div>
          <div className="text-2xs uppercase tracking-wider text-tertiary">
            Role
          </div>
          <Badge variant="outline" className="mt-0.5 text-2xs">
            {header.role}
          </Badge>
        </div>
      )}
      {header.index.length > 0 && (
        <div>
          <div className="text-2xs uppercase tracking-wider text-tertiary">
            Sections ({header.index.length})
          </div>
          <ul className="mt-1 space-y-0.5 max-h-48 overflow-y-auto pr-1">
            {header.index.map((e, i) => (
              <li
                key={i}
                className="text-2xs font-mono text-fg-muted truncate"
              >
                {e.description}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function SectionReader({
  path,
  initialLine,
}: {
  path: string;
  initialLine: number;
}) {
  const windowBefore = 20;
  const windowAfter = 40;
  const [start, setStart] = useState(Math.max(1, initialLine - windowBefore));
  const [end, setEnd] = useState(initialLine + windowAfter);
  const pathRef = useRef(path);

  // Reset range when selection changes path.
  useEffect(() => {
    if (pathRef.current !== path) {
      pathRef.current = path;
      setStart(Math.max(1, initialLine - windowBefore));
      setEnd(initialLine + windowAfter);
    }
  }, [path, initialLine]);

  const q = useQuery({
    queryKey: ["cortex", "file", path, start, end],
    queryFn: () => cortexApi.fileSlice(path, start, end),
  });

  return (
    <div>
      <div className="flex items-center justify-between">
        <div className="text-2xs uppercase tracking-wider text-tertiary">
          Read section
        </div>
        {q.data && (
          <div className="text-2xs text-tertiary tabular-nums">
            L{q.data.start_line}–{q.data.end_line} / {q.data.total_lines}
          </div>
        )}
      </div>
      <div className="mt-1 flex gap-2 items-center">
        <Input
          type="number"
          value={start}
          onChange={(e) => setStart(Math.max(1, Number(e.target.value) || 1))}
          min={1}
          className="h-7 w-20 text-2xs"
          aria-label="Start line"
        />
        <span className="text-2xs text-tertiary">→</span>
        <Input
          type="number"
          value={end}
          onChange={(e) => setEnd(Math.max(start, Number(e.target.value) || start))}
          min={start}
          className="h-7 w-20 text-2xs"
          aria-label="End line"
        />
      </div>
      <div className="mt-2 rounded border border-border bg-surface max-h-96 overflow-auto">
        {q.isLoading ? (
          <div className="p-3 text-2xs text-tertiary">Loading…</div>
        ) : q.isError ? (
          <div className="p-3 text-2xs text-error">
            {q.error instanceof Error ? q.error.message : "Load failed"}
          </div>
        ) : (
          <pre className="p-3 text-2xs font-mono text-fg whitespace-pre-wrap break-words leading-relaxed">
            {q.data?.content || "(empty)"}
          </pre>
        )}
      </div>
    </div>
  );
}

