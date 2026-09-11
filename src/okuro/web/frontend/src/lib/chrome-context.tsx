// Shell-chrome visibility API. Lets a feature page (e.g. okuro·flow) collapse
// the surrounding app chrome — the left Sidebar (pulse) and top NavBar — to go
// full-screen, then restore it. State is ephemeral on purpose: chrome should
// always be present on a fresh load and on any page that doesn't opt out.
//
// Consume via useChrome(); the hook returns a no-op API when used outside the
// provider (embed mode, tests) so callers never need a null check.
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

export interface ChromeApi {
  /** true = chrome (left + top) is slid out / faded to opacity 0. */
  hidden: boolean;
  setHidden: (v: boolean) => void;
  hide: () => void;
  show: () => void;
  toggle: () => void;
}

const NOOP: ChromeApi = { hidden: false, setHidden: () => {}, hide: () => {}, show: () => {}, toggle: () => {} };

const ChromeContext = createContext<ChromeApi | null>(null);

export function ChromeProvider({ children }: { children: ReactNode }) {
  const [hidden, setHidden] = useState(false);
  const hide = useCallback(() => setHidden(true), []);
  const show = useCallback(() => setHidden(false), []);
  const toggle = useCallback(() => setHidden((v) => !v), []);
  const value = useMemo<ChromeApi>(() => ({ hidden, setHidden, hide, show, toggle }), [hidden, hide, show, toggle]);
  return <ChromeContext.Provider value={value}>{children}</ChromeContext.Provider>;
}

export function useChrome(): ChromeApi {
  return useContext(ChromeContext) ?? NOOP;
}
