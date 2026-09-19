// SPDX-License-Identifier: Apache-2.0
/**
 * THE FIVE PANEL SLOTS — pinned leaves, configurable, jumping on click.
 *
 * The owner, 2026-09-16: *"I thought a configurable panel with fast 'pinned'
 * functions for fast access … icons jump to function."* Ruled the same day:
 * the pins live on the okuro profile under `ui_adaptations.pins`, and what can
 * be pinned is **leaves only**.
 *
 * ===========================================================================
 * NO BACKEND CHANGE WAS NEEDED, AND THAT IS A MEASURED CLAIM
 * ===========================================================================
 * `PATCH /api/onboarding/profile` already does what this wants, for two
 * reasons that are both written down in `onboarding.py`:
 *
 *   1. `ProfilePatch` carries `model_config = ConfigDict(extra="allow")`, whose
 *      own docstring says UI-state keys *"flow through without needing a model
 *      edit every time a new one is added"*. `ui_adaptations` is such a key.
 *   2. `_merge_profile_patch` is a DEEP merge, so sending
 *      `{ui_adaptations: {pins: [...]}}` leaves `ui_adaptations.visual`
 *      untouched — the section is not replaced.
 *
 * And a LIST inside that merge replaces rather than merges, which is exactly
 * right for an ordered five-slot row: the array IS the order.
 *
 * ===========================================================================
 * WHY LEAVES ONLY, IN CODE TERMS
 * ===========================================================================
 * A pin is `"<topic>/<slug>"`, resolved through `IA` and `leafSlugs` — the same
 * two functions the router and the topic bars use. So a pin cannot name a
 * destination the shell does not have, and a leaf renamed in `IA` invalidates
 * its pin visibly (the slot empties) instead of navigating to a 404. Actions
 * ("new note", "start recording") would have been a second target kind with its
 * own handler table; that is a later ruling, not a silent extension.
 */

import { createContext, useCallback, useContext, useMemo, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { IA, type TopicId } from "../ia";
import { leafSlugs } from "../routes";

/** `"<topic>/<slug>"` — the address without its leading slash. */
export type Pin = string;

/** Figma measured five slots; the row's geometry is not ours to change. */
export const PIN_SLOTS = 5;

const PROFILE_KEY = ["ui-profile-pins"];

export interface ResolvedPin {
  pin: Pin;
  topic: TopicId;
  slug: string;
  /** The leaf's display title from `IA.titles`, e.g. "Notes". */
  label: string;
  path: string;
}

/**
 * Every leaf in the IA, as pinnable rows. Built from `IA` itself so a leaf
 * added there is offerable here with no second list to update.
 */
export function pinCatalogue(): ResolvedPin[] {
  const out: ResolvedPin[] = [];
  for (const t of IA) {
    const slugs = leafSlugs(t.id);
    slugs.forEach((slug, i) => {
      out.push({
        pin: `${t.id}/${slug}`,
        topic: t.id,
        slug,
        label: t.titles[i] ?? t.kids[i] ?? slug,
        path: `/${t.id}/${slug}`,
      });
    });
  }
  return out;
}

/** `null` for a pin that no longer names a leaf — an empty slot, not a 404. */
export function resolvePin(pin: Pin | null | undefined): ResolvedPin | null {
  if (!pin) return null;
  return pinCatalogue().find((c) => c.pin === pin) ?? null;
}

/**
 * THE DEFAULTS ARE THE PANEL'S OLD FIVE GLYPHS, READ AS INTENT.
 *
 * `icons.tsx` chose Newspaper / Activity / Gauge / Lightbulb / TrendingUp to
 * hint at the brief, sessions, telemetry, thoughts and progress — the four
 * dashboard panels plus the brief. The nearest LEAVES to that intent are these,
 * so a profile with no pins yet starts where the design was pointing rather
 * than empty.
 */
const DEFAULT_PINS: Pin[] = ["start/inbox", "know/notes", "work/tasks", "know/brain", "system/health"];

interface ProfileShape {
  ui_adaptations?: { pins?: unknown };
}

function coercePins(raw: unknown): Pin[] {
  if (!Array.isArray(raw)) return DEFAULT_PINS;
  const out = raw.filter((p): p is string => typeof p === "string").slice(0, PIN_SLOTS);
  return out.length ? out : DEFAULT_PINS;
}

export interface PinsApi {
  pins: Pin[];
  setPin(slot: number, pin: Pin | null): void;
  saving: boolean;
}

const PinsContext = createContext<PinsApi | null>(null);

export function PinsProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();

  const { data } = useQuery({
    queryKey: PROFILE_KEY,
    queryFn: async () => coercePins((await api<ProfileShape>("/api/onboarding/profile"))?.ui_adaptations?.pins),
    staleTime: 60_000,
  });

  const pins = useMemo(() => data ?? DEFAULT_PINS, [data]);

  const mutation = useMutation({
    mutationFn: (next: Pin[]) =>
      api("/api/onboarding/profile", {
        method: "PATCH",
        body: JSON.stringify({ ui_adaptations: { pins: next } }),
      }),
    // The row must answer the click immediately — a five-slot preference that
    // waits for a round trip feels broken. Write the cache, then reconcile.
    onMutate: (next) => {
      const prev = qc.getQueryData<Pin[]>(PROFILE_KEY);
      qc.setQueryData(PROFILE_KEY, next);
      return { prev };
    },
    onError: (_e, _next, ctx) => {
      if (ctx?.prev) qc.setQueryData(PROFILE_KEY, ctx.prev);
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: PROFILE_KEY }),
  });

  const setPin = useCallback(
    (slot: number, pin: Pin | null) => {
      // A fixed-length array, so an unpinned slot stays a SLOT. Splicing it out
      // would shift every icon after it and move a target the user has learned
      // the position of — the whole point of a pinned row.
      const next = Array.from({ length: PIN_SLOTS }, (_, i) => pins[i] ?? "");
      next[slot] = pin ?? "";
      mutation.mutate(next);
    },
    [pins, mutation],
  );

  const value = useMemo<PinsApi>(
    () => ({ pins, setPin, saving: mutation.isPending }),
    [pins, setPin, mutation.isPending],
  );

  return <PinsContext.Provider value={value}>{children}</PinsContext.Provider>;
}

/**
 * An absent provider yields the defaults and a no-op setter — same contract as
 * `CornerActionsProvider` and `PulseDataProvider`, so `?embed=1` and every unit
 * test render the row without a query client above them.
 */
export function usePins(): PinsApi {
  return useContext(PinsContext) ?? { pins: DEFAULT_PINS, setPin: () => {}, saving: false };
}
