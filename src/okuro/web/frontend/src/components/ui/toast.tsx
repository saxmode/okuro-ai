import { Toaster as SonnerToaster, toast as sonnerToast } from "sonner";

/**
 * Global toast surface. Replaces silent empty-catch blocks that gaslight
 * users about failures. Uses role="alert" via sonner's built-in live
 * regions.
 *
 * NO `theme` PROP, and its absence is the fix rather than an omission. It was
 * hardcoded `theme="dark"`, which makes sonner stamp `data-sonner-theme="dark"`
 * and paint from its own private palette -- so toasts were dark on a light
 * ground, today, before any of this. That is the same class of defect as the 22
 * `dark:` utilities deleted alongside it: a polarity encoded in a component
 * instead of read from the ground it sits on.
 *
 * Sonner is one of the few overlays that needs no ground stamp: verified, its
 * dist bundle contains no `createPortal` at all, so it renders IN the React tree
 * and inherits `--ground` like any other element. Dropping the prop lets the
 * classNames bridge below -- already all token classes -- carry the whole
 * appearance, which means a toast now wears whatever ground it lands on.
 *
 * THE ONE SOURCE OF TRUTH FOR TOAST APPEARANCE, exported because the showcase
 * renders static at-rest specimens per tone and must wear the same classes —
 * a second hand-written copy of them would drift the day a tone changes.
 *
 * EACH SEVERITY CARRIES A FILL, and that is the fix rather than a flourish. The
 * four rows used to set `!border-{role}` and nothing else, so every tone
 * inherited the base `!bg-surface-elevated` and all four rendered identically
 * (black in the app, white in a light frame) with a 1px border as the only
 * signal. `bg-{role}-subtle` is the adapter's own `--color-status-{role}-subtle`
 * — the signal composited over the RESOLVED ground — so the fill follows the
 * ground's polarity and `!text-fg` stays legible on it.
 *
 * WHY EVERY SEVERITY RULE IS QUALIFIED `data-[type=…]:` AND NOT A BARE UTILITY.
 * Both strings land in the SAME class attribute, both are `!important`, and both
 * are one class — so a bare `!bg-success-subtle` beats the base's
 * `!bg-surface-elevated` only if Tailwind happens to emit it later, and Tailwind
 * emits background utilities ALPHABETICALLY by token name. Measured in the built
 * bundle: `bg-error/info/success-subtle` all sort BEFORE `bg-surface-elevated`
 * and lost; only `bg-warning-subtle` sorted after it and won. The borders worked
 * for the same accidental reason (every role name sorts after "border").
 * `data-[type=success]:` compiles to `.class[data-type="success"]` — one class
 * plus one attribute — so the severity outranks the base on SPECIFICITY and the
 * emission order stops mattering. Sonner writes `data-type` on every toast; a
 * static specimen must carry it too.
 */
export const TOAST_CLASSNAMES = {
  toast:
    "!bg-surface-elevated !border !border-border !text-fg !rounded-md !font-mono !text-sm",
  title: "!text-fg",
  description: "!text-fg-muted",
  success: "data-[type=success]:!bg-success-subtle data-[type=success]:!border-success",
  error: "data-[type=error]:!bg-error-subtle data-[type=error]:!border-error",
  warning: "data-[type=warning]:!bg-warning-subtle data-[type=warning]:!border-warning",
  info: "data-[type=info]:!bg-info-subtle data-[type=info]:!border-info",
} as const;

export const TOAST_TONES = ["success", "error", "warning", "info"] as const;

/**
 * MOUNT THIS EXACTLY ONCE PER APP, in the shell (`app.tsx`). Having no portal is
 * the same reason a second mount is a bug: the store behind `toast()` is a
 * MODULE SINGLETON, so every mounted Toaster renders every toast, in its own
 * document. A second one inside the design-engine preview frame put two boxes on
 * screen 12px apart -- that frame's `right:24px` is measured from the iframe's
 * edge -- wearing two different resolved border colours, which read as "a border
 * detached from the box" and cost a root-cause session. A SPECIMEN of a toast is
 * static markup wearing `TOAST_CLASSNAMES`, never a second live Toaster.
 */
export function Toaster() {
  return (
    <SonnerToaster
      position="bottom-right"
      duration={4_000}
      toastOptions={{ classNames: { ...TOAST_CLASSNAMES } }}
    />
  );
}

export const toast = sonnerToast;
