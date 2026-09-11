/**
 * /design-engine — configure a brand, and know whether it is any good.
 *
 * TWO JOBS, in this order when they conflict:
 *
 *   1. JUDGE — is my configuration good? Answered by the VERDICT BAND, at 20px,
 *      above the fold, in one of three sentences, with a glyph, a colour and one
 *      action. The page used to answer this with the substring `3 flags`, at
 *      7px, in grey, in a toolbar, unclickable.
 *
 *   2. CONFIGURE — change a brand. Eight primary decisions in a 320px rail,
 *      each showing its resolved value and owning one reserved help slot, then
 *      seven one-level collapses. It used to be 137 flat controls over 5.7
 *      screens with 7px labels.
 *
 * AND THE REQUIREMENT THAT PRECEDES BOTH, ratified and unchanged: a builder
 * must SEE THE SYSTEM GROW. It still does — one 40px line that names the stage
 * and fills a rule left to right, collapsing afterwards to a 32px row with a
 * replay. What is gone is the artefact: twelve identically-styled 10px boxes
 * that wrapped into two rows at 1440, destroying any reading of them as a
 * sequence, under a caption truncated at every viewport including 2560.
 *
 * THERE ARE NO TABS ON THIS PAGE. That is not a style decision: `<Tabs
 * defaultValue="rule">` was UNCONTROLLED, so clicking a value on the canvas
 * while GRAPH, NUMBERS or GUESTS was showing changed nothing anywhere — three
 * of four tabs silently ate the page's central explanatory act. With no tabs
 * there is nothing to swallow it, and the inspector opens from every scene.
 *
 * `String(err)` APPEARS NOWHERE. A refusal is parsed into an `EngineError` and
 * routed to the help slot of the field it names, or into the verdict band when
 * it names none.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { Segmented } from "@/components/ui/segmented";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import "@/components/design-engine/chrome.css";
import { engineApi, engineError } from "@/components/design-engine/engine-api";
import { EssentialsGate } from "@/components/design-engine/essentials";
import { ForkKitDialog } from "@/components/design-engine/fork-kit-dialog";
import { derivationBySlot } from "@/components/design-engine/panels";
import { NestingSection, GuestsEmpty } from "@/components/design-engine/nesting";
import { ComponentShowcase } from "@/components/design-engine/component-showcase";
import { KitShowcase } from "@/components/design-engine/kit-showcase";
import { PreviewFrame, type FrameMessage } from "@/components/design-engine/preview-frame";
import { ScanKitDialog } from "@/components/design-engine/scan-kit-dialog";
import { onboardingApi } from "@/lib/api";
import { reloadEngineSheet } from "@/lib/theme";
import { toast } from "@/components/ui/toast";
import { AuthoringRail, setPath, slotsNamedBy } from "@/components/design-engine/rail";
import {
  ChromeStrip,
  Rail,
  Toolbar,
  useAutoCollapsedChrome,
} from "@/components/design-engine/shell";
import { SCENES, TokensScene, type Scene } from "@/components/design-engine/scenes";
import { SystemsScene } from "@/components/design-engine/systems";
import { Inspector } from "@/components/design-engine/inspector";
import {
  VerdictBand,
  useAcknowledged,
  withAcknowledgements,
} from "@/components/design-engine/verdict";
import type { AdviceAction } from "@/components/design-engine/severity";
import type {
  BrandJson,
  Flag,
  ResolvedModel,
  TreeNode,
} from "@/components/design-engine/types";

/**
 * Re-key a tree against a model.
 *
 * The tree stores REQUESTS, never colours. Ground keys are looked up in the
 * engine's transition table on every model change, so a colour edit re-keys the
 * whole tree without the builder's structure moving at all.
 */
function retree(
  node: TreeNode,
  parentKey: string,
  transitions: Record<string, Record<string, string>>,
): TreeNode {
  const key = node.request
    ? (transitions[parentKey]?.[node.request] ?? parentKey)
    : parentKey;
  return {
    ...node,
    groundKey: key,
    children: node.children.map((child) => retree(child, key, transitions)),
  };
}

function mapTree(node: TreeNode, fn: (n: TreeNode) => TreeNode): TreeNode {
  const next = fn(node);
  return { ...next, children: next.children.map((child) => mapTree(child, fn)) };
}

function dropNode(node: TreeNode, id: string): TreeNode {
  return {
    ...node,
    children: node.children
      .filter((child) => child.id !== id)
      .map((child) => dropNode(child, id)),
  };
}

const EMPTY_TREE: TreeNode = { id: "root", request: null, groundKey: "", children: [] };

/** The two escalations of a slow boot, in seconds. See the loading note below. */
const SLOW = 8_000;
const VERY_SLOW = 30_000;

export function DesignEnginePage() {
  const [params, setParams] = useSearchParams();

  /* THE LAYOUT IS THE QUERY STRING. Holding it in state as well would be two
     sources for one fact, and the one the URL carries is the one a builder can
     send to somebody else. Absent means OPEN — a bare URL is the whole
     workbench rather than a surprise. */
  /* The rail is a real split pane on a workspace-sized viewport. On a phone the
     canvas is the first thing a reader needs to see, so the URL can still force
     it open but a bare link starts with the authoring surface collapsed. */
  const railOpen = params.has("rail")
    ? params.get("rail") !== "0"
    : typeof window === "undefined" || window.innerWidth >= 901;
  const scene = (params.get("scene") as Scene) || "document";

  const setParam = useCallback(
    (key: string, value: string) => {
      setParams(
        (previous: URLSearchParams) => {
          const next = new URLSearchParams(previous);
          next.set(key, value);
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  const [brand, setBrand] = useState<BrandJson | null>(null);
  const [model, setModel] = useState<ResolvedModel | null>(null);
  const [sheet, setSheet] = useState("");
  const [authoring, setAuthoring] = useState(false);
  /** Set when the sheet is opened from the GUESTS empty state. See §9. */
  const [seed, setSeed] = useState<{ id: string; colour: string } | undefined>();
  const [busy, setBusy] = useState(false);
  const [gateError, setGateError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [pageError, setPageError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);

  const [ready, setReady] = useState("");
  const [appearance, setAppearance] = useState<"light" | "dark">("light");
  const [rootPlacement, setRootPlacement] = useState<string | null>(null);
  /**
   * THE SCENE'S SIZE RUNG — his items 2 and 5, "the user must be able to choose
   * the size rung the example renders at".
   *
   * It lives on the CANVAS, not in the rail, because it is a property of the
   * example being looked at rather than of the brand being authored: nothing it
   * does is saved, and the rail is the authored set. `null` means the brand's own
   * `defaults.rung.desktop`, which is the answer a reader gets without touching
   * anything.
   */
  const [sceneRung, setSceneRung] = useState<string | null>(null);
  /**
   * THE DOWNSCALE SWITCH — his SCALES axis, in his own words:
   *
   *     "Exist, so that one switch downsizes everything based on their current
   *      mode. A component that has the size L is immediately changed to size
   *      XS, while a text of size S is changed to XS."
   *
   * The sheet has always carried it: `emit.py::_size_rules` emits the whole
   * `[data-scale="down"]` rule set, with the two different mappings his sentence
   * asks for — text moves one rung, components three. What was missing was
   * anything that SET the attribute; measured 2026-08-20, zero elements in either
   * document carried `data-scale`, which is why the axis read as absent.
   *
   * It sits beside the rung for the same reason the rung is here: it is a
   * property of the example, not of the authored brand, and nothing it does is
   * saved. Boolean rather than an enum because the sheet has exactly one scaled
   * state — `down` — and offering a second would be inventing vocabulary.
   */
  const [sceneScaled, setSceneScaled] = useState(false);
  const [tree, setTree] = useState<TreeNode>(EMPTY_TREE);
  /**
   * What the inspector is open on.
   *
   * It carries the SCENE it was opened in, because a panel anchored over the
   * document and left open through a scene switch re-anchors over unrelated
   * content — measured, clipping the GUESTS empty state's first line. And it
   * carries an optional FLAG, because a mark on the canvas asks a different
   * question from a value on the canvas: not "why is this what it is" but
   * "what is wrong here".
   */
  const [selected, setSelected] = useState<{
    ground: string;
    slot: string;
    scene: Scene;
    flag?: string;
  } | null>(null);
  const [anchorY, setAnchorY] = useState<number | null>(null);
  const paneRef = useRef<HTMLElement>(null);
  const [tokenFilter, setTokenFilter] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);

  const nextId = useRef(1);

  /* ------------------------------------------------------------------ boot */

  const boot = useQuery({
    queryKey: ["design-engine", "boot"],
    queryFn: () => engineApi.boot(),
    staleTime: Infinity,
  });

  const kits = useQuery({
    queryKey: ["design-engine", "kits"],
    queryFn: () => engineApi.kits(),
    /* BOTH FIELDS, or the SYSTEMS scene marks nothing as live on the first
       paint and then marks one a moment later when the query lands — a badge
       that appears by itself reads as a bug. `/boot` serves the same two. */
    initialData: () =>
      boot.data ? { kits: boot.data.kits, active: boot.data.active } : undefined,
    enabled: Boolean(boot.data),
  });

  const stageList = boot.data?.stages ?? [];
  const families = boot.data?.families ?? [];

  /* THE LOADING STATE ESCALATES, because a featureless wait is
     indistinguishable from a broken page. The LATENCY itself is not a page
     defect — the same work is 0.24s in-process and 89-101s over HTTP against a
     sick orchestrator — and it is deliberately not optimised from here. What is
     fixed is what the page SAYS while it waits. */
  useEffect(() => {
    if (!boot.isLoading) return;
    const started = Date.now();
    const timer = window.setInterval(() => setElapsed(Date.now() - started), 500);
    return () => window.clearInterval(timer);
  }, [boot.isLoading]);

  /* --------------------------------------------------------- the growth beat */

  /**
   * A GROWTH RUN CANCELS THE ONE BEFORE IT.
   *
   * Each beat is a `setTimeout` that appends its stage key to one string. Two
   * overlapping runs interleave into a sequence that is neither run's — measured
   * as thirteen keys in the wrong order when a builder authored a new brand
   * while the first run was still firing. Kept exactly as it was; it fixes a
   * real bug.
   */
  const beats = useRef<number[]>([]);
  const runGrowth = useCallback(
    (resolved: ResolvedModel, stages = stageList) => {
      beats.current.forEach(window.clearTimeout);
      beats.current = [];
      const speed = resolved.motion.speeds[resolved.motion.default_speed] ?? 650;
      const beat = Math.max(320, Math.min(speed, 900));
      setReady("");
      stages.forEach((stage, i) => {
        beats.current.push(
          window.setTimeout(() => {
            setReady((prev) => (prev ? `${prev} ${stage.key}` : stage.key));
          }, beat * (i + 1)),
        );
      });
    },
    [stageList],
  );

  useEffect(() => () => beats.current.forEach(window.clearTimeout), []);

  const booted = useRef(false);
  useEffect(() => {
    if (!boot.data || booted.current) return;
    booted.current = true;
    setBrand(boot.data.brand);
    setModel(boot.data.model);
    setSheet(boot.data.sheet);
    setTree({ ...EMPTY_TREE, groundKey: boot.data.model.roots[0]?.key ?? "" });
    runGrowth(boot.data.model, boot.data.stages);
  }, [boot.data, runGrowth]);

  /* ---------------------------------------------------------- re-derivation */

  const refresh = useCallback(async (next: BrandJson) => {
    const [resolved, css] = await Promise.all([
      engineApi.resolve(next),
      engineApi.sheet(next),
    ]);
    setModel(resolved);
    setSheet(css);
    return resolved;
  }, []);

  /**
   * A refusal is ADDRESSED, or it is the verdict band's.
   *
   * `setError(String(err))` used to put machine text into a 7px red span in the
   * beat strip. The field is in the message and always was.
   */
  const routeError = useCallback((err: unknown) => {
    const parsed = engineError(err);
    if (parsed.field) {
      setFieldErrors((prev) => ({ ...prev, [parsed.field as string]: parsed.message }));
    } else {
      setPageError(parsed.message);
    }
  }, []);

  // An edit re-derives. Debounced because a colour input fires per pixel of
  // drag and the whole system is recomputed each time — the debounce is about
  // the round trip, never about hiding a slow render.
  const pending = useRef<number | null>(null);
  const onBrandChange = useCallback(
    (next: BrandJson) => {
      setBrand(next);
      setDirty(true);
      setFieldErrors({});
      setPageError(null);
      if (pending.current) window.clearTimeout(pending.current);
      pending.current = window.setTimeout(() => {
        refresh(next).catch(routeError);
      }, 140);
    },
    [refresh, routeError],
  );

  const grow = useCallback(
    async (id: string, family: string, colour: string) => {
      setBusy(true);
      setGateError(null);
      try {
        const result = await engineApi.essentials(id, family, colour);
        setBrand(result.brand);
        const resolved = await refresh(result.brand);
        nextId.current = 1;
        setTree({ ...EMPTY_TREE, groundKey: resolved.roots[0]?.key ?? "" });
        setRootPlacement(null);
        setAppearance("light");
        setAuthoring(false);
        setDirty(true);
        runGrowth(resolved);
      } catch (err) {
        setGateError(engineError(err).message);
      } finally {
        setBusy(false);
      }
    },
    [refresh, runGrowth],
  );

  const openKit = useCallback(
    async (id: string) => {
      setBusy(true);
      setGateError(null);
      try {
        const kit = await engineApi.kit(id);
        setBrand(kit.brand);
        const resolved = await refresh(kit.brand);
        nextId.current = 1;
        setTree({ ...EMPTY_TREE, groundKey: resolved.roots[0]?.key ?? "" });
        setRootPlacement(null);
        setAppearance("light");
        setAuthoring(false);
        setDirty(false);
        runGrowth(resolved);
      } catch (err) {
        setGateError(engineError(err).message);
      } finally {
        setBusy(false);
      }
    },
    [refresh, runGrowth],
  );

  /* --------------------------------------------------------- the descent */

  const transitions = useMemo(() => {
    const table: Record<string, Record<string, string>> = {};
    for (const ground of model?.grounds ?? []) table[ground.key] = ground.transitions;
    return table;
  }, [model]);

  const rootKey = useMemo(() => {
    if (!model) return "";
    const wanted = rootPlacement ?? appearance;
    return (
      model.roots.find((r) => r.id === wanted)?.key ??
      model.roots.find((r) => r.id === appearance)?.key ??
      ""
    );
  }, [model, rootPlacement, appearance]);

  const keyedTree = useMemo(
    () => (rootKey ? retree({ ...tree, groundKey: rootKey }, rootKey, transitions) : null),
    [tree, rootKey, transitions],
  );

  const groundsByKey = useMemo(() => {
    const map: Record<string, ResolvedModel["grounds"][number]> = {};
    for (const ground of model?.grounds ?? []) map[ground.key] = ground;
    return map;
  }, [model]);

  const nodeGround = useCallback(
    (nodeId: string) => {
      if (!keyedTree) return null;
      let found: TreeNode | null = null;
      const walk = (node: TreeNode) => {
        if (node.id === nodeId) found = node;
        node.children.forEach(walk);
      };
      walk(keyedTree);
      return found ? (groundsByKey[(found as TreeNode).groundKey] ?? null) : null;
    },
    [keyedTree, groundsByKey],
  );

  /**
   * A viewport y, in the CANVAS pane's own coordinates.
   *
   * The panel is `position: absolute` inside the pane, so a raw viewport
   * coordinate anchored it wherever the pane happened to start — which is
   * 136px down on this page and a different number in every scene.
   */
  const anchorIn = useCallback((y: number | undefined) => {
    if (y == null) return null;
    const top = paneRef.current?.getBoundingClientRect().top ?? 0;
    return Math.max(0, y - top);
  }, []);

  const onFrameMessage = useCallback(
    (msg: FrameMessage) => {
      if (msg.type === "place" && msg.node) {
        const id = `n${nextId.current++}`;
        setTree((prev) =>
          mapTree(prev, (node) =>
            node.id === msg.node
              ? {
                  ...node,
                  children: [
                    ...node.children,
                    { id, request: msg.request ?? null, groundKey: "", children: [] },
                  ],
                }
              : node,
          ),
        );
      } else if (msg.type === "drop" && msg.node && msg.node !== "root") {
        setTree((prev) => dropNode(prev, msg.node as string));
      } else if (msg.type === "reveal" && msg.slot) {
        /* THE B4 FIX. There is no tab to be on and nothing to swallow this: the
           inspector opens over the canvas, anchored where the click landed, no
           matter which scene is showing. */
        const ground = msg.ground ? nodeGround(msg.ground) : null;
        setSelected({ ground: ground?.key ?? rootKey, slot: msg.slot, scene });
        setAnchorY(anchorIn(msg.y));
      } else if (msg.type === "flag" && msg.flag) {
        /* A MARK ON THE CANVAS. The reason is the page's own `<Note>` — the
           same sentence the rail carries, with the same one action — and it
           opens here rather than in a 681-character native tooltip. */
        const ground = msg.ground ? nodeGround(msg.ground) : null;
        setSelected({
          ground: ground?.key ?? rootKey,
          slot: msg.slot ?? "",
          scene,
          flag: msg.flag,
        });
        setAnchorY(anchorIn(msg.y));
      }
    },
    [nodeGround, rootKey, scene, anchorIn],
  );

  /* ----------------------------------------------------------- selection */

  const selectedGround = selected
    ? (groundsByKey[selected.ground] ?? groundsByKey[rootKey] ?? null)
    : (groundsByKey[rootKey] ?? null);
  const selectedDerivation = derivationBySlot(selectedGround, selected?.slot ?? null);
  const selectedRule =
    boot.data?.rules.find((r) => r.id === selectedDerivation?.rule) ?? null;

  /** The finding a canvas mark was clicked for, if it was a mark. */
  const selectedFlag = useMemo<Flag | null>(() => {
    if (!selected?.flag) return null;
    const here = selectedGround?.palette.flags ?? [];
    return (
      (model?.flags ?? []).find((f) => f.code === selected.flag) ??
      here.find((f) => f.code === selected.flag) ??
      null
    );
  }, [selected?.flag, model, selectedGround]);

  const pickSlot = useCallback(
    (slot: string) =>
      setSelected({ ground: selectedGround?.key ?? rootKey, slot, scene }),
    [selectedGround, rootKey, scene],
  );

  /* THE PANEL DOES NOT SURVIVE A SCENE SWITCH. It is anchored to a gesture on
     one scene's content; carried into another it re-anchors over whatever
     happens to be at that height. Closing it is the honest behaviour — the
     value it explained is not on screen any more. */
  useEffect(() => {
    setSelected((prev) => (prev && prev.scene !== scene ? null : prev));
  }, [scene]);

  const revealed = ready.split(" ").filter(Boolean);
  const grown = stageList.length > 0 && revealed.length >= stageList.length;
  const currentStage =
    stageList.find((s) => s.key === revealed[revealed.length - 1]) ?? null;

  /* ------------------------------------------------------------- verdict */

  const { acknowledge, isAcknowledged } = useAcknowledged(brand?.id);

  /**
   * Every finding the page ranks, from ONE source: the BRAND's own lint.
   *
   * IT USED TO INCLUDE THE SELECTED GROUND'S PALETTE FLAGS, and that made a
   * PREVIEW ACTION CHANGE THE JUDGEMENT: switching the canvas to the warning
   * ground took the band from "1 thing to fix" to "2 things to fix", because the
   * per-ground `foreground.contrast-short` on `ground:signal:warning` is the
   * SAME finding as the brand-level one on `signals.warning` — counted twice.
   * The lint already checks every ground a brand authors (both neutrals, the
   * canonical, all four signals), so nothing is lost and the verdict is now a
   * property of the brand rather than of what is on screen.
   *
   * The per-ground flags still reach the CANVAS, which is where a finding about
   * one ground belongs, and the inspector's "On this ground" list.
   *
   * Acknowledged findings drop to `note` — they still mark their control and
   * their component, they just stop driving the verdict, because the reader has
   * answered them.
   */
  const flags = useMemo(
    () => withAcknowledgements(model?.flags ?? [], isAcknowledged),
    [model, isAcknowledged],
  );

  /** The model the canvas paints: the same one, wearing the acknowledgements. */
  const canvasModel = useMemo<ResolvedModel | null>(
    () => (model ? { ...model, flags } : null),
    [model, flags],
  );

  /** A flag's action, executed. Only the engine's own numbers are ever sent. */
  const onAction = useCallback(
    (action: AdviceAction, flag: Flag) => {
      if (!brand) return;
      if (action.kind === "set-shade") {
        let next = setPath(brand, `brand.shade_${action.pole}`, action.amount);
        next = setPath(next, `brand.on_${action.pole}`, null);
        onBrandChange(next);
      } else if (action.kind === "apply-both") {
        let next = setPath(brand, "brand.shade_light", action.light);
        next = setPath(next, "brand.shade_dark", action.dark);
        next = setPath(next, "brand.on_light", null);
        next = setPath(next, "brand.on_dark", null);
        onBrandChange(next);
      } else if (action.kind === "acknowledge") {
        acknowledge(flag.code);
      } else if (action.kind === "explain") {
        pickSlot(action.slot);
      } else if (action.kind === "show") {
        /* THE FLAGGED VALUE, ON THE FLAGGED GROUND.
           `[Show me]` used to open the named slot on whatever ground the canvas
           happened to be showing — which for the shipped brand is the LIGHT one,
           so a complaint about 4.07 : 1 on the warning ground was answered with
           a passing 20.62 : 1 on a different ground, and the canvas never moved.
           The ground is in the engine's own `where`; `rootFor` reads it out, the
           page PLACES it, and only then does the panel open. */
        setParam("scene", action.scene);
        let ground = selectedGround?.key ?? rootKey;
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
        if (action.slot) setSelected({ ground, slot: action.slot, scene: action.scene });
      }
    },
    [
      brand,
      model,
      onBrandChange,
      acknowledge,
      pickSlot,
      setParam,
      selectedGround,
      rootKey,
    ],
  );

  /** `Review` / `Fix it` — go to the control the finding is about. */
  const onReview = useCallback((flag: Flag | null) => {
    if (!flag || !slotsNamedBy(flag.where).length) return;
    const el = document.querySelector(`[data-flag="${flag.code}"]`);
    el?.scrollIntoView({ block: "center" });
    (el?.closest("[data-control]")?.querySelector("input,button,[role='slider']") as
      | HTMLElement
      | undefined)?.focus();
  }, []);

  /* -------------------------------------------------------------- guests */

  const guestBrands = useQuery({
    queryKey: ["design-engine", "guest-brands", kits.data?.kits.map((k) => k.id)],
    enabled: (kits.data?.kits.length ?? 0) > 0,
    queryFn: async () => {
      const rows = await Promise.all(
        (kits.data?.kits ?? []).map((row) => engineApi.kit(row.id)),
      );
      return rows.map((row) => row.brand);
    },
  });

  const availableGuests = useMemo(
    () => (guestBrands.data ?? []).filter((b) => b.id !== brand?.id),
    [guestBrands.data, brand?.id],
  );

  /**
   * A CONTRASTING GUEST, PREFILLED, when this machine has only one brand.
   *
   * GUESTS was a permanent dead end on a fresh install — an empty state with
   * no value on it, so the page's central explanatory act was unreachable from
   * one of its three scenes. The host's own brand with ONE colour changed is
   * enough: the adaptations are cleared so the ENGINE re-derives them, which is
   * the whole demonstration. The page computes no colour; it supplies one.
   */
  const DEMO_GUEST_COLOUR = "#3ba0ff";
  const demoGuest = useMemo<BrandJson | null>(
    () =>
      brand
        ? {
            ...brand,
            id: "guest-demo",
            brand: {
              ...brand.brand,
              canonical: DEMO_GUEST_COLOUR,
              on_light: null,
              on_dark: null,
              shade_light: null,
              shade_dark: null,
            },
          }
        : null,
    [brand],
  );

  const guestsOnOffer = availableGuests.length
    ? availableGuests
    : demoGuest
      ? [demoGuest]
      : [];
  const guestsAreDemo = availableGuests.length === 0 && Boolean(demoGuest);

  /** Open the inspector on a value of the ground the GUESTS scene is showing. */
  const revealOnRoot = useCallback(
    (slot: string, root: string) => {
      const found = model?.roots.find((r) => r.id === root);
      setSelected({ ground: found?.key ?? rootKey, slot, scene: "guests" });
      setAnchorY(null);
    },
    [model, rootKey],
  );

  /* ---------------------------------------------------------------- save */

  /**
   * CREATE OR UPDATE, decided BEFORE the call.
   *
   * It used to be `create(brand).catch(() => save(brand))` — a silent fallback
   * that made a failed create indistinguishable from an update, and gave a
   * builder no way to know which one happened. The kit list already says which
   * ids exist.
   */
  const exists = (kits.data?.kits ?? []).some((k) => k.id === brand?.id);

  /* FORKING IS THE ONLY WAY OUT OF A SHIPPED KIT, and until now the page had
     none. `store.save` refuses a shipped id outright — open okuro-ds, edit,
     press Save and the answer is a 409, with the fork its own docstring
     prescribes unreachable from here. The dialog supplies the one thing that
     was missing: an editable id. */
  const [forking, setForking] = useState(false);
  const [forkPending, setForkPending] = useState(false);
  /* THE SOURCE IS NOT ALWAYS THE BRAND ON SCREEN. From the toolbar it is — that
     is the whole point of forking mid-edit. From a row in the SYSTEMS library it
     is the kit as SAVED, fetched on demand, because duplicating a system you are
     merely looking at must not first replace what you have open. `null` means
     "the draft", so the toolbar path is unchanged. */
  const [forkSource, setForkSource] = useState<BrandJson | null>(null);
  const fork = useCallback(
    async (next: BrandJson) => {
      setForkPending(true);
      setPageError(null);
      try {
        await engineApi.create(next);
        setForking(false);
        setForkSource(null);
        await kits.refetch();
        void openKit(next.id);
      } catch (err) {
        routeError(err);
      } finally {
        setForkPending(false);
      }
    },
    [kits, openKit, routeError],
  );

  /** Duplicate a kit from the library, without opening it first. */
  const forkFromLibrary = useCallback(
    async (id: string) => {
      setPageError(null);
      try {
        const kit = await engineApi.kit(id);
        setForkSource(kit.brand);
        setForking(true);
      } catch (err) {
        routeError(err);
      }
    },
    [routeError],
  );

  /* DUPLICATE FROM A WEBSITE — the other way in, moved off Settings on
     2026-09-06 so that authoring happens in one place. The endpoint does the
     whole job: it reads the pages and forks okuro-ds with what they show, then
     saves. So this only has to refresh the library and open what arrived —
     opening it is the point, because the four scanned values are exactly what a
     builder will want to correct by hand. */
  const [scanning, setScanning] = useState(false);
  const [scanPending, setScanPending] = useState(false);
  const scanIntoKit = useCallback(
    async (urls: string[], name?: string) => {
      setScanPending(true);
      setPageError(null);
      try {
        const made = await onboardingApi.extractDesign(urls, name);
        setScanning(false);
        await kits.refetch();
        void openKit(made.id);
      } catch (err) {
        /* THE 501s ARE THE INTERESTING ONES and they carry install guidance in
           their detail, so they must reach the page rather than be flattened
           into "scrape failed". `routeError` renders the message it is given. */
        routeError(err);
      } finally {
        setScanPending(false);
      }
    },
    [kits, openKit, routeError],
  );

  /* DELETING IS THE ONE ACT HERE THAT CANNOT BE UNDONE, and the route has
     always existed with nothing able to reach it — a kit created by a typo was
     permanent. Two deliberate presses in the row arm and fire it; see the note
     in systems.tsx on why this is not a `window.confirm`. */
  const removeKit = useCallback(
    async (id: string) => {
      setPageError(null);
      const wasActive = kits.data?.active ?? null;
      try {
        await engineApi.remove(id);
        const listed = await kits.refetch();
        /* If the editor had that kit open it is now showing a brand with no
           file behind it. Fall back to the first kit that still exists rather
           than leaving a canvas whose Save would silently re-create the thing
           just deleted. */
        if (brand?.id === id) {
          const next = listed.data?.kits?.[0]?.id;
          if (next) void openKit(next);
        }
        /* DELETING THE KIT THAT PAINTS THE APP also changes what `/engine.css`
           answers — the server falls back to okuro's own kit — so the link is
           stale for the same reason a save leaves it stale. Same class, other
           direction. */
        if (listed.data?.active && listed.data.active !== wasActive) {
          reloadEngineSheet();
        }
        toast.success(`${id} deleted`);
      } catch (err) {
        routeError(err);
      }
    },
    [brand, kits, openKit, routeError],
  );

  /* SAVING THE KIT THAT PAINTS THE APP HAS TO REPAINT THE APP.
     The owner, 2026-09-06: "If I save the default template that is used by
     okuro-ui, the ui is not updated. I need to go to the design settings,
     select another drop down element, and reselect the one i just updated."

     Exactly right, and the cause is that `/engine.css` is a `<link>` the
     browser has already fetched. The server regenerates per request, so the
     saved bytes are live the moment the PUT returns — but nothing told the
     link. Settings' preset select has always called `reloadEngineSheet`;
     this write, which changes the same answer, never did.

     THE CLASS, and it is the third instance today: a write that changes what
     `/engine.css` serves must invalidate the link, exactly as the sheet-swap
     race and the pulse blob were. Reload only when the saved kit IS the active
     one — re-fetching for a kit nobody is painted with would be a request that
     changes nothing.

     AND IT SAYS SO. The only feedback was an 8px dot 900px away going quiet;
     his words: "no idea whether something has been saved." */
  const [saving, setSaving] = useState(false);
  const save = useCallback(async () => {
    if (!brand) return;
    setPageError(null);
    setSaving(true);
    try {
      if (exists) await engineApi.save(brand);
      else await engineApi.create(brand);
      setDirty(false);
      const listed = await kits.refetch();
      const active = listed.data?.active ?? kits.data?.active ?? null;
      if (active === brand.id) {
        reloadEngineSheet();
        toast.success(`${brand.id} saved`, {
          description: "It paints okuro, so the app re-themed.",
        });
      } else {
        toast.success(`${brand.id} saved`);
      }
    } catch (err) {
      routeError(err);
    } finally {
      setSaving(false);
    }
  }, [brand, exists, kits, routeError]);

  useEffect(() => {
    document.title = "design engine · okuro";
  }, []);

  /* The inspector owns Escape while it is open — see `useAutoCollapsedChrome`. */
  const chrome = useAutoCollapsedChrome(Boolean(selected));

  /* --------------------------------------------------------------- render */

  if (boot.isError) {
    return (
      <div data-page="design-engine" style={{ padding: "var(--ce-f6)" }}>
        <div className="ce-stack" style={{ maxWidth: "var(--ce-measure)" }}>
          <h1 className="ce-title">The engine did not answer</h1>
          <p className="ce-body ce-2">
            The engine is not responding. It may be starting up.
          </p>
          <button
            type="button"
            className="ce-primary"
            style={{ alignSelf: "flex-start" }}
            onClick={() => boot.refetch()}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      className="flex h-full min-h-0"
      data-page="design-engine"
      data-dirty={dirty ? "true" : "false"}
      data-scene={scene}
    >
      {/* ── 0 · the app's own 48px gutter, when its chrome is collapsed ───
          FULL HEIGHT, beside the toolbar rather than under it. The APP's
          `ChromeRestoreButton` is an absolute 24x24 at (8, 8) over the routed
          page, so with this strip starting below the toolbar that button landed
          8px on top of the `h1` and ate the first glyph of the brand name at
          1280 and 1440. One gutter down the left edge is where it belongs. */}
      <ChromeStrip hidden={chrome.hidden} onShow={chrome.show} />

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      {/* ── 1 · the toolbar ─────────────────────────────────────── 48px ── */}
      <Toolbar>
        <h1 className="ce-title" style={{ whiteSpace: "nowrap" }}>
          {brand?.id ?? "design engine"}
        </h1>
        {/* THE DIRTY DOT. There was no unsaved state at all: an edit and a saved
            brand looked identical, and `save` fell back silently between create
            and update. */}
        <span
          aria-label={dirty ? "unsaved changes" : "saved"}
          data-dirty-dot={dirty ? "true" : "false"}
          style={{
            width: 8,
            height: 8,
            borderRadius: 4,
            flex: "none",
            background: dirty ? "var(--ce-notice)" : "transparent",
            boxShadow: dirty ? "none" : "inset 0 0 0 1px var(--ce-border)",
          }}
        />

        <Segmented
          ariaLabel="scene"
          className="mx-auto"
          value={scene}
          onChange={(value) => setParam("scene", value as Scene)}
          options={SCENES}
        />

        <button
          type="button"
          className="ce-quiet"
          data-testid="open-new-brand"
          onClick={() => {
            setSeed(undefined);
            setAuthoring(true);
          }}
        >
          New system
        </button>
        {/* FORK AND SAVE ACT ON THE OPEN KIT, so they leave with the rail on the
            library scene. Save was the sharp one: pressing it there writes a
            brand that is not the row under the pointer, and the only feedback is
            a dot 900px away going quiet. The library carries its own per-row
            Duplicate, which names its subject. */}
        {scene !== "systems" && (
          <>
            {/* EXACTLY ONE FILLED BUTTON ON THE PAGE. Measured before: 74 of 82
                buttons were transparent and the single filled one was filled
                with the page background, so there was no primary action. */}
            <button
              type="button"
              className="ce-quiet"
              data-testid="open-fork"
              onClick={() => setForking(true)}
              disabled={!brand}
            >
              Duplicate
            </button>
            {/* THE BUTTON IS THE FIRST PLACE A PRESS IS ANSWERED, before any
                toast: the press has to stop looking available while the write
                is in flight, or a slow save reads as a dead button and gets
                pressed again. */}
            <button
              type="button"
              className="ce-primary"
              data-testid="save-kit"
              data-saving={saving ? "true" : "false"}
              onClick={save}
              disabled={!brand || saving}
            >
              {saving ? "Saving…" : exists ? "Save" : "Save as new"}
            </button>
          </>
        )}
      </Toolbar>

      {/* ── 2 · the verdict band ────────────────────────────────── 56px ──
          THE BAND, THE GROWTH LINE AND THE RAIL ARE ALL ABOUT THE BRAND BEING
          EDITED. The library is about WHICH brand, so on that scene all three
          are absent. Measured before this: `2 things to fix. Contrast, on your
          error colour.` sat in red above a list of four design systems, where
          it reads as a fault in the LIST — and the Configure rail beside it
          offered a colour picker that silently edited whichever kit happened to
          be open, not the one under the pointer. */}
      {scene !== "systems" && (
        <VerdictBand
          flags={flags}
          model={model}
          busy={!model}
          onReview={onReview}
        />
      )}
      {pageError && (
        <div
          role="alert"
          data-page-error
          className="ce-row"
          style={{
            padding: "12px var(--ce-f6)",
            borderBottom: "1px solid var(--ce-border)",
            color: "var(--ce-danger)",
          }}
        >
          <span className="ce-body">{pageError}</span>
        </div>
      )}

      {/* ── 3 · the growth line ─────────────── 40px, then 32px, transient ── */}
      {scene !== "systems" && (
        <GrowthLine
          grown={grown}
          stages={stageList.length}
          done={revealed.length}
          title={currentStage?.title ?? null}
          caption={currentStage?.caption ?? null}
          onReplay={() => model && runGrowth(model)}
        />
      )}

      {/* ── 4 + 5 · the rail and the canvas ─────────────────────────────── */}
      <div className="relative flex min-h-0 flex-1 overflow-hidden" data-studio>
        {scene !== "systems" && (
          <Rail
            title="Author system"
            open={railOpen}
            onOpen={(open) => setParam("rail", open ? "1" : "0")}
          >
            {brand && model ? (
              <div className="ce-stack">
                <div className="ce-stack-tight" data-authoring-contract>
                  <span className="ce-kicker">Authored inputs only</span>
                  <p className="ce-body ce-2">
                    Change identity here. The engine recalculates every dependent
                    ground, role and component in the canvas.
                  </p>
                </div>
                <hr className="ce-hairline" />
                <AuthoringRail
                  brand={brand}
                  families={families}
                  flags={flags}
                  colours={model.brand_colours}
                  additional={model.additional}
                  onChange={onBrandChange}
                  onReveal={pickSlot}
                  onAction={onAction}
                />
              </div>
            ) : (
              <RailSkeleton />
            )}
            {Object.entries(fieldErrors).map(([field, message]) => (
              <span key={field} data-help data-state="error" role="alert">
                {field}: {message}
              </span>
            ))}
          </Rail>
        )}

        <main
          ref={paneRef}
          className="relative flex min-w-0 flex-1 flex-col overflow-hidden"
          data-pane="canvas"
          style={{ background: "var(--ce-surface)" }}
        >
          {/* THE CANVAS SERVES TWO SCENES, and it is one canvas rather than two.
              DOCUMENT and COMPONENTS share the frame, the appearance switch, the
              root-placement chips and the rung — because those are properties of
              the EXAMPLE, and a reader who has just placed a signal on the root
              should be able to switch to the component set and find it still
              there. What differs is the occupant: the seven hand-built blocks,
              or okuro's own 33 primitives. */}
          {(scene === "document" || scene === "components") && (
            <>
              <div
                className="ce-row ce-canvas-toolbar"
                data-canvas-toolbar
                style={{
                  minHeight: 40,
                  flex: "none",
                  gap: 16,
                  flexWrap: "wrap",
                  padding: "8px var(--ce-f3)",
                  borderBottom: "1px solid var(--ce-border)",
                }}
              >
                {/* THE EXPOSURE RULING, AS PERMANENT TEXT. It was the single
                    most valuable sentence on the page and it was hidden behind
                    a hover on a 17px word. */}
                <span className="ce-body ce-2" style={{ flex: "1 1 240px", minWidth: 0 }}>
                  A user chooses only dark or light. Everything else is a design
                  fact a builder places.
                </span>
                <Segmented
                  ariaLabel="appearance"
                  value={appearance}
                  onChange={(v) => setAppearance(v as "light" | "dark")}
                  options={[
                    { value: "light", label: "Light" },
                    { value: "dark", label: "Dark" },
                  ]}
                />
                {(model?.roots ?? [])
                  .filter((root) => !root.user_choice)
                  .map((root) => (
                    <button
                      key={root.id}
                      type="button"
                      className="ce-chip"
                      aria-pressed={rootPlacement === root.id}
                      data-root-placement={root.id}
                      onClick={() =>
                        setRootPlacement((prev) => (prev === root.id ? null : root.id))
                      }
                    >
                      {root.label.replace("signal ", "")}
                    </button>
                  ))}

                {/* ── THE SCENE'S SIZE RUNG (items 2 + 5) ────────────────
                    SIZE-BY-FRAME, which is the register's own mechanism: the
                    rung is stamped on the canvas's root frame and every child
                    consumes it. So this one control re-sizes the whole example,
                    type and components together, and a reader can see what the
                    brand looks like at XS without authoring anything.

                    ENUMERATED, NOT A DROPDOWN — eight options, all visible, the
                    pattern the reference surfaces use for every enum they ship.
                    `Default` is a real rung on this row rather than an empty
                    state: it is what the brand itself says, and naming it is how
                    a reader learns the brand HAS a default. */}
                <div
                  role="radiogroup"
                  aria-label="size rung"
                  data-rung-switch
                  className="ce-row"
                  style={{ gap: 4, flexWrap: "wrap", marginLeft: "auto" }}
                >
                  <span className="ce-micro ce-2">Rung</span>
                  {[null, ...(model?.sizes.text_rungs ?? [])].map((option) => (
                    <button
                      key={option ?? "default"}
                      type="button"
                      role="radio"
                      className="ce-chip"
                      aria-checked={sceneRung === option}
                      aria-label={option ?? "the brand's default rung"}
                      data-rung={option ?? "default"}
                      onClick={() => setSceneRung(option)}
                    >
                      {option ?? "Default"}
                    </button>
                  ))}
                </div>

                {/* ── THE DOWNSCALE SWITCH (SCALES) ──────────────────────
                    One switch, stamped on the same frame root as the rung, and
                    the sheet does the rest: text drops one rung, components
                    three. It is deliberately NOT a second rung picker — the
                    scaled state is relative to whatever rung is showing, which
                    is the whole point of his sentence. */}
                <button
                  type="button"
                  className="ce-chip"
                  data-scale-switch
                  aria-pressed={sceneScaled}
                  aria-label="downscale the scene"
                  onClick={() => setSceneScaled((prev) => !prev)}
                >
                  Downscale
                </button>
              </div>

              <div className="min-h-0 flex-1">
                {brand && canvasModel ? (
        <PreviewFrame
                    fill
                    className="h-full w-full"
                    sheet={sheet}
                    model={canvasModel}
                    tree={keyedTree}
                    ready={ready}
                    appearance={appearance}
                    rootPlacement={rootPlacement}
                    rung={sceneRung}
                    scaled={sceneScaled}
                    variant={scene === "components" ? "showcase" : "document"}
                    onMessage={onFrameMessage}
                  >
                    {/* TWO OCCUPANTS, ONE MOUNT. On DOCUMENT it is the ten
                        primitives that sit inside the demo page (item 6); on
                        COMPONENTS it is the whole vendored set, grouped and
                        live. Both portal into `[data-okuro-mount]`, so both
                        inherit the root node's own ground rather than the page
                        root's. */}
                    {(doc) =>
                      scene === "components" ? (
                        <ComponentShowcase doc={doc} />
                      ) : (
                        <KitShowcase doc={doc} />
                      )
                    }
                  </PreviewFrame>
                ) : (
                  <CanvasSkeleton elapsed={elapsed} onRetry={() => boot.refetch()} />
                )}
              </div>
            </>
          )}

          {/* THE LIBRARY. It does not wait on `model`: the list is answered by
              `/kits` and is meaningful even when the open brand fails to
              resolve — which is exactly when a builder most needs to reach a
              different one. */}
          {scene === "systems" && (
            <SystemsScene
              kits={kits.data?.kits ?? []}
              active={kits.data?.active ?? boot.data?.active ?? null}
              opened={brand?.id ?? null}
              busy={busy}
              onOpen={(id) => {
                void openKit(id);
                setParam("scene", "document");
              }}
              onFork={(id) => void forkFromLibrary(id)}
              onDelete={(id) => void removeKit(id)}
              onScan={() => setScanning(true)}
            />
          )}

          {scene === "tokens" &&
            (model ? (
              <TokensScene
                model={model}
                ground={selectedGround}
                stages={stageList}
                onPick={pickSlot}
                filter={tokenFilter}
                onFilter={setTokenFilter}
              />
            ) : (
              <CanvasSkeleton elapsed={elapsed} onRetry={() => boot.refetch()} />
            ))}

          {scene === "guests" &&
            (brand && guestsOnOffer.length > 0 ? (
              <NestingSection
                host={brand}
                available={guestsOnOffer}
                demo={guestsAreDemo}
                onReveal={revealOnRoot}
                onKeepDemo={() => {
                  /* A CONTRASTING COLOUR, on purpose. Two guests that both go
                     the same way demonstrate nothing; the sharpest thing this
                     system says needs one of them to land on the other clause. */
                  setSeed({ id: "guest-demo", colour: DEMO_GUEST_COLOUR });
                  setAuthoring(true);
                }}
              />
            ) : (
              <GuestsEmpty
                onCreate={() => {
                  setSeed({ id: "guest-demo", colour: DEMO_GUEST_COLOUR });
                  setAuthoring(true);
                }}
              />
            ))}

          {/* ── 6 · the inspector, on demand, from every scene ───────────── */}
          <Inspector
            derivation={selectedDerivation}
            rule={selectedRule}
            ground={selectedGround}
            flag={selectedFlag}
            colours={model?.brand_colours ?? null}
            onAction={onAction}
            anchorY={anchorY}
            onPick={pickSlot}
            onShowReaders={(slot) => {
              setTokenFilter(slot);
              setParam("scene", "tokens");
            }}
            onClose={() => setSelected(null)}
          />
        </main>
      </div>
      </div>

      {/* FORKING, beside authoring rather than inside it: authoring GROWS a
          brand from a typeface and a colour, forking COPIES the one on screen.
          Both write to the user store; only the fork can start from a shipped
          kit, which is why it exists. */}
      {(forkSource ?? brand) && (
        <ForkKitDialog
          open={forking}
          onOpenChange={(next) => {
            setForking(next);
            if (!next) setForkSource(null);
          }}
          source={(forkSource ?? brand) as BrandJson}
          fromScreen={forkSource === null}
          taken={(kits.data?.kits ?? []).map((k) => k.id)}
          pending={forkPending}
          onCreate={fork}
        />
      )}

      {/* DUPLICATE FROM A WEBSITE. Mounted unconditionally, unlike the fork
          dialog: forking needs a source brand to copy, scanning starts from the
          shipped base, so there is nothing it can be missing. */}
      <ScanKitDialog
        open={scanning}
        onOpenChange={setScanning}
        pending={scanPending}
        onScan={(urls, name) => void scanIntoKit(urls, name)}
      />

      {/* AUTHORING A NEW BRAND IS A DELIBERATE ACT, in a sheet over the running
          canvas rather than a gate in front of it. `data-page` travels with it
          so the chrome contract reaches a portalled surface. */}
      <Sheet open={authoring} onOpenChange={setAuthoring}>
        <SheetContent
          side="right"
          data-page="design-engine"
          className="w-[min(520px,92vw)] overflow-y-auto"
          style={{ padding: "var(--ce-f6)" }}
        >
          <SheetTitle className="ce-title">Start a new brand</SheetTitle>
          <EssentialsGate
            key={seed?.id ?? "new"}
            families={families}
            busy={busy}
            error={gateError}
            onGrow={grow}
            seed={seed}
          />
          {(kits.data?.kits.length ?? 0) > 0 && (
            <div className="ce-stack" style={{ marginTop: 32 }}>
              <hr className="ce-hairline" />
              <span className="ce-label">Or open one that exists</span>
              <div className="ce-row" style={{ flexWrap: "wrap", gap: 8 }}>
                {kits.data?.kits.map((kit) => (
                  <button
                    key={kit.id}
                    type="button"
                    className="ce-chip"
                    disabled={busy}
                    data-kit={kit.id}
                    onClick={() => openKit(kit.id)}
                  >
                    {kit.id} · {kit.origin}
                  </button>
                ))}
              </div>
            </div>
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}

/* ---------------------------------------------------------- the growth line */

/**
 * One line while the system grows, one row afterwards.
 *
 * The requirement is ratified — a builder must SEE the system grow — and what
 * was rejected is the artefact, not the choreography: twelve identically-styled
 * 10px bordered boxes that wrapped into two rows at 1440 (destroying any reading
 * of them as a sequence) under a caption `truncate`d at EVERY viewport,
 * including 2560, and a decorative strip that stayed forever after the run had
 * fired once.
 *
 * The caption WRAPS to two lines and the line reserves both, so nothing below it
 * reflows as the stages advance.
 */
function GrowthLine({
  grown,
  stages,
  done,
  title,
  caption,
  onReplay,
}: {
  grown: boolean;
  stages: number;
  done: number;
  title: string | null;
  caption: string | null;
  onReplay: () => void;
}) {
  /* THE LINE IS PRESENT FROM THE FIRST FRAME OF A RUN, not from the first beat.
     A run whose first stage has not fired yet is still a run, and a page that
     showed "Grown in 10 stages" for the 650ms before it started would be
     telling a reader the opposite of what is happening. */
  const running = stages > 0 && !grown;
  if (!running) {
    return (
      <div
        className="ce-row"
        data-growth="done"
        style={{
          height: 32,
          flex: "none",
          gap: 8,
          padding: "0 var(--ce-f6)",
          borderBottom: "1px solid var(--ce-border)",
        }}
      >
        <span className="ce-body ce-2">
          Grown in {stages} stage{stages === 1 ? "" : "s"}
        </span>
        <button type="button" className="ce-quiet" data-testid="replay-growth" onClick={onReplay}>
          Replay growth
        </button>
      </div>
    );
  }

  return (
    <div
      data-growth="running"
      style={{
        position: "relative",
        flex: "none",
        height: 56,
        padding: "8px var(--ce-f6)",
        borderBottom: "1px solid var(--ce-border)",
        overflow: "hidden",
      }}
    >
      <div
        className="ce-body"
        data-testid="growth-caption"
        style={{
          /* A FIXED TWO-LINE HEIGHT. The caption is a whole sentence and it
             wraps; reserving both lines is what keeps the canvas from stepping
             up and down once per stage. */
          height: 40,
          display: "flex",
          alignItems: "center",
          maxWidth: "var(--ce-measure)",
        }}
      >
        <span aria-hidden style={{ marginRight: 8, color: "var(--ce-fg-2)" }}>
          ▸
        </span>
        <span style={{ display: "-webkit-box", WebkitBoxOrient: "vertical", WebkitLineClamp: 2, overflow: "hidden" }}>
          <b style={{ fontWeight: 600 }}>{title ?? "Deriving"}</b>
          {caption ? ` — ${caption}` : " — the engine is answering."}
        </span>
      </div>
      <div
        aria-hidden
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          bottom: 0,
          height: 2,
          background: "var(--ce-border)",
        }}
      >
        <div
          style={{
            height: 2,
            width: `${stages ? (done / stages) * 100 : 0}%`,
            background: "var(--ce-fg-2)",
            transition: "width 240ms linear",
          }}
        />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- the waiting */

/**
 * The REAL layout's skeleton, at the real sizes, plus what is happening.
 *
 * A featureless spinner is indistinguishable from a broken page, which is
 * exactly how a 90-second boot was read. The escalation is stated in seconds so
 * a reader knows the page has not given up before it offers them a retry.
 */
function CanvasSkeleton({ elapsed, onRetry }: { elapsed: number; onRetry: () => void }) {
  return (
    <div className="ce-scene" data-skeleton>
      <span className="ce-body ce-2">
        {elapsed > VERY_SLOW
          ? "Still deriving — the engine is answering slowly."
          : elapsed > SLOW
            ? "Still deriving — the engine is answering slowly."
            : "Deriving the system…"}
      </span>
      <div
        style={{
          flex: 1,
          minHeight: 240,
          border: "1px solid var(--ce-border)",
          borderRadius: "var(--ce-r-surface)",
          background: "var(--ce-surface-2)",
        }}
      />
      {elapsed > VERY_SLOW && (
        <button
          type="button"
          className="ce-primary"
          style={{ alignSelf: "flex-start" }}
          onClick={onRetry}
        >
          Retry
        </button>
      )}
    </div>
  );
}

/** The rail's rows at their real heights, so nothing jumps when they arrive. */
function RailSkeleton() {
  return (
    <div className="ce-groups" data-skeleton>
      {Array.from({ length: 8 }, (_, i) => (
        <div key={i} className="ce-stack-tight">
          <div
            style={{
              height: 18,
              width: "40%",
              borderRadius: 4,
              background: "var(--ce-surface-2)",
            }}
          />
          <div
            style={{
              height: 32,
              borderRadius: "var(--ce-r-control)",
              background: "var(--ce-surface-2)",
            }}
          />
          <div style={{ height: 40 }} />
        </div>
      ))}
    </div>
  );
}
