// <!-- AGENT_HEADER
// role: code
// purpose: okuro assets — the unified MEDIA grid (bucket migration 080). Shows
//   every asset kind (image · video · audio · icon · illustration) from
//   /api/assets/media with kind + source + tag + text filters, a tile grid that
//   renders each kind appropriately, and an inspector to view/tag/download one.
//   The visible face of "assets is the single file-bucket". The detail popup
//   itself lives in media-lightbox.tsx — shared with the Studio library so both
//   surfaces open the SAME inspector.
// index: Tile | MediaGrid
// AGENT_HEADER_END -->
import { ArrowLeft, Image as ImageIcon, Music } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { KIND_META, MediaLightbox } from "@/components/assets/media-lightbox";
import { mediaApi, type MediaAsset, type MediaKind } from "@/lib/assets-api";

const KINDS: MediaKind[] = ["image", "video", "audio", "icon", "illustration"];

/** Render one asset by kind — image/icon/illustration inline, video as a muted
 *  preview, audio as a labelled tile. */
function Tile({ a, onOpen }: { a: MediaAsset; onOpen: () => void }) {
  const url = mediaApi.fileUrl(a.id);
  const Icon = KIND_META[a.kind]?.icon ?? ImageIcon;
  return (
    <button
      onClick={onOpen}
      className="group flex flex-col overflow-hidden rounded-lg border border-border text-left transition-colors hover:border-accent/60"
      title={a.title || a.id}
    >
      <div className="flex aspect-square items-center justify-center bg-black/10">
        {a.kind === "audio" ? (
          <Music className="h-8 w-8 text-tertiary" />
        ) : a.kind === "video" ? (
          <video src={url} className="h-full w-full object-cover" muted playsInline preload="metadata" />
        ) : (
          <img src={url} alt={a.title || ""} loading="lazy" className={a.kind === "icon" ? "h-2/3 w-2/3 object-contain" : "h-full w-full object-contain p-1"} />
        )}
      </div>
      <div className="flex items-center gap-1 border-t border-border/60 px-2 py-1">
        <Icon className="h-3 w-3 shrink-0 text-tertiary" />
        <span className="truncate text-[11px] text-fg">{a.title || a.id}</span>
      </div>
    </button>
  );
}

/** The unified media bucket grid. `onBack` returns to the icon browser. */
export function MediaGrid({ onBack }: { onBack: () => void }) {
  const [items, setItems] = useState<MediaAsset[]>([]);
  const [kinds, setKinds] = useState<Record<string, number>>({});
  const [tagVocab, setTagVocab] = useState<{ name: string; count?: number }[]>([]);
  const [kind, setKind] = useState<string>("");
  const [studioOnly, setStudioOnly] = useState(false);
  const [tag, setTag] = useState<string>("");
  const [q, setQ] = useState("");
  const [selIdx, setSelIdx] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  // Patch one asset's tags in place (no refetch) so the open lightbox's index
  // stays valid while tags are edited from its rail.
  const patchTags = useCallback((id: string, tags: string[]) => {
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, tags } : it)));
  }, []);

  // Drop a deleted asset from the grid + decrement its kind count locally; close
  // the lightbox if it was the last item, else the next one slides into view.
  const handleDeleted = useCallback((id: string) => {
    setItems((prev) => {
      const gone = prev.find((it) => it.id === id);
      const next = prev.filter((it) => it.id !== id);
      setSelIdx((cur) => (cur === null ? null : cur >= next.length ? null : cur));
      if (gone) setKinds((k) => ({ ...k, [gone.kind]: Math.max(0, (k[gone.kind] || 1) - 1) }));
      return next;
    });
  }, []);

  const load = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const res = await mediaApi.list({ kind: kind || undefined, source: studioOnly ? "studio" : undefined, tag: tag || undefined, q: q || undefined, limit: 120 });
      setItems(res.items); setKinds(res.kinds || {});
    } catch (e) { setErr(String(e)); } finally { setLoading(false); }
  }, [kind, studioOnly, tag, q]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { mediaApi.tags(kind || undefined).then((r) => setTagVocab((r.tags || []).filter((t) => t?.name))).catch(() => {}); }, [kind]);

  const total = Object.values(kinds).reduce((a, b) => a + b, 0);
  const chip = (on: boolean) => `rounded-md border px-2 py-1 text-xs transition-colors ${on ? "border-accent bg-accent/10 text-accent" : "border-border text-fg hover:border-accent/50"}`;

  return (
    <div className="flex h-full flex-col">
      {/* header + Icons/Media toggle */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2">
        <button onClick={onBack} className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-tertiary hover:text-fg">
          <ArrowLeft className="h-3.5 w-3.5" /> Icons
        </button>
        <span className="text-sm font-semibold text-fg">Assets · Media</span>
        <span className="text-xs text-tertiary">{total} in the bucket</span>
        <div className="ml-auto flex items-center gap-1.5">
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="search…" className="w-40 rounded-md border border-border bg-transparent px-2 py-1 text-sm text-fg placeholder:text-tertiary focus:outline-none" />
          <button onClick={() => setStudioOnly((v) => !v)} className={chip(studioOnly)}>Studio</button>
        </div>
      </div>

      {/* kind chips */}
      <div className="flex flex-wrap items-center gap-1.5 border-b border-border px-4 py-2">
        <button onClick={() => setKind("")} className={chip(kind === "")}>All · {total}</button>
        {KINDS.map((k) => {
          const Icon = KIND_META[k].icon;
          const n = kinds[k] ?? 0;
          return (
            <button key={k} onClick={() => setKind(kind === k ? "" : k)} className={chip(kind === k)} disabled={!n && kind !== k}>
              <span className="inline-flex items-center gap-1"><Icon className="h-3 w-3" /> {KIND_META[k].label} · {n}</span>
            </button>
          );
        })}
        {tag && <button onClick={() => setTag("")} className={chip(true)}>#{tag} ×</button>}
      </div>

      <div className="flex min-h-0 flex-1">
        <div className="min-w-0 flex-1 overflow-y-auto p-4">
          {err ? (
            <p className="text-sm text-[var(--color-status-error,#f92f77)]">{err}</p>
          ) : loading ? (
            <p className="text-sm text-tertiary">Loading…</p>
          ) : items.length === 0 ? (
            <div className="flex flex-1 items-center justify-center rounded-xl border border-border py-16 text-sm text-tertiary">
              Nothing here yet — Studio generations and uploads land in this bucket.
            </div>
          ) : (
            <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(120px, 1fr))" }}>
              {items.map((a, i) => <Tile key={a.id} a={a} onOpen={() => setSelIdx(i)} />)}
            </div>
          )}
          {/* tag vocabulary */}
          {tagVocab.length > 0 && (
            <div className="mt-4 flex flex-wrap gap-1 border-t border-border pt-3">
              <span className="mr-1 text-[11px] uppercase tracking-wide text-tertiary">tags</span>
              {tagVocab.slice(0, 40).map((t) => (
                <button key={t.name} onClick={() => setTag(tag === t.name ? "" : t.name)} className={`rounded border px-1.5 py-0.5 text-[11px] ${tag === t.name ? "border-accent text-accent" : "border-border text-tertiary hover:text-fg"}`}>
                  {t.name}{t.count ? ` ${t.count}` : ""}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
      {selIdx !== null && items[selIdx] && (
        <MediaLightbox
          items={items}
          index={selIdx}
          onClose={() => setSelIdx(null)}
          onIndex={setSelIdx}
          onPatchTags={patchTags}
          onDelete={handleDeleted}
        />
      )}
    </div>
  );
}
