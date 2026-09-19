/**
 * THE SEVEN GROUNDS, AND WHO MAY BE A GUEST ON ONE — page-neutral.
 *
 * Both were inline in `pages/design-engine.tsx`, which is why `/ds-engine-codex`
 * could only PRINT `model.grounds.length` as a number: the switch that renders
 * them and the query that finds guest brands were not reachable from anywhere
 * else. Matrix rows 40 and 42.
 *
 * THE VOCABULARY SEAM IS THE THING TO GET RIGHT. The engine names the branded
 * root `brand` and the signal roots `signal:<role>`; `user_choice` is true for
 * exactly `light` and `dark`, because those are the only two a USER picks. The
 * other five are BUILDER PLACEMENTS that happen to sit at the root, not a second
 * kind of theme — so they are chips beside the appearance switch, never entries
 * in it.
 *
 * WHAT IS SHARED AND WHAT IS NOT. The chips publish `data-root-placement` and
 * `aria-pressed`; a gate reads those. The CLASS that paints a chip is the
 * PAGE's, handed in, because the two surfaces have different chrome vocabularies
 * and inventing a third here would be the copy this wave exists to avoid.
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { engineApi } from "@/components/design-engine/engine-api";
import type { BrandJson, RootChoice } from "@/components/design-engine/types";

export interface RootPlacementChipsProps {
  /** `model.roots` — the whole list. The filter is this component's job. */
  roots: RootChoice[];
  /** The placement in force, or `null` for "whatever the appearance says". */
  value: string | null;
  onChange: (next: string | null) => void;
  /** The page's own chip class. Logic is shared; chrome is not. */
  chipClassName: string;
}

/**
 * The builder placements, as toggles. Pressing the one already in force clears
 * it — a placement is a thing you put down and pick up, not a radio group, and
 * there has to be a way back to the plain appearance without a seventh chip
 * labelled "none".
 */
export function RootPlacementChips({
  roots,
  value,
  onChange,
  chipClassName,
}: RootPlacementChipsProps) {
  return (
    <>
      {roots
        .filter((root) => !root.user_choice)
        .map((root) => (
          <button
            key={root.id}
            type="button"
            className={chipClassName}
            aria-pressed={value === root.id}
            data-root-placement={root.id}
            onClick={() => onChange(value === root.id ? null : root.id)}
          >
            {root.label.replace("signal ", "")}
          </button>
        ))}
    </>
  );
}

/**
 * A CONTRASTING GUEST, PREFILLED, when this machine has only one brand.
 *
 * The nesting proof needs two brands and a fresh install ships one, so the scene
 * was a permanent dead end on exactly the machine that most needed it. The
 * host's own brand with ONE colour changed is enough: the adaptations are
 * cleared so the ENGINE re-derives them, which IS the demonstration. The page
 * computes no colour; it supplies one.
 */
export const DEMO_GUEST_COLOUR = "#3ba0ff";

export interface GuestBrands {
  /** What the nesting scene may place. Never empty unless there is no host. */
  guestsOnOffer: BrandJson[];
  /** True when the only guest on offer is the demo rather than a saved kit. */
  guestsAreDemo: boolean;
}

/**
 * Every saved kit's full brand, minus the one that is open, plus the demo
 * fallback. One fan-out over the kit list, cached on the list itself.
 */
export function useGuestBrands(
  kitIds: string[] | undefined,
  host: BrandJson | null,
): GuestBrands {
  const guestBrands = useQuery({
    queryKey: ["design-authoring", "guest-brands", kitIds],
    enabled: (kitIds?.length ?? 0) > 0,
    queryFn: async () => {
      const rows = await Promise.all((kitIds ?? []).map((id) => engineApi.kit(id)));
      return rows.map((row) => row.brand);
    },
  });

  const available = useMemo(
    () => (guestBrands.data ?? []).filter((b) => b.id !== host?.id),
    [guestBrands.data, host?.id],
  );

  const demoGuest = useMemo<BrandJson | null>(
    () =>
      host
        ? {
            ...host,
            id: "guest-demo",
            brand: {
              ...host.brand,
              canonical: DEMO_GUEST_COLOUR,
              on_light: null,
              on_dark: null,
              shade_light: null,
              shade_dark: null,
            },
          }
        : null,
    [host],
  );

  return {
    guestsOnOffer: available.length ? available : demoGuest ? [demoGuest] : [],
    guestsAreDemo: available.length === 0 && Boolean(demoGuest),
  };
}
