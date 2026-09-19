// <!-- AGENT_HEADER
// role: code
// purpose: /assets/icons — pro icon manager: sidebar+set CRUD, fg grid, multi-select, inspector (favorite/tags), search/browse, paging.
// index: AssetsPage
// AGENT_HEADER_END -->
import { useEffect, useMemo, useRef, useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Search, Copy, Check, Star, LayoutGrid, Boxes, X, Plus, Pencil, Trash2, Upload, Download } from "lucide-react";
import { useNavigate } from "react-router";
import { leafSlugs, pathFor } from "@/shell/routes";
import { useSectionTitle } from "@/shell/components/PageTitle";
import { useInView } from "@/hooks/use-in-view";
import { SectionLabel } from "@/components/ui/section-label";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { iconsApi, type IconResult, type IconSet } from "@/lib/assets-api";
import { toCurrentColor, combineSvgs } from "@/lib/icon-color";
import { SidePanel, MobilePanelTrigger } from "@/components/ui/side-panel";

const PERMISSIVE = new Set(["lucide", "tabler", "tabler-outline", "tabler-filled"]);
const licenseOf = (sets: string[]) =>
  sets.some((s) => PERMISSIVE.has(s.toLowerCase())) ? "MIT" : "LICENSED";

type Filter =
  | { kind: "all" }
  | { kind: "favorites" }
  | { kind: "set"; value: string; label: string }
  | { kind: "tag"; value: string; label: string };

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

function IconCard({
  icon, inspected, selected, iconPx, scrollRoot, onSelect, onToggleFav, onToggleSelect,
}: {
  icon: IconResult; inspected: boolean; selected: boolean; iconPx: number; scrollRoot: Element | null;
  onSelect: (e: React.MouseEvent) => void; onToggleFav: () => void; onToggleSelect: () => void;
}) {
  const [copied, setCopied] = useState(false);
  // Lazy: only parse/inject the SVG once the card scrolls into view (keeps a
  // 35k-icon grid responsive). `once` keeps it mounted after the first reveal.
  // root MUST be the inner scroll container, not the viewport.
  const [boxRef, inView] = useInView<HTMLDivElement>({ once: true, root: scrollRoot });
  const tinted = useMemo(() => (inView ? toCurrentColor(icon.svg) : ""), [inView, icon.svg]);
  const copy = (e: React.MouseEvent) => {
    e.stopPropagation();
    navigator.clipboard.writeText(icon.svg ?? icon.kebab_name).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    });
  };
  return (
    <button onClick={onSelect} title={`${icon.name}\n${icon.sets.join(", ")}`}
      className={
        "group relative flex select-none flex-col items-center gap-2 rounded-md border bg-surface p-3 text-fg transition-colors hover:bg-surface-subtle " +
        (inspected ? "border-accent" : selected ? "border-accent/40" : "border-border hover:border-accent")
      }>
      <span onClick={(e) => { e.stopPropagation(); onToggleSelect(); }}
        className={"absolute left-1 top-1 transition-opacity " + (selected ? "opacity-100 text-accent" : "text-fg-muted opacity-0 group-hover:opacity-100")}>
        {selected ? <Check className="h-3 w-3" /> : <span className="block h-3 w-3 rounded-[3px] border border-current" />}
      </span>
      <span onClick={(e) => { e.stopPropagation(); onToggleFav(); }}
        className={"absolute right-5 top-1 transition-opacity " + (icon.favorite ? "text-accent opacity-100" : "text-fg-muted opacity-0 group-hover:opacity-100")}>
        <Star className="h-3 w-3" fill={icon.favorite ? "currentColor" : "none"} />
      </span>
      <span onClick={copy} className="absolute right-1 top-1 text-fg-muted opacity-0 transition-opacity group-hover:opacity-100">
        {copied ? <Check className="h-3 w-3 text-accent" /> : <Copy className="h-3 w-3" />}
      </span>
      <div ref={boxRef} style={{ width: iconPx, height: iconPx }}
        className="flex items-center justify-center text-fg [&>svg]:h-full [&>svg]:w-full"
        dangerouslySetInnerHTML={{ __html: tinted }} />
      <span className="line-clamp-1 max-w-full text-2xs text-fg-muted" title={icon.kebab_name}>{icon.kebab_name}</span>
    </button>
  );
}

function SidebarRow({ active, onClick, icon, label, count, indent, actions }: {
  active: boolean; onClick: () => void; icon?: React.ReactNode; label: string; count?: number; indent?: boolean; actions?: React.ReactNode;
}) {
  return (
    <div className={"group/row flex items-center " + (indent ? "pl-4" : "")}>
      <button onClick={onClick}
        className={"flex flex-1 items-center gap-2 rounded px-2 py-1 text-left text-xs transition-colors " +
          (active ? "bg-accent-subtle text-accent" : "text-fg-muted hover:bg-surface-subtle hover:text-fg")}>
        {icon}
        <span className="line-clamp-1 flex-1">{label}</span>
        {count != null ? <span className="text-2xs text-fg-muted">{count.toLocaleString()}</span> : null}
      </button>
      {actions ? <span className="opacity-0 transition-opacity group-hover/row:opacity-100">{actions}</span> : null}
    </div>
  );
}

function Inspector({ icon, onClose, onToggleFav, onTagsChanged, onDelete }: {
  icon: IconResult; onClose: () => void; onToggleFav: () => void;
  onTagsChanged: (tags: string[]) => void; onDelete: () => void;
}) {
  const [draft, setDraft] = useState("");
  const tinted = useMemo(() => toCurrentColor(icon.svg), [icon.svg]);
  const license = licenseOf(icon.sets);
  const download = () => {
    const url = URL.createObjectURL(new Blob([icon.svg ?? ""], { type: "image/svg+xml" }));
    const a = document.createElement("a");
    a.href = url; a.download = `${icon.kebab_name}.svg`; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const addTag = async () => {
    const name = draft.trim();
    if (!name) return;
    setDraft("");
    const r = await iconsApi.editTags(icon.id, { add: [name] });
    if (r.tags) onTagsChanged(r.tags);
  };
  const removeTag = async (name: string) => {
    const r = await iconsApi.editTags(icon.id, { remove: [name] });
    if (r.tags) onTagsChanged(r.tags);
  };
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start justify-between">
        <SectionLabel className="text-fg-muted">Icon</SectionLabel>
        <button onClick={onClose} className="text-fg-muted hover:text-fg"><X className="h-4 w-4" /></button>
      </div>
      <div className="flex items-center justify-center rounded-md border border-border bg-surface py-8 text-fg [&>svg]:h-16 [&>svg]:w-16"
        dangerouslySetInnerHTML={{ __html: tinted }} />
      <div>
        <div className="break-words text-sm text-fg">{icon.name}</div>
        <div className="mt-0.5 break-words text-2xs text-fg-muted">{icon.kebab_name}</div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" onClick={onToggleFav}>
          <Star className="mr-1 h-3.5 w-3.5" fill={icon.favorite ? "currentColor" : "none"} />
          {icon.favorite ? "Favorited" : "Favorite"}
        </Button>
        <Button variant="outline" size="sm" onClick={() => navigator.clipboard.writeText(icon.svg ?? "")}>
          <Copy className="mr-1 h-3.5 w-3.5" /> Copy SVG
        </Button>
        <Button variant="outline" size="sm" onClick={download}>
          <Download className="mr-1 h-3.5 w-3.5" /> Download
        </Button>
        <Button variant="outline" size="sm" onClick={onDelete}>
          <Trash2 className="mr-1 h-3.5 w-3.5" /> Delete
        </Button>
        <Badge variant="outline" className={license === "MIT" ? "" : "text-accent"}>{license}</Badge>
      </div>
      <div>
        <SectionLabel className="mb-1 text-fg-muted">Sets</SectionLabel>
        <div className="flex flex-wrap gap-1">{icon.sets.map((s) => <Badge key={s} variant="outline">{s}</Badge>)}</div>
      </div>
      <div>
        <SectionLabel className="mb-1 text-fg-muted">Tags</SectionLabel>
        <div className="mb-2 flex flex-wrap gap-1">
          {icon.tags.length === 0 ? <span className="text-2xs text-fg-muted">no tags</span> : null}
          {icon.tags.map((t) => (
            <span key={t} className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-2xs text-fg-muted">
              {t}<button onClick={() => removeTag(t)} className="hover:text-accent"><X className="h-2.5 w-2.5" /></button>
            </span>
          ))}
        </div>
        <div className="flex gap-1">
          <Input value={draft} onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") addTag(); }} placeholder="add tag…" className="h-7 text-xs" />
          <Button variant="outline" size="sm" onClick={addTag}><Plus className="h-3.5 w-3.5" /></Button>
        </div>
      </div>
    </div>
  );
}

/** Resolved from the IA by slug, never pinned (3417a932's mount rule). */
const MEDIA_LEAF = leafSlugs("deliver").indexOf("media");

export function AssetsPage() {
  const navigate = useNavigate();
  /**
   * R5 (372ccdb2), PERMANENT BRANCH — ONE MODAL, THREE ACTIONS, AND
   * `window.confirm` IS GONE FROM THIS LEAF.
   *
   * There were three `window.confirm` calls here — delete a set, delete the
   * selection, delete one icon. R5 rules a modal for the permanent branch and
   * reserves the two-step arm for what can be set back; all three of these
   * are permanent (`iconsApi.deleteIcons` and `containers.remove` have no
   * undo). `window.confirm` was the wrong instrument for a second reason the
   * STACKS pass measured: it BLOCKS the event loop, and the shell runs a
   * 650ms topic transition through it.
   *
   * ONE piece of state rather than three: a discriminated pending action, so
   * the three sentences live beside each other and cannot drift apart.
   */
  const [pending, setPending] = useState<
    | { kind: "set"; id: string; name: string }
    | { kind: "bulk"; count: number }
    | { kind: "one"; id: string }
    | null
  >(null);
  const qc = useQueryClient();
  // Open straight into the Media browser when linked as /assets?view=media (the
  // "MEDIA" nav) — otherwise the icon manager. Read once on mount.
  /**
   * D1 — ASSETS PAINTED THE **MEDIA LEAF** AT ITS OWN ADDRESS, and the state
   * that did it is gone.
   *
   * This was `useState(searchParams.get("view") === "media" ? "media" : "icons")`
   * with an early `return <MediaGrid …/>` further down and an in-page button
   * calling `setView("media")` — none of it touching the URL. Measured
   * consequences, all user-visible: the rail highlighted ASSETS while the
   * bucket was on screen, the address was not bookmarkable, browser Back did
   * not undo it, and the "Icons" button meant two different things depending
   * on how you arrived (a leaf change through the mount, local state here).
   *
   * Q-D8 ruled A: two leaves, two addresses, one page file. So this page has
   * exactly one job now, and the `?view=media` LINK still works — it is
   * intercepted before the LEGACY table and sent to `/deliver/media`
   * (`shell/routes.ts`), which is where the mount lives.
   *
   * THE GRAMMAR COLLISION THE MOUNT'S HEADER DESCRIBES DIES WITH IT: this file
   * no longer reads `?view=` at all, so the shell's own section slugs are the
   * only meaning that key has.
   */
  const [rawQuery, setRawQuery] = useState("");
  const [filter, setFilter] = useState<Filter>({ kind: "all" });
  const query = useDebounced(rawQuery.trim(), 250);

  const stats = useQuery({ queryKey: ["icon-stats"], queryFn: () => iconsApi.stats() });
  const sets = useQuery({ queryKey: ["icon-sets"], queryFn: () => iconsApi.sets() });
  const tags = useQuery({ queryKey: ["icon-tags-top"], queryFn: () => iconsApi.tags(undefined, 24) });

  const [items, setItems] = useState<IconResult[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [total, setTotal] = useState<number | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [anchor, setAnchor] = useState<number | null>(null); // range-select anchor index
  const [copiedToast, setCopiedToast] = useState<string | null>(null);
  const [size, setSize] = useState<"s" | "m" | "l">(
    () => (localStorage.getItem("assets-icon-size") as "s" | "m" | "l") || "m",
  );
  useEffect(() => localStorage.setItem("assets-icon-size", size), [size]);
  // tile = grid min column width; iconPx = rendered glyph box.
  const SIZES = { s: { tile: 76, icon: 28 }, m: { tile: 104, icon: 40 }, l: { tile: 148, icon: 64 } } as const;
  const dims = SIZES[size];
  // The grid scrolls inside an inner overflow container, NOT the viewport — the
  // observers must use it as their root or they never fire on scroll.
  const [scrollEl, setScrollEl] = useState<HTMLDivElement | null>(null);
  const [sentinelRef, sentinelInView] = useInView<HTMLDivElement>({ root: scrollEl });
  const [reloadKey, setReloadKey] = useState(0); // bump to refetch the current view
  const fileInputRef = useRef<HTMLInputElement>(null);

  const mode: "search" | "browse" = query ? "search" : "browse";
  const setParam = filter.kind === "set" ? filter.value : undefined;
  const tagParam = filter.kind === "tag" ? filter.value : undefined;

  useEffect(() => {
    let cancelled = false;
    setLoading(true); setItems([]); setCursor(null); setTotal(null);
    (async () => {
      try {
        if (query) {
          const r = await iconsApi.search(query, { set: setParam, tag: tagParam, limit: 80 });
          if (!cancelled) { setItems(r.results); setTotal(r.count); }
        } else {
          const r = await iconsApi.browse({ set: setParam, tag: tagParam, favorite: filter.kind === "favorites" || undefined, limit: 120 });
          if (!cancelled) { setItems(r.results); setCursor(r.next_cursor); }
        }
      } finally { if (!cancelled) setLoading(false); }
    })();
    return () => { cancelled = true; };
  }, [query, filter.kind, setParam, tagParam, reloadKey]);

  const loadMore = async () => {
    if (!cursor || loading) return;
    setLoading(true);
    try {
      const r = await iconsApi.browse({ set: setParam, tag: tagParam, favorite: filter.kind === "favorites" || undefined, cursor, limit: 120 });
      setItems((prev) => [...prev, ...r.results]); setCursor(r.next_cursor);
    } finally { setLoading(false); }
  };

  // Infinite scroll: auto-load the next page when the sentinel scrolls in.
  useEffect(() => {
    if (sentinelInView && mode === "browse" && cursor && !loading) void loadMore();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sentinelInView, cursor, loading, mode]);

  const patchItem = (id: string, patch: Partial<IconResult>) =>
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, ...patch } : it)));

  const toggleFav = async (icon: IconResult) => {
    const next = !icon.favorite;
    patchItem(icon.id, { favorite: next });
    if (filter.kind === "favorites" && !next) setItems((prev) => prev.filter((it) => it.id !== icon.id));
    await iconsApi.favorite([icon.id], next);
    qc.invalidateQueries({ queryKey: ["icon-stats"] });
  };

  const toggleSel = (id: string) =>
    setSel((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const clearSel = () => setSel(new Set());

  // Card click: plain = open inspector; ⇧ = range-select; ⌘/Ctrl = toggle-select.
  const onCardActivate = (icon: IconResult, index: number, e: React.MouseEvent) => {
    if (e.shiftKey && anchor != null) {
      window.getSelection()?.removeAllRanges(); // clear the incidental text highlight
      const [a, b] = [Math.min(anchor, index), Math.max(anchor, index)];
      const range = items.slice(a, b + 1).map((it) => it.id);
      setSel((prev) => { const n = new Set(prev); range.forEach((id) => n.add(id)); return n; });
    } else if (e.shiftKey || e.metaKey || e.ctrlKey) {
      toggleSel(icon.id);
      setAnchor(index);
    } else {
      setSelectedId(icon.id);
      setAnchor(index);
    }
  };

  // Copy the selected icons' SVGs (or the inspected one) — joined by a blank line.
  const copySelectedSvgs = async (): Promise<number> => {
    const ids = sel.size ? [...sel] : selectedId ? [selectedId] : [];
    if (!ids.length) return 0;
    const byId = new Map(items.map((it) => [it.id, it]));
    const svgs = ids.map((id) => byId.get(id)?.svg).filter(Boolean) as string[];
    if (!svgs.length) return 0;
    // One SVG → copy as-is; multiple → combine into a single SVG (tiled group)
    // so it pastes into Figma as one importable vector. For files on disk, use Export.
    const payload = svgs.length === 1 ? svgs[0]! : combineSvgs(svgs);
    await navigator.clipboard.writeText(payload);
    setCopiedToast(`Copied ${svgs.length} SVG${svgs.length > 1 ? "s (one group)" : ""}`);
    setTimeout(() => setCopiedToast(null), 1400);
    return svgs.length;
  };

  // ⌘/Ctrl+C copies SVG(s) when not editing text / not selecting page text.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.key === "c" && (e.metaKey || e.ctrlKey))) return;
      const el = document.activeElement;
      const inField = el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || (el as HTMLElement).isContentEditable);
      if (inField) return;                  // don't hijack copying from the search box
      if (!sel.size && !selectedId) return; // nothing picked → let the browser copy page text
      e.preventDefault();
      void copySelectedSvgs();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sel, selectedId, items]);

  const bulkFavorite = async () => {
    const ids = [...sel];
    ids.forEach((id) => patchItem(id, { favorite: true }));
    await iconsApi.favorite(ids, true);
    qc.invalidateQueries({ queryKey: ["icon-stats"] });
    clearSel();
  };
  const attachToSet = async (setId: string) => {
    await iconsApi.attach("set", setId, [...sel]);
    qc.invalidateQueries({ queryKey: ["icon-sets"] });
    clearSel();
  };

  const createSet = async () => {
    const name = window.prompt("New set name");
    if (!name?.trim()) return;
    await iconsApi.containers.create("set", name.trim());
    qc.invalidateQueries({ queryKey: ["icon-sets"] });
  };
  const renameSet = async (id: string, current: string) => {
    const name = window.prompt("Rename set", current);
    if (!name?.trim() || name === current) return;
    await iconsApi.containers.rename("set", id, name.trim());
    qc.invalidateQueries({ queryKey: ["icon-sets"] });
  };
  const deleteSet = async (id: string, name: string) => {
    await iconsApi.containers.remove("set", id);
    if (filter.kind === "set" && filter.value === name) setFilter({ kind: "all" });
    qc.invalidateQueries({ queryKey: ["icon-sets"] });
  };

  const downloadBlob = (blob: Blob, name: string) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  // D — import SVG files into the current set (or All Icons).
  const onImportFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    const items = await Promise.all(
      [...files].filter((f) => /\.svg$/i.test(f.name) || f.type.includes("svg"))
        .map(async (f) => ({ name: f.name.replace(/\.svg$/i, ""), svg: await f.text() })),
    );
    if (!items.length) return;
    const setId = filter.kind === "set" ? (sets.data?.sets ?? []).find((s) => s.name === filter.value)?.id : undefined;
    const r = await iconsApi.importSvgs(items, setId);
    if (r.error) { window.alert(`Import failed: ${r.error}`); return; }
    qc.invalidateQueries({ queryKey: ["icon-stats"] });
    qc.invalidateQueries({ queryKey: ["icon-sets"] });
    setReloadKey((k) => k + 1);
  };

  // F — delete selected icons.
  const bulkDelete = async () => {
    const ids = [...sel];
    await iconsApi.deleteIcons(ids);
    setItems((prev) => prev.filter((it) => !sel.has(it.id)));
    if (selectedId && sel.has(selectedId)) setSelectedId(null);
    clearSel();
    qc.invalidateQueries({ queryKey: ["icon-stats"] });
  };

  const deleteOne = async (id: string) => {
    await iconsApi.deleteIcons([id]);
    setItems((prev) => prev.filter((it) => it.id !== id));
    setSelectedId(null);
    qc.invalidateQueries({ queryKey: ["icon-stats"] });
  };

  // E — export selection as a zip.
  const bulkExport = async () => {
    const blob = await iconsApi.exportZip([...sel]);
    downloadBlob(blob, "okuro-icons.zip");
  };

  const tree = useMemo(() => {
    const all = (sets.data?.sets ?? []).filter((s) => s.icon_count > 0 || true);
    const byParent = new Map<string, IconSet[]>();
    for (const s of all) if (s.parent_set_id) byParent.set(s.parent_set_id, [...(byParent.get(s.parent_set_id) ?? []), s]);
    return all.filter((s) => !s.parent_set_id).sort((a, b) => b.icon_count - a.icon_count)
      .map((t) => ({ set: t, children: (byParent.get(t.id) ?? []).sort((a, b) => a.name.localeCompare(b.name)) }));
  }, [sets.data]);

  const selected = items.find((it) => it.id === selectedId) ?? null;
  const [libOpen, setLibOpen] = useState(false);
  const noLibrary = stats.data?.error || sets.data?.error;
  const activeLabel = filter.kind === "all" ? "All icons" : filter.kind === "favorites" ? "Favorites" : filter.label;
  const setOptions = (sets.data?.sets ?? []).filter((s) => !s.parent_set_id);

  /* THE SEARCH BOX IS THE PLATE'S. It is the leaf's one genuine
     search/sort/filter/add control; the icon-size and import controls sit
     with the grid they act on and stay. `c-band-pin` names this glyph for
     the band's centring rule rather than letting `.absolute` catch every
     positioned node the plate is handed (audit C5-1). */
  const header = useMemo(
    () => ({
      actions: (
        <div className="relative w-56">
          <Search className="c-band-pin pointer-events-none absolute left-3 text-fg-muted" />
          <Input
            value={rawQuery}
            onChange={(e) => setRawQuery(e.target.value)}
            placeholder="search icons…"
            aria-label="Search icons"
            className="pl-8"
          />
        </div>
      ),
    }),
    [rawQuery],
  );
  useSectionTitle(header);

  return (
    <div className="flex h-full">
      <SidePanel
        side="left"
        /* R3 (8546865f) — THE LIBRARY RAIL ASKS THE PANE, NOT THE WINDOW.
           `w-60` is 276px at the 8px root and it was fixed, so inside a 728px
           pane the rail took 37% and the icon grid got 462px; at 1900 the same
           276px is 22% of 1262. p3's Q2 recommended C — "the problem is
           measured as width, not as information architecture" — and the rung
           comes out of the grid rather than from taste: the tiles are
           `repeat(auto-fill, minmax(120px, 1fr))`, so a grid under ~500px
           shows three columns, and 500 + 276 = 776. `@4xl` is 896px here,
           which is the first rung that clears it with room.
           Below the rung the rail narrows rather than disappearing: its
           visibility is `SidePanel`'s `useIsMobile()` + `MobilePanelTrigger`'s
           `md:hidden`, a matched WINDOW pair, and re-keying one half of that
           is a one-way trip to no rail at all (the NOTES lesson, 477b0449). */
        desktopClassName="w-44 @4xl:w-60 shrink-0 overflow-y-auto border-r border-border px-3 py-4"
        open={libOpen}
        onOpenChange={setLibOpen}
        title="Library"
      >
        <SectionLabel className="mb-1 text-fg-muted">Library</SectionLabel>
        <SidebarRow active={filter.kind === "all"} onClick={() => setFilter({ kind: "all" })}
          icon={<LayoutGrid className="h-3.5 w-3.5" />} label="All Icons" count={stats.data?.icons_total} />
        <SidebarRow active={filter.kind === "favorites"} onClick={() => setFilter({ kind: "favorites" })}
          icon={<Star className="h-3.5 w-3.5" />} label="Favorites" count={stats.data?.icons_favorite} />

        <div className="mb-1 mt-4 flex items-center justify-between">
          <SectionLabel className="text-fg-muted">Sets</SectionLabel>
          <button onClick={createSet} title="New set" className="text-fg-muted hover:text-accent"><Plus className="h-3.5 w-3.5" /></button>
        </div>
        {tree.map(({ set, children }) => (
          <div key={set.id}>
            <SidebarRow active={filter.kind === "set" && filter.value === set.name}
              onClick={() => setFilter({ kind: "set", value: set.name, label: set.name })}
              icon={<Boxes className="h-3.5 w-3.5" />} label={set.name} count={set.icon_count}
              actions={
                <span className="flex gap-1 pr-1">
                  <button onClick={() => renameSet(set.id, set.name)} className="text-fg-muted hover:text-fg"><Pencil className="h-3 w-3" /></button>
                  <button onClick={() => setPending({ kind: "set", id: set.id, name: set.name })} aria-label={`Delete set ${set.name}`} className="text-fg-muted hover:text-error"><Trash2 className="h-3 w-3" /></button>
                </span>
              } />
            {children.map((c) => (
              <SidebarRow key={c.id} indent active={filter.kind === "set" && filter.value === c.name}
                onClick={() => setFilter({ kind: "set", value: c.name, label: c.name })}
                label={c.name} count={c.icon_count}
                actions={
                  <span className="flex gap-1 pr-1">
                    <button onClick={() => renameSet(c.id, c.name)} className="text-fg-muted hover:text-fg"><Pencil className="h-3 w-3" /></button>
                    <button onClick={() => setPending({ kind: "set", id: c.id, name: c.name })} aria-label={`Delete set ${c.name}`} className="text-fg-muted hover:text-error"><Trash2 className="h-3 w-3" /></button>
                  </span>
                } />
            ))}
          </div>
        ))}

        <SectionLabel className="mb-1 mt-4 text-fg-muted">Tags</SectionLabel>
        <div className="flex flex-wrap gap-1">
          {(tags.data?.tags ?? []).map((t) => (
            <button key={t.name} onClick={() => setFilter({ kind: "tag", value: t.name, label: `#${t.name}` })}
              className={"rounded border px-1.5 py-0.5 text-2xs transition-colors " +
                (filter.kind === "tag" && filter.value === t.name ? "border-accent text-accent" : "border-border text-fg-muted hover:border-border-hover hover:text-fg")}>
              {t.name}
            </button>
          ))}
        </div>
      </SidePanel>

      <div ref={setScrollEl} className="flex-1 overflow-y-auto px-4 py-6 md:px-8">
        <MobilePanelTrigger
          icon={<LayoutGrid className="h-3.5 w-3.5" />}
          label="Library"
          onClick={() => setLibOpen(true)}
          className="mb-3"
        />
        <div className="mb-3 flex gap-1">
          <button className="rounded-md border border-accent bg-accent-subtle px-2.5 py-1 text-xs text-accent">Icons</button>
          {/* NAVIGATES, rather than flipping local state — see D1 above. The
              index is resolved from the IA by slug, never pinned (3417a932). */}
          <button onClick={() => navigate(pathFor("deliver", MEDIA_LEAF < 0 ? 2 : MEDIA_LEAF))} className="rounded-md border border-border px-2.5 py-1 text-xs text-fg hover:border-accent hover:text-accent">Media</button>
        </div>
        {/* R1 (86b8f1f0) — THE LEAF TITLE IS THE SHELL'S. `TopicBar` renders
            `<h1 class="c-title">Assets</h1>` above this pane, so the page's own
            `ASSETS · ICONS` h1 is gone. The COUNTS survive as the first content
            line, because they are the only place the library's size is stated
            and R7 wants a true total said out loud. `text-fg-muted` rather than
            `PageHeader`'s `text-tertiary` (kit todo c581c9b2). */}
        <p className="type-small mb-4 text-fg-muted">
          {stats.data?.icons_total != null
            ? `${stats.data.icons_total.toLocaleString()} icons · ${stats.data.sets_total} sets · ${stats.data.vec_loaded ? "semantic + keyword" : "keyword only"}`
            : "icon library"}
        </p>

        {noLibrary ? (
          <EmptyState title="No icon library installed"
            description="Import a library pack to enable icon search: okuro assets icons import <pack.zip>" />
        ) : (
          <>
            {/* THE SEARCH BAR WAS HERE, `sticky top-0 z-20`, and that is audit
                C4-3: `.c-panes{z-index:0}` is a containment boundary, so a
                leaf's sticky header pins BEHIND the plate however high its own
                z-index climbs. It is published onto the plate now, which IS
                the sticky surface. BRAIN and KNOWLEDGE carried the same shape
                and are fixed the same way. */}

            {sel.size > 0 ? (
              <div className="mt-4 flex items-center gap-3 rounded-md border border-accent/40 bg-accent-subtle px-3 py-2 text-xs">
                <span className="text-accent">{sel.size} selected</span>
                <Button variant="outline" size="sm" onClick={bulkFavorite}>
                  <Star className="mr-1 h-3.5 w-3.5" /> Favorite
                </Button>
                <span className="flex items-center gap-1">
                  <span className="text-fg-muted">Add to set:</span>
                  <select onChange={(e) => { if (e.target.value) attachToSet(e.target.value); e.currentTarget.value = ""; }}
                    defaultValue="" className="rounded border border-border bg-surface px-1 py-0.5 text-xs text-fg">
                    <option value="" disabled>choose…</option>
                    {setOptions.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  </select>
                </span>
                <Button variant="outline" size="sm" onClick={bulkExport}>
                  <Download className="mr-1 h-3.5 w-3.5" /> Export
                </Button>
                <Button variant="outline" size="sm" onClick={() => setPending({ kind: "bulk", count: sel.size })}>
                  <Trash2 className="mr-1 h-3.5 w-3.5" /> Delete
                </Button>
                <button onClick={clearSel} className="ml-auto text-fg-muted hover:text-fg">clear</button>
              </div>
            ) : null}

            <div className="mb-3 mt-5 flex items-center gap-2">
              <SectionLabel>{mode === "search" ? `${total ?? items.length} results for “${query}”` : activeLabel}</SectionLabel>
              {mode === "search" && (setParam || tagParam) ? <Badge variant="outline">{setParam ?? `#${tagParam}`}</Badge> : null}
              {mode === "browse" ? <Badge variant="outline">{items.length}{cursor ? "+" : ""}</Badge> : null}
              {copiedToast ? <span className="text-2xs text-accent">{copiedToast}</span> : null}
              <div className="ml-auto flex items-center gap-2">
                <input ref={fileInputRef} type="file" accept=".svg,image/svg+xml" multiple hidden
                  onChange={(e) => { onImportFiles(e.target.files); e.currentTarget.value = ""; }} />
                <Button variant="outline" size="sm" onClick={() => fileInputRef.current?.click()}
                  title={filter.kind === "set" ? `Import into “${filter.value}”` : "Import into All Icons"}>
                  <Upload className="mr-1 h-3.5 w-3.5" /> Import
                </Button>
                <div className="flex items-center overflow-hidden rounded border border-border" title="Icon size">
                  {(["s", "m", "l"] as const).map((k) => (
                    <button key={k} onClick={() => setSize(k)}
                      className={"px-2 py-0.5 text-2xs case-label transition-colors " +
                        (size === k ? "bg-accent-subtle text-accent" : "text-fg-muted hover:bg-surface-subtle hover:text-fg")}>
                      {k}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            {loading && items.length === 0 ? (
              <SectionLabel className="text-fg-muted">loading…</SectionLabel>
            ) : items.length === 0 ? (
              <EmptyState title="No icons" description={mode === "search" ? `Nothing for “${query}”.` : "This filter is empty."} />
            ) : (
              <>
                <div className="grid gap-2"
                  style={{ gridTemplateColumns: `repeat(auto-fill,minmax(${dims.tile}px,1fr))` }}>
                  {items.map((icon, idx) => (
                    <IconCard key={icon.id} icon={icon} inspected={icon.id === selectedId} selected={sel.has(icon.id)}
                      iconPx={dims.icon} scrollRoot={scrollEl}
                      onSelect={(e) => onCardActivate(icon, idx, e)} onToggleFav={() => toggleFav(icon)} onToggleSelect={() => toggleSel(icon.id)} />
                  ))}
                </div>
                {mode === "browse" && cursor ? (
                  <div ref={sentinelRef} className="mt-6 flex justify-center">
                    <Button variant="outline" onClick={loadMore} disabled={loading}>{loading ? "loading…" : "Load more"}</Button>
                  </div>
                ) : null}
              </>
            )}
          </>
        )}
      </div>

      {selected ? (
        <SidePanel
          side="right"
          desktopClassName="flex w-72 shrink-0 flex-col gap-4 overflow-y-auto border-l border-border px-4 py-4"
          open={!!selected}
          onOpenChange={(o) => { if (!o) setSelectedId(null); }}
          title="Icon details"
        >
          <Inspector icon={selected} onClose={() => setSelectedId(null)}
            onToggleFav={() => toggleFav(selected)} onTagsChanged={(tags) => patchItem(selected.id, { tags })}
            onDelete={() => setPending({ kind: "one", id: selected.id })} />
        </SidePanel>
      ) : null}

      {/* R5 (372ccdb2), PERMANENT BRANCH — ONE MODAL FOR ALL THREE DELETES.
          Each sentence names what is lost; "this cannot be undone" on its own
          tells the reader nothing they did not already assume. `ui/dialog`
          steers `open` through `usePaneModalOpen`, so this cannot survive a
          topic change and leave the app unclickable. */}
      <Dialog open={!!pending} onOpenChange={(o) => { if (!o) setPending(null); }}>
        <DialogContent className="max-w-md">
          {pending && (
            <>
              <DialogHeader>
                <DialogTitle>
                  {pending.kind === "set"
                    ? `Delete the set \u201c${pending.name}\u201d?`
                    : pending.kind === "bulk"
                      ? `Delete ${pending.count} icon${pending.count === 1 ? "" : "s"} permanently?`
                      : "Delete this icon permanently?"}
                </DialogTitle>
                <DialogDescription>
                  {pending.kind === "set"
                    ? "The set goes; the icons in it stay in the library. Only the membership is removed, so nothing you imported is lost — but the grouping is, and okuro cannot reconstruct which icons were in it."
                    : "The SVG leaves the library for good. Anything that referenced it by id — a deck, a note, a generated page — will render nothing there, and re-importing gives it a new id."}
                </DialogDescription>
              </DialogHeader>
              <DialogFooter>
                <Button variant="outline" size="sm" onClick={() => setPending(null)}>
                  Cancel
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className="text-error"
                  onClick={() => {
                    const p = pending;
                    setPending(null);
                    if (p.kind === "set") void deleteSet(p.id, p.name);
                    else if (p.kind === "bulk") void bulkDelete();
                    else void deleteOne(p.id);
                  }}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  {pending.kind === "set" ? "Delete the set" : "Delete permanently"}
                </Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
