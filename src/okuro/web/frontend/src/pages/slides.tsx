import { useCallback, useEffect, useRef, useState } from "react";
import { SlideDeck } from "@/components/slides/slide-deck";
import { SlidesEditor } from "@/components/slides/slides-editor";
import { ElementInspector } from "@/components/slides/element-inspector";
import { LayerPanel } from "@/components/slides/layer-panel";
import { SlideThumb } from "@/components/slides/slide-thumb";
import { AssetPanel } from "@/components/slides/asset-panel";
import { PresentMode } from "@/components/slides/present-mode";
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
import { slidesApi, brandsApi, brandToTheme, generateStream, type BrandAsset, type BrandSummary, type DeckSummary, type Density, type Jargon, type VariantSummary } from "@/lib/slides-api";
import { api } from "@/lib/api";
import { takeSlideGen, setOpenDeckId } from "@/lib/slide-gen-queue";

const BRAND_ID = "okuro";
const ACCENT = "#8ff0a4";

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

const DEMO_DECK: Deck = {
  id: "okuro-slides-demo",
  title: "okuro·slides demo",
  arrangement: "horizontal",
  size: { w: 1280, h: 720 },
  transition: { duration: 0.7, easing: [0.22, 1, 0.36, 1] },
  background: "#0b0f0c",
  slides: [
    { id: "s0", elements: [
      { id: "logo", kind: "box", x: 80, y: 80, w: 120, h: 120, bg: ACCENT, radius: 20, z: 2 },
      { id: "title", kind: "text", x: 240, y: 290, w: 800, h: 120, text: "okuro·slides", fontSize: 84, fontWeight: 700, color: "#eafff0", align: "center", z: 1 },
      { id: "sub", kind: "text", x: 240, y: 430, w: 800, h: 60, text: "recipient-tailored decks · smart-animate", fontSize: 30, color: ACCENT, align: "center", z: 1 },
    ]},
    { id: "s1", elements: [
      { id: "logo", kind: "box", x: 80, y: 70, w: 72, h: 72, bg: ACCENT, radius: 14, z: 2 },
      { id: "title", kind: "text", x: 180, y: 78, w: 700, h: 64, text: "How it animates", fontSize: 44, fontWeight: 700, color: "#eafff0", align: "left", z: 1 },
      { id: "c1", kind: "text", x: 180, y: 220, w: 940, h: 400, text: "• state A → state B over a duration + curve\n• shared layers morph (this title just did)\n• layers absent in the next slide exit in the arrangement direction — not a blind fade\n• arriving layers enter from the opposite side", fontSize: 34, color: "#d2ffdd", align: "left", z: 1 },
    ]},
    { id: "s2", elements: [
      { id: "logo", kind: "box", x: 80, y: 70, w: 72, h: 72, bg: ACCENT, radius: 14, z: 2 },
      { id: "title", kind: "text", x: 180, y: 78, w: 700, h: 64, text: "Composable surfaces", fontSize: 44, fontWeight: 700, color: "#eafff0", align: "left", z: 1 },
      { id: "c2", kind: "box", x: 180, y: 230, w: 520, h: 380, bg: "#10231a", radius: 18, text: "video / image embed", fontSize: 26, color: "#bfeccd", align: "center", z: 1 },
      { id: "fc", kind: "box", x: 740, y: 230, w: 360, h: 380, bg: "rgba(143,240,164,0.12)", radius: 18, text: "okuro flow chart", fontSize: 26, color: ACCENT, align: "center", z: 1 },
    ]},
  ],
};

type Mode = "edit" | "play";

export function SlidesPage() {
  const [list, setList] = useState<DeckSummary[]>([]);
  const [deck, setDeck] = useState<Deck | null>(null);
  const [index, setIndex] = useState(0);
  const [mode, setMode] = useState<Mode>("edit");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [status, setStatus] = useState("loading…");
  const [dirty, setDirty] = useState(false);
  const [people, setPeople] = useState<PersonLite[]>([]);
  const [genTopic, setGenTopic] = useState("");
  const [genPerson, setGenPerson] = useState("");
  const [genMode, setGenMode] = useState<"fast" | "quality">("fast");
  const [generating, setGenerating] = useState(false);
  const [panel, setPanel] = useState<"none" | "generate" | "assets" | "tailor">("none");
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const [presenting, setPresenting] = useState(false);
  const [flows, setFlows] = useState<{ id: string; name?: string }[]>([]);
  const [brands, setBrands] = useState<BrandSummary[]>([]);
  // Recipient re-tailoring (Phase 3): axes + variant state.
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
  // Undo/redo history of deck snapshots. Only content edits push (see edit());
  // load/generate/live-reload replace the deck wholesale and reset both stacks.
  const [past, setPast] = useState<Deck[]>([]);
  const [future, setFuture] = useState<Deck[]>([]);
  const HISTORY_CAP = 50;
  const resetHistory = useCallback(() => { setPast([]); setFuture([]); }, []);

  const load = useCallback(async (id: string) => {
    const d = await slidesApi.get(id);
    setDeck(d.deck);
    setIndex(0);
    setSelectedIds([]);
    setDirty(false);
    resetHistory();
    setStatus(`loaded ${d.title}`);
  }, [resetHistory]);

  useEffect(() => {
    (async () => {
      try {
        let { decks } = await slidesApi.list();
        if (decks.length === 0) {
          await slidesApi.save(DEMO_DECK.title, DEMO_DECK, DEMO_DECK.id);
          decks = (await slidesApi.list()).decks;
        }
        setList(decks);
        if (decks[0]) await load(decks[0].id);
      } catch (e) {
        setStatus(`error: ${(e as Error).message}`);
      }
    })();
  }, [load]);

  useEffect(() => {
    api<PersonLite[]>("/api/people").then((p) => setPeople(Array.isArray(p) ? p : [])).catch(() => setPeople([]));
    api<{ flows?: { id: string; name?: string }[] }>("/api/flow-designer").then((r) => setFlows(r.flows ?? [])).catch(() => setFlows([]));
    brandsApi.list().then((r) => setBrands(r.brands ?? [])).catch(() => setBrands([]));
  }, []);

  const applyBrand = useCallback(async (brandId: string) => {
    if (!deck || !brandId) return;
    setStatus(`applying brand ${brandId}…`);
    try {
      const theme = brandToTheme(await brandsApi.resolve(brandId));
      if (theme) { edit(applyBrandTheme(deck, brandId, theme)); setStatus(`brand: ${brandId}`); }
      else setStatus(`brand ${brandId} has no design system`);
    } catch (e) { setStatus(`brand failed: ${(e as Error).message}`); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deck]);

  // Expose the open deck id so the global chat's slides_edit action can target it.
  useEffect(() => {
    setOpenDeckId(deck?.id ?? null);
    return () => setOpenDeckId(null);
  }, [deck?.id]);

  // Live-reload when a chat/agent edit lands (event) or the change-feed moves
  // (poll) — unless the user has unsaved local edits.
  useEffect(() => {
    const id = deck?.id;
    if (!id) return;
    const onEdited = (e: Event) => {
      if ((e as CustomEvent).detail?.deckId === id && !dirty) void load(id);
    };
    window.addEventListener("okuro:slides-edited", onEdited);
    let lastSeq = -1;
    const poll = setInterval(async () => {
      if (dirty) return;
      try {
        const r = await api<{ events: { deck_id: string; kind: string }[]; seq: number }>(
          `/api/slides/events?since=${lastSeq < 0 ? 0 : lastSeq}`,
        );
        if (lastSeq < 0) { lastSeq = r.seq; return; }
        if (r.seq !== lastSeq) {
          lastSeq = r.seq;
          if (r.events.some((ev) => ev.deck_id === id && ev.kind === "saved")) void load(id);
        }
      } catch { /* ignore */ }
    }, 3000);
    return () => { window.removeEventListener("okuro:slides-edited", onEdited); clearInterval(poll); };
  }, [deck?.id, dirty, load]);

  // Chat-driven generation: consume a queued "slides" action on mount, and
  // listen for it while already on /slides.
  useEffect(() => {
    const pend = takeSlideGen();
    if (pend?.topic) { setMode("edit"); void generate(pend.topic, pend.personId, pend.mode, pend.brandId); }
    const onGen = (e: Event) => { const d = (e as CustomEvent).detail; if (d?.topic) void generate(d.topic, d.recipient, d.mode, d.brand); };
    window.addEventListener("okuro:slides-generate", onGen);
    return () => window.removeEventListener("okuro:slides-generate", onGen);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const last = (deck?.slides.length ?? 1) - 1;
  const go = useCallback((d: number) => setIndex((i) => Math.min(last, Math.max(0, i + d))), [last]);

  useEffect(() => {
    if (mode !== "play") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "ArrowRight" || e.key === "ArrowDown") go(1);
      else if (e.key === "ArrowLeft" || e.key === "ArrowUp") go(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, mode]);

  // Edit-mode keyboard: delete · copy/cut/paste · duplicate · arrow-nudge.
  useEffect(() => {
    if (mode !== "edit") return;
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, deck, index, selectedIds]);

  // A user content edit: snapshot the PRIOR deck onto `past` (capped), clear
  // the redo stack, then apply. Use ONLY for content mutations — never for
  // load/generate/live-reload (those call setDeck + resetHistory directly).
  const edit = (d: Deck) => {
    setDeck((prev) => {
      if (prev) setPast((p) => [...p, prev].slice(-HISTORY_CAP));
      return d;
    });
    setFuture([]);
    setDirty(true);
  };

  const undo = () => {
    setPast((p) => {
      if (p.length === 0) return p;
      const prev = p[p.length - 1]!;
      setDeck((cur) => { if (cur) setFuture((f) => [...f, cur]); return prev; });
      setDirty(true);
      setSelectedIds([]);
      return p.slice(0, -1);
    });
  };

  const redo = () => {
    setFuture((f) => {
      if (f.length === 0) return f;
      const next = f[f.length - 1]!;
      setDeck((cur) => { if (cur) setPast((p) => [...p, cur].slice(-HISTORY_CAP)); return next; });
      setDirty(true);
      setSelectedIds([]);
      return f.slice(0, -1);
    });
  };

  const setArrangement = (a: Arrangement) => deck && edit({ ...deck, arrangement: a });

  const save = useCallback(async () => {
    if (!deck) return;
    setStatus("saving…");
    try {
      const saved = await slidesApi.save(deck.title, deck, deck.id);
      setDeck(saved.deck); setDirty(false); setStatus(`saved ${saved.title}`);
      setList((await slidesApi.list()).decks);
    } catch (e) { setStatus(`save failed: ${(e as Error).message}`); }
  }, [deck]);

  const generate = useCallback(async (topicArg?: string, personArg?: string, modeArg?: "fast" | "quality", brandArg?: string) => {
    const topic = (topicArg ?? genTopic).trim();
    const person = personArg ?? genPerson;
    const mode = modeArg ?? genMode;
    const brand = brandArg || deck?.brandId || BRAND_ID;
    if (modeArg) setGenMode(modeArg);
    if (!topic || generating) return;
    setGenerating(true); setStatus("generating…");
    await generateStream(
      topic,
      { personId: person || undefined, brandId: brand, mode },
      {
        onActivity: (m) => setStatus(m),
        onError: (m) => setStatus(`generate failed: ${m}`),
        onDone: async (d) => {
          setDeck(d.deck); setIndex(0); setSelectedIds([]); setDirty(false); resetHistory();
          setStatus(`generated ${d.title}`); setPanel("none");
          setList((await slidesApi.list()).decks);
        },
      },
    );
    setGenerating(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [genTopic, genPerson, generating, genMode, deck?.brandId]);

  // --- Recipient re-tailoring (Phase 3) -------------------------------------

  // Source id for the variant family = this deck's variantOf, or itself.
  const variantRoot = deck ? (deck.variantOf ?? deck.id) : null;

  const refreshVariants = useCallback(async (rootId: string | null) => {
    if (!rootId) { setVariants([]); setVariantSource(null); return; }
    try {
      const r = await slidesApi.variants(rootId);
      setVariantSource(r.source);
      setVariants(r.variants);
    } catch { setVariants([]); setVariantSource(null); }
  }, []);

  // Load the variant family whenever the open deck (or its source) changes.
  useEffect(() => { void refreshVariants(variantRoot); }, [variantRoot, refreshVariants]);

  // Reset the diff overlay when the open deck changes.
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
        // Switch to the new variant + refresh the family list.
        setDeck(d.deck); setIndex(0); setSelectedIds([]); setDirty(false); resetHistory();
        setStatus(`variant: ${d.title}`);
        setList((await slidesApi.list()).decks);
        await refreshVariants(d.deck.variantOf ?? d.deck.id);
      } else {
        await load(deck.id);
        setStatus(`re-tailored ${d.title}`);
        await refreshVariants(deck.variantOf ?? deck.id);
      }
      setPanel("none");
    } catch (e) { setStatus(`re-tailor failed: ${(e as Error).message}`); }
    setTailoring(false);
  }, [deck, tailoring, tailorPerson, tailorDensity, tailorJargon, tailorLang, load, resetHistory, refreshVariants]);

  // Toggle the "Diff vs source" overlay — fetch the source deck on first open.
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
    return p?.display_name || p?.name || id;
  }, [people]);

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

  const btn = "rounded-md border border-border px-2.5 py-1 text-sm text-fg transition-colors hover:border-accent hover:text-accent disabled:opacity-40";
  const tab = (on: boolean) => `rounded-md px-2.5 py-1 text-sm transition-colors ${on ? "bg-accent/15 text-accent" : "border border-border text-fg hover:text-accent"}`;

  return (
    <div className="flex h-full flex-col gap-2 p-3">
      {/* Top bar */}
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold text-fg">okuro·slides</span>
        {/* Recipient chip + variant-meta badge (Phase 3) */}
        {deck?.recipient && (
          <span className="rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5 text-[11px] text-accent" title="deck tailored to this recipient">
            ◎ {personName(deck.recipient)}
          </span>
        )}
        {deck?.variantMeta && (
          <span className="text-[10px] uppercase tracking-wide text-tertiary">
            {[deck.variantMeta.language, deck.variantMeta.density, deck.variantMeta.jargon].filter(Boolean).join(" · ")}
          </span>
        )}
        <span className="truncate text-xs text-tertiary">{status}</span>
        <div className="ml-auto flex items-center gap-2">
          <button className={tab(mode === "edit")} onClick={() => setMode("edit")}>Edit</button>
          <button className={tab(mode === "play")} onClick={() => { setMode("play"); setIndex(0); }}>Play</button>
          {list.length > 0 && (
            <select value={deck?.id ?? ""} onChange={(e) => load(e.target.value)} className="rounded-md border border-border bg-transparent px-2 py-1 text-sm text-fg">
              {list.map((d) => <option key={d.id} value={d.id} className="bg-background text-fg">{d.title}</option>)}
            </select>
          )}
          <button className={btn} onClick={save} disabled={!deck || !dirty}>{dirty ? "Save*" : "Saved"}</button>
          <button className={btn} onClick={() => deck && setPresenting(true)} disabled={!deck}>▶ Present</button>
          <select
            value=""
            onChange={(e) => {
              const v = e.target.value;
              e.currentTarget.value = "";
              if (!deck) return;
              if (v === "pdf") exportPdf(deck);
              else if (v === "pptx") exportPptx(deck);
              else if (v === "png") exportPng(deck, index);
            }}
            disabled={!deck}
            className="rounded-md border border-border bg-transparent px-2 py-1 text-sm text-fg"
            title="Export this deck"
          >
            <option value="" className="bg-background text-fg">Export ▾</option>
            <option value="pdf" className="bg-background text-fg">PDF (print)</option>
            <option value="pptx" className="bg-background text-fg">PPTX</option>
            <option value="png" className="bg-background text-fg">PNG (current slide)</option>
          </select>
        </div>
      </div>

      {/* Sub toolbar (edit) — workflow-ordered clusters: history │ create │ style │ ai */}
      {mode === "edit" && (
        <div className="flex flex-wrap items-center gap-2">
          {/* History */}
          <button className={btn} onClick={undo} disabled={past.length === 0} title="Undo (⌘Z)">↶ Undo</button>
          <button className={btn} onClick={redo} disabled={future.length === 0} title="Redo (⌘⇧Z)">↷ Redo</button>
          <span className="mx-1 h-4 w-px bg-border" />

          {/* Create */}
          <button className={btn} onClick={() => addEl("text")} disabled={!deck}>+ Text</button>
          <button className={btn} onClick={() => addEl("box")} disabled={!deck}>+ Box</button>
          <button className={btn} onClick={() => addEl("list")} disabled={!deck}>+ List</button>
          <button className={btn} onClick={() => addEl("kpi")} disabled={!deck}>+ KPI</button>
          <button className={btn} onClick={() => addEl("quote")} disabled={!deck}>+ Quote</button>
          <button className={btn} onClick={() => addEl("divider")} disabled={!deck}>+ Rule</button>
          <button className={btn} onClick={() => addEl("chart")} disabled={!deck}>+ Chart</button>
          <button className={btn} onClick={() => addEl("table")} disabled={!deck}>+ Table</button>
          <select
            value=""
            onChange={(e) => { if (e.target.value) addLayout(e.target.value as LayoutKind); e.currentTarget.value = ""; }}
            disabled={!deck}
            className="rounded-md border border-border bg-transparent px-2 py-1 text-sm text-fg"
          >
            <option value="" className="bg-background text-fg">+ Layout…</option>
            {LAYOUTS.map((l) => <option key={l.kind} value={l.kind} className="bg-background text-fg">{l.label}</option>)}
          </select>
          <select
            value=""
            onChange={(e) => { if (e.target.value) insertFlow(e.target.value); e.currentTarget.value = ""; }}
            disabled={!deck || flows.length === 0}
            className="rounded-md border border-border bg-transparent px-2 py-1 text-sm text-fg"
            title="embed an okuro·flow chart"
          >
            <option value="" className="bg-background text-fg">+ Flow…</option>
            {flows.map((f) => <option key={f.id} value={f.id} className="bg-background text-fg">{f.name || f.id}</option>)}
          </select>
          <span className="mx-1 h-4 w-px bg-border" />

          {/* Style */}
          <div className="flex items-center gap-1" title="Motion direction — slides advance left↔right or top↔bottom; sets the enter/exit direction">
            <span className="text-xs text-tertiary">Motion</span>
            <button onClick={() => setArrangement("horizontal")} disabled={!deck} className={tab(deck?.arrangement === "horizontal")} title="Horizontal — slides advance left↔right; sets the enter/exit direction">→</button>
            <button onClick={() => setArrangement("vertical")} disabled={!deck} className={tab(deck?.arrangement === "vertical")} title="Vertical — slides advance top↔bottom; sets the enter/exit direction">↓</button>
          </div>
          <select
            onChange={(e) => applyThemeByName(e.target.value)}
            disabled={!deck}
            className="rounded-md border border-border bg-transparent px-2 py-1 text-sm text-fg"
            title="theme"
          >
            <option value="" className="bg-background text-fg">Theme…</option>
            {THEMES.map((t) => <option key={t.name} value={t.name} className="bg-background text-fg">{t.name}</option>)}
          </select>
          <select
            value={deck?.brandId ?? ""}
            onChange={(e) => applyBrand(e.target.value)}
            disabled={!deck || brands.length === 0}
            className="rounded-md border border-border bg-transparent px-2 py-1 text-sm text-fg"
            title="apply an okuro brand's design system + assets"
          >
            <option value="" className="bg-background text-fg">Brand…</option>
            {brands.map((b) => <option key={b.id} value={b.id} className="bg-background text-fg">{b.name || b.id}</option>)}
          </select>
          <span className="mx-1 h-4 w-px bg-border" />

          {/* AI */}
          <button className={tab(panel === "assets")} onClick={() => setPanel(panel === "assets" ? "none" : "assets")} disabled={!deck}>Assets ▾</button>
          <button className={tab(panel === "generate")} onClick={() => setPanel(panel === "generate" ? "none" : "generate")}>Generate ▾</button>
          <button className={tab(panel === "tailor")} onClick={() => setPanel(panel === "tailor" ? "none" : "tailor")} disabled={!deck} title="re-tailor this deck to a recipient + density/jargon/language">Tailor ▾</button>
        </div>
      )}

      {/* Collapsible: generate */}
      {mode === "edit" && panel === "generate" && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-2">
          <input value={genTopic} onChange={(e) => setGenTopic(e.target.value)} onKeyDown={(e) => e.key === "Enter" && generate()} placeholder="topic for a recipient-tailored deck…" className="min-w-[260px] flex-1 rounded border border-border bg-transparent px-2 py-1 text-sm text-fg placeholder:text-tertiary" />
          <select value={genPerson} onChange={(e) => setGenPerson(e.target.value)} className="rounded border border-border bg-transparent px-2 py-1 text-sm text-fg">
            <option value="" className="bg-background text-fg">— recipient (optional) —</option>
            {people.map((p) => <option key={p.id} value={p.id} className="bg-background text-fg">{p.display_name || p.name || p.id}</option>)}
          </select>
          <div className="flex items-center gap-1" title="fast = one-shot (seconds); quality = outline→layout with reasoning">
            {(["fast", "quality"] as const).map((m) => (
              <button key={m} onClick={() => setGenMode(m)} className={tab(genMode === m)}>{m}</button>
            ))}
          </div>
          <button onClick={() => generate()} disabled={!genTopic.trim() || generating} className="rounded-md bg-accent/15 px-3 py-1 text-sm text-accent hover:bg-accent/25 disabled:opacity-40">{generating ? "generating…" : "Generate"}</button>
        </div>
      )}

      {/* Collapsible: assets */}
      {mode === "edit" && panel === "assets" && deck && <AssetPanel brandId={deck.brandId || BRAND_ID} onInsert={insertAsset} />}

      {/* Collapsible: tailor (recipient re-tailoring — the moat) */}
      {mode === "edit" && panel === "tailor" && deck && (
        <div className="flex flex-wrap items-end gap-3 rounded-lg border border-border p-2">
          <label className="flex flex-col gap-0.5 text-[10px] uppercase tracking-wide text-tertiary">
            recipient
            <select value={tailorPerson} onChange={(e) => setTailorPerson(e.target.value)} className="rounded border border-border bg-transparent px-2 py-1 text-sm text-fg">
              <option value="" className="bg-background text-fg">— none —</option>
              {people.map((p) => <option key={p.id} value={p.id} className="bg-background text-fg">{p.display_name || p.name || p.id}</option>)}
            </select>
          </label>
          <div className="flex flex-col gap-0.5 text-[10px] uppercase tracking-wide text-tertiary">
            density
            <div className="flex items-center gap-1">
              {DENSITIES.map((d) => (
                <button key={d} onClick={() => setTailorDensity(tailorDensity === d ? "" : d)} className={tab(tailorDensity === d)}>{d}</button>
              ))}
            </div>
          </div>
          <div className="flex flex-col gap-0.5 text-[10px] uppercase tracking-wide text-tertiary">
            jargon
            <div className="flex items-center gap-1">
              {JARGONS.map((j) => (
                <button key={j} onClick={() => setTailorJargon(tailorJargon === j ? "" : j)} className={tab(tailorJargon === j)}>{j}</button>
              ))}
            </div>
          </div>
          <label className="flex flex-col gap-0.5 text-[10px] uppercase tracking-wide text-tertiary">
            language
            <select value={tailorLang} onChange={(e) => setTailorLang(e.target.value)} className="rounded border border-border bg-transparent px-2 py-1 text-sm text-fg">
              {LANGUAGES.map((l) => <option key={l.code} value={l.code} className="bg-background text-fg">{l.label}</option>)}
            </select>
          </label>
          <div className="ml-auto flex items-center gap-2">
            <button onClick={() => retailor(false)} disabled={tailoring} className={btn} title="overwrite this deck's text in place">{tailoring ? "…" : "Re-tailor in place"}</button>
            <button onClick={() => retailor(true)} disabled={tailoring} className="rounded-md bg-accent/15 px-3 py-1 text-sm text-accent hover:bg-accent/25 disabled:opacity-40" title="save a new switchable audience variant linked to this deck">{tailoring ? "creating…" : "Create variant"}</button>
          </div>
        </div>
      )}

      {/* Variant tabs — source + its audience variants (Phase 3) */}
      {mode === "edit" && deck && (variants.length > 0 || deck.variantOf) && (
        <div className="flex flex-wrap items-center gap-1.5 rounded-lg border border-border px-2 py-1.5">
          <span className="text-[10px] uppercase tracking-wide text-tertiary">variants</span>
          {variantSource && (
            <button onClick={() => load(variantSource.id)} className={tab(deck.id === variantSource.id)} title="the source deck">source</button>
          )}
          {variants.map((v) => {
            const m = v.variantMeta ?? {};
            const label = personName(m.person_id) || m.language || m.density || m.jargon || v.title;
            return (
              <button key={v.id} onClick={() => load(v.id)} className={tab(deck.id === v.id)} title={v.title}>{label}</button>
            );
          })}
          {deck.variantOf && (
            <button onClick={toggleDiff} className={`${btn} ml-2`} title="highlight text that differs from the source">{showDiff ? "Hide diff" : "Diff vs source"}</button>
          )}
        </div>
      )}

      {/* Diff view — text elements whose text differs from the source, by id */}
      {mode === "edit" && deck && showDiff && diffSource && (
        <div className="max-h-40 overflow-y-auto rounded-lg border border-border p-2 text-xs">
          {(() => {
            const srcRows = new Map(textRows(diffSource).map((r) => [r.id, r]));
            const changes = textRows(deck).filter((r) => srcRows.get(r.id)?.text !== r.text);
            if (changes.length === 0) return <div className="text-tertiary">no text differences from source</div>;
            return changes.map((r, i) => (
              <div key={`${r.id}-${i}`} className="mb-1 flex flex-wrap items-baseline gap-1.5">
                <span className="text-tertiary">slide {r.slide + 1} ·</span>
                <span className="text-[var(--color-status-error,#f92f77)] line-through">{srcRows.get(r.id)?.text ?? "∅"}</span>
                <span className="text-tertiary">→</span>
                <span className="text-accent">{r.text}</span>
              </div>
            ));
          })()}
        </div>
      )}

      {/* Main area */}
      {!deck ? (
        <div className="flex flex-1 items-center justify-center rounded-xl border border-border text-sm text-tertiary">{status}</div>
      ) : mode === "edit" ? (
        <div className="flex min-h-0 flex-1 gap-3">
          {/* Thumbnail rail */}
          <div className="flex w-44 shrink-0 flex-col gap-2 overflow-y-auto pr-1">
            <div className="flex gap-1">
              <button className={`${btn} flex-1`} onClick={dupSlide} title="Duplicate current slide (shared element ids morph)">＋ Dup</button>
              <button className={`${btn} flex-1`} onClick={blankSlide} title="Add a blank slide">＋ Blank</button>
            </div>
            {deck.slides.map((s, i) => (
              <div
                key={s.id}
                draggable
                onDragStart={() => setDragFrom(i)}
                onDragOver={(e) => e.preventDefault()}
                onDrop={() => { if (dragFrom !== null && dragFrom !== i) reorder(dragFrom, i); setDragFrom(null); }}
                onClick={() => { setIndex(i); setSelectedIds([]); }}
                className={`group relative cursor-pointer rounded-md border p-1 ${i === index ? "border-accent" : "border-border hover:border-accent/50"}`}
              >
                <div className="mb-0.5 flex items-center justify-between">
                  <span className="text-[10px] text-tertiary">{i + 1}</span>
                  {deck.slides.length > 1 && (
                    <button onClick={(e) => { e.stopPropagation(); delSlide(i); }} className="hidden text-[10px] text-[var(--color-status-error,#f92f77)] group-hover:block">✕</button>
                  )}
                </div>
                <SlideThumb deck={deck} slideIndex={i} width={150} />
              </div>
            ))}
          </div>

          {/* Canvas */}
          <div className="min-w-0 flex-1">
            <SlidesEditor deck={deck} slideIndex={index} selectedIds={selectedIds} onSelect={setSelectedIds} onChange={edit} />
          </div>

          {/* Layers + inspector + notes */}
          <div className="flex w-72 shrink-0 flex-col rounded-lg border border-border">
            <LayerPanel deck={deck} slideIndex={index} selectedIds={selectedIds} onSelect={setSelectedIds} onChange={edit} />
            <div className="min-h-0 flex-1 overflow-y-auto">
              <ElementInspector deck={deck} slideIndex={index} selectedIds={selectedIds} onSelect={setSelectedIds} onChange={edit} />
            </div>
            <div className="shrink-0 border-t border-border p-3">
              <div className="mb-1 text-xs uppercase tracking-wide text-tertiary">speaker notes</div>
              <textarea
                value={deck.slides[index]?.notes ?? ""}
                onChange={(e) => setNotes(e.target.value)}
                rows={3}
                placeholder="notes for this slide (shown in Present, press N)…"
                className="w-full resize-y rounded border border-border bg-transparent px-2 py-1 text-xs text-fg placeholder:text-tertiary"
              />
            </div>
          </div>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col gap-2">
          <div className="flex flex-1 items-center justify-center">
            <div className="w-full max-w-6xl">
              <SlideDeck deck={deck} index={index} />
            </div>
          </div>
          <div className="flex items-center justify-center gap-2">
            <button className={btn} onClick={() => go(-1)} disabled={index === 0}>← Prev</button>
            {deck.slides.map((s, i) => (
              <button key={s.id} onClick={() => setIndex(i)} aria-label={`slide ${i + 1}`} className={`h-2.5 w-2.5 rounded-full ${i === index ? "bg-accent" : "bg-border hover:bg-accent/50"}`} />
            ))}
            <span className="text-xs text-tertiary">{index + 1} / {last + 1}</span>
            <button className={btn} onClick={() => go(1)} disabled={index === last}>Next →</button>
          </div>
        </div>
      )}

      {presenting && deck && <PresentMode deck={deck} onExit={() => setPresenting(false)} />}
    </div>
  );
}
