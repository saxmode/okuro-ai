// FE-fixed renderer for the BE `color_class` enum. BE supplies one of the
// five tokens; the FE owns the Tailwind class mapping. Adding a new BE
// state value never touches FE code — only adds a row server-side. See
// docs/audit-2026-05-26/02-frontend-state-audit.md §6.

import type { ColorClass } from "@/types/api";

export const TONE_BORDER: Record<ColorClass, string> = {
  neutral: "border-border",
  info: "border-info/50",
  warning: "border-warning/50",
  error: "border-error/50",
  success: "border-success/50",
};

export const TONE_BG: Record<ColorClass, string> = {
  neutral: "bg-surface",
  info: "bg-info-subtle/30",
  warning: "bg-warning-subtle/30",
  error: "bg-error-subtle/30",
  success: "bg-success-subtle/30",
};

export const TONE_TEXT: Record<ColorClass, string> = {
  neutral: "text-tertiary",
  info: "text-info",
  warning: "text-warning",
  error: "text-error",
  success: "text-success",
};

export const TONE_RAIL: Record<ColorClass, string> = {
  neutral: "bg-tertiary/40",
  info: "bg-info/60",
  warning: "bg-warning/60",
  error: "bg-error/60",
  success: "bg-success/60",
};

/**
 * Bundle the four tone-token classes for one ColorClass into a single
 * object. C10 helper: replaces inline conditional chains in SubtaskCard,
 * PhaseDivider and BlockerCard with a single lookup. Undefined input
 * collapses to the neutral row — never thrown, since BE-supplied
 * snapshots always carry a value.
 */
export function renderTone(
  colorClass: ColorClass | undefined,
): { border: string; bg: string; rail: string; text: string } {
  const cc: ColorClass = colorClass ?? "neutral";
  return {
    border: TONE_BORDER[cc],
    bg: TONE_BG[cc],
    rail: TONE_RAIL[cc],
    text: TONE_TEXT[cc],
  };
}
