/**
 * Color helpers for the accent picker.
 *
 * The picker writes a single hex; we derive the hover and subtle
 * companions so callers don't have to think about it.
 */

import { APCAcontrast, sRGBtoY } from "apca-w3";

const HEX6 = /^#?[0-9a-fA-F]{6}$/;

const FG_DARK = "#0a0a0a";
const FG_LIGHT = "#ffffff";

export function isHex6(value: string): boolean {
  return HEX6.test(value.trim());
}

export function normalizeHex(value: string): string {
  const t = value.trim();
  return t.startsWith("#") ? t.toLowerCase() : `#${t.toLowerCase()}`;
}

export function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  if (!isHex6(hex)) return null;
  const h = hex.replace("#", "");
  return {
    r: parseInt(h.slice(0, 2), 16),
    g: parseInt(h.slice(2, 4), 16),
    b: parseInt(h.slice(4, 6), 16),
  };
}

/** Multiply each channel by (1 - amount). amount in [0,1]. */
export function darken(hex: string, amount: number): string {
  const rgb = hexToRgb(hex);
  if (!rgb) return hex;
  const k = Math.max(0, Math.min(1, 1 - amount));
  const to2 = (n: number) => Math.round(n * k).toString(16).padStart(2, "0");
  return `#${to2(rgb.r)}${to2(rgb.g)}${to2(rgb.b)}`;
}

/** Return rgba() string with given alpha in [0,1]. */
export function withAlpha(hex: string, alpha: number): string {
  const rgb = hexToRgb(hex);
  if (!rgb) return hex;
  const a = Math.max(0, Math.min(1, alpha));
  return `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${a})`;
}

/**
 * Pick the higher-contrast foreground (dark vs light) for a background hex
 * using APCA — the perceptual contrast algorithm WCAG 3 is built on. APCA
 * is polarity-aware (signed Lc); we compare absolute magnitudes to choose
 * the side that yields more readable text. Saturated mid-luminance hues
 * (vivid magenta, lime, orange) are the cases where a luminance threshold
 * mis-picks but APCA gets right.
 */
export function pickForeground(bgHex: string): string {
  const rgb = hexToRgb(bgHex);
  if (!rgb) return FG_DARK;
  const bgY = sRGBtoY([rgb.r, rgb.g, rgb.b]);
  const darkY = sRGBtoY([10, 10, 10]);
  const lightY = sRGBtoY([255, 255, 255]);
  const lcDark = Math.abs(APCAcontrast(darkY, bgY));
  const lcLight = Math.abs(APCAcontrast(lightY, bgY));
  return lcLight > lcDark ? FG_LIGHT : FG_DARK;
}
