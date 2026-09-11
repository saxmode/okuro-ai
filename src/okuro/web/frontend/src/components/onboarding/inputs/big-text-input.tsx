import { useEffect, useRef } from "react";

/**
 * Large, center-stage text input — no visible label, no border chrome.
 *
 * The surrounding page provides the question as an H1; the input itself
 * is just a clean, oversized line that autofocuses on mount. The underline
 * is the only visual affordance so the control never competes with the
 * question copy for attention.
 */
export function BigTextInput({
  value,
  onChange,
  placeholder,
  autoFocus = true,
  type = "text",
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  autoFocus?: boolean;
  type?: "text" | "password";
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    // preventScroll:true — every wizard page mounts simultaneously
    // (scroll-snap container, not lazy), so EVERY autoFocus input would
    // call .focus() at mount. Without preventScroll, the LAST focus call
    // wins AND drags the viewport to that input via the browser's
    // implicit scrollIntoView, landing the user on page 5 (Location was
    // the last page with autoFocus = true). Audit 2026-04-27.
    if (autoFocus) ref.current?.focus({ preventScroll: true });
  }, [autoFocus]);

  return (
    <input
      ref={ref}
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="w-full bg-transparent border-0 border-b border-border-subtle focus:border-accent focus:outline-none text-2xl md:text-3xl font-light text-fg-primary placeholder:text-fg-disabled py-3 transition-colors"
    />
  );
}
