import { createContext, useContext, useEffect, useState } from "react";
import { ChevronDown, Copy, RotateCcw, Save, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import type {
  BrandColourModel,
  BrandJson,
  Flag,
  FontFamily,
  ResolvedModel,
} from "@/components/design-engine/types";
import {
  assignFaceToBand,
  faceOptionsFor,
  facesFor,
  facesOfBands,
  TYPE_BANDS,
} from "@/lib/weights";
import { BrandColourSection } from "@/components/design-authoring/brand-colour";
import { AdditionalColours } from "@/components/design-authoring/additional-colours";
import {
  SIGNAL_ROLES,
  SIGNAL_ROLE_OF,
  SignalInkChoice,
  setSignalForeground,
} from "@/components/design-authoring/signal-ink";
import {
  BorderWeightFactors,
  EffectIntensities,
} from "@/components/design-authoring/size-fields";
import { MotionSpeeds } from "@/components/design-authoring/motion-fields";
import { BASE } from "@/lib/scale";
import {
  focusFlaggedField,
  placeFlags,
  severityOf,
  EVERY_SCENE,
  type AdviceAction,
  type AdviceScene,
} from "@/lib/flags";
import { Note } from "@/components/design-authoring/advice";
import { VerdictBand } from "@/components/design-authoring/verdict";
import { CODEX_VOCABULARY } from "./vocabulary";

/**
 * The host's reachable scene set, for the field wrappers that render advice.
 *
 * DEFAULT IS EVERY SCENE, which is what an unmounted-through-a-provider render
 * gets, and it is the honest default: a host that says nothing is claiming
 * nothing is missing. The panel below always provides the real answer.
 */
const ScenesContext = createContext<readonly AdviceScene[]>(EVERY_SCENE);

interface ConfigurationPanelProps {
  brand: BrandJson;
  model: ResolvedModel;
  /**
   * The findings this rail renders — the model's, WEARING the reader's
   * acknowledgements. Handed in rather than read off `model.flags`, because
   * "keep as is" belongs to the reader and the page owns that store.
   */
  flags: Flag[];
  /** "Keep as is" — the note's own close. Writes nothing to the brand. */
  onAcknowledge: (code: string) => void;
  /**
   * "Show me" — the page places the ground the finding is about, scrolls to the
   * section that owns the scene and opens the inspector on the slot. The panel
   * cannot do any of the three: all three are the page's own state.
   */
  onShow: (action: AdviceAction & { kind: "show" }) => void;
  /**
   * THE NAMED PLACES THE HOST CAN SEND THE CANVAS TO — wave 5, todo d049a0fe.
   *
   * DERIVED by the page from the stage map `onShow` dispatches on, never typed
   * twice. Two constants stating one fact is how the chip and the handler
   * disagreed in the first place; a scene missing from this array is a `show`
   * chip `adviceFor` never draws, and it is missing from it precisely because
   * the handler has nowhere to send it.
   */
  scenes: readonly AdviceScene[];
  families: FontFamily[];
  appearance: "light" | "dark";
  /** The preview rung; `null` is the configured one. Scene state, never saved. */
  rung: string | null;
  /**
   * THE VIEWER'S RUNG PER VIEWPORT CLASS — the configuration item, and the one
   * control on this rail that is NOT part of any design system.
   *
   * It is profile state, like the active kit, so it is deliberately not reached
   * through `onChange`/`brand`: a kit save must never carry it. `null` while the
   * boot payload has not answered yet.
   */
  viewportRungs: Record<string, string> | null;
  /** Where the mobile rung starts applying. SERVED, never a literal here. */
  mobileMaxPx: number | null;
  onViewportRung: (viewport: "desktop" | "mobile", rung: string) => void;
  scaled: boolean;
  editable: boolean;
  dirty: boolean;
  saving: boolean;
  onAppearance: (value: "light" | "dark") => void;
  onRung: (value: string | null) => void;
  onScaled: (value: boolean) => void;
  onChange: (brand: BrandJson) => void;
  onReset: () => void;
  onSave: () => void;
  onDuplicate: (id: string) => void;
}

/* THE LADDER IS THE ENGINE'S, READ OFF THE MODEL — it was typed here as
   eight names beside a canvas switch that also typed five. Two hand-written
   copies of a list `model.sizes.text_rungs` already publishes, and a brand
   that grows a rung would have been invisible to both. */
const FONT_SLOTS = [
  { key: "slot_1", ordinal: 1 },
  { key: "slot_2", ordinal: 2 },
  { key: "slot_3", ordinal: 3 },
  { key: "slot_4", ordinal: 4 },
] as const;

/**
 * The authored paths this panel actually renders a control for.
 *
 * Declared beside the fields rather than inferred, because "is there a field for
 * this address" is the question that decides whether a finding is placed or
 * listed — and a lookup that guesses would place a flag under a control that
 * does not write that value.
 */
/** The colour-role fields, so that group can ask "is anything of mine flagged". */
const COLOUR_ROLE_FIELDS = [
  "neutrals.black",
  "neutrals.white",
  "signals.red",
  "signals.green",
  "signals.blue",
  "signals.pink",
];

const PANEL_FIELDS = new Set([
  "brand.canonical",
  "brand.on_light",
  "brand.on_dark",
  "neutrals.black",
  "neutrals.white",
  "signals.red",
  "signals.green",
  "signals.blue",
  "signals.pink",
]);

/**
 * THIS PANEL'S OWN WORDS FOR THE THREE BRAND-COLOUR SLOTS.
 *
 * The shared block hands a label down with each colour decision, and the one it
 * carries is the other rail's — "Brand colour", "On light grounds". Copy is
 * chrome: this panel has always said "Canonical brand", "On light", "On dark",
 * its gates address the fields by those names, and a shared component silently
 * renaming a host's controls is the same failure as a shared component painting
 * in the host's stylesheet. Keyed by the authored SLOT, which is the one string
 * that means the same thing on both pages.
 */
const CODEX_COLOUR_LABELS: Record<string, string> = {
  "brand.canonical": "Canonical brand",
  "brand.on_light": "On light",
  "brand.on_dark": "On dark",
};

/** A dot per severity, so the tone is visible before the sentence is read. */
const SEVERITY_TONE: Record<string, string> = {
  "must-fix": "var(--color-status-error)",
  decide: "var(--color-status-warning)",
  note: "var(--color-status-info)",
};

/**
 * The engine's findings for one field, rendered where the value is typed.
 *
 * NEVER AN ERROR, ALWAYS A HINT — the owner: "Never error -> hint. Users need to
 * be able to configure what they want." The engine already agrees: a flag comes
 * back ALONGSIDE a fully resolved model, never instead of one, so `must-fix`
 * names how bad a finding is and not whether the value was accepted. The rail
 * was the only place in the chain that turned a hint into a refusal.
 *
 * WAVE 3: THE WRITTEN REMEDY, not the payload sentence. This printed
 * `flag.message` — the engine's own prose, which is written to a builder
 * reading JSON ("foreground #ffffff on ... is 3.68:1, under WCAG AA 4.5:1.
 * Flagged, not corrected."). `components/design-authoring/advice.tsx` restates
 * the five findings a user actually meets as a topic, a measurement and an
 * imperative naming the control, and falls through to the engine's own sentence
 * for everything else — so nothing is lost and the five best are gained. It is
 * the SAME component the other rail has always rendered, in this page's chrome.
 */
function FieldFlags({
  flags,
  id,
  colours,
  onAction,
}: {
  flags: Flag[];
  id: string;
  colours?: BrandColourModel | null;
  onAction?: (action: AdviceAction, flag: Flag) => void;
}) {
  /* WHICH SCENES THE HOST HAS, out of context rather than through six field
     wrappers. `Field`, `ColorField` and `ShadeField` all render a `FieldFlags`
     and none of them has any business knowing about scenes; a prop threaded
     through them would be six signatures carrying one fact. */
  const scenes = useContext(ScenesContext);
  if (!flags.length) return null;
  return (
    <ul className="dsc-field-flags ds-n8 ds-paragraphs" id={id}>
      {flags.map((flag) => (
        <li key={`${flag.code}-${flag.where}`} data-severity={severityOf(flag)}>
          <i style={{ background: SEVERITY_TONE[severityOf(flag)] }} />
          <Note
            flag={flag}
            colours={colours ?? null}
            onAction={onAction}
            vocabulary={CODEX_VOCABULARY}
            /* THE SCENES THIS HOST HAS — wave 5. A chip for one it lacks is
               never drawn, rather than drawn and inert. */
            scenes={scenes}
          />
        </li>
      ))}
    </ul>
  );
}

function numericWeight(descriptor: string, faces: Record<string, number> | null): number | null {
  if (!faces) return null;
  const normalized = descriptor.replace(/[-_ ]/g, "").toLowerCase();
  const match = Object.entries(faces).find(([name]) => name.replace(/[-_ ]/g, "").toLowerCase() === normalized);
  return match?.[1] ?? null;
}

function setPath<T>(source: T, path: string, value: unknown): T {
  const keys = path.split(".");
  const clone = (node: any, index: number): any => {
    const key = keys[index] as string;
    const next = index === keys.length - 1 ? value : clone(node?.[key] ?? {}, index + 1);
    return { ...node, [key]: next };
  };
  return clone(source, 0) as T;
}

/**
 * A collapsible group — and it OPENS ITSELF WHEN SOMETHING INSIDE IS FLAGGED.
 *
 * The other rail's `Collapse` has done this since its own rewrite, for the
 * reason Geist states about `/collapse`: nothing a person needs in order to
 * JUDGE the configuration may sit behind a disclosure. A finding is exactly
 * that. Shut, this section hid the engine's advice AND swallowed the verdict
 * band's action, which lands on a control that is in the DOM and not rendered.
 *
 * `flagged` rather than `open` so the two reasons stay apart: `open` is this
 * page's own default for a group, `flagged` is the engine having something to
 * say. A section is open when either is true.
 */
function PanelSection({ title, meta, open = false, flagged = false, children }: { title: string; meta?: string; open?: boolean; flagged?: boolean; children: React.ReactNode }) {
  return <details className="dsc-panel-section" open={open || flagged} data-flagged-section={flagged || undefined}><summary><span className="ds-n7 ds-leads">{title}</span>{meta && <small className="ds-n8 ds-paragraphs">{meta}</small>}<ChevronDown /></summary><div className="dsc-panel-section-body">{children}</div></details>;
}

/**
 * `beside` IS RENDERED BELOW THE FIELD, ON ITS OWN ROW — this panel's layout.
 *
 * A shared authored-field block hands over a control that belongs to the same
 * decision (the two-colour toggle) without saying where it goes. The other rail
 * puts it on the control's own row because it has 616px to spend; this panel is
 * 384px wide and describes its switches, so it keeps the shape every other
 * switch here already has. The node is the shared block's; the placement is
 * this page's, which is the whole point of the seam.
 */
function Field({ label, hint, flags, beside, besideHint, slot, colours, onAction, children }: { label: string; hint?: string; flags?: Flag[]; beside?: React.ReactNode; besideHint?: string; slot?: string; colours?: BrandColourModel | null; onAction?: (action: AdviceAction, flag: Flag) => void; children: React.ReactNode }) {
  const id = `dsc-flags-${label.replace(/\W+/g, "-").toLowerCase()}`;
  /* `data-control` IS WHAT THE VERDICT'S ACTION JUMPS TO. The band names the
     worst finding and its action moves focus to the control the flag is about;
     `lib/flags.ts::focusFlaggedField` finds the note by `data-flag` and then the
     field around it by what the field PUBLISHES. Publishing the authored slot —
     the one string that means the same thing on both pages — is what makes the
     shared jump work here without it knowing a single class name. */
  return <>
    <label className="dsc-panel-field" data-control={slot}><span className="ds-n7 ds-leads">{label}</span>{children}{hint && <small className="ds-n8 ds-paragraphs">{hint}</small>}{flags && <FieldFlags flags={flags} id={id} colours={colours} onAction={onAction} />}</label>
    {beside && <div className="dsc-panel-switch">{besideHint && <span><small className="ds-n8 ds-paragraphs">{besideHint}</small></span>}{beside}</div>}
  </>;
}

/**
 * A colour slot, with whatever the engine has to say about it underneath.
 *
 * `aria-invalid` AND `aria-describedby` COME FROM THE SAME SOURCE as the visible
 * sentences, so a screen reader and an eye cannot disagree about which field is
 * flagged. `aria-invalid` is set only for `must-fix`: `decide` and `note` are
 * advice about a choice that was accepted, and marking them invalid would be the
 * rail once again reporting a hint as a refusal.
 */
function ColorField({ label, hint, value, flags = [], beside, besideHint, slot, colours, onAction, onChange }: { label: string; hint?: string; value: string; flags?: Flag[]; beside?: React.ReactNode; besideHint?: string; slot?: string; colours?: BrandColourModel | null; onAction?: (action: AdviceAction, flag: Flag) => void; onChange: (value: string) => void }) {
  const id = `dsc-flags-${label.replace(/\W+/g, "-").toLowerCase()}`;
  const invalid = flags.some((flag) => severityOf(flag) === "must-fix");
  return <Field label={label} hint={hint} flags={flags} beside={beside} besideHint={besideHint} slot={slot} colours={colours} onAction={onAction}><span className="dsc-color-field"><input type="color" value={value} onChange={(event) => onChange(event.target.value)} aria-label={`${label} color`} /><Input value={value} aria-label={label} aria-invalid={invalid || undefined} aria-describedby={flags.length ? id : undefined} onChange={(event) => onChange(event.target.value)} /></span></Field>;
}

function NumberField({ label, value, min, max, step = 1, onChange }: { label: string; value: number; min?: number; max?: number; step?: number; onChange: (value: number) => void }) {
  return <Field label={label}><Input type="number" value={value} min={min} max={max} step={step} onChange={(event) => onChange(Number(event.target.value))} /></Field>;
}

export function ConfigurationPanel(props: ConfigurationPanelProps) {
  const { brand, model, flags, families, onAcknowledge, onShow, scenes, onChange } = props;
  const [copyId, setCopyId] = useState(`${brand.id}-custom`);
  const set = (path: string, value: unknown) => onChange(setPath(brand, path, value));

  // EVERY FLAG IS PLACED, AND THE ONES THAT ARE NOT ARE STILL SHOWN. A `where`
  // this panel has no field for must never be dropped silently — that is how a
  // finding becomes invisible the day the engine learns a new address. The
  // leftovers render as a list at the foot of the panel instead.
  //
  // THE ADDRESSING IS `lib/flags.ts` NOW, not this file. It lived here as
  // `addressesOf` + `WHERE_ALIASES` and, in its other half, as
  // `rail.tsx::slotsNamedBy` — two implementations of "which field does this
  // flag's `where` name", which is exactly the class the authoring-core
  // migration ends. `PANEL_FIELDS` stays: WHICH controls this panel draws is
  // this page's own fact and the only input the shared function takes.
  const { placed, unplaced } = placeFlags(flags, PANEL_FIELDS);
  const flagsAt = (path: string) => placed.get(path) ?? [];

  /**
   * A finding's ONE action, performed — and only the engine's own numbers.
   *
   * `set-shade` and `apply-both` carry an amount the engine proposed
   * (`poles.{pole}.proposed_shade`), so applying one writes an AMOUNT and
   * clears the literal, which is his colour rule satisfied by construction: the
   * shade is recomputed from the canonical through `fork.py::shade_for_own_colour`
   * whenever the canonical moves. `explain` jumps to the control.
   *
   * `acknowledge` is the note's own close and it is the PAGE's store, handed
   * down as `onAcknowledge`.
   *
   * `show` IS ANSWERED NOW — wave 5, todo d049a0fe, and it was answered in BOTH
   * directions because one alone leaves the class open. The page honours the
   * two scenes it has (`onShow` below places the ground, scrolls to the section
   * and opens the inspector), and `REACHABLE_SCENES` stops `adviceFor` from
   * drawing a chip for the one it has not. Either half alone still ships an
   * inert control: a handler that cannot reach `tokens`, or a hidden chip with
   * no handler behind the two that are reachable.
   */
  const onFlagAction = (action: AdviceAction, flag: Flag) => {
    if (action.kind === "set-shade") {
      let next = setPath(brand, `brand.shade_${action.pole}`, action.amount);
      next = setPath(next, `brand.on_${action.pole}`, null);
      onChange(next);
    } else if (action.kind === "apply-both") {
      let next = setPath(brand, "brand.shade_light", action.light);
      next = setPath(next, "brand.shade_dark", action.dark);
      next = setPath(next, "brand.on_light", null);
      next = setPath(next, "brand.on_dark", null);
      onChange(next);
    } else if (action.kind === "explain") {
      focusFlaggedField(action.slot);
    } else if (action.kind === "show") {
      onShow(action);
    } else if (action.kind === "acknowledge") {
      /* "KEEP AS IS" — matrix row 30, and it is the page's store, not the
         brand's. It was missing here and the control still rendered: the note
         drew its quiet button, the click arrived, and nothing happened. That is
         exactly the shape the charter names — a control whose precondition
         lives one layer down cannot know it is inert. Either lift the condition
         to the control or delete the control; the capability is ruled, so the
         condition is lifted. */
      onAcknowledge(flag.code);
    }
  };
  /* THE SAME FACE RESOLUTION THE OTHER RAIL USES. This read `brand.font.faces`
     alone, so a family this machine measures but the stored kit does not spell
     offered nothing here and a full quad next door. `facesFor` prefers the
     machine's measurement and falls back to the kit's copy. */
  const faces = facesFor(families, brand.font);
  const faceOptions = faceOptionsFor(faces);
  /* What each band WEARS — derived, never stored. The slot ordinal is the
     model and stays the Figma contract; the face is what a person picks. */
  const bandFaces = facesOfBands(brand.font, brand.weights);
  const slotName = (ordinal: number) => {
    const descriptor = brand.font[`slot_${ordinal}` as keyof typeof brand.font];
    const weight = numericWeight(String(descriptor), brand.font.faces);
    return `Slot ${ordinal} — ${descriptor}${weight === null ? "" : ` · ${weight}`}`;
  };

  useEffect(() => setCopyId(`${brand.id}-custom`), [brand.id]);

  return <ScenesContext.Provider value={scenes}><div className="dsc-config-content ds-n7 ds-paragraphs">
    {/* THE VERDICT — matrix row 29, and the first thing this rail says.
        It said `2 findings` beside a green dot, which is a COUNT: it does not
        state how bad, about what, or what to do. The band is
        `components/design-authoring/verdict.tsx`, the same one the other rail
        has always carried, wearing this page's chrome and this page's box.
        Its action moves focus to the control the worst finding names. */}
    <VerdictBand
      flags={flags}
      model={model}
      onReview={(flag) => flag && focusFlaggedField(flag.code)}
      vocabulary={CODEX_VOCABULARY}
      className="dsc-verdict"
    />
    <div className="dsc-panel-actions">
      <Button size="sm" variant="ghost" onClick={props.onReset} disabled={!props.dirty}><RotateCcw /> Reset</Button>
      {props.editable
        ? <Button size="sm" data-testid="save-kit" data-saving={props.saving} onClick={props.onSave} disabled={!props.dirty || props.saving}><Save />{props.saving ? "Saving…" : "Save system"}</Button>
        : <span className="dsc-protected ds-n8 ds-leads">Protected source</span>}
    </div>

    {/* ── THE VIEWER'S RUNG — the configuration item ────────────────────
        His ruling, 2026-09-17: "The font sizes are everywhere the same. The
        font rungs aren't. ... standard for the viewer: if desktop standard is
        L, for mobile it might be M." And: "okuro-design-system has a
        configuration item for this. Standard can be defined."

        IT SITS ABOVE "Preview" AND OUTSIDE THE KIT FORM, and both placements
        are the point. Every other control in this rail edits the OPEN design
        system and is saved with it; this one belongs to no system at all and
        applies to all of them. Putting it in the form would make "save this
        design system" also mean "resize every design system", which is the
        opposite of the ruling. Its meta line says so rather than relying on
        the reader to infer it from the position.

        The rung right below it, in "Preview", is the scene's and is thrown
        away on reload. Two rungs, one rail, and the mode each belongs to is
        what the section headers are for. */}
    <PanelSection title="Viewer" meta="saved · applies to every system" open>
      {(["desktop", "mobile"] as const).map((viewport) => (
        <div className="dsc-panel-field" key={viewport}>
          <span className="ds-n7 ds-leads">{viewport === "desktop" ? "Desktop rung" : "Mobile rung"}</span>
          <select
            className="ds-n7 ds-paragraphs"
            data-testid={`viewport-rung-${viewport}`}
            disabled={!props.viewportRungs}
            value={props.viewportRungs?.[viewport] ?? ""}
            onChange={(event) => props.onViewportRung(viewport, event.target.value)}
          >
            {!props.viewportRungs && <option value="">…</option>}
            {model.sizes.text_rungs.map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
          <small className="ds-n8 ds-paragraphs">
            {viewport === "desktop"
              ? "What every design system opens on. Sizes follow this, not the kit."
              : `Below ${props.mobileMaxPx ?? "the mobile"}px. Equal to desktop means no mobile rule at all.`}
          </small>
        </div>
      ))}
    </PanelSection>

    <PanelSection title="Preview" meta="not saved" open>
      <div className="dsc-panel-field"><span className="ds-n7 ds-leads">Appearance</span><div className="dsc-panel-segments">{(["light", "dark"] as const).map((value) => <button className="ds-n7 ds-paragraphs" key={value} type="button" data-active={props.appearance === value || undefined} onClick={() => props.onAppearance(value)}>{value}</button>)}</div></div>
      <div className="dsc-panel-field"><span className="ds-n7 ds-leads">Root size rung</span><select className="ds-n7 ds-paragraphs" value={props.rung ?? "default"} onChange={(event) => props.onRung(event.target.value === "default" ? null : event.target.value)}>{[null, ...model.sizes.text_rungs].map((value) => <option key={value ?? "default"} value={value ?? "default"}>{value ?? "Default"}</option>)}</select><small className="ds-n8 ds-paragraphs">Inherited by text and components in the full page.</small></div>
      <div className="dsc-panel-switch"><span><b className="ds-n7 ds-leads">Downscale context</b><small className="ds-n8 ds-paragraphs">Text shifts one rung; components shift three.</small></span><Switch checked={props.scaled} onCheckedChange={props.onScaled} /></div>
    </PanelSection>

    {/* ONE BRAND COLOUR BLOCK, SHARED — wave 2, matrix row 12.
        This panel used to answer the same three questions itself, and answered
        two of them differently: a new canonical left the literal `on_light` /
        `on_dark` standing (so every branded figure kept painting the colour the
        brand no longer had), and the two-colour toggle wrote the flag alone
        (so a brand could claim one colour while storing three). The block that
        holds those records is `components/design-authoring/brand-colour.tsx`
        now, and it takes THIS page's chrome: `ColorField` for a colour row,
        this panel's own findings list under a pole, `CODEX_VOCABULARY` for
        every class it paints. */}
    <PanelSection title="Identity" meta="authored" open>
      <BrandColourSection
        brand={brand}
        colours={model.brand_colours}
        threshold={model.threshold}
        vocabulary={CODEX_VOCABULARY}
        flagsFor={flagsAt}
        onChange={onChange}
        renderColour={(field) => (
          <ColorField
            key={field.slot}
            slot={field.slot}
            label={CODEX_COLOUR_LABELS[field.slot] ?? field.label}
            hint={field.calm}
            value={field.value}
            flags={field.flags}
            beside={field.beside}
            besideHint={field.besideHint}
            colours={model.brand_colours}
            onAction={onFlagAction}
            onChange={field.onChange}
          />
        )}
        renderHelp={(help) => (
          <div className="dsc-pole-help">
            <small className="ds-n8 ds-paragraphs">{help.calm}</small>
            <FieldFlags flags={help.flags} id={`dsc-flags-${help.slot.replace(/\W+/g, "-")}`} colours={model.brand_colours} onAction={onFlagAction} />
          </div>
        )}
      />
    </PanelSection>

    <PanelSection title="Color roles" meta={`6 authored · ${brand.brand.additional.length} extra`} flagged={COLOUR_ROLE_FIELDS.some((field) => flagsAt(field).length > 0)}>
      <div className="dsc-panel-pair"><ColorField label="Black" slot="neutrals.black" value={brand.neutrals.black} flags={flagsAt("neutrals.black")} colours={model.brand_colours} onAction={onFlagAction} onChange={(value) => set("neutrals.black", value)} /><ColorField label="White" slot="neutrals.white" value={brand.neutrals.white} flags={flagsAt("neutrals.white")} colours={model.brand_colours} onAction={onFlagAction} onChange={(value) => set("neutrals.white", value)} /></div>
      {/* THE INK OVERRIDE SITS ON THE ROLE IT IS ABOUT — register rule 7, his
          ruling of 2026-08-18. A brand may author a foreground per role; absent
          means compute it, and the engine flags the exception rather than
          refusing it. This panel had no control for a RULED capability, so the
          only way to state it was the other page. The two candidates and all
          three numbers come from `api.py::_signal_inks`; nothing here measures
          a contrast of its own. */}
      {SIGNAL_ROLES.map(([slot, label]) => {
        const role = SIGNAL_ROLE_OF[slot];
        const ink = role ? model.brand_colours.signals?.[role] : undefined;
        return <div className="dsc-signal-role" key={slot}>
          <ColorField label={label} slot={`signals.${slot}`} value={brand.signals[slot]} flags={flagsAt(`signals.${slot}`)} colours={model.brand_colours} onAction={onFlagAction} onChange={(value) => set(`signals.${slot}`, value)} />
          {ink && <SignalInkChoice role={role as string} ink={ink} vocabulary={CODEX_VOCABULARY} onChange={(value) => onChange(setSignalForeground(brand, role as string, value))} />}
        </div>;
      })}
      {/* THE ADDITIONAL COLOURS — ten authored slots of register rule 12, and
          this page reached none of them. Each one joins the emitted vocabulary
          with the ink the engine computes for it. */}
      <div className="dsc-type-subsection">
        <div className="dsc-type-subsection-head"><span className="ds-n8 ds-leads">ADDITIONAL COLOURS</span><small className="ds-n8 ds-paragraphs">Free palette entries for slides and charts. Each gets the same foreground calculation.</small></div>
        <AdditionalColours entries={brand.brand.additional} resolved={model.additional} max={model.brand_colours.max_additional} vocabulary={CODEX_VOCABULARY} onChange={(next) => set("brand.additional", next)} />
      </div>
    </PanelSection>

    <PanelSection title="Typography" meta={`${brand.font.family} · 4 roles`}>
      <Field label="Typeface"><select value={brand.font.family} onChange={(event) => {
        const family = families.find((item) => item.family === event.target.value);
        let next = setPath(brand, "font.family", event.target.value);
        next = setPath(next, "font.faces", family?.faces ?? null);
        if (family?.quad.length === 4) {
          family.quad.forEach((face, index) => { next = setPath(next, `font.slot_${index + 1}`, face); });
        }
        onChange(next);
      }}>{families.map((family) => <option key={family.family} value={family.family}>{family.family}</option>)}</select></Field>
      {/* THE KIT-LEVEL RUNG PAIR USED TO SIT HERE, and its removal is the other
          half of his 2026-09-17 ruling rather than fallout from it. Two selects
          wrote `defaults.rung.desktop` / `.mobile` onto the BRAND, which is
          exactly the thing that made type jump when you changed design system.
          The rung is now the viewer's, once for every system, and its control is
          the "Viewer" section at the top of this rail. `Brand` has no field left
          for a kit to state one with. */}
      <div className="dsc-type-subsection">
        <div className="dsc-type-subsection-head"><span className="ds-n8 ds-leads">WEIGHT SLOTS</span><small className="ds-n8 ds-paragraphs">Each slot is named by its selected font face.</small></div>
        <div className="dsc-font-slots">
          {FONT_SLOTS.map((slot) => {
            const descriptor = brand.font[slot.key];
            const weight = numericWeight(descriptor, brand.font.faces);
            return <label className="dsc-font-slot" key={slot.key}>
              <span><b className="ds-n7 ds-leads">Slot {slot.ordinal} — {descriptor}</b><small className="ds-n8 ds-paragraphs">Assigned font face{weight === null ? "" : ` · CSS ${weight}`}</small></span>
              {faceOptions
                ? <select className="ds-n7 ds-paragraphs" value={descriptor} onChange={(event) => set(`font.${slot.key}`, event.target.value)}>{faceOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select>
                : <Input value={descriptor} aria-label={`Slot ${slot.ordinal} face name`} onChange={(event) => set(`font.${slot.key}`, event.target.value)} />}
            </label>;
          })}
        </div>
      </div>
      <div className="dsc-type-subsection">
        <div className="dsc-type-subsection-head"><span className="ds-n8 ds-leads">ROLE ASSIGNMENT</span><small className="ds-n8 ds-paragraphs">Each role wears a font face and its case.</small></div>
        {/* A BAND PICKS A FACE, NOT A SLOT ORDINAL — the owner's model of
            2026-09-06, which this panel did not have. It wrote
            `weights.{role} = <ordinal>`, i.e. it asked a person to know the
            indirection exists and to make two writes for one decision. The
            other rail has called `assignFaceToBand` since that ruling; the
            function does both writes AND re-sorts the four slots descending,
            because `schema.lint` raises `font.weights-not-monotone` otherwise.
            THE FALLBACK IS THE SAME ONE THE OTHER RAIL KEEPS: a family with no
            measured faces has no honest picker to offer, so the ordinal stays
            visible rather than inventing descriptors. */}
        {TYPE_BANDS.map((role) => <div className="dsc-role-row" key={role}><span className="ds-n7 ds-paragraphs">{role}</span>{faceOptions
          ? <select className="ds-n8 ds-paragraphs" aria-label={`${role} face`} value={bandFaces[role]} onChange={(event) => {
              const next = assignFaceToBand(brand.font, brand.weights, role, event.target.value, faces);
              onChange({ ...brand, font: next.font, weights: next.weights });
            }}>{faceOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select>
          : <select className="ds-n8 ds-paragraphs" aria-label={`${role} weight slot`} value={String(brand.weights[role])} onChange={(event) => set(`weights.${role}`, Number(event.target.value))}>{FONT_SLOTS.map((slot) => <option key={slot.ordinal} value={slot.ordinal}>{slotName(slot.ordinal)}</option>)}</select>}<Switch checked={brand.case[role]} onCheckedChange={(value) => set(`case.${role}`, value)} aria-label={`${role} uppercase`} /></div>)}
      </div>
      <p className="dsc-panel-note">8 rungs × 21 text roles form the mother table. Components use 9 rungs × 16 roles; frame context selects the row.</p>
    </PanelSection>

    <PanelSection title="Geometry" meta="borders · radius · shadows">
      <div className="dsc-panel-pair"><NumberField label="Radius base" value={brand.radius.base} min={1} onChange={(value) => set("radius.base", value)} /><NumberField label="Border opacity" value={brand.border.opacity} min={0} max={1} step={.05} onChange={(value) => set("border.opacity", value)} /></div>
      <div className="dsc-panel-triple">{(["s_factor", "m_factor", "l_factor"] as const).map((key) => <NumberField key={key} label={key.replace("_factor", "").toUpperCase()} value={brand.radius[key]} min={0} step={.25} onChange={(value) => set(`radius.${key}`, value)} />)}</div>
      <div className="dsc-panel-pair"><NumberField label="Solid shadow" value={brand.shadow_anchors.solid} step={.05} onChange={(value) => set("shadow_anchors.solid", value)} /><NumberField label="Blurred shadow" value={brand.shadow_anchors.blurred} step={.05} onChange={(value) => set("shadow_anchors.blurred", value)} /></div>
      {/* THE FOUR BORDER WEIGHTS — register rule 12's authored ladder, which
          this panel never offered: it had the opacity and nothing else, so four
          of the six numbers a brand authors about a border were reachable from
          the other page only. Shown in px, saved as factors. */}
      <div className="dsc-type-subsection">
        <div className="dsc-type-subsection-head"><span className="ds-n8 ds-leads">BORDER WEIGHTS</span><small className="ds-n8 ds-paragraphs">Pixels here, factors of the system base in the kit.</small></div>
        <div className="dsc-panel-quad"><BorderWeightFactors factors={brand.border.weight_factors} base={BASE} vocabulary={CODEX_VOCABULARY} onChange={(next) => onChange({ ...brand, border: { ...brand.border, weight_factors: next as BrandJson["border"]["weight_factors"] } })} /></div>
      </div>
    </PanelSection>

    {/* EFFECTS — register rule 12's intensity quads. This panel had no control
        for any of them; the names and their fields come from the brand, so a
        brand that grows a fifth intensity renders without this file changing. */}
    <PanelSection title="Effects" meta={`${Object.keys(brand.effects).length} intensities`}>
      <EffectIntensities effects={brand.effects} base={BASE} vocabulary={CODEX_VOCABULARY} onChange={(next) => onChange({ ...brand, effects: next })} />
    </PanelSection>

    <PanelSection title="Motion" meta={`${Object.keys(brand.motion.speeds).length} speeds · ${Object.keys(brand.motion.curves).length} curves`}>
      <Field label="Default speed"><select value={brand.motion.default_speed} onChange={(event) => set("motion.default_speed", event.target.value)}>{Object.keys(brand.motion.speeds).map((value) => <option key={value}>{value}</option>)}</select></Field>
      <Field label="Default curve"><select value={brand.motion.default_curve} onChange={(event) => set("motion.default_curve", event.target.value)}>{Object.keys(brand.motion.curves).map((value) => <option key={value}>{value}</option>)}</select></Field>
      <Field label="Signature"><Input value={brand.motion.signature ?? ""} placeholder="None" onChange={(event) => set("motion.signature", event.target.value || null)} /></Field>
      {/* THE SPEED SCALE — the VALUES behind the keys the three selects above
          choose between. This panel authored `default_speed` and never the
          durations it names, so the one authored group of register rule 12 that
          no control on this page could reach was the scale itself.
          `onChange` hands back the WHOLE map, which is this folder's shape for a
          collection. Measured, against the obvious justification: a dotted
          `set("motion.speeds.medium", …)` would in fact be byte-identical here,
          because gotcha d1911006's dialect split is about ARRAYS and both
          `setPath`s spread an OBJECT the same way. The whole-map write is kept
          because it is the seam's rule and it cannot be miswired, not because a
          divergence lives at this address. */}
      <div className="dsc-type-subsection">
        <div className="dsc-type-subsection-head"><span className="ds-n8 ds-leads">SPEED SCALE</span><small className="ds-n8 ds-paragraphs">Durations for animation. Hover, focus and press never run at these — those use the system's own interaction constant.</small></div>
        <MotionSpeeds speeds={brand.motion.speeds as Record<string, number>} vocabulary={CODEX_VOCABULARY} onChange={(next) => onChange({ ...brand, motion: { ...brand.motion, speeds: next } })} />
      </div>
    </PanelSection>

    {/* THE COUNT STAYS HERE, where a count is the right thing: this section is
        the model's own accounting. The JUDGEMENT is the band at the top. */}
    <PanelSection title="System logic" meta={flags.length ? `${flags.length} findings` : "no findings"}>
      {unplaced.length > 0 && <ul className="dsc-field-flags ds-n8 ds-paragraphs">
        {unplaced.map((flag) => <li key={`${flag.code}-${flag.where}`} data-severity={severityOf(flag)}><i style={{ background: SEVERITY_TONE[severityOf(flag)] }} /><span><b>{flag.where}</b> — {flag.message}</span></li>)}
      </ul>}
      <dl className="dsc-provenance"><div><dt>Authored</dt><dd>{model.authored.length} inputs</dd></div><div><dt>Resolved</dt><dd>{model.grounds.length} grounds</dd></div><div><dt>Threshold</dt><dd>{model.threshold}</dd></div><div><dt>Generated</dt><dd>{model.requests.length} requests</dd></div><div><dt>Type table</dt><dd>{Object.keys(model.sizes.text_rungs).length} rungs</dd></div></dl>
      <ol className="dsc-stage-list">{["Authored identity", "Measured polarity", "Generated vocabulary", "Inherited frame", "Consumed component"].map((label, index) => <li key={label}><span>{index + 1}</span>{label}</li>)}</ol>
    </PanelSection>

    {!props.editable && <div className="dsc-duplicate" data-duplicate-flow><span><Sparkles />Create an editable branch</span><p>Shipped systems stay immutable. Your current draft can be saved as a user system.</p><Input value={copyId} onChange={(event) => setCopyId(event.target.value)} /><Button onClick={() => props.onDuplicate(copyId)} disabled={!copyId.trim() || props.saving}><Copy />{props.saving ? "Creating…" : "Duplicate draft"}</Button></div>}
  </div></ScenesContext.Provider>;
}
