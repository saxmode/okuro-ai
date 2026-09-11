import { api, cachedToken, getToken } from "./api";
import type { Deck } from "@/components/slides/scene";

/** okuro-slides deck persistence client (Phase 2). */

export interface DeckSummary {
  id: string;
  title: string;
  arrangement: string;
  slide_count: number;
  created_at: string;
  updated_at: string;
}

export interface DeckDetail extends DeckSummary {
  deck: Deck;
}

export type Density = "concise" | "balanced" | "detailed";
export type Jargon = "plain" | "balanced" | "technical";

export interface RetailorAxes {
  personId?: string;
  density?: Density;
  jargon?: Jargon;
  language?: string;
  asVariant?: boolean;
}

/** A deck summary enriched with variant linkage (returned by /variants). */
export interface VariantSummary extends DeckSummary {
  variantOf?: string | null;
  variantMeta?: { person_id?: string; language?: string; density?: string; jargon?: string };
  recipient?: string | null;
}

export interface VariantsResponse {
  source: VariantSummary | null;
  variants: VariantSummary[];
  source_id: string;
}

/** Append the cached bearer to a same-origin /api/ URL so an <img src> (which
 *  can't set headers) authenticates. Non-/api/ srcs (data:, https:) pass through. */
export function tokenizeApiSrc(src?: string): string | undefined {
  if (!src || !src.startsWith("/api/")) return src;
  const t = cachedToken();
  return t ? `${src}${src.includes("?") ? "&" : "?"}token=${encodeURIComponent(t)}` : src;
}

/** Which background a logo is designed to sit on. */
export type BgTarget = "dark" | "light" | "both";

export interface BrandAsset {
  id: string;
  brand_id: string;
  kind: string;
  name: string;
  mime: string;
  width: number | null;
  height: number | null;
  bytes: number;
  created_at: string;
  bg_target: BgTarget;
  url: string;
}

export const brandAssetsApi = {
  list: (brandId: string, kind?: string) =>
    api<{ assets: BrandAsset[] }>(
      `/api/brand-assets?brand_id=${encodeURIComponent(brandId)}${kind ? `&kind=${encodeURIComponent(kind)}` : ""}`,
    ),
  upload: async (
    brandId: string,
    kind: string,
    file: File,
    bgTarget: BgTarget = "both",
  ): Promise<BrandAsset> => {
    await getToken(); // ensure auth header
    const fd = new FormData();
    fd.append("file", file);
    fd.append("brand_id", brandId);
    fd.append("kind", kind);
    fd.append("name", file.name);
    fd.append("bg_target", bgTarget);
    return api<BrandAsset>("/api/brand-assets", { method: "POST", body: fd });
  },
  remove: (id: string) =>
    api<{ deleted: boolean }>(`/api/brand-assets/${encodeURIComponent(id)}`, { method: "DELETE" }),
};

/**
 * Pick the best logo for a given background from a brand's logo assets.
 * `bg` = the canvas background the logo will sit on. Prefers an exact
 * bg_target match, then a 'both' logo, then any logo as a last resort.
 */
export function pickLogoForBackground(
  logos: BrandAsset[],
  bg: "dark" | "light",
): BrandAsset | null {
  if (!logos.length) return null;
  return (
    logos.find((l) => l.bg_target === bg) ??
    logos.find((l) => l.bg_target === "both") ??
    logos[0] ??
    null
  );
}

export interface BrandSummary { id: string; name?: string }
export interface BrandTheme {
  background: string;
  font?: string;
  accent: string;
  text: string;
  /** Full resolved token set as CSS custom properties (--color-*, --font-*).
   *  Superset of the four shorthand fields; use to theme an entire surface,
   *  not just the deck's four hot spots. */
  vars: Record<string, string>;
}

/** Flatten a resolved design profile's visual.palette into --color-* vars,
 *  mirroring the backend token generator (okuro.design.tokens._flatten_css). */
function paletteToVars(pal: any, prefix = "--color", out: Record<string, string> = {}): Record<string, string> {
  if (!pal || typeof pal !== "object") return out;
  for (const [key, val] of Object.entries(pal)) {
    if (key === "constraints") continue;
    const name = `${prefix}-${key.replace(/_/g, "-")}`;
    if (val && typeof val === "object") paletteToVars(val, name, out);
    else if (val != null) out[name] = String(val);
  }
  return out;
}

/** Resolve a brand's design system into a deck theme: four shorthand fields
 *  plus a full `vars` map covering the entire palette + typography. */
export function brandToTheme(resolved: any): BrandTheme | null {
  let des = resolved?.slots?.design;
  if (Array.isArray(des)) des = des[0];
  const v = des?.data?.visual;
  if (!v) return null;
  const pal = v.palette ?? {};
  const font = [v.typography?.primary, v.typography?.fallback].filter(Boolean).join(", ");
  const vars = paletteToVars(pal);
  if (font) vars["--font-family-base"] = font;
  return {
    background: pal.background?.base ?? "#0b0f0c",
    font: font || undefined,
    accent: pal.accent ?? "#8ff0a4",
    text: pal.foreground?.primary ?? "#eafff0",
    vars,
  };
}

/** Streaming generate (SSE): activity steps then the final deck. Mirrors the
 *  flow draw stream so it works in WebKit (pywebview/Safari). */
export async function generateStream(
  topic: string,
  opts: { personId?: string; brandId?: string; mode?: "fast" | "quality" },
  h: { onActivity?: (msg: string) => void; onDone?: (deck: DeckDetail) => void; onError?: (msg: string) => void },
): Promise<void> {
  const token = await getToken();
  const params = new URLSearchParams({ prompt: topic, mode: opts.mode ?? "fast", token });
  if (opts.personId) params.set("person_id", opts.personId);
  if (opts.brandId) params.set("brand_id", opts.brandId);
  return new Promise<void>((resolve) => {
    const es = new EventSource(`/api/slides/generate/sse?${params.toString()}`);
    let done = false;
    const finish = () => { if (!done) { done = true; es.close(); resolve(); } };
    es.onmessage = (e: MessageEvent) => {
      let ev: { type?: string; msg?: string; deck?: DeckDetail; error?: string };
      try { ev = JSON.parse(e.data); } catch { return; }
      if (ev.type === "activity") h.onActivity?.(ev.msg ?? "");
      else if (ev.type === "done" && ev.deck) { h.onDone?.(ev.deck); finish(); }
      else if (ev.type === "error") { h.onError?.(ev.error ?? "generation error"); finish(); }
    };
    es.onerror = () => { if (!done) { h.onError?.("stream connection lost"); finish(); } };
  });
}

export const brandsApi = {
  list: () => api<{ brands: BrandSummary[] }>("/api/stack/brands"),
  resolve: (id: string) => api<any>(`/api/stack/brands/${encodeURIComponent(id)}/resolve`),
};

export const slidesApi = {
  list: () => api<{ decks: DeckSummary[]; seq: number }>("/api/slides"),
  get: (id: string) => api<DeckDetail>(`/api/slides/${encodeURIComponent(id)}`),
  generate: (topic: string, personId?: string, brandId?: string) =>
    api<DeckDetail>("/api/slides/generate", {
      method: "POST",
      body: JSON.stringify({ topic, person_id: personId, brand_id: brandId }),
    }),
  save: (title: string, deck: Deck, id?: string) =>
    api<DeckDetail>("/api/slides", {
      method: "POST",
      body: JSON.stringify({ id, title, deck }),
    }),
  remove: (id: string) =>
    api<{ deleted: boolean }>(`/api/slides/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  /** Re-tailor a deck to a recipient + density/jargon/language axes. With
   *  asVariant (default true) saves a NEW linked variant; false edits in place. */
  retailor: (deckId: string, axes: RetailorAxes) =>
    api<DeckDetail & { applied?: Record<string, unknown>; as_variant?: boolean }>(
      "/api/slides/retailor",
      {
        method: "POST",
        body: JSON.stringify({
          deck_id: deckId,
          person_id: axes.personId,
          density: axes.density,
          jargon: axes.jargon,
          language: axes.language,
          as_variant: axes.asVariant ?? true,
        }),
      },
    ),
  /** List a deck's audience variants (source + variants). Accepts a source or
   *  any variant id — the server resolves to the source. */
  variants: (sourceId: string) =>
    api<VariantsResponse>(`/api/slides/variants?source=${encodeURIComponent(sourceId)}`),
};
