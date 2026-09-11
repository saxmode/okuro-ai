import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface VisualAdaptations {
  font_scale: number;
  whitespace: "compact" | "standard" | "spacious";
  contrast: "standard" | "high";
  size_contrast: "subtle" | "standard" | "extreme";
  animation: "reduced" | "standard";
}

const DEFAULTS: VisualAdaptations = {
  font_scale: 1.0,
  whitespace: "standard",
  contrast: "standard",
  size_contrast: "standard",
  animation: "standard",
};

function coerceVisual(raw: unknown): VisualAdaptations {
  const v = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return {
    font_scale: typeof v.font_scale === "number" ? v.font_scale : DEFAULTS.font_scale,
    whitespace: (v.whitespace as VisualAdaptations["whitespace"]) ?? DEFAULTS.whitespace,
    contrast: (v.contrast as VisualAdaptations["contrast"]) ?? DEFAULTS.contrast,
    size_contrast: (v.size_contrast as VisualAdaptations["size_contrast"]) ?? DEFAULTS.size_contrast,
    animation: (v.animation as VisualAdaptations["animation"]) ?? DEFAULTS.animation,
  };
}

/**
 * Reader-facing visual prefs from the okuro user profile.
 * Falls back to neutral defaults while the profile is loading so surfaces
 * never flicker between two typography scales.
 */
export function useVisualAdaptations(): VisualAdaptations {
  const { data } = useQuery({
    queryKey: ["ui-profile-visual"],
    queryFn: async () => {
      const profile = await api<{ ui_adaptations?: { visual?: unknown } }>(
        "/api/onboarding/profile",
      );
      return coerceVisual(profile?.ui_adaptations?.visual);
    },
    staleTime: 60_000,
  });
  return data ?? DEFAULTS;
}
