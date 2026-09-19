import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDown, Check, CircleAlert, Copy, SlidersHorizontal, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Segmented } from "@/components/ui/segmented";
import { toast } from "@/components/ui/toast";
import { engineApi, engineError } from "@/components/design-engine/engine-api";
import { createKit, deleteKit, saveKit, setActiveKit } from "@/lib/kit-writes";
import { setViewportRung } from "@/lib/active-kit";
import { onboardingApi } from "@/lib/api";
import { useLeaveGuard } from "@/lib/leave-guard";
import { useSectionTitle } from "@/shell/components/PageTitle";
import type { BrandJson, ResolvedModel } from "@/components/design-engine/types";
import { COMPONENT_DOCS, COMPONENT_GROUPS } from "@/components/ds-engine-codex/catalog";
import { ConfigurationPanel } from "@/components/ds-engine-codex/configuration-panel";
import { LiveDemo } from "@/components/ds-engine-codex/native-specimens";
import { ComponentShowcase } from "@/components/design-authoring/component-showcase";
import { RootPlacementChips, useGuestBrands } from "@/components/design-authoring/grounds";
import { NestingSection } from "@/components/design-authoring/nesting";
import { revealAll, useCanvasGround } from "@/components/design-authoring/canvas";
import { PreviewFrame, type FrameMessage } from "@/components/design-authoring/preview-frame";
import { Inspector } from "@/components/design-authoring/inspector";
/* ── THE KIT LIFECYCLE, MOVED HERE IN WAVE 5 ─────────────────────────────
   All three were `/design-engine`'s alone (matrix rows 2, 4, 5), and two of
   them are named in the charter as things that page is "the ONLY place" for.
   They are page-neutral now and this page mounts the same code the old one
   does — the essentials sheet takes this page's vocabulary, the two dialogs
   are built from `components/ui/*` and name no page's chrome at all. */
import { EssentialsGate } from "@/components/design-authoring/essentials";
import { ForkKitDialog } from "@/components/design-authoring/fork-kit-dialog";
import { ScanKitDialog } from "@/components/design-authoring/scan-kit-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { LogicAtlas } from "@/components/ds-engine-codex/logic-atlas";
import { useChrome } from "@/lib/chrome-context";
import { liveAppearance } from "@/lib/theme";
import { CODEX_VOCABULARY } from "@/components/ds-engine-codex/vocabulary";
import {
  useAcknowledged,
  verdictOf,
  withAcknowledgements,
  type AdviceAction,
  type AdviceScene,
} from "@/lib/flags";
import { derivationBySlot } from "@/lib/derivations";
/* THIS PAGE IMPORTS NO OTHER PAGE'S STYLESHEET, and that is a rule rather than
   a fact. Wave 4 added `import "@/components/design-engine/chrome.css"` here so
   the moved nested-guests block could reach the `.ce-*` vocabulary it was still
   written in. It worked and it was the wrong direction: "no old parts overwrite
   new parts in the ds-engine-codex" (his rule, 2026-09-13, memory df6e21cb).
   The shared component takes class names from its HOST now — ours are in
   `components/ds-engine-codex/vocabulary.ts`, painted by the file below. */
import "@/components/ds-engine-codex/ds-engine-codex.css";

const SECTION_LINKS = [
  ["systems", "Systems"],
  ["demo", "Live system"],
  ["components", "Components"],
] as const;

/**
 * WHICH STAGE OWNS WHICH SCENE — the `show` action's answer on this page.
 *
 * `document` is the live system and `guests` is the nesting proof; both are
 * real anchored stages here. `tokens` has no entry because this page has no
 * token list, and `REACHABLE_SCENES` in the configuration panel says the same
 * thing one layer up so the chip is never drawn. Two statements of one fact,
 * which is why the gate asserts they agree.
 */
const SHOW_STAGE: Partial<Record<AdviceScene, string>> = {
  document: "live-system",
  guests: "guests",
};

/**
 * WHICH SCENES THIS PAGE CAN REACH — DERIVED from the map above, never typed a
 * second time.
 *
 * Two constants stating one fact is exactly how the chip and the handler
 * disagreed in the first place (todo d049a0fe): `adviceFor` drew a `show` chip
 * and this page had no branch to answer it. Deriving the set means adding a
 * token list is ONE edit — a stage in `SHOW_STAGE` — and the chip starts being
 * drawn on the same line that makes it work.
 */
const REACHABLE_SCENES = Object.keys(SHOW_STAGE) as AdviceScene[];

/** How far below the scroller's top edge a section counts as arrived. The
 *  sticky header is 56px and `scroll-margin-top` is 120px; 120 is the line an
 *  anchor jump actually lands on, so the pill and the jump agree. */
const NAV_BAND = 120;

/**
 * WHAT A CLICK ON THE CANVAS SELECTS — a value, or a finding.
 *
 * `PreviewFrame` publishes one message for every gesture it accepts: `reveal`
 * for a value (that includes the node's GROUND PILL, which is a slot like any
 * other), `flag` for one of the marks `markFlags` draws on a flagged element,
 * and `place`/`drop` for the tree builder. This page has no tree builder — the
 * place bar is invisible and unclickable because `revealAll` withholds the
 * `descent` beat that gives it pointer events — so those two are not handled.
 *
 * UNTIL WAVE 3 THIS PAGE DROPPED ALL FOUR (`NO_CANVAS_GESTURES`), which is why
 * the ground pill accepted a click and did nothing: the frame reported it and
 * the host had nowhere to put it. The mark and the pill are the SAME event now,
 * and the host answers both by opening the inspector.
 */
interface CanvasSelection {
  /** The resolved ground key the gesture happened on. */
  ground: string;
  /** The authored slot the click landed on. `""` when a mark was clicked. */
  slot: string;
  /** The finding's code, when a MARK was what was clicked. */
  flag?: string;
}

export function DsEngineCodexPage() {
  const { hide, show } = useChrome();
  const boot = useQuery({
    queryKey: ["ds-engine-codex", "boot"],
    queryFn: () => engineApi.boot(),
    staleTime: Infinity,
  });
  /* WHICH KIT PAINTS THE APP, ON ITS OWN QUERY — and it cannot be `/boot`.
     `/boot` carries `active` too, and refetching IT to learn the new answer
     would re-run the effect below that seeds `brand`/`model`/`sheet`/`rung`
     from the payload: pressing "Set as active" would silently throw away the
     kit you have open and every unsaved edit in it. `/kits` answers the same
     question — the same `_active_kit_id` resolves both — and nothing on this
     page is seeded from it. Read, never computed: the badge still comes off a
     server payload, just a smaller one. */
  const kitsQuery = useQuery({
    queryKey: ["ds-engine-codex", "kits"],
    queryFn: () => engineApi.kits(),
    initialData: () =>
      boot.data ? { kits: boot.data.kits, active: boot.data.active } : undefined,
    enabled: Boolean(boot.data),
  });
  const activeKit = kitsQuery.data?.active ?? boot.data?.active ?? null;
  /* THE WRITE MODULE RE-READS WHO IS ACTIVE THROUGH THIS QUERY. `lib/kit-writes.ts`
     owns the rule that a write changing what `/engine.css` serves reloads the
     link; it needs the server's answer to decide, and this page needs the same
     answer for its badge, so the refetch is handed in rather than duplicated. */
  const listKits = useCallback(async () => (await kitsQuery.refetch()).data ?? null, [kitsQuery]);
  const [brand, setBrand] = useState<BrandJson | null>(null);
  const [savedBrand, setSavedBrand] = useState<BrandJson | null>(null);
  const [model, setModel] = useState<ResolvedModel | null>(null);
  const [sheet, setSheet] = useState("");
  /**
   * THE PREVIEW'S APPEARANCE, AND IT NO LONGER TOUCHES THE APP — Q-L5, ruled A
   * ("the rail writes only the frames; never touch the app root").
   *
   * WHAT THE HARDCODED `"light"` COST, measured at this HEAD before the change:
   * arriving at `/system/design` set `data-appearance="light"` on the APP's
   * documentElement while `okuro.theme-mode` still said `dark`, so the sidebar
   * and all five topic bars flipped with it, the shell's own toggle became two
   * dead presses, the stored value and the rendered one disagreed, and the
   * unmount undo restored the value captured at MOUNT — discarding a flip made
   * during the visit. Four defects from one seed and one effect.
   *
   * EVERY PREVIEW FRAME ALREADY WROTE ITS OWN. `preview-frame.tsx:1553` sets
   * `data-appearance` on the frame's own documentElement from this prop, and a
   * srcdoc frame IS a document root, so the engine's `[data-appearance]` blocks
   * resolve inside it. The app-level write was never what made the preview
   * work; it was only what made the app follow.
   *
   * SEEDED FROM THE LIVE VALUE rather than from a literal, so the preview opens
   * showing what the reader is already looking at and the switch then moves the
   * PREVIEW alone.
   */
  const [appearance, setAppearance] = useState<"light" | "dark">(liveAppearance);
  /* THE PREVIEW RUNG, and `null` is "whatever the brand itself says".
     IT WAS A TYPED LIST OF FIVE — `["XS","S","M","L","XL"]` — while the engine
     publishes the whole ladder as `model.sizes.text_rungs`. That is a second
     authority for a list one layer already answers, and it was also a REDUCTION:
     the old page's canvas offers every rung the engine has plus the brand's own
     default, and a reader here could reach five of them. Read, never typed. */
  const [rung, setRung] = useState<string | null>(null);
  /* THE VIEWER'S RUNG PER VIEWPORT CLASS — the configuration item, and a
     DIFFERENT THING from the preview rung above it. That one is scene state and
     is thrown away on reload; this one is profile state every design system
     inherits (his ruling, 2026-09-17). They are two fields rather than one
     because they answer two questions, and the boot payload keeps them apart
     under `viewport` vs `rung` for the same reason. */
  const [viewportRungs, setViewportRungs] = useState<Record<string, string> | null>(null);
  /* A BUILDER PLACEMENT at the canvas root — brand-full or a signal. `null` is
     "whatever the appearance says", which is what a reader gets untouched.
     Light and dark are the only two a USER picks and they stay on the
     appearance switch; these five are design facts a builder places. */
  const [rootPlacement, setRootPlacement] = useState<string | null>(null);
  const [scaled, setScaled] = useState(false);
  const [componentId, setComponentId] = useState("button");
  const [activeSection, setActiveSection] = useState("systems");
  const [error, setError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [mobileConfigOpen, setMobileConfigOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  /* "WORKED ON" IS DIRTY *OR* SAVED, and the second half is the one a dirty
     flag cannot carry: pressing Save clears `dirty`, and a kit you edited and
     saved but never made live is exactly the case the owner's leave rule is
     about. Reset when a different kit is opened — the question is about the kit
     on screen, not about the session. */
  const [savedThisSession, setSavedThisSession] = useState(false);
  /* ── THE KIT LIFECYCLE — matrix rows 2, 3, 4, 5, 8 ─────────────────────
     Three sheets and one armed control. `lifecyclePending` is one flag rather
     than three because only one of them can be open at a time, and a second
     flag that can disagree with the first is a second authority. */
  const [creating, setCreating] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [forking, setForking] = useState(false);
  /* THE SOURCE IS THE KIT AS SAVED, never the draft on screen — the whole point
     of row 3 is duplicating a system you are only LOOKING at, which must not
     first replace what you have open. The rail's own `data-duplicate-flow`
     still copies the draft; that is the other question and it keeps its
     answer. */
  const [forkSource, setForkSource] = useState<BrandJson | null>(null);
  const [lifecyclePending, setLifecyclePending] = useState(false);
  /* The id awaiting a SECOND press. One at a time: arming another row disarms
     this one, so there is never more than one loaded gun. Never a
     `window.confirm` — a native dialog blocks the event loop and takes every
     Playwright gate in the suite with it. */
  const [armedDelete, setArmedDelete] = useState<string | null>(null);
  /** Which stage the advice last sent the reader to, while the mark is up. */
  const [showStage, setShowStage] = useState<string | null>(null);
  const [settingActive, setSettingActive] = useState(false);
  const pending = useRef<number | null>(null);

  useEffect(() => {
    hide();
    return show;
  }, [hide, show]);

  /* THE APP-LEVEL APPEARANCE WRITE IS GONE — Q-L5. It read
     `useEffect(() => applyAppearance(appearance), [appearance])`, which is the
     line that re-themed the whole shell on arrival. The preview's appearance
     now lives only in the frames (see the state's own note above).

     ONE THING THAT WRITE DID CORRECTLY AND IS NOT LOST: it set `color-scheme`
     alongside `data-appearance`, because otherwise Chromium painted the rail's
     native `<select>` popup black on a light page. That pairing belongs to the
     APP's appearance, which the shell's toggle owns and still sets through the
     same `setAppearance`; the rail's `<select>` lives in the app document and
     therefore follows the app, which is the correct answer now that the preview
     no longer pretends to be the app. */

  /* THE DRAFT SHEET LIVES IN THE PREVIEW DOCUMENT, AND THIS PAGE ANNOUNCES
     NOTHING.

     It used to render `<style data-selected-engine-sheet>{sheet}</style>` inside
     `<main>` — in the APP document, later in the cascade than the `/engine.css`
     link at the same `:root` specificity — so merely OPENING a kit re-themed the
     whole application with a system the reader was only previewing. That is the
     exact opposite of ruling D-C, where a row previews and only "Set as active"
     changes what paints okuro. Measured in wave 1 (memory b40a818e): on load the
     computed `--color-accent` was the draft's `#649d8d` while the served sheet
     said `#7711cc`.

     The sheet is now handed to `PreviewFrame`, which writes it into ITS OWN
     document's `#engine` node. The frame is a real document root, so the sheet
     gets the `html { font-size: 50% }` it is written against and every rem in
     the preview is the number the engine meant. The page's own chrome keeps
     reading `/engine.css`, which is the active kit — D-C made visible rather
     than asserted.

     AND THERE IS NOTHING LEFT TO ANNOUNCE. `okuro:engine-sheet-changed` exists
     for readers outside the cascade — the pulse canvas caches `--color-accent`
     off `:root`. This page no longer moves `:root`, so dispatching would wake
     those readers for a repaint that did not happen. The one write that DOES
     move the app's sheet is "Set as active", and `lib/kit-writes.ts` fires the
     event there, on the link's LOAD. */

  useEffect(() => {
    if (!boot.data) return;
    setBrand(boot.data.brand);
    setSavedBrand(boot.data.brand);
    setModel(boot.data.model);
    setSheet(boot.data.sheet);
    /* `null` is the configured rung — see the state declaration. */
    setRung(null);
    /* THE CONFIGURED RUNGS COME FROM THE SERVER, and re-seeding them on every
       refetch is SAFE in a way re-seeding `brand` is not: this is not something
       the reader is editing in a form, it is a setting whose only writer round
       trips through the profile and comes back. */
    if (boot.data.viewport?.rungs) setViewportRungs(boot.data.viewport.rungs);
  }, [boot.data]);

  /* WRITE THE SETTING, AND SHOW IT IMMEDIATELY.
     Optimistic on purpose: the select must not snap back while the PATCH is in
     flight. On failure the server's answer wins — `setViewportRung` already
     re-fetches nothing, so the next boot refetch is what corrects a rejected
     write, and the error surfaces through the page's own banner. */
  const applyViewportRung = async (viewport: "desktop" | "mobile", value: string) => {
    const previous = viewportRungs;
    setViewportRungs({ ...(previous ?? {}), [viewport]: value });
    try {
      await setViewportRung(viewport, value);
    } catch (err) {
      setViewportRungs(previous);
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  // THE SCROLL SPY, AND WHY ITS THRESHOLDS WERE UNREACHABLE.
  //
  // It asked for `threshold: [0.1, 0.35, 0.7]` inside a band of `-20% 0px -65%`
  // — 15 % of the viewport, 157px at 1050. `intersectionRatio` is a fraction of
  // the TARGET's area, and the targets are whole page sections:
  //
  //   #systems      720px   highest reachable ratio 0.219   fires
  //   #demo        5141px   highest reachable ratio 0.031   NEVER
  //   #components  1451px   highest reachable ratio 0.109   a 13px window
  //
  // So the control was frozen on its `useState("systems")` default: clicking
  // through moved the scroller 0 -> 1412 -> 6553 and `data-active` never left
  // the first pill. A threshold expressed as a fraction of a target whose size
  // you do not control cannot be right for two sections of different heights.
  //
  // THE FIX ASKS A QUESTION THAT HAS NO AREA IN IT: with `threshold: 0` an entry
  // arrives whenever the section crosses the band at all, and the active one is
  // the LAST section whose top has already passed the band — the ordinary
  // scroll-spy shape, correct at any height.
  //
  // THE ROOT IS THE REAL SCROLLER, NOT THE VIEWPORT. This page does not scroll
  // the document: `window.scrollY` stayed 0 through every measurement while an
  // inner `overflow-y-auto` container moved. A viewport-rooted observer reports
  // against a box the content is not moving inside of.
  useEffect(() => {
    const nodes = SECTION_LINKS.map(([id]) => document.getElementById(id)).filter(
      (node): node is HTMLElement => node !== null,
    );
    const first = nodes[0];
    if (!first) return;

    /* THE SCROLLER IS FOUND BY BEHAVIOUR NOW, NOT BY CLASS NAME.
       ---------------------------------------------------------------------
       `closest(".overflow-y-auto")` was right for the full-page route, where
       the page owned an inner Tailwind-classed scroller. Under the shell the
       real scroller is `.content` (R2: the container scrolls, ring included),
       and `shell.css` styles its overflow in a rule rather than with the
       Tailwind class — so `closest` returned NULL and both the observer root
       and `pick()`'s band fell back to the viewport. Measured: the pane's top
       is 199.17px, and the fallback used 0, so the band was 199px off.

       Walking the ancestors and asking each one whether it actually scrolls
       finds the right box under either arrangement, and it keeps working if
       the shell renames anything. The class is still tried first so nothing
       changes on the routes that have one. */
    const findScroller = (node: HTMLElement): HTMLElement | null => {
      const byClass = node.closest<HTMLElement>(".overflow-y-auto");
      if (byClass) return byClass;
      for (let el = node.parentElement; el; el = el.parentElement) {
        const overflowY = getComputedStyle(el).overflowY;
        if (overflowY !== "auto" && overflowY !== "scroll") continue;
        if (el.scrollHeight > el.clientHeight + 1) return el;
      }
      return null;
    };
    const scroller = findScroller(first);
    const pick = () => {
      const top = (scroller?.getBoundingClientRect().top ?? 0) + NAV_BAND;
      let current = first;
      for (const node of nodes) {
        if (node.getBoundingClientRect().top <= top) current = node;
      }
      setActiveSection(current.id);
    };

    const observer = new IntersectionObserver(pick, {
      root: scroller ?? null,
      rootMargin: `-${NAV_BAND}px 0px -${100 - 1}% 0px`,
      threshold: 0,
    });
    nodes.forEach((node) => observer.observe(node));
    // The observer reports CROSSINGS; the scroll handler covers the rest of the
    // travel, and the first call sets the pill before either has fired.
    const target: HTMLElement | Window = scroller ?? window;
    target.addEventListener("scroll", pick, { passive: true });
    pick();
    return () => {
      observer.disconnect();
      target.removeEventListener("scroll", pick);
    };
    // KEYED ON `model`, WHICH IS WHAT PUTS THE SECTIONS IN THE DOM — the second
    // cause, and the one the arithmetic above hid. With `[]` deps this ran on
    // mount, when the page still renders `<EngineLoading />`, so all three
    // `getElementById` calls returned null: it observed nothing and never ran
    // again. Measured on the deployed page after the threshold was fixed: the
    // scroller moved 0 -> 1500 -> 3000 -> 6500 with `#demo` at top -1468 and the
    // pill still on `Systems`. A correct observer with no targets is
    // indistinguishable from a broken one.
    //
    // AND `boot.data` IS ONE COMMIT TOO EARLY, measured the same way. The render
    // on which `boot.data` first arrives still has `brand`/`model` at null, so it
    // returns the failure state and the sections are STILL absent; a separate
    // effect then sets them, and only the render after that carries the sections.
    // `model` is the value that gates them, so it is the one this can follow.
  }, [model]);

  const selectedComponent = useMemo(
    () => COMPONENT_DOCS.find((component) => component.id === componentId) ?? COMPONENT_DOCS[0],
    [componentId],
  );

  /* WHICH GROUND THE PREVIEW DOCUMENT IS ON — the shared answer, the same one
     `/design-engine` uses. `components/design-authoring/canvas.ts` owns the root
     lookup and the descent table so neither page re-derives it. */
  const { keyedTree, rootKey, groundsByKey } = useCanvasGround({
    model,
    appearance,
    rootPlacement,
  });

  /* ── ACKNOWLEDGEMENTS — matrix row 30 ───────────────────────────────────
     "Keep as is" is a READER's note, not a brand fact: register rule 12 lists
     the authored set completely and an acknowledgement is not on it. It is
     stored per KIT in `lib/flags.ts`, under a key named for the kit rather than
     for the page, so answering a finding here answers it on the other authoring
     surface too. The brand payload is BYTE-IDENTICAL across one, which is the
     property that makes it legal.

     An acknowledged finding DROPS TO `note`. It is not deleted: it still marks
     its control and its component and still reads in the inspector. What it
     stops doing is driving the verdict, because the reader has answered it. */
  const { acknowledge, isAcknowledged } = useAcknowledged(brand?.id);
  const flags = useMemo(
    () => withAcknowledgements(model?.flags ?? [], isAcknowledged),
    [model, isAcknowledged],
  );
  /** The model the canvas paints: the same one, wearing the acknowledgements. */
  const judgedModel = useMemo<ResolvedModel | null>(
    () => (model ? { ...model, flags } : null),
    [model, flags],
  );

  /* ── THE INSPECTOR — matrix rows 35 and 36 ──────────────────────────────
     A click on a value, or on a flag mark, opens the provenance panel beside
     it. Everything it shows is READ off the resolved model; nothing here
     recomputes a derivation or a reason. */
  const [selected, setSelected] = useState<CanvasSelection | null>(null);
  const [anchorY, setAnchorY] = useState<number | null>(null);
  const paneRef = useRef<HTMLDivElement>(null);

  /** The gesture's y, in the PANE's own coordinates.
      The frame reports the PARENT viewport's y because the panel lives outside
      the frame; the panel is absolutely positioned inside the pane, so a raw
      viewport coordinate would anchor it wherever the pane happens to start. */
  const anchorIn = useCallback((y: number | undefined) => {
    if (y == null) return null;
    const top = paneRef.current?.getBoundingClientRect().top ?? 0;
    return Math.max(0, y - top);
  }, []);

  /**
   * ONE HANDLER FOR EVERY CANVAS GESTURE, mark and pill alike.
   *
   * A node's id is its ground key on this page's canvas — `retree` writes
   * `groundKey` onto every node and the frame reports the node's `data-node`.
   * An id this page cannot resolve falls back to the ROOT's ground rather than
   * to the first in the list, because the root is what the reader is actually
   * looking at; an unknown SLOT opens nothing at all, which is the honest
   * answer and is the control the gate asserts.
   */
  const onCanvasGesture = useCallback(
    (message: FrameMessage) => {
      if (message.type !== "reveal" && message.type !== "flag") return;
      const ground =
        (message.ground && groundsByKey[message.ground]?.key) ??
        keyedTree?.groundKey ??
        rootKey;
      if (message.type === "flag") {
        if (!message.flag) return;
        setSelected({ ground, slot: message.slot ?? "", flag: message.flag });
      } else {
        if (!message.slot) return;
        setSelected({ ground, slot: message.slot });
      }
      setAnchorY(anchorIn(message.y));
    },
    [groundsByKey, keyedTree, rootKey, anchorIn],
  );

  /* ── "SHOW ME" — todo d049a0fe, wave 5 ──────────────────────────────────
     The advice's one navigation action. `/design-engine` answers it with
     `?scene=`; this page has anchored SECTIONS, so it answers with the stage
     that OWNS the scene. Three steps, and the first is the one that was worth
     porting: a finding about ONE ground is only visible on that ground, so the
     root is placed BEFORE the canvas is scrolled to — `rootFor` read it out of
     the engine's own `where` and the action carries it.

     `tokens` is deliberately absent from this map AND from
     `REACHABLE_SCENES`, which is what stops the chip being drawn at all. The
     two must agree; the gate asserts they do. */
  const showScene = useCallback(
    (action: AdviceAction & { kind: "show" }) => {
      const stage = SHOW_STAGE[action.scene];
      if (!stage) return;
      let ground = rootKey;
      const root = action.root ? model?.roots.find((r) => r.id === action.root) : null;
      if (root) {
        ground = root.key;
        if (root.user_choice) {
          setAppearance(root.id as "light" | "dark");
          setRootPlacement(null);
        } else {
          setRootPlacement(root.id);
        }
      }
      const target = document.querySelector<HTMLElement>(`[data-stage="${stage}"]`);
      if (!target) return;
      target.scrollIntoView({ block: "center" });
      /* THE HIGHLIGHT IS PUBLISHED, not painted from here. The attribute says
         which stage was sent for; this page's own stylesheet decides what that
         looks like, and a gate reads the attribute rather than a colour. */
      setShowStage(stage);
      window.setTimeout(() => setShowStage((prev) => (prev === stage ? null : prev)), 2400);
      /* AND OPEN THE VALUE, because "show me" means the flagged value, not the
         neighbourhood it lives in. */
      if (action.slot) {
        setSelected({ ground, slot: action.slot });
        setAnchorY(null);
      }
    },
    [model, rootKey],
  );

  /* EVERY STAGE AT ONCE, NAMED BY THE ENGINE. The frame reveals its blocks one
     growth beat at a time and holds a "assembling the first surface" overlay
     until `foregrounds` arrives. That choreography is the old page's DOCUMENT
     scene demonstrating how a system grows; here the canvas is a STAGE, and a
     stage that spends six seconds blank is the blank-frame defect with a timer
     on it. `revealAll` reads the keys off the payload, so a stage added to
     `growth.STAGES` needs no edit here. */
  const ready = useMemo(() => revealAll(boot.data?.stages ?? []), [boot.data]);

  /* WHO MAY BE A GUEST on the host ground — the shared query, the same one
     `/design-engine` uses, including the contrasting demo brand a machine
     with one kit falls back to. */
  const { guestsOnOffer, guestsAreDemo } = useGuestBrands(
    (kitsQuery.data?.kits ?? boot.data?.kits ?? []).map((kit) => kit.id),
    brand,
  );

  const openKit = async (id: string) => {
    setError(null);
    try {
      const kit = await engineApi.kit(id);
      const [resolved, css] = await Promise.all([
        engineApi.resolve(kit.brand),
        engineApi.sheet(kit.brand),
      ]);
      setBrand(kit.brand);
      setSavedBrand(kit.brand);
      setModel(resolved);
      setSheet(css);
      /* `null` IS THE BRAND'S OWN DEFAULT, so opening a kit stops pinning a
         literal rung that then survives into the next kit. */
      setRung(null);
      setDirty(false);
      /* A DIFFERENT KIT IS A DIFFERENT QUESTION. Carrying the flag across would
         make the leave-prompt fire for a kit you only looked at, because you
         had saved an earlier one. */
      setSavedThisSession(false);
    } catch (reason) {
      setError(engineError(reason).message);
    }
  };

  const changeBrand = (next: BrandJson) => {
    setBrand(next);
    setDirty(true);
    setError(null);
    if (pending.current) window.clearTimeout(pending.current);
    pending.current = window.setTimeout(async () => {
      try {
        const [resolved, css] = await Promise.all([
          engineApi.resolve(next),
          engineApi.sheet(next),
        ]);
        setModel(resolved);
        setSheet(css);
      } catch (reason) {
        setError(engineError(reason).message);
      }
    }, 140);
  };

  /* SAVING THE KIT THAT PAINTS THE APP REPAINTS THE APP — and this page did not.
     It called `engineApi.save` and stopped there, so a builder who set a system
     active and then edited it saw the canvas move and the app stay put; the old
     page had been fixed for the same defect on 2026-09-06 and the rule never
     travelled. `lib/kit-writes.ts` performs the write, asks the server who is
     active and reloads the link, so neither page can forget it again. */
  const saveBrand = async () => {
    if (!brand) return;
    setSaving(true);
    setError(null);
    try {
      /* AND IT SAYS SO. His words, 2026-09-06: "no idea whether something has
         been saved." The only feedback on either page had been an 8px dirty dot
         900px from the rail going quiet. The old page grew a toast then; this
         one had the write and not the confirmation, which is the same defect
         one page over. `repainted` is the SERVER's answer to "did this change
         what /engine.css serves", never a guess made here. */
      const written = await saveKit(brand, { listKits });
      setSavedBrand(brand);
      setDirty(false);
      setSavedThisSession(true);
      if (written.repainted) {
        toast.success(`${brand.id} saved`, {
          description: "It paints okuro, so the app re-themed.",
        });
      } else {
        toast.success(`${brand.id} saved`);
      }
    } catch (reason) {
      setError(engineError(reason).message);
    } finally {
      setSaving(false);
    }
  };

  const duplicateBrand = async (id: string) => {
    if (!brand) return;
    const next = { ...brand, id: id.trim() };
    setSaving(true);
    setError(null);
    try {
      await createKit(next, { listKits });
      setBrand(next);
      setSavedBrand(next);
      setDirty(false);
      setSavedThisSession(true);
    } catch (reason) {
      setError(engineError(reason).message);
    } finally {
      setSaving(false);
    }
  };

  /* ══ THE KIT LIFECYCLE ══════════════════════════════════════════════════
     Matrix rows 2, 3, 4, 5 and 8. Every WRITE below goes through
     `lib/kit-writes.ts` — convention f2963680, asserted by
     `src/__tests__/correctness/kit-writes-one-owner.test.ts`, which refuses any
     page that reaches `engineApi.create/remove` itself. The module performs the
     call, asks the SERVER who is active through `listKits`, and reloads
     `/engine.css` only when the answer covers the kit just written. None of
     that is re-decided here.

     AND THE RE-READ IS `/kits`, NEVER `/boot` (memory 6829af78): refetching the
     boot payload re-runs the effect that seeds brand/model/sheet/rung and would
     throw away the kit you have open together with every unsaved edit in it. */

  /* CREATE FROM ESSENTIALS — row 2. `POST /essentials` derives a COMPLETE and
     valid brand from a typeface and one colour; `fork.py::fork_kit` is the
     recipe behind it, never `growth.brand_from_essentials`, which drops okuro's
     motion signature and re-derives the signals (charter, CREATION). The old
     page leaves the derived brand on screen as a dirty draft and writes it when
     Save is pressed; here the sheet is a dialog and closing it with nothing
     written would be a system that was never born, so the same brand goes
     straight to `createKit` and the new row is open when the dialog closes. */
  const growFromEssentials = async (id: string, family: string, colour: string) => {
    setLifecyclePending(true);
    setError(null);
    try {
      const result = await engineApi.essentials(id, family, colour);
      await createKit(result.brand, { listKits });
      setCreating(false);
      await openKit(result.brand.id);
    } catch (reason) {
      setError(engineError(reason).message);
    } finally {
      setLifecyclePending(false);
    }
  };

  /* DUPLICATE A KIT YOU CAN SEE — row 3, and the PARTIAL this closes. The rail
     could only copy the OPEN draft, and only when that draft could not be
     saved. Fetching the kit as saved is what makes "duplicate the row I am
     looking at" mean what it says. */
  const forkFromRow = async (id: string) => {
    setError(null);
    try {
      const kit = await engineApi.kit(id);
      setForkSource(kit.brand);
      setForking(true);
    } catch (reason) {
      setError(engineError(reason).message);
    }
  };

  const createFork = async (next: BrandJson) => {
    setLifecyclePending(true);
    setError(null);
    try {
      await createKit(next, { listKits });
      setForking(false);
      setForkSource(null);
      await openKit(next.id);
    } catch (reason) {
      setError(engineError(reason).message);
    } finally {
      setLifecyclePending(false);
    }
  };

  /* DUPLICATE FROM A WEBSITE — row 4. The endpoint does the whole job: it reads
     the pages and forks okuro-ds with the colour, typeface, radius and border
     weight they show, then saves. So the write is the server's and what is left
     here is to re-read the library and OPEN what arrived — opening it is the
     point, because those four scanned values are exactly what a builder will
     want to correct by hand. The 501s carry install guidance in their detail and
     must reach the page rather than be flattened into "scan failed". */
  const scanIntoKit = async (urls: string[], name?: string) => {
    setLifecyclePending(true);
    setError(null);
    try {
      const made = await onboardingApi.extractDesign(urls, name);
      setScanning(false);
      await kitsQuery.refetch();
      await openKit(made.id);
    } catch (reason) {
      setError(engineError(reason).message);
    } finally {
      setLifecyclePending(false);
    }
  };

  /* DELETE — rows 5 and 8, and the one act here that cannot be undone.
     DELETING THE KIT THAT PAINTS THE APP also changes what `/engine.css`
     answers: the server falls back to okuro's own kit. That is the same class
     as a save, in the other direction, and `deleteKit` owns both — it reads the
     listing BEFORE the delete, because afterwards the id is gone either way and
     "was it the one painting the app" can no longer be asked. */
  const removeKit = async (id: string) => {
    setError(null);
    try {
      const written = await deleteKit(id, { listKits });
      toast.success(`${id} deleted`);
      /* If the editor had that kit open it is now showing a brand with no file
         behind it. Fall back to the first kit that still exists rather than
         leaving a canvas whose Save would silently re-create what was deleted. */
      if (brand?.id === id) {
        const next = written.listing?.kits?.[0]?.id;
        if (next) await openKit(next);
      }
    } catch (reason) {
      setError(engineError(reason).message);
    }
  };

  const resetBrand = () => {
    if (!savedBrand) return;
    setBrand(savedBrand);
    setDirty(false);
    setError(null);
    void Promise.all([engineApi.resolve(savedBrand), engineApi.sheet(savedBrand)]).then(([resolved, css]) => {
      setModel(resolved);
      setSheet(css);
    }).catch((reason) => setError(engineError(reason).message));
  };

  const copyComponentImport = async () => {
    if (!selectedComponent) return;
    const statement = `import { ${selectedComponent.name.replace(/\s/g, "")} } from "@/components/ui/${selectedComponent.id}";`;
    try {
      if (navigator.clipboard) {
        await navigator.clipboard.writeText(statement);
      } else {
        const field = document.createElement("textarea");
        field.value = statement;
        field.style.position = "fixed";
        field.style.opacity = "0";
        document.body.appendChild(field);
        field.select();
        document.execCommand("copy");
        field.remove();
      }
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setError("The component import could not be copied. Clipboard access was refused.");
    }
  };

  /* ── the two controls his 2026-09-12 rulings add ─────────────────────────
     ISSUE 6 IS OPTION D-C: a row PREVIEWS, and one explicit control makes the
     opened kit the one that paints okuro. No row click ever writes the profile,
     which is why `openKit` above is unchanged. The write itself is
     `lib/active-kit.ts` — the same function /settings' preset select calls, so
     "set as default" cannot mean two things in two places. */
  const openedEditable =
    (kitsQuery.data?.kits ?? boot.data?.kits ?? []).find((kit) => kit.id === brand?.id)
      ?.editable ?? false;

  const makeActive = useCallback(async () => {
    if (!brand) return;
    setSettingActive(true);
    setError(null);
    try {
      await setActiveKit(brand.id);
      /* THE BADGE MOVES BECAUSE THE SERVER SAYS SO. Refetching `/kits` re-reads
         `active` through the same resolver `/engine.css` uses; setting a local
         "it is active now" flag would be a projection that computes, i.e. a
         second authority for a question one layer already answers. */
      await kitsQuery.refetch();
    } catch (reason) {
      setError(engineError(reason).message);
    } finally {
      setSettingActive(false);
    }
  }, [brand, kitsQuery]);

  /* HIS NEW RULE: leaving the editor with a system you worked on and never made
     live asks the question once. The hook owns the semantics and the dialog;
     this page owns what "save" and "duplicate" mean on it. */
  const { dialog: leaveDialog } = useLeaveGuard({
    openedKit: brand?.id ?? null,
    activeKit,
    dirty,
    savedThisSession,
    editable: openedEditable,
    onSetDefault: async () => {
      if (!brand) return;
      if (dirty && openedEditable) await saveBrand();
      await makeActive();
    },
    onDuplicate: () => {
      /* THE EXISTING FLOW, REVEALED — never a second fork path. The duplicate
         affordance is already in the configuration rail (it renders for exactly
         the kits that cannot be saved); this opens the rail on narrow viewports
         and puts the control in front of the eye. */
      setMobileConfigOpen(true);
      window.requestAnimationFrame(() => {
        const flow = document.querySelector<HTMLElement>("[data-duplicate-flow]");
        flow?.scrollIntoView({ block: "center" });
        flow?.querySelector<HTMLInputElement>("input")?.focus();
      });
    },
  });

  /* THE LOCAL NAV MOVES ONTO THE PLATE — R5, the owner 2026-09-17.
     =========================================================================
     This page was the last leaf still drawing its own top bar. As a `position:
     sticky` header inside `.c-panes` it pinned BEHIND the shell's plate, which
     is the same defect ASSETS, BRAIN and KNOWLEDGE were fixed for; it survived
     the sweep only because it declares the sticky in the engine's stylesheet
     rather than with a Tailwind utility, so a grep for `sticky top-0` could not
     see it.

     NO TITLE IS INVENTED. The slot carries `actions` only, so the plate keeps
     the shell's derived "Design" as its one `<h1>` (R1). This bar never said
     what the page is FOR — it is navigation — so there is nothing here the
     shell cannot already derive, and `title` would be a second name for one
     leaf.

     THE SPLIT STOPS HERE. The configuration rail's sticky pieces stay where
     they are: that rail scrolls its own column and pinning its head is page
     behaviour, not shell chrome. See the note on `.dsc-local-nav` in
     `ds-engine-codex.css`.

     MEMOISED ON `activeSection` ALONE, because that is the only value the JSX
     below reads — `useSectionTitle` keys on the slot's identity and refuses to
     guess a dependency list for state it cannot see (`PageTitle.tsx`).

     ABOVE THE TWO GUARD CLAUSES, NOT BESIDE THE JSX. `boot.isLoading` and the
     failure branch return early, so a hook placed next to the markup it feeds
     runs on some renders and not others — React counted more hooks than the
     previous render and the ErrorBoundary swallowed the whole leaf. Measured,
     not reasoned about: the page rendered blank with
     "Rendered more hooks than during the previous render". The nav also has
     nothing to wait for; `activeSection` has a value from the first frame. */
  const header = useMemo(
    () => ({
      actions: (
        <div className="dsc-local-nav ds-n7 ds-leads" aria-label="Design engine sections">
          <a className="dsc-wordmark ds-n7 ds-leads" href="#systems" aria-label="Okuro design engine home">
            <span>OKURO</span><i />ENGINE
          </a>
          <nav>
            {SECTION_LINKS.map(([id, label]) => (
              <a key={id} href={`#${id}`} data-active={activeSection === id || undefined}>{label}</a>
            ))}
          </nav>
          <span className="dsc-release ds-n8 ds-leads"><span />LIVE MODEL</span>
        </div>
      ),
    }),
    [activeSection],
  );
  useSectionTitle(header);

  if (boot.isLoading) return <EngineLoading />;
  if (boot.isError || !boot.data || !brand || !model) {
    return <EngineFailure message={engineError(boot.error).message} />;
  }
  const payload = boot.data;
  /* THE LIBRARY IS THE LIVE LISTING, NOT THE BOOT PAYLOAD — and until wave 5
     it was the boot payload, which never refetches. A kit created, scanned or
     deleted on this page would have left the table exactly as it was. `/kits`
     is the query every write already re-reads through `listKits`, so the rows
     and the write module read one answer. `/boot` stays untouched for the same
     reason it always did (memory 6829af78). */
  const kitRows = kitsQuery.data?.kits ?? payload.kits;

  /* ── WHAT THE INSPECTOR IS SHOWING ──────────────────────────────────────
     Four reads of the model, no computation. `derivationBySlot` and the rule
     lookup are the same ones `/design-engine` makes; the flag is found by the
     code the MARK carried, in the brand's own lint first and the ground's
     palette second, because the same finding is reported from both places and
     the brand-level one is the address the fields use. */
  const selectedGround = selected
    ? (groundsByKey[selected.ground] ?? groundsByKey[rootKey] ?? null)
    : null;
  const selectedDerivation = derivationBySlot(selectedGround, selected?.slot ?? null);
  const selectedRule =
    payload.rules.find((rule) => rule.id === selectedDerivation?.rule) ?? null;
  const selectedFlag = selected?.flag
    ? (model.flags.find((f) => f.code === selected.flag) ??
       selectedGround?.palette.flags.find((f) => f.code === selected.flag) ??
       null)
    : null;


  return (
    <main className="dsc-page ds-surface ds-n5 ds-paragraphs" data-page="ds-engine-codex">
      <div className="dsc-layout">
        <div className="dsc-flow">
          <section id="systems" className="dsc-section dsc-hero">
            <div className="dsc-eyebrow ds-n8 ds-leads">DESIGN SYSTEM ENGINE / 01</div>
            {/* R1 (86b8f1f0) — h2, NOT h1, AND THE REASON IS THE HEADING
                LADDER RATHER THAN THE TITLE RULE. This line is not a leaf
                title: DESIGN is the one SYSTEM leaf whose lead is editorial,
                and it stays on screen. But `TopicBar` renders
                `<h1 class="c-title">Design</h1>` above this pane, so an h1
                here is a SECOND top-level heading inside one pane region —
                and the page's other two sections already head themselves with
                `h2` (`demo-title`, the components section), so h1 here made
                the ladder skip a level backwards. The type class is unchanged,
                so nothing moves visually. */}
            <h2 className="ds-h1 ds-titles">One identity.<br />Every interface decision.</h2>
            <p className="dsc-deck ds-n4 ds-leads">Select an available system, inspect the generated result, then trace every component back to the rule that shaped it.</p>
            <div className="dsc-actions">
              <Button asChild><a href="#demo">Open the live system <ArrowDown /></a></Button>
              <Button variant="outline" asChild><a href="#components">Browse {COMPONENT_DOCS.length} components</a></Button>
            </div>
            <div className="dsc-proofline" aria-label="System summary">
              <div><strong className="ds-n2 ds-headings">{payload.kits.length}</strong><span className="ds-n8 ds-leads">available systems</span></div>
              <div><strong className="ds-n2 ds-headings">{model.grounds.length}</strong><span className="ds-n8 ds-leads">resolved grounds</span></div>
              <div><strong className="ds-n2 ds-headings">{model.authored.length}</strong><span className="ds-n8 ds-leads">authored inputs</span></div>
              <div><strong className="ds-n2 ds-headings">{COMPONENT_DOCS.length}</strong><span className="ds-n8 ds-leads">live primitives</span></div>
            </div>
          </section>

          <section className="dsc-section dsc-systems" aria-labelledby="systems-title">
            <div className="dsc-section-heading">
              <div>
                <span className="ds-n8 ds-leads">AVAILABLE SYSTEMS</span>
                <h2 id="systems-title" className="ds-h3 ds-headings">Choose the identity. The logic stays.</h2>
                {/* ── THE TWO WAYS A SYSTEM IS BORN — matrix rows 2 and 4 ──
                    Beside the library they land in, because both of them WRITE
                    a row into this table. A scan IS a duplication — it forks
                    okuro-ds with what a website shows — which is why its home
                    is beside the other one rather than on Settings. */}
                <div className="dsc-header-actions">
                  <Button size="sm" type="button" data-action="create" disabled={lifecyclePending} onClick={() => setCreating(true)}>New system</Button>
                  <Button size="sm" variant="outline" type="button" data-action="scan" disabled={lifecyclePending} onClick={() => setScanning(true)}>Duplicate from a website</Button>
                </div>
              </div>
              <p className="ds-n5 ds-paragraphs">Every row is a real engine kit. Shipped systems are protected; user systems remain editable.</p>
            </div>
            <div className="dsc-system-table" role="list">
              {kitRows.map((kit, index) => {
                const selected = kit.id === brand.id;
                const armed = armedDelete === kit.id;
                /* THE ROW PUBLISHES WHICH KIT IT IS (`data-kit-row`). Without
                   it the only way to reach one row is its class name or its
                   text, and a gate that sniffs either is testing the
                   implementation. The old page publishes the same thing as
                   `data-system-row`. */
                return (
                  /* A ROW IS NO LONGER ONE BUTTON — wave 5. Duplicate and
                     Delete live in the row and a button cannot contain a
                     button. `data-kit-row` and `data-selected` stay on the OPEN
                     control, unmoved, because that is what every existing gate
                     clicks and reads; the wrapper publishes `data-system-row`,
                     the same name the old page's library uses for the same
                     thing. */
                  <div className="dsc-system-row" key={kit.id} role="listitem" data-system-row={kit.id} data-origin={kit.origin}>
                  <button className="ds-n6 ds-paragraphs" type="button" onClick={() => void openKit(kit.id)} data-kit-row={kit.id} data-selected={selected || undefined}>
                    <span className="dsc-system-index ds-n8 ds-paragraphs">{String(index + 1).padStart(2, "0")}</span>
                    <span className="dsc-system-swatch" style={{ background: kit.colour ?? "var(--color-background-subtle)" }} />
                    <span className="dsc-system-name"><strong className="ds-n5 ds-headings">{kit.id}</strong><small className="ds-n7 ds-paragraphs">{kit.font ?? "Typeface unavailable"}</small></span>
                    {/* READ, NEVER COMPUTED. `active` is the kit /engine.css is
                        painting the app with and `opened` is the one this page
                        is showing; both arrive on the boot payload and the page
                        read neither, so a row gave no way to tell a preview from
                        the live system. Deriving either from `brand.id` plus an
                        assumption is the shape the charter calls a projection
                        that computes, i.e. a second authority. */}
                    <span className="dsc-system-origin ds-n7 ds-paragraphs">
                      {kit.id === activeKit ? "Painting the app" : kit.id === brand.id ? "Opened" : kit.editable ? "Editable" : "Protected"}
                    </span>
                    <span className="dsc-system-state ds-n7 ds-leads">{selected ? <><Check /> Open</> : "Inspect"}</span>
                  </button>
                  <div className="dsc-system-actions">
                    {/* DUPLICATE ON EVERY ROW — matrix row 3. Including the
                        shipped one: forking okuro-ds is the SANCTIONED path,
                        the sentence its "Protected" badge is the first half
                        of. */}
                    <button type="button" className="dsc-quiet ds-n8 ds-leads" data-action="fork" data-kit={kit.id} disabled={lifecyclePending || Boolean(kit.unreadable)} onClick={() => void forkFromRow(kit.id)}>Duplicate</button>
                    {/* DELETE EXISTS ONLY WHERE IT CAN SUCCEED — matrix row 5.
                        `store.save` refuses a shipped id with a 409, and a
                        button whose only outcome is an error teaches that the
                        refusal is a malfunction. The badge already says why it
                        is absent. TWO PRESSES on the same control; the label
                        changes with the colour, so colour is never the only
                        channel. */}
                    {kit.editable && (
                      <button
                        type="button"
                        className="dsc-quiet ds-n8 ds-leads"
                        data-action={armed ? "delete-confirm" : "delete"}
                        data-kit={kit.id}
                        disabled={lifecyclePending}
                        aria-label={armed ? `Confirm deleting ${kit.id}` : `Delete ${kit.id}`}
                        onClick={() => {
                          if (armed) {
                            setArmedDelete(null);
                            void removeKit(kit.id);
                          } else {
                            setArmedDelete(kit.id);
                          }
                        }}
                        onBlur={() => setArmedDelete((prev) => (prev === kit.id ? null : prev))}
                      >
                        {armed ? "Really?" : "Delete"}
                      </button>
                    )}
                  </div>
                  </div>
                );
              })}
            </div>
          </section>

          <section id="demo" className="dsc-section dsc-demo" aria-labelledby="demo-title">
            <div className="dsc-section-heading">
              <div><span className="ds-n8 ds-leads">LIVE SYSTEM / 02</span><h2 id="demo-title" className="ds-h3 ds-headings">A real product surface, wearing {brand.id}.</h2></div>
              <p className="ds-n5 ds-paragraphs">The entire page consumes the engine’s emitted CSS. Change the view or authored system and every surface re-resolves together.</p>
            </div>
            <div className="dsc-demo-toolbar">
              <Segmented ariaLabel="Preview appearance" value={appearance} onChange={setAppearance} options={[{ value: "light" as const, label: "Light" }, { value: "dark" as const, label: "Dark" }]} />
              {/* ── THE OTHER FIVE GROUNDS ────────────────────────────────
                  Seven roots resolve from a system; this page printed the
                  number and rendered none of them (matrix row 40). Light and
                  dark are the user's two and live in the switch beside this;
                  brand-full and the four signals are BUILDER PLACEMENTS, so
                  they are chips. Same component, same published attributes as
                  the old page — only the chip class is ours. */}
              <div className="dsc-ground-chips ds-n8 ds-leads" aria-label="Root ground placement">
                <RootPlacementChips
                  roots={model.roots}
                  value={rootPlacement}
                  onChange={setRootPlacement}
                  chipClassName="dsc-ground-chip"
                />
              </div>
              {/* ── THE SIZE RUNG ────────────────────────────────────────
                  ENUMERATED FROM THE ENGINE, not typed here. `Default` is a
                  real rung on this row rather than an empty state: it is what
                  the brand itself says, and naming it is how a reader learns
                  the brand HAS a default. Every attribute below is the one the
                  old page publishes for the same control — a gate that finds a
                  rung by its visible label would be testing the copy. */}
              <div
                role="radiogroup"
                aria-label="size rung"
                data-rung-switch
                className="dsc-rung-picker ds-n8 ds-leads"
              >
                {[null, ...(model.sizes.text_rungs ?? [])].map((option) => (
                  <button
                    key={option ?? "default"}
                    type="button"
                    role="radio"
                    aria-checked={rung === option}
                    aria-label={option ?? "the brand's default rung"}
                    data-rung={option ?? "default"}
                    data-active={rung === option || undefined}
                    onClick={() => setRung(option)}
                  >
                    {option ?? "Default"}
                  </button>
                ))}
              </div>
              <button className="dsc-scale-toggle ds-n8 ds-leads" type="button" data-scale-switch aria-pressed={scaled} aria-label="downscale the scene" data-active={scaled || undefined} onClick={() => setScaled((value) => !value)}>Downscale</button>
            </div>
            {/* `data-pane="canvas"` IS WHAT THE SHARED INSPECTOR DISMISSES ON.
                A click on the chrome — a chip, a rung, the rail — is not a
                dismissal; a click elsewhere on the CANVAS is, because that is
                the gesture that means "I am looking at something else now".
                The attribute is published rather than the class read, so the
                shared panel needs to know nothing about this page. */}
            <div className="dsc-system-stage" data-pane="canvas" ref={paneRef} data-stage="live-system" data-show-target={showStage === "live-system" ? "" : undefined}>
              <PreviewFrame
                title="the live system"
                sheet={sheet}
                model={judgedModel ?? model}
                tree={keyedTree}
                ready={ready}
                appearance={appearance}
                rootPlacement={rootPlacement}
                rung={rung}
                scaled={scaled}
                variant="showcase"
                onMessage={onCanvasGesture}
              >
                {() => (
                  <LiveDemo
                    system={brand.id}
                    grounds={model.grounds.length}
                    components={COMPONENT_DOCS.length}
                  />
                )}
              </PreviewFrame>
              {/* ── THE INSPECTOR, from a value or from a mark ─────────────
                  Matrix rows 35 and 36. Anchored at the height of the gesture
                  on the canvas's right edge, in this page's own chrome. Every
                  field is READ off the derivation the engine published. */}
              <Inspector
                derivation={selectedDerivation}
                rule={selectedRule}
                ground={selectedGround}
                flag={selectedFlag}
                colours={model.brand_colours}
                anchorY={anchorY}
                onPick={(slot) =>
                  setSelected((prev) => (prev ? { ...prev, slot, flag: undefined } : prev))
                }
                onClose={() => setSelected(null)}
                vocabulary={CODEX_VOCABULARY}
                width="var(--dsc-inspector)"
                padding="16px"
              />
            </div>
            {/* THE CHOREOGRAPHY, NAMED BY THE ENGINE — matrix row 45. This was
                five words typed on the page ("Authored · Measured · Generated
                · Inherited · Consumed") beside a canvas that reveals itself in
                the ORDER `growth.STAGES` declares. Two accounts of one sequence,
                and the typed one could not be wrong in a way anything noticed.
                `payload.stages` is the account the frame animates. */}
            <div className="dsc-logic-strip" data-stage-register>
              {payload.stages.map((stage, index) => (
                <div key={stage.key} data-stage-caption={stage.key}>
                  <span className="ds-n8 ds-paragraphs">{String(index + 1).padStart(2, "0")}</span>
                  <strong className="ds-n7 ds-leads">{stage.title}</strong>
                  <small className="ds-n8 ds-paragraphs">{stage.caption}</small>
                </div>
              ))}
            </div>
            {/* ── NESTED GUESTS, LIVE ───────────────────────────────────
                Matrix row 42. The atlas below states the inheritance contract
                as a table; this is the same contract RESOLVED — guest brands
                placed on the host's ground, each letter answered by
                `engineApi.nesting` rather than by a rule written on the page.
                The table stays: one documents, one proves.

                IT WEARS THIS PAGE'S CHROME. `vocabulary` is how the shared
                component learns our class names; nothing about the old page's
                look reaches here. See `components/ds-engine-codex/vocabulary.ts`
                and the note beside the import at the top of this file. */}
            {guestsOnOffer.length > 0 && (
              <div className="dsc-nesting" data-stage="guests" data-show-target={showStage === "guests" ? "" : undefined}>
                <NestingSection
                  host={brand}
                  available={guestsOnOffer}
                  demo={guestsAreDemo}
                  vocabulary={CODEX_VOCABULARY}
                />
              </div>
            )}
            <LogicAtlas model={model} rules={payload.rules} stages={payload.stages} />
          </section>

          <section id="components" className="dsc-section dsc-components" aria-labelledby="components-title">
            <div className="dsc-section-heading">
              <div><span className="ds-n8 ds-leads">COMPONENTS / 03</span><h2 id="components-title" className="ds-h3 ds-headings">Behavior stays local. Appearance inherits.</h2></div>
              <p className="ds-n5 ds-paragraphs">Every vendored primitive is indexed. Select one to inspect its purpose, contract, configuration and live states.</p>
            </div>
            {/* ── ALL 33 ON ONE CANVAS, matrix width and forced states ──────
                Matrix rows 38, 39 and 41. The scene below it is a REFERENCE
                view — one component, its prose, its contract — and that is a
                different job, not a duplicate. This is the evidence: every
                vendored primitive alive at once, in one resolved ground, with
                the variant matrix and its `data-demo` companions.

                `fill` rather than the auto-height form on purpose. The scene is
                some seven thousand pixels tall; measured into the page it would
                turn one section into most of the document. Bounded, the frame's
                own document scrolls and the page keeps its shape. */}
            <div className="dsc-components-canvas" data-stage="components">
              <PreviewFrame
                fill
                className="h-full w-full"
                title="every primitive, live"
                sheet={sheet}
                model={judgedModel ?? model}
                tree={keyedTree}
                ready={ready}
                appearance={appearance}
                rootPlacement={rootPlacement}
                rung={rung}
                scaled={scaled}
                variant="showcase"
                onMessage={onCanvasGesture}
              >
                {(doc) => <ComponentShowcase doc={doc} />}
              </PreviewFrame>
            </div>

            <div className="dsc-component-layout">
              <aside className="dsc-component-menu" aria-label="Component index">
                {COMPONENT_GROUPS.map((group) => <div key={group.id}><h3 className="ds-n8 ds-leads">{group.label}</h3>{group.components.map((component) => <button className="ds-n7 ds-paragraphs" key={component.id} type="button" data-active={component.id === componentId || undefined} onClick={() => setComponentId(component.id)}>{component.name}</button>)}</div>)}
              </aside>
              <div className="dsc-component-document">
                <div className="dsc-component-title"><div><span className="ds-n8 ds-leads">{selectedComponent?.group.replace(/-/g, " ")}</span><h3 className="ds-h5 ds-headings">{selectedComponent?.name}</h3><p className="ds-n5 ds-paragraphs">{selectedComponent?.purpose}</p></div><Button variant="outline" size="sm" onClick={() => void copyComponentImport()}><Copy /> {copied ? "Copied" : "Copy import"}</Button></div>
                <div className="dsc-specimen" data-component={selectedComponent?.id}>
                  <div className="dsc-specimen-toolbar ds-n8 ds-leads"><span>INTERACTIVE PREVIEW</span><span>{brand.id} / {appearance} / {rung}</span></div>
                  <div className="dsc-specimen-stage" data-stage="specimen">
                    <PreviewFrame
                      title="the component specimen"
                      sheet={sheet}
                      model={judgedModel ?? model}
                      tree={keyedTree}
                      ready={ready}
                      appearance={appearance}
                      rootPlacement={rootPlacement}
                      rung={rung}
                      scaled={scaled}
                      variant="showcase"
                      onMessage={onCanvasGesture}
                    >
                      {/* THE SAME SPECIMEN THE FULL SCENE RENDERS, selected out
                          of it — not a second implementation of it. The switch
                          statement this replaces lived in `native-specimens.tsx`
                          and was already one variant behind the canvas. */}
                      {(doc) => (
                        <ComponentShowcase
                          doc={doc}
                          only={selectedComponent?.id ?? "button"}
                        />
                      )}
                    </PreviewFrame>
                  </div>
                </div>
                <div className="dsc-doc-grid">
                  <article><span className="ds-n8 ds-leads">CONFIGURATION</span><table className="ds-n7 ds-paragraphs"><tbody>{selectedComponent?.configuration.map((item) => <tr key={item}><th>{item}</th><td>Component API</td></tr>)}</tbody></table></article>
                  <article><span className="ds-n8 ds-leads">BEHAVIOR</span><p className="ds-n6 ds-paragraphs">{selectedComponent?.behavior}</p><span className="ds-n8 ds-leads">ACCESSIBILITY</span><p className="ds-n6 ds-paragraphs">{selectedComponent?.accessibility}</p></article>
                </div>
                <div className="dsc-inheritance-table"><span className="ds-n8 ds-leads">INHERITANCE CONTRACT</span><table className="ds-n7 ds-paragraphs"><thead><tr><th>Layer</th><th>Owns</th><th>Source</th></tr></thead><tbody><tr><td>Component</td><td>Behavior · state · content</td><td>{selectedComponent?.name}</td></tr><tr><td>Frame</td><td>Ground · rung · scale</td><td>Nearest ancestor</td></tr><tr><td>Engine</td><td>Color · type · space · radius · motion</td><td>{brand.id}</td></tr></tbody></table></div>
              </div>
            </div>
          </section>
        </div>

        <aside className="dsc-config" aria-label="Design system configuration" data-mobile-open={mobileConfigOpen || undefined}>
          <div className="dsc-config-head"><div><span className="ds-n8 ds-leads">CONFIGURATION</span><strong className="ds-n6 ds-headings">{brand.id}</strong></div>{/* THE COUNT IS GONE FROM HERE — wave 3. `2 findings` in this head and the
              same number again in the System logic section were two readings of a
              set, neither of which said anything about it; the verdict band
              directly below is the judgement now. What stays is the dot, and it
              wears the verdict's own tone instead of being green whatever the
              engine found. Read from `lib/flags.ts::verdictOf`, the same
              function the band states its sentence from, so the two cannot
              disagree.

              `data-health-verdict`, NOT `data-verdict`: the second is the
              band's published identity, and two elements answering to it would
              make "the verdict" ambiguous to anything that reads the page —
              which is the same reading-in-two-places defect the band exists to
              end. */}
              <div className="dsc-health ds-n8 ds-paragraphs" data-health-verdict={verdictOf(model.flags)}><i /><button type="button" onClick={() => setMobileConfigOpen(false)} aria-label="Close configuration"><X /></button></div></div>
          {/* SET AS ACTIVE — his ruling D-C, 2026-09-12. A row PREVIEWS; this
              is the only control on the page that writes the profile, and it
              says which kit it is about. It goes QUIET rather than away when
              the opened kit already paints the app: a control that vanishes
              teaches nobody where it went, and the sentence beside it is the
              honest answer to "why can I not press this". */}
          <div className="dsc-config-active" data-set-active-row>
            {brand.id === activeKit ? (
              <span className="ds-n8 ds-paragraphs"><Check /> This system paints okuro</span>
            ) : (
              <>
                <span className="ds-n8 ds-paragraphs">Previewing. okuro is painted with <strong>{activeKit ?? "its own system"}</strong>.</span>
                <Button size="sm" type="button" disabled={settingActive} onClick={() => void makeActive()} data-set-active>
                  {settingActive ? "Setting…" : "Set as active"}
                </Button>
              </>
            )}
          </div>
          {error && <div className="dsc-error"><CircleAlert />{error}</div>}
          <ConfigurationPanel
            brand={brand}
            model={model}
            flags={flags}
            onAcknowledge={acknowledge}
            onShow={showScene}
            scenes={REACHABLE_SCENES}
            families={payload.families}
            appearance={appearance}
            rung={rung}
            viewportRungs={viewportRungs}
            mobileMaxPx={payload.viewport?.mobile_max_px ?? null}
            onViewportRung={(viewport, value) => void applyViewportRung(viewport, value)}
            scaled={scaled}
            editable={payload.kits.find((kit) => kit.id === brand.id)?.editable ?? false}
            dirty={dirty}
            saving={saving}
            onAppearance={setAppearance}
            onRung={setRung}
            onScaled={setScaled}
            onChange={changeBrand}
            onReset={resetBrand}
            onSave={() => void saveBrand()}
            onDuplicate={(id) => void duplicateBrand(id)}
          />
        </aside>
      </div>
      <button className="dsc-config-mobile-trigger ds-n7 ds-leads" type="button" onClick={() => setMobileConfigOpen(true)} aria-expanded={mobileConfigOpen}><SlidersHorizontal /> Configure <span className="ds-n8 ds-leads">{model.flags.length}</span></button>
      {leaveDialog}
      {/* ══ THE KIT LIFECYCLE SHEETS — matrix rows 2, 3 and 4 ═══════════════
          Three sheets, one code path each, and each is the SAME file
          `/design-engine` mounts. Nothing below re-implements a recipe. */}
      <Dialog open={creating} onOpenChange={setCreating}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Start a new system</DialogTitle>
            <DialogDescription>
              A typeface and one brand colour. Everything else derives from
              those two, and the result is saved in your instance, never in
              okuro&rsquo;s package.
            </DialogDescription>
          </DialogHeader>
          <EssentialsGate
            families={payload.families}
            busy={lifecyclePending}
            error={error}
            onGrow={(id, family, colour) => void growFromEssentials(id, family, colour)}
            vocabulary={CODEX_VOCABULARY}
          />
        </DialogContent>
      </Dialog>

      {forkSource && (
        <ForkKitDialog
          open={forking}
          onOpenChange={(open) => {
            setForking(open);
            if (!open) setForkSource(null);
          }}
          source={forkSource}
          /* AS SAVED, not as displayed — this is a row the reader is looking
             at, and the dialog says which of the two it is rather than making
             a promise that is false here. */
          fromScreen={false}
          taken={kitRows.map((kit) => kit.id)}
          pending={lifecyclePending}
          onCreate={(next) => void createFork(next)}
        />
      )}

      <ScanKitDialog
        open={scanning}
        onOpenChange={setScanning}
        pending={lifecyclePending}
        onScan={(urls, name) => void scanIntoKit(urls, name)}
      />
    </main>
  );
}

/* THESE TWO STATES USED TO PRINT AN `<h1>` EACH — audit C4-4, and the only
   two left in the page tree once R1 had swept the rest. The shell renders
   the leaf's `<h1 class="c-title">Design</h1>` on the plate ABOVE this pane
   in every state, including these, so each of them was the document's second
   heading. The RANK was the defect, never the line: `type-title` is a
   typography role, not a tag, so the text is unchanged at the same size and
   the document keeps exactly one h1. `role="status"` says what the block IS
   now that it is no longer a heading — a live region announcing the
   resolve. */
function EngineLoading() {
  return <main className="dsc-state" role="status"><span className="type-label">DESIGN ENGINE</span><p className="type-title">Resolving the system…</p><p className="type-body">Loading kits, rules, fonts, emitted CSS and the resolved model in one pass.</p></main>;
}

function EngineFailure({ message }: { message: string }) {
  return <main className="dsc-state" role="alert"><span className="type-label">DESIGN ENGINE</span><p className="type-title">The system did not resolve.</p><p className="type-body">{message}</p></main>;
}
