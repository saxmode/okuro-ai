import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDown, Check, CircleAlert, Copy, SlidersHorizontal, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Segmented } from "@/components/ui/segmented";
import { engineApi, engineError } from "@/components/design-engine/engine-api";
import type { BrandJson, ResolvedModel } from "@/components/design-engine/types";
import { COMPONENT_DOCS, COMPONENT_GROUPS } from "@/components/ds-engine-codex/catalog";
import { ConfigurationPanel } from "@/components/ds-engine-codex/configuration-panel";
import { ComponentSpecimen, LiveDemo } from "@/components/ds-engine-codex/native-specimens";
import { LogicAtlas } from "@/components/ds-engine-codex/logic-atlas";
import { useChrome } from "@/lib/chrome-context";
import { SHEET_CHANGED_EVENT, setAppearance as applyAppearance } from "@/lib/theme";
import "@/components/ds-engine-codex/ds-engine-codex.css";

const SECTION_LINKS = [
  ["systems", "Systems"],
  ["demo", "Live system"],
  ["components", "Components"],
] as const;

/** How far below the scroller's top edge a section counts as arrived. The
 *  sticky header is 56px and `scroll-margin-top` is 120px; 120 is the line an
 *  anchor jump actually lands on, so the pill and the jump agree. */
const NAV_BAND = 120;

export function DsEngineCodexPage() {
  const { hide, show } = useChrome();
  const boot = useQuery({
    queryKey: ["ds-engine-codex", "boot"],
    queryFn: () => engineApi.boot(),
    staleTime: Infinity,
  });
  const [brand, setBrand] = useState<BrandJson | null>(null);
  const [savedBrand, setSavedBrand] = useState<BrandJson | null>(null);
  const [model, setModel] = useState<ResolvedModel | null>(null);
  const [sheet, setSheet] = useState("");
  const [appearance, setAppearance] = useState<"light" | "dark">("light");
  const [rung, setRung] = useState("M");
  const [scaled, setScaled] = useState(false);
  const [componentId, setComponentId] = useState("button");
  const [activeSection, setActiveSection] = useState("systems");
  const [error, setError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [mobileConfigOpen, setMobileConfigOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const pending = useRef<number | null>(null);

  useEffect(() => {
    hide();
    return show;
  }, [hide, show]);

  // BOTH APPEARANCE KEYS, through the one writer. This used to set
  // `data-appearance` alone, so `color-scheme` kept whatever `applyThemeMode`
  // had last written and Chromium painted the rail's native `<select>` popup
  // black on a light page. `setAppearance` hands back the undo for the pair.
  useEffect(() => applyAppearance(appearance), [appearance]);

  // THE SHEET SWAP IS AN EVENT. Third instance of the class in memory
  // e52786a8: this page renders the selected kit's CSS into an in-body
  // `<style data-selected-engine-sheet>`, which repaints everything reading the
  // cascade and tells everything that is NOT reading it — nothing. The pulse
  // canvas paints into a `<canvas>`, so it reads `--color-accent` off `:root`
  // and caches it. Measured: selecting `meridian` moved `:root --color-accent`
  // mint -> gold while the canvas centre pixel stayed rgb(13,13,13), and zero
  // `okuro:engine-sheet-changed` events fired. It corrected itself the instant
  // `data-appearance` mutated, which separates NO SIGNAL from wrong moment.
  //
  // AFTER THE DOM HAS THE NEW TEXT, NEVER BEFORE. That is the whole lesson of
  // e52786a8's second half: a MutationObserver on the engine link fired when
  // the href was ASSIGNED, before the sheet was applied, so the reader woken by
  // it got the OLD value and marked itself clean. An effect keyed on `sheet`
  // runs after React has committed the new text, which is the moment the values
  // are really there.
  //
  // ONE SWAP, ONE EVENT. The unmount announcement is a SEPARATE effect on
  // purpose: written as a cleanup of this one it would also fire on every sheet
  // change, since React tears an effect down before re-running it — two events
  // per swap, and a listener that does real work would do it twice for one
  // action.
  useEffect(() => {
    if (!sheet) return;
    window.dispatchEvent(new CustomEvent(SHEET_CHANGED_EVENT));
  }, [sheet]);

  // AND ON UNMOUNT, because the style node goes with the page and the app
  // reverts to whatever `/engine.css` serves — a reversion is a sheet change
  // like any other, and a canvas left on the previewed kit is the same defect
  // pointed the other way.
  useEffect(() => () => {
    window.dispatchEvent(new CustomEvent(SHEET_CHANGED_EVENT));
  }, []);

  useEffect(() => {
    if (!boot.data) return;
    setBrand(boot.data.brand);
    setSavedBrand(boot.data.brand);
    setModel(boot.data.model);
    setSheet(boot.data.sheet);
    setRung(boot.data.brand.defaults.rung.desktop);
  }, [boot.data]);

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

    const scroller = first.closest<HTMLElement>(".overflow-y-auto");
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
      setRung(kit.brand.defaults.rung.desktop);
      setDirty(false);
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

  const saveBrand = async () => {
    if (!brand) return;
    setSaving(true);
    setError(null);
    try {
      await engineApi.save(brand);
      setSavedBrand(brand);
      setDirty(false);
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
      await engineApi.create(next);
      setBrand(next);
      setSavedBrand(next);
      setDirty(false);
    } catch (reason) {
      setError(engineError(reason).message);
    } finally {
      setSaving(false);
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

  if (boot.isLoading) return <EngineLoading />;
  if (boot.isError || !boot.data || !brand || !model) {
    return <EngineFailure message={engineError(boot.error).message} />;
  }
  const payload = boot.data;

  return (
    <main className="dsc-page ds-surface ds-n5 ds-paragraphs" data-page="ds-engine-codex" data-rung={rung} data-scale={scaled ? "down" : undefined}>
      <style data-selected-engine-sheet>{sheet}</style>
      <header className="dsc-local-nav ds-n7 ds-leads" aria-label="Design engine sections">
        <a className="dsc-wordmark ds-n7 ds-leads" href="#systems" aria-label="Okuro design engine home">
          <span>OKURO</span><i />ENGINE
        </a>
        <nav>
          {SECTION_LINKS.map(([id, label]) => (
            <a key={id} href={`#${id}`} data-active={activeSection === id || undefined}>{label}</a>
          ))}
        </nav>
        <span className="dsc-release ds-n8 ds-leads"><span />LIVE MODEL</span>
      </header>

      <div className="dsc-layout">
        <div className="dsc-flow">
          <section id="systems" className="dsc-section dsc-hero">
            <div className="dsc-eyebrow ds-n8 ds-leads">DESIGN SYSTEM ENGINE / 01</div>
            <h1 className="ds-h1 ds-titles">One identity.<br />Every interface decision.</h1>
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
              <div><span className="ds-n8 ds-leads">AVAILABLE SYSTEMS</span><h2 id="systems-title" className="ds-h3 ds-headings">Choose the identity. The logic stays.</h2></div>
              <p className="ds-n5 ds-paragraphs">Every row is a real engine kit. Shipped systems are protected; user systems remain editable.</p>
            </div>
            <div className="dsc-system-table" role="list">
              {payload.kits.map((kit, index) => {
                const selected = kit.id === brand.id;
                return (
                  <button className="ds-n6 ds-paragraphs" key={kit.id} type="button" role="listitem" onClick={() => void openKit(kit.id)} data-selected={selected || undefined}>
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
                      {kit.id === payload.active ? "Painting the app" : kit.id === payload.opened ? "Opened" : kit.editable ? "Editable" : "Protected"}
                    </span>
                    <span className="dsc-system-state ds-n7 ds-leads">{selected ? <><Check /> Open</> : "Inspect"}</span>
                  </button>
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
              <div className="dsc-rung-picker ds-n8 ds-leads" aria-label="Preview size rung">
                {["XS", "S", "M", "L", "XL"].map((value) => <button key={value} type="button" data-active={rung === value || undefined} onClick={() => setRung(value)}>{value}</button>)}
              </div>
              <button className="dsc-scale-toggle ds-n8 ds-leads" type="button" data-active={scaled || undefined} onClick={() => setScaled((value) => !value)}>Downscale</button>
            </div>
            <div className="dsc-system-stage"><LiveDemo system={brand.id} grounds={model.grounds.length} components={COMPONENT_DOCS.length} /></div>
            <div className="dsc-logic-strip">
              {["Authored", "Measured", "Generated", "Inherited", "Consumed"].map((step, index) => <div key={step}><span className="ds-n8 ds-paragraphs">{String(index + 1).padStart(2, "0")}</span><strong className="ds-n7 ds-leads">{step}</strong><small className="ds-n8 ds-paragraphs">{["identity", "polarity", "vocabulary", "context", "components"][index]}</small></div>)}
            </div>
            <LogicAtlas model={model} rules={payload.rules} stages={payload.stages} />
          </section>

          <section id="components" className="dsc-section dsc-components" aria-labelledby="components-title">
            <div className="dsc-section-heading">
              <div><span className="ds-n8 ds-leads">COMPONENTS / 03</span><h2 id="components-title" className="ds-h3 ds-headings">Behavior stays local. Appearance inherits.</h2></div>
              <p className="ds-n5 ds-paragraphs">Every vendored primitive is indexed. Select one to inspect its purpose, contract, configuration and live states.</p>
            </div>
            <div className="dsc-component-layout">
              <aside className="dsc-component-menu" aria-label="Component index">
                {COMPONENT_GROUPS.map((group) => <div key={group.id}><h3 className="ds-n8 ds-leads">{group.label}</h3>{group.components.map((component) => <button className="ds-n7 ds-paragraphs" key={component.id} type="button" data-active={component.id === componentId || undefined} onClick={() => setComponentId(component.id)}>{component.name}</button>)}</div>)}
              </aside>
              <div className="dsc-component-document">
                <div className="dsc-component-title"><div><span className="ds-n8 ds-leads">{selectedComponent?.group.replace(/-/g, " ")}</span><h3 className="ds-h5 ds-headings">{selectedComponent?.name}</h3><p className="ds-n5 ds-paragraphs">{selectedComponent?.purpose}</p></div><Button variant="outline" size="sm" onClick={() => void copyComponentImport()}><Copy /> {copied ? "Copied" : "Copy import"}</Button></div>
                <div className="dsc-specimen" data-component={selectedComponent?.id}>
                  <div className="dsc-specimen-toolbar ds-n8 ds-leads"><span>INTERACTIVE PREVIEW</span><span>{brand.id} / {appearance} / {rung}</span></div>
                  <div className="dsc-specimen-stage"><ComponentSpecimen id={selectedComponent?.id ?? "button"} /></div>
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
          <div className="dsc-config-head"><div><span className="ds-n8 ds-leads">CONFIGURATION</span><strong className="ds-n6 ds-headings">{brand.id}</strong></div><div className="dsc-health ds-n8 ds-paragraphs"><i />{model.flags.length ? `${model.flags.length} findings` : "Ready"}<button type="button" onClick={() => setMobileConfigOpen(false)} aria-label="Close configuration"><X /></button></div></div>
          {error && <div className="dsc-error"><CircleAlert />{error}</div>}
          <ConfigurationPanel
            brand={brand}
            model={model}
            families={payload.families}
            appearance={appearance}
            rung={rung}
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
    </main>
  );
}

function EngineLoading() {
  return <main className="dsc-state"><span className="type-label">DESIGN ENGINE</span><h1 className="type-title">Resolving the system…</h1><p className="type-body">Loading kits, rules, fonts, emitted CSS and the resolved model in one pass.</p></main>;
}

function EngineFailure({ message }: { message: string }) {
  return <main className="dsc-state"><span className="type-label">DESIGN ENGINE</span><h1 className="type-title">The system did not resolve.</h1><p className="type-body">{message}</p></main>;
}
