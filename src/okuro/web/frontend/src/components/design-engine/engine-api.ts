/**
 * The engine's routes, typed. Thin — every call is one fetch and one cast.
 */

import { api } from "@/lib/api";
import type {
  BrandJson,
  EssentialsResult,
  FontFamily,
  KitRow,
  NestingResult,
  ResolvedModel,
  Rule,
  Stage,
} from "./types";

const BASE = "/api/design-engine";

/**
 * An engine refusal, ADDRESSED.
 *
 * `setError(String(err))` used to put `ApiError: radius.base: Input should be
 * greater than 0` into a 7px red span in the beat strip — machine text, in the
 * wrong register, up to 800px from the field that caused it. The information
 * needed to place it was already in the string: FastAPI flattens a Pydantic
 * validation error to `field.path: message`, and `lib/api.ts` already does that
 * flattening. All that was missing was reading it.
 *
 * A field-addressed error goes to THAT FIELD's help slot. An unaddressed one
 * becomes a must-fix row in the verdict band. `String(err)` never reaches a
 * user.
 */
export class EngineError extends Error {
  /** The authored slot, dotted — `radius.base`. Absent when the engine refused
      the whole brand rather than one value. */
  field?: string;

  constructor(message: string, field?: string) {
    super(message);
    this.name = "EngineError";
    this.field = field;
  }
}

/** A dotted path at the head of a message, as Pydantic and FastAPI write it. */
const ADDRESSED = /^([a-z_][\w]*(?:\.[\w]+)+)\s*:\s*(.+)$/s;

/**
 * Turn whatever came back into an `EngineError`.
 *
 * The first clause of a flattened Pydantic error is the path. Anything that
 * does not match is kept whole and left unaddressed — inventing a field for it
 * would put a sentence under a control that has nothing to do with the refusal.
 */
export function engineError(err: unknown): EngineError {
  const raw =
    err instanceof Error ? err.message : typeof err === "string" ? err : "";
  const text = raw.replace(/^\w*Error:\s*/, "").trim();
  const hit = ADDRESSED.exec(text);
  if (hit) return new EngineError(hit[2] as string, hit[1]);
  return new EngineError(
    text || "The engine refused this value and did not say why.",
  );
}

/** The one payload the page opens on. `opened` names the kit it is showing. */
export interface BootPayload {
  brand: BrandJson;
  model: ResolvedModel;
  sheet: string;
  stages: Stage[];
  rules: Rule[];
  kits: KitRow[];
  /** The kit `/engine.css` is currently painting the app with. */
  active: string;
  families: FontFamily[];
  opened: string | null;
}

export const engineApi = {
  /**
   * Everything the first paint needs, in ONE round trip.
   *
   * The page used to open four independent queries for its vocabulary and then,
   * only after a form was submitted, three more for the brand itself. Seven
   * serial round trips before a single ground appeared — and any one of them
   * being slow was indistinguishable from the page being broken, which is
   * exactly how it was read.
   */
  boot: () => api<BootPayload>(`${BASE}/boot`),

  fonts: () => api<{ families: FontFamily[] }>(`${BASE}/fonts`),

  stages: () => api<{ stages: Stage[] }>(`${BASE}/stages`),

  rules: () => api<{ rules: Rule[] }>(`${BASE}/rules`),

  kits: () => api<{ kits: KitRow[]; active: string }>(`${BASE}/kits`),

  kit: (id: string) =>
    api<{ id: string; origin: string; editable: boolean; brand: BrandJson; notes: string[] }>(
      `${BASE}/kits/${encodeURIComponent(id)}`,
    ),

  essentials: (id: string, font_family: string, brand_colour: string) =>
    api<EssentialsResult>(`${BASE}/essentials`, {
      method: "POST",
      body: JSON.stringify({ id, font_family, brand_colour }),
    }),

  resolve: (brand: BrandJson) =>
    api<ResolvedModel>(`${BASE}/resolve`, {
      method: "POST",
      body: JSON.stringify({ brand }),
    }),

  /** The emitted sheet, as text. The preview frame wears this unmodified. */
  sheet: async (brand: BrandJson): Promise<string> => {
    const res = await fetch(`${BASE}/sheet`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${await (await import("@/lib/api")).getToken()}`,
      },
      body: JSON.stringify({ brand }),
    });
    // THE SERVER ALREADY SAID WHAT IS WRONG; THIS USED TO THROW THE NUMBER.
    // `_brand_of` (api.py) wraps the pydantic message as the HTTPException's
    // `detail`, which is the addressed form `EngineError` above is built to
    // read — so `sheet failed: 400` threw away the one sentence that could
    // reach a field and replaced it with a status code. Measured on the
    // deployed page: typing `nothex` into the canonical colour produced a
    // banner reading "sheet failed: 400", 478px above the field, with
    // `aria-invalid` null on the input.
    //
    // This route reads `res.text()` rather than going through `api()` because
    // it returns CSS, not JSON — so the detail has to be lifted here.
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        if (body && typeof body === "object" && "detail" in body) {
          detail = String((body as Record<string, unknown>).detail);
        }
      } catch {
        /* a non-JSON refusal keeps the status text */
      }
      throw new Error(detail || `sheet failed: ${res.status}`);
    }
    return res.text();
  },

  /**
   * Guest brands nested inside a host — the inbox story, resolved live.
   *
   * The sheet comes back with the guests' own blocks in it, so what the frame
   * wears is the real emitted CSS rather than a rendering of the placements.
   * The placements beside it say WHICH clause of rule 4 each guest took.
   */
  nesting: (brand: BrandJson, guests: BrandJson[], ground: string) =>
    api<NestingResult>(`${BASE}/nesting`, {
      method: "POST",
      body: JSON.stringify({ brand, guests, ground }),
    }),

  create: (brand: BrandJson) =>
    api<{ id: string; path: string }>(`${BASE}/kits`, {
      method: "POST",
      body: JSON.stringify({ brand }),
    }),

  save: (brand: BrandJson) =>
    api<{ id: string; path: string }>(`${BASE}/kits/${encodeURIComponent(brand.id)}`, {
      method: "PUT",
      body: JSON.stringify({ brand }),
    }),

  /**
   * Delete a design system. The ROUTE has existed since the store was written
   * and nothing anywhere could reach it, so a brand created by a typo was
   * permanent. It refuses a shipped id with a 409 — the same refusal
   * `store.save` makes, for the same reason.
   */
  remove: (id: string) =>
    api<{ id: string; deleted: boolean }>(`${BASE}/kits/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
};
