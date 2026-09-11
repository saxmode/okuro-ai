import path from "path";

// ONE alias map for all three vite configs (SPA, prism export, deck export).
//
// Why a shared module: the deck export config carried its own copy of the
// aliases and never gained `@kitgallery` when kit-assets.ts started importing
// `@kitgallery/composed.css?raw` (2026-07-25). `pnpm build:export-deck` then
// failed on every machine for seven weeks — unnoticed on dev because
// update.sh downgrades a UI build failure to a warning, and fatal on every
// fresh install, which is how a Mac found it on 2026-09-12. Three copies of
// one map is how that happens; this is the single source.
//
// The prism board kit and the kit gallery live OUTSIDE the frontend src
// (../../prism/kit): the deck runtime imports their CSS/JS as ?raw and
// injects them into a shadow root — single source of truth, never copied.
export function okuroAliases(dirname: string): Record<string, string> {
  return {
    "@": path.resolve(dirname, "./src"),
    "@kit": path.resolve(dirname, "../../prism/kit/board"),
    "@kitgallery": path.resolve(dirname, "../../prism/kit/gallery"),
  };
}
