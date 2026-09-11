import { useState, useEffect, useRef, useCallback } from "react";
import { pickQuote, type Quote } from "@/lib/quotes";
import { useReducedMotion } from "@/hooks/use-reduced-motion";

const ROTATION_MS = 45_000;
const TYPEWRITER_SPEED = 35; // ms per character
const FADE_DURATION = 800;
// Slightly longer than one typed character, so a wrap glides rather than snaps,
// but short enough that the block has settled before the next line wraps.
const HEIGHT_DURATION = 260;

interface QuoteDisplayProps {
  isActive: boolean;
}

/**
 * Ambient quote display with typewriter effect.
 * Shows in the lower area of the PULSE canvas sidebar.
 * Rotates every 45s, picks from active/contemplative pool based on system state.
 */
export function QuoteDisplay({ isActive }: QuoteDisplayProps) {
  const reducedMotion = useReducedMotion();
  const [quote, setQuote] = useState<Quote>(() => pickQuote(isActive));
  const [displayText, setDisplayText] = useState("");
  const [opacity, setOpacity] = useState(1);
  const typewriterRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined);
  const isActiveRef = useRef(isActive);
  isActiveRef.current = isActive;
  const reducedMotionRef = useRef(reducedMotion);
  reducedMotionRef.current = reducedMotion;

  const startTypewriter = useCallback((q: Quote) => {
    if (typewriterRef.current) clearInterval(typewriterRef.current);
    // Reduced-motion: render full text immediately, skip typewriter entirely.
    if (reducedMotionRef.current) {
      setDisplayText(q.text);
      return;
    }
    setDisplayText("");
    let i = 0;
    typewriterRef.current = setInterval(() => {
      i++;
      setDisplayText(q.text.slice(0, i));
      if (i >= q.text.length) {
        clearInterval(typewriterRef.current);
      }
    }, TYPEWRITER_SPEED);
  }, []);

  // Initial typewriter
  useEffect(() => {
    startTypewriter(quote);
    return () => {
      if (typewriterRef.current) clearInterval(typewriterRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Rotation cycle
  useEffect(() => {
    const interval = setInterval(() => {
      // Fade out
      setOpacity(0);
      setTimeout(() => {
        const next = pickQuote(isActiveRef.current);
        setQuote(next);
        startTypewriter(next);
        setOpacity(1);
      }, FADE_DURATION);
    }, ROTATION_MS);

    return () => clearInterval(interval);
  }, [startTypewriter]);

  // Animate the block's own height instead of letting it hug its content.
  //
  // The typewriter changes this block's height twice over: every time a line
  // wraps, and again when the attribution appears on the last character. The
  // parent stack is centered on itself (translateY(-50%)), so each of those
  // steps yanked the wordmark and stats above it. Measuring the content and
  // transitioning to the measured height turns those steps into a drift.
  const innerRef = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState<number | undefined>(undefined);

  useEffect(() => {
    const el = innerRef.current;
    if (!el) return;
    // ResizeObserver, not a displayText effect: it also catches reflows we
    // don't drive (sidebar resize, font swap), and fires after layout — a
    // post-render measure would read a stale height mid-wrap.
    const ro = new ResizeObserver(() => setHeight(el.offsetHeight));
    ro.observe(el);
    setHeight(el.offsetHeight);
    return () => ro.disconnect();
  }, []);

  // Inline block — positioning is owned by the parent PulseCanvas stack.
  // Both lines at 14px (body + attribution share the scale); the whole
  // block at reduced opacity so it reads as ambient, not foreground.
  return (
    <div
      className="overflow-hidden"
      style={{
        height,
        transition: reducedMotion ? undefined : `height ${HEIGHT_DURATION}ms cubic-bezier(0.4, 0, 0.2, 1)`,
      }}
    >
      <div
        ref={innerRef}
        className="select-none text-center transition-opacity"
        style={{
          opacity: opacity * 0.55,
          transitionDuration: `${FADE_DURATION}ms`,
        }}
      >
        <p
          className="font-mono leading-relaxed text-fg-muted"
          style={{ fontSize: "14px" }}
        >
          {displayText}
        </p>
        {displayText.length === quote.text.length && (
          <p
            className="mt-2 font-mono uppercase tracking-[0.25em] text-tertiary"
            style={{ fontSize: "14px" }}
          >
            {quote.author}
          </p>
        )}
      </div>
    </div>
  );
}
