import { useCallback } from "react";
import { useSearchParams } from "react-router";

/**
 * Sync a `<Tabs>` active value with the URL `?tab=` query string so
 * refresh, back/forward, and bookmarks preserve tab state.
 *
 * `defaultValue` is returned when no `?tab=` is present. Unknown values
 * in the URL are passed through — the `Tabs` component tolerates that.
 */
export function useUrlTab(defaultValue: string, paramName: string = "tab") {
  const [params, setParams] = useSearchParams();
  const value = params.get(paramName) ?? defaultValue;

  const onValueChange = useCallback(
    (next: string) => {
      setParams(
        (prev) => {
          const copy = new URLSearchParams(prev);
          if (next === defaultValue) {
            copy.delete(paramName);
          } else {
            copy.set(paramName, next);
          }
          return copy;
        },
        { replace: true },
      );
    },
    [setParams, paramName, defaultValue],
  );

  return { value, onValueChange };
}
