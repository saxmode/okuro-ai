import { useEffect, useMemo, useState } from "react";
import { Segmented } from "@/components/ui/segmented";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Search, X } from "lucide-react";
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
  modelRowKey,
  type ModelEntry,
  type CatalogEntry,
  type Discovery,
  type PullStatus,
} from "@/lib/models-api";
import { useDownloads } from "@/lib/downloads-context";
import { useIsPaneActive } from "@/lib/pane-active";
import { CommercialBadge } from "@/components/models/commercial-badge";
import { sectionSlugs } from "@/shell/views/sections";
import type { LeafViewProps } from "@/shell/views/registry";
import { InventoryTab } from "@/components/models/inventory-tab";
import { ConsumersTab } from "@/components/models/consumers-tab";
import { SwapPlansTab } from "@/components/models/swap-plans-tab";
import { RelationBadge, PLACEMENT_TONE } from "@/components/models/shared";
import { PullAction, PullButton } from "@/components/models/pull-action";
import { useSectionTitle } from "@/shell/components/PageTitle";

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

/**
 * The FIVE sub-views, in the section list's own order.
 *
 * Main's model-manager work turned one Installed/Discover toggle into five
 * tabs, each answering one of the questions the page exists for:
 *
 *   Q1 what models exist and where          -> Inventory
 *   Q2 what uses them, and what would break -> Consumers
 *   Q3/Q5 what is new that runs here        -> Interesting
 *   Q4 is it an update, and how do I swap   -> Swap plans
 *   plus the pre-existing registry view     -> Installed
 *
 * The control stays `ui/segmented`, a RADIO GROUP, not Radix `Tabs`. That is a
 * decision, not a preference: a controlled Radix `Tabs` defaults to
 * `activationMode="automatic"` and fires `onValueChange` on focus AND on
 * click, so one mouse click pushed two history entries and the first Back
 * press appeared to do nothing. Measured on this app 2026-09-15. Segmented is
 * one tab stop with a roving tabindex and pushes nothing — which is also what
 * makes it safe to bind to `?view=`, because the shell owns the history entry.
 */
type ModelsMode = "installed" | "inventory" | "consumers" | "interesting" | "swaps";

const MODES = [
  "installed",
  "inventory",
  "consumers",
  "interesting",
  "swaps",
] as const satisfies readonly ModelsMode[];

/**
 * EVERY SUB-VIEW IS A SECTION, so the choice lives in `?view=` and not in
 * `useState` — p3's Q-L1, recommendation A, now covering all five tabs rather
 * than the two the leaf pass found.
 *
 * BOTH DIRECTIONS READ THE DECLARED LIST, never a pinned index. That is the
 * rule `mounts/work-tasks.tsx:46` states, and with five tabs it earns its keep
 * twice over: inserting a section above SWAPS must not silently start
 * rendering a different panel. `shell/tests/sections.test.ts` holds the other
 * half — that `LEAF_SECTIONS["system/models"]` and `MODES` are the same five
 * words in the same order, so `indexOf` can never miss.
 *
 * `?view=discover` STILL RESOLVES, through `SECTION_ALIASES`. Main renamed the
 * panel Discover -> Interesting; the alias table is the mechanism that already
 * exists for exactly that (HEALTH's `services` -> `checks`), and without it a
 * stale address would land on INSTALLED and look like a correct page.
 *
 * `?tab=` KEEPS WORKING AND THIS FILE DOES NOTHING TO EARN THAT.
 * `routes.ts` reads `params.get("view") ?? params.get("tab")`, so the alias is
 * class-scope — which matters because it is the grammar 31 other sub-views are
 * already URL-bound on.
 */
const sectionIndexOf = (mode: ModelsMode): number => {
  const i = sectionSlugs("system", "models").indexOf(mode);
  return i < 0 ? 0 : i;
};
const MODE_OF_SECTION = (index: number): ModelsMode => {
  // `[index]` is `string | undefined` under `noUncheckedIndexedAccess`, and an
  // out-of-range section is a real case: `?view=` resolves to 0 for anything it
  // does not know, but a pane on its way out can still hold a stale index.
  const slug: string = sectionSlugs("system", "models")[index] ?? "";
  return (MODES as readonly string[]).includes(slug)
    ? (slug as ModelsMode)
    : "installed";
};

export function ModelsPage({ view: section, onSelectView }: Partial<LeafViewProps> = {}) {
  const [filterProvider, setFilterProvider] = useState<string | null>(null);
  const [filterTier, setFilterTier] = useState<string | null>(null);
  const [filterSource, setFilterSource] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [detailModel, setDetailModel] = useState<ModelEntry | null>(null);
  /**
   * `onSelectView` IS ABSENT OUTSIDE THE SHELL and for a pane on its way out
   * (`TopicBar.tsx:429`), so the section keeps a local fallback. Without it
   * `?embed=1` and every unit test would render a toggle that does nothing —
   * the same reason `pane-active`'s default is `true`.
   */
  const [localSection, setLocalSection] = useState(0);
  const mode = MODE_OF_SECTION(section ?? localSection);
  const setMode = (next: ModelsMode) => {
    const index = sectionIndexOf(next);
    if (onSelectView) onSelectView(index);
    else setLocalSection(index);
  };
  const [showGpu, setShowGpu] = useState(false);

  /**
   * A MODAL THIS LEAF OPENED MUST NOT SURVIVE THE LEAF LEAVING THE SCREEN.
   *
   * MEASURED, with a real user gesture and not a synthetic one. From
   * `/know/brain`, click the SYSTEM bar (which lands on MODELS when that is
   * the remembered leaf), open the GPU modal, press browser Back. Law 3 keeps
   * SYSTEM's pane mounted through a topic change, so this component does NOT
   * unmount, `showGpu` stays true, and at `/know/brain`:
   *
   *     dialogs mounted            1
   *     overlay up                 yes
   *     body pointer-events        none      <- the whole app is unclickable
   *     topic-bar click            times out
   *     /api/models/gpu per 12 s   2
   *
   * Only Escape gets the user out, and nothing on screen says so. A LEAF
   * change is already safe — the pane swaps its page, this unmounts, the
   * dialog goes with it (measured: 0 dialogs, pointer-events auto) — so the
   * hole is exactly the topic axis, which is exactly what `useIsPaneActive`
   * answers.
   *
   * THIS IS AN INSTANCE FIX AND THE CLASS IS BIGGER: 22 files render a
   * `Dialog`/`Sheet` with a bound `open`, and every one of them mounted in a
   * leaf pane has the same hole. The class fix is four lines inside
   * `components/ui/dialog.tsx` — close on an inactive pane, which is never the
   * wrong behaviour for something modal — but that primitive is reached by
   * every page in the app, so it is carried as a question rather than taken
   * from inside a two-leaf pass.
   */
  const paneActive = useIsPaneActive();
  useEffect(() => {
    if (paneActive) return;
    setShowGpu(false);
    setDetailModel(null);
  }, [paneActive]);

  // Edition gate — okuro-air (no local GPU) has no local inference, so the
  // Discover/pull surface is hidden. Default to enabled while loading so it
  // shows immediately on advanced/pro.
  const { data: edition } = useQuery({
    queryKey: ["models", "edition"],
    queryFn: () => modelsApi.edition(),
    staleTime: 5 * 60_000,
  });
  /**
   * THE GUARD THE ADDRESS NEEDS. On a cloud-only box (okuro-air) there is no
   * local inference, so there is nothing to discover and the toggle is not
   * rendered — but `/system/models?view=discover` is still a valid URL, and
   * without this it would resolve to a panel the user cannot get back out of.
   * The clamp already existed for the local-state toggle; it now also answers
   * for an address someone pasted.
   */
  const localInference = edition?.local_inference ?? true;
  const effectiveMode: ModelsMode = localInference ? mode : "installed";

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

  /* THE LEAF'S TWO CHROME ACTIONS GO TO THE PLATE. They were the old
     `PageHeader`'s `right` slot, then the trailing edge of the first content
     row; the plate is where the sketch puts them. No `title` override — the
     shell derives "Models" and R1 gives it that rank. */
  const header = useMemo(
    () => ({
      actions: (
        <>
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
            <RefreshCw className={cn(isFetching && "animate-spin")} aria-hidden="true" />
          </Button>
        </>
      ),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [localInference, isFetching, refetch],
  );
  useSectionTitle(header);

  return (
    <div className="page-shell space-y-10">
      {/* R1 (86b8f1f0) — THE LEAF TITLE IS THE SHELL'S. `TopicBar` renders
          `<h1 class="c-title">Models</h1>` above this pane, so the page's own
          PageHeader h1 is gone.

          WHAT MOVED AND WHY IT COULD NOT JUST BE DELETED. `PageHeader` carried
          two REAL controls in its `right` slot — the GPU modal trigger and the
          registry reload — and they are the leaf's only two chrome actions. So
          the header becomes the first CONTENT block: the census line keeps its
          job as the block's lead (it is a measurement, not decoration, and R7
          makes it the true total), and the two buttons sit on the same row at
          its trailing edge, where the header put them.

          `text-fg-muted` rather than `PageHeader`'s `text-tertiary`: that token
          is the standing AA failure (kit todo c581c9b2), and re-homing the line
          was the chance to stop feeding it. */}
      {/* THE TWO CHROME ACTIONS ARE ON THE PLATE NOW — see the memo above.
          The census line stays here: it is a measurement the block leads with,
          not a control. */}
      <p className="type-small text-fg-muted">
        {data
          ? `${data.total} models across ${Object.keys(data.by_provider).length} providers`
          : isLoading
            ? "loading…"
            : "—"}
      </p>

      <VramReclaimModal open={showGpu} onClose={() => setShowGpu(false)} />

      {localInference ? (
        <Segmented<ModelsMode>
          ariaLabel="Model view"
          value={mode}
          onChange={setMode}
          options={[
            { label: "Installed", value: "installed" },
            { label: "Inventory", value: "inventory" },
            { label: "Consumers", value: "consumers" },
            { label: "Interesting", value: "interesting" },
            { label: "Swap plans", value: "swaps" },
          ]}
        />
      ) : (
        <p className="rounded-md border border-border-subtle bg-surface-subtle px-3 py-2 text-xs text-fg-subtle">
          Cloud-only (okuro-{edition?.edition ?? "air"}) — no local GPU, so local
          model discovery and inference are off. Subscription models below.
        </p>
      )}

      {effectiveMode === "inventory" ? (
        <InventoryTab />
      ) : effectiveMode === "consumers" ? (
        <ConsumersTab />
      ) : effectiveMode === "swaps" ? (
        <SwapPlansTab />
      ) : effectiveMode === "interesting" ? (
        <InterestingPanel />
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
  //
  // R7 (372ccdb2) — `hasParams` USED TO TEST THE WRONG FIELD. It read
  // `!!m.parameters || m.size_gb > 0`, so the Params column appeared whenever
  // ANY row in the group had a file size — including a group where not one row
  // carries a parameter count. Paired with the cell's own `|| size_gb`
  // fallback (below) that is how five of 48 rows printed their file size under
  // a heading that says Params, with the same number repeated in Size beside
  // it. A column named after a field is gated on THAT field.
  const hasParams = models.some((m) => !!m.parameters);
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
      <div className="overflow-x-auto rounded-md border border-border-subtle">
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
                key={modelRowKey(m)}
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
                    /* A CASE LITERAL THE SOURCE CENSUS CANNOT SEE. `0d37d05e`
                       says letter case is the kit's decision, and the p3 census
                       greps for the `uppercase` CLASS — so this `.toUpperCase()`
                       counted as zero while doing the same thing one layer
                       lower, and worse: baking the case into the STRING defeats
                       `StatusBadge`'s own `case-label`, which already reads
                       `--type-label-transform`. Passing the raw value lets the
                       kit decide. Zero visual delta under `standard`, which
                       publishes `uppercase`. */
                    <StatusBadge
                      tone={TIER_TONE[m.tier] || "neutral"}
                      label={m.tier}
                    />
                  ) : (
                    <span className="text-xs text-fg-subtle">—</span>
                  )}
                </td>
                {hasSource && (
                  <td className="px-3 py-2 text-fg-muted">{m.source}</td>
                )}
                {hasParams && (
                  /* R7 — AN UNKNOWN VALUE RENDERS AS A DASH, NEVER AS A
                     NEIGHBOURING FIELD. This cell used to fall back to
                     `size_gb`, so a row with no parameter count printed its
                     file size here AND in the Size column one cell to the
                     right: measured, 5 of 48 rows read `Params 21.1GB · Size
                     21.1GB`. A file size is not a parameter count, and a
                     plausible wrong value is the exact failure 245d4942 is
                     about — the dash grep and the duplicate-key sweep both
                     PASS on "21.1GB". The dash is also what makes the column
                     auditable: its count must equal the endpoint's null count
                     for `parameters`, the same cross-check the Tier column's
                     39 dashes already satisfy. */
                  <td className="px-3 py-2 text-fg-muted">
                    {m.parameters || "—"}
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
          <div className="case-label mb-1 text-2xs tracking-wider text-tertiary">
            Path
          </div>
          <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-surface-subtle px-2 py-1 text-xs font-mono text-fg-muted">
            {model.path}
          </pre>
        </div>
      )}
      {model.capabilities.length > 0 && (
        <div>
          <div className="case-label mb-1 text-2xs tracking-wider text-tertiary">
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
        <div className="case-label mb-1 text-2xs tracking-wider text-tertiary">
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
      <div className="case-label text-2xs tracking-wider text-tertiary">
        {label}
      </div>
      <div className="text-fg">{value}</div>
    </div>
  );
}

/**
 * Interesting — Q3 and Q5: what was published that would actually run here.
 *
 * Default content is the durable list the Monday scan files, each row carrying
 * P3's lineage RELATION to something installed and its best PLACEMENT on this
 * host. A NULL relation is "the pass has not judged this row", which is a
 * different fact from `unrelated` and is rendered as such.
 *
 * The variants toggle is ruling 3: abliterated / uncensored / nsfw / roleplay
 * rows are TAGGED by the scan and kept. This decides only what is on screen.
 *
 * A live OSS search overrides the list when a query is submitted.
 */
/**
 * Rows the discoveries list asks for — and the reason the number appears twice.
 *
 * R7 (372ccdb2): "a capped list shows the TRUE TOTAL, never the cap; unknown
 * values render as a dash." The label under the search form printed
 * `discoveries.length`, which is the CAP whenever the durable list is at least
 * this long. `/api/models/discoveries` returns rows and nothing else — no
 * total — so a response that comes back at exactly the ceiling is ambiguous:
 * it may be complete or it may be truncated, and the honest reading of an
 * ambiguous number is that it is unknown.
 *
 * Measured live 2026-09-15: 40 rows at `limit=100`, 40 at `limit=101`, 40 at
 * `limit=500`. So the list is NOT capped today and the label prints the true
 * 40 — the dash is the state this fires into once the weekly scan has filed a
 * hundred, not a change to what is on screen now.
 *
 * MAIN'S VARIANTS TOGGLE DOES NOT WEAKEN THIS. Hiding variants makes the
 * response SHORTER, never longer, so a hidden-variants list at the ceiling is
 * still ambiguous and still renders the dash.
 */
const DISCOVERY_LIMIT = 100;

function InterestingPanel() {
  const qc = useQueryClient();
  const { startPull, jobFor } = useDownloads();
  const [q, setQ] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [modality, setModality] = useState("text");
  // Ruling 3: abliterated / uncensored / nsfw / roleplay rows are TAGGED by the
  // scan and kept. This is the only thing that decides whether they are on
  // screen, and it is off by default.
  const [variants, setVariants] = useState<"hide" | "show">("hide");

  const searching = submitted.length > 0;

  // Default content: the durable discoveries list the weekly scan files.
  // No network round-trip to HF — this is what "the list shows discoveries
  // by default" means.
  const discQ = useQuery({
    queryKey: ["models", "discoveries", variants],
    queryFn: () => modelsApi.discoveries({ limit: DISCOVERY_LIMIT, variants }),
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
  // R7 — `null` means "not knowable from this fetch", which is true both while
  // nothing has answered and when the answer came back at the ceiling.
  const discoveryTotal =
    discQ.data !== undefined && discoveries.length < DISCOVERY_LIMIT
      ? discoveries.length
      : null;

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
        {!searching && (
          <label className="flex items-center gap-2 text-xs text-fg-subtle">
            <input
              type="checkbox"
              checked={variants === "show"}
              onChange={(e) => setVariants(e.target.checked ? "show" : "hide")}
            />
            Show variants
          </label>
        )}
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
          <div className="overflow-x-auto rounded-md border border-border-subtle">
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
            {discoveryTotal ?? "—"} discovered · fit this box · newest first
            {variants === "hide" ? " · variants hidden" : " · variants shown"}
          </SectionLabel>
          <div className="overflow-x-auto rounded-md border border-border-subtle">
            <table className="w-full text-sm">
              <thead className="bg-surface-subtle text-xs font-medium text-fg-subtle">
                <tr>
                  <th className="px-3 py-2 text-left">Name</th>
                  <th className="px-3 py-2 text-left">Relation</th>
                  <th className="px-3 py-2 text-left">Why</th>
                  <th className="px-3 py-2 text-left">Best placement</th>
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
      <td className="px-3 py-2">
        <RelationBadge relation={disc.relation} target={disc.relation_target} />
      </td>
      <td className="max-w-md px-3 py-2 text-fg-muted">
        <span className="line-clamp-2">{why}</span>
        {disc.interesting_why && (
          <span className="mt-0.5 block text-3xs text-accent">
            {disc.interesting_why}
          </span>
        )}
        {disc.caveats && (
          <span className="mt-0.5 block text-3xs text-fg-subtle">
            {disc.caveats}
          </span>
        )}
      </td>
      {/* NO `whitespace-nowrap`, AND MAIN'S CELL NEEDS THE RULE MORE THAN THE
          ONE IT REPLACED. The old Fit cell held a bare GPU name, and a long
          one ("NVIDIA RTX PRO 4000 Blackwell 23GB") held the column at
          367.59px — measured — which alone is half the 751.63px pane. This
          cell carries a badge, the placement MODE and that same GPU name, so
          the unbreakable run is strictly longer. The badge keeps its own
          shape; the row is what is allowed to wrap. */}
      <td className="px-3 py-2">
        {disc.best_fit ? (
          <span className="inline-flex flex-wrap items-center gap-1.5">
            <StatusBadge
              tone={PLACEMENT_TONE[disc.best_fit.speed_class] ?? "neutral"}
              label={disc.best_fit.speed_class}
            />
            <span className="text-xs text-fg-muted">
              {disc.best_fit.mode}
              {disc.best_fit.gpu ? ` · ${disc.best_fit.gpu}` : ""}
            </span>
          </span>
        ) : (
          <span className="text-xs text-fg-subtle">{disc.fit_gpu || "—"}</span>
        )}
      </td>
      <td className="px-3 py-2 text-fg-muted">
        {disc.min_vram_gb ? `${disc.min_vram_gb.toFixed(1)}GB` : "—"}
      </td>
      <td className="px-3 py-2 text-right">
        <div className="inline-flex items-center gap-2">
          <PullAction
            catalogId={disc.catalog_id}
            displayName={disc.display_name}
            status={status}
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


