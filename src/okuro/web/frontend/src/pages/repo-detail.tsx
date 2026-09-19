import { useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router";
import { registerSnapshotContext } from "@/lib/handover-context";
import {
  RefreshCw,
  Loader2,
  GitBranch,
  Network,
  Boxes,
  FileCode,
  CircleCheck,
  CircleDashed,
  Search,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { toast } from "@/components/ui/toast";
import { api } from "@/lib/api";
import { leafSlugs, pathFor } from "@/shell/routes";
import { useDetailTitle } from "@/shell/components/PageTitle";
import {
  RepoCodeGraph,
  type CodeGraphNode,
  type CodeGraphEdge,
} from "@/components/repos/repo-code-graph";

/**
 * /repos/:id — dedicated codebase-intelligence dashboard for one repo.
 *
 * Index proof (files/symbols/edges), hubs, subsystems, and a real file-level
 * code graph — the code intelligence surface, distinct from /knowledge.
 */

type Repo = {
  id: string;
  name: string;
  url: string;
  workspace: string;
  tier: string;
  status: string;
  default_branch: string | null;
  last_indexed_sha: string | null;
  error: string | null;
  project_id: string | null;
};

type Stats = {
  files: number;
  symbols: number;
  imports: number;
  calls: number;
  inherits: number;
  languages: Record<string, number>;
  layers: Record<string, number>;
  communities: number;
  cortex_docs: number;
  has_vectors: boolean;
};

type GodNode = {
  node: string;
  degree: number;
  in: number;
  out: number;
  layer: string | null;
};

type Insights = {
  god_nodes: GodNode[];
  communities: { community: string; files: number }[];
  community_count: number;
};

type GraphData = {
  nodes: CodeGraphNode[];
  edges: CodeGraphEdge[];
  truncated: boolean;
  dropped: number;
};

type FileDetail = {
  path: string;
  layer: string | null;
  community: string | null;
  symbols: string[];
  imports: string[];
  out_calls: string[];
  called_by: string[];
};

type SearchHit = { path: string; score: number; snippet: string; purpose: string | null };

const repoApi = {
  get: (id: string) => api<Repo>(`/api/repos/${encodeURIComponent(id)}`),
  stats: (id: string) => api<Stats>(`/api/repos/${encodeURIComponent(id)}/stats`),
  insights: (id: string) =>
    api<Insights>(`/api/repos/${encodeURIComponent(id)}/insights?top_n=12`),
  graph: (id: string) =>
    api<GraphData>(`/api/repos/${encodeURIComponent(id)}/graph`),
  file: (id: string, path: string) =>
    api<FileDetail>(
      `/api/repos/${encodeURIComponent(id)}/file?path=${encodeURIComponent(path)}`,
    ),
  search: (id: string, q: string) =>
    api<{ query: string; results: SearchHit[] }>(
      `/api/repos/${encodeURIComponent(id)}/search?q=${encodeURIComponent(q)}`,
    ),
  sync: (id: string) =>
    api<{ id: string }>(`/api/repos/${encodeURIComponent(id)}/sync`, {
      method: "POST",
    }),
};

function StatTile({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex flex-col gap-0.5 rounded-lg border border-border px-4 py-3">
      <span className="text-2xl font-semibold tabular-nums">{value}</span>
      <span className="text-xs case-label tracking-wider text-fg-muted">
        {label}
      </span>
    </div>
  );
}

function BreakdownBars({
  title,
  data,
}: {
  title: string;
  data: Record<string, number>;
}) {
  const entries = Object.entries(data);
  const max = Math.max(1, ...entries.map(([, v]) => v));
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-xs font-medium case-label tracking-wider text-fg-muted">
        {title}
      </h3>
      {entries.length === 0 ? (
        <p className="text-sm text-fg-muted">—</p>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {entries.slice(0, 10).map(([k, v]) => (
            <li key={k} className="flex flex-col gap-0.5">
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate text-xs">{k}</span>
                <span className="text-xs tabular-nums text-fg-muted">{v}</span>
              </div>
              <div className="h-1 rounded-full bg-muted">
                <div
                  className="h-1 rounded-full bg-primary"
                  style={{ width: `${(v / max) * 100}%` }}
                />
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FileList({ title, items, mono }: { title: string; items: string[]; mono?: boolean }) {
  return (
    <div className="flex flex-col gap-1">
      <h4 className="text-2xs font-medium case-label tracking-wider text-fg-muted">
        {title}
      </h4>
      {items.length === 0 ? (
        <p className="text-xs text-fg-muted">—</p>
      ) : (
        <ul className="flex flex-col gap-0.5">
          {items.slice(0, 40).map((it) => (
            <li key={it} className={mono ? "truncate font-mono text-xs" : "truncate text-xs"} title={it}>
              {it}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Resolved from the declared list, never a pinned index — the back arrow
 *  must keep pointing at the repo LIST if a leaf is inserted above it. */
const REPOS_LEAF = leafSlugs("know").indexOf("repos");

export function RepoDetailPage() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");

  const { data: repo } = useQuery({ queryKey: ["repo", id], queryFn: () => repoApi.get(id) });
  const { data: stats } = useQuery({ queryKey: ["repo-stats", id], queryFn: () => repoApi.stats(id) });

  // L2 snapshot context — hand over the repo record + stats, not just pixels.
  useEffect(() => {
    if (!repo) return;
    return registerSnapshotContext(() => ({
      entity: { type: "repo", id },
      data: { repo, stats },
    }));
  }, [repo, stats, id]);
  const { data: insights } = useQuery({ queryKey: ["repo-insights2", id], queryFn: () => repoApi.insights(id) });
  const { data: graph, isLoading: graphLoading } = useQuery({
    queryKey: ["repo-graph", id],
    queryFn: () => repoApi.graph(id),
  });
  const { data: fileDetail } = useQuery({
    queryKey: ["repo-file", id, selected],
    queryFn: () => repoApi.file(id, selected!),
    enabled: !!selected,
  });
  const { data: search, isFetching: searching } = useQuery({
    queryKey: ["repo-search", id, submitted],
    queryFn: () => repoApi.search(id, submitted),
    enabled: submitted.length > 0,
  });

  const syncMut = useMutation({
    mutationFn: () => repoApi.sync(id),
    onSuccess: () => {
      toast.success("Sync started");
      ["repo", "repo-stats", "repo-insights2", "repo-graph"].forEach((k) =>
        qc.invalidateQueries({ queryKey: [k, id] }),
      );
    },
    onError: (e: unknown) => toast.error(e instanceof Error ? e.message : "sync failed"),
  });

  /* ===================================================================
     THE SECOND DETAIL VIEW ON THE CHANNEL — and the end of audit C4-4.
     ===================================================================
     This page carried the document's SECOND `<h1>`: the shell already
     renders one for the leaf, and R1 rules exactly one per document. The
     old comment said "the h1 stays for now: it is the REPO's name, not the
     leaf's, so R1 does not reach it" — which was true only while there was
     no way to hand a record's name to the shell. `useDetailTitle` is that
     way, so the rank is not demoted here, it MOVES, and the back link
     beside it stops being this page's private pattern: `DetailTitle`
     requires `onBack`, which is what todo 29bd484f was waiting for.

     THE MAPPING follows `pages/task-detail.tsx`, the first consumer:
       title    the repo's name — the record's own name, as the owner's
                sketch draws it
       status   the tier
       meta     branch, short sha, and whether the semantic index is ready
       actions  Sync
       onBack   the repo LIST, resolved from the IA rather than the
                hardcoded `/know/repos` the link used */
  const detailSlot = useMemo(
    () => ({
      title: repo?.name ?? id,
      status: repo ? <span className="case-label">{repo.tier}</span> : undefined,
      meta: (
        <span className="inline-flex items-center gap-3">
          {repo?.default_branch && (
            <span className="inline-flex items-center gap-1">
              <GitBranch />
              {repo.default_branch}
            </span>
          )}
          {repo?.last_indexed_sha && (
            <span className="font-mono">{repo.last_indexed_sha.slice(0, 8)}</span>
          )}
          {stats && (
            <span className="inline-flex items-center gap-1">
              {stats.has_vectors ? (
                <>
                  <CircleCheck className="text-success" /> index ready
                </>
              ) : (
                <>
                  <CircleDashed className="text-warning" /> index pending
                </>
              )}
            </span>
          )}
        </span>
      ),
      actions: (
        <Button
          variant="outline"
          className="gap-2"
          disabled={syncMut.isPending}
          onClick={() => syncMut.mutate()}
        >
          <RefreshCw className={syncMut.isPending ? "animate-spin" : ""} />
          Sync
        </Button>
      ),
      onBack: () => navigate(pathFor("know", REPOS_LEAF < 0 ? 0 : REPOS_LEAF)),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [repo, stats, id, syncMut.isPending, navigate],
  );
  useDetailTitle(detailSlot);

  return (
    /* R2 — the shell's ring is the only ring, and the pane is the only width.
       `p-6` was 27.6px inside it and `max-w-6xl` (1152px) bound above a
       ~1180px pane. The record's name, tier, branch, sha and Sync button are
       all on the plate now; what is left below is the index proof and the
       search, which are the page rather than its header. */
    <div className="flex w-full flex-col gap-6">
      {/* Index proof */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <StatTile label="Files" value={stats?.files ?? "—"} />
        <StatTile label="Symbols" value={stats?.symbols ?? "—"} />
        <StatTile label="Imports" value={stats?.imports ?? "—"} />
        <StatTile label="Calls" value={stats?.calls ?? "—"} />
        <StatTile label="Subsystems" value={stats?.communities ?? "—"} />
        <StatTile label="Indexed docs" value={stats?.cortex_docs ?? "—"} />
      </div>

      {/* Search-in-repo */}
      <form
        className="flex items-center gap-2 rounded-lg border border-border px-3 py-2"
        onSubmit={(e) => {
          e.preventDefault();
          setSubmitted(query.trim());
        }}
      >
        <Search className="size-4 text-fg-muted" />
        <input
          className="flex-1 bg-transparent text-sm outline-none"
          placeholder="Search this codebase (semantic + keyword)…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {searching && <Loader2 className="size-4 animate-spin text-fg-muted" />}
        {submitted && (
          <button
            type="button"
            onClick={() => {
              setQuery("");
              setSubmitted("");
            }}
          >
            <X className="size-4 text-fg-muted" />
          </button>
        )}
      </form>

      {submitted && (
        <div className="flex flex-col divide-y divide-border rounded-lg border border-border">
          {(search?.results ?? []).length === 0 && !searching ? (
            <p className="px-4 py-3 text-sm text-fg-muted">No matches.</p>
          ) : (
            (search?.results ?? []).map((h) => (
              <div key={h.path} className="px-4 py-2">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="truncate font-mono text-xs">{h.path}</span>
                  <span className="shrink-0 text-xs tabular-nums text-fg-muted">
                    {h.score.toFixed(2)}
                  </span>
                </div>
                {h.snippet && (
                  <p className="mt-0.5 line-clamp-2 text-xs text-fg-muted">{h.snippet}</p>
                )}
              </div>
            ))
          )}
        </div>
      )}

      {/* Code graph — the centerpiece */}
      <section className="flex flex-col gap-2">
        <h2 className="flex items-center gap-2 text-sm font-medium">
          <Network className="size-4" /> Code graph
          {graph?.truncated && (
            <Badge className="bg-warning/15 text-warning">
              top {graph.nodes.length} of {graph.nodes.length + graph.dropped} files
            </Badge>
          )}
          <span className="text-xs font-normal text-fg-muted">· click a file to inspect</span>
        </h2>
        <div className="grid gap-3 lg:grid-cols-[1fr_18rem]">
          <div className="h-[520px] overflow-hidden rounded-lg border border-border">
            {graphLoading ? (
              <div className="flex h-full items-center justify-center text-fg-muted">
                <Loader2 className="size-5 animate-spin" />
              </div>
            ) : (
              <RepoCodeGraph
                nodes={graph?.nodes ?? []}
                edges={graph?.edges ?? []}
                onSelect={setSelected}
                selectedId={selected}
              />
            )}
          </div>

          {selected && (
            <aside className="flex h-[520px] flex-col gap-3 overflow-y-auto rounded-lg border border-border p-3">
              <div className="flex items-start justify-between gap-2">
                <span className="break-all font-mono text-xs font-medium">{selected}</span>
                <button onClick={() => setSelected(null)}>
                  <X className="size-4 text-fg-muted" />
                </button>
              </div>
              <div className="flex flex-wrap gap-1 text-2xs case-label tracking-wider text-fg-muted">
                {fileDetail?.layer && <span className="rounded bg-muted px-1.5 py-0.5">{fileDetail.layer}</span>}
                {fileDetail?.community && <span className="rounded bg-muted px-1.5 py-0.5">{fileDetail.community}</span>}
              </div>
              <FileList title={`Symbols (${fileDetail?.symbols.length ?? 0})`} items={fileDetail?.symbols ?? []} mono />
              <FileList title={`Imports (${fileDetail?.imports.length ?? 0})`} items={fileDetail?.imports ?? []} mono />
              <FileList title={`Called by (${fileDetail?.called_by.length ?? 0})`} items={fileDetail?.called_by ?? []} mono />
            </aside>
          )}
        </div>
      </section>

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-4">
        <BreakdownBars title="Languages" data={stats?.languages ?? {}} />
        <BreakdownBars title="Layers" data={stats?.layers ?? {}} />

        <section className="flex flex-col gap-2">
          <h3 className="flex items-center gap-1.5 text-xs font-medium case-label tracking-wider text-fg-muted">
            <FileCode className="size-3.5" /> Hub symbols
          </h3>
          <ul className="flex flex-col gap-1">
            {(insights?.god_nodes ?? []).slice(0, 10).map((g) => (
              <li key={g.node} className="flex items-baseline justify-between gap-2">
                <span className="truncate font-mono text-xs" title={g.node}>
                  {g.node}
                </span>
                <span className="shrink-0 text-xs tabular-nums text-fg-muted">
                  {g.degree}
                </span>
              </li>
            ))}
          </ul>
        </section>

        <section className="flex flex-col gap-2">
          <h3 className="flex items-center gap-1.5 text-xs font-medium case-label tracking-wider text-fg-muted">
            <Boxes className="size-3.5" /> Subsystems
          </h3>
          <ul className="flex flex-col gap-1">
            {(insights?.communities ?? []).slice(0, 10).map((c) => (
              <li key={c.community} className="flex items-baseline justify-between gap-2">
                <span className="truncate text-xs">{c.community}</span>
                <span className="shrink-0 text-xs tabular-nums text-fg-muted">
                  {c.files}
                </span>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}
