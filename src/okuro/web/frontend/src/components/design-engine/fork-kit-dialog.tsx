/**
 * Fork a design system — how a curator starts a brand from an existing one.
 *
 * PORTED FROM v1's duplicate-kit-dialog, which shipped this and worked. v2 had
 * the whole mechanism and no way to reach it: `GET /kits/{id}` returns the full
 * authored brand and `POST /kits` writes a new one to the user store, but no
 * control anywhere changed `brand.id`. So the only path was open okuro-ds →
 * edit → Save → PUT /kits/okuro-ds → ShippedKit → 409, and the fork that
 * store.py's own docstring prescribes could not be performed from the page.
 *
 * IT COPIES WHAT IS ON SCREEN, not what is on disk. If you have been tweaking
 * and decide this should be its own brand, the tweaks are the reason you are
 * forking; leaving them behind would be the surprising answer.
 *
 * THE ID IS A FILENAME. `store._validate_id` checks the slug shape before it
 * becomes a path and `create=True` refuses an id that already exists, so this
 * dialog's checks are an affordance rather than the enforcement — they exist so
 * the answer arrives while you are still typing instead of after you press the
 * button.
 *
 * WHY THIS IS THE SANCTIONED PATH AND NOT A WORKAROUND, in store.py's words:
 * "A builder who wants to start from okuro-ds forks it under a new name; the
 * fork is theirs and the shipped kit stays byte-identical."
 */

import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { BrandJson } from "@/components/design-engine/types";

/** Mirrors `store.KIT_ID` — lowercase, digits, single hyphens between them. */
const SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
/** Mirrors `store.MAX_KIT_ID`. */
const MAX_ID = 64;

/** `Acme Brand` → `acme-brand`, so the common case needs no explaining. */
function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, MAX_ID);
}

export interface ForkKitDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The brand being forked. From the toolbar this is the DRAFT on screen; from
      a row in the SYSTEMS library it is the kit as saved. */
  source: BrandJson;
  /** Which of those two it is. The dialog says so rather than assuming: "copies
      what is on screen, including unsaved changes" is a promise, and it is
      false for a row in a list the builder has not opened. */
  fromScreen?: boolean;
  /** Every id already taken, so the clash is named before the request. */
  taken: string[];
  pending: boolean;
  onCreate: (brand: BrandJson) => void;
}

export function ForkKitDialog({
  open,
  onOpenChange,
  source,
  fromScreen = true,
  taken,
  pending,
  onCreate,
}: ForkKitDialogProps) {
  const [raw, setRaw] = useState("");

  useEffect(() => {
    if (open) setRaw(`${source.id}-copy`);
  }, [open, source.id]);

  const id = slugify(raw);

  const problem = useMemo(() => {
    if (!raw.trim()) return "An id is required.";
    if (!id) return "An id needs at least one letter or digit.";
    if (!SLUG.test(id) || id.length > MAX_ID) return "Lowercase letters, digits and hyphens.";
    if (taken.includes(id)) return `${id} already exists — pick another.`;
    return null;
  }, [raw, id, taken]);

  const submit = () => {
    if (problem || pending) return;
    // The WHOLE brand with a new id. A partial payload is a 422 rather than a
    // partial brand — the schema is strict and every block is required.
    onCreate({ ...source, id });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Fork {source.id}</DialogTitle>
          <DialogDescription>
            {fromScreen
              ? "Copies what is on screen, including unsaved changes."
              : `Copies ${source.id} as saved.`}{" "}
            The new kit is your own brand and is saved in your instance, never in
            okuro&rsquo;s package.
          </DialogDescription>
        </DialogHeader>

        <label className="flex flex-col gap-1">
          <span className="font-mono type-small text-fg-tertiary">id</span>
          <Input
            autoFocus
            aria-label="New design system id"
            aria-invalid={Boolean(problem)}
            value={raw}
            spellCheck={false}
            onChange={(event) => setRaw(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") submit();
            }}
            className="font-mono"
          />
          <span
            className={`font-mono type-small ${problem ? "text-destructive" : "text-fg-tertiary"}`}
            role={problem ? "alert" : undefined}
          >
            {problem ?? `saves as ${id}.json in your kit store`}
          </span>
        </label>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={pending}>
            CANCEL
          </Button>
          <Button onClick={submit} disabled={Boolean(problem) || pending}>
            {pending ? "FORKING…" : "FORK"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default ForkKitDialog;
