import { useState } from "react";
import { AlertCircle, Check, Download, Loader2, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { modelsApi, type PullPlan, type PullStatus } from "@/lib/models-api";

/**
 * Pull, with the headroom numbers on screen BEFORE the bytes move.
 *
 * Ruling 8, 2026-09-15: there is NO headroom floor and a download is never
 * refused for space. So this cannot be a gate, and it is not one — the first
 * click asks the server to PLAN the pull and ARMS the button; the plan's
 * destination and any shortfall appear beside it. A second click pulls.
 * Nothing is blocked; the person is simply told first.
 *
 * WHY THE ARM IS UNCONDITIONAL, which is the p4 leaf pass meeting main's
 * planner. R5 (372ccdb2) splits confirmation by consequence: a modal for the
 * permanent, a two-step arm for the reversible. A pull is the reversible
 * branch — the download lands in the bundle store, the tray shows progress, a
 * finished job can be dismissed — but it is NOT cheap: the rows on screen are
 * sized in tens of GB (`min VRAM` is the column beside the button) and one
 * stray click starts a background job on somebody's connection.
 *
 * Main's flow armed only when the planner reported a shortfall, so a pull with
 * comfortable headroom was still one click. That is the R5 hole, and it closes
 * by moving the arm into `PullButton` — the primitive BOTH pull paths reach —
 * rather than into this wrapper. The catalog-search rows use the bare button
 * and get the arm for free; nothing that reaches the primitive can be
 * one-click by construction, which is the point of fixing it there.
 *
 * A FAILED PLAN STILL ARMS. Main's fall-through pulled unplanned when the
 * planner erred, which would make a planner hiccup re-open the one-click hole.
 * Planning stays a courtesy and never a gate: the arm holds, the numbers are
 * simply absent, and the second click pulls exactly as it would have.
 */
export function PullAction({
  catalogId,
  displayName,
  status,
  acquired,
  onPull,
}: {
  catalogId: string;
  displayName?: string;
  status?: PullStatus;
  acquired?: boolean;
  onPull: () => void;
}) {
  const [plan, setPlan] = useState<PullPlan | null>(null);
  const [checking, setChecking] = useState(false);

  /** Runs when the button ARMS, so the numbers are up before the second click. */
  async function planIt() {
    setChecking(true);
    try {
      setPlan(await modelsApi.pullDryRun(catalogId));
    } catch {
      /* planning is a courtesy, never a gate — the arm holds without numbers */
      setPlan(null);
    } finally {
      setChecking(false);
    }
  }

  const warn = plan?.ok === true && plan.headroom?.warn === true;

  return (
    /* `relative` IS LOAD-BEARING AND IT IS NOT DECORATION. The `sr-only` span
       at the bottom of this box is `position: absolute` (that is what Tailwind's
       `sr-only` is), so its containing block is the nearest POSITIONED
       ancestor. Every box between here and the shell's pane is static, so the
       55 spans on a full discoveries list were laid out against `.pane` at the
       table's scrolled-out x — MEASURED at x=2010 in a 752px pane — and they
       extended the PANE'S scrollable overflow to 1496px while the table's own
       `overflow-x-auto` wrapper, `.page-shell` and the document all correctly
       reported none.
       A/B, one run at 1366 (pane 751.63, clientWidth 752):
         as shipped .............................. pane.scrollWidth 1496
         with the 55 sr-only spans hidden ........ pane.scrollWidth  752
         with this box made `relative` ........... pane.scrollWidth  752
       Nothing was visually wrong and nothing was unreachable, which is why five
       readings of this page called it clean; what broke was the leaf contract's
       own "the pane does not overflow horizontally" clause, on a box no
       intermediate element reports. Fixed HERE rather than on the one table,
       because the component that renders the absolute child is the one that
       owes it a containing block. */
    <div className="relative inline-flex flex-col items-end gap-1">
      <PullButton
        state={status?.state}
        error={status?.error}
        acquired={acquired}
        checking={checking}
        /* The armed label says what the second click will do, and when the
           planner has warned it says it in main's words. */
        armedLabel={warn ? "Pull anyway" : undefined}
        onArm={planIt}
        onPull={onPull}
      />
      {warn && plan?.headroom && (
        <p
          role="alert"
          className="max-w-xs text-right text-3xs text-warning"
          style={{ lineHeight: "var(--type-label-line)" }}
        >
          <TriangleAlert
            className="mr-1 inline h-3 w-3 align-[-2px]"
            aria-hidden="true"
          />
          {plan.headroom.text}
        </p>
      )}
      {plan?.ok && plan.destination && (
        <p className="max-w-xs truncate text-right text-3xs text-fg-subtle">
          → {plan.destination.path}
        </p>
      )}
      {plan && !plan.ok && plan.reason && (
        <p className="max-w-xs text-right text-3xs text-fg-subtle">
          {plan.reason}
        </p>
      )}
      <span className="sr-only">{displayName}</span>
    </div>
  );
}

/**
 * R5, reversible branch — A MULTI-GIGABYTE WRITE IS NEVER ONE CLICK.
 *
 * The arm lives here and not in `PullAction` because this is the primitive
 * both pull surfaces reach: the discoveries table goes through `PullAction`,
 * the catalog-search table renders this directly. One arm, both paths, no way
 * to add a third path that forgets it.
 *
 * Armed state lives in THIS component, so it is per button by construction —
 * one row's arm cannot arm another's.
 */
export function PullButton({
  state,
  error,
  acquired,
  checking,
  label = "Pull",
  armedLabel,
  onArm,
  onPull,
}: {
  state?: PullStatus["state"];
  error?: string | null;
  acquired?: boolean;
  checking?: boolean;
  label?: string;
  armedLabel?: string;
  onArm?: () => void;
  onPull: () => void;
}) {
  const [armed, setArmed] = useState(false);
  if (acquired || state === "done") {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-success">
        <Check className="h-3 w-3" aria-hidden="true" />
        Installed
      </span>
    );
  }
  if (state === "downloading") {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-fg-subtle">
        <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
        Pulling…
      </span>
    );
  }
  if (state === "error") {
    return (
      <span
        title={error ?? ""}
        className="inline-flex items-center gap-1 text-xs text-error"
      >
        <AlertCircle className="h-3 w-3" aria-hidden="true" />
        Failed
      </span>
    );
  }
  if (armed) {
    return (
      <Button
        size="sm"
        variant="destructive"
        disabled={checking}
        onClick={() => {
          setArmed(false);
          onPull();
        }}
        /* THE ARMED BUTTON'S ACCESSIBLE NAME IS ITS VISIBLE LABEL when the
           planner has warned, so a screen reader and a test both hear "Pull
           anyway" rather than a generic word that contradicts the text. An
           `aria-label` overrides text content, which is why the generic one
           cannot simply stay. */
        aria-label={armedLabel ?? "Confirm download"}
      >
        {checking ? (
          <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
        ) : (
          <Download className="h-3 w-3" aria-hidden="true" />
        )}
        {checking ? "Checking…" : (armedLabel ?? "Download?")}
      </Button>
    );
  }
  return (
    <Button
      size="sm"
      variant="outline"
      onClick={() => {
        setArmed(true);
        onArm?.();
      }}
      aria-label="Pull this model"
    >
      <Download className="h-3 w-3" aria-hidden="true" />
      {label}
    </Button>
  );
}
