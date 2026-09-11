/**
 * The redline stage for a PRISM DECK — the left pane when the document is a
 * deck rather than a page.
 *
 * A DeckDoc is not HTML. Nothing can serve it into the sandboxed frame the
 * other two inputs use, because what turns one into pixels is the SPA's own
 * deck runtime — so the deck renders HERE, in this page's React tree, and the
 * overlay attaches to the open shadow root that runtime already creates
 * (`deck-shadow-host.tsx`).
 *
 * WHICH ANCHOR TIERS DIE INSIDE THAT SHADOW ROOT, stated rather than
 * discovered later. All five tiers work — but only because every one of them
 * takes a resolution root and this component passes the shadow root. Attempted
 * from the DOCUMENT they are dead, measured and by specification:
 *
 * | Tier | From the document root |
 * |---|---|
 * | T1 `attr`, T2 `css`, T4 `attrquote` | `document.querySelector` does not pierce a shadow boundary — zero matches |
 * | T3 `quote` | `document.body.textContent` excludes shadow text — the quote is not there to find |
 * | T5 `ancestor` | walks the same excluded tree — zero matches |
 * | hit-testing | `document.elementFromPoint` returns the HOST, never the inner element |
 *
 * Two consequences the design accepts on purpose:
 *
 * 1. The anchor records `hosts` — the shadow-host chain — and that is the only
 *    thing in the anchor that says WHERE the element lives. A deck comment read
 *    without it is unresolvable, which is why `redline_list` returns it.
 * 2. A deck whose runtime later grows an EXTRA host level is an `orphan`, not a
 *    wrong answer: `hosts` names one root, and an element that is no longer
 *    inside it fails every tier rather than matching something else.
 *
 * The server cannot resolve a deck anchor at all — this module's Python
 * resolver parses HTML — so `redline_list` reports `anchor_state:
 * "unresolved"` for a prism comment and labels the identity from what the
 * anchor CARRIES. The bubble you see is the browser's own verdict, reached
 * here with the same five tiers and the same two-tier agreement rule.
 */

import { useEffect, useRef, useState } from "react";
import type { DeckDoc } from "@/components/prism/deck2/deck-types";
import { DeckShadowHost } from "@/components/prism/deck2/deck-shadow-host";
import { createOverlay, type OverlayHandle } from "./overlay-core";
import type { FrameToParent, RenderComment } from "./protocol";

/** The selector recorded in `anchor.hosts` — `deck-shadow-host.tsx`'s own. */
export const DECK_HOST_SELECTOR = '[data-testid="deck-host"]';

export interface DeckStageProps {
  /**
   * The version's own deck JSON, served under the redline capability token.
   * Always the SNAPSHOT rather than the live deck: the deck2 store is
   * last-write-wins with no revision, so the live file is only ever "now" and
   * a version needs the bytes its comments were made on.
   */
  deckUrl: string;
  /** Re-mount key — a new version means a new deck and a new shadow root. */
  versionSeq: number;
  comments: RenderComment[];
  addMode: boolean;
  onMessage: (message: FrameToParent) => void;
  /**
   * Handed the live overlay so the panel can scroll the deck to an element —
   * the in-page equivalent of the frame's `redline:scrollTo` message, which a
   * deck needs no postMessage for.
   */
  onOverlay?: (overlay: OverlayHandle | null) => void;
}

export function RedlineDeckStage({
  deckUrl,
  versionSeq,
  comments,
  addMode,
  onMessage,
  onOverlay,
}: DeckStageProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const overlayRef = useRef<OverlayHandle | null>(null);
  const messageRef = useRef(onMessage);
  messageRef.current = onMessage;
  const overlayCallbackRef = useRef(onOverlay);
  overlayCallbackRef.current = onOverlay;

  const [deck, setDeck] = useState<DeckDoc | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attached, setAttached] = useState(0);

  // The snapshot route carries its credential in the path and is exempt from
  // the bearer middleware, exactly like the document serve route — so this is
  // a plain fetch with no header to attach.
  useEffect(() => {
    let alive = true;
    setDeck(null);
    setError(null);
    fetch(deckUrl)
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status} — re-open the document`);
        return res.json();
      })
      .then((doc: DeckDoc) => {
        if (alive) setDeck(doc);
      })
      .catch((err: unknown) => {
        if (alive)
          setError(err instanceof Error ? err.message : "could not load the deck");
      });
    return () => {
      alive = false;
    };
  }, [deckUrl]);

  // Attach the overlay to the shadow root the deck runtime creates. That root
  // is attached in the host's own effect, so it may not exist on our first
  // pass — poll on animation frames rather than guessing a delay, and give up
  // loudly rather than silently after a bounded number of tries.
  useEffect(() => {
    if (!deck) return;
    let frame = 0;
    let tries = 0;
    let handle: OverlayHandle | null = null;

    const attach = () => {
      const hostEl = containerRef.current?.querySelector(DECK_HOST_SELECTOR);
      const shadow = (hostEl as HTMLElement | null)?.shadowRoot ?? null;
      if (!shadow || !shadow.querySelector(".deck-mount")) {
        if (++tries > 120) {
          setError("the deck rendered without a shadow root — nothing to anchor in");
          return;
        }
        frame = requestAnimationFrame(attach);
        return;
      }
      handle = createOverlay({
        root: shadow,
        hosts: [DECK_HOST_SELECTOR],
        emit: (message) => messageRef.current(message),
        // The SPA page owns the keyboard; this overlay must not also claim it.
        bindKeys: false,
        // No pointer-events layer: inside a shadow root the candidate is read
        // from the event's composed path, and a layer over the page would make
        // every event target the layer instead of the deck.
        captureLayer: false,
      });
      overlayRef.current = handle;
      overlayCallbackRef.current?.(handle);
      setAttached((n) => n + 1);
    };
    attach();

    return () => {
      cancelAnimationFrame(frame);
      handle?.destroy();
      overlayRef.current = null;
      overlayCallbackRef.current?.(null);
    };
  }, [deck, versionSeq]);

  // Re-place the bubbles whenever the deck's own tree changes.
  //
  // A served document is static: resolve once and the answer holds. A deck is
  // a LIVE React tree — it mounts its slides after the shadow root exists, and
  // it REPLACES them every time the owner navigates a topic or a level. A
  // single render at attach time would therefore pin every bubble to the slide
  // that happened to be on screen at mount, and leave them there.
  //
  // Same class as the frame's "the comment list never reached the frame": a
  // one-shot announcement aimed at a moment nobody can predict. The remedy is
  // the same — say it again rather than guess when to say it once. The
  // overlay's chrome lives outside this shadow root, so re-rendering cannot
  // feed the observer that triggered it.
  useEffect(() => {
    const overlay = overlayRef.current;
    if (!overlay) return;
    overlay.render(comments);

    const hostEl = containerRef.current?.querySelector(DECK_HOST_SELECTOR);
    const shadow = (hostEl as HTMLElement | null)?.shadowRoot;
    if (!shadow) return;

    let frame = 0;
    const observer = new MutationObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => overlay.render(comments));
    });
    observer.observe(shadow, { childList: true, subtree: true });
    // A webfont landing after first paint moves every rect on the slide.
    void document.fonts?.ready.then(() => overlay.render(comments));
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [comments, attached]);

  useEffect(() => {
    overlayRef.current?.setAddMode(addMode);
  }, [addMode, attached]);

  if (error) {
    return (
      <div className="p-4 text-sm text-tertiary">
        could not show the deck: {error}
      </div>
    );
  }
  if (!deck) {
    return <div className="p-4 text-sm text-tertiary">loading the deck…</div>;
  }
  return (
    <div ref={containerRef} className="h-full w-full overflow-hidden">
      <DeckShadowHost key={versionSeq} deck={deck} />
    </div>
  );
}
