// SPDX-License-Identifier: Apache-2.0
/**
 * ONE TOPIC BAR — collapsed rail or grown section, decided entirely by
 * `data-active` and read entirely by CSS.
 *
 * React sets one attribute. The grow, the quarter-turn, the divider, the
 * icon→text crossfade and the content reveal are all stylesheet rules keyed off
 * it. Nothing here writes a width, a flex value or a resting transform.
 */

import { useEffect, useRef, useState } from "react";
import { IA, leafTitle, type Topic } from "../ia";
import { useWindowedPanes } from "../hooks/useSlide";
import { motionDurationMs } from "../slider";
import { LEAF_ICONS, TOPIC_ICONS } from "../icons";
import { LeafView } from "./LeafView";
import { PageTitleProvider, PlateTitle } from "./PageTitle";

/**
 * `inert` AT REST, NEVER MID-SLIDE — and the asymmetry is the whole hook.
 *
 * ---------------------------------------------------------------------------
 * WHY IT IS NEEDED. Law 3 says collapsed bars go to zero width and are never
 * removed, which is correct and is what makes laws 1 and 2 non-arbitrary — but
 * the law says nothing about the ACCESSIBILITY TREE. Measured at HEAD
 * 3d0062216 on an isolated chromium at 1366x1024, the four inactive bars put
 * every focusable control of their own page into the tab order:
 *
 *     route              tab stops in the four INACTIVE panes
 *     /start/now         174
 *     /know/repos         40
 *     /work/bridge       197
 *     /deliver/assets    198
 *     /system/health     389
 *     /system/design      45
 *
 * The count is not a constant: it is the sum of whatever page each bar happens
 * to remember, so it moves with history and with how much data has loaded. What
 * IS constant is the mechanism — five panes, none inert, none display:none — and
 * that a Tab walk on /system/health reached the active pane on press 390.
 *
 * ---------------------------------------------------------------------------
 * THE ASYMMETRY. `inert` is applied only once the bar has finished leaving, and
 * removed in the SAME COMMIT that makes it active:
 *
 *   becoming active    -> `!active` is false during render, so inert is gone
 *                         before the frame the slide starts in. No effect, no
 *                         second render, no window where a bar both grows and
 *                         refuses focus.
 *   becoming inactive  -> `settled` is still false, so the DEPARTING content is
 *                         never inert while it is still visible and travelling.
 *                         It goes inert one motion duration later.
 *   first mount        -> an inactive bar is inert immediately. A cold arrival
 *                         animates nothing (Law 3), so there is nothing to wait
 *                         for.
 *
 * The duration is READ from `--sh-speed-medium` rather than repeated, for the
 * reason `motionDurationMs` records: a literal disagrees with the token the
 * moment reduced motion or slow-mo is on.
 */
function useRestingInert(active: boolean, ref: React.RefObject<HTMLElement | null>): boolean {
  const [settled, setSettled] = useState(!active);

  useEffect(() => {
    if (active) {
      setSettled(false);
      return;
    }
    const el = ref.current;
    const ms = (el ? motionDurationMs(el) : 650) + 60;
    const t = window.setTimeout(() => setSettled(true), ms);
    return () => window.clearTimeout(t);
  }, [active, ref]);

  return !active && settled;
}

export interface TopicBarProps {
  topic: Topic;
  /** this bar's position in the IA — arrow-key stepping needs its neighbours */
  index: number;
  active: boolean;
  /** the selected sub-item for THIS topic */
  sub: number;
  /** false while a topic switch is already moving this content (Law 2) */
  animateSub: boolean;
  /** which section `?view=` selects for the ACTIVE leaf */
  view: number;
  /** the detail segment of `/{topic}/{leaf}/{id}`, for the ACTIVE leaf */
  id?: string;
  onSelectView(viewIndex: number): void;
  onActivate(sub?: number): void;
  onSelectSub(index: number): void;
  /** C4 — activate the topic at this IA index, for ArrowLeft/ArrowRight */
  onActivateTopic(index: number): void;
}

export function TopicBar({
  topic,
  index,
  active,
  sub,
  animateSub,
  view,
  id,
  onSelectView,
  onActivate,
  onSelectSub,
  onActivateTopic,
}: TopicBarProps) {
  // At most two panes exist at a time — the active one, plus the one being
  // animated away while it travels. Law 4 covers the BARS, not the panes.
  const panes = useWindowedPanes(sub, animateSub);

  // THE DEPARTING PANE KEEPS THE VIEW IT HAD. `?view=` belongs to the leaf being
  // addressed, so when the leaf changes the outgoing pane would otherwise read
  // the new URL — which has no `?view=` for it — and snap its section back to
  // the first while still fully opaque and sliding out. Captured at the moment
  // of the switch and held for the duration, the same shape as `direction`.
  const viewOf = useRef(new Map<number, number>());
  viewOf.current.set(sub, view);

  // AND THE DEPARTING PANE KEEPS ITS DETAIL ID, for exactly the same reason and
  // with exactly the same shape. `/work/tasks/abc123` -> `/work/agents` leaves
  // the TASKS pane reading a URL with no id in it, so without this it would
  // snap from its detail back to its list while still fully opaque and sliding
  // out. Held per sub-item so each pane leaves looking like itself.
  const idOf = useRef(new Map<number, string | undefined>());
  idOf.current.set(sub, id);

  // `inert` is measured from this element's own motion token — see the hook.
  const barRef = useRef<HTMLDivElement>(null);
  const restingInert = useRestingInert(active, barRef);

  // Bound once so the windowed panes below read the same array the labels do.
  // `useWindowedPanes` only ever yields indices it was given, so a miss here is
  // impossible; the local is what lets the reads stay honest under the
  // frontend's `noUncheckedIndexedAccess`, which the shell's own tsconfig did
  // not set.
  const kids = topic.kids;

  // C1 — THE REAL GLYPHS. Bound here, beside `kids`, for the same reason `kids`
  // is: both arrays are indexed by the same `i` below and a mismatch has to be
  // impossible to write. `icons.test.ts` asserts the lengths agree with the IA.
  const TopicIcon = TOPIC_ICONS[topic.id];
  const leafIcons = LEAF_ICONS[topic.id];

  /**
   * C4 — ARROW KEYS ON A BAR LABEL, AND THE SCOPE IS THE WHOLE SAFETY ARGUMENT.
   *
   * =========================================================================
   * WHY IT IS A HANDLER ON THIS BUTTON AND NOT A `window` LISTENER
   * =========================================================================
   * The shell already has one global key listener — Shift+A / Shift+D in
   * `App.tsx` — and it is global safely BECAUSE it needs a modifier. Bare arrow
   * keys cannot be: they scroll the container, they move a caret in every
   * textarea on 31 leaves, they move the selection in every Radix menu, and
   * `.content` is now the scroller (R2) so hijacking them globally would break
   * scrolling on every page. Scoped to the five `.bar-label` buttons, the keys
   * are reachable exactly where the user is already navigating and nowhere else.
   *
   * =========================================================================
   * THE MODEL: LEFT/RIGHT ACROSS TOPICS, UP/DOWN WITHIN ONE — WAI-ARIA MENUBAR
   * =========================================================================
   * This is the menubar pattern, which is what the rail is: a horizontal row of
   * five top-level items, each revealing its own list. Left/right walk the row,
   * up/down walk the revealed list. The owner asked for exactly that split.
   *
   * RELATIVE TO THE FOCUSED BAR, AND FOCUS TRAVELS WITH THE ACTIVATION. The
   * alternative was relative to the RESOLVED topic, the way `step()` is, and it
   * is wrong here for one concrete reason: with focus parked on START's label,
   * every ArrowRight would resolve against whatever topic is currently open, so
   * the key would walk while the focus ring stood still and the two would
   * disagree after the first press. Moving focus to the target's own label keeps
   * "where am I" and "what does the next press do" the same question.
   *
   * THE FOCUS MOVE IS A DIRECT DOM CALL AND IT CANNOT MISS. Law 3 — everything
   * is side by side, nothing is ever removed — guarantees all five bars and all
   * five labels are in the document at all times, collapsed to zero width
   * rather than unmounted. So the query resolves in every one of the 20 chrome
   * states. Managing it through React state would mean a ref per bar plus an
   * effect that fires after the navigation, to land on the same element.
   *
   * NO WRAP AT EITHER END. START's ArrowLeft and SYSTEM's ArrowRight do
   * nothing, and `preventDefault` is NOT called in that case — so the key
   * reaches the page and still scrolls, rather than being silently eaten.
   */
  const onLabelKey = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    // A modifier means the user is asking for something else — Shift+A/D is the
    // shell's own, and Ctrl/Meta/Alt+Arrow are the browser's history and
    // word-motion bindings. Never shadow either.
    if (e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;

    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      const target = index + (e.key === "ArrowRight" ? 1 : -1);
      if (target < 0 || target >= IA.length) return;
      const next = IA[target];
      if (!next) return;
      e.preventDefault();
      e.stopPropagation();
      onActivateTopic(target);
      document
        .querySelector<HTMLElement>(`.bar[data-bar="${next.id}"] .bar-label`)
        ?.focus();
      return;
    }

    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const target = sub + (e.key === "ArrowDown" ? 1 : -1);
      if (target < 0 || target >= topic.kids.length) return;
      e.preventDefault();
      e.stopPropagation();
      // An INACTIVE bar has a remembered sub-item but no address, so stepping
      // it has to activate the topic AT that sub rather than select within a
      // section nobody is looking at. `onActivate` is the one that carries a
      // leaf index; `onSelectSub` assumes this bar is already the addressed one.
      if (active) onSelectSub(target);
      else onActivate(target);
      // Focus stays on THIS label — it is the same element and the same bar.
    }
  };

  return (
    <div
      className="bar"
      ref={barRef}
      data-bar={topic.id}
      /* THIS BAR'S POSITION IN THE RAIL, PUBLISHED FOR THE CASCADE.
         `.content`'s resting transform is `(--sh-i - --sh-active-i)` columns,
         clamped to one — see the stylesheet. That is the whole of what used to
         be `direction.ts` plus a driver computing an offset per switch: a fact
         each bar already knows about itself, written once, read by CSS.

         THERE IS NO `onPointerDown` HERE ANY MORE. It opened a speculative
         section change one frame before React knew, with a withdrawal path for
         every way a press can fail to become a click. The owner ruled the whole
         mechanism out on 2026-09-18; `App.tsx`'s click handler writes the rail's
         three resting attributes synchronously instead, which needs no
         prediction because a click has already happened. */
      style={{ "--sh-i": index } as React.CSSProperties}
      {...(active ? { "data-active": "" } : {})}
      onClick={(e) => {
        if (active) return;
        e.stopPropagation();
        onActivate();
      }}
    >
      {/* The fixed point of the whole transformation: (16 across, 32 along) in
          BOTH variants, so it is positioned absolutely and animates nothing but
          opacity. Everything else swings around it. */}
      {/* C1 — IT STAYS A `<span>` WITH THE GLYPH INSIDE, AND THAT IS TRAP 3 OF
          `937c7f7b` BEING AVOIDED RATHER THAN SURVIVED. `.bar-ico` is an
          element-keyed selector nowhere, but `.sb-icons i` IS one, so the rule
          applied to all three icon sets is the same: keep the slot element,
          nest the svg. The slot keeps its 16x16 box, so `.swing`'s hugged width
          — which `--sh-divider-h` is measured from — cannot move.
          `aria-hidden` because the label beside it already names the topic. */}
      <span className="bar-ico">
        <TopicIcon aria-hidden="true" />
      </span>

      {/* EVERY CONTROL IN THE RAIL IS A REAL `<button>` AS OF F2b.
          The chrome was mouse-only: measured, a Tab walk reached 3 stops in the
          whole frame — the skip link, the appearance toggle and the feedback
          button — and stop 4 was already inside a page. Five topics, 31 leaves
          and both chrome toggles had no keyboard path at all.
          `role="button" tabIndex={0}` was the alternative and it was rejected:
          it needs its own Enter/Space handler at every site, which is four more
          places to forget one. A real button gets activation, the disabled
          semantics and the accessibility role for free. `.sh-ctl` strips the UA
          chrome so the measured geometry does not move — see the stylesheet. */}
      <div className="swing">
        <button
          type="button"
          className="bar-label sh-ctl"
          // The bar's own onClick already activates on a pointer; this is the
          // keyboard path to the same action, and it must not fire twice.
          aria-current={active ? "true" : undefined}
          onKeyDown={onLabelKey}
          onClick={(e) => {
            if (active) return;
            e.stopPropagation();
            onActivate();
          }}
        >
          {topic.label}
        </button>
        <div className="kids">
          <div className="k-icons">
            {topic.kids.map((k, i) => {
              // Bound per row so the JSX below can render it as a component.
              // `?? TopicIcon` is unreachable while `icons.test.ts` passes and is
              // here only to satisfy `noUncheckedIndexedAccess`, the same reason
              // `kids` is bound to a local above.
              const LeafIcon = leafIcons[i] ?? TopicIcon;
              return (
              // An icon in a COLLAPSED bar routes to its own subnav entry: it
              // activates the topic AND selects that sub-item, which then
              // anchors the stagger.
              //
              // IT IS HIDDEN FROM THE TAB ORDER WHILE THE BAR IS ACTIVE, because
              // then it is the `.k-text` label that carries the same action and
              // the two groups share one grid cell — the invisible one lies on
              // top of the visible one. Two tab stops for one leaf would be the
              // defect this fix exists to remove, in the other direction.
              <button
                key={k}
                type="button"
                className="hit sh-ctl"
                data-i={i}
                title={k}
                aria-label={k}
                tabIndex={active ? -1 : 0}
                aria-hidden={active ? "true" : undefined}
                {...(i === sub ? { "data-on": "" } : {})}
                onClick={(e) => {
                  if (active) return;
                  e.stopPropagation();
                  onActivate(i);
                }}
              >
                <LeafIcon aria-hidden="true" />
              </button>
              );
            })}
          </div>
          <div className="k-text">
            {topic.kids.map((k, i) => (
              // A label in the ACTIVE bar switches subnav within the section.
              // Mirror of the icons above: out of the tab order while the bar is
              // collapsed, where it is invisible and inert to the pointer.
              //
              // NO `transitionDelay` HERE ANY MORE. Each span used to carry one,
              // computed from its distance to the clicked item, so the label you
              // pointed at appeared first and the rest radiated outward at 45ms a
              // step. The owner's #13 makes the menu points ONE BEAT of the section
              // change — phase 0 out, phase 4 in, together — so the fade moved up
              // to the `.k-text` / `.k-icons` pair the stylesheet already calls a
              // two-cell crossfade: one opacity per container instead of eight per
              // bar. MEASURED reason it had to go: DELIVER's eight children put the
              // last label 920ms after the commit, and R4 budgets the whole switch
              // at 650ms of visible motion.
              <button
                key={k}
                type="button"
                className="k-lbl sh-ctl"
                data-i={i}
                tabIndex={active ? 0 : -1}
                aria-hidden={active ? undefined : "true"}
                aria-current={active && i === sub ? "page" : undefined}
                {...(i === sub ? { "data-on": "" } : {})}
                onClick={(e) => {
                  if (!active) return;
                  e.stopPropagation();
                  onSelectSub(i);
                }}
              >
                {k}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* THE CONTENT-CONTAINER CONTRACT: the title lives INSIDE the container,
          not floating above it on the ground. pad 48/32/0/48, title, then 32,
          then the body. Every section inherits this rhythm for free. */}
      {/* `inert` SITS ON `.content`, NOT ON EACH `.pane`, and that is the same
          boundary the stylesheet already draws: `.bar[data-active] .content`
          is what flips `pointer-events` and `opacity`. One attribute per bar
          covers the pane AND the `.c-title` above it, so an inactive topic
          contributes nothing to the accessibility tree rather than contributing
          a heading with no reachable body. Width, display and every transition
          are untouched — Law 3 holds, nothing is removed and nothing is
          `display:none`. */}
      <div className="content" inert={restingInert}>
        {/* THE PROVIDER WRAPS THE PLATE **AND** THE PANES, AND THAT BOUNDARY IS
            THE WHOLE POINT. A leaf renders inside `.c-panes`; the plate renders
            above it. One provider spanning both is the only place a leaf can
            reach the plate from. ONE PER BAR, not one per app: Law 3 keeps five
            panes mounted, so an app-wide slot would let four invisible leaves
            fight the visible one for the title. See `PageTitle.tsx`. */}
        {/* `owner={sub}` IS LOAD-BEARING. The provider holds ONE section slot,
            and two panes are mounted while one travels — so the slot needs to
            say which leaf filled it. `sub` is this bar's current leaf index,
            which is exactly what distinguishes two leaves of one bar. See the
            `Published` note in `PageTitle.tsx` for the two defects it closes. */}
        <PageTitleProvider owner={sub}>
        <div className="c-inner">
          {/* THE TITLE IS THE LEAF'S, AND IT IS THE DOCUMENT'S ONE `<h1>` — R1.
              ---------------------------------------------------------------
              It used to print `topic.title`, the topic tagline, which read
              "Setup your personal okuro" on all eight SYSTEM leaves. The owner,
              2026-09-14: *"Title should be the leaf title … What is currently
              the title of the subpages should be the title: 'Scheduled' for
              example."*

              `sub` IS THE RIGHT INDEX FOR EVERY BAR, active or not. The active
              bar's `sub` is `resolved.leafIndex`, so the heading tracks the
              address — including `?view=` and `/{id}`, which address a SECTION
              or a DETAIL *of that leaf* and therefore keep its name. An
              inactive bar's `sub` is its own remembered leaf, which is exactly
              what its own mounted pane renders, so bar and heading never
              disagree.

              A `<div>` BECAME AN `<h1>` AND THAT IS THE POINT, not a detail:
              S1 ruled one `h1` per document, and `lib/page-context.ts:112`
              already derives the chat's page title from `h1?.innerText`. The
              four INACTIVE copies are inside `inert` `.content` (see below), so
              the frame contributes exactly one live heading.

              NO BACK-BUTTON INVENTION HERE. Detail views keep whatever back
              affordance they already have; the shell-level pattern is todo
              `29bd484f` and it is deliberately not this batch's. */}
          {/* THE HEADING MOVED INTO `SectionTitle`, WHICH IS THE PLATE.
              The owner ruled 2026-09-16 that the title sits on a blurred plate
              with content scrolling under it, and that section and detail
              headers are two components leaves and detail pages share. The
              `<h1>` is still the document's one heading — it moved, it was not
              duplicated.

              NO `actions` YET, and that is honest rather than unfinished: the
              right-hand slot takes the LEAF's own search / sort / filter / add,
              which each leaf owns today with its own state. Handing them over
              is per-leaf work; the plate and the slot exist now so that work has
              somewhere to land. */}
          {/* `PlateTitle` READS WHAT THE VISIBLE LEAF PUBLISHED and falls back
              to the shell's derived title when it published nothing — R1 still
              holds, a leaf overrides it only to say what the shell cannot know.
              The `actions` slot is no longer empty by construction: the channel
              a leaf hands its search / sort / filter / add through now exists,
              which is what was actually missing when that slot was called
              "honest rather than unfinished". */}
          {/* `topic`, NOT `active`: every bar publishes its title into its own
              slot now, and the SLOT carries activity. An inactive bar's `sub`
              is its own remembered leaf, so the title parked off-screen for
              this topic is already the one that has to slide in. */}
          <PlateTitle fallback={leafTitle(topic, sub)} topic={topic.id} />
          {/* `#main-content` IS A CONTRACT WITH TWO READERS, and it had to land
              on exactly one element. `lib/capture-snapshot.ts:75` renders it to
              a PNG for the chat hand-over and `lib/page-context.ts:13` derives
              the chat's section list from inside it; the old frame put the id on
              its single `<main>`. The shell has five content slots, one per bar,
              so the id goes on the ACTIVE one — the only element that is both
              unique and the right box. It excludes the sidebar, the pulse orb
              and the bar labels, which is what both readers want, and its child
              `.pane` is the scroller. */}
          <div className="c-panes" {...(active ? { id: "main-content" } : {})}>
            {panes.mounted.map((i) => (
              // WINDOWED. Every mounted pane carries `data-on` — at rest that is
              // exactly one, and mid-transition the slider drives the outgoing
              // one's opacity inline while it leaves.
              <div
                key={kids[i]}
                className="pane"
                data-i={i}
                data-on=""
                ref={(el) => panes.register(i, el)}
              >
                <LeafView
                  topic={topic.id}
                  leaf={(kids[i] ?? "").toLowerCase()}
                  // F5 — the pane is "on screen" only if its BAR is the active
                  // one AND it is the addressed sub-item. The second term is
                  // what covers the departing pane during a sub-item slide: it
                  // is mounted and visible for 650ms, and it is already leaving,
                  // so it has no business starting another poll cycle.
                  active={active && i === sub}
                  id={i === sub ? id : idOf.current.get(i)}
                  view={i === sub ? view : (viewOf.current.get(i) ?? 0)}
                  // `active &&`, NOT just `i === sub`. An INACTIVE bar also has
                  // exactly one mounted pane with `i === sub`, so without the
                  // first term that pane was handed a callback built from the
                  // ACTIVE bar's `resolved.leafIndex` — a section click inside a
                  // 48px-wide bar would have navigated to this topic with
                  // another topic's leaf index. Unreachable by pointer and, from
                  // this batch's F2a, by keyboard too; corrected at the cause
                  // rather than left resting on two separate guards.
                  onSelectView={active && i === sub ? onSelectView : undefined}
                />
              </div>
            ))}
          </div>
        </div>
        </PageTitleProvider>
      </div>
    </div>
  );
}
