// <!-- AGENT_HEADER
// role: code
// purpose: /prism/deck route — the kit-first 2D deck v2 runtime (A4). Fetches a
//   real DeckDoc from the backend deck2 store by ?id= (compiler → adapter →
//   /api/prism/deck2/{id}); falls back to the committed golden fixture when no id
//   is given or the fetch fails (keeps the route demoable + G1 usable). Edit-mode
//   A/B picks persist optimistically AND POST to /api/prism/deck2/{id}/pick.
//   Static export is retained via the legacy path and is NON-authoritative.
// AGENT_HEADER_END -->
import { useCallback, useEffect, useState } from "react";
import demoDeck from "@/components/prism/deck2/fixtures/demo-deck.json";
import type { BrandId, DeckDoc, DeckLevel } from "@/components/prism/deck2/deck-types";
import { DeckShadowHost } from "@/components/prism/deck2/deck-shadow-host";
import { prismDeck2Api } from "@/lib/prism-api";
import { redlineApi } from "@/lib/redline-api";

/** Immutably record an A/B pick on a cell (edit-mode swap persistence seam). */
function applyPick(deck: DeckDoc, row: "hero" | DeckLevel, col: number, pick: number): DeckDoc {
  if (row === "hero") return { ...deck, hero: { ...deck.hero, pick } };
  if (row === "L3") return deck;
  return {
    ...deck,
    topics: deck.topics.map((t, i) => {
      if (i !== col) return t;
      const cell = t.levels[row];
      if (!cell) return t;
      return { ...t, levels: { ...t.levels, [row]: { ...cell, pick } } };
    }),
  };
}

/** Deck id from the URL: ?id=… (query) or the #id=… hash fragment fallback. */
function deckIdFromUrl(): string | null {
  const q = new URLSearchParams(window.location.search).get("id");
  if (q) return q;
  const h = new URLSearchParams(window.location.hash.replace(/^#/, "")).get("id");
  return h || null;
}

export function PrismDeckPage() {
  const [deck, setDeck] = useState<DeckDoc>(() => demoDeck as unknown as DeckDoc);
  const [deckId, setDeckId] = useState<string | null>(() => deckIdFromUrl());

  useEffect(() => {
    const id = deckIdFromUrl();
    setDeckId(id);
    if (!id) return; // no id → keep the demo fixture
    let alive = true;
    prismDeck2Api
      .get(id)
      .then((d) => {
        if (alive) setDeck(d);
      })
      .catch((err) => {
        // Fall back to the demo fixture but surface the miss for debugging.
        console.error(`prism deck2 fetch failed for id=${id}:`, err);
      });
    return () => {
      alive = false;
    };
  }, []);

  const onPickAlt = useCallback(
    (row: "hero" | DeckLevel, col: number, pick: number) => {
      setDeck((prev) => applyPick(prev, row, col, pick));
      if (deckId && row !== "L3") {
        prismDeck2Api
          .pick(deckId, row, col, pick)
          .catch((err) => console.error("prism deck2 pick persist failed:", err));
      }
    },
    [deckId],
  );

  // Live brand restyle — repaint immediately (theme is CSS-var driven), persist
  // in the background. Visuals only; the voice follows the audience (retailor).
  const onBrandSwitch = useCallback(
    (brand: BrandId) => {
      setDeck((prev) => ({ ...prev, brand }));
      if (deckId) {
        prismDeck2Api.setBrand(deckId, brand).catch((err) => console.error("prism deck2 brand persist failed:", err));
      }
    },
    [deckId],
  );

  const onExport = useCallback(() => {
    if (!deckId) return;
    prismDeck2Api.exportFile(deckId).catch((err) => {
      console.error("prism deck2 export failed:", err);
      window.alert("Export failed — see console.");
    });
  }, [deckId]);

  const onRetailor = useCallback(() => {
    if (!deckId) return;
    const recipient = window.prompt("Re-tailor this deck to which audience? (recipient name)");
    if (!recipient) return;
    prismDeck2Api
      .retailor(deckId, recipient, deck.brand)
      .then((r) => window.alert(`Re-tailoring for “${recipient}” started (job ${r.job_id}).`))
      .catch((err) => {
        console.error("prism deck2 retailor failed:", err);
        window.alert(`Retailor failed: ${err?.message ?? "see console"}`);
      });
  }, [deckId, deck.brand]);

  const onDelete = useCallback(() => {
    if (!deckId) return;
    prismDeck2Api
      .remove(deckId)
      .then(() => { window.location.href = "/prism"; })
      .catch((err) => { console.error("prism deck2 delete failed:", err); window.alert("Delete failed — see console."); });
  }, [deckId]);

  // "Redline" — open this deck for commenting and go to the split view. The
  // version is the hash of the deck JSON as it reads right now, snapshotted so
  // the version keeps the bytes its comments were placed on; the deck2 store
  // is last-write-wins and would otherwise only ever hold "now".
  const onRedline = useCallback(() => {
    if (!deckId) return;
    redlineApi
      .open("prism", deckId)
      .then((opened) => {
        window.location.href = opened.web;
      })
      .catch((err) => {
        console.error("redline open failed:", err);
        window.alert(`Redline failed: ${err?.message ?? "see console"}`);
      });
  }, [deckId]);

  return (
    <div className="relative h-full w-full overflow-hidden">
      {deckId && (
        <button
          type="button"
          onClick={onRedline}
          title="Comment on this deck, anchored to its elements"
          className="absolute right-3 top-3 z-50 border border-border bg-surface px-2 py-1 text-xs text-fg hover:bg-surface-elevated"
        >
          redline
        </button>
      )}
      <DeckShadowHost
        deck={deck}
        onPickAlt={onPickAlt}
        onBrandSwitch={onBrandSwitch}
        onExport={onExport}
        onRetailor={onRetailor}
        onDelete={onDelete}
        canManage={!!deckId}
      />
    </div>
  );
}
