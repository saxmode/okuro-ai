import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Segmented } from "@/components/ui/segmented";
import { SectionLabel } from "@/components/ui/section-label";
import { EmptyState } from "@/components/ui/empty-state";
import { StatusBadge } from "@/components/ui/status-badge";
import { cn } from "@/lib/utils";
import {
  modelsApi,
  type ModelUnit,
  type StoreInfo,
  type StoreSummary,
} from "@/lib/models-api";
import {
  NotConfigured,
  TierBadge,
  Since,
  PLACEMENT_TONE,
  fetchedTotal,
  renderCapNote,
} from "./shared";

/**
 * Inventory — Q1: what models exist, on which store, how big, what is broken.
 *
 * Two things this tab must not imply. An empty `consumers` list means no
 * DECLARED consumer names the unit, never that nothing uses it — a loader can
 * build its path at runtime, and one 21.7 GB unit on this host is read daily
 * with no string reference anywhere. And a store bar at 89 % is a fact, not a
 * recommendation: ruling 8 removed the headroom floor, so this warns and
 * proposes nothing.
 */

/** Ruling 5: the cold store is re-walked on demand, dir-stat only. */
const REFRESH_MODE = "stat" as const;

/** The line above which a store bar turns warning. Display only — it gates
 *  nothing, because there is no headroom floor anywhere in this feature. */
const FULL_PCT = 85;

/** The fetch ceiling and the render ceiling — named because R7's label has to
 *  describe BOTH, and an inline `2000`/`400` describes neither. */
const FETCH_LIMIT = 2000;
const RENDER_LIMIT = 400;

type Filter = "all" | "broken" | "twins" | "unreferenced";

export function InventoryTab() {
  const [filter, setFilter] = useState<Filter>("all");
  const [store, setStore] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  const q = useQuery({
    queryKey: ["models", "inventory"],
    queryFn: () => modelsApi.inventory({ twins: true, limit: FETCH_LIMIT }),
    staleTime: 60_000,
  });

  // The live re-walk is a BUTTON, not a page load. Ruling 5 asked for the
  // cold store to be current on demand; a stat walk of 271k files on every
  // render would spin the disks of an archive that changes weekly.
  async function rescan() {
    setRefreshing(true);
    try {
      await modelsApi.inventory({ refresh: REFRESH_MODE, limit: 1 });
      await q.refetch();
    } finally {
      setRefreshing(false);
    }
  }

  const data = q.data;
  const units = useMemo(() => {
    let out = data?.units ?? [];
    if (store) out = out.filter((u) => u.store === store);
    if (filter === "broken") out = out.filter((u) => u.status !== "ok");
    if (filter === "unreferenced")
      out = out.filter((u) => (u.consumers?.length ?? 0) === 0);
    if (filter === "twins") {
      const byIdentity = new Map<string, number>();
      for (const u of data?.units ?? [])
        if (u.identity) byIdentity.set(u.identity, (byIdentity.get(u.identity) ?? 0) + 1);
      out = out.filter((u) => !!u.identity && (byIdentity.get(u.identity) ?? 0) > 1);
    }
    if (search) {
      const s = search.toLowerCase();
      out = out.filter(
        (u) =>
          u.rel_path.toLowerCase().includes(s) ||
          (u.family ?? "").toLowerCase().includes(s) ||
          (u.consumers ?? []).some((c) => c.toLowerCase().includes(s)),
      );
    }
    return [...out].sort((a, b) => b.size_bytes - a.size_bytes);
  }, [data, store, filter, search]);

  if (q.isLoading)
    return <div className="h-64 animate-pulse rounded-md border border-border-subtle bg-surface-elevated" />;
  if (data && !data.configured) return <NotConfigured reason={data.reason} />;

  const counts = data?.units ?? [];
  const broken = counts.filter((u) => u.status !== "ok").length;
  const unreferenced = counts.filter((u) => (u.consumers?.length ?? 0) === 0).length;
  const twinIds = new Set(
    Object.entries(
      counts.reduce<Record<string, number>>((acc, u) => {
        if (u.identity) acc[u.identity] = (acc[u.identity] ?? 0) + 1;
        return acc;
      }, {}),
    )
      .filter(([, n]) => n > 1)
      .map(([id]) => id),
  );
  const twins = counts.filter((u) => u.identity && twinIds.has(u.identity)).length;
  // `null` when the fetch came back at its own ceiling — see `fetchedTotal`.
  const unitTotal =
    fetchedTotal(counts.length, FETCH_LIMIT) === null ? null : units.length;

  return (
    <div className="space-y-6">
      {/* R3 (8546865f) — PANE-KEYED, NOT WINDOW-KEYED. `sm:`/`lg:` are 384px and
          512px under the engine's 8px root, so both were TRUE in every state
          the shell can produce and three store cards were squeezed into a
          751.63px pane. `@2xl`/`@6xl` resolve against the named `pane`
          container at 672px and 1152px, which is where two and three columns
          actually fit. Rungs from `--container-*` (globals.css), the scale the
          shell's own R3 declaration documents. */}
      <div className="grid gap-3 @2xl:grid-cols-2 @6xl:grid-cols-3">
        {(data?.summary ?? []).map((s) => (
          <StoreCard
            key={s.store}
            summary={s}
            store={data?.stores.find((x) => x.name === s.store)}
          />
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Input
          placeholder="Filter by path, family or consumer…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-64"
        />
        <Segmented
          ariaLabel="Store"
          variant="ghost"
          value={store ?? ""}
          onChange={(v) => setStore(v === store ? null : v)}
          options={(data?.stores ?? []).map((s) => ({ label: s.name, value: s.name }))}
        />
        <Segmented
          ariaLabel="Inventory filter"
          value={filter}
          onChange={(v) => setFilter(v as Filter)}
          options={[
            { label: "All", value: "all", count: counts.length },
            { label: "Broken", value: "broken", count: broken },
            { label: "Twins", value: "twins", count: twins },
            { label: "No declared consumer", value: "unreferenced", count: unreferenced },
          ]}
        />
        <Button variant="outline" size="sm" onClick={rescan} disabled={refreshing}>
          <RefreshCw className={cn("h-3 w-3", refreshing && "animate-spin")} aria-hidden="true" />
          Refresh stores
        </Button>
      </div>

      {filter === "unreferenced" && (
        <p className="rounded-md border border-border-subtle bg-surface-subtle px-3 py-2 text-xs text-fg-subtle">
          These are the units no <em>declared</em> consumer names. That is evidence
          for a conversation and never grounds for deleting anything: a loader can
          build its path at runtime, and two units on this host are known to be
          read daily with no string reference anywhere.
        </p>
      )}

      {/* R7 (372ccdb2) — THE TRUE TOTAL, A DASH WHEN IT IS NOT KNOWABLE, AND
          THE RENDER CAP NAMED WHEN IT BITES. `units.length` is the filtered
          count of what the fetch returned; at the fetch ceiling that count IS
          the ceiling and says nothing about the real number, and the table
          paints only the largest 400 of it either way. */}
      <SectionLabel>
        {unitTotal ?? "—"} unit{unitTotal === 1 ? "" : "s"} · largest first
        {renderCapNote(units.length, RENDER_LIMIT)}
      </SectionLabel>

      {units.length === 0 ? (
        <EmptyState
          title="No units match"
          description="Clear the filters, or press Refresh stores to re-walk them."
        />
      ) : (
        // `overflow-x-auto`, not `overflow-hidden`: seven columns against a
        // 90-character unit path pushed Quant, Consumers and Best placement
        // off the right edge of the card, where nothing could reach them.
        //
        // `min-w-[200rem]`, NOT `64rem` — AND THE UNIT IS THE WHOLE POINT.
        // The engine's root is 8px, so `64rem` is 512px, not the 1024px it
        // reads as. Measured: `min-width: 512px` against a 750px wrapper, so
        // the min never bound, the wrapper never scrolled, and `table-fixed`
        // divided 750px among seven percentage columns. 513 cells were clipped
        // at 1366 and 9 at 1900 with NO ellipsis, NO scroll and NO tooltip —
        // including the HEADERS: `Store` is 54px of text in a 7% column that
        // was 52px, so the column could not say what it was.
        //
        // WHY 200rem AND NOT A SMALLER NUMBER. Widest unbreakable run per
        // column, divided by that column's share, is the width the table needs:
        //
        //   Best placement  15%  112px  ->   747
        //   Store            7%   64px  ->   915
        //   Size GB          7%   70px  ->  1000
        //   Quant            8%   94px  ->  1175
        //   Family          10%  154px  ->  1540   <- the binding one
        //   Unit            34% 1090px  ->  3206   } ellipsis + title BY DESIGN
        //   Consumers       19%  696px  ->  3664   }
        //
        // 1540px is the floor for the five columns that carry short values;
        // 200rem is 1600px at this root, the next round rung above it. The two
        // long-string columns keep `truncate` + `title`, which is the right
        // answer for a 90-character path and is already what they do — fitting
        // THEM would want a 3664px table. A local constant rather than a token
        // because the kit ships no width scale (D1, and the same reasoning the
        // shell's own R3 rungs carry).
        <div className="overflow-x-auto rounded-md border border-border-subtle">
          <table className="w-full min-w-[200rem] table-fixed text-sm">
            <thead className="bg-surface-subtle text-xs font-medium text-fg-subtle">
              <tr>
                <th className="w-[34%] px-3 py-2 text-left">Unit</th>
                <th className="w-[7%] px-3 py-2 text-left">Store</th>
                <th className="w-[7%] px-3 py-2 text-right">Size&nbsp;GB</th>
                <th className="w-[10%] px-3 py-2 text-left">Family</th>
                <th className="w-[8%] px-3 py-2 text-left">Quant</th>
                <th className="w-[19%] px-3 py-2 text-left">Consumers</th>
                <th className="w-[15%] px-3 py-2 text-left">Best placement</th>
              </tr>
            </thead>
            <tbody>
              {units.slice(0, RENDER_LIMIT).map((u) => (
                <UnitRow key={u.unit_id} unit={u} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function StoreCard({
  summary,
  store,
}: {
  summary: StoreSummary;
  store?: StoreInfo;
}) {
  // used / free come from the FILESYSTEM, measured on this request, not from
  // the sum of unit sizes — a store holds more than models, and the same
  // number drives the swap plan's headroom warning.
  const free = store?.free_gb ?? null;
  const total = store?.total_gb ?? null;
  const pct = store?.used_pct ?? null;
  const tight = pct !== null && pct >= FULL_PCT;
  return (
    <div className="rounded-md border border-border-subtle bg-surface-subtle p-3">
      <div className="flex items-baseline justify-between">
        <span className="font-medium text-fg">{summary.store}</span>
        <StatusBadge tone={store?.tier === "cold" ? "info" : "accent"} label={store?.tier ?? "—"} />
      </div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-fg">
        {free === null ? "—" : free.toLocaleString(undefined, { maximumFractionDigits: 0 })}
        <span className="ml-1 text-xs font-normal text-fg-subtle">GB free</span>
      </div>
      <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-surface-elevated">
        <div
          className={cn("h-full", tight ? "bg-warning" : "bg-primary")}
          style={{ width: `${Math.min(100, pct ?? 0)}%` }}
        />
      </div>
      <div className="mt-2 space-y-0.5 text-xs text-fg-subtle">
        <div className="tabular-nums">
          {pct ?? "—"}% used
          {total !== null && ` of ${total.toLocaleString(undefined, { maximumFractionDigits: 0 })} GB`}
          {" · "}
          {summary.size_gb.toLocaleString(undefined, { maximumFractionDigits: 0 })} GB in models
        </div>
        {tight && (
          <div className="text-warning">
            above {FULL_PCT}% — a warning, not a limit: nothing here refuses a
            download over it
          </div>
        )}
        <div>
          {summary.units} units · {summary.identified} fingerprinted
          {summary.broken > 0 && <span className="text-error"> · {summary.broken} broken</span>}
        </div>
        {store?.path && <div className="truncate font-mono text-3xs">{store.path}</div>}
        <div>
          scanned <Since iso={summary.last_scanned} />
        </div>
      </div>
    </div>
  );
}

function UnitRow({ unit }: { unit: ModelUnit }) {
  const fit = unit.best_fit;
  return (
    <tr className="border-t border-border-subtle align-top">
      <td className="px-3 py-2">
        <div className="truncate font-medium text-fg" title={unit.rel_path}>
          {unit.rel_path}
        </div>
        {unit.status !== "ok" && (
          <div className="text-3xs text-error">
            {unit.status}: {unit.note ?? "no reason recorded"}
          </div>
        )}
        {/* P6 — where to SEE what this model produces. Absent when the host
            has declared no results_urls for its consumers or its modality,
            which is the honest answer and never a guessed port. */}
        {unit.results && unit.results.length > 0 && (
          <div className="mt-0.5 flex flex-wrap gap-2 text-3xs">
            <span className="text-fg-subtle">see results:</span>
            {unit.results.map((r) => (
              <a
                key={r.url}
                href={r.url}
                target="_blank"
                rel="noreferrer"
                className="text-accent hover:underline"
                title={`${r.label} (matched by ${r.via})`}
              >
                {r.label}
              </a>
            ))}
          </div>
        )}
      </td>
      <td className="px-3 py-2 text-fg-muted">{unit.store}</td>
      <td className="px-3 py-2 text-right tabular-nums text-fg-muted">
        {unit.size_gb.toFixed(1)}
      </td>
      <td className="px-3 py-2 text-fg-muted">{unit.family ?? "—"}</td>
      <td className="px-3 py-2 text-fg-muted">{unit.quant ?? "—"}</td>
      <td className="px-3 py-2">
        {unit.consumers && unit.consumers.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1">
            <TierBadge tier={unit.tier} />
            <span className="truncate text-xs text-fg-muted"
                  title={unit.consumers.join(", ")}>
              {unit.consumers.join(", ")}
            </span>
            {unit.reachable === false && (
              <StatusBadge tone="warning" label="unreachable" />
            )}
          </div>
        ) : (
          <span
            className="text-xs text-fg-subtle"
            title="No DECLARED consumer names it. Not proof nothing uses it."
          >
            none declared
          </span>
        )}
      </td>
      <td className="px-3 py-2">
        {fit ? (
          <span className="inline-flex flex-wrap items-center gap-1.5">
            <StatusBadge
              tone={PLACEMENT_TONE[fit.speed_class] ?? "neutral"}
              label={fit.speed_class}
            />
            <span className="text-xs text-fg-muted">
              {fit.mode}
              {fit.gpu ? ` · ${fit.gpu}` : ""}
            </span>
          </span>
        ) : (
          <span className="text-xs text-fg-subtle">no placement fits when idle</span>
        )}
      </td>
    </tr>
  );
}
