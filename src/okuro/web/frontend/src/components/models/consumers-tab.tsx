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
import { modelsApi, type ConsumerRef } from "@/lib/models-api";
import {
  NotConfigured,
  TierBadge,
  Since,
  fetchedTotal,
  renderCapNote,
} from "./shared";

/**
 * Consumers — Q2: which tool uses which model, proven by a config file:line.
 *
 * The column that carries the weight is LOCATION. "tm-mitate uses Llama-3.3"
 * is something a person then has to go and check; "tm-mitate names it at
 * lib/lifecycle.py:50" IS the check, and it is the line a swap plan rewrites.
 *
 * Two facts this tab states rather than implies. A DEAD ref resolves to no
 * unit on this host and is the finding, not an error. UNREACHABLE means the
 * unit exists and this consumer's own bind mount cannot open it — a symlink
 * that resolves for a shell and not for the container.
 */

type Filter = "all" | "dead" | "unreachable";

/** The two owners a string scanner provably cannot find on this host, named
 *  so an empty consumer list is never read as "nothing uses it". */
/** The fetch ceiling and the two render ceilings, named for R7's labels. */
const FETCH_LIMIT = 3000;
const RENDER_LIMIT = 400;
const UNREFERENCED_RENDER_LIMIT = 60;

const COMPUTED_PATH_BLIND_SPOTS = [
  "avatar/LongCat-Video — its loader builds the path from a sibling's",
  "video/Wan2_2_VAE_bf16.safetensors — same shape, no string reference anywhere",
];

export function ConsumersTab() {
  const [filter, setFilter] = useState<Filter>("all");
  const [consumer, setConsumer] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  const q = useQuery({
    queryKey: ["models", "consumers"],
    queryFn: () => modelsApi.consumers({ unreferenced: true, limit: FETCH_LIMIT }),
    staleTime: 60_000,
  });

  async function rescan() {
    setRefreshing(true);
    try {
      await modelsApi.consumers({ refresh: true, limit: 1 });
      await q.refetch();
    } finally {
      setRefreshing(false);
    }
  }

  const data = q.data;
  const rows = useMemo(() => {
    let out: ConsumerRef[] = data?.rows ?? [];
    if (consumer) out = out.filter((r) => r.consumer === consumer);
    if (filter === "dead") out = out.filter((r) => r.unit_id === null);
    if (filter === "unreachable") out = out.filter((r) => !r.reachable);
    if (search) {
      const s = search.toLowerCase();
      out = out.filter(
        (r) =>
          r.model_ref.toLowerCase().includes(s) ||
          r.config_path.toLowerCase().includes(s) ||
          (r.unit_id ?? "").toLowerCase().includes(s),
      );
    }
    return out;
  }, [data, consumer, filter, search]);

  if (q.isLoading)
    return <div className="h-64 animate-pulse rounded-md border border-border-subtle bg-surface-elevated" />;
  if (data && !data.configured) return <NotConfigured reason={data.reason} />;

  // `null` when the fetch came back at its own ceiling -- see `fetchedTotal`.
  const refTotal =
    fetchedTotal(data?.rows?.length ?? 0, FETCH_LIMIT) === null
      ? null
      : rows.length;

  return (
    <div className="space-y-6">
      <div className="overflow-x-auto rounded-md border border-border-subtle">
        <table className="w-full text-sm">
          <thead className="bg-surface-subtle text-xs font-medium text-fg-subtle">
            <tr>
              <th className="px-3 py-2 text-left">Consumer</th>
              <th className="px-3 py-2 text-left">Tier</th>
              <th className="px-3 py-2 text-left">Run mode</th>
              <th className="px-3 py-2 text-left">State</th>
              <th className="px-3 py-2 text-right">Refs</th>
              <th className="px-3 py-2 text-right">Units</th>
              <th className="px-3 py-2 text-right">Dead</th>
              <th className="px-3 py-2 text-right">Unreachable</th>
              <th className="px-3 py-2 text-left">Scanned</th>
            </tr>
          </thead>
          <tbody>
            {(data?.consumers ?? []).map((c) => (
              <tr
                /* THE SUMMARY IS GROUPED BY THREE COLUMNS AND THE KEY CARRIED
                   ONE. `consumers.py:1653` is `GROUP BY consumer, run_mode,
                   tier`, so one consumer reached by two run modes is two rows.
                   MEASURED LIVE 2026-09-16: 19 summary rows, 18 distinct
                   `consumer` -- `tm-model-creator` appears as `python/script`
                   (72 refs) and as `systemd/running` (1 ref), and React logged
                   "two children with the same key, tm-model-creator" twice at
                   every read time from 0 to 3000 ms. Third instance of the
                   same class after the 48-row MODELS key (d4f74697) and the
                   reference row below: a key is only as unique as the columns
                   it carries, and the query that produced the rows says which
                   they are. */
                key={JSON.stringify([c.consumer, c.run_mode, c.tier])}
                onClick={() => setConsumer(consumer === c.consumer ? null : c.consumer)}
                className={cn(
                  "cursor-pointer border-t border-border-subtle transition-fast hover:bg-surface-subtle",
                  consumer === c.consumer && "bg-surface-subtle",
                )}
              >
                <td className="px-3 py-2 font-medium text-fg">{c.consumer}</td>
                <td className="px-3 py-2">
                  <TierBadge tier={c.tier} />
                </td>
                <td className="px-3 py-2 text-fg-muted">{c.run_mode}</td>
                <td className="px-3 py-2 text-fg-muted">{c.state}</td>
                <td className="px-3 py-2 text-right tabular-nums text-fg-muted">{c.refs}</td>
                <td className="px-3 py-2 text-right tabular-nums text-fg-muted">{c.units}</td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {c.dead > 0 ? <span className="text-error">{c.dead}</span> : "—"}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {c.unreachable > 0 ? <span className="text-warning">{c.unreachable}</span> : "—"}
                </td>
                <td className="px-3 py-2 text-xs text-fg-subtle">
                  <Since iso={c.last_seen} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Input
          placeholder="Filter by reference, path or unit…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-64"
        />
        <Segmented
          ariaLabel="Reference filter"
          value={filter}
          onChange={(v) => setFilter(v as Filter)}
          options={[
            { label: "All", value: "all", count: data?.totals.rows ?? 0 },
            { label: "Dead refs", value: "dead", count: data?.totals.dead ?? 0 },
            { label: "Unreachable", value: "unreachable", count: data?.totals.unreachable ?? 0 },
          ]}
        />
        {consumer && (
          <Button variant="ghost" size="sm" onClick={() => setConsumer(null)}>
            Clear {consumer}
          </Button>
        )}
        <Button variant="outline" size="sm" onClick={rescan} disabled={refreshing}>
          <RefreshCw className={cn("h-3 w-3", refreshing && "animate-spin")} aria-hidden="true" />
          Re-walk roots
        </Button>
      </div>

      {/* R7 -- the true total, a dash at the fetch ceiling, and the render cap
          named when it bites. */}
      <SectionLabel>
        {refTotal ?? "—"} reference{refTotal === 1 ? "" : "s"}
        {consumer ? ` · ${consumer}` : ""} · each proven by a file:line
        {renderCapNote(rows.length, RENDER_LIMIT)}
      </SectionLabel>

      {rows.length === 0 ? (
        <EmptyState
          title="No references match"
          description="Clear the filters, or press Re-walk roots to rescan the declared consumer roots."
        />
      ) : (
        <div className="overflow-x-auto rounded-md border border-border-subtle">
          <table className="w-full text-sm">
            <thead className="bg-surface-subtle text-xs font-medium text-fg-subtle">
              <tr>
                <th className="px-3 py-2 text-left">Consumer</th>
                <th className="px-3 py-2 text-left">Reference as written</th>
                <th className="px-3 py-2 text-left">Resolves to</th>
                <th className="px-3 py-2 text-left">Location</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, RENDER_LIMIT).map((r) => (
                <tr
                  /* THE KEY DROPPED ONE OF THE FOUR COLUMNS THAT MAKE A ROW
                     UNIQUE. `model_consumers` own constraint is
                     `UNIQUE (consumer, config_path, line, model_ref)`
                     (migration 151), and two consumers CAN name the same model
                     at the same file:line -- a shared compose fragment or a
                     shared yaml read by two units is exactly that shape. Same
                     class as the 48-row MODELS key (d4f74697): a key is only
                     as unique as the columns it carries, and the table holding
                     the rows already states which they are.

                     MEASURED, AND HONESTLY: on this host today the three-column
                     key does NOT collide -- 737 reference rows, 737 distinct
                     three-column keys, 737 distinct four-column keys. So this
                     is hardening against what the constraint permits, not a
                     live defect. The summary key above WAS a live defect and
                     is what sent me looking here.

                     `JSON.stringify` rather than a joined string, because a
                     separator is only injective if it cannot occur in a field,
                     and `model_ref` is arbitrary user text. Guessing that it
                     will not contain the separator is the reasoning that
                     produced the two wrong MODELS keys. */
                  key={JSON.stringify([
                    r.consumer,
                    r.config_path,
                    r.line,
                    r.model_ref,
                  ])}
                  className="border-t border-border-subtle align-top"
                >
                  <td className="px-3 py-2 text-fg-muted">{r.consumer}</td>
                  <td className="px-3 py-2 font-mono text-3xs text-fg">{r.model_ref}</td>
                  <td className="px-3 py-2">
                    {r.unit_id ? (
                      <div className="flex flex-wrap items-center gap-1">
                        <span className="font-mono text-3xs text-fg-muted">{r.unit_id}</span>
                        {!r.reachable && <StatusBadge tone="warning" label="unreachable" />}
                      </div>
                    ) : (
                      <StatusBadge tone="error" label="dead ref" showDot />
                    )}
                    {r.note && r.note !== "dead-ref" && (
                      <div className="text-3xs text-fg-subtle">{r.note}</div>
                    )}
                  </td>
                  <td className="px-3 py-2 font-mono text-3xs text-fg-subtle">
                    {r.config_path}:{r.line}
                    {r.match_kind && <span className="ml-1 text-fg-muted">({r.match_kind})</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(data?.unreferenced?.length ?? 0) > 0 && (
        <section className="space-y-2">
          <SectionLabel>
            {data?.unreferenced?.length} units no declared consumer names
            {renderCapNote(
              data?.unreferenced?.length ?? 0,
              UNREFERENCED_RENDER_LIMIT,
            )}
          </SectionLabel>
          <p className="rounded-md border border-border-subtle bg-surface-subtle px-3 py-2 text-xs text-fg-subtle">
            Evidence for a conversation, never grounds for deleting anything: a
            loader can build a path at runtime and leave no reference to find.
            Two owners on this host are known to be invisible to any string
            scanner —{" "}
            {COMPUTED_PATH_BLIND_SPOTS.map((b, i) => (
              <span key={b}>
                {i > 0 && "; "}
                {b}
              </span>
            ))}
            .
          </p>
          <div className="overflow-x-auto rounded-md border border-border-subtle">
            <table className="w-full text-sm">
              <thead className="bg-surface-subtle text-xs font-medium text-fg-subtle">
                <tr>
                  <th className="px-3 py-2 text-left">Store</th>
                  <th className="px-3 py-2 text-right">GB</th>
                  <th className="px-3 py-2 text-left">Unit</th>
                </tr>
              </thead>
              <tbody>
                {(data?.unreferenced ?? []).slice(0, UNREFERENCED_RENDER_LIMIT).map((u) => (
                  <tr key={u.unit_id} className="border-t border-border-subtle">
                    <td className="px-3 py-2 text-fg-muted">{u.store}</td>
                    <td className="px-3 py-2 text-right tabular-nums text-fg-muted">
                      {u.size_gb.toFixed(1)}
                    </td>
                    <td className="px-3 py-2 font-mono text-3xs text-fg">{u.rel_path}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
