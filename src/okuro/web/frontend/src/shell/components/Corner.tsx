// SPDX-License-Identifier: Apache-2.0
/**
 * THE BOTTOM-RIGHT ACTION CONTAINER — collapsed by default, opened by reaching.
 *
 * ===========================================================================
 * WHAT THE OWNER ASKED FOR (walkthrough 2026-09-15, todo `9a2e62aa`)
 * ===========================================================================
 * *"bottom-right action container COLLAPSED by default, opens on hover like the
 * window controls (R9); FEEDBACK and ADD TASK move inside it; the floating '+'
 * add-task bubble is removed from START."*
 *
 * The defect behind it is measurable rather than aesthetic. At 1366x1024 under
 * the active `standard` kit, BEFORE this change:
 *
 *   .genact plate            176.78 x 71.19 @ 1183.22, 952.81   (right 1360)
 *   floating feedback button  46 x 46       @ 1295.61, 959.61
 *
 * The feedback button sat INSIDE the plate's box — on top of its third glyph —
 * on every one of the 31 leaves. Two floating affordances and a blurred plate
 * were competing for the same corner, which is why the corner had to collapse
 * before anything else about it could be right.
 *
 * ===========================================================================
 * IT IS A NEW FILE BECAUSE OF A BOUNDARY, NOT BECAUSE OF SIZE
 * ===========================================================================
 * `Frame.tsx` states the rule: `App.tsx` is the signed-off shell — geometry,
 * motion, the three laws — and okuro's own furniture stays out of it so no
 * future session mistakes a feedback button for part of the design. This
 * container is BOTH: a Figma-measured plate that has to live inside `.app` to
 * be positioned by it, holding three okuro actions. So the plate moves here,
 * out of `Chrome`, and the actions arrive through a context that `Frame`
 * provides — `App.tsx` gains no prop and learns nothing about tasks or reviews.
 *
 * ===========================================================================
 * FOUR SLOTS WHERE THERE WERE THREE, AND THAT IS A MEASURED CONSEQUENCE
 * ===========================================================================
 * The plate had three anonymous placeholder boxes. Three behaviours have to
 * land here (search, feedback, add task) and the R9 precedent puts the TRIGGER
 * in the last slot — because the plate is anchored `right:0`, so whichever
 * element is last holds the same x in both states and nothing has to be
 * recomputed. Three behaviours plus a trigger is four slots, so the OPEN plate
 * grows by one glyph and one pitch:
 *
 *   closed   padding 36.8 + glyph 16 + padding 36.8             =  89.6
 *   open     + fold (3 x 16 + 2 x 27.6) + margin 27.6           = 220.4
 *   was      padding 73.6 + 3 x 16 + 2 x 27.6                   = 176.8
 *
 * NOTHING ALIGNS TO THIS PLATE, which is why the growth is affordable and the
 * window controls' was not. `--sh-gutter` encodes "the collapsed SYSTEM icon
 * centre equals the TOP plate's last glyph centre" (`880dfd28`); the bottom
 * plate carries no constraint, it is at the opposite corner from the rail, and
 * it grows leftward from a fixed right edge. The p1 proof that records its
 * 176.78 is therefore updated rather than defended.
 */

import { createContext, useContext, type ReactNode } from "react";
import { ChevronLeft, ChevronRight, Maximize2, Moon, Plus, RotateCw, Search, Sun } from "lucide-react";
import { FeedbackButton } from "@/components/review/feedback-button";
import { OPEN_COMMAND_PALETTE_EVENT } from "@/components/ui/command-palette";
import { useDisclosure } from "../hooks/useDisclosure";
import { hardResetAndReload } from "../lib/hard-reset";

/**
 * THE ACTIONS THE PLATE CANNOT OWN.
 *
 * `onAddTask` opens `CreateDialog`, which `Frame` mounts and whose open state
 * `Frame` holds — the same state the command palette's "create new task" entry
 * already drives. A context rather than a prop through `App.tsx`, for the
 * boundary reason in the header. An absent provider is not an error: the glyph
 * is then simply not rendered, which is what `?embed=1` wants.
 */
export interface CornerActions {
  onAddTask?: () => void;
}

const CornerActionsContext = createContext<CornerActions>({});

export function CornerActionsProvider({
  value,
  children,
}: {
  value: CornerActions;
  children: ReactNode;
}) {
  return <CornerActionsContext.Provider value={value}>{children}</CornerActionsContext.Provider>;
}

/**
 * SEARCH GOES THROUGH THE PALETTE'S OWN DOOR, not through a fourth copy of its
 * open state. `command-palette.tsx:33-35` exports
 * `OPEN_COMMAND_PALETTE_EVENT` with the comment *"Fire this window event to
 * open the palette without the Cmd/Ctrl+K shortcut"* — it exists for exactly
 * this caller. Passing `open`/`onOpenChange` instead would make the palette
 * controlled, and its own props warn why that is worse: a controlled palette
 * stops answering Cmd+K on its own.
 */
function openSearch() {
  window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE_EVENT));
}

export function Corner({
  maxed,
  appearance,
  onToggleMax,
  onToggleAppearance,
}: {
  maxed: boolean;
  appearance: "dark" | "light";
  onToggleMax(): void;
  onToggleAppearance(): void;
}) {
  const { open, plate, toggle } = useDisclosure(true);
  const { onAddTask } = useContext(CornerActionsContext);

  return (
    <div className="genact sh-plate" {...plate}>
      {/* THE FOLD — the shared collapsing wrapper, `.pl-fold`, the same class
          the window controls wear. `--pl-n` is its glyph COUNT, read by the
          one width formula in the stylesheet; three here, four there. A wrapper
          rather than three collapsing children, because `gap` still applies
          between zero-width flex items and three would leave 2 x 27.6px of dead
          plate behind. */}
      {/* SIX, NOT THREE. Section-maximise, appearance and hard-refresh moved
          here from the window plate on 2026-09-19 — okuro's own controls belong
          with okuro's own actions, and the window plate keeps only what talks
          to the OS. */}
      <div className="pl-fold" style={{ ["--pl-n" as string]: 6 }}>
        <button
          type="button"
          className="hit wc-max sh-ctl"
          title="maximise section"
          aria-label="Maximise section"
          aria-pressed={maxed}
          {...(maxed ? { "data-on": "" } : {})}
          onClick={(e) => { e.stopPropagation(); onToggleMax(); }}
        >
          <Maximize2 aria-hidden="true" />
        </button>
        <button
          type="button"
          className="hit wc-appearance sh-ctl"
          aria-label={`Switch to ${appearance === "dark" ? "light" : "dark"} appearance`}
          aria-pressed={appearance === "light"}
          title="Toggle dark / light"
          {...(appearance === "light" ? { "data-on": "" } : {})}
          onClick={(e) => { e.stopPropagation(); onToggleAppearance(); }}
        >
          {appearance === "dark" ? <Moon aria-hidden="true" /> : <Sun aria-hidden="true" />}
        </button>
        <button
          type="button"
          className="hit wc-reload sh-ctl"
          aria-label="Hard refresh — clear caches and reload"
          title="Hard refresh (clears the service worker + caches)"
          onClick={(e) => { e.stopPropagation(); void hardResetAndReload(); }}
        >
          <RotateCw aria-hidden="true" />
        </button>
        <button
          type="button"
          className="hit sh-ctl"
          aria-label="Search okuro"
          title="Search — or press Cmd/Ctrl + K"
          onClick={(e) => {
            e.stopPropagation();
            openSearch();
          }}
        >
          <Search aria-hidden="true" />
        </button>

        {/* FEEDBACK, THE WHOLE COMPONENT, IN GLYPH FORM.
            It is not a copy of the button — it is the same `FeedbackButton`
            that `Frame` used to float here, asked to render its trigger as a
            16px glyph and its panel through a portal. Everything the component
            owns stays its own: pick mode, the severity row, the sync warning,
            the route evidence, the "what was the pointer over" resolution.
            Re-implementing the trigger here would have forked that. */}
        <FeedbackButton variant="glyph" />

        {/* ADD TASK — rendered only when a provider supplied the action, so the
            plate never shows a glyph that does nothing. */}
        {onAddTask && (
          <button
            type="button"
            className="hit sh-ctl"
            aria-label="Create new task"
            title="Create new task"
            onClick={(e) => {
              e.stopPropagation();
              onAddTask();
            }}
          >
            <Plus aria-hidden="true" />
          </button>
        )}
      </div>

      {/* THE TRIGGER IS LAST, AND THAT IS GEOMETRY BEFORE IT IS DESIGN — the
          same argument R9 made for the window controls. `right:0` plus the
          plate's own padding means the last element in the row holds its x in
          both states, so the fold grows leftward out from under it and the
          closed plate's glyph sits exactly where the open plate's last one did.
          `pin` only ever opens: the pointer path has already opened it by the
          time a click lands, so a toggle would shut it under the cursor. */}
      {/* AN ARROW, NOT THREE DOTS, AND IT TOGGLES. The owner, 2026-09-19:
          "not with a three dot icon but an arrow right. when collapsed: arrow
          left." The plate is anchored right and the fold grows LEFTWARD out
          from under this glyph, so the arrow points the way the fold will go:
          RIGHT while open (press to send it away), LEFT while collapsed (press
          to bring it back).
          `toggle`, not `pin` — the old trigger only ever opened, because hover
          had already opened the plate by the time a click landed and a toggle
          would have shut it under the cursor. Hover no longer opens anything,
          so the click is the whole interaction. */}
      <button
        type="button"
        className="hit ga-more sh-ctl"
        aria-expanded={open}
        aria-label={open ? "Collapse actions" : "Expand actions"}
        title={open ? "Collapse" : "Expand"}
        onClick={(e) => {
          e.stopPropagation();
          toggle();
        }}
      >
        {open ? <ChevronRight aria-hidden="true" /> : <ChevronLeft aria-hidden="true" />}
      </button>
    </div>
  );
}
