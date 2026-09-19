// <!-- AGENT_HEADER
// role: code
// purpose: DELIVER/SLIDES — the deck gallery, the editor and the reader, at
//   three addresses, in a layout that fits the pane it is given.
// AGENT_HEADER_END -->
/**
 * THE ONE REBUILD VERDICT OF p3, AND WHY IT WAS A REBUILD.
 *
 * The engine is sound: 2,294 lines of scene model, text metrics, auto-fit and
 * export behind 17 test files. The chrome that arranged it was not, and the
 * reason is arithmetic rather than taste. Measured at HEAD 60ba1988e, 1366px,
 * kit `standard`:
 *
 *     pane scrollWidth 1233   pane clientWidth 728   CLIPPED 505px
 *     sub-toolbar rows 8 (207px of the pane's height before any canvas)
 *     the slide stage itself 167px wide, of a 1280px design
 *
 * 505px, and `.c-panes` is `overflow-x: clip`, so it was not even scrollable —
 * the Present button, the inspector and most of the canvas were unreachable at
 * the shell's default width. (The p3 spec recorded 241px against a 738px pane;
 * both numbers moved when the frame batches landed, and the defect was more
 * than twice what was written down.)
 *
 * THE FIX IS TO FIT THE PANE BY CONSTRUCTION, keyed to the pane container per
 * R3 — not to ask the shell for more room. Two decisions carry it:
 *
 *   1. NOTHING IS A ROW OF EVERYTHING. The insert and style clusters live in
 *      ONE markup that renders as a disclosure panel below its trigger when the
 *      pane is narrow and inline when it is wide. One list, two shapes, no
 *      duplicated buttons to drift apart.
 *   2. THE THREE COLUMNS BECOME ONE COLUMN. Below `@4xl` the thumbnail rail is
 *      a filmstrip across the top and the inspector is a panel over the canvas;
 *      at `@4xl` and above they are the left and right columns they were. The
 *      canvas always gets the full pane width, which is the leaf's primary
 *      action.
 *
 * `@4xl` is measured, not assumed: it is FALSE at a 728px pane (1366px window)
 * and TRUE at a 1262px pane (1900px window), flipping between 862 and 962. So
 * the two states the contract is measured at are exactly the two shapes.
 *
 * THE ADDRESS, ruled by the same grammar FLOW and WORKFLOWS took:
 *
 *     /deliver/slides                  the gallery         (DECKS)
 *     /deliver/slides/{id}             the editor          (EDIT)
 *     /deliver/slides/{id}?view=play   the reader          (PLAY)
 *
 * which is what fixes D6 — the leaf used to open `decks[0]`, always.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import {
  ChevronDown,
  Loader2,
  PanelRight,
  Play,
  Redo2,
  Save,
  Undo2,
} from "lucide-react";

import { SlideDeck } from "@/components/slides/slide-deck";
import { SlidesEditor } from "@/components/slides/slides-editor";
import { ElementInspector } from "@/components/slides/element-inspector";
import { LayerPanel } from "@/components/slides/layer-panel";
import { SlideThumb } from "@/components/slides/slide-thumb";
import { AssetPanel } from "@/components/slides/asset-panel";
import { PresentMode } from "@/components/slides/present-mode";
import { DeckGallery } from "@/components/slides/deck-gallery";
import { exportPdf, exportPptx, exportPng } from "@/components/slides/export";
import type { Arrangement, Deck } from "@/components/slides/scene";
import {
  addBlankSlide,
  addElement,
  addElements,
  applyTheme,
  applyBrandTheme,
  cloneElements,
  duplicateSlide,
  insertLayoutSlide,
  LAYOUTS,
  makeElement,
  makeImageElement,
  makeVideoElement,
  makeFlowElement,
  moveElements,
  moveSlide,
  removeElements,
  removeSlide,
  THEMES,
  type LayoutKind,
} from "@/components/slides/scene-ops";
import type { SlideElement } from "@/components/slides/scene";
import {
  slidesApi,
  brandsApi,
  brandToTheme,
  generateStream,
  type BrandAsset,
  type BrandSummary,
  type DeckSummary,
  type Density,
  type Jargon,
  type VariantSummary,
} from "@/lib/slides-api";
import { api } from "@/lib/api";
import { takeSlideGen, setOpenDeckId } from "@/lib/slide-gen-queue";
import { usePaneInterval, usePaneKeyboardArmed } from "@/lib/pane-active";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import type { LeafViewProps } from "@/shell/views/registry";
import { sectionSlugs } from "@/shell/views/sections";

const BRAND_ID = "okuro";

const DENSITIES: Density[] = ["concise", "balanced", "detailed"];
const JARGONS: Jargon[] = ["plain", "balanced", "technical"];
// Short, sensible target-language list for the re-tailor panel (BCP-47).
const LANGUAGES: { code: string; label: string }[] = [
  { code: "", label: "— keep language —" },
  { code: "en", label: "English" },
  { code: "de", label: "Deutsch" },
  { code: "de-CH", label: "Schweizerdeutsch (de-CH)" },
  { code: "fr", label: "Français" },
  { code: "it", label: "Italiano" },
  { code: "es", label: "Español" },
];

/** The insert palette — one list, rendered once, in both toolbar shapes. */
const ELEMENT_KINDS: { kind: SlideElement["kind"]; label: string }[] = [
  { kind: "text", label: "Text" },
  { kind: "box", label: "Box" },
  { kind: "list", label: "List" },
  { kind: "kpi", label: "KPI" },
  { kind: "quote", label: "Quote" },
  { kind: "divider", label: "Rule" },
  { kind: "chart", label: "Chart" },
  { kind: "table", label: "Table" },
];

interface PersonLite { id: string; display_name?: string; name?: string }

/** Flatten every text element of a deck into {slideIndex, id, text} rows — used
 *  by the variant diff (text-level, by element id; no diff library needed). */
function textRows(deck: Deck): { slide: number; id: string; text: string }[] {
  const out: { slide: number; id: string; text: string }[] = [];
  const walk = (els: SlideElement[], slide: number) => {
    for (const e of els) {
      if (typeof e.text === "string" && e.text.trim()) out.push({ slide, id: e.id, text: e.text });
      if (e.children?.length) walk(e.children, slide);
    }
  };
  deck.slides.forEach((s, i) => walk(s.elements, i));
  return out;
}

/**
 * The seed deck, created once when the store is empty so the leaf has something
 * to show. Its colours are the THEME the user would pick — content, exempt
 * under R4 — and it is data rather than chrome.
 */
const DEMO_TITLE = "okuro·slides demo";

function demoDeck(): Deck {
  const theme = THEMES[0]!;
  const t = (
    id: string,
    text: string,
    y: number,
    fontSize: number,
    extra: Partial<SlideElement> = {},
  ): SlideElement => ({
    id, kind: "text", x: 180, y, w: 920, h: Math.round(fontSize * 1.4),
    text, fontSize, z: 1, ...extra,
  });
  return {
    id: "okuro-slides-demo",
    title: DEMO_TITLE,
    arrangement: "horizontal",
    size: { w: 1280, h: 720 },
    transition: { duration: 0.7, easing: [0.22, 1, 0.36, 1] },
    background: theme.background,
    font: theme.font,
    slides: [
      { id: "s0", elements: [
        t("title", "okuro·slides", 280, 84, { fontWeight: 700, align: "center", color: theme.text }),
        t("sub", "recipient-tailored decks · smart-animate", 420, 30, { align: "center", color: theme.accent }),
      ] },
      { id: "s1", elements: [
        t("title", "How it animates", 78, 44, { fontWeight: 700, color: theme.text }),
        t("c1",
          "• state A → state B over a duration + curve\n" +
          "• shared layers morph (this title just did)\n" +
          "• layers absent in the next slide exit in the arrangement direction\n" +
          "• arriving layers enter from the opposite side",
          220, 34, { color: theme.text }),
      ] },
      { id: "s2", elements: [
        t("title", "Composable surfaces", 78, 44, { fontWeight: 700, color: theme.text }),
        t("c2", "video · image · an okuro flow chart, embedded live", 230, 30, { color: theme.text }),
      ] },
    ],
  };
}

/** Sections, resolved from the declared list rather than pinned — inserting a
 *  section above PLAY must not turn an editor visit into a reader visit. */
const SECTIONS = sectionSlugs("deliver", "slides");
const PLAY_SECTION = SECTIONS.indexOf("play");

type Panel = "none" | "insert" | "style" | "generate" | "assets" | "tailor";

export function SlidesPage({ id: pathId, view = 0, onSelectView }: Partial<LeafViewProps> = {}) {
  const [params] = useSearchParams();
  const navigate = useNavigate();

  /* `?id=` is still READ — bookmarks spell it that way and the shell delivers
     it intact — but the PATH is what this leaf writes. Same grammar as FLOW. */
  const deckId = pathId ?? params.get("id");
  const mode: "edit" | "play" = PLAY_SECTION >= 0 && view === PLAY_SECTION ? "play" : "edit";

  const [list, setList] = useState<DeckSummary[]>([]);
  const [deck, setDeck] = useState<Deck | null>(null);
  const [index, setIndex] = useState(0);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [status, setStatus] = useState("loading…");
  const [dirty, setDirty] = useState(false);
  const [people, setPeople] = useState<PersonLite[]>([]);
  const [genTopic, setGenTopic] = useState("");
  const [genPerson, setGenPerson] = useState("");
  const [genMode, setGenMode] = useState<"fast" | "quality">("fast");
  const [generating, setGenerating] = useState(false);
  const [panel, setPanel] = useState<Panel>("none");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const [presenting, setPresenting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [flows, setFlows] = useState<{ id: string; name?: string }[]>([]);
  const [brands, setBrands] = useState<BrandSummary[]>([]);
  const [tailorPerson, setTailorPerson] = useState("");
  const [tailorDensity, setTailorDensity] = useState<Density | "">("");
  const [tailorJargon, setTailorJargon] = useState<Jargon | "">("");
  const [tailorLang, setTailorLang] = useState("");
  const [tailoring, setTailoring] = useState(false);
  const [variants, setVariants] = useState<VariantSummary[]>([]);
  const [variantSource, setVariantSource] = useState<VariantSummary | null>(null);
  const [showDiff, setShowDiff] = useState(false);
  const [diffSource, setDiffSource] = useState<Deck | null>(null);
  const clipboard = useRef<SlideElement[]>([]);
  /** The pane element — Present goes full screen on THIS, not on the document. */
  const rootRef = useRef<HTMLDivElement | null>(null);

  // Undo/redo history of deck snapshots. Only content edits push (see edit());
  // load/generate/live-reload replace the deck wholesale and reset both stacks.
  const [past, setPast] = useState<Deck[]>([]);
  const [future, setFuture] = useState<Deck[]>([]);
  const HISTORY_CAP = 50;
  const resetHistory = useCallback(() => { setPast([]); setFuture([]); }, []);

  /* THE ADDRESS IS THE ROUTER'S. The merging form, never a fresh
     `URLSearchParams` — the whole query belongs to the shell and this callback
     owns exactly one part of it. `?id=` is removed so one deck never carries
     two spellings through the history. */
  const openDeck = useCallback(
    (id: string | null, opts?: { play?: boolean; replace?: boolean }) => {
      const merged = new URLSearchParams(params);
      merged.delete("id");
      if (opts?.play) merged.set("view", "play");
      else if (merged.get("view") === "play") merged.delete("view");
      const q = merged.toString();
      navigate(
        (id ? `/deliver/slides/${encodeURIComponent(id)}` : "/deliver/slides") +
          (q ? `?${q}` : ""),
        { replace: opts?.replace ?? false },
      );
    },
    [navigate, params],
  );

  const load = useCallback(async (id: string) => {
    const d = await slidesApi.get(id);
    setDeck(d.deck);
    setIndex(0);
    setSelectedIds([]);
    setDirty(false);
    resetHistory();
    setStatus(`loaded ${d.title}`);
  }, [resetHistory]);

  const refreshList = useCallback(async () => {
    const { decks } = await slidesApi.list();
    setList(decks);
    return decks;
  }, []);

  /* The list, once. It no longer opens `decks[0]` — that was D6, and the
     gallery is now the landing. The seed deck is still created when the store
     is empty, because a leaf with nothing in it cannot show what it is for. */
  useEffect(() => {
    (async () => {
      try {
        let decks = await refreshList();
        if (decks.length === 0) {
          const seed = demoDeck();
          await slidesApi.save(seed.title, seed, seed.id);
          decks = await refreshList();
        }
        setStatus(decks.length ? "" : "no decks");
      } catch (e) {
        setStatus(`error: ${(e as Error).message}`);
      }
    })();
  }, [refreshList]);

  /* The open deck follows the ADDRESS. A deck id that the store does not have
     says so rather than silently showing a different deck. */
  useEffect(() => {
    if (!deckId) { setDeck(null); return; }
    let cancelled = false;
    (async () => {
      try {
        const d = await slidesApi.get(deckId);
        if (cancelled) return;
        setDeck(d.deck); setIndex(0); setSelectedIds([]); setDirty(false);
        setPast([]); setFuture([]);
        setStatus("");
      } catch (e) {
        if (!cancelled) setStatus(`no deck ${deckId}: ${(e as Error).message}`);
      }
    })();
    return () => { cancelled = true; };
  }, [deckId]);

  useEffect(() => {
    api<PersonLite[]>("/api/people").then((p) => setPeople(Array.isArray(p) ? p : [])).catch(() => setPeople([]));
    api<{ flows?: { id: string; name?: string }[] }>("/api/flow-designer").then((r) => setFlows(r.flows ?? [])).catch(() => setFlows([]));
    brandsApi.list().then((r) => setBrands(r.brands ?? [])).catch(() => setBrands([]));
  }, []);

  // A user content edit: snapshot the PRIOR deck onto `past` (capped), clear
  // the redo stack, then apply. Use ONLY for content mutations — never for
  // load/generate/live-reload (those call setDeck + resetHistory directly).
  const edit = useCallback((d: Deck) => {
    setDeck((prev) => {
      if (prev) setPast((p) => [...p, prev].slice(-HISTORY_CAP));
      return d;
    });
    setFuture([]);
    setDirty(true);
  }, []);

  const applyBrand = useCallback(async (brandId: string) => {
    if (!deck || !brandId) return;
    setStatus(`applying brand ${brandId}…`);
    try {
      const theme = brandToTheme(await brandsApi.resolve(brandId));
      if (theme) { edit(applyBrandTheme(deck, brandId, theme)); setStatus(`brand: ${brandId}`); }
      else setStatus(`brand ${brandId} has no design system`);
    } catch (e) { setStatus(`brand failed: ${(e as Error).message}`); }
  }, [deck, edit]);

  // Expose the open deck id so the global chat's slides_edit action can target it.
  useEffect(() => {
    setOpenDeckId(deck?.id ?? null);
    return () => setOpenDeckId(null);
  }, [deck?.id]);

  /* THE 3-SECOND CHANGE FEED IS GATED ON THE PANE (T9). Five panes stay
     mounted, one per topic bar, so an ungated interval polls while the user is
     reading a different topic. `usePaneInterval` returns false when this pane is
     off screen, which stops the timer rather than throttling it. */
  const feedMs = usePaneInterval(3000);
  useEffect(() => {
    const id = deck?.id;
    if (!id) return;
    const onEdited = (e: Event) => {
      if ((e as CustomEvent).detail?.deckId === id && !dirty) void load(id);
    };
    window.addEventListener("okuro:slides-edited", onEdited);
    if (feedMs === false) {
      return () => window.removeEventListener("okuro:slides-edited", onEdited);
    }
    let lastSeq = -1;
    const poll = setInterval(async () => {
      if (dirty) return;
      try {
        const r = await api<{ events: { deck_id: string; kind: string }[]; seq: number }>(
          `/api/slides/events?since=${lastSeq < 0 ? 0 : lastSeq}`,
        );
        /* The first poll only RECORDS the sequence, so an edit landing in the
           first 3 s after mount is skipped. Kept deliberately: a first
           observation is a baseline, not evidence of change — the same rule the
           graph module's dirty check needed. */
        if (lastSeq < 0) { lastSeq = r.seq; return; }
        if (r.seq !== lastSeq) {
          lastSeq = r.seq;
          if (r.events.some((ev) => ev.deck_id === id && ev.kind === "saved")) void load(id);
        }
      } catch { /* ignore */ }
    }, feedMs);
    return () => { window.removeEventListener("okuro:slides-edited", onEdited); clearInterval(poll); };
  }, [deck?.id, dirty, load, feedMs]);

  const generate = useCallback(async (topicArg?: string, personArg?: string, modeArg?: "fast" | "quality", brandArg?: string) => {
    const topic = (topicArg ?? genTopic).trim();
    const person = personArg ?? genPerson;
    const m = modeArg ?? genMode;
    const brand = brandArg || deck?.brandId || BRAND_ID;
    if (modeArg) setGenMode(modeArg);
    if (!topic || generating) return;
    setGenerating(true); setStatus("generating…");
    await generateStream(
      topic,
      { personId: person || undefined, brandId: brand, mode: m },
      {
        onActivity: (msg) => setStatus(msg),
        onError: (msg) => setStatus(`generate failed: ${msg}`),
        onDone: async (d) => {
          setStatus(`generated ${d.title}`); setPanel("none");
          await refreshList();
          /* A generated deck gets its own address, so it can be linked and
             reloaded — the whole point of D6's fix. */
          openDeck(d.deck.id);
        },
      },
    );
    setGenerating(false);
  }, [genTopic, genPerson, generating, genMode, deck?.brandId, refreshList, openDeck]);

  /* Chat-driven generation. The queue is drained on mount AND the event is
     listened for while on the leaf — `onSlidesLeaf` in `slide-gen-queue.ts` is
     what makes the second half reachable at all (it used to test a path the
     shell does not use). */
  const generateRef = useRef(generate);
  generateRef.current = generate;
  useEffect(() => {
    const pend = takeSlideGen();
    if (pend?.topic) void generateRef.current(pend.topic, pend.personId, pend.mode, pend.brandId);
    const onGen = (e: Event) => {
      const d = (e as CustomEvent).detail;
      if (d?.topic) void generateRef.current(d.topic, d.recipient, d.mode, d.brand);
    };
    window.addEventListener("okuro:slides-generate", onGen);
    return () => window.removeEventListener("okuro:slides-generate", onGen);
  }, []);

  const last = (deck?.slides.length ?? 1) - 1;
  const go = useCallback((d: number) => setIndex((i) => Math.min(last, Math.max(0, i + d))), [last]);

  /* THE WINDOW-LEVEL ARROW KEYS ARE ARMED ON THE PANE — D4. All five topic
     bars keep a pane mounted, so an ungated listener stayed live from every
     other topic once this leaf had been in Play mode. Not observed firing, so
     this closes a structural defect rather than a measured one. */
  const keysArmed = usePaneKeyboardArmed();
  useEffect(() => {
    if (mode !== "play" || !keysArmed) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "ArrowRight" || e.key === "ArrowDown") go(1);
      else if (e.key === "ArrowLeft" || e.key === "ArrowUp") go(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, mode, keysArmed]);

  const save = useCallback(async () => {
    if (!deck) return;
    setStatus("saving…");
    try {
      const saved = await slidesApi.save(deck.title, deck, deck.id);
      setDeck(saved.deck); setDirty(false); setStatus(`saved ${saved.title}`);
      await refreshList();
    } catch (e) { setStatus(`save failed: ${(e as Error).message}`); }
  }, [deck, refreshList]);

  const undo = useCallback(() => {
    setPast((p) => {
      if (p.length === 0) return p;
      const prev = p[p.length - 1]!;
      setDeck((cur) => { if (cur) setFuture((f) => [...f, cur]); return prev; });
      setDirty(true);
      setSelectedIds([]);
      return p.slice(0, -1);
    });
  }, []);

  const redo = useCallback(() => {
    setFuture((f) => {
      if (f.length === 0) return f;
      const next = f[f.length - 1]!;
      setDeck((cur) => { if (cur) setPast((p) => [...p, cur].slice(-HISTORY_CAP)); return next; });
      setDirty(true);
      setSelectedIds([]);
      return f.slice(0, -1);
    });
  }, []);

  // Edit-mode keyboard: delete · copy/cut/paste · duplicate · arrow-nudge.
  useEffect(() => {
    if (mode !== "edit" || !keysArmed) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
      if (!deck) return;
      const slide = deck.slides[index];
      if (!slide) return;
      const meta = e.metaKey || e.ctrlKey;
      const key = e.key.toLowerCase();
      const selEls = () => slide.elements.filter((el) => selectedIds.includes(el.id));
      if (meta && key === "z" && e.shiftKey) {
        e.preventDefault(); redo();
      } else if (meta && key === "z") {
        e.preventDefault(); undo();
      } else if (meta && key === "y") {
        e.preventDefault(); redo();
      } else if (!meta && (e.key === "Delete" || e.key === "Backspace") && selectedIds.length) {
        e.preventDefault(); edit(removeElements(deck, slide.id, selectedIds)); setSelectedIds([]);
      } else if (meta && key === "c" && selectedIds.length) {
        clipboard.current = selEls();
      } else if (meta && key === "x" && selectedIds.length) {
        e.preventDefault(); clipboard.current = selEls(); edit(removeElements(deck, slide.id, selectedIds)); setSelectedIds([]);
      } else if (meta && key === "v" && clipboard.current.length) {
        e.preventDefault(); const clones = cloneElements(clipboard.current); edit(addElements(deck, slide.id, clones)); setSelectedIds(clones.map((c) => c.id));
      } else if (meta && key === "d" && selectedIds.length) {
        e.preventDefault(); const clones = cloneElements(selEls()); edit(addElements(deck, slide.id, clones)); setSelectedIds(clones.map((c) => c.id));
      } else if (!meta && e.key.startsWith("Arrow") && selectedIds.length) {
        e.preventDefault();
        const step = e.shiftKey ? 10 : 1;
        const dx = e.key === "ArrowLeft" ? -step : e.key === "ArrowRight" ? step : 0;
        const dy = e.key === "ArrowUp" ? -step : e.key === "ArrowDown" ? step : 0;
        edit(moveElements(deck, slide.id, selectedIds, dx, dy));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode, deck, index, selectedIds, keysArmed, edit, undo, redo]);

  const setArrangement = (a: Arrangement) => deck && edit({ ...deck, arrangement: a });

  // --- Recipient re-tailoring -----------------------------------------------

  const variantRoot = deck ? (deck.variantOf ?? deck.id) : null;

  const refreshVariants = useCallback(async (rootId: string | null) => {
    if (!rootId) { setVariants([]); setVariantSource(null); return; }
    try {
      const r = await slidesApi.variants(rootId);
      setVariantSource(r.source);
      setVariants(r.variants);
    } catch { setVariants([]); setVariantSource(null); }
  }, []);

  useEffect(() => { void refreshVariants(variantRoot); }, [variantRoot, refreshVariants]);
  useEffect(() => { setShowDiff(false); setDiffSource(null); }, [deck?.id]);

  const retailor = useCallback(async (asVariant: boolean) => {
    if (!deck || tailoring) return;
    if (!tailorPerson && !tailorDensity && !tailorJargon && !tailorLang) {
      setStatus("pick a recipient or an axis to re-tailor"); return;
    }
    setTailoring(true);
    setStatus(asVariant ? "creating variant…" : "re-tailoring…");
    try {
      const d = await slidesApi.retailor(deck.id, {
        personId: tailorPerson || undefined,
        density: (tailorDensity || undefined) as Density | undefined,
        jargon: (tailorJargon || undefined) as Jargon | undefined,
        language: tailorLang || undefined,
        asVariant,
      });
      if (asVariant) {
        setStatus(`variant: ${d.title}`);
        await refreshList();
        await refreshVariants(d.deck.variantOf ?? d.deck.id);
        /* A variant is a deck, so it gets an address like any other. */
        openDeck(d.deck.id);
      } else {
        await load(deck.id);
        setStatus(`re-tailored ${d.title}`);
        await refreshVariants(deck.variantOf ?? deck.id);
      }
      setPanel("none");
    } catch (e) { setStatus(`re-tailor failed: ${(e as Error).message}`); }
    setTailoring(false);
  }, [deck, tailoring, tailorPerson, tailorDensity, tailorJargon, tailorLang, load, refreshVariants, refreshList, openDeck]);

  const toggleDiff = useCallback(async () => {
    if (!deck?.variantOf) return;
    if (showDiff) { setShowDiff(false); return; }
    try {
      const src = await slidesApi.get(deck.variantOf);
      setDiffSource(src.deck); setShowDiff(true);
    } catch (e) { setStatus(`diff failed: ${(e as Error).message}`); }
  }, [deck?.variantOf, showDiff]);

  const personName = useCallback((id?: string | null) => {
    if (!id) return "";
    const p = people.find((x) => x.id === id);
    /* The `display_name || name || id` chain is deliberate and measured: the
       endpoint returns rows shaped by `_fetch_person`, which always carries
       `display_name`, but a `?? ` chain that renders SOMETHING plausible when a
       field name is wrong is the shape memory 245d4942 warns about. The id is
       the last link because an id is at least true. */
    return p?.display_name || p?.name || id;
  }, [people]);

  const removeDeck = useCallback(async (id: string) => {
    setDeleting(true);
    try {
      await slidesApi.remove(id);
      setStatus("deck deleted");
      const decks = await refreshList();
      if (deckId === id) openDeck(null, { replace: true });
      if (decks.length === 0) setStatus("no decks");
    } catch (e) { setStatus(`delete failed: ${(e as Error).message}`); }
    setDeleting(false);
  }, [refreshList, deckId, openDeck]);

  const newDeck = useCallback(async () => {
    setStatus("creating…");
    try {
      const blank: Deck = {
        id: `deck-${Date.now().toString(36)}`,
        title: "Untitled deck",
        arrangement: "horizontal",
        size: { w: 1280, h: 720 },
        transition: { duration: 0.7, easing: [0.22, 1, 0.36, 1] },
        slides: [{ id: "s0", elements: [] }],
      };
      const saved = await slidesApi.save(blank.title, blank, blank.id);
      await refreshList();
      openDeck(saved.deck.id);
    } catch (e) { setStatus(`create failed: ${(e as Error).message}`); }
  }, [refreshList, openDeck]);

  const curSlide = deck?.slides[index];
  const addEl = (kind: SlideElement["kind"]) => {
    if (!deck || !curSlide) return;
    const el = makeElement(kind, deck);
    edit(addElement(deck, curSlide.id, el)); setSelectedIds([el.id]);
  };
  const insertAsset = (a: BrandAsset) => {
    if (!deck || !curSlide) return;
    const el = (a.mime || "").startsWith("video") ? makeVideoElement(deck, a.url) : makeImageElement(deck, a.url);
    edit(addElement(deck, curSlide.id, el)); setSelectedIds([el.id]);
  };
  const insertFlow = (flowId: string) => {
    if (!deck || !curSlide || !flowId) return;
    const el = makeFlowElement(deck, flowId);
    edit(addElement(deck, curSlide.id, el)); setSelectedIds([el.id]);
  };
  const dupSlide = () => { if (!deck) return; const { deck: d, newIndex } = duplicateSlide(deck, index); edit(d); setIndex(newIndex); };
  const blankSlide = () => { if (!deck) return; const { deck: d, newIndex } = addBlankSlide(deck); edit(d); setIndex(newIndex); };
  const delSlide = (i: number) => { if (!deck) return; const { deck: d, newIndex } = removeSlide(deck, i); edit(d); setIndex(newIndex); setSelectedIds([]); };
  const reorder = (from: number, to: number) => { if (!deck) return; edit(moveSlide(deck, from, to)); setIndex(to); };
  const addLayout = (kind: LayoutKind) => { if (!deck) return; const { deck: d, newIndex } = insertLayoutSlide(deck, index, kind); edit(d); setIndex(newIndex); setSelectedIds([]); };
  const applyThemeByName = (name: string) => { const th = THEMES.find((t) => t.name === name); if (deck && th) edit(applyTheme(deck, th)); };
  const setNotes = (v: string) => { if (!deck) return; edit({ ...deck, slides: deck.slides.map((s, i) => (i === index ? { ...s, notes: v } : s)) }); };

  const diffRows = useMemo(() => {
    if (!deck || !diffSource) return [];
    const srcRows = new Map(textRows(diffSource).map((r) => [r.id, r]));
    return textRows(deck)
      .filter((r) => srcRows.get(r.id)?.text !== r.text)
      .map((r) => ({ ...r, was: srcRows.get(r.id)?.text ?? "∅" }));
  }, [deck, diffSource]);

  // ── the gallery ───────────────────────────────────────────────────────────
  if (!deckId) {
    return (
      <div ref={rootRef} className="h-full min-h-0">
        <DeckGallery
          decks={list}
          openId={null}
          onOpen={(id) => openDeck(id)}
          onDelete={removeDeck}
          onNew={newDeck}
          deleting={deleting}
          status={status}
        />
        {/* The generate panel is reachable from the gallery too, because
            generating is how a deck comes into existence. */}
        <GeneratePanel
          open={panel === "generate"}
          topic={genTopic} setTopic={setGenTopic}
          person={genPerson} setPerson={setGenPerson}
          people={people}
          genMode={genMode} setGenMode={setGenMode}
          generating={generating}
          onGenerate={() => generate()}
        />
      </div>
    );
  }

  if (!deck) {
    return (
      <div className="flex h-full items-center justify-center type-small text-tertiary">
        {status || "loading…"}
      </div>
    );
  }

  const disclosure = (p: Panel, label: string) => (
    <Button
      size="sm"
      variant={panel === p ? "default" : "outline"}
      aria-expanded={panel === p}
      onClick={() => setPanel(panel === p ? "none" : p)}
    >
      {label}
      <ChevronDown className="h-3 w-3" />
    </Button>
  );

  return (
    <div ref={rootRef} className="flex h-full min-h-0 flex-col gap-2">
      {/* ── header ─────────────────────────────────────────────────────────
          R1: no title of its own. The deck's NAME is editable here, which is
          the one identity this surface owns. */}
      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={deck.title}
          aria-label="Deck title"
          onChange={(e) => edit({ ...deck, title: e.target.value })}
          className="w-field-md min-w-0 flex-1 text-sm font-medium"
        />
        {deck.recipient && (
          <span
            className="shrink-0 rounded-full border border-border bg-accent-subtle px-2 py-0.5 type-small text-accent"
            title="deck tailored to this recipient"
          >
            ◎ {personName(deck.recipient)}
          </span>
        )}
        {deck.variantMeta && (
          <span className="shrink-0 case-label type-small text-tertiary">
            {[deck.variantMeta.language, deck.variantMeta.density, deck.variantMeta.jargon].filter(Boolean).join(" · ")}
          </span>
        )}
        <div className="ml-auto flex shrink-0 items-center gap-1">
          <Button
            size="sm"
            variant={mode === "edit" ? "default" : "outline"}
            onClick={() => onSelectView?.(SECTIONS.indexOf("edit") >= 0 ? SECTIONS.indexOf("edit") : 0)}
          >
            Edit
          </Button>
          <Button
            size="sm"
            variant={mode === "play" ? "default" : "outline"}
            onClick={() => { setIndex(0); if (PLAY_SECTION >= 0) onSelectView?.(PLAY_SECTION); }}
          >
            Play
          </Button>
          <Button size="sm" variant="outline" onClick={save} disabled={!dirty} title="Save (the deck is saved on the server)">
            <Save className="h-3.5 w-3.5" />
            {dirty ? "Save*" : "Saved"}
          </Button>
          <Button size="sm" variant="outline" onClick={() => setPresenting(true)} title="Present full screen">
            <Play className="h-3.5 w-3.5" />
            Present
          </Button>
          <Select
            value=""
            onValueChange={(v) => {
              if (v === "pdf") exportPdf(deck);
              else if (v === "pptx") exportPptx(deck);
              else if (v === "png") exportPng(deck, index);
            }}
          >
            <SelectTrigger size="sm" className="w-field-sm" aria-label="Export this deck">
              <SelectValue placeholder="Export" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="pdf">PDF (print)</SelectItem>
              <SelectItem value="pptx">PPTX</SelectItem>
              <SelectItem value="png">PNG (current slide)</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* ── toolbar: triggers only, so this row can never be wider than the
             pane. Each cluster's own markup renders below. ───────────────── */}
      {mode === "edit" && (
        <div className="flex flex-wrap items-center gap-1">
          <Button size="sm" variant="outline" onClick={undo} disabled={past.length === 0} aria-label="Undo" title="Undo (⌘Z)">
            <Undo2 className="h-3.5 w-3.5" />
          </Button>
          <Button size="sm" variant="outline" onClick={redo} disabled={future.length === 0} aria-label="Redo" title="Redo (⌘⇧Z)">
            <Redo2 className="h-3.5 w-3.5" />
          </Button>
          {/* Below @4xl these open the panel rows; at @4xl the rows are already
              inline, so the triggers would be duplicate controls. */}
          <span className="@4xl:hidden">{disclosure("insert", "Insert")}</span>
          <span className="@4xl:hidden">{disclosure("style", "Style")}</span>
          {disclosure("assets", "Assets")}
          {disclosure("generate", "Generate")}
          {disclosure("tailor", "Tailor")}
          <Button
            size="sm"
            variant={inspectorOpen ? "default" : "outline"}
            className="@4xl:hidden"
            aria-expanded={inspectorOpen}
            onClick={() => setInspectorOpen((v) => !v)}
            title="Layers, properties and speaker notes"
          >
            <PanelRight className="h-3.5 w-3.5" />
            Panels
          </Button>
        </div>
      )}

      {/* ── insert cluster: ONE markup, two shapes ──────────────────────────
          `hidden` unless open below @4xl, always `flex` at @4xl and above. The
          eight element buttons and the two pickers exist exactly once. */}
      {mode === "edit" && (
        <div
          className={cn(
            "flex-wrap items-center gap-1 rounded-md border border-border p-1 @4xl:flex @4xl:border-0 @4xl:p-0",
            panel === "insert" ? "flex" : "hidden",
          )}
        >
          {ELEMENT_KINDS.map((k) => (
            <Button key={k.kind} size="sm" variant="outline" onClick={() => addEl(k.kind)}>
              {k.label}
            </Button>
          ))}
          <Select value="" onValueChange={(v) => addLayout(v as LayoutKind)}>
            <SelectTrigger size="sm" className="w-field-sm" aria-label="Insert a layout">
              <SelectValue placeholder="Layout" />
            </SelectTrigger>
            <SelectContent>
              {LAYOUTS.map((l) => <SelectItem key={l.kind} value={l.kind}>{l.label}</SelectItem>)}
            </SelectContent>
          </Select>
          <Select value="" onValueChange={insertFlow} disabled={flows.length === 0}>
            <SelectTrigger size="sm" className="w-field-sm" aria-label="Embed an okuro flow chart">
              <SelectValue placeholder="Flow" />
            </SelectTrigger>
            <SelectContent>
              {flows.map((f) => <SelectItem key={f.id} value={f.id}>{f.name || f.id}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
      )}

      {/* ── style cluster: same shape rule ──────────────────────────────── */}
      {mode === "edit" && (
        <div
          className={cn(
            "flex-wrap items-center gap-1 rounded-md border border-border p-1 @4xl:flex @4xl:border-0 @4xl:p-0",
            panel === "style" ? "flex" : "hidden",
          )}
        >
          <span className="type-small text-tertiary">Motion</span>
          <Button
            size="sm"
            variant={deck.arrangement === "horizontal" ? "default" : "outline"}
            onClick={() => setArrangement("horizontal")}
            aria-label="Horizontal motion"
            title="Horizontal — slides advance left↔right; sets the enter/exit direction"
          >
            →
          </Button>
          <Button
            size="sm"
            variant={deck.arrangement === "vertical" ? "default" : "outline"}
            onClick={() => setArrangement("vertical")}
            aria-label="Vertical motion"
            title="Vertical — slides advance top↔bottom; sets the enter/exit direction"
          >
            ↓
          </Button>
          <Select value="" onValueChange={applyThemeByName}>
            <SelectTrigger size="sm" className="w-field-sm" aria-label="Apply a theme">
              <SelectValue placeholder="Theme" />
            </SelectTrigger>
            <SelectContent>
              {THEMES.map((t) => <SelectItem key={t.name} value={t.name}>{t.name}</SelectItem>)}
            </SelectContent>
          </Select>
          <Select value={deck.brandId ?? ""} onValueChange={applyBrand} disabled={brands.length === 0}>
            <SelectTrigger size="sm" className="w-field-sm" aria-label="Apply a brand">
              <SelectValue placeholder="Brand" />
            </SelectTrigger>
            <SelectContent>
              {brands.map((b) => <SelectItem key={b.id} value={b.id}>{b.name || b.id}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
      )}

      {mode === "edit" && panel === "assets" && (
        <AssetPanel brandId={deck.brandId || BRAND_ID} onInsert={insertAsset} />
      )}

      {mode === "edit" && (
        <GeneratePanel
          open={panel === "generate"}
          topic={genTopic} setTopic={setGenTopic}
          person={genPerson} setPerson={setGenPerson}
          people={people}
          genMode={genMode} setGenMode={setGenMode}
          generating={generating}
          onGenerate={() => generate()}
        />
      )}

      {/* ── tailor ─────────────────────────────────────────────────────── */}
      {mode === "edit" && panel === "tailor" && (
        <div className="flex flex-wrap items-end gap-2 rounded-md border border-border p-2">
          <label className="flex min-w-0 flex-col gap-0.5 case-label type-small text-tertiary">
            recipient
            <Select value={tailorPerson} onValueChange={setTailorPerson}>
              <SelectTrigger size="sm" className="w-field-md" aria-label="Recipient">
                <SelectValue placeholder="— none —" />
              </SelectTrigger>
              <SelectContent>
                {people.map((p) => (
                  <SelectItem key={p.id} value={p.id}>{p.display_name || p.name || p.id}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
          <div className="flex flex-col gap-0.5 case-label type-small text-tertiary">
            density
            <div className="flex flex-wrap items-center gap-1">
              {DENSITIES.map((d) => (
                <Button key={d} size="sm" variant={tailorDensity === d ? "default" : "outline"}
                  onClick={() => setTailorDensity(tailorDensity === d ? "" : d)}>{d}</Button>
              ))}
            </div>
          </div>
          <div className="flex flex-col gap-0.5 case-label type-small text-tertiary">
            jargon
            <div className="flex flex-wrap items-center gap-1">
              {JARGONS.map((j) => (
                <Button key={j} size="sm" variant={tailorJargon === j ? "default" : "outline"}
                  onClick={() => setTailorJargon(tailorJargon === j ? "" : j)}>{j}</Button>
              ))}
            </div>
          </div>
          <label className="flex min-w-0 flex-col gap-0.5 case-label type-small text-tertiary">
            language
            <Select value={tailorLang} onValueChange={setTailorLang}>
              <SelectTrigger size="sm" className="w-field-md" aria-label="Target language">
                <SelectValue placeholder="— keep language —" />
              </SelectTrigger>
              <SelectContent>
                {LANGUAGES.filter((l) => l.code).map((l) => (
                  <SelectItem key={l.code} value={l.code}>{l.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" onClick={() => retailor(false)} disabled={tailoring}
              title="overwrite this deck's text in place">
              {tailoring ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
              Re-tailor in place
            </Button>
            <Button size="sm" onClick={() => retailor(true)} disabled={tailoring}
              title="save a new switchable audience variant linked to this deck">
              {tailoring ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
              Create variant
            </Button>
          </div>
        </div>
      )}

      {/* ── variants ───────────────────────────────────────────────────── */}
      {mode === "edit" && (variants.length > 0 || deck.variantOf) && (
        <div className="flex flex-wrap items-center gap-1 rounded-md border border-border px-2 py-1">
          <span className="case-label type-small text-tertiary">variants</span>
          {variantSource && (
            <Button size="sm" variant={deck.id === variantSource.id ? "default" : "outline"}
              onClick={() => openDeck(variantSource.id)} title="the source deck">source</Button>
          )}
          {variants.map((v) => {
            const m = v.variantMeta ?? {};
            const label = personName(m.person_id) || m.language || m.density || m.jargon || v.title;
            return (
              <Button key={v.id} size="sm" variant={deck.id === v.id ? "default" : "outline"}
                onClick={() => openDeck(v.id)} title={v.title}>{label}</Button>
            );
          })}
          {deck.variantOf && (
            <Button size="sm" variant="outline" className="ml-2" onClick={toggleDiff}
              title="highlight text that differs from the source">
              {showDiff ? "Hide diff" : "Diff vs source"}
            </Button>
          )}
        </div>
      )}

      {mode === "edit" && showDiff && diffSource && (
        <div className="max-h-40 shrink-0 overflow-y-auto rounded-md border border-border p-2 type-small">
          {diffRows.length === 0 ? (
            <div className="text-tertiary">no text differences from source</div>
          ) : (
            diffRows.map((r, i) => (
              <div key={`${r.id}-${i}`} className="mb-1 flex flex-wrap items-baseline gap-1.5">
                <span className="text-tertiary">slide {r.slide + 1} ·</span>
                <span className="text-error line-through">{r.was}</span>
                <span className="text-tertiary">→</span>
                <span className="text-accent">{r.text}</span>
              </div>
            ))
          )}
        </div>
      )}

      {/* ── main ───────────────────────────────────────────────────────────
          ONE COLUMN below @4xl (filmstrip · canvas, inspector over the top),
          THREE at @4xl and above. `min-w-0` on every flex child, because a
          flex item's default `min-width:auto` is what let a 1,233px row sit in
          a 728px pane. */}
      {mode === "edit" ? (
        <div className="relative flex min-h-0 min-w-0 flex-1 flex-col gap-2 @4xl:flex-row @4xl:gap-3">
          {/* the rail */}
          <div className="flex min-w-0 shrink-0 gap-2 overflow-x-auto pb-1 @4xl:w-44 @4xl:flex-col @4xl:overflow-x-hidden @4xl:overflow-y-auto @4xl:pb-0 @4xl:pr-1">
            <div className="flex shrink-0 gap-1 @4xl:shrink @4xl:flex-none">
              <Button size="sm" variant="outline" onClick={dupSlide} title="Duplicate current slide (shared element ids morph)">Dup</Button>
              <Button size="sm" variant="outline" onClick={blankSlide} title="Add a blank slide">Blank</Button>
            </div>
            {deck.slides.map((s, i) => (
              <div
                key={s.id}
                draggable
                onDragStart={() => setDragFrom(i)}
                onDragOver={(e) => e.preventDefault()}
                onDrop={() => { if (dragFrom !== null && dragFrom !== i) reorder(dragFrom, i); setDragFrom(null); }}
                onClick={() => { setIndex(i); setSelectedIds([]); }}
                className={cn(
                  "group relative w-32 shrink-0 cursor-pointer rounded-md border p-1 @4xl:w-auto",
                  i === index ? "border-accent" : "border-border hover:border-accent",
                )}
              >
                <div className="mb-0.5 flex items-center justify-between">
                  <span className="type-small text-tertiary">{i + 1}</span>
                  {deck.slides.length > 1 && (
                    <button
                      type="button"
                      onClick={(e) => { e.stopPropagation(); delSlide(i); }}
                      aria-label={`Delete slide ${i + 1}`}
                      title="Delete this slide (⌘Z puts it back)"
                      className="type-small text-error opacity-0 transition-opacity focus-visible:opacity-100 group-hover:opacity-100"
                    >
                      ✕
                    </button>
                  )}
                </div>
                <SlideThumb deck={deck} slideIndex={i} width={112} />
              </div>
            ))}
          </div>

          {/* the canvas — always the full remaining width */}
          <div className="min-h-0 min-w-0 flex-1">
            <SlidesEditor deck={deck} slideIndex={index} selectedIds={selectedIds} onSelect={setSelectedIds} onChange={edit} />
          </div>

          {/* layers · properties · notes.
              Below @4xl this is a panel OVER the canvas, opened by `Panels`, so
              it costs the canvas no width. At @4xl it is the right column. */}
          <div
            className={cn(
              "min-w-0 flex-col rounded-md border border-border bg-surface",
              inspectorOpen
                ? "absolute inset-y-0 right-0 z-20 flex w-64 max-w-full shadow-lg"
                : "hidden",
              "@4xl:static @4xl:z-auto @4xl:flex @4xl:w-72 @4xl:shrink-0 @4xl:shadow-none",
            )}
          >
            <LayerPanel deck={deck} slideIndex={index} selectedIds={selectedIds} onSelect={setSelectedIds} onChange={edit} />
            <div className="min-h-0 flex-1 overflow-y-auto">
              <ElementInspector deck={deck} slideIndex={index} selectedIds={selectedIds} onSelect={setSelectedIds} onChange={edit} />
            </div>
            <div className="shrink-0 border-t border-border p-2">
              <label className="mb-1 block case-label type-small text-tertiary" htmlFor="slide-notes">
                speaker notes
              </label>
              <textarea
                id="slide-notes"
                value={deck.slides[index]?.notes ?? ""}
                onChange={(e) => setNotes(e.target.value)}
                rows={3}
                placeholder="notes for this slide (shown in Present, press N)…"
                className="w-full resize-y rounded border border-border bg-transparent px-2 py-1 type-small text-fg placeholder:text-tertiary"
              />
            </div>
          </div>
        </div>
      ) : (
        /* ── the reader ─────────────────────────────────────────────────── */
        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
          <div className="flex min-h-0 flex-1 items-center justify-center">
            <div className="w-full min-w-0">
              <SlideDeck deck={deck} index={index} />
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-center gap-2">
            <Button size="sm" variant="outline" onClick={() => go(-1)} disabled={index === 0}>← Prev</Button>
            {deck.slides.map((s, i) => (
              <button
                key={s.id}
                type="button"
                onClick={() => setIndex(i)}
                aria-label={`slide ${i + 1}`}
                aria-current={i === index}
                className={cn("h-2.5 w-2.5 rounded-full", i === index ? "bg-accent" : "bg-border")}
              />
            ))}
            <span className="type-small text-tertiary">{index + 1} / {last + 1}</span>
            <Button size="sm" variant="outline" onClick={() => go(1)} disabled={index === last}>Next →</Button>
          </div>
        </div>
      )}

      {presenting && (
        <PresentMode deck={deck} onExit={() => setPresenting(false)} surface={rootRef.current} />
      )}
    </div>
  );
}

/** The generate panel — one markup, used from the gallery and the editor. */
function GeneratePanel({
  open, topic, setTopic, person, setPerson, people, genMode, setGenMode, generating, onGenerate,
}: {
  open: boolean;
  topic: string; setTopic: (v: string) => void;
  person: string; setPerson: (v: string) => void;
  people: PersonLite[];
  genMode: "fast" | "quality"; setGenMode: (v: "fast" | "quality") => void;
  generating: boolean;
  onGenerate: () => void;
}) {
  if (!open) return null;
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-border p-2">
      <Input
        value={topic}
        onChange={(e) => setTopic(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") onGenerate(); }}
        placeholder="topic for a recipient-tailored deck…"
        aria-label="Deck topic"
        className="min-w-field-md flex-1"
      />
      <Select value={person} onValueChange={setPerson}>
        <SelectTrigger size="sm" className="w-field-md" aria-label="Recipient (optional)">
          <SelectValue placeholder="— recipient (optional) —" />
        </SelectTrigger>
        <SelectContent>
          {people.map((p) => (
            <SelectItem key={p.id} value={p.id}>{p.display_name || p.name || p.id}</SelectItem>
          ))}
        </SelectContent>
      </Select>
      <div className="flex items-center gap-1" title="fast = one-shot (seconds); quality = outline→layout with reasoning">
        {(["fast", "quality"] as const).map((m) => (
          <Button key={m} size="sm" variant={genMode === m ? "default" : "outline"} onClick={() => setGenMode(m)}>{m}</Button>
        ))}
      </div>
      <Button size="sm" onClick={onGenerate} disabled={!topic.trim() || generating}>
        {generating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
        {generating ? "generating…" : "Generate"}
      </Button>
    </div>
  );
}
