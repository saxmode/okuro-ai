// SPDX-License-Identifier: Apache-2.0
/**
 * THE SHELL. React owns chrome, the URL owns location, CSS owns motion.
 *
 * THE SHELL IS NOT A ROUTE — IT IS THE FRAME. There is deliberately no
 * `<Routes>` around it. Swapping the shell through a route element would
 * unmount and remount the topic bars on every navigation, and Law 4 says the
 * bars are never unmounted: the slide needs both of them present to move. So
 * the shell renders ONCE and reads the pathname; the route selects which pane
 * shows inside a frame that never goes away.
 *
 * WHAT REACT IS ALLOWED TO DO, still a short list:
 *   1. set `data-topic` / `data-panel` / `data-content` on one element
 *      — the first now DERIVED from the path, not stored
 *   2. set `--sh-divider-h` once per bar
 *   3. set the per-span stagger delays
 *   4. park and release a pane for a directional slide, in a LAYOUT effect
 *
 * `motion` is not a dependency: a JS animation driving a value CSS already
 * drives is Law 2, the failure that cost two separate bugs in the mockup.
 */

import { startTransition, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useNavigationType } from "react-router";
import { IA, type TopicId } from "./ia";
import { installBackdropGlass } from "./lib/backdrop-glass";
import {
  loadChrome,
  loadLastLocation,
  loadRemembered,
  rememberLeaf,
  rememberLocation,
  saveChrome,
  toggleContent,
  togglePanel,
  type ChromeState,
} from "./state";
import { pathFor, pathWithView, resolveEntry, shouldAnimate, stepPath } from "./routes";
import { applyThemeMode, currentThemeMode, type ThemeMode } from "@/lib/theme";
import { useDividerHeights, useStateColumnWidth } from "./hooks/useSlide";
import { TopicBar } from "./components/TopicBar";
import { slotId } from "./components/PageTitle";
import { useBandHeight } from "./hooks/useBandHeight";
import { Chrome, Sidebar } from "./components/Sidebar";
import { CHROME_PLATE_H, TOKENS, TOP_LINE_Y, rightGutter, restingStates } from "./geometry";
import { TOPIC_COUNT } from "./ia";
import type { Resolved } from "./routes";

/**
 * VERIFICATION IS MEASUREMENT, NOT SCREENSHOTS.
 *
 * A still frame hid four separate defects in this build, so the harness gets a
 * DECLARED expectation table to compare against. Read-only — the shell never
 * consults it.
 *
 * IT LIVES HERE NOW, not in an entry file. The shell had its own `main.tsx` and
 * published the static half of this table at module load; inside `frontend/`
 * the entry is the app's and the shell is a component, so the whole table is
 * published from the one effect below. The consequence is deliberate: the
 * expectations are recomputed on every resolution, so a window resize no longer
 * leaves the harness comparing against the width the page was loaded at.
 */
declare global {
  interface Window {
    __okuroShell?: {
      navigate?: (to: string) => void;
      setTopic?: (id: TopicId, sub?: number) => void;
      step?: (delta: number) => void;
      location?: string;
      resolved?: Resolved;
      expected?: ReturnType<typeof restingStates>;
      topLineY?: number;
      chromePlateH?: number;
      gutter?: number;
      topics?: number;
      tokens?: typeof TOKENS;
    };
  }
}

export function App() {
  const { pathname, search } = useLocation();
  const navigate = useNavigate();
  const navigationType = useNavigationType();

  const [chrome, setChrome] = useState<ChromeState>(loadChrome);
  const planeRef = useRef<HTMLDivElement>(null);
  /* THE FRAME ITSELF, because a bar click writes the rail's three resting
     attributes and `--sh-active-i` on it one frame before React commits them. A ref rather than
     `getElementById("app")`: the element is rendered right here, so reaching
     for it by id would be a second handle on a node this component holds. */
  const appRef = useRef<HTMLDivElement>(null);
  /* The teardown for the in-flight switch flag — see `openRail`. A ref rather
     than state because nothing renders from it: it exists so a second switch
     can cancel the first one's listener instead of stacking on top of it. */
  const switchEndRef = useRef<(() => void) | null>(null);

  // The memory is read ONCE per resolution and never kept in state, which is
  // what stops it becoming a second source of truth alongside the path.
  // `search` is load-bearing, not cosmetic: live `/assets?view=media` is the
  // MEDIA leaf while bare `/assets` is ASSETS — one path, two destinations,
  // distinguished only by the query.
  const resolved = useMemo(
    () => resolveEntry(pathname, loadRemembered(), search, loadLastLocation()),
    [pathname, search],
  );

  const previousTopic = useRef<TopicId | null>(null);

  useDividerHeights(planeRef);
  // The state column is measured, not pinned — see the hook for why a width
  // taken on one letter case stopped describing either kit once case and type
  // size both came from the design system.
  useStateColumnWidth();

  /* THE ACTIVE TOPIC'S POSITION IN THE RAIL — `--sh-active-i`, and it is the
     ONE fact the whole section change reads. The plate's `left`, the plate's
     notch, the track's `right`, the right gutter, every page column's resting
     offset and every title slot's are all expressions in it. It is published on
     `.app` in the JSX below and written again, synchronously, by `go` — see
     there for why both. */
  const activeIndex = IA.findIndex((t) => t.id === resolved.topic);

  const animate = shouldAnimate(previousTopic.current, navigationType);

  // Record where we ended up, for the next bare-topic resolution.
  useEffect(() => {
    if (!resolved.redirect) {
      previousTopic.current = resolved.topic;
      rememberLeaf(resolved.topic, resolved.leafIndex);
      // `/` resolves against this. Stored WITH the query so a returning visitor
      // gets their section back too, not just the leaf.
      rememberLocation(pathname + search);
    }
  }, [resolved.redirect, resolved.topic, resolved.leafIndex]);

  useEffect(() => saveChrome(chrome), [chrome]);

  /**
   * THE APPEARANCE, AND IT IS NOT PART OF `chrome`.
   *
   * `chrome` is the shell's own two-axis state and it persists under the
   * shell's own key. The appearance persists under `okuro.theme-mode`, which is
   * the LIVE app's key — the whole point being that the choice survives the p5
   * cutover. Folding it into ChromeState would mean writing the user's
   * appearance into a shell-private blob, and at the cutover everyone would
   * silently start again at dark.
   *
   * The initial value is read pre-mount by main.tsx, so `currentThemeMode()`
   * here reports what is already painted rather than deciding it. React only
   * mirrors it, which keeps the DOM the single authority — the same discipline
   * every other attribute in this shell follows.
   */
  const [appearance, setAppearance] = useState<ThemeMode>(currentThemeMode);

  /**
   * THE RAIL'S THREE RESTING ATTRIBUTES, WRITTEN ON THE CLICK'S OWN FRAME.
   *
   * React's commit for a bar click is ONE long main-thread task — measured
   * 176-245ms in WebKitGTK, 35-49 in Chromium — and nothing paints inside it.
   * Written only by the commit, the transition therefore starts after that
   * task, which is the "jumps (onclick) then animation" the owner reported.
   *
   * THIS IS NOT A PREDICTION AND HAS NO WITHDRAWAL. It ran from `pointerdown`
   * once, before anyone had clicked anything, and every way a press can fail to
   * become a click then needed an answer — a capturing `pointerup` listener, a
   * `pointercancel` listener, a reversal window, a timer. A click has already
   * happened, so there is nothing to take back.
   *
   * IT WRITES WHAT THE CASCADE WILL SETTLE ON ANYWAY. React's commit sets the
   * same four values from the same resolution a moment later, so no computed
   * value changes at the commit and no transition restarts. If the router
   * resolves somewhere else after all, the values simply change again and every
   * surface travels on from where it had got to — same curve, same duration,
   * no snap, because there is only ever one mechanism.
   *
   * KEYBOARD AND PROGRAMMATIC PATHS SKIP IT and are correct without it: they
   * reach the same resting rules through React's own commit, one commit later.
   */
  const openRail = useCallback((topic: TopicId) => {
    /* Four writes: the frame's topic, the index every geometric member reads,
       which bar is open, and which title slot is on. Nothing else. */
    const app = appRef.current;
    const index = IA.findIndex((t) => t.id === topic);
    if (!app || index < 0) return;
    app.dataset.topic = topic;
    app.style.setProperty("--sh-active-i", String(index));
    for (const bar of app.querySelectorAll<HTMLElement>(".bar")) {
      bar.toggleAttribute("data-active", bar.dataset.bar === topic);
    }
    const on = slotId(topic);
    for (const slot of app.querySelectorAll<HTMLElement>(".c-slot")) {
      slot.toggleAttribute("data-on", slot.id === on);
    }

    /* A FIFTH WRITE: THE SWITCH SAYS WHILE IT IS RUNNING. The owner, 2026-09-19:
       *"in transition phases of the section bars: hover effect disabled."*
       The bar's hover wash is the only hover on this axis and it fires on a bar
       that is on its way somewhere, which reads as a flicker under the pointer.

       THE END COMES FROM THE MOTION, NOT FROM A TIMER. `flex-grow` is the one
       property here that spans the whole phase, so its `transitionend` IS the
       end of the switch — a `setTimeout` would be this file's duration written
       a second time, and the two would drift the moment a token moves. The
       listener is on the frame and one-shot; the ref clears a previous one so
       a switch interrupted mid-flight cannot leave the flag standing. */
    switchEndRef.current?.();
    app.dataset.switching = "";
    const done = (e: TransitionEvent) => {
      if (e.propertyName !== "flex-grow") return;
      switchEndRef.current?.();
    };
    switchEndRef.current = () => {
      app.removeEventListener("transitionend", done);
      delete app.dataset.switching;
      switchEndRef.current = null;
    };
    app.addEventListener("transitionend", done);
  }, []);

  /**
   * ONE FUNNEL FOR EVERY SECTION ACTIVATION — a bar's body, its label, its leaf
   * glyphs and ArrowLeft/ArrowRight all arrive here.
   *
   * `startTransition` KEEPS THE COMMIT OFF THIS FRAME. The rail has already
   * started moving by the line above; marking the route change non-urgent lets
   * React yield rather than render the incoming leaf inside the same task and
   * stall the first frames of a motion that has already begun.
   */
  const go = useCallback(
    (topic: TopicId, leaf: number) => {
      openRail(topic);
      startTransition(() => navigate(pathFor(topic, leaf)));
    },
    [navigate, openRail],
  );

  const step = useCallback(
    (delta: number) => {
      if (resolved.redirect) return;
      const to = stepPath(resolved.topic, delta, loadRemembered());
      if (to) navigate(to);
    },
    [navigate, resolved],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      /* CMD / CTRL / SUPER + ARROW STEPS THE SECTION — the owner, 2026-09-19.
         All three modifiers, because the same build runs on three desktops and
         the key under the user's thumb has a different name on each: `metaKey`
         is Command on macOS and Super on most Linux desktops, `ctrlKey` is what
         Windows and Linux users reach for. Accepting any one of them is what
         makes the binding the same gesture everywhere rather than three
         bindings that happen to overlap.

         SHIFT + A / D IS GONE — the owner, 2026-09-19: *"Shift + D is also
         switching and Shift+A also. This interferes with writing notes. Should
         not be implemented."* The previous note here argued it should stay
         because it was the binding the app already had; that reasoning missed
         what the binding actually was. Shift+A and Shift+D are not a chord at
         all — they are how a capital A and a capital D are typed. Every note,
         every search field and every title containing one of those letters
         moved the user to another section.

         IT ALSO HAD NO FIELD GUARD, which is why the arrow binding below
         carries one and this one could not be rescued by adding it: a guard
         would only have covered inputs, and the shell has editable surfaces
         that are neither INPUT nor TEXTAREA. A binding that cannot coexist
         with typing has to go, not be fenced. Cmd/Ctrl+Arrow remains. */
      const stepper = e.metaKey || e.ctrlKey;
      if (stepper && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
        /* NOT when the caret is in a field: Cmd/Ctrl+Arrow is word-wise or
           line-wise motion in every text control, and stealing it there would
           break typing to add navigation. */
        const el = document.activeElement as HTMLElement | null;
        const tag = el?.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || el?.isContentEditable) return;
        e.preventDefault();
        step(e.key === "ArrowLeft" ? -1 : 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step]);

  // Measurement handle. Read-only; the shell never consults it.
  useEffect(() => {
    window.__okuroShell = {
      ...(window.__okuroShell ?? {}),
      navigate: (to: string) => navigate(to),
      setTopic: (id: TopicId, sub?: number) => navigate(pathFor(id, sub ?? 0)),
      step,
      location: pathname,
      resolved,
      // The declared half — see the `declare global` above for why it is
      // computed here rather than once at module load.
      //
      // THE FOURTH ARGUMENT IS MEASURED, NOT DECLARED. `html` reserves a
      // scrollbar gutter (`globals.css:829`) that no token names and that the
      // two engines size differently, so the width `.app` really lays out in is
      // the viewport LESS that reservation. `body` is `.app`'s containing
      // block, which is where the reservation lands — Chromium 2, WebKitGTK 0,
      // both read here rather than assumed. See `restingStates`.
      expected: restingStates(
        window.innerWidth,
        TOPIC_COUNT,
        false,
        window.innerWidth - document.body.clientWidth,
      ),
      topLineY: TOP_LINE_Y,
      chromePlateH: CHROME_PLATE_H,
      gutter: rightGutter(),
      topics: TOPIC_COUNT,
      // The MIRROR ITSELF, published so the drift guard can compare the numbers
      // this bundle actually shipped against the CSS the browser actually
      // computed. Reading the TS source text instead would test the repository,
      // not the build. See `tests/web/test_shell_geometry_live.py`.
      tokens: TOKENS,
    };
  }, [navigate, step, pathname, resolved]);

  /**
   * AN INCOMPLETE ADDRESS IS CANONICALISED WITHOUT UNMOUNTING THE SHELL.
   *
   * THIS USED TO BE `if (resolved.redirect) return <Navigate replace />`, and
   * that one line cost the transition on every entry through a non-canonical
   * address — a reload or bookmark at `/work`, Back or Forward onto a topic
   * root, any deep link that omits the leaf.
   *
   * WHAT WAS MEASURED, per frame of `navigate('/work')`:
   *
   *     pre  n=5 survived=5  grow=[1,0,0,0,0]
   *     f0   n=0 survived=0                    <- the whole plane is GONE
   *     f3   n=5 survived=0  grow=[0,0,1,0,0]  <- five NEW bars, final geometry
   *
   * Returning a different element unmounts everything below it. The five bars
   * that come back are NEW nodes, and a new node has no previous computed
   * value, so CSS has nothing to interpolate FROM: the rail's growth jumps
   * 1 -> 0 with no transition, and a 56-70ms long task plus 64.6ms of layout
   * lands in the same frame. A pure-CSS control that moves `data-active` by hand
   * interpolates perfectly — the stylesheet was never at fault, the remount was.
   *
   * WHY RENDERING THE SHELL ANYWAY IS CORRECT, and this is the part that makes
   * the fix safe rather than merely cheaper: EVERY redirect branch in
   * `resolveEntry` already carries the TARGET's own resolution, not the typed
   * address's. `routes.ts:149` and `:157` spread `resolveEntry(<target>)`
   * wholesale; `:167` and `:174` set `topic`/`leafIndex` to what the target
   * would resolve to. So this render is byte-identical to the one the redirect
   * would have produced — there is no frame of wrong content to flash.
   *
   * `useEffect`, NOT `useLayoutEffect`, AND THAT IS F10 — MEASURED 2026-09-18.
   * ------------------------------------------------------------------------
   * It was a layout effect, to correct the address before paint. A layout
   * effect in a CHILD runs before its parent's, and `BrowserRouter` subscribes
   * to the history in a layout effect of its own — so on the FIRST MOUNT this
   * navigate reached the browser's history with nobody listening, and React's
   * router state never moved. Measured in Chromium against the dev server,
   * entering at `/`, `/work` and `/know/memory`, 2.5s after load:
   *
   *     location.pathname               /work/tasks   (the browser DID move)
   *     useLocation().pathname          /             (React did NOT)
   *     resolved.redirect               "/work/tasks" (still set, forever)
   *
   * `resolved.redirect` therefore stays truthy for the whole session, so
   * `previousTopic` below is never recorded and the first user switch fails
   * `shouldAnimate` at its NULL-PREVIOUS branch. That axis now reaches only the
   * SUB-ITEM slide, since the section change is resting CSS rules that need no
   * previous topic at all — but the bug is the same bug and the fix is the same
   * fix, and reverting this line reopens it for the sub-item slide.
   *
   * IT IS FIRST-MOUNT ONLY, which is what makes this the cause rather than a
   * coincidence. The same redirect issued LATER lands perfectly — measured from
   * a canonical entry, `navigate('/work')` and `navigate('/know/memory')` both
   * left `location.pathname`, `useLocation().pathname` and `resolved.redirect`
   * in agreement. The subscription exists by then.
   *
   * NOT `shouldAnimate`'s REPLACE BRANCH, which was the standing hypothesis
   * (memory `898fb846`, where the mechanism is recorded as unexplained). That
   * branch is correct and is left alone: a redirect IS a resolution rather than
   * a move, and `navigationType` on the first user switch measured PUSH.
   *
   * WHAT THE PASSIVE EFFECT COSTS: the non-canonical address is in the URL bar
   * for one paint. Nothing wrong is ever painted — every redirect branch in
   * `resolveEntry` already carries the TARGET's resolution, so the shell
   * rendered at `/work` is byte-identical to the one rendered at `/work/tasks`.
   * `replace` is unchanged and still keeps it out of history — pressing Back
   * from /work/agents must not step through /work.
   *
   * IT CANNOT LOOP. Navigating to the canonical path makes `resolveEntry`
   * return no `redirect`, so the effect does not fire again; the dependency is
   * the redirect string itself, so a re-render with the same value is inert.
   */
  useEffect(() => {
    if (resolved.redirect) navigate(resolved.redirect, { replace: true });
  }, [resolved.redirect, navigate]);

  /**
   * GLASS FOR AN ENGINE THAT WILL NOT PAINT `backdrop-filter`.
   *
   * A no-op in Chromium: the module returns immediately unless `index.html`
   * marked the engine `data-backdrop="none"`, so the browser keeps the real
   * property and pays nothing. It is mounted here rather than in `Frame`
   * because it reaches for `.top-plate`, which this component renders, and it
   * must outlive every route change — its own observers follow the active bar.
   */
  useEffect(() => installBackdropGlass(), []);

  /**
   * THE BAND'S HEIGHT FOLLOWS ITS CONTENT — A-3 #11, and the plate's geometry
   * follows the band's.
   *
   * Mounted here for the same reason the glass is: it reaches for `.top-plate`,
   * which this component renders, and it has to outlive every route change.
   * `useBandHeight.ts` is where the whole mechanism is argued — including why
   * exactly one length is measured and why the one-row/two-row DECISION is not.
   */
  useBandHeight(resolved.topic);

  return (
    <div
      className="app"
      id="app"
      ref={appRef}
      data-topic={resolved.topic}
      data-panel={chrome.panel}
      data-content={chrome.content}
      /* `--sh-active-i` — HOW FAR THE OPEN COLUMN SITS FROM THE LEFT, IN
         COLLAPSED BARS, AND THE ONE FACT THE SECTION CHANGE IS BUILT ON.
         ---------------------------------------------------------------------
         The plate's `left`, the plate's notch, the track's `right`, the right
         gutter and every page column's and title slot's resting offset are all
         expressions in it. CSS knows every width in the rail; it cannot know
         which bar is open.

         IT IS BACK IN THE JSX, and the objection that moved it out is answered
         rather than ignored. It was moved to the driver because written here it
         landed at the COMMIT while the driver had already started moving other
         things — so the plate travelled on a different clock from the bars, and
         `.swing`'s R5 stacking depends on those two edges tracking the active
         column frame by frame. Nothing starts before the commit any more except
         `go` above, which writes THIS property and the two attributes together,
         in one statement, one frame earlier. The per-frame identity holds
         because every reader moves on the same instant and the same clock. */
      style={{ "--sh-active-i": activeIndex } as React.CSSProperties}
    >
      <Sidebar
        collapsed={chrome.panel === "collapsed"}
        onToggle={() => setChrome((c) => ({ ...c, panel: togglePanel(c.panel) }))}
      />

      <div className="plane" id="plane" ref={planeRef}>
        {IA.map((t, i) => (
          <TopicBar
            key={t.id}
            topic={t}
            index={i}
            active={t.id === resolved.topic}
            // Each bar shows its own remembered leaf; only the ACTIVE one is
            // addressed by the path.
            sub={t.id === resolved.topic ? resolved.leafIndex : (loadRemembered()[t.id] ?? 0)}
            animateSub={animate}
            view={t.id === resolved.topic ? resolved.viewIndex : 0}
            // The detail segment addresses ONE leaf under ONE topic, so only
            // the active bar can carry it. Same rule as `view`.
            id={t.id === resolved.topic ? resolved.id : undefined}
            // `search` is carried so a section click keeps the open document —
            // see `pathWithView`. `resolved.leafIndex` is the ACTIVE bar's, and
            // TopicBar only forwards this to the active bar's addressed pane.
            // A SECTION CHANGE TO THE SECTION YOU ARE ALREADY ON IS NOT A
            // NAVIGATION, and without this guard it burnt a history entry that
            // Back could not get past. Found in the p4 DELIVER pass on PEOPLE,
            // and the mechanism is general: Radix `Tabs` defaults to
            // `activationMode="automatic"`, so ONE mouse click on a trigger
            // fires `onValueChange` twice — once on focus, once on the click.
            // Measured at 1366, controlled Radix Tabs vs HEALTH's `Segmented`:
            //
            //   PEOPLE, one click on DETAILED SETTINGS   history.length 2 -> 4
            //   then Back                                 ?view=directory  (unchanged)
            //   HEALTH, one click on STORAGE              history.length 2 -> 3
            //   then Back                                 ''               (correct)
            //
            // The guard is here rather than in the page because every leaf that
            // drives Radix Tabs from the section index has the same shape, and
            // because a no-op navigation is wrong for ANY caller — a page that
            // syncs its section in an effect would have done the same thing.
            //
            // IT COMPARES THE TARGET AGAINST THE LIVE URL, NOT AGAINST
            // `resolved`, AND THAT IS THE WHOLE FIX. The first version read
            // `resolved.viewIndex` and changed nothing: both `onValueChange`
            // calls land in ONE event turn, so React has not re-rendered
            // between them and both closures still see the old index. The
            // address bar HAS moved by then — react-router's push is
            // synchronous — so the live URL is the only witness that is already
            // current when the duplicate arrives.
            onSelectView={(v) => {
              const target = pathWithView(t.id, resolved.leafIndex, v, search);
              if (target === window.location.pathname + window.location.search) {
                return;
              }
              navigate(target);
            }}
            onActivate={(sub) => go(t.id, sub ?? loadRemembered()[t.id] ?? 0)}
            onSelectSub={(leaf) => go(t.id, leaf)}
            // C4 — ArrowLeft/ArrowRight. The BAR does the clamping and the
            // focus move, because it knows its own neighbours; this only has to
            // answer "activate the topic at index n, at its remembered leaf",
            // which is the same thing a pointer click on that bar already does.
            onActivateTopic={(n) => {
              const target = IA[n];
              if (target) go(target.id, loadRemembered()[target.id] ?? 0);
            }}
          />
        ))}
      </div>

      {/* THE TOP CHROME SURFACE — menu strip AND page header, ONE element because
          two backdrop-filters cannot blur across the boundary between them. It is a
          SIBLING of `.plane` rather than a child of the active bar because
          `.bar` is `overflow:hidden` and the strip has to reach past the topic
          bars to the window's right edge. Decorative: `aria-hidden`, and
          `pointer-events:none` in the stylesheet, so nothing under it loses a
          click. It is no longer `aria-hidden`: the leaf titles PORTAL into it
          (see `PageTitle.tsx`), so hiding it would hide the heading from every
          reader. The decorative layers inside it are hidden individually
          instead.

          IT CARRIES FIVE `<h1>`s, NOT ONE, and the sentence here used to say
          one. Since the track landed, every bar publishes its own title and all
          five live in this element; four of them are `inert`, so the
          ACCESSIBILITY TREE still sees exactly one heading. The conclusion is
          unchanged and the reason is better: `aria-hidden` here would hide the
          one heading that is exposed, and `inert` on the four slots is what
          keeps the other four from being a second, third and fourth.

          THE FIVE SLOTS ARE RENDERED HERE, NOT BY THE BARS, and that is what
          makes them one track rather than five private boxes. Each bar portals
          its own title into the slot that carries its topic id; this component
          owns where the slots ARE and which one is on, which is the same split
          every other axis in this file uses — React sets one attribute, the
          stylesheet reads it.

          THE ORDER IS THE IA'S, and it is the order the track slides through.
          Nothing reorders: five keyed children, mounted once, exactly as Law 4
          keeps the five bars mounted.

          `inert` ON THE FOUR THAT ARE NOT ON. The portalled title leaves its
          bar's DOM subtree, so neither `.content`'s own `inert` nor its
          `content-visibility:hidden` reaches it — four invisible titles would
          otherwise sit in the tab order with their leaves' controls in them. It
          is applied IMMEDIATELY rather than one motion duration late, unlike
          the bar's own: until today the outgoing title was UNMOUNTED at this
          instant, so anything focused inside it already lost focus. Immediate
          is no worse than what shipped, and it needs no second timer.

          THE MIRROR ALSO LIVES IN HERE. `lib/backdrop-glass.ts` inserts its
          `.sh-glass` clone as this element's FIRST child on engines that will
          not paint `backdrop-filter`. React does not know about that node and
          does not need to: it never reorders these five, and the glass sits at
          z-index -2 underneath them. */}
      <div className="top-plate sh-plate" id="top-plate">
        <div className="c-track">
          {IA.map((t) => (
            <div
              key={t.id}
              className="c-slot"
              id={slotId(t.id)}
              /* This slot's position in the track, read by its own resting
                 transform exactly as a bar's `--sh-i` is — see `.c-slot`. */
              style={{ "--sh-i": IA.findIndex((x) => x.id === t.id) } as React.CSSProperties}
              {...(t.id === resolved.topic ? { "data-on": "" } : {})}
              inert={t.id !== resolved.topic}
            />
          ))}
        </div>
      </div>

      {/* THE ONE SURFACE THE DESKTOP WINDOW CAN BE DRAGGED BY.
          `web_launcher.py` opens the window `frameless=True`, so the OS draws no
          title bar and the only way to move it is pywebview's own drag region:
          it attaches a `mousedown` handler to `document.body` unconditionally on
          every platform (`webview/js/customize.js:89`) and starts the move when
          the event's ancestor chain reaches an element matching
          `.pywebview-drag-region` (`webview/__init__.py:122`). Until now the app
          had none, so the window was immovable on GTK, Cocoa AND WebView2 — one
          element fixes all three, because this path is DOM-based and the native
          ones are gated off by `easy_drag=False`.

          IT IS EMPTY AND IT STAYS EMPTY. The handler walks UP from the event
          target, so anything focusable in here would start a window move instead
          of doing its own job. Never give this element children.

          WHY THIS BAND AND NOT THE PLATE. `.top-plate` is `pointer-events:none`
          and the leaf title portals INTO it with its own buttons and inputs —
          both disqualifying. Measured instead, in all four resting states: the
          only region of the chrome that is never interactive is the strip above
          the top line's hit targets, right of the sidebar. Everything on the top
          line centres on y=40 with a 40px hit area, so it begins at y=20; the
          sidebar's whole 64px head is itself a button, which is why the left
          edge is the plane's and not the frame's. */}
      <div className="pywebview-drag-region" aria-hidden="true" />

      <Chrome
        maxed={chrome.content === "max"}
        appearance={appearance}
        onToggleMax={() => setChrome((c) => ({ ...c, content: toggleContent(c.content) }))}
        onToggleAppearance={() => {
          // applyThemeMode writes BOTH keys and persists; React then mirrors.
          // No reload, no re-render of the plane: every colour in the shell is
          // a var() onto an engine name, so the attribute flip repaints through
          // the cascade alone.
          const next: ThemeMode = appearance === "dark" ? "light" : "dark";
          applyThemeMode(next);
          setAppearance(next);
        }}
      />
    </div>
  );
}
