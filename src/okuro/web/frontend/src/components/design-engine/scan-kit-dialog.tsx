/**
 * Duplicate okuro-ds from a WEBSITE — the second sanctioned way to start a
 * brand, beside forking one you can already see.
 *
 * IT MOVED HERE, IT IS NOT NEW. This was "Scrape a design" on Settings →
 * Design, next to a kit chooser, an accent picker and a free-form token editor.
 * The other three were dead, lying or an exception to "okuro-ds is not
 * editable" and were removed on 2026-09-06; this one is neither, because it
 * does exactly what Duplicate does. `POST /api/onboarding/design/extract` reads
 * the pages, then `fork_kit`s okuro-ds with what it found — the same recipe,
 * the same store, the same refusal to touch the shipped kit. So its home is
 * beside Duplicate, and Settings is left with the choice alone.
 *
 * WHAT A PAGE CAN SUPPLY IS FOUR THINGS: a colour, a typeface, a radius and a
 * border weight. Everything else stays the base's — the owner, "rest same as
 * base". That is why a page missing a border radius is not an error here.
 *
 * THE FIRST READABLE PAGE WINS per field, which is why order matters and the
 * hint says so. Averaging two brand colours produces a third that belongs to
 * neither.
 *
 * NO `window.confirm`, NO NATIVE PROMPT — same rule as the rest of this page:
 * a native dialog blocks the event loop and takes every Playwright gate with
 * it.
 */

import { useEffect, useState } from "react";

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

/** How many URLs the endpoint accepts. Mirrors `ExtractRequest`. */
const MAX_URLS = 6;

export interface ScanKitDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  pending: boolean;
  /** The dialog collects; the page owns the request, the refresh and the
      error routing — the same split the fork dialog uses. */
  onScan: (urls: string[], name?: string) => void;
}

export function ScanKitDialog({
  open,
  onOpenChange,
  pending,
  onScan,
}: ScanKitDialogProps) {
  const [urls, setUrls] = useState<string[]>([""]);
  const [name, setName] = useState("");

  useEffect(() => {
    if (open) {
      setUrls([""]);
      setName("");
    }
  }, [open]);

  const cleaned = urls.map((u) => u.trim()).filter(Boolean);
  const problem = cleaned.length === 0 ? "At least one URL is required." : null;

  const submit = () => {
    if (problem || pending) return;
    onScan(cleaned.slice(0, MAX_URLS), name.trim() || undefined);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Duplicate from a website</DialogTitle>
          <DialogDescription>
            Reads the pages and forks okuro&rsquo;s own system with the colour,
            typeface, corner radius and border weight they show. Everything else
            stays the base&rsquo;s. Put your main site first &mdash; where two
            pages disagree, the first readable one wins.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <label className="flex flex-col gap-1">
            <span className="font-mono type-small text-fg-tertiary">
              urls &middot; up to {MAX_URLS}
            </span>
            {urls.map((url, i) => (
              <Input
                key={i}
                autoFocus={i === 0}
                aria-label={`Reference URL ${i + 1}`}
                value={url}
                spellCheck={false}
                placeholder="https://example.com"
                onChange={(event) => {
                  const next = urls.slice();
                  next[i] = event.target.value;
                  setUrls(next);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") submit();
                }}
                className="font-mono"
              />
            ))}
            {urls.length < MAX_URLS && (
              <Button
                variant="outline"
                size="sm"
                className="self-start"
                onClick={() => setUrls([...urls, ""])}
                disabled={pending}
              >
                ADD URL
              </Button>
            )}
          </label>

          <label className="flex flex-col gap-1">
            <span className="font-mono type-small text-fg-tertiary">
              name &middot; optional
            </span>
            <Input
              aria-label="New design system name"
              value={name}
              spellCheck={false}
              placeholder="derived from the first URL"
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") submit();
              }}
              className="font-mono"
            />
            <span className="font-mono type-small text-fg-tertiary">
              {/* SAYS WHERE IT LANDS, because the shipped kit is the thing a
                  builder is most likely to fear overwriting. */}
              saved as a new kit in your instance, never in okuro&rsquo;s package
            </span>
          </label>
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={pending}
          >
            CANCEL
          </Button>
          <Button onClick={submit} disabled={Boolean(problem) || pending}>
            {pending ? "READING…" : "READ THE PAGES"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default ScanKitDialog;
