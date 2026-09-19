import { useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  RefreshCw,
  Loader2,
  Plus,
  Trash2,
  BookOpen,
  FolderOpen,
  Globe,
  CircleCheck,
  Search,
  FileText,
  Layers,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  ChangeSummary,
  SourceStatusBadge,
} from "@/components/sources/source-status";
import { EmptyState } from "@/components/ui/empty-state";
import { toast } from "@/components/ui/toast";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatAge } from "@/lib/format";
import { useSectionTitle } from "@/shell/components/PageTitle";

/**
 * /corpora — make non-git document sources searchable.
 *
 * Paste a Confluence link or a folder path → okuro materializes it as markdown
 * under ~/.okuro/corpora/<workspace>/<name>, registers it as a project (→ cortex
 * root), and indexes it. Agents then query it with the ordinary cortex_* tools:
 * cortex_search(query, project="<workspace>__<name>"). There is no separate
 * wiki-search tool by design.
 *
 * Backend: /api/corpora/* (src/okuro/orchestrator/api/corpora.py).
 */

// ── Types ────────────────────────────────────────────────────────────

type SourceType = "confluence" | "local_folder";

type CorpusChange = {
  added: number;
  updated: number;
  unchanged: number;
  removed: number;
  failed: number;
  errors?: string[];
  cortex?: { indexed: boolean; files_seen?: number; files_indexed?: number };
};

type Corpus = {
  id: string;
  source_type: SourceType;
  source_ref: string;
  workspace: string;
  name: string;
  path: string;
  materialized: boolean;
  config: Record<string, unknown>;
  status: "pending" | "fetching" | "indexing" | "ready" | "error";
  project_id: string | null;
  item_count: number;
  error: string | null;
  last_change: CorpusChange | null;
  last_synced_at: string | null;
};

type Detected = {
  source_type: SourceType;
  config: Record<string, unknown>;
  suggested_name: string;
  label: string;
};

type AddPayload = {
  url: string;
  workspace: string;
  name?: string;
  token_key?: string;
  username?: string;
};

type SearchHit = {
  path: string;
  score: number;
  snippet: string;
  purpose: string | null;
};

// ── API ──────────────────────────────────────────────────────────────

const corporaApi = {
  list: () => api<Corpus[]>("/api/corpora"),
  detect: (url: string) =>
    api<Detected>("/api/corpora/detect", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
  add: (p: AddPayload) =>
    api<{ id: string; status: string; message: string }>("/api/corpora", {
      method: "POST",
      body: JSON.stringify(p),
    }),
  sync: ({ id, full = false }: { id: string; full?: boolean }) =>
    api<{ id: string; status: string }>(
      `/api/corpora/${encodeURIComponent(id)}/sync${full ? "?full=true" : ""}`,
      { method: "POST" },
    ),
  syncAll: (full = false) =>
    api<{ accepted: number; ids: string[]; message: string }>(
      `/api/corpora/sync-all${full ? "?full=true" : ""}`,
      { method: "POST" },
    ),
  remove: (id: string, deleteFiles: boolean) =>
    api<Record<string, unknown>>(
      `/api/corpora/${encodeURIComponent(id)}?delete_files=${deleteFiles}`,
      { method: "DELETE" },
    ),
  search: (id: string, q: string) =>
    api<{ query: string; project: string; results: SearchHit[] }>(
      `/api/corpora/${encodeURIComponent(id)}/search?q=${encodeURIComponent(q)}&n=12`,
    ),
};

const BUSY = new Set(["pending", "fetching", "indexing"]);

const SOURCE_ICON: Record<SourceType, typeof Globe> = {
  confluence: Globe,
  local_folder: FolderOpen,
};

// ── Change summary + status badge: SHARED WITH REPOS ─────────────────
// The badge was byte-identical to `repos.tsx`'s. Q10 ruled option A (share the
// primitives, keep two leaves), so both live in
// `components/sources/source-status.tsx` and this file maps its own data in.

/** What the last sync moved. Silence on "nothing changed" would read as a
 *  failed sync, so an all-unchanged run says so explicitly — the rule now
 *  lives in the shared component, where both leaves inherit it. */
function CorpusChangeSummary({ change }: { change: CorpusChange | null }) {
  if (!change) return null;
  const parts: React.ReactNode[] = [];
  if (change.added > 0)
    parts.push(
      <span key="a" className="text-success">
        +{change.added}
      </span>,
    );
  if (change.updated > 0)
    parts.push(
      <span key="u" className="text-foreground">
        ~{change.updated}
      </span>,
    );
  if (change.removed > 0)
    parts.push(
      <span key="r" className="text-error">
        −{change.removed}
      </span>,
    );
  if (change.failed > 0)
    parts.push(
      <span key="x" className="text-warning" title={change.errors?.join("\n")}>
        {change.failed} failed
      </span>,
    );
  return (
    <ChangeSummary
      parts={parts}
      idleLabel={`up to date · ${change.unchanged} unchanged`}
    />
  );
}

// ── Page ─────────────────────────────────────────────────────────────

export function CorporaPage() {
  const qc = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);
  const [removing, setRemoving] = useState<Corpus | null>(null);
  const [searching, setSearching] = useState<Corpus | null>(null);

  const { data: corpora = [], isLoading } = useQuery({
    queryKey: ["corpora"],
    queryFn: corporaApi.list,
    // Poll while any corpus is mid fetch/index so status flips to ready live.
    refetchInterval: (q) => {
      const rows = (q.state.data as Corpus[] | undefined) ?? [];
      return rows.some((c) => BUSY.has(c.status)) ? 2000 : false;
    },
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["corpora"] });

  const addMut = useMutation({
    mutationFn: corporaApi.add,
    onSuccess: () => {
      toast.success("Fetch started — indexing in the background");
      setAddOpen(false);
      invalidate();
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "add failed"),
  });

  const syncMut = useMutation({
    mutationFn: corporaApi.sync,
    onSuccess: (_r, vars) => {
      toast.success(vars.full ? "Full re-fetch started" : "Sync started");
      invalidate();
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "sync failed"),
  });

  const syncAllMut = useMutation({
    mutationFn: corporaApi.syncAll,
    onSuccess: (r, full) => {
      const verb = full ? "Re-fetching" : "Syncing";
      toast.success(
        r.accepted > 0
          ? `${verb} ${r.accepted} ${r.accepted === 1 ? "corpus" : "corpora"}…`
          : "Nothing to sync — all corpora busy",
      );
      invalidate();
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "sync-all failed"),
  });

  const removeMut = useMutation({
    mutationFn: ({ id, deleteFiles }: { id: string; deleteFiles: boolean }) =>
      corporaApi.remove(id, deleteFiles),
    onSuccess: () => {
      toast.success("Corpus removed");
      setRemoving(null);
      invalidate();
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "remove failed"),
  });

  const anyBusy = corpora.some((c) => BUSY.has(c.status));

  const grouped = useMemo(() => {
    const m = new Map<string, Corpus[]>();
    for (const c of corpora) {
      const arr = m.get(c.workspace) ?? [];
      arr.push(c);
      m.set(c.workspace, arr);
    }
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [corpora]);

  /* THE TWO CHROME ACTIONS GO TO THE PLATE — the sketch's ADD slot and the
     leaf's one bulk action. */
  const header = useMemo(
    () => ({
      actions: (
        <>
          <Button
            variant="outline"
            className="gap-2"
            disabled={corpora.length === 0 || anyBusy || syncAllMut.isPending}
            onClick={() => syncAllMut.mutate(false)}
            title="Sync every corpus — only changed items are re-fetched"
          >
            <RefreshCw className={cn((anyBusy || syncAllMut.isPending) && "animate-spin")} />
            Sync all
          </Button>
          <Button onClick={() => setAddOpen(true)} className="gap-2">
            <Plus /> Add corpus
          </Button>
        </>
      ),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [corpora.length, anyBusy, syncAllMut.isPending],
  );
  useSectionTitle(header);

  return (
    /* R1 + R2 — the same two deletions as REPOS, its twin, and the same
       measurements: `p-6` was 27.6px inside the shell's ring and `max-w-5xl`
       (1024px) bound above a ~1050px pane. The two controls (Sync all, Add
       corpus) are on the PLATE as of A-1 step 3b — they were relocated into
       the first content block first, which was still under the plate. */
    <div className="flex w-full flex-col gap-6">
      {/* SYNC-ALL AND ADD ARE ON THE PLATE NOW — see the memo above. The
          sentence stays as the block's lead. */}
      <p className="type-small text-fg-muted">
        Make wikis, docs, and folders searchable by agents.
      </p>

      {isLoading ? (
        <div className="flex justify-center py-16 text-fg-muted">
          <Loader2 className="size-5 animate-spin" />
        </div>
      ) : corpora.length === 0 ? (
        <EmptyState
          icon={<BookOpen className="size-6" />}
          title="No corpora yet"
          description="Paste a Confluence link or a folder path to index it for agent search."
          action={
            <Button onClick={() => setAddOpen(true)} className="gap-2">
              <Plus className="size-4" /> Add your first corpus
            </Button>
          }
        />
      ) : (
        <div className="flex flex-col gap-8">
          {grouped.map(([ws, rows]) => (
            <section key={ws} className="flex flex-col gap-3">
              <h2 className="text-xs font-medium case-label tracking-wider text-fg-muted">
                {ws}
              </h2>
              <div className="flex flex-col divide-y divide-border rounded-lg border border-border">
                {rows.map((c) => {
                  const Icon = SOURCE_ICON[c.source_type] ?? Globe;
                  return (
                    <div
                      key={c.id}
                      className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <Icon className="size-4 shrink-0 text-fg-muted" />
                          <span className="truncate font-medium">{c.name}</span>
                          <SourceStatusBadge status={c.status} />
                          {!c.materialized && (
                            <span
                              className="case-label text-xs text-fg-muted"
                              title="Indexed in place — okuro did not copy your files"
                            >
                              in place
                            </span>
                          )}
                        </div>
                        <div className="mt-0.5 flex flex-wrap items-center gap-3 text-xs text-fg-muted">
                          <span
                            className="inline-flex items-center gap-1"
                            title={c.source_ref}
                          >
                            <FileText className="size-3" />
                            {c.item_count} {c.item_count === 1 ? "item" : "items"}
                          </span>
                          {c.last_synced_at && (
                            <span
                              className="inline-flex items-center gap-1"
                              title={`Last synced ${c.last_synced_at} UTC`}
                            >
                              <RefreshCw className="size-3" />
                              {formatAge(c.last_synced_at)}
                            </span>
                          )}
                          <CorpusChangeSummary change={c.last_change} />
                          {c.status === "ready" && c.project_id && (
                            <span
                              className="font-mono text-fg-muted"
                              title="Query it with cortex_search(query, project=…)"
                            >
                              {c.project_id}
                            </span>
                          )}
                          {c.status === "error" && c.error && (
                            <span className="truncate text-error" title={c.error}>
                              {c.error}
                            </span>
                          )}
                        </div>
                      </div>

                      <div className="flex items-center gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={c.status !== "ready"}
                          className={cn(c.status !== "ready" && "opacity-40")}
                          onClick={() => setSearching(c)}
                          title="Search this corpus"
                        >
                          <Search className="size-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={BUSY.has(c.status) || syncMut.isPending}
                          onClick={() => syncMut.mutate({ id: c.id, full: false })}
                          title="Sync — re-fetch only what changed at the source"
                        >
                          <RefreshCw
                            className={cn(
                              "size-4",
                              syncMut.isPending &&
                                syncMut.variables?.id === c.id &&
                                !syncMut.variables?.full &&
                                "animate-spin",
                            )}
                          />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={BUSY.has(c.status) || syncMut.isPending}
                          onClick={() => syncMut.mutate({ id: c.id, full: true })}
                          title="Full re-fetch — re-download every item and force a re-index"
                        >
                          <Layers
                            className={cn(
                              "size-4",
                              syncMut.isPending &&
                                syncMut.variables?.id === c.id &&
                                syncMut.variables?.full &&
                                "animate-spin",
                            )}
                          />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setRemoving(c)}
                          title="Remove"
                        >
                          <Trash2 className="size-4" />
                        </Button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </section>
          ))}
        </div>
      )}

      <AddCorpusDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        submitting={addMut.isPending}
        onSubmit={(p) => addMut.mutate(p)}
      />

      <RemoveCorpusDialog
        corpus={removing}
        onOpenChange={(o) => !o && setRemoving(null)}
        submitting={removeMut.isPending}
        onConfirm={(deleteFiles) =>
          removing && removeMut.mutate({ id: removing.id, deleteFiles })
        }
      />

      <SearchDialog
        corpus={searching}
        onOpenChange={(o) => !o && setSearching(null)}
      />
    </div>
  );
}

// ── Add dialog ───────────────────────────────────────────────────────

/**
 * One field carries the whole interaction: paste a link or a path.
 *
 * The url is resolved server-side (pure string parsing, no network) and the
 * result is echoed back before submit, so the user sees "Confluence space PS on
 * …" rather than discovering a wrong guess a minute later in a background
 * failure. Credentials only appear once a source that can need them is
 * detected — a folder never asks for a token.
 */
function AddCorpusDialog({
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
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [tokenKey, setTokenKey] = useState("");
  const [username, setUsername] = useState("");
  const [detected, setDetected] = useState<Detected | null>(null);
  const [detectError, setDetectError] = useState<string | null>(null);
  const [detecting, setDetecting] = useState(false);

  // Reset on close so a second open never inherits the previous source.
  useEffect(() => {
    if (!open) {
      setUrl("");
      setName("");
      setNameTouched(false);
      setTokenKey("");
      setUsername("");
      setDetected(null);
      setDetectError(null);
    }
  }, [open]);

  // Debounced detect. 400ms is long enough that typing a url does not fire a
  // request per character, short enough that the echo feels immediate.
  useEffect(() => {
    const u = url.trim();
    if (!u) {
      setDetected(null);
      setDetectError(null);
      return;
    }
    let cancelled = false;
    setDetecting(true);
    const t = setTimeout(() => {
      corporaApi
        .detect(u)
        .then((d) => {
          if (cancelled) return;
          setDetected(d);
          setDetectError(null);
          if (!nameTouched) setName(d.suggested_name);
        })
        .catch((e: unknown) => {
          if (cancelled) return;
          setDetected(null);
          setDetectError(e instanceof Error ? e.message : "not recognised");
        })
        .finally(() => !cancelled && setDetecting(false));
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(t);
      setDetecting(false);
    };
  }, [url, nameTouched]);

  const submit = () => {
    if (!url.trim() || !detected) return;
    onSubmit({
      url: url.trim(),
      workspace: workspace.trim() || "default",
      name: name.trim() || undefined,
      token_key: tokenKey.trim() || undefined,
      username: username.trim() || undefined,
    });
  };

  const field =
    "w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring";

  const needsCredentials = detected?.source_type === "confluence";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add corpus</DialogTitle>
          <DialogDescription>
            Paste a wiki link or a folder path. okuro indexes it and agents query
            it with the normal cortex tools.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4 py-2">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-fg-muted">Link or folder path</span>
            <input
              className={field}
              placeholder="https://site.atlassian.net/wiki/spaces/KEY  ·  ~/docs"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              autoFocus
            />
            <span className="min-h-4 text-xs">
              {detecting ? (
                <span className="text-fg-muted">resolving…</span>
              ) : detected ? (
                <span className="inline-flex items-center gap-1 text-success">
                  <CircleCheck className="size-3" /> {detected.label}
                </span>
              ) : detectError && url.trim() ? (
                <span className="text-error">{detectError}</span>
              ) : null}
            </span>
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-fg-muted">Name</span>
              <input
                className={field}
                placeholder="derived from the source"
                value={name}
                onChange={(e) => {
                  setName(e.target.value);
                  setNameTouched(true);
                }}
              />
            </label>
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-fg-muted">Workspace</span>
              <input
                className={field}
                value={workspace}
                onChange={(e) => setWorkspace(e.target.value)}
              />
            </label>
          </div>

          {needsCredentials && (
            <div className="grid grid-cols-2 gap-3">
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-fg-muted">
                  Keyring token name{" "}
                  <span className="text-fg-muted">(private spaces only)</span>
                </span>
                <input
                  className={field}
                  placeholder="leave empty if public"
                  value={tokenKey}
                  onChange={(e) => setTokenKey(e.target.value)}
                />
              </label>
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-fg-muted">
                  Account email{" "}
                  <span className="text-fg-muted">(with token)</span>
                </span>
                <input
                  className={field}
                  placeholder="you@example.com"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                />
              </label>
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={submit}
            disabled={submitting || !detected}
            className="gap-2"
          >
            {submitting && <Loader2 className="size-4 animate-spin" />}
            Fetch &amp; index
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ── Search dialog ────────────────────────────────────────────────────

/** Proves a corpus is queryable, against the same vectors cortex_search uses. */
function SearchDialog({
  corpus,
  onOpenChange,
}: {
  corpus: Corpus | null;
  onOpenChange: (o: boolean) => void;
}) {
  const [q, setQ] = useState("");
  const [submitted, setSubmitted] = useState("");

  useEffect(() => {
    if (!corpus) {
      setQ("");
      setSubmitted("");
    }
  }, [corpus]);

  const { data, isFetching } = useQuery({
    queryKey: ["corpus-search", corpus?.id, submitted],
    queryFn: () => corporaApi.search(corpus!.id, submitted),
    enabled: !!corpus && submitted.length > 0,
  });

  const field =
    "w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring";

  return (
    <Dialog open={!!corpus} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Search className="size-4" /> Search {corpus?.name}
          </DialogTitle>
          <DialogDescription>
            The same index agents reach via cortex_search(query, project=
            <span className="font-mono">{corpus?.project_id}</span>).
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            setSubmitted(q.trim());
          }}
        >
          <input
            className={field}
            placeholder="What do you want to find?"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            autoFocus
          />
        </form>

        <div className="max-h-[55vh] overflow-y-auto">
          {isFetching ? (
            <div className="flex justify-center py-12 text-fg-muted">
              <Loader2 className="size-5 animate-spin" />
            </div>
          ) : !submitted ? (
            <p className="py-8 text-center text-sm text-fg-muted">
              Type a query and press Enter.
            </p>
          ) : !data || data.results.length === 0 ? (
            <p className="py-8 text-center text-sm text-fg-muted">
              No matches.
            </p>
          ) : (
            <ul className="flex flex-col gap-3 py-2">
              {data.results.map((r, i) => (
                <li key={`${r.path}-${i}`} className="flex flex-col gap-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="truncate text-xs font-medium" title={r.path}>
                      {r.path}
                    </span>
                    <span className="shrink-0 text-xs tabular-nums text-fg-muted">
                      {r.score.toFixed(2)}
                    </span>
                  </div>
                  <p className="line-clamp-3 text-xs text-fg-muted">
                    {r.snippet}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ── Remove dialog ────────────────────────────────────────────────────

function RemoveCorpusDialog({
  corpus,
  onOpenChange,
  submitting,
  onConfirm,
}: {
  corpus: Corpus | null;
  onOpenChange: (o: boolean) => void;
  submitting: boolean;
  onConfirm: (deleteFiles: boolean) => void;
}) {
  const [deleteFiles, setDeleteFiles] = useState(false);

  useEffect(() => {
    if (!corpus) setDeleteFiles(false);
  }, [corpus]);

  return (
    <Dialog open={!!corpus} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Remove {corpus?.name}?</DialogTitle>
          <DialogDescription>
            Unregisters the corpus, deactivates its project, and retracts its
            entries from the search index.
          </DialogDescription>
        </DialogHeader>

        {corpus?.materialized ? (
          <label className="flex items-center gap-2 py-2 text-sm">
            <input
              type="checkbox"
              checked={deleteFiles}
              onChange={(e) => setDeleteFiles(e.target.checked)}
            />
            Also delete okuro&apos;s copy from disk
          </label>
        ) : (
          <p className="py-2 text-sm text-fg-muted">
            Your folder at{" "}
            <span className="font-mono text-xs">{corpus?.path}</span> is indexed
            in place and will not be touched.
          </p>
        )}

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
