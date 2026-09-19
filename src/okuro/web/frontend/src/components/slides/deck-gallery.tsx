// <!-- AGENT_HEADER
// role: code
// purpose: DELIVER/SLIDES · DECKS — the gallery the base address never had.
//   Opens a deck at its own path, and deletes one behind a modal because the
//   server's delete is permanent.
// AGENT_HEADER_END -->
/**
 * WHY THIS FILE EXISTS AT ALL.
 *
 * D6: the leaf used to list every deck and then call `load(decks[0].id)` — the
 * FIRST deck, always — from a `<select>` buried in a toolbar. There was no way
 * to link to a specific deck and no way to see what a deck looked like before
 * opening it. The base address now renders the collection, which is what the
 * declared `DECKS` section always claimed.
 *
 * THE DELETE IS NEW, AND THE p3 SPEC'S 37-ACTION INVENTORY DOES NOT CONTAIN IT.
 * `slidesApi.remove` has existed at `lib/slides-api.ts:199` with no caller, and
 * `DELETE /api/slides/{deck_id}` has existed behind it. Read from the store
 * rather than from the verb (PATTERN delta 4): `slides/storage.py:227`
 * `delete_deck` runs `DELETE FROM slides_decks` and there is no history table
 * for decks, so nothing can put one back. R5's PERMANENT branch: a modal, and
 * the sentence names what goes rather than asking whether you are sure.
 *
 * This is the same class as PEOPLE's built-but-unused soft delete, seen from
 * the other side — there, the recoverable branch existed and the UI took the
 * permanent one; here the action existed and the UI had none.
 */

import { useState } from "react";
import { Loader2, Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { countLabel, totalOrUnknown } from "@/lib/capped-count";
import { parseApiDate } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { DeckSummary } from "@/lib/slides-api";

/**
 * R7: the label shows the TRUE total, never the cap.
 *
 * `GET /api/slides` is `list_decks()` with no LIMIT (`slides/storage.py`), so
 * the array IS the whole set and the cap can never be hit. The shared helper is
 * used anyway rather than printing `decks.length`: if a limit is ever added
 * upstream, this prints a dash instead of quietly under-reporting.
 */
const DECK_CAP = 10_000;

/** `parseApiDate` returns an INVALID Date rather than null for a missing or
 *  unparseable stamp, and `toLocaleDateString()` on one returns the literal
 *  string "Invalid Date" — which the p4 WORK pass caught being shown to a user.
 *  The dash R7 already uses for an unknown value is the honest answer. */
function updatedLabel(d: DeckSummary): string {
  const t = parseApiDate(d.updated_at);
  return Number.isNaN(t.getTime()) ? "—" : t.toLocaleDateString();
}

export function DeckGallery({
  decks,
  openId,
  onOpen,
  onDelete,
  onNew,
  deleting,
  status,
}: {
  decks: DeckSummary[];
  openId?: string | null;
  onOpen: (id: string) => void;
  onDelete: (id: string) => void;
  onNew: () => void;
  deleting: boolean;
  status: string;
}) {
  const [query, setQuery] = useState("");
  /** R5's PERMANENT branch. The id being confirmed, or null. */
  const [confirm, setConfirm] = useState<DeckSummary | null>(null);

  const q = query.trim().toLowerCase();
  const shown = q ? decks.filter((d) => d.title.toLowerCase().includes(q)) : decks;
  const total = totalOrUnknown(decks, DECK_CAP);

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      {/* The leaf carries no title of its own — R1. The shell's `.c-title`
          already reads "Slides"; a second heading here was the duplicate that
          ruling removed. */}
      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="find a deck…"
          aria-label="Find a deck"
          className="w-field-sm"
        />
        <span className="type-small text-tertiary">
          {countLabel(shown.length, q ? total : total, "decks")}
        </span>
        <span className="truncate type-small text-tertiary">{status}</span>
        <Button size="sm" className="ml-auto" onClick={onNew}>
          <Plus className="h-3.5 w-3.5" />
          New deck
        </Button>
      </div>

      {shown.length === 0 ? (
        <EmptyState
          title={decks.length === 0 ? "No decks yet" : "No deck matches that"}
          description={
            decks.length === 0
              ? "Generate one from a topic, or start a blank deck."
              : "Clear the filter to see the rest."
          }
        />
      ) : (
        /* A grid that asks the PANE, not the window — R3. `minmax(0,1fr)` and
           not `auto`: an implicit column is at least as wide as its content, so
           a long deck title would push the track past the pane. That is the
           mistake PATTERN delta 6 records. */
        <ul className="grid min-h-0 flex-1 auto-rows-min grid-cols-[repeat(auto-fill,minmax(0,1fr))] gap-3 overflow-y-auto @2xl:grid-cols-2 @5xl:grid-cols-3">
          {shown.map((d) => (
            <li key={d.id}>
              <div
                className={cn(
                  "group flex w-full flex-col gap-2 rounded-lg border p-3 text-left transition-colors",
                  d.id === openId
                    ? "border-accent bg-accent-subtle"
                    : "border-border hover:border-accent",
                )}
              >
                <button
                  type="button"
                  onClick={() => onOpen(d.id)}
                  className="min-w-0 text-left"
                >
                  <span className="block truncate text-sm font-medium text-fg">
                    {d.title}
                  </span>
                  <span className="mt-0.5 block type-small text-tertiary">
                    {d.slide_count} {d.slide_count === 1 ? "slide" : "slides"} ·{" "}
                    {d.arrangement} · {updatedLabel(d)}
                  </span>
                </button>
                <div className="flex items-center gap-2">
                  <Button size="sm" variant="outline" onClick={() => onOpen(d.id)}>
                    Open
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="ml-auto text-error"
                    aria-label={`Delete ${d.title}`}
                    onClick={() => setConfirm(d)}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}

      {/* R5, PERMANENT BRANCH — A MODAL, NOT AN ARM.
          `slides/storage.py:227` runs `DELETE FROM slides_decks` and there is no
          deck history, so this cannot be set back. `ui/dialog` steers `open`
          through `usePaneModalOpen`, so it cannot outlive a topic change and
          leave the app unclickable. */}
      <Dialog open={!!confirm} onOpenChange={(v) => !v && setConfirm(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Delete “{confirm?.title}” permanently?</DialogTitle>
            <DialogDescription>
              This removes the deck and all {confirm?.slide_count} of its slides
              from the database. Its speaker notes, its recipient tailoring and
              any audience variants that point at it go with them, and okuro
              cannot put any of it back. Files you already exported keep what
              they contain.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => setConfirm(null)}>
              Cancel
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="text-error"
              disabled={deleting}
              onClick={() => {
                if (confirm) onDelete(confirm.id);
                setConfirm(null);
              }}
            >
              {deleting ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Trash2 className="h-3.5 w-3.5" />
              )}
              Delete permanently
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
