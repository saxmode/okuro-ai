import { memo, useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Send, Share2, X, XCircle } from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import { taskApi, previewApi } from "@/lib/api";
import { redlineApi } from "@/lib/redline-api";
import type { ArtifactInfo, DeliveryInfo } from "@/types/api";
import { MarkdownContent } from "@/components/ui/markdown-content";
import { Segmented } from "@/components/ui/segmented";
import { HandoverDialog } from "@/components/handover/handover-dialog";
import type { ContentIR } from "@/lib/handover-api";
import { useVisualAdaptations } from "@/hooks/use-ui-profile";
import { parseApiDate } from "@/lib/format";

/** Audience tab for the panel. "internal" folds process + agent artifacts;
 *  "deliveries" surfaces the Stream-C deliveries list. */
type AudienceTab = "user" | "internal" | "deliveries";

/** Audience of an artifact, defaulting missing/disk values to "user". */
function audienceOf(a: ArtifactInfo): "user" | "process" | "agent" {
  return a.audience ?? "user";
}

interface ArtifactsViewerProps {
  taskId: string;
  artifacts: ArtifactInfo[];
  /**
   * When set, the viewer narrows to artifacts produced by this subtask
   * (matched on the ``subtask_id`` field the backend derived from the
   * filename prefix). A "Filtered: X (a of b) — clear" banner appears
   * so the user can widen back to all artifacts in one click. When the
   * subtask produced no artifacts, the banner says so and the list is
   * empty rather than silently showing everything.
   */
  selectedSubtaskId?: string;
  onClearSubtaskFilter?: () => void;
  /** Task project slug — stamped onto a handed-over artifact's Content IR so
   *  the destination (prism brand, note project) resolves correctly. */
  project?: string;
}

type SortKey = "date" | "type" | "name";
type ViewMode = "list" | "timeline";

/**
 * An artifact after superseded-round collapse. ``versions`` is how many
 * underlying artifacts share this row's group (1 = no review rounds, N>1 =
 * reviewed N times). The spread fields are the ACTIVE artifact's.
 */
type CollapsedArtifact = ArtifactInfo & { versions: number };

function extOf(name: string): string {
  const idx = name.lastIndexOf(".");
  return idx === -1 ? "" : name.slice(idx + 1).toLowerCase();
}

/**
 * Display extension for an artifact. Brain artifacts have a UUID for
 * ``name`` so ``extOf`` returns ""; we fall back to the kind/media_type
 * the API reports. Disk artifacts behave as before.
 */
function displayExt(a: ArtifactInfo): string {
  if (a.source === "brain") {
    if (a.media_type?.includes("markdown")) return "md";
    if (a.media_type?.includes("pdf")) return "pdf";
    if (a.media_type?.includes("json")) return "json";
    if (a.kind === "plan") return "plan";
    if (a.kind === "evidence") return "evid";
    return "rep"; // report
  }
  return extOf(a.name);
}

// Stable color per extension, keyed to semantic tokens.
// Blue has been retired from this viewer — md/css roll into the brand
// accent so the doc viewer reads as a single-color-family surface.
const EXT_COLOR: Record<string, string> = {
  md: "bg-accent",
  txt: "bg-tertiary",
  json: "bg-warning",
  yaml: "bg-warning",
  yml: "bg-warning",
  py: "bg-success",
  ts: "bg-accent",
  tsx: "bg-accent",
  js: "bg-accent",
  html: "bg-error",
  css: "bg-accent",
  png: "bg-fg-muted",
  jpg: "bg-fg-muted",
  svg: "bg-fg-muted",
};

function extColor(ext: string): string {
  return EXT_COLOR[ext] ?? "bg-tertiary";
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function formatRelTime(iso: string | undefined): string {
  if (!iso) return "";
  const then = parseApiDate(iso).getTime();
  const now = Date.now();
  const secs = Math.round((now - then) / 1000);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

function dayKey(iso: string | undefined): string {
  if (!iso) return "unknown";
  const d = parseApiDate(iso);
  return d.toISOString().slice(0, 10);
}

function formatDayLabel(key: string): string {
  if (key === "unknown") return "Unknown date";
  const d = new Date(key + "T00:00:00Z");
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const yday = new Date(today);
  yday.setDate(today.getDate() - 1);
  if (d.getTime() === today.getTime()) return "Today";
  if (d.getTime() === yday.getTime()) return "Yesterday";
  return d.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

export const ArtifactsViewer = memo(function ArtifactsViewer({
  taskId,
  artifacts,
  selectedSubtaskId,
  onClearSubtaskFilter,
  project,
}: ArtifactsViewerProps) {
  const [sortKey, setSortKey] = useState<SortKey>("date");
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [selected, setSelected] = useState<ArtifactInfo | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  // Audience tab — default to the user-facing deliverables (hides QA/agent meta).
  const [tab, setTab] = useState<AudienceTab>("user");
  // Per-row hand-over: the Content IR opens the shared dialog once the artifact
  // body is fetched; handingOver marks the row whose body is loading.
  const [handoverContent, setHandoverContent] = useState<ContentIR | null>(null);
  const [handingOver, setHandingOver] = useState<string | null>(null);

  // Stream C — deliveries for this task (ROCK-SOLID v5 P3.3).
  //
  // Was a mount-only useEffect + useState: it ran once per taskId and had no
  // way to hear about anything afterwards, so a delivery that landed while
  // the page was open stayed invisible until a reload. Now a query, keyed so
  // the WS state_change handler can invalidate it — the engine emits
  // `delivery_sent` on every successful send (it emitted nothing before).
  //
  // Endpoint may be absent on older backends; a failure is "no deliveries",
  // not an error state, so the tab renders empty rather than broken.
  const { data: deliveries = [] } = useQuery({
    queryKey: ["deliveries", taskId],
    queryFn: () =>
      taskApi
        .getDeliveries(taskId)
        .then((resp) => resp.deliveries ?? [])
        .catch(() => [] as DeliveryInfo[]),
    enabled: !!taskId,
  });

  const deliveriesByArtifact = useMemo(() => {
    const map = new Map<string, DeliveryInfo[]>();
    for (const d of deliveries) {
      const aid = d.artifact_id;
      if (!aid) continue;
      if (!map.has(aid)) map.set(aid, []);
      map.get(aid)!.push(d);
    }
    return map;
  }, [deliveries]);

  // Audience filter is applied FIRST — before subtask scope and review-round
  // collapse (FIX A) — so the default panel is user deliverables only. The
  // deliveries tab is a distinct view (the Stream-C list), not an artifact set.
  const byAudience = useMemo(() => {
    if (tab === "deliveries") return [] as ArtifactInfo[];
    if (tab === "internal")
      return artifacts.filter((a) => audienceOf(a) !== "user");
    return artifacts.filter((a) => audienceOf(a) === "user");
  }, [artifacts, tab]);

  // Subtask scope is applied next; sort/group operate on the narrowed list.
  // A subtask with no artifacts produces an empty list (not silently widened) —
  // explicit "0 of N" feedback beats hiding the filter behavior.
  const scoped = useMemo(
    () =>
      selectedSubtaskId
        ? byAudience.filter((a) => a.subtask_id === selectedSubtaskId)
        : byAudience,
    [byAudience, selectedSubtaskId],
  );

  // Hand a brain artifact to another tool: fetch its body (same path the
  // overlay uses), build a text Content IR, then open the shared dialog.
  const openHandover = async (a: ArtifactInfo) => {
    const key = a.id ?? a.name;
    const fetchKey = a.source === "brain" && a.id ? a.id : a.name;
    setHandingOver(key);
    try {
      const body = await taskApi.getArtifact(taskId, fetchKey);
      setHandoverContent({
        kind: "text",
        title: a.title || a.name,
        body_md: body,
        project: project || undefined,
        source: { tool: "artifacts", id: a.id ?? undefined, label: a.title || a.name },
      });
    } catch (e) {
      toast.error("Could not load artifact", {
        description: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setHandingOver(null);
    }
  };

  // FIX A — collapse superseded review-round artifacts. A subtask reviewed
  // N times produces N artifacts with the same title; losers come back at
  // confidence≈0.1, the active one keeps ~0.8/0.9 (and carries `supersedes`).
  // Group by (subtask_id | title), keep ONLY the active artifact per group,
  // and stash the round count so the row can show a "N versions" badge.
  // Disk artifacts have confidence == null → never treated as superseded, so
  // legacy/binary outputs are unaffected.
  const collapsed = useMemo<CollapsedArtifact[]>(() => {
    const groups = new Map<string, ArtifactInfo[]>();
    for (const a of scoped) {
      const key = a.subtask_id || a.title || a.name;
      const list = groups.get(key);
      if (list) list.push(a);
      else groups.set(key, [a]);
    }
    const out: CollapsedArtifact[] = [];
    for (const list of groups.values()) {
      if (list.length === 1) {
        out.push({ ...list[0]!, versions: 1 });
        continue;
      }
      // Active = highest confidence (null treated as 0.5 so disk artifacts
      // outrank confidence≈0.1 losers but lose to genuine 0.8/0.9 winners),
      // newest as tiebreak. A row with confidence > 0.1 is the survivor.
      const rank = (a: ArtifactInfo) =>
        a.confidence == null ? 0.5 : a.confidence;
      const active = [...list].sort(
        (a, b) =>
          rank(b) - rank(a) ||
          (b.modified_at ?? "").localeCompare(a.modified_at ?? ""),
      )[0]!;
      out.push({ ...active, versions: list.length });
    }
    return out;
  }, [scoped]);

  const sorted = useMemo(() => {
    const copy = [...collapsed];
    if (sortKey === "date") {
      copy.sort(
        (a, b) => (b.modified_at ?? "").localeCompare(a.modified_at ?? ""),
      );
    } else if (sortKey === "type") {
      copy.sort((a, b) => {
        const ea = extOf(a.name);
        const eb = extOf(b.name);
        if (ea !== eb) return ea.localeCompare(eb);
        return a.name.localeCompare(b.name);
      });
    } else {
      copy.sort((a, b) => a.name.localeCompare(b.name));
    }
    return copy;
  }, [scoped, sortKey]);

  // Group by day for timeline view (newest first).
  const grouped = useMemo(() => {
    const map = new Map<string, CollapsedArtifact[]>();
    for (const a of sorted) {
      const k = dayKey(a.modified_at);
      if (!map.has(k)) map.set(k, []);
      map.get(k)!.push(a);
    }
    return Array.from(map.entries()).sort((a, b) => b[0].localeCompare(a[0]));
  }, [sorted]);

  if (artifacts.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-tertiary">
        No artifacts yet
      </div>
    );
  }

  const userCount = artifacts.filter((a) => audienceOf(a) === "user").length;
  const internalCount = artifacts.length - userCount;

  return (
    <div className="flex h-full flex-col">
      {/* Audience tabs — user deliverables (default), internal QA/agent meta,
          and the Stream-C deliveries list. */}
      <div className="flex h-row-dense items-center border-b border-border px-3">
        <Segmented<AudienceTab>
          value={tab}
          onChange={setTab}
          ariaLabel="Artifact audience"
          options={[
            { label: "User", value: "user", count: userCount },
            { label: "Internal", value: "internal", count: internalCount },
            { label: "Deliveries", value: "deliveries", count: deliveries.length },
          ]}
        />
      </div>

      {/* Filter banner — only when scoped to a subtask */}
      {tab !== "deliveries" && selectedSubtaskId && (
        <div className="flex h-row-dense items-center gap-2 border-b border-accent/40 bg-accent/10 px-3 text-2xs">
          <span className="uppercase tracking-wider text-accent">Filtered</span>
          <span className="text-fg">{selectedSubtaskId}</span>
          <span className="text-tertiary">
            {sorted.length} of {artifacts.length}
          </span>
          {onClearSubtaskFilter && (
            <button
              type="button"
              onClick={onClearSubtaskFilter}
              aria-label="Clear part filter"
              className="ml-auto flex items-center gap-1 rounded px-1.5 py-0.5 text-3xs uppercase tracking-wider text-tertiary hover:bg-surface-elevated/40 hover:text-fg-muted"
            >
              <XCircle className="h-3 w-3" />
              Clear
            </button>
          )}
        </div>
      )}

      {/* Toolbar — sort/view apply to the artifact tabs, not the deliveries list */}
      {tab !== "deliveries" && (
      <div className="flex h-row-dense flex-wrap items-center gap-2 border-b border-border px-3">
        <span className="text-2xs uppercase tracking-wider text-tertiary">
          Documents ({sorted.length}
          {selectedSubtaskId && sorted.length !== byAudience.length
            ? ` / ${byAudience.length}`
            : ""}
          )
        </span>

        <div className="ml-2 flex items-center gap-1">
          <span className="text-3xs text-tertiary">Sort</span>
          {(["date", "type", "name"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setSortKey(k)}
              className={cn(
                "rounded px-1.5 py-0.5 text-3xs uppercase tracking-wider",
                sortKey === k
                  ? "bg-accent text-inverse"
                  : "text-tertiary hover:text-fg-muted",
              )}
            >
              {k}
            </button>
          ))}
        </div>

        <div className="ml-auto flex items-center gap-1">
          {(["list", "timeline"] as const).map((m) => (
            <button
              key={m}
              onClick={() => setViewMode(m)}
              className={cn(
                "rounded px-1.5 py-0.5 text-3xs uppercase tracking-wider",
                viewMode === m
                  ? "bg-accent text-inverse"
                  : "text-tertiary hover:text-fg-muted",
              )}
            >
              {m}
            </button>
          ))}
        </div>
      </div>
      )}

      {/* Body */}
      <div className="flex-1 overflow-y-auto">
        {tab === "deliveries" ? (
          deliveries.length === 0 ? (
            <div className="flex h-full items-center justify-center text-xs text-tertiary">
              No deliveries yet
            </div>
          ) : (
            <DeliveriesPanel deliveries={deliveries} />
          )
        ) : sorted.length === 0 ? (
          <div className="flex h-full items-center justify-center text-xs text-tertiary">
            {selectedSubtaskId
              ? `No artifacts produced by ${selectedSubtaskId}`
              : tab === "internal"
                ? "No internal artifacts"
                : "No user-facing artifacts"}
          </div>
        ) : viewMode === "list" ? (
          <ul className="divide-y divide-border/40">
            {sorted.map((a) => {
              const aid = a.id ?? a.name;
              const rowDeliveries = deliveriesByArtifact.get(aid) ?? [];
              return (
                <ArtifactRow
                  key={a.name}
                  artifact={a}
                  versions={a.versions}
                  onOpen={() => setSelected(a)}
                  onHandover={
                    a.source === "brain" ? () => openHandover(a) : undefined
                  }
                  handingOver={handingOver === (a.id ?? a.name)}
                  deliveries={rowDeliveries}
                  expanded={expanded === aid}
                  onToggleExpand={() =>
                    setExpanded(expanded === aid ? null : aid)
                  }
                />
              );
            })}
          </ul>
        ) : (
          <div className="px-3 py-2">
            {grouped.map(([day, items]) => (
              <div key={day} className="mb-3 last:mb-0">
                <div className="mb-1 text-3xs uppercase tracking-wider text-tertiary">
                  {formatDayLabel(day)}
                </div>
                <ol>
                  {items.map((a, i) => (
                    <TimelineArtifactItem
                      key={a.name}
                      artifact={a}
                      versions={a.versions}
                      isLast={i === items.length - 1}
                      onOpen={() => setSelected(a)}
                    />
                  ))}
                </ol>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Viewer overlay */}
      {selected && (
        <ArtifactOverlay
          taskId={taskId}
          artifact={selected}
          onClose={() => setSelected(null)}
        />
      )}

      {/* Hand-over: send a brain artifact's body to another tool. Mounted only
          when invoked so the panel doesn't require a Router until then. */}
      {handoverContent && (
        <HandoverDialog
          open
          onOpenChange={(o) => !o && setHandoverContent(null)}
          content={handoverContent}
        />
      )}
    </div>
  );
});

function ArtifactRow({
  artifact,
  versions = 1,
  onOpen,
  onHandover,
  handingOver = false,
  deliveries = [],
  expanded = false,
  onToggleExpand,
}: {
  artifact: ArtifactInfo;
  versions?: number;
  onOpen: () => void;
  /** Present only for brain artifacts — opens the shared handover dialog. */
  onHandover?: () => void;
  handingOver?: boolean;
  deliveries?: DeliveryInfo[];
  expanded?: boolean;
  onToggleExpand?: () => void;
}) {
  const ext = displayExt(artifact);
  const successful = deliveries.filter((d) => d.success).length;
  const aud = audienceOf(artifact);
  return (
    <li>
      <div className="flex w-full items-center gap-2 px-3 py-1.5 transition-colors hover:bg-surface-elevated">
        <button
          onClick={onOpen}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <span
            className={cn(
              "shrink-0 rounded px-1 py-0.5 text-3xs font-bold uppercase tracking-wider text-inverse",
              extColor(ext),
            )}
          >
            {ext || "?"}
          </span>
          <span className="min-w-0 flex-1 truncate text-xs text-fg">
            {artifact.title || artifact.name}
          </span>
          {aud !== "user" && (
            <span
              className="shrink-0 rounded border border-border px-1 py-0.5 text-3xs uppercase tracking-wider text-tertiary"
              title={`audience: ${aud}`}
            >
              {aud}
            </span>
          )}
          {versions > 1 && (
            <span
              className="shrink-0 rounded border border-border px-1 py-0.5 text-3xs uppercase tracking-wider text-tertiary"
              title={`${versions} review rounds — showing the latest`}
            >
              {versions} versions
            </span>
          )}
          <span className="shrink-0 text-3xs text-tertiary">
            {formatBytes(artifact.size_bytes)}
          </span>
          <span className="shrink-0 text-3xs text-tertiary">
            {formatRelTime(artifact.modified_at)}
          </span>
        </button>
        {onHandover && (
          <button
            type="button"
            onClick={onHandover}
            disabled={handingOver}
            aria-label="Hand over to another tool"
            title="Hand over → another tool"
            className="flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-3xs uppercase tracking-wider text-accent hover:bg-accent/10 disabled:opacity-50"
          >
            <Share2 className="h-3 w-3" />
            <span>{handingOver ? "…" : "Hand over"}</span>
          </button>
        )}
        {deliveries.length > 0 && (
          <button
            type="button"
            onClick={onToggleExpand}
            aria-label={`Toggle ${deliveries.length} deliveries`}
            className={cn(
              "flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-3xs uppercase tracking-wider",
              expanded
                ? "bg-accent text-inverse"
                : "text-accent hover:bg-accent/10",
            )}
            title={`${deliveries.length} deliveries (${successful} successful)`}
          >
            <Send className="h-3 w-3" />
            <span>{deliveries.length}</span>
          </button>
        )}
      </div>
      {expanded && deliveries.length > 0 && (
        <DeliveriesPanel deliveries={deliveries} />
      )}
    </li>
  );
}


function DeliveriesPanel({ deliveries }: { deliveries: DeliveryInfo[] }) {
  return (
    <ul className="border-t border-border/40 bg-surface-elevated/40">
      {deliveries.map((d) => (
        <li
          key={d.id}
          className="flex items-center gap-2 px-6 py-1 text-3xs"
        >
          <span
            className={cn(
              "shrink-0 rounded px-1 py-0.5 uppercase tracking-wider",
              d.success ? "bg-accent/20 text-accent" : "bg-error/20 text-error",
            )}
          >
            {d.channel}
          </span>
          <span className="min-w-0 flex-1 truncate text-fg-muted">
            {d.title || d.artifact_id.slice(0, 8)}
            {d.person_id && (
              <>
                {" "}
                <span className="text-tertiary">
                  → {d.person_id.slice(0, 8)}
                </span>
              </>
            )}
          </span>
          {d.duration_ms != null && (
            <span className="shrink-0 text-tertiary">{d.duration_ms}ms</span>
          )}
          <span className="shrink-0 text-tertiary">
            {formatRelTime(d.created_at ?? undefined)}
          </span>
          {d.success && (
            <a
              className="shrink-0 rounded px-1.5 py-0.5 uppercase tracking-wider text-accent hover:bg-accent/10"
              href={`/api/deliveries/${encodeURIComponent(d.id)}`}
              target="_blank"
              rel="noreferrer noopener"
            >
              open
            </a>
          )}
          {!d.success && d.error && (
            <span className="shrink-0 truncate text-error" title={d.error}>
              error
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

function TimelineArtifactItem({
  artifact,
  versions = 1,
  isLast,
  onOpen,
}: {
  artifact: ArtifactInfo;
  versions?: number;
  isLast: boolean;
  onOpen: () => void;
}) {
  const ext = displayExt(artifact);
  return (
    <li className={cn("relative pl-5", !isLast && "pb-2")}>
      {!isLast && (
        <span
          aria-hidden="true"
          className="absolute left-[4px] top-2 bottom-0 w-px bg-border/60"
        />
      )}
      <span
        aria-hidden="true"
        className={cn(
          "absolute left-0 top-1.5 h-2 w-2 rounded-full ring-2 ring-surface",
          extColor(ext),
        )}
      />
      <button
        onClick={onOpen}
        className="flex w-full items-baseline gap-2 text-left"
      >
        <span className="truncate text-xs text-fg hover:text-accent">
          {artifact.title || artifact.name}
        </span>
        {versions > 1 && (
          <span
            className="shrink-0 rounded border border-border px-1 text-3xs uppercase tracking-wider text-tertiary"
            title={`${versions} review rounds — showing the latest`}
          >
            {versions} versions
          </span>
        )}
        <span className="shrink-0 text-3xs uppercase tracking-wider text-tertiary">
          {ext}
        </span>
        <span className="ml-auto shrink-0 text-3xs text-tertiary">
          {formatRelTime(artifact.modified_at)}
        </span>
      </button>
    </li>
  );
}

type ArtifactKind =
  | "markdown"
  | "html"
  | "image"
  | "video"
  | "audio"
  | "pdf"
  | "text"
  | "binary";

function artifactKindForArtifact(a: ArtifactInfo): ArtifactKind {
  // Brain artifacts route on media_type / kind — name is a UUID with no ext.
  if (a.source === "brain") {
    const mt = a.media_type ?? "";
    if (mt.includes("markdown") || a.kind === "report" || a.kind === "plan")
      return "markdown";
    if (mt.includes("html")) return "html";
    if (mt.startsWith("image/")) return "image";
    if (mt.startsWith("video/")) return "video";
    if (mt.startsWith("audio/")) return "audio";
    if (mt === "application/pdf") return "pdf";
    if (mt.startsWith("text/")) return "text";
    return "text"; // safest default for brain rows
  }
  return artifactKind(a.name);
}

function artifactKind(name: string): ArtifactKind {
  const ext = extOf(name);
  if (ext === "md" || ext === "markdown") return "markdown";
  if (ext === "html" || ext === "htm") return "html";
  if (["png", "jpg", "jpeg", "gif", "webp", "avif", "svg", "bmp", "ico"].includes(ext))
    return "image";
  if (["mp4", "webm", "mov", "ogv", "m4v"].includes(ext)) return "video";
  if (["mp3", "wav", "ogg", "m4a", "flac", "aac"].includes(ext)) return "audio";
  if (ext === "pdf") return "pdf";
  if (
    [
      "txt", "log", "json", "jsonl", "yaml", "yml", "toml", "ini", "csv", "tsv",
      "py", "ts", "tsx", "js", "jsx", "css", "scss", "xml", "sh",
      "rs", "go", "java", "rb", "c", "cpp", "h", "hpp", "sql", "env",
    ].includes(ext)
  )
    return "text";
  return "binary";
}

/**
 * "Redline" — open this html artifact for commenting and go to the viewer.
 *
 * The artifact is the DOCUMENT and its supersede chain is the version history,
 * so opening it once gives the switcher every version there has ever been —
 * each one still holding the bytes its own comments were placed on.
 */
function RedlineButton({ artifactId }: { artifactId: string }) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      disabled={busy}
      title="Comment on this artifact, anchored to its elements"
      onClick={async () => {
        setBusy(true);
        try {
          const opened = await redlineApi.open("artifact", artifactId);
          window.location.href = opened.web;
        } catch (err) {
          toast.error(
            err instanceof Error ? err.message : "could not open this artifact",
          );
          setBusy(false);
        }
      }}
      className="rounded px-1.5 py-0.5 text-3xs uppercase tracking-wider text-tertiary hover:text-fg-muted disabled:opacity-50"
    >
      {busy ? "opening…" : "redline"}
    </button>
  );
}

function ArtifactOverlay({
  taskId,
  artifact,
  onClose,
}: {
  taskId: string;
  artifact: ArtifactInfo;
  onClose: () => void;
}) {
  const kind = artifactKindForArtifact(artifact);
  // Brain artifacts route on UUID id; disk artifacts route on filename.
  const fetchKey = artifact.source === "brain" && artifact.id
    ? artifact.id
    : artifact.name;
  const artifactUrl = `/api/tasks/${taskId}/artifacts/${encodeURIComponent(fetchKey)}`;
  // Inside pywebview (WKWebView / WebKitGTK) the HTML5 `download` attribute is
  // ignored — a `<a href="blob:" download>` click is a silent no-op, so the
  // browser blob path can't save a file. The desktop shell instead routes the
  // bearer-gated URL (with ?token=) to the OS browser via openResult, exactly
  // like every other "open this file" affordance. Detect the bridge the same
  // way the window chrome does.
  const isPywebview = Boolean(window.pywebview?.api?.open_external);
  const needsText = kind === "markdown" || kind === "text" || kind === "html";
  // `binary` joins the blob path too: a raw `<a href="/api/...">` download
  // carries no Authorization header and 401s on the bearer-gated route, so in
  // a real browser every non-media binary (xlsx, zip, docx, …) is fetched
  // authed and handed to `<a download>` as a `blob:` object URL — same
  // mechanism the media kinds use. In pywebview we skip the eager blob fetch
  // and hand the tokenized URL to the OS browser on click instead.
  const needsBinaryBlob = kind === "binary" && !isPywebview;
  const needsBlob =
    kind === "image" ||
    kind === "video" ||
    kind === "audio" ||
    kind === "pdf" ||
    needsBinaryBlob;

  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(needsText);
  const [htmlView, setHtmlView] = useState<"rendered" | "source">("rendered");
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [blobLoading, setBlobLoading] = useState(needsBlob);
  const [blobError, setBlobError] = useState<string | null>(null);
  const visual = useVisualAdaptations();

  useEffect(() => {
    if (!needsText) return;
    let alive = true;
    setLoading(true);
    taskApi
      .getArtifact(taskId, fetchKey)
      .then((text) => {
        if (alive) setContent(text);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [taskId, fetchKey, needsText]);

  // Binary artifacts (images/video/audio/pdf) live under bearer-authed
  // /api/* so `<img src>`-style loads 401. Fetch as a blob and expose a
  // `blob:` object URL that native tags can consume without auth. Own
  // the URL lifetime here — revoke on unmount / artifact change.
  useEffect(() => {
    if (!needsBlob) return;
    let alive = true;
    let createdUrl: string | null = null;
    setBlobLoading(true);
    setBlobError(null);
    setBlobUrl(null);
    taskApi
      .getArtifactBlob(taskId, fetchKey)
      .then((blob) => {
        if (!alive) return;
        createdUrl = URL.createObjectURL(blob);
        setBlobUrl(createdUrl);
      })
      .catch((e) => {
        if (alive) setBlobError(e instanceof Error ? e.message : "Failed to load");
      })
      .finally(() => {
        if (alive) setBlobLoading(false);
      });
    return () => {
      alive = false;
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [taskId, fetchKey, needsBlob]);

  // Esc closes. Capture at document level so the overlay wins even when
  // focus sits on the markdown body.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Whitespace preference → padding pair (outer panel / reading column).
  const paddingClass =
    visual.whitespace === "spacious"
      ? "px-10 py-9 md:px-16 md:py-12"
      : visual.whitespace === "compact"
        ? "px-6 py-5 md:px-8 md:py-6"
        : "px-8 py-7 md:px-12 md:py-9";

  return (
    <div
      className="fixed inset-0 z-50 flex cursor-pointer items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={`Viewing ${artifact.name}`}
    >
      <div
        className="flex max-h-[94vh] w-[min(92vw,900px)] flex-col overflow-hidden rounded-lg border border-border bg-surface-elevated shadow-2xl"
        onClick={(e) => e.stopPropagation()}
        style={{ "--md-scale": visual.font_scale } as React.CSSProperties}
      >
        {/* Header — fixed at the top of the reader so scroll keeps the
            close control + filename in view. */}
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border bg-surface-elevated/95 px-6 py-3 backdrop-blur">
          <div className="min-w-0 flex-1">
            <div className="truncate text-xs uppercase tracking-wider text-tertiary">
              Artifact
            </div>
            <div className="truncate text-base font-semibold text-fg">
              {artifact.title || artifact.name}
            </div>
          </div>
          {kind === "html" && (
            <div className="flex shrink-0 items-center gap-1">
              {(["rendered", "source"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setHtmlView(m)}
                  className={cn(
                    "rounded px-1.5 py-0.5 text-3xs uppercase tracking-wider",
                    htmlView === m
                      ? "bg-accent text-inverse"
                      : "text-tertiary hover:text-fg-muted",
                  )}
                >
                  {m}
                </button>
              ))}
              {/* Only a BRAIN artifact can be redlined: the document is the
                  artifact row, and every link of its supersede chain becomes a
                  version. A disk file has no chain and no id to open by. */}
              {artifact.source === "brain" && artifact.id && (
                <RedlineButton artifactId={artifact.id} />
              )}
            </div>
          )}
          <button
            onClick={onClose}
            aria-label="Close viewer"
            title="Close (Esc)"
            className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-md border border-border bg-surface text-fg transition-colors hover:bg-surface-elevated"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Reading body */}
        <div className={cn("flex-1 overflow-y-auto", paddingClass)}>
          {needsBlob && blobLoading && (
            <div className="flex items-center justify-center py-16 text-sm text-tertiary">
              Loading…
            </div>
          )}
          {needsBlob && blobError && (
            <div className="flex items-center justify-center py-16 text-sm text-error">
              Failed to load: {blobError}
            </div>
          )}

          {kind === "image" && blobUrl && (
            <div className="flex items-center justify-center">
              <img
                src={blobUrl}
                alt={artifact.title || artifact.name}
                className="max-h-[78vh] max-w-full rounded-md object-contain"
              />
            </div>
          )}

          {kind === "video" && blobUrl && (
            <div className="flex items-center justify-center">
              <video
                src={blobUrl}
                controls
                playsInline
                className="max-h-[78vh] max-w-full rounded-md bg-black"
              />
            </div>
          )}

          {kind === "audio" && blobUrl && (
            <div className="mx-auto max-w-[60ch] py-8">
              <audio src={blobUrl} controls className="w-full" />
            </div>
          )}

          {kind === "pdf" && blobUrl && (
            <iframe
              src={blobUrl}
              title={artifact.name}
              className="h-[80vh] w-full rounded-md border border-border bg-white"
            />
          )}

          {kind === "binary" && (
            <div className="mx-auto max-w-[60ch] py-10 text-center">
              <p className="mb-3 text-sm text-tertiary">
                Preview not available for this file type.
              </p>
              {isPywebview ? (
                // WKWebView/WebKitGTK ignore `<a download>`; hand the tokenized
                // URL to the OS browser, which saves the file (filename comes
                // from the URL path). openResult appends ?token= + open_external.
                <button
                  type="button"
                  onClick={() => {
                    void previewApi.openResult(artifactUrl);
                  }}
                  className="inline-flex items-center rounded-md border border-border bg-surface px-3 py-1.5 text-sm text-fg transition-colors hover:bg-surface-elevated"
                >
                  Download {artifact.name}
                </button>
              ) : blobUrl ? (
                <a
                  href={blobUrl}
                  download={artifact.name}
                  className="inline-flex items-center rounded-md border border-border bg-surface px-3 py-1.5 text-sm text-fg transition-colors hover:bg-surface-elevated"
                >
                  Download {artifact.name}
                </a>
              ) : (
                <span className="text-sm text-tertiary">Preparing…</span>
              )}
            </div>
          )}

          {needsText && loading && (
            <div className="text-sm text-tertiary">Loading…</div>
          )}
          {needsText && !loading && content !== null && (
            kind === "html" && htmlView === "rendered" ? (
              // Empty `sandbox` = max isolation: no scripts, no forms, no
              // same-origin. Agent-produced HTML is untrusted. Scripts can
              // be re-enabled per-artifact later if the viewer grows a
              // "trust" toggle. srcDoc is used instead of src= because
              // /api/tasks/.../artifacts/ is bearer-auth-gated and iframes
              // don't send Authorization headers — content is fetched via
              // the authed taskApi.getArtifact path above. Trade-off:
              // relative sub-resources (./style.css, ./img.png) can't
              // resolve from an about:srcdoc origin, so rendering is
              // best-suited to self-contained HTML reports.
              <iframe
                srcDoc={content}
                title={artifact.name}
                sandbox=""
                className="h-[80vh] w-full rounded-md border border-border bg-white"
              />
            ) : (
              <div className="mx-auto max-w-[72ch]">
                {kind === "markdown" ? (
                  <MarkdownContent variant="viewer">{content}</MarkdownContent>
                ) : (
                  <pre
                    className="whitespace-pre-wrap break-words font-mono leading-relaxed text-fg"
                    style={{
                      // An engine name, not a bare rem: `0.9375rem` was authored
                      // against a 16px root and renders 7.5px on the app's 50 %
                      // one, and a literal cannot follow the rung. Same class as
                      // the two sites in `detail-modal.tsx`.
                      fontSize: "calc(var(--font-size-sm, 1.75rem) * var(--md-scale, 1))",
                    }}
                  >
                    {content}
                  </pre>
                )}
              </div>
            )
          )}
        </div>
      </div>
    </div>
  );
}
