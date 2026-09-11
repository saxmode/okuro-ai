/**
 * Bundled-font registry — the single source of truth for which fonts okuro
 * self-hosts (Tier A) versus references system-only (Tier B).
 *
 * Two-tier model (see okuro decision — brand-drives-ui font loading):
 *   - Tier A "bundled": open-licence fonts (SIL OFL 1.1 / Apache-2.0) shipped
 *     as @fontsource* packages. Their @font-face is declared once at boot
 *     (main.tsx side-effect import) and self-hosted by Vite — no CDN, works
 *     offline / LAN-only. Browsers lazy-load the WOFF2 only when a design
 *     profile actually references the family, so listing all of them is free.
 *   - Tier B "system": proprietary fonts (e.g. a corporate Frutiger-based
 *     "Northwind Sans"). okuro has NO code path that serves these files —
 *     they render only if installed on the viewer's OS, else the design
 *     profile's fallback drops to a bundled Tier-A family. Zero file
 *     possession = zero licence/privacy surface.
 *
 * INVARIANT (enforced by fonts.test.ts): every bundled font MUST come from an
 * @fontsource* package, which are all OFL/Apache — making it structurally
 * impossible to bundle a proprietary font. Adding a family here also requires
 * a matching side-effect import in main.tsx.
 */

export type FontSource = "bundled" | "system";
export type FontCategory = "sans" | "mono" | "serif";

export interface BundledFont {
  /** CSS family name exactly as declared by the package's @font-face. */
  family: string;
  /** npm package that declares the @font-face (must be @fontsource*). */
  pkg: string;
  /** SPDX licence id. */
  license: "OFL-1.1" | "Apache-2.0";
  /** Weight axis [min, max] — the variable range, or the discrete min/max for
   *  a static font (the slider bounds either way). */
  weights: [number, number];
  /** Grouping for the curator font picker. */
  category: FontCategory;
  /** false = static font shipped as per-weight faces (no variable axis
   *  available upstream); omitted = variable. Static exceptions are allowed
   *  only when a brand needs a face with no variable version. */
  variable?: boolean;
}

// okuro's default curated set — user-selected (note "Font-families from
// fontsource.org"), all VARIABLE, all OFL/Apache (commercial + self-host).
// Each entry MUST have a matching side-effect import in lib/fonts-bundle.ts.
export const BUNDLED_FONTS: BundledFont[] = [
  // Sans
  { family: "Inter Variable", pkg: "@fontsource-variable/inter", license: "OFL-1.1", weights: [100, 900], category: "sans" },
  { family: "Roboto Variable", pkg: "@fontsource-variable/roboto", license: "Apache-2.0", weights: [100, 900], category: "sans" },
  { family: "Geist Variable", pkg: "@fontsource-variable/geist", license: "OFL-1.1", weights: [100, 900], category: "sans" },
  { family: "Open Sans Variable", pkg: "@fontsource-variable/open-sans", license: "Apache-2.0", weights: [300, 800], category: "sans" },
  { family: "Noto Sans Variable", pkg: "@fontsource-variable/noto-sans", license: "OFL-1.1", weights: [100, 900], category: "sans" },
  // Static exception — no variable version upstream; a brand font.
  { family: "Be Vietnam Pro", pkg: "@fontsource/be-vietnam-pro", license: "OFL-1.1", weights: [100, 900], category: "sans", variable: false },
  // Mono
  { family: "Roboto Mono Variable", pkg: "@fontsource-variable/roboto-mono", license: "Apache-2.0", weights: [100, 700], category: "mono" },
  { family: "Geist Mono Variable", pkg: "@fontsource-variable/geist-mono", license: "OFL-1.1", weights: [100, 900], category: "mono" },
  { family: "JetBrains Mono Variable", pkg: "@fontsource-variable/jetbrains-mono", license: "OFL-1.1", weights: [100, 800], category: "mono" },
  { family: "Spline Sans Mono Variable", pkg: "@fontsource-variable/spline-sans-mono", license: "OFL-1.1", weights: [300, 700], category: "mono" },
  { family: "Noto Sans Mono Variable", pkg: "@fontsource-variable/noto-sans-mono", license: "OFL-1.1", weights: [100, 900], category: "mono" },
  // Serif
  { family: "Noto Serif Variable", pkg: "@fontsource-variable/noto-serif", license: "OFL-1.1", weights: [100, 900], category: "serif" },
  { family: "Faustina Variable", pkg: "@fontsource-variable/faustina", license: "OFL-1.1", weights: [300, 800], category: "serif" },
  { family: "Lora Variable", pkg: "@fontsource-variable/lora", license: "OFL-1.1", weights: [400, 700], category: "serif" },
  { family: "Newsreader Variable", pkg: "@fontsource-variable/newsreader", license: "OFL-1.1", weights: [200, 800], category: "serif" },
  { family: "Literata Variable", pkg: "@fontsource-variable/literata", license: "OFL-1.1", weights: [200, 900], category: "serif" },
];

/** Family names okuro self-hosts — used to decide bundled vs system at render. */
export const BUNDLED_FAMILIES: ReadonlySet<string> = new Set(
  BUNDLED_FONTS.map((f) => f.family),
);

/** The suffix @fontsource appends to a variable font's CSS family name. */
const VARIABLE_SUFFIX = " Variable";

/**
 * Find the registry entry for a profile's family, or undefined.
 *
 * Matching is loose in one direction on purpose. A profile writes the family a
 * designer would write ("JetBrains Mono"); the registry carries the family the
 * @fontsource package declares ("JetBrains Mono Variable"). Exact matching
 * alone classified okuro's own architecture-noir as a system font, which put
 * its typeface under "Current (system / custom)" in the picker and bounded its
 * weight sliders to 100–900 instead of the face's real 100–800.
 *
 * This mirrors `classify_face` in okuro/design/fonts.py — the two run the same
 * rule against the same registry, and they must agree or the UI will offer a
 * weight the loader then rejects.
 */
export function findBundledFont(family: string): BundledFont | undefined {
  if (!family) return undefined;
  return BUNDLED_FONTS.find(
    (f) => f.family === family || f.family === family + VARIABLE_SUFFIX,
  );
}

/**
 * Classify a design profile's primary family. A family okuro bundles is
 * Tier A; anything else is Tier B (system-only, relies on OS install +
 * fallback). The profile may also state `source` explicitly; this is the
 * safe default when it doesn't.
 */
export function classifyFont(family: string): FontSource {
  return findBundledFont(family) ? "bundled" : "system";
}

/** The weight range a family can actually render — its axis, or its cuts. */
export function weightAxisFor(family: string): [number, number] {
  return findBundledFont(family)?.weights ?? [100, 900];
}
