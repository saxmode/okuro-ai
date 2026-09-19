import { api } from "@/lib/api";

/**
 * Project authorship, client side.
 *
 * Mirrors `GET /api/about` (src/okuro/web/app.py::api_about), which reads
 * the installed distribution's metadata. The name and address live in
 * pyproject's `[project] authors` and nowhere else — this module must never
 * hard-code either, because the release owner gate permits them in LICENSE,
 * NOTICE and pyproject.toml only, and a literal here would be a finding.
 *
 * WHY THE API AND NOT A BUILD-TIME CONSTANT. Same reason as `features-api`:
 * the bundle is built once and installed everywhere. Baking a value in means
 * the source of truth is whatever the build machine happened to read, which
 * is exactly the drift the single-home rule exists to prevent.
 */

export interface AboutResponse {
  /** Display name, or null when the distribution declares no author. */
  author: string | null;
  /** Address, or null. Either half may be absent independently. */
  author_email: string | null;
}

/** Credit-less shape. A failed fetch drops the line; it never blocks the page. */
export const ABOUT_UNKNOWN: AboutResponse = {
  author: null,
  author_email: null,
};

export function fetchAbout(): Promise<AboutResponse> {
  return api<AboutResponse>("/api/about");
}
