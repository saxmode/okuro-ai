// SPDX-License-Identifier: Apache-2.0
/**
 * THE SIDE PANEL — 300px open, 72px collapsed, and independent of content-max.
 *
 * 72 IS THE PANEL COLUMN, NOT THE 48 OF A COLLAPSED BAR. This line said 48
 * until 2026-09-17 and was the third instance of one class — prose restating a
 * token — alongside `geometry.ts` and its two test assertions. The numbers live
 * in `styles/tokens.css` (`--sh-sidebar-open`, `--sh-sidebar-collapsed`); this
 * sentence is a signpost and the stylesheet is the authority.
 *
 * Collapsed, the panel IS a bar: the arrow stays on the top line at 16 across,
 * the blob alone centres vertically, and the icon stack rotates a quarter-turn
 * about the FIRST icon's centre while translating to the bottom. All of that is
 * CSS keyed off `data-panel` on the shell; this component is markup only.
 */

import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
/* THREE GLYPHS, NOT EIGHT. Maximize2, Moon, MoreHorizontal, RotateCw and Sun
   moved to `Corner.tsx` with the controls they draw (2026-09-19) and were left
   imported here, along with `hardResetAndReload`, which moved with them. What
   remains is the window trio this file still owns. */
import { Minus, Square, X } from "lucide-react";
import { PANEL_ICONS, ROW_CHEVRON } from "../icons";
import { PIN_SLOTS, pinCatalogue, resolvePin, usePins } from "./Pins";
import { useDisclosure } from "../hooks/useDisclosure";
import { pickQuote, type Quote } from "@/lib/quotes";
import { ActivityMeter } from "./ActivityMeter";
import { BriefList } from "./BriefList";
import { Blob } from "./Blob";
import { Corner } from "./Corner";
import { useShellPulse } from "./PulseData";

/* TWENTY SECONDS, AND THE PANEL NO LONGER MATCHES `QuoteDisplay`.
   The owner, 2026-09-19: *"quote rotation is too slow. was faster before."*

   THE INTERVAL WAS NEVER THE THING THAT CHANGED — it was 45s here and 45s in
   `components/pulse/quote-display.tsx` (`ROTATION_MS`), which is what the line
   this replaces was pointing at. What changed at the cutover is the REVEAL: the
   old component typed the text out character by character, so a new quote began
   arriving the instant the cycle turned, while this one crossfades whole. Same
   pace, far less evidence of it. The owner was asked which of the two he meant
   and chose the interval, so the fade stays as it is and the cycle halves-plus.

   20_000 IS MINE, NOT HIS — he picked the axis, not the number. It is the
   largest step that still turns the quote inside a normal glance at the panel,
   and it leaves the 650ms crossfade three percent of the cycle rather than one
   and a half. Move it if it reads as busy; nothing else depends on the value.

   `QuoteDisplay` keeps 45s. The two surfaces are no longer on one pace, which
   is fine while the pulse panel is the legacy one — if it ever returns to the
   shell, one of these two constants has to lose. */
const QUOTE_ROTATION_MS = 20_000;

type RowKey = "briefs" | "messages" | "activity";

/**
 * THE FOUR WINDOW CONTROLS THE CUTOVER DROPPED, PORTED BACK VERBATIM.
 *
 * The owner, 2026-09-17: *"in the last version on the top right there were five
 * icons from the right: close, minimize, maximize, hard-refresh, light/dark and
 * maximize content."* The shell kept two of those six — content-max and
 * appearance — and left two inert placeholders where four actions had been.
 * `components/shell/window-chrome.tsx` went with the old chrome and took them.
 *
 * HARD REFRESH IS NOT A RELOAD, AND THAT IS WHY IT HAD TO COME BACK FIRST.
 * okuro registers a caching service worker that intercepts every fetch, and the
 * app runs in a pywebview window with no DevTools and no working Ctrl+Shift+R —
 * so a plain reload can serve a stale bundle forever, which is exactly the state
 * the owner was stuck in. The sequence is the old file's, unchanged: unregister
 * every service worker, delete every Cache Storage entry, then navigate with a
 * cache-busting param so even an HTTP-cached index.html is bypassed.
 *
 * THE THREE NATIVE ONES GO THROUGH THE pywebview BRIDGE and are no-ops in a
 * browser tab, which is correct — a page cannot close its own window there.
 */
type NativeAction = "minimize" | "toggle_maximize" | "close";

function nativeWindow(action: NativeAction): void {
  const api = (window as unknown as { pywebview?: { api?: Record<string, () => Promise<void>> } })
    .pywebview?.api;
  void api?.[action]?.().catch((err) => console.warn(`[winctl] ${action} failed`, err));
}

/* TWO HOOKS STOOD HERE, AND THE COLUMN RETIRED BOTH.
 *
 *   useMeasuredInfoHeight  a ResizeObserver on `.sb-info` that republished
 *                          its height as `--sh-sb-info-content`, because two
 *                          absolutely-positioned neighbours were anchored on
 *                          it and had no other way to know where the block
 *                          began.
 *   useDrawerInMotion      a second effect that stripped `top` and `height`
 *                          from those neighbours' transition lists while a
 *                          drawer animated, because each re-measure restarted
 *                          a 650ms ease toward a still-moving target. That is
 *                          the jump the owner reported on 2026-09-19 for the
 *                          blob and the icon row.
 *
 * `.sidebar` is a flex column now (see `shell.css`), so the head, the blob,
 * the icon row and the info block each take their own space and the browser
 * does that arithmetic once per layout. Nothing measures a sibling, so
 * nothing restarts a transition, so there is nothing to suppress. */

/**
 * THE QUOTE ROTATES AGAIN — BUT `QuoteDisplay` ITSELF STAYS UNMOUNTED, AND THAT
 * IS A GEOMETRY DECISION, NOT A PREFERENCE.
 *
 * The owner ruled 2026-09-16: rotating, not the hardcoded Kernighan line. What
 * comes back is `pickQuote` — the DATA. The component does not, because it
 * carries a layout that fights two of the panel's invariants:
 *
 *   - it measures its own content and TRANSITIONS ITS HEIGHT. `.sb-quote b` is
 *     pinned to exactly three lines precisely so `--sh-sb-info-content` stops
 *     depending on content (`tokens.css`, Q1 2026-09-14 — the quote wrapping was
 *     the "nasty" jump that put `.sb-icons` on top of the DAILY BRIEFS row).
 *   - it is `text-center` at a 14px literal with `mt-2`, against a block that is
 *     `text-align:right` at `--font-size-xs`. Under the 8px root `mt-2` is 4px,
 *     not the 8px its author meant (b423e839).
 *
 * So the rotation lives here and the three-line pin survives. The pool and the
 * active/contemplative split are `lib/quotes`' — unchanged, one source. THE
 * PACE IS NOT: it is `QUOTE_ROTATION_MS` at the top of this file, and since
 * 2026-09-19 it no longer agrees with `QuoteDisplay`'s. That constant carries
 * why.
 */
function useRotatingQuote(isActive: boolean): Turn {
  const [turn, setTurn] = useState<Turn>(() => ({ quote: pickQuote(isActive), seq: 0 }));
  // Read through a ref so a change in liveness does not restart the cycle —
  // it only decides which pool the NEXT pick comes from.
  const activeRef = useRef(isActive);
  activeRef.current = isActive;

  useEffect(() => {
    const id = setInterval(
      () => setTurn((cur) => ({ quote: pickQuote(activeRef.current), seq: cur.seq + 1 })),
      QUOTE_ROTATION_MS,
    );
    return () => clearInterval(id);
  }, []);

  return turn;
}

/**
 * ONE TURN OF THE CYCLE — the quote plus a counter that only ever increases.
 *
 * THE COUNTER IS WHAT MAKES THE FADE RELIABLE, and it is not decoration.
 * `pickQuote` draws at RANDOM from a pool of over a hundred, so it can draw the
 * quote that is already on screen — roughly once every hundred turns. Keyed on
 * the text, that turn would produce no new layer and no crossfade, and a gate
 * watching for one would time out and be called a flake. Keyed on `seq`, every
 * turn crossfades, even onto itself.
 */
interface Turn {
  quote: Quote;
  seq: number;
}

/**
 * THE QUOTE CROSSFADES INSTEAD OF BEING REPLACED — A-3 #5.
 *
 * The owner: *"quotes fade out/in; currently they jump"*. Measured on main, both
 * engines: `.sb-quote` and `.sb-quote b` both report `transition-duration: 0s`,
 * so there was no surface to fade ON. The text was simply a different string in
 * the next commit.
 *
 * TWO LAYERS IN ONE GRID CELL, WHICH IS THE PANEL'S OWN IDIOM. `.kids`
 * (`shell.css`) already crossfades a bar's icon row against its text row by
 * stacking both in `grid-area:1/1`, and this is the same movement: swap A for B
 * in place. No new vocabulary, and no JS clock — the two layers carry CSS
 * ANIMATIONS rather than transitions, because a transition needs the element to
 * have been painted at its start value and a freshly mounted layer never was.
 *
 * THE HEIGHT CANNOT MOVE, WHICH IS WHY THIS IS SAFE HERE AND NOWHERE ELSE.
 * `.sb-quote b` is pinned to exactly `3lh` with `overflow:hidden` (Q1, 2026-09-14),
 * so both layers are the same height by construction and the grid row cannot
 * change — which matters because `.sb-info`'s measured height drives
 * `--sh-sb-info-content`, and that drives the icon row and the blob.
 *
 * THE OUTGOING LAYER IS ADJUSTED DURING RENDER, NOT IN AN EFFECT. An effect runs
 * after paint, so there would be exactly one frame showing the new text with no
 * outgoing layer behind it — a flash, which is the defect in miniature. This is
 * React's documented "adjusting state when a prop changes": the extra render
 * happens before the browser paints anything.
 */
function QuoteBlock({ turn }: { turn: Turn }) {
  const [pair, setPair] = useState<{ arriving: Turn; leaving: Turn | null }>(() => ({
    arriving: turn,
    leaving: null,
  }));

  if (pair.arriving.seq !== turn.seq) {
    setPair({ arriving: turn, leaving: pair.arriving });
  }

  return (
    <div className="sb-quote">
      {pair.leaving !== null && (
        <div
          className="sb-quote-layer"
          data-leaving=""
          key={pair.leaving.seq}
          aria-hidden="true"
          /* IT REMOVES ITSELF WHEN ITS OWN ANIMATION ENDS — no timer, and no
             duration repeated in two places. The guard on `seq` is for the case
             where a third turn arrives before the second has finished fading:
             without it, the late event from the first layer would clear the
             second. */
          onAnimationEnd={() =>
            setPair((cur) =>
              cur.leaving !== null && cur.leaving.seq === pair.leaving!.seq
                ? { arriving: cur.arriving, leaving: null }
                : cur,
            )
          }
        >
          <b>{pair.leaving.quote.text}</b>
          <span>{pair.leaving.quote.author}</span>
        </div>
      )}
      <div className="sb-quote-layer" key={pair.arriving.seq}>
        <b>{pair.arriving.quote.text}</b>
        <span>{pair.arriving.quote.author}</span>
      </div>
    </div>
  );
}

export function Sidebar({
  onToggle,
  collapsed,
}: {
  onToggle(): void;
  /** only for the toggle's label and pressed state — the geometry is CSS's */
  collapsed: boolean;
}) {
  const { isActive } = useShellPulse();
  const turn = useRotatingQuote(isActive);
  const infoRef = useRef<HTMLDivElement>(null);

  /* ONE ROW OPEN AT A TIME. The block is anchored to the panel's BOTTOM and
     grows upward into the blob, so independent toggles would let three open
     sections eat the whole panel with no single place to stop it. One-at-a-time
     bounds the growth by construction instead of by a max-height guess. */
  const [openRow, setOpenRow] = useState<RowKey | null>(null);
  const toggleRow = (k: RowKey) => setOpenRow((cur) => (cur === k ? null : k));

  return (
    /* THE PANEL IS NO LONGER ITS OWN TOGGLE. The owner ruled it 2026-09-16 after
       reporting "click everywhere collapses the sidepanel".
 
       IT WAS NEVER ONLY AN ANNOYANCE — IT WAS A TAX ON EVERY FUTURE CHILD. A
       handler on the container makes each control inside it a special case, and
       `.sb-head` below already documents the trap: *"stopPropagation is
       load-bearing: without it the click bubbles to the aside's own handler and
       the panel toggles twice, i.e. not at all."* Three rows about to become
       disclosures, a blob that will get a surface and a meter that will get a
       target are three more copies of that line. Removing the container handler
       removes the class, so none of them has to remember.
 
       THE HEADER KEEPS THE WHOLE 64px ROW at `width:100%`, so collapsed — where
       the panel IS a 48px bar — the top of that bar is still one large target.
       Nothing in the open state loses an affordance that was announced: the
       aside carried no role, no label and no `aria-expanded`; the button carries
       all three and always did. */
    <aside className="sidebar" id="sidebar">
      {/* THE PANEL TOGGLE IS NOW A REAL BUTTON — the owner's ruling is "sidebar
          and bars focusable with roles", and the panel axis was one of the two
          chrome toggles with no keyboard path at all (measured: 0 focusables in
          the entire sidebar). `.sb-head` already carries the 64px row, the
          padding and the arrow, so it becomes the button rather than gaining a
          nested one — a nested control inside a clickable aside would give the
          same action two stops.

          `stopPropagation` is load-bearing: without it the click bubbles to the
          aside's own handler and the panel toggles twice, i.e. not at all. */}
      <button
        type="button"
        className="sb-head sh-ctl"
        aria-expanded={!collapsed}
        aria-controls="sidebar"
        aria-label={collapsed ? "Expand side panel" : "Collapse side panel"}
        onClick={onToggle}
      >
        <svg className="sb-arrow" viewBox="0 0 12 12" fill="none" stroke="currentColor" aria-hidden="true">
          <path d="M7.5 1.5 3 6l4.5 4.5" />
        </svg>
        <span className="sb-mark">OKURO</span>
      </button>

      {/* C3 — THE REAL BLOB. `.sb-blob`'s BOX is unchanged (230px, centred at
          y=512 in a 1024 frame, 40px collapsed, both transitions); what paints
          inside it is now the pulse engine on a canvas instead of a static
          radial gradient. Under `prefers-reduced-motion: reduce` no canvas
          mounts and the gradient is what remains — see `Blob.tsx`. */}
      <Blob />

      {/* THE FIVE SLOTS ARE PINNED LEAVES NOW — see `Pins.tsx` for the store
          and why leaves only. They were inert `<i>` placeholders whose comment
          said they *"stay unfocusable … an `<i>` has no tab stop, these five
          have no action, and F2b's condition is that a placeholder becomes
          focusable in the SAME change that gives it one."* This is that change,
          so they are buttons.

          THE ELEMENT-KEYED SELECTOR IS WHY `<i>` SURVIVES INSIDE THE BUTTON.
          `.sb-icons i` sizes the glyph box (shell.css), and trap 3 of `937c7f7b`
          is that swapping the element silently unstyles the row — five invisible
          0x0 boxes, no error. So the button wraps an `<i>` exactly as `.bar-ico`
          does, and the measured 16px geometry is untouched. */}
      {/* ONE BOTTOM-ANCHORED STACK, AND IT IS WHAT LETS THE BLOB STOP
          MEASURING ANYTHING. The icon row sits directly above the info block
          in Figma, and a drawer opening pushes the row up by exactly the
          height it gains. Putting the two in one flow container is that
          relationship stated as layout: the block grows, the row rides on it,
          and nothing has to publish a height for the other to read. That
          measured token (`--sh-sb-info-content`), its ResizeObserver and the
          hook that suppressed the transitions it kept restarting are what
          this replaces. */}
      {/* THE ORDER IS QUOTE, ACCORDIONS, ICONS, METER — the owner, 2026-09-19:
          *"bottom part: sequence change top to bottom: quote, accordion, icons,
          agent meter."* It was icons, accordions, quote, meter.

          THAT MAKES THE STACK THREE SLOTS RATHER THAN TWO, and the middle one
          is the reason: the icon row is the ONE part of this stack that
          survives the collapse, so it cannot live inside the box that collapses.
          It used to sit above that box, which is why two tracks were enough. Put
          it between the accordions and the meter and the collapsing content is
          suddenly on BOTH sides of it, so each side needs its own track.

          ALL THREE TRACKS ARE DECLARED IN ONE PROPERTY ON `.sb-stack`, and both
          ends of that declaration have the same shape — `minmax(0,Nfr) auto
          minmax(0,Nfr)`. A track list only interpolates when the lists match
          track for track, so `auto -> minmax(0,0fr)` on the meter would have
          snapped rather than eased: the same non-interpolatable class as the
          drawer's bare `0fr` and the `flex-basis:auto`. */}
      <div className="sb-stack">
        <div className="sb-info" ref={infoRef}>
          {/* C1 — THE ROWS ARE CONTROLS NOW, AND THE CHEVRONS ANNOUNCE SOMETHING.
              They shipped as three `<div>`s whose comment said *"the rows have no
              disclosure behaviour yet to announce"*. They have one, so F2b's rule
              applies in full: a placeholder becomes focusable in the SAME change
              that gives it an action. Each row is a real `<button>` carrying
              `aria-expanded` and `aria-controls`, and the chevrons stay
              `aria-hidden` because the button already says the state.

              This is also what the panel's old click-anywhere handler made
              impossible without a `stopPropagation` on every one of them — that
              handler went in the previous batch, so these need nothing. */}
          <QuoteBlock turn={turn} />
          <Row k="briefs" label="DAILY BRIEFS" open={openRow} onToggle={toggleRow}>
            <BriefList />
          </Row>
          <Row k="messages" label="MESSAGES" open={openRow} onToggle={toggleRow}>
            {/* MESSAGES HAS NO INBOUND PRODUCER — measured, not assumed:
                `/api/handover` is send-only and deliveries are per-task outbound
                renders with no global list. So it opens and says so, rather than
                rendering an empty box that looks broken. The owner rules what goes
                here; nothing is invented meanwhile. */}
            <p className="sb-note">NO MESSAGE SOURCE YET</p>
          </Row>
          <Row k="activity" label="ACTIVITY" open={openRow} onToggle={toggleRow}>
            <p className="sb-note">NOT BUILT YET</p>
          </Row>
        </div>

        <PinRow />

        {/* THE SLOT EXISTS SO THE METER KEEPS ITS OWN BOX WHILE THE TRACK GOES
            TO ZERO. `.sb-meter` carries a 24px padding-top and a hairline
            border; stretched into a 0px track it would keep both — a
            border-box cannot squeeze its own padding — and leave ~25px of line
            standing in the collapsed panel. A wrapper with nothing on it CAN be
            zero, and the meter inside it simply overflows and is clipped, which
            is the same thing `.sb-drawer` does one level up. */}
        <div className="sb-meter-slot">
          <ActivityMeter />
        </div>
      </div>
    </aside>
  );
}

/**
 * THE PINNED ICON ROW.
 *
 * A filled slot jumps to its leaf. An EMPTY slot opens the picker — which is
 * also the only in-panel way to CHANGE a pin, and that is a deliberate limit
 * rather than an oversight: the row is five Figma-measured 16px boxes with no
 * sixth affordance available, so a full row has no room for an "edit" control
 * that is not itself a sixth glyph. Re-assigning a filled slot belongs in
 * Settings, where every other profile preference already lives.
 */
function PinRow() {
  const { pins, setPin } = usePins();
  const navigate = useNavigate();
  const [picking, setPicking] = useState<number | null>(null);
  const catalogue = pinCatalogue();

  return (
    <div className="sb-icons">
      {Array.from({ length: PIN_SLOTS }, (_, slot) => {
        const resolved = resolvePin(pins[slot]);
        // The old glyph set stays the FALLBACK art, so a slot never renders as
        // a blank box while the profile loads or after a leaf is renamed away.
        const Icon = PANEL_ICONS[slot] ?? PANEL_ICONS[0]!;
        return (
          <button
            key={slot}
            type="button"
            className="hit sh-ctl"
            /* THE SLOT INDEX IS THE ONLY THING CSS CANNOT WORK OUT FOR ITSELF,
               and it is what lets both arrangements be ONE interpolatable
               property. Each button is placed by `transform` — across in the
               open row, up the column when collapsed — so the container's own
               height never changes and the bottom stack it sits above cannot
               be moved by a layout-mode switch. It is a plain custom property
               read by a `calc`, never an animated one: WebKitGTK cannot
               interpolate a registered custom property at 60Hz, which is why
               the rail's motion sits on `flex-grow` and this one on
               `transform`. */
            style={{ ["--i" as string]: slot }}
            aria-label={resolved ? `Go to ${resolved.label}` : `Pin a leaf to slot ${slot + 1}`}
            title={resolved ? resolved.label : "Pin a leaf"}
            aria-expanded={picking === slot ? true : undefined}
            onClick={() => {
              if (resolved) navigate(resolved.path);
              else setPicking((cur) => (cur === slot ? null : slot));
            }}
          >
            <i>
              <Icon aria-hidden="true" />
            </i>
          </button>
        );
      })}

      {picking !== null && (
        <div className="sb-picker" role="dialog" aria-label="Pin a leaf">
          {catalogue.map((c) => (
            <button
              key={c.pin}
              type="button"
              className="sb-pick sh-ctl"
              onClick={() => {
                setPin(picking, c.pin);
                setPicking(null);
              }}
            >
              {c.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * ONE DISCLOSURE ROW — the header button plus the region it controls.
 *
 * The open region is a SIBLING of the button, not a child, so the button keeps
 * its measured `--sh-sb-row-h` height whatever it discloses.
 *
 * ===========================================================================
 * `hidden` IS GONE, AND ITS TWO PROPERTIES ARE KEPT DELIBERATELY — A-3 #7, R10
 * ===========================================================================
 * The attribute was chosen here for three things, and the comment it replaces
 * named all three: out of the accessibility tree, out of the tab order, out of
 * layout so `.sb-info`'s measured height stays truthful. That reasoning was
 * sound, and it is exactly what made the drawer unanimatable — `hidden` is
 * `display:none`, which no engine interpolates. The owner ruled the replacement
 * (R10): `inert` carries the a11y tree and the tab order, and
 * `content-visibility:hidden` carries the layout cost. Neither of those is a
 * consolation prize: `inert` is the idiom this shell already uses for its four
 * resting panes, and `content-visibility` is what `.content` uses for exactly
 * this saving.
 *
 * The state is an ATTRIBUTE rather than a class because the CSS needs to select
 * on it (`.sb-drawer[data-open]`) and because `inert` has to be driven by the
 * same boolean — two names for one state is how they drift apart.
 *
 * THE INNER ELEMENT IS NOT DECORATION. The drawer animates on
 * `grid-template-rows: 0fr -> 1fr`, and a grid row of `0fr` still pays for its
 * container's padding — so the cap, the scroll and the 12px of breathing room
 * live on `.sb-drawer-in`, whose `min-height:0` is what lets the row reach zero
 * at all.
 */
function Row({
  k,
  label,
  open,
  onToggle,
  children,
}: {
  k: RowKey;
  label: string;
  open: RowKey | null;
  onToggle(k: RowKey): void;
  children: React.ReactNode;
}) {
  const isOpen = open === k;
  const Chevron = isOpen ? ROW_CHEVRON.down : ROW_CHEVRON.up;
  return (
    <>
      <button
        type="button"
        className="sb-row sh-ctl"
        aria-expanded={isOpen}
        aria-controls={`sb-panel-${k}`}
        onClick={() => onToggle(k)}
      >
        <span>{label}</span>
        <Chevron className="sb-chev" aria-hidden="true" />
      </button>
      <div
        className="sb-drawer"
        id={`sb-panel-${k}`}
        data-open={isOpen ? "" : undefined}
        inert={!isOpen}
      >
        <div className="sb-drawer-in">{children}</div>
      </div>
    </>
  );
}

/**
 * THE TWO CHROME BARS — constant in all 20 states, floating over the rail.
 *
 * They MIRROR each other: 32 toward the frame edge, 16 toward the content, so
 * with 16px glyphs both plates come out 64 tall. MAX is the first window
 * control: it maximises the current section and never touches the panel, which
 * is what keeps the two axes independent.
 *
 * THE APPEARANCE TOGGLE TAKES AN EXISTING SLOT AND ADDS NO CHROME.
 *
 * The live app puts its `ThemeModeToggle` in the trailing cluster of the global
 * nav — `<div className="flex shrink-0 items-center gap-3 pl-4">` holding the
 * toggle and the health dot (frontend/src/components/shell/nav-bar.tsx:360-363).
 * The logic is: persistent global chrome, trailing cluster, beside the other
 * always-present utility glyph. Not in a page, not in a menu.
 *
 * `.winctl` IS that slot here, and it already rendered five glyphs — MAX plus
 * four placeholders with no behaviour. The toggle becomes the second, so the
 * plate keeps its measured 240x64 and its 24px pitch and the glyph count does
 * not move. Adding a sixth glyph would have widened a Figma-measured plate to
 * 280 to make room for a control the design never asked for; that is the
 * "invent chrome" failure, and it is avoided by using what is already there.
 */
export function Chrome({
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
  /**
   * R9 — THE PLATE IS CLOSED UNTIL YOU REACH FOR IT.
   *
   * The owner, 2026-09-14, after rejecting all three layout options the p3 report
   * offered: *"window controls collapse into a single icon, like the bottom-right
   * action container (.genact); HOVER opens the plate with the glyphs."* It
   * resolves the plate-vs-rail collision at every width BY CONSTRUCTION rather
   * than by moving either control — the closed plate is 89.6px wide under the
   * active kit and the rail's invariant right edge is 140px clear of it at 1280,
   * the narrowest width in the sweep.
   *
   * ONE PIECE OF STATE, WHICH IS WHY IT IS IN REACT AND THE MOTION IS NOT.
   * `:hover` + `:focus-within` would have done the same job in pure CSS and it
   * was not taken, for one reason: `aria-expanded` then has nothing to read, so
   * a screen reader is told about a button that opens nothing. React owns the
   * boolean and sets one attribute (`data-open`); every width, opacity and
   * visibility change is the stylesheet's (`cd04fca9`).
   *
   * POINTER AND KEYBOARD ARE THE SAME STATE, deliberately. `onPointerEnter`
   * fires from a descendant because React synthesises enter/leave out of
   * `pointerover`/`pointerout`, which bubble — so hovering the closed trigger
   * opens the plate even though the plate itself is `pointer-events:none` while
   * closed. The focus pair is what F2b's rule asks for: a placeholder glyph
   * becomes focusable in the same change that gives it an action, and this one
   * now has one.
   */
  /**
   * C2 — THE MECHANISM IS NOW SHARED WITH `.genact`, NOT OWNED HERE.
   *
   * R9's own wording is *"window controls collapse into a single icon, **like
   * the bottom-right action container (.genact)**"* — the ruling points at the
   * corner container as the pattern. C2 makes that container actually behave
   * that way, so the six lines of state and the four handlers move into
   * `useDisclosure` and both plates read the same implementation. Every comment
   * that explained why each handler is necessary moved with it.
   */
  /* ONLY `plate` IS READ HERE. `open` and `pin` were what the removed
     three-dot trigger used; the window plate no longer collapses, so this
     component needs the props bundle and nothing else. */
  const { plate } = useDisclosure();

  return (
    <>
      <div className="winctl sh-plate" {...plate}>
        {/* THE FOUR THAT COLLAPSE. A wrapper rather than four collapsing
            children, because `gap` still applies between zero-width items — four
            glyphs at width 0 would have left 3 x 24px of dead plate behind. One
            box with one width to animate, and the plate shrink-wraps it.
            `.pl-fold` is the shared class; `--pl-n` is its glyph COUNT, which
            the one width formula in the stylesheet reads. */}
        {/* SIX, NOT FOUR. `--pl-n` is the glyph COUNT the width formula reads,
            so restoring the four dropped actions is a count change rather than a
            layout one. The plate is anchored `right:0` and the TRIGGER stays
            last, so it grows LEFTWARD and the last glyph's centre — the one
            `--sh-gutter` is derived from — does not move. That is the specific
            constraint `d568f0b2` warns about for this plate, and it is why the
            growth is affordable here even though it was not for a generic
            widening. */}
        <div className="pl-fold" style={{ ["--pl-n" as string]: 3 }}>
        {/* THE CONTENT-MAX TOGGLE WAS MOUSE-ONLY, and it is the second of the
            shell's two chrome axes. The appearance glyph beside it already
            carried role/tabIndex and its own Enter/Space handler; this one
            becomes a real `<button>` instead, which is where that pattern was
            always heading — three hand-written key handlers in one row is three
            chances to forget the third. `.winctl .hit` is what styles it now
            that the element is no longer an `<i>`. */}
        {/* SECTION-MAXIMISE, APPEARANCE AND HARD-REFRESH LEFT THIS PLATE.
            The owner, 2026-09-19: they belong in the bottom corner, and the three
            dots there become an arrow. What stays here is what the name says —
            the WINDOW controls: maximise, minimise, close. The split is the one
            the two plates were always implying: this plate talks to the OS, the
            corner talks to okuro. They are handed to `<Corner />` below, which
            is rendered from this same component and already had the props. */}
          <button
            type="button"
            className="hit wc-winmax sh-ctl"
            aria-label="Maximise window"
            title="Maximise window"
            onClick={(e) => {
              e.stopPropagation();
              nativeWindow("toggle_maximize");
            }}
          >
            <Square aria-hidden="true" />
          </button>
          <button
            type="button"
            className="hit wc-min sh-ctl"
            aria-label="Minimise window"
            title="Minimise window"
            onClick={(e) => {
              e.stopPropagation();
              nativeWindow("minimize");
            }}
          >
            <Minus aria-hidden="true" />
          </button>
          <button
            type="button"
            className="hit wc-close sh-ctl"
            aria-label="Close window"
            title="Close window"
            onClick={(e) => {
              e.stopPropagation();
              nativeWindow("close");
            }}
          >
            <X aria-hidden="true" />
          </button>
        </div>

        {/* THE TRIGGER IS THE PLATE'S OLD RIGHTMOST GLYPH, AND THAT IS A
            GEOMETRY DECISION BEFORE IT IS A DESIGN ONE.
            -----------------------------------------------------------------
            The right gutter is an ALIGNMENT CONSTRAINT: the collapsed SYSTEM
            icon's centre must equal the plate's last glyph centre, and
            `--sh-gutter` is derived from that equation (`tokens.css`, decision
            `880dfd28`). The plate is anchored `right:0` with its own padding, so
            whichever element is LAST in this row sits at the same x — which
            means promoting the existing last glyph to the trigger keeps the
            constraint satisfied in BOTH states, with nothing to recompute.
            Adding a sixth glyph would have widened a Figma-measured 240x64
            plate to 280 and moved that centre; the p1 comment above says as
            much about the appearance toggle, and it applies here too.

            IT WAS A PLACEHOLDER WITH NO BEHAVIOUR AND NOW IT HAS ONE, which is
            exactly the condition F2b set for making one focusable: *"They
            become focusable in the same change that gives them an action."*
            So it is a real `<button>` with a real `aria-expanded`, and the two
            remaining placeholders above stay inert. */}
        {/* THE TRIGGER IS GONE WITH THE THING IT TRIGGERED. This plate does not
            collapse any more — the owner ruled both corner stacks open on
            2026-09-19 and the bottom one keeps an ARROW for its own collapse —
            so a "..." that opens an already-open plate was a control with
            nothing to do. `aria-expanded` went with it: there is no longer a
            disclosure here to announce. */}
      </div>
      {/* C2 — THE CORNER CONTAINER IS ITS OWN COMPONENT NOW.
          It was three anonymous placeholder boxes here. It is now a collapsed
          plate holding search, feedback and add-task, and it lives in
          `Corner.tsx` because it carries okuro's own actions — the boundary
          `Frame.tsx` draws around the signed-off shell. It still renders from
          inside `.app`, because `position:absolute` needs `.app` as its
          containing block. */}
      <Corner
        maxed={maxed}
        appearance={appearance}
        onToggleMax={onToggleMax}
        onToggleAppearance={onToggleAppearance}
      />
    </>
  );
}
