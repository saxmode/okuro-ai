import { useMemo, useState } from "react";
import { Segmented } from "@/components/ui/segmented";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  Check,
  Download,
  Loader2,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { SectionLabel } from "@/components/ui/section-label";
import { EmptyState } from "@/components/ui/empty-state";
import { StatusBadge } from "@/components/ui/status-badge";
import { VramReclaimModal } from "@/components/models/vram-reclaim-modal";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import {
  modelsApi,
  type ModelEntry,
  type CatalogEntry,
  type Discovery,
  type PullStatus,
} from "@/lib/models-api";
import { PageHeader } from "@/components/shell/page-header";
import { useDownloads } from "@/lib/downloads-context";
import { CommercialBadge } from "@/components/models/commercial-badge";

/**
 * /models — the AI model registry.
 *
 * Shows two kinds of models:
 *  - Subscription — mapped in ~/.okuro/config.yaml inference.providers
 *    (e.g. claude/sonnet, gemini/2.5-pro, codex/o3)
 *  - Local GGUF — scanned from the configured model store, ~/.cache/huggingface
 *
 * Tier (fast | standard | quality) comes from the bridge config mapping;
 * local GGUFs don't advertise a tier until the bridge picks them up.
 */

const TIER_ORDER: Record<string, number> = {
  fast: 0,
  standard: 1,
  quality: 2,
};

const TIER_TONE: Record<string, "success" | "info" | "accent"> = {
  fast: "success",
  standard: "info",
  quality: "accent",
};

// Short badge label per prompt_syntax — signals a model carries a
// model-conditioned prompting block (see ai_models/optimize.py).
const SYNTAX_LABEL: Record<string, string> = {
  chat_template: "chat",
  booru_tags: "booru",
  natural_language: "prose",
};

export function ModelsPage() {
  const [filterProvider, setFilterProvider] = useState<string | null>(null);
  const [filterTier, setFilterTier] = useState<string | null>(null);
  const [filterSource, setFilterSource] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [detailModel, setDetailModel] = useState<ModelEntry | null>(null);
  const [mode, setMode] = useState<"installed" | "discover">("installed");
  const [showGpu, setShowGpu] = useState(false);

  // Edition gate — okuro-air (no local GPU) has no local inference, so the
  // Discover/pull surface is hidden. Default to enabled while loading so it
  // shows immediately on advanced/pro.
  const { data: edition } = useQuery({
    queryKey: ["models", "edition"],
    queryFn: () => modelsApi.edition(),
    staleTime: 5 * 60_000,
  });
  const localInference = edition?.local_inference ?? true;
  const effectiveMode = localInference ? mode : "installed";

  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["models", "list"],
    queryFn: () => modelsApi.list(),
    staleTime: 30_000,
  });

  const allModels = data?.models ?? [];

  const filtered = useMemo(() => {
    let out = allModels;
    if (filterProvider) out = out.filter((m) => m.provider === filterProvider);
    if (filterTier) out = out.filter((m) => m.tier === filterTier);
    if (filterSource) out = out.filter((m) => m.source === filterSource);
    if (search) {
      const q = search.toLowerCase();
      out = out.filter(
        (m) =>
          m.name.toLowerCase().includes(q) ||
          m.alias.toLowerCase().includes(q) ||
          (m.parameters || "").toLowerCase().includes(q),
      );
    }
    return out;
  }, [allModels, filterProvider, filterTier, filterSource, search]);

  const grouped = useMemo(() => {
    const groups: Record<string, ModelEntry[]> = {};
    for (const m of filtered) {
      const key = m.provider || "unknown";
      (groups[key] ??= []).push(m);
    }
    // Sort each group by tier then name
    for (const key of Object.keys(groups)) {
      const list = groups[key];
      if (!list) continue;
      list.sort((a, b) => {
        const ta = a.tier ? TIER_ORDER[a.tier] ?? 99 : 99;
        const tb = b.tier ? TIER_ORDER[b.tier] ?? 99 : 99;
        if (ta !== tb) return ta - tb;
        return a.name.localeCompare(b.name);
      });
    }
    return groups;
  }, [filtered]);

  const providerOptions = useMemo(
    () => Object.keys(data?.by_provider ?? {}).sort(),
    [data],
  );
  const sourceOptions = useMemo(
    () => Object.keys(data?.by_source ?? {}).sort(),
    [data],
  );
  const tierOptions = ["fast", "standard", "quality"];

  const activeFilters =
    Number(!!filterProvider) + Number(!!filterTier) + Number(!!filterSource);

  return (
    <div className="page-shell space-y-10">
      <PageHeader
        title="Models"
        subtitle={
          data
            ? `${data.total} models across ${Object.keys(data.by_provider).length} providers`
            : isLoading
              ? "loading…"
              : "—"
        }
        right={
          <div className="flex items-center gap-2">
            {localInference && (
              <Button variant="outline" size="sm" onClick={() => setShowGpu(true)}>
                GPU
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => refetch()}
              disabled={isFetching}
              aria-label="Reload model registry"
            >
              <RefreshCw
                className={cn("h-4 w-4", isFetching && "animate-spin")}
                aria-hidden="true"
              />
            </Button>
          </div>
        }
      />

      <VramReclaimModal open={showGpu} onClose={() => setShowGpu(false)} />

      {localInference ? (
        <Segmented
          ariaLabel="Model view"
          value={mode}
          onChange={(v) => setMode(v as "installed" | "discover")}
          options={[
            { label: "Installed", value: "installed" },
            { label: "Discover", value: "discover" },
          ]}
        />
      ) : (
        <p className="rounded-md border border-border-subtle bg-surface-subtle px-3 py-2 text-xs text-fg-subtle">
          Cloud-only (okuro-{edition?.edition ?? "air"}) — no local GPU, so local
          model discovery and inference are off. Subscription models below.
        </p>
      )}

      {effectiveMode === "discover" ? (
        <DiscoverPanel />
      ) : (
        <>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          placeholder="Search models..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-60"
        />
        <FilterChips
          label="Provider"
          options={providerOptions}
          value={filterProvider}
          onChange={setFilterProvider}
        />
        <FilterChips
          label="Tier"
          options={tierOptions}
          value={filterTier}
          onChange={setFilterTier}
        />
        <FilterChips
          label="Source"
          options={sourceOptions}
          value={filterSource}
          onChange={setFilterSource}
        />
        {(activeFilters > 0 || search) && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setFilterProvider(null);
              setFilterTier(null);
              setFilterSource(null);
              setSearch("");
            }}
          >
            <X className="h-3 w-3" aria-hidden="true" />
            Clear
          </Button>
        )}
      </div>

      {isLoading ? (
        <div className="h-40 animate-pulse rounded-md border border-border-subtle bg-surface-elevated" />
      ) : filtered.length === 0 ? (
        <EmptyState
          title="No models match"
          description="Clear filters or install an AI CLI to populate subscription models."
        />
      ) : (
        <div className="space-y-6">
          {Object.entries(grouped).map(([provider, models]) => (
            <ProviderGroup
              key={provider}
              provider={provider}
              models={models}
              onSelect={setDetailModel}
            />
          ))}
        </div>
      )}
        </>
      )}

      <Dialog
        open={!!detailModel}
        onOpenChange={(open) => !open && setDetailModel(null)}
      >
        <DialogContent className="max-w-2xl">
          {detailModel && (
            <>
              <DialogHeader>
                <DialogTitle>{detailModel.name}</DialogTitle>
                <DialogDescription>
                  {detailModel.provider || "?"}{" "}
                  {detailModel.tier && <>· tier {detailModel.tier}</>} ·{" "}
                  {detailModel.source}
                </DialogDescription>
              </DialogHeader>
              <DetailBody model={detailModel} />
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function FilterChips({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: string[];
  value: string | null;
  onChange: (v: string | null) => void;
}) {
  if (options.length === 0) return null;
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs font-medium text-fg-subtle">{label}</span>
      <Segmented
        variant="ghost"
        ariaLabel={label}
        value={value ?? ""}
        onChange={(v) => onChange(v === value ? null : v)}
        options={options.map((opt) => ({
          label: opt.charAt(0).toUpperCase() + opt.slice(1).toLowerCase(),
          value: opt,
        }))}
      />
    </div>
  );
}

function ProviderGroup({
  provider,
  models,
  onSelect,
}: {
  provider: string;
  models: ModelEntry[];
  onSelect: (m: ModelEntry) => void;
}) {
  // Hide columns that are empty for every row in this group so the
  // table doesn't display rows of em-dashes (PARAMS + SIZE are only
  // populated for local models).
  const hasParams = models.some((m) => !!m.parameters || m.size_gb > 0);
  const hasSize = models.some((m) => m.size_gb > 0);
  const hasSource = new Set(models.map((m) => m.source)).size > 1;

  return (
    <section>
      <div className="mb-2 flex items-center justify-between">
        <SectionLabel>{provider}</SectionLabel>
        <span className="text-xs text-fg-subtle">
          {models.length} model{models.length === 1 ? "" : "s"}
        </span>
      </div>
      <div className="overflow-hidden rounded-md border border-border-subtle">
        <table className="w-full text-sm">
          <thead className="bg-surface-subtle text-xs font-medium text-fg-subtle">
            <tr>
              <th className="px-3 py-2 text-left">Name</th>
              <th className="px-3 py-2 text-left">Tier</th>
              {hasSource && <th className="px-3 py-2 text-left">Source</th>}
              {hasParams && <th className="px-3 py-2 text-left">Params</th>}
              {hasSize && <th className="px-3 py-2 text-left">Size</th>}
              <th className="px-3 py-2 text-left">Capabilities</th>
            </tr>
          </thead>
          <tbody>
            {models.map((m) => (
              <tr
                key={m.id + m.source}
                onClick={() => onSelect(m)}
                className="cursor-pointer border-t border-border-subtle transition-fast hover:bg-surface-subtle"
              >
                <td className="px-3 py-2 font-medium text-fg">
                  <div className="flex items-center gap-2">
                    <span>{m.name}</span>
                    {m.prompt_ready && (
                      <span
                        title={`Prompt-ready — model-conditioned (${
                          m.prompt_syntax ?? "unknown"
                        })`}
                      >
                        <StatusBadge
                          tone="success"
                          label={`⚡ ${SYNTAX_LABEL[m.prompt_syntax ?? ""] ?? "ready"}`}
                        />
                      </span>
                    )}
                  </div>
                </td>
                <td className="px-3 py-2">
                  {m.tier ? (
                    <StatusBadge
                      tone={TIER_TONE[m.tier] || "neutral"}
                      label={m.tier.toUpperCase()}
                    />
                  ) : (
                    <span className="text-xs text-fg-subtle">—</span>
                  )}
                </td>
                {hasSource && (
                  <td className="px-3 py-2 text-fg-muted">{m.source}</td>
                )}
                {hasParams && (
                  <td className="px-3 py-2 text-fg-muted">
                    {m.parameters ||
                      (m.size_gb > 0 ? `${m.size_gb.toFixed(1)}GB` : "—")}
                  </td>
                )}
                {hasSize && (
                  <td className="px-3 py-2 text-fg-muted">
                    {m.size_gb > 0 ? `${m.size_gb.toFixed(1)}GB` : "—"}
                  </td>
                )}
                <td className="px-3 py-2">
                  <div className="flex flex-wrap gap-1">
                    {m.capabilities.slice(0, 3).map((c) => (
                      <Badge
                        key={c}
                        variant="outline"
                        className="text-3xs font-normal"
                      >
                        {c}
                      </Badge>
                    ))}
                    {m.capabilities.length > 3 && (
                      <span className="text-2xs text-tertiary">
                        +{m.capabilities.length - 3}
                      </span>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function DetailBody({ model }: { model: ModelEntry }) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 text-sm">
        <DetailField label="Name" value={model.name} />
        <DetailField label="Alias" value={model.alias} />
        <DetailField label="Provider" value={model.provider || "—"} />
        <DetailField label="Tier" value={model.tier || "—"} />
        <DetailField label="Source" value={model.source} />
        <DetailField
          label="Parameters"
          value={model.parameters || "—"}
        />
        <DetailField
          label="Quantization"
          value={model.quantization || "—"}
        />
        <DetailField
          label="Size"
          value={model.size_gb > 0 ? `${model.size_gb.toFixed(2)} GB` : "—"}
        />
      </div>
      {model.path && (
        <div>
          <div className="mb-1 text-2xs uppercase tracking-wider text-tertiary">
            Path
          </div>
          <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-surface-subtle px-2 py-1 text-xs font-mono text-fg-muted">
            {model.path}
          </pre>
        </div>
      )}
      {model.capabilities.length > 0 && (
        <div>
          <div className="mb-1 text-2xs uppercase tracking-wider text-tertiary">
            Capabilities
          </div>
          <div className="flex flex-wrap gap-1">
            {model.capabilities.map((c) => (
              <Badge key={c} variant="outline" className="text-xs">
                {c}
              </Badge>
            ))}
          </div>
        </div>
      )}
      <div>
        <div className="mb-1 text-2xs uppercase tracking-wider text-tertiary">
          Raw
        </div>
        <pre className="overflow-auto rounded bg-surface-subtle px-2 py-2 text-xs font-mono text-fg-muted">
          {JSON.stringify(model, null, 2)}
        </pre>
      </div>
    </div>
  );
}

function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-2xs uppercase tracking-wider text-tertiary">
        {label}
      </div>
      <div className="text-fg">{value}</div>
    </div>
  );
}

/**
 * Discover mode — search the OSS catalog (HuggingFace + Civitai), ranked to
 * run on this box, and pull a model into the bundle store. Pulls run as a
 * background job on the server; we poll status and refresh the installed
 * list when one lands.
 */
function DiscoverPanel() {
  const qc = useQueryClient();
  const { startPull, jobFor } = useDownloads();
  const [q, setQ] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [modality, setModality] = useState("text");

  const searching = submitted.length > 0;

  // Default content: the durable discoveries list the weekly scan files.
  // No network round-trip to HF — this is what "the list shows discoveries
  // by default" means.
  const discQ = useQuery({
    queryKey: ["models", "discoveries"],
    queryFn: () => modelsApi.discoveries({ limit: 100 }),
    enabled: !searching,
    staleTime: 60_000,
  });

  // Live OSS search overrides the default list when the user submits a query.
  const searchQ = useQuery({
    queryKey: ["models", "search", submitted, modality],
    queryFn: () => modelsApi.search({ query: submitted, modality, limit: 20 }),
    enabled: searching,
    staleTime: 60_000,
  });

  // Pull progress + persistence is owned by the app-wide downloads store
  // (rehydrates from the server, survives refresh, shown in the tray).

  async function dismiss(id: string) {
    try {
      await modelsApi.setDiscoveryStatus(id, "dismissed");
      qc.invalidateQueries({ queryKey: ["models", "discoveries"] });
    } catch {
      /* leave the row — a failed dismiss is non-destructive */
    }
  }

  const results = searchQ.data ?? [];
  const discoveries = discQ.data?.discoveries ?? [];

  return (
    <div className="space-y-4">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setSubmitted(q.trim());
        }}
        className="flex flex-wrap items-center gap-2"
      >
        <Input
          placeholder="Search HuggingFace + Civitai…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          className="w-72"
        />
        <FilterChips
          label="Modality"
          options={["text", "image", "audio"]}
          value={modality}
          onChange={(v) => setModality(v ?? "text")}
        />
        <Button type="submit" size="sm" disabled={!q.trim() || searchQ.isFetching}>
          <Search className="h-4 w-4" aria-hidden="true" />
          Search
        </Button>
        {searching && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              setQ("");
              setSubmitted("");
            }}
          >
            <X className="h-4 w-4" aria-hidden="true" />
            Discoveries
          </Button>
        )}
      </form>

      {searching ? (
        searchQ.isFetching ? (
          <div className="h-40 animate-pulse rounded-md border border-border-subtle bg-surface-elevated" />
        ) : searchQ.isError ? (
          <EmptyState
            title="Search failed"
            description="Try again, or check that HuggingFace/Civitai credentials are set."
          />
        ) : results.length === 0 ? (
          <EmptyState
            title="No runnable models"
            description={`Nothing for "${submitted}" fits this box's VRAM.`}
          />
        ) : (
          <div className="overflow-hidden rounded-md border border-border-subtle">
            <table className="w-full text-sm">
              <thead className="bg-surface-subtle text-xs font-medium text-fg-subtle">
                <tr>
                  <th className="px-3 py-2 text-left">Name</th>
                  <th className="px-3 py-2 text-left">Source</th>
                  <th className="px-3 py-2 text-left">Task</th>
                  <th className="px-3 py-2 text-left">min VRAM</th>
                  <th className="px-3 py-2 text-left">Popularity</th>
                  <th className="px-3 py-2 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {results.map((e) => (
                  <CatalogRow
                    key={e.catalog_id}
                    entry={e}
                    status={jobFor(e.catalog_id)}
                    onPull={() => startPull(e.catalog_id, e.display_name)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : discQ.isFetching ? (
        <div className="h-40 animate-pulse rounded-md border border-border-subtle bg-surface-elevated" />
      ) : discoveries.length === 0 ? (
        <EmptyState
          title="No discoveries yet"
          description="The weekly scan (Mondays 04:00) surfaces new, reputable models that fit this box. New finds land here automatically."
        />
      ) : (
        <>
          <SectionLabel>
            {discoveries.length} discovered · fit this box · newest first
          </SectionLabel>
          <div className="overflow-hidden rounded-md border border-border-subtle">
            <table className="w-full text-sm">
              <thead className="bg-surface-subtle text-xs font-medium text-fg-subtle">
                <tr>
                  <th className="px-3 py-2 text-left">Name</th>
                  <th className="px-3 py-2 text-left">Why</th>
                  <th className="px-3 py-2 text-left">Fit</th>
                  <th className="px-3 py-2 text-left">min VRAM</th>
                  <th className="px-3 py-2 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {discoveries.map((d) => (
                  <DiscoveryRow
                    key={d.catalog_id}
                    disc={d}
                    status={jobFor(d.catalog_id)}
                    onPull={() => startPull(d.catalog_id, d.display_name)}
                    onDismiss={() => dismiss(d.catalog_id)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

function DiscoveryRow({
  disc,
  status,
  onPull,
  onDismiss,
}: {
  disc: Discovery;
  status?: PullStatus;
  onPull: () => void;
  onDismiss: () => void;
}) {
  const installed = disc.status === "installed";
  const why = disc.rationale || disc.use_case || "—";
  return (
    <tr className="border-t border-border-subtle align-top">
      <td className="px-3 py-2 font-medium text-fg">
        <div className="flex items-center gap-2">
          {disc.source_url ? (
            <a
              href={disc.source_url}
              target="_blank"
              rel="noreferrer"
              className="hover:underline"
            >
              {disc.display_name}
            </a>
          ) : (
            disc.display_name
          )}
          {disc.status === "new" && (
            <Badge variant="outline" className="text-3xs font-normal">
              new
            </Badge>
          )}
          <CommercialBadge status={disc.commercial_status} licenseId={disc.license_id} />
        </div>
      </td>
      <td className="max-w-md px-3 py-2 text-fg-muted">
        <span className="line-clamp-2">{why}</span>
        {disc.caveats && (
          <span className="mt-0.5 block text-3xs text-fg-subtle">
            {disc.caveats}
          </span>
        )}
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-fg-muted">
        {disc.fit_gpu || "—"}
      </td>
      <td className="px-3 py-2 text-fg-muted">
        {disc.min_vram_gb ? `${disc.min_vram_gb.toFixed(1)}GB` : "—"}
      </td>
      <td className="px-3 py-2 text-right">
        <div className="inline-flex items-center gap-2">
          <PullButton
            state={status?.state}
            error={status?.error}
            acquired={installed}
            onPull={onPull}
          />
          {!installed && (
            <Button
              size="sm"
              variant="ghost"
              onClick={onDismiss}
              aria-label={`Dismiss ${disc.display_name}`}
              title="Dismiss — hide from discoveries"
            >
              <X className="h-3 w-3" aria-hidden="true" />
            </Button>
          )}
        </div>
      </td>
    </tr>
  );
}

function CatalogRow({
  entry,
  status,
  onPull,
}: {
  entry: CatalogEntry;
  status?: PullStatus;
  onPull: () => void;
}) {
  const dl = entry.quality_signals?.downloads ?? 0;
  const likes = entry.quality_signals?.likes ?? 0;
  return (
    <tr className="border-t border-border-subtle">
      <td className="px-3 py-2 font-medium text-fg">
        {entry.display_name || entry.catalog_id}
        {entry.gated && (
          <Badge variant="outline" className="ml-2 text-3xs font-normal">
            gated
          </Badge>
        )}
      </td>
      <td className="px-3 py-2 text-fg-muted">{entry.source}</td>
      <td className="px-3 py-2 text-fg-muted">{entry.task}</td>
      <td className="px-3 py-2 text-fg-muted">
        {entry.min_vram_gb ? `${entry.min_vram_gb.toFixed(1)}GB` : "—"}
      </td>
      <td className="px-3 py-2 text-fg-muted">
        {dl.toLocaleString()} ↓ · {likes} ♥
      </td>
      <td className="px-3 py-2 text-right">
        <PullButton
          state={status?.state}
          error={status?.error}
          acquired={entry.acquired}
          onPull={onPull}
        />
      </td>
    </tr>
  );
}

function PullButton({
  state,
  error,
  acquired,
  onPull,
}: {
  state?: PullStatus["state"];
  error?: string | null;
  acquired?: boolean;
  onPull: () => void;
}) {
  if (acquired || state === "done") {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-success">
        <Check className="h-3 w-3" aria-hidden="true" />
        Installed
      </span>
    );
  }
  if (state === "downloading") {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-fg-subtle">
        <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
        Pulling…
      </span>
    );
  }
  if (state === "error") {
    return (
      <span
        title={error ?? ""}
        className="inline-flex items-center gap-1 text-xs text-error"
      >
        <AlertCircle className="h-3 w-3" aria-hidden="true" />
        Failed
      </span>
    );
  }
  return (
    <Button size="sm" variant="outline" onClick={onPull}>
      <Download className="h-3 w-3" aria-hidden="true" />
      Pull
    </Button>
  );
}
