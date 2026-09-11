import { useEffect, useState } from "react";

// Single source of truth for "are we on a phone-sized viewport".
//
// Breakpoint = 768px to align with Tailwind's `md` (>=768 is desktop),
// so JS-gated layout swaps and `md:` utility classes never disagree.
//
// Desktop browser and the pywebview window are always wider than this,
// so this hook returns false for them — every mobile-only code path is
// dead weight there and the existing desktop layout renders untouched.
const MOBILE_QUERY = "(max-width: 767px)";

export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState<boolean>(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia(MOBILE_QUERY).matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia(MOBILE_QUERY);
    const onChange = () => setIsMobile(mq.matches);
    onChange();
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  return isMobile;
}
