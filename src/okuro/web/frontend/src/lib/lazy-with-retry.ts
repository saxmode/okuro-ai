/**
 * THE STALE-CHUNK GUARD, extracted from `app.tsx` in p2 so the shell can wear it.
 *
 * A stale deploy or a transient/tunnelled fetch can 404 a code-split chunk after
 * a rebuild: the browser holds an index.html naming `page-abc123.js`, the box
 * now serves `page-def456.js`, and the import rejects with "Importing a module
 * script failed". On the FIRST such failure we reload once — index.html is
 * no-cache, so the reload pulls the current chunk map — and a repeat failure
 * surfaces normally rather than looping.
 *
 * WHY IT LEFT `app.tsx`. It used to guard the 40 page imports in the route
 * table, and after p2 the route table is gone: the shell's leaf registry
 * (`shell/views/registry.ts`) owns 31 of those imports and its per-leaf mounts
 * own the rest. A guard that covers the routes a frame no longer has is not a
 * guard. Splitting `retryOnce` out of `lazyWithRetry` is what lets the registry
 * keep its own `lazy()` call — it memoises the returned component type, because
 * `lazy()` returns a NEW type on every call and a new type remounts the subtree
 * mid-slide.
 *
 * The sessionStorage key is unchanged, deliberately: one reload budget shared by
 * every chunk in the app, not one per import site.
 */

import { lazy, type ComponentType } from "react";

const KEY = "okuro:chunk-reloaded";

/** Wrap a dynamic-import factory so the first module-script failure reloads once. */
export function retryOnce<T>(factory: () => Promise<T>): () => Promise<T> {
  return async () => {
    try {
      const mod = await factory();
      sessionStorage.removeItem(KEY);
      return mod;
    } catch (err) {
      if (!sessionStorage.getItem(KEY)) {
        sessionStorage.setItem(KEY, "1");
        window.location.reload();
        return await new Promise<T>(() => {}); // hold render until reload
      }
      throw err;
    }
  };
}

/** `React.lazy` with the guard already on.
 *
 *  The generic is `T extends ComponentType<any>` rather than a props type, which
 *  is what the original in `app.tsx` used and is not interchangeable: a page
 *  module resolves to a union of "the named export" and "the default export"
 *  shapes, and inferring a PROPS parameter from that union collapses it to
 *  `never` on the class branch and fails. Inferring the COMPONENT type keeps
 *  every call site's own props. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function lazyWithRetry<T extends ComponentType<any>>(
  factory: () => Promise<{ default: T }>,
) {
  return lazy(retryOnce(factory));
}
