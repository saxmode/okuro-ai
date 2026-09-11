// <!-- AGENT_HEADER
// role: code
// purpose: Frontend API client for the okuro-assets.icons engine.
// index: types | iconsApi
// AGENT_HEADER_END -->
import { api, getToken } from "./api";
import { tokenizeApiSrc } from "./slides-api";

export interface IconResult {
  id: string;
  name: string;
  kebab_name: string;
  tags: string[];
  sets: string[];
  favorite: boolean;
  colorability: string | null;
  score: number;
  vec_score: number | null;
  fts_score: number | null;
  svg?: string | null;
}

export interface IconSearchResponse {
  count: number;
  results: IconResult[];
  error?: string;
  hint?: string;
}

export interface IconSet {
  id: string;
  name: string;
  parent_set_id: string | null;
  icon_count: number;
}

export interface IconStats {
  icons_total?: number;
  icons_embedded?: number;
  icons_favorite?: number;
  tags_total?: number;
  sets_total?: number;
  vec_loaded?: boolean;
  error?: string;
}

function qs(params: Record<string, string | number | boolean | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

export type MediaKind = "image" | "video" | "audio" | "icon" | "illustration";

export interface MediaAsset {
  id: string;
  kind: MediaKind;
  source?: string | null;
  folder?: string | null;
  title?: string | null;
  mime?: string | null;
  created_at?: string;
  tags: string[];
  meta: Record<string, unknown>;
}

/** The unified media bucket (migration 080) — every kind, with tags. */
export const mediaApi = {
  list: (p: { kind?: string; source?: string; folder?: string; tag?: string; q?: string; limit?: number; offset?: number }) =>
    api<{ count: number; items: MediaAsset[]; kinds: Record<string, number> }>(`/api/assets/media${qs(p)}`),
  tags: (kind?: string) =>
    api<{ tags: { name: string; count?: number }[] }>(`/api/assets/media/tags${qs({ kind })}`),
  editTags: (id: string, add: string[], remove: string[]) =>
    api<{ id: string; tags: string[]; error?: string }>(`/api/assets/media/${encodeURIComponent(id)}/tags`, {
      method: "POST",
      body: JSON.stringify({ add, remove }),
    }),
  del: (id: string) =>
    api<{ deleted?: string; error?: string }>(`/api/assets/media/${encodeURIComponent(id)}/delete`, {
      method: "POST",
    }),
  /** Bearer-tokenised file URL for <img>/<video>/<audio> src (GET can't set headers). */
  fileUrl: (id: string) => tokenizeApiSrc(`/api/assets/media/${encodeURIComponent(id)}/file`) ?? "",
};

export const iconsApi = {
  search: (
    q: string,
    opts: { set?: string; tag?: string; limit?: number; includeSvg?: boolean } = {},
  ) =>
    api<IconSearchResponse>(
      `/api/assets/icons/search${qs({
        q,
        set: opts.set,
        tag: opts.tag,
        limit: opts.limit ?? 60,
        include_svg: opts.includeSvg ?? true,
      })}`,
    ),
  browse: (
    opts: {
      set?: string;
      tag?: string;
      favorite?: boolean;
      cursor?: string | null;
      limit?: number;
      includeSvg?: boolean;
    } = {},
  ) =>
    api<IconBrowseResponse>(
      `/api/assets/icons/browse${qs({
        set: opts.set,
        tag: opts.tag,
        favorite: opts.favorite,
        cursor: opts.cursor ?? undefined,
        limit: opts.limit ?? 120,
        include_svg: opts.includeSvg ?? true,
      })}`,
    ),
  containers: {
    create: (type: string, name: string, parentSetId?: string | null) =>
      api<{ id?: string; name?: string; error?: string }>("/api/assets/icons/containers/create", {
        method: "POST",
        body: JSON.stringify({ type, name, parent_set_id: parentSetId ?? null }),
      }),
    rename: (type: string, id: string, name: string) =>
      api<{ id?: string; error?: string }>("/api/assets/icons/containers/rename", {
        method: "POST",
        body: JSON.stringify({ type, id, name }),
      }),
    remove: (type: string, id: string) =>
      api<{ deleted?: string; error?: string }>("/api/assets/icons/containers/delete", {
        method: "POST",
        body: JSON.stringify({ type, id }),
      }),
  },
  attach: (type: string, containerId: string, iconIds: string[], detach = false) =>
    api<{ attached?: number; detached?: number; error?: string }>("/api/assets/icons/attach", {
      method: "POST",
      body: JSON.stringify({ type, container_id: containerId, icon_ids: iconIds, detach }),
    }),
  favorite: (ids: string[], favorite: boolean) =>
    api<{ updated: number; favorite?: boolean; error?: string }>(
      "/api/assets/icons/favorite",
      { method: "POST", body: JSON.stringify({ ids, favorite }) },
    ),
  editTags: (iconId: string, opts: { add?: string[]; remove?: string[] }) =>
    api<{ icon_id: string; tags: string[]; error?: string }>(
      "/api/assets/icons/tags",
      { method: "POST", body: JSON.stringify({ icon_id: iconId, ...opts }) },
    ),
  importSvgs: (items: { name: string; svg: string }[], setId?: string | null) =>
    api<{ imported: number; ids: string[]; embedded: number; error?: string }>(
      "/api/assets/icons/import",
      { method: "POST", body: JSON.stringify({ items, set_id: setId ?? null }) },
    ),
  deleteIcons: (ids: string[]) =>
    api<{ deleted: number; error?: string }>("/api/assets/icons/delete", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),
  exportZip: async (ids: string[]): Promise<Blob> => {
    const token = await getToken();
    const res = await fetch("/api/assets/icons/export", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    if (!res.ok) throw new Error(`export failed (${res.status})`);
    return res.blob();
  },
  sets: () => api<{ sets: IconSet[]; error?: string }>("/api/assets/icons/sets"),
  tags: (prefix?: string, limit = 100) =>
    api<{ count: number; tags: { name: string; icon_count: number }[]; error?: string }>(
      `/api/assets/icons/tags${qs({ prefix, limit })}`,
    ),
  stats: () => api<IconStats>("/api/assets/icons/stats"),
};

export interface IconBrowseResponse {
  count: number;
  results: IconResult[];
  next_cursor: string | null;
  error?: string;
}
