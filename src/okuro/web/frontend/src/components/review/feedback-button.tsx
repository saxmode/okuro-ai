/**
 * The always-there feedback affordance — fixed lower right, on every page.
 *
 * "the possibility to write everything down in context that i don't like."
 * Fixed position because the whole point is reachability: if it lived in a
 * page's layout it would move, or be missing, exactly where a complaint is
 * most likely.
 *
 * WHAT IT REVIEWS. The innermost declared <ReviewSurface> if the user is
 * inside one, otherwise the route-level default that AppShell registers — so
 * every page is reviewable with zero per-page work. The route travels as
 * EVIDENCE, never as identity (routes here have already been renamed).
 *
 * PICK MODE (option D) turns the whole app into a click target. The clicked
 * element resolves to its nearest DECLARED ancestor for identity, and its DOM
 * path is stored only as a hint — a selector dies on the next markup change,
 * so it can describe but never identify.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router";
import { MessageSquarePlus, Crosshair, X, Check, AlertCircle } from "lucide-react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/toast";
import { reviewApi, reviewSyncApi } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  declareSurface,
  domHint,
  pointedTarget,
  surfaceFromElement,
  trackPointer,
  type ReviewTarget,
} from "@/lib/review-surface";
import type { ReviewSeverity } from "@/types/api";

/** Order matters — this is the row the user scans. Actionable first. */
const SEVERITIES: { value: ReviewSeverity; label: string; hint: string }[] = [
  { value: "blocker", label: "Blocker", hint: "stops me — makes a todo" },
  { value: "annoyance", label: "Annoyance", hint: "friction — makes a todo" },
  { value: "idea", label: "Idea", hint: "recorded, no todo" },
  { value: "praise", label: "Praise", hint: "recorded, no todo" },
];

/** Severities that create a todo. Mirrors reviews.TODO_SEVERITIES server-side. */
const MAKES_TODO: ReviewSeverity[] = ["blocker", "annoyance"];

export function FeedbackButton() {
  const location = useLocation();
  // Resolved when the panel OPENS, from whatever the pointer last touched.
  // Context cannot work here: this button is mounted in AppShell, above every
  // page, and React context only flows downward.
  const [contextTarget, setContextTarget] = useState<ReviewTarget | null>(null);

  const [open, setOpen] = useState(false);
  const [picking, setPicking] = useState(false);
  const [comment, setComment] = useState("");
  const [severity, setSeverity] = useState<ReviewSeverity>("annoyance");
  // What the user clicked in pick mode. null = review the ambient surface.
  const [picked, setPicked] = useState<{ surfaceId: string | null; hint: string } | null>(null);

  const panelRef = useRef<HTMLDivElement>(null);

  const routeDefault = `route:${location.pathname}`;
  const surfaceId = picked?.surfaceId ?? contextTarget?.surfaceId ?? routeDefault;

  // Register the route-level default so EVERY page is reviewable with zero
  // per-page work. Components declaring a finer <ReviewSurface> simply win
  // over this one; they do not have to replace it.
  useEffect(() => {
    declareSurface({ surface_id: routeDefault, route: location.pathname });
  }, [routeDefault, location.pathname]);

  // Remember what the pointer is over, so opening the panel can answer "what
  // did he mean" with the thing he was actually looking at.
  useEffect(() => trackPointer(), []);

  const openPanel = useCallback(() => {
    setContextTarget(pointedTarget());
    setOpen(true);
  }, []);

  const reset = useCallback(() => {
    setOpen(false);
    setPicking(false);
    setComment("");
    setSeverity("annoyance");
    setPicked(null);
  }, []);

  const submit = useMutation({
    mutationFn: () =>
      reviewApi.submit({
        surface_id: surfaceId,
        comment: comment.trim() || null,
        severity,
        target_type: contextTarget?.targetType ?? "unresolved",
        target_id: contextTarget?.targetId ?? null,
        // Evidence — so a complaint about layout can be reproduced.
        route: location.pathname,
        viewport: `${window.innerWidth}x${window.innerHeight}`,
        dom_hint: picked?.hint || null,
      }),
    onSuccess: (row) => {
      toast.success(
        row?.todo_id ? "Noted — added to your todos" : "Noted",
      );
      reset();
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "Could not save that"),
  });

  // Pick mode: one click anywhere selects a target, Escape cancels.
  useEffect(() => {
    if (!picking) return;

    const onClick = (e: MouseEvent) => {
      const el = e.target as Element | null;
      if (panelRef.current?.contains(el as Node)) return;
      e.preventDefault();
      e.stopPropagation();
      setPicked({
        surfaceId: surfaceFromElement(el),
        hint: domHint(el),
      });
      setPicking(false);
      setOpen(true);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPicking(false);
    };

    document.addEventListener("click", onClick, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", onClick, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [picking]);

  // Dismiss the panel on Escape. Outside-click is deliberately NOT wired: the
  // panel holds typed text, and a stray click losing a half-written complaint
  // is exactly the friction that stops people reporting things.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  // Only asked once the panel is open — this must never cost anything on a
  // page the user is not reviewing.
  const { data: sync } = useQuery({
    queryKey: ["reviewSync"],
    queryFn: reviewSyncApi.status,
    enabled: open,
    staleTime: 60_000,
    retry: false,
  });

  // THE UPGRADE GAP. An install that updated into this feature has no
  // org_label, so its reviews queue instead of reaching the maintainer. Say so
  // here — at the moment of writing — and link straight to the field, rather
  // than failing at submit or staying silent.
  const syncBlocked = sync?.enabled && !sync?.ready;

  const willMakeTodo = MAKES_TODO.includes(severity);
  const canSubmit = comment.trim().length > 0 && !submit.isPending;

  if (picking) {
    return (
      <div data-review-ignore className="fixed inset-0 z-[100] cursor-crosshair bg-accent/5">
        <div className="fixed bottom-4 right-4 rounded border border-accent bg-surface-elevated px-3 py-2 text-3xs text-fg">
          Click what you want to review · Esc to cancel
        </div>
      </div>
    );
  }

  return (
    <div
      data-review-ignore
      className="fixed bottom-4 right-4 z-[90] flex flex-col items-end gap-2"
    >
      {open && (
        <div
          ref={panelRef}
          className="w-80 rounded border border-border bg-surface-elevated p-3 shadow-lg"
        >
          <div className="mb-2 flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="text-3xs uppercase tracking-wider text-tertiary">
                Reviewing
              </div>
              <div className="truncate font-mono text-3xs text-fg-muted" title={surfaceId}>
                {picked?.surfaceId
                  ? surfaceId
                  : contextTarget?.label ?? contextTarget?.surfaceId ?? location.pathname}
              </div>
            </div>
            <button
              type="button"
              aria-label="Close feedback"
              onClick={() => setOpen(false)}
              className="text-tertiary hover:text-fg"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          <Textarea
            autoFocus
            rows={3}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="What's wrong with this? e.g. 'the stars are too small to see'"
            className="mb-2 text-xs"
          />

          <div className="mb-2 flex flex-wrap gap-1">
            {SEVERITIES.map((s) => (
              <button
                key={s.value}
                type="button"
                title={s.hint}
                onClick={() => setSeverity(s.value)}
                className={cn(
                  "rounded border px-2 py-0.5 text-3xs transition-colors",
                  severity === s.value
                    ? "border-accent text-accent"
                    : "border-border text-tertiary hover:text-fg-muted",
                )}
              >
                {s.label}
              </button>
            ))}
          </div>

          {syncBlocked && (
            <div className="mb-2 flex items-start gap-1 rounded border border-warning/40 bg-warning/5 p-1.5 text-3xs text-warning">
              <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />
              <span>
                Saved here, but not sent — your organisation name is missing.{" "}
                <Link to="/settings?tab=feedback" className="underline">
                  Set it in Settings
                </Link>{" "}
                and {sync?.pending ?? 0} queued review
                {(sync?.pending ?? 0) === 1 ? "" : "s"} will send automatically.
              </span>
            </div>
          )}

          <div className="mb-2 flex items-center gap-1 text-3xs text-tertiary">
            {willMakeTodo ? (
              <>
                <Check className="h-3 w-3 text-accent" />
                becomes a todo
              </>
            ) : (
              "recorded — no todo"
            )}
          </div>

          <div className="flex items-center gap-2">
            <Button
              size="sm"
              disabled={!canSubmit}
              onClick={() => submit.mutate()}
            >
              {submit.isPending ? "Saving…" : "Send"}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setOpen(false);
                setPicking(true);
              }}
            >
              <Crosshair className="mr-1 h-3 w-3" />
              Pick element
            </Button>
          </div>
        </div>
      )}

      <button
        type="button"
        aria-label="Give feedback"
        title="Give feedback on what you're looking at"
        onClick={() => (open ? setOpen(false) : openPanel())}
        className={cn(
          "flex h-10 w-10 items-center justify-center rounded-full border shadow-lg transition-colors",
          open
            ? "border-accent bg-accent/10 text-accent"
            : "border-border bg-surface-elevated text-tertiary hover:text-accent",
        )}
      >
        <MessageSquarePlus className="h-4 w-4" />
      </button>
    </div>
  );
}
