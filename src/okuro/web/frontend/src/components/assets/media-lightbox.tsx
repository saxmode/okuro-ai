// <!-- AGENT_HEADER
// role: code
// purpose: The ONE media detail popup — full-viewport preview of a media asset
//   plus a metadata/tag/download rail. Extracted from media-grid.tsx so every
//   surface that shows bucket media (assets grid, Studio library) opens the same
//   inspector instead of growing its own. Bytes always resolve through the asset
//   store by id (mediaApi.fileUrl), never by rebuilding a filesystem path.
// index: KIND_META | MediaFull | Inspector | MediaLightbox
// AGENT_HEADER_END -->
import { ChevronLeft, ChevronRight, Download, Film, Image as ImageIcon, Music, Share2, Shapes, Sparkles, Trash2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { HandoverDialog } from "@/components/handover/handover-dialog";
import type { ContentIR } from "@/lib/handover-api";
import { mediaApi, type MediaAsset, type MediaKind } from "@/lib/assets-api";
import { useFrameStamp } from "@/lib/ground-portal";

export const KIND_META: Record<MediaKind, { icon: typeof ImageIcon; label: string }> = {
  image: { icon: ImageIcon, label: "Images" },
  video: { icon: Film, label: "Video" },
  audio: { icon: Music, label: "Audio" },
  icon: { icon: Shapes, label: "Icons" },
  illustration: { icon: Sparkles, label: "Illustrations" },
};

/** Full-viewport render of one asset — the "fullpage" view (Slice A). Fills the
 *  media area of the Lightbox; capped to 90vh so controls/rail stay visible. */
function MediaFull({ a }: { a: MediaAsset }) {
  const url = mediaApi.fileUrl(a.id);
  if (a.kind === "audio") {
    return (
      <div className="flex w-full max-w-xl flex-col items-center gap-4 rounded-xl border border-border bg-black/20 p-8">
        <Music className="h-16 w-16 text-tertiary" />
        <div className="text-center text-sm text-fg">{a.title || a.id}</div>
        <audio controls autoPlay src={url} className="w-full" />
      </div>
    );
  }
  if (a.kind === "video") {
    return <video controls autoPlay src={url} className="max-h-[90vh] max-w-full rounded-lg" />;
  }
  // image · icon · illustration — inline (svg renders natively via <img>)
  return <img src={url} alt={a.title || ""} className="max-h-[90vh] max-w-full object-contain" />;
}

export function Inspector({ a, onClose, onTagsChanged, onDelete, hidePreview = false }: { a: MediaAsset; onClose: () => void; onTagsChanged: (tags: string[]) => void; onDelete?: (id: string) => void; hidePreview?: boolean }) {
  const url = mediaApi.fileUrl(a.id);
  const [draft, setDraft] = useState("");
  const [tags, setTags] = useState(a.tags);
  const [confirmDel, setConfirmDel] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [handover, setHandover] = useState<ContentIR | null>(null);
  useEffect(() => { setTags(a.tags); setConfirmDel(false); }, [a.id, a.tags]);

  // Hand this asset to another tool (e.g. embed the image into a note). The
  // clean /api url goes in the IR; the destination tokenizes it at display.
  const openHandover = () => setHandover({
    kind: "asset",
    title: a.title || a.id,
    body_md: "",
    structured: { assets: [{ id: a.id, kind: a.kind, mime: a.mime, url: `/api/assets/media/${a.id}/file`, title: a.title }] },
    source: { tool: "media", id: a.id, label: a.title || a.id },
  });

  const doDelete = async () => {
    if (!confirmDel) { setConfirmDel(true); return; }
    setDeleting(true);
    const res = await mediaApi.del(a.id);
    setDeleting(false);
    if (res.deleted) onDelete?.(a.id);
    else setConfirmDel(false);
  };

  const add = async () => {
    const t = draft.trim().toLowerCase();
    if (!t || tags.includes(t)) return;
    setDraft("");
    const res = await mediaApi.editTags(a.id, [t], []);
    if (res.tags) { setTags(res.tags); onTagsChanged(res.tags); }
  };
  const remove = async (t: string) => {
    const res = await mediaApi.editTags(a.id, [], [t]);
    if (res.tags) { setTags(res.tags); onTagsChanged(res.tags); }
  };

  return (
    <div className="flex w-72 shrink-0 flex-col gap-3 overflow-y-auto border-l border-border p-3">
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-tertiary">{KIND_META[a.kind]?.label ?? a.kind}</span>
        <button onClick={onClose} className="ml-auto rounded border border-border p-1 text-tertiary hover:text-fg"><X className="h-3.5 w-3.5" /></button>
      </div>
      {!hidePreview && (
        <div className="flex items-center justify-center rounded-lg border border-border bg-black/10 p-2">
          {a.kind === "audio" ? (
            <audio controls src={url} className="w-full" />
          ) : a.kind === "video" ? (
            <video controls src={url} className="max-h-56 w-full rounded" />
          ) : (
            <img src={url} alt={a.title || ""} className="max-h-56 w-auto object-contain" />
          )}
        </div>
      )}
      <div className="truncate text-sm font-medium text-fg">{a.title || a.id}</div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-tertiary">
        {a.source && <span>source: {a.source}</span>}
        {a.mime && <span>{a.mime}</span>}
        {a.created_at && <span>{a.created_at.slice(0, 10)}</span>}
      </div>
      <div>
        <div className="mb-1 text-[11px] uppercase tracking-wide text-tertiary">Tags</div>
        <div className="flex flex-wrap gap-1">
          {tags.map((t) => (
            <button key={t} onClick={() => remove(t)} title="remove" className="group rounded border border-border px-1.5 py-0.5 text-[11px] text-fg hover:border-[var(--color-status-error,#f92f77)]">
              {t} <span className="opacity-40 group-hover:opacity-100">×</span>
            </button>
          ))}
        </div>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
          placeholder="add tag…"
          className="mt-1.5 w-full rounded border border-border bg-transparent px-1.5 py-1 text-xs text-fg placeholder:text-tertiary focus:outline-none"
        />
      </div>
      {Object.keys(a.meta || {}).length > 0 && (
        <div className="text-[11px] text-tertiary">
          <div className="mb-1 uppercase tracking-wide">Meta</div>
          <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-words rounded bg-black/10 p-1.5">{JSON.stringify(a.meta, null, 1)}</pre>
        </div>
      )}
      <button onClick={openHandover} className="flex items-center justify-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-fg hover:border-accent hover:text-accent">
        <Share2 className="h-3.5 w-3.5" /> Hand over
      </button>
      <a href={url} download className="flex items-center justify-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-fg hover:border-accent hover:text-accent">
        <Download className="h-3.5 w-3.5" /> Download
      </a>
      <HandoverDialog open={!!handover} onOpenChange={(o) => !o && setHandover(null)} content={handover} />
      {onDelete && (
        <button
          onClick={doDelete}
          disabled={deleting}
          className={`flex items-center justify-center gap-1 rounded-md border px-2 py-1 text-xs transition-colors disabled:opacity-40 ${confirmDel ? "border-[var(--color-status-error,#f92f77)] text-[var(--color-status-error,#f92f77)]" : "border-border text-tertiary hover:border-[var(--color-status-error,#f92f77)] hover:text-[var(--color-status-error,#f92f77)]"}`}
          title={confirmDel ? "Click again to confirm" : "Delete this asset"}
        >
          <Trash2 className="h-3.5 w-3.5" /> {deleting ? "deleting…" : confirmDel ? "Confirm delete" : "Delete"}
        </button>
      )}
    </div>
  );
}

/** Full-page lightbox (Slice A): the asset fills the viewport with a metadata
 *  rail. Esc closes; ←/→ step through the current result set. role=dialog +
 *  aria-modal + focus-on-open + backdrop-click-to-close (WCAG-friendly). */
export function MediaLightbox({ items, index, onClose, onIndex, onPatchTags, onDelete }: {
  items: MediaAsset[];
  index: number;
  onClose: () => void;
  onIndex: (i: number) => void;
  onPatchTags: (id: string, tags: string[]) => void;
  onDelete?: (id: string) => void;
}) {
  const a = items[index];
  const ref = useRef<HTMLDivElement>(null);
  // Before the early return: a hook cannot be conditional, and this one has to
  // run on every render for the same reason it exists -- the ground under the
  // trigger can change without this component re-rendering for its own reasons.
  const { probe, frameProps } = useFrameStamp();
  // Lock background scroll while the modal is open (proper modal behavior, and it
  // removes the page scrollbar so the fixed overlay fills the full viewport width).
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = prev; };
  }, []);
  useEffect(() => {
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowRight" && index < items.length - 1) onIndex(index + 1);
      else if (e.key === "ArrowLeft" && index > 0) onIndex(index - 1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [index, items.length, onClose, onIndex]);

  if (!a) return null;
  const navBtn = "absolute top-1/2 -translate-y-1/2 rounded-full border border-border bg-black/40 p-2 text-fg transition-colors hover:border-accent hover:text-accent disabled:opacity-30";
  // Portal to <body>: escapes any transformed/filtered ancestor that would make
  // a `fixed` overlay resolve to the ancestor's box instead of the viewport, so
  // the lightbox truly fills the screen.
  return (
    <>
      {/* In-tree marker: a raw createPortal has no `container` to aim at, so the
          ground is read here and stamped on the portalled root below. */}
      <span {...probe} />
      {createPortal(
        <div
          {...frameProps}
          role="dialog"
          aria-modal="true"
          aria-label={a.title || "asset preview"}
          ref={ref}
          tabIndex={-1}
          className="fixed left-0 top-0 z-50 flex h-screen w-screen bg-black/90 outline-none backdrop-blur-sm"
          onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
        >
          <div className="relative flex min-w-0 flex-1 items-center justify-center p-6" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
            <button onClick={onClose} title="Close (Esc)" className="absolute right-3 top-3 z-10 rounded-full border border-border bg-black/40 p-2 text-fg hover:border-accent hover:text-accent">
              <X className="h-4 w-4" />
            </button>
            <span className="absolute left-4 top-4 z-10 text-xs text-tertiary">{index + 1} / {items.length}</span>
            <button onClick={() => onIndex(index - 1)} disabled={index === 0} className={`${navBtn} left-3`} title="Previous (←)"><ChevronLeft className="h-5 w-5" /></button>
            <button onClick={() => onIndex(index + 1)} disabled={index === items.length - 1} className={`${navBtn} right-3`} title="Next (→)"><ChevronRight className="h-5 w-5" /></button>
            <MediaFull a={a} />
          </div>
          <Inspector a={a} hidePreview onClose={onClose} onTagsChanged={(tags) => onPatchTags(a.id, tags)} onDelete={onDelete} />
        </div>,
        document.body,
      )}
    </>
  );
}
