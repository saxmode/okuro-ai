import { useCallback, useEffect, useRef, useState } from "react";
import { brandAssetsApi, tokenizeApiSrc, type BrandAsset } from "@/lib/slides-api";

/**
 * AssetPanel — the brand asset library inside the slides editor.
 *
 * Upload logos / images / portraits / signatures / backgrounds for a brand,
 * see them in a grid, click one to insert it as an image element on the current
 * slide. Backed by /api/brand-assets (a general subsystem reused by okuro-video
 * later), so the same library is available wherever brand artifacts are built.
 */

const KINDS = ["logo", "image", "portrait", "signature", "background"] as const;

export function AssetPanel({
  brandId,
  onInsert,
}: {
  brandId: string;
  onInsert: (asset: BrandAsset) => void;
}) {
  const [assets, setAssets] = useState<BrandAsset[]>([]);
  const [kind, setKind] = useState<(typeof KINDS)[number]>("logo");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      setAssets((await brandAssetsApi.list(brandId)).assets);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [brandId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setErr(null);
    try {
      await brandAssetsApi.upload(brandId, kind, file);
      await refresh();
    } catch (ex) {
      setErr((ex as Error).message);
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const del = async (id: string) => {
    await brandAssetsApi.remove(id);
    await refresh();
  };

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border p-3">
      <div className="flex items-center gap-2">
        <span className="text-xs uppercase tracking-wide text-tertiary">brand assets</span>
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as (typeof KINDS)[number])}
          className="rounded border border-border bg-transparent px-2 py-1 text-sm text-fg"
        >
          {KINDS.map((k) => (
            <option key={k} value={k} className="bg-background text-fg">{k}</option>
          ))}
        </select>
        <input ref={fileRef} type="file" accept="image/*,video/*" onChange={onFile} className="hidden" id="asset-file" />
        <label
          htmlFor="asset-file"
          className="cursor-pointer rounded-md border border-border px-3 py-1.5 text-sm text-fg hover:border-accent hover:text-accent"
        >
          {busy ? "uploading…" : `+ Upload ${kind}`}
        </label>
        <span className="text-xs text-tertiary">{brandId}</span>
        {err && <span className="text-xs text-[var(--color-status-error,#f92f77)]">{err}</span>}
      </div>

      {assets.length === 0 ? (
        <p className="py-2 text-center text-xs text-tertiary">No assets yet — upload a logo, portrait, image…</p>
      ) : (
        <div className="grid grid-cols-6 gap-2">
          {assets.map((a) => (
            <div key={a.id} className="group relative">
              <button
                onClick={() => onInsert(a)}
                title={`${a.kind} · ${a.name} — click to insert`}
                className="flex h-16 w-full items-center justify-center overflow-hidden rounded border border-border bg-black/20 hover:border-accent"
              >
                <img src={tokenizeApiSrc(a.url)} alt={a.name} className="h-full w-full object-contain" />
              </button>
              <button
                onClick={() => del(a.id)}
                aria-label="delete asset"
                className="absolute -right-1 -top-1 hidden h-4 w-4 items-center justify-center rounded-full bg-[var(--color-status-error,#f92f77)] text-[10px] text-white group-hover:flex"
              >
                ×
              </button>
              <span className="mt-0.5 block truncate text-[10px] text-tertiary">{a.kind}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
